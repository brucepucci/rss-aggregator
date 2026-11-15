"""Guarded local FreshRSS lifecycle, independent of the command-line adapter."""

import fcntl
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.error import URLError
from urllib.request import urlopen

Operation = Literal["setup", "start", "status", "fetch", "verify", "replace", "stop"]

MACHINE = "rss-aggregator"
APP = "rss-aggregator-freshrss"
VOLUME = "rss-aggregator-data"
CHECK = "rss-aggregator-check"
LABEL = "io.rss-aggregator.owner"


class RuntimeFailure(Exception):
    """Report a lifecycle guard or external command failure without CLI coupling."""


class Runtime:
    """Manage one checkout's resources using its independently resolved state.

    Parameters
    ----------
    root : Path, optional
        Checkout containing the pinned image and FreshRSS configuration. Defaults
        to the checkout containing this editable source file, not the caller's cwd.
    state_dir : Path, optional
        Ownership and audit state. Overrides RSS_STATE_DIR and then root/.local.
        Relative explicit paths and environment values resolve against the cwd.
    """

    def __init__(self, root: Path | None = None, state_dir: Path | None = None) -> None:
        """Resolve instance paths without inspecting or changing native resources."""
        self.root = (
            (root if root is not None else Path(__file__).resolve().parents[1])
            .expanduser()
            .resolve()
        )
        self.state_dir = (
            Path(
                state_dir
                if state_dir is not None
                else os.environ.get("RSS_STATE_DIR", self.root / ".local")
            )
            .expanduser()
            .resolve()
        )

    def run(self, operation: Operation) -> dict[str, Any]:
        """Execute one public operation under the nonblocking project lock.

        Returns
        -------
        dict[str, Any]
            JSON-compatible operation-specific results, with diagnostics on stderr.

        Raises
        ------
        RuntimeFailure
            If the operation is unknown, another command holds the lock, or a
            resource safety guard fails. OS and configuration errors propagate.
        """
        handlers = {
            "setup": self._setup,
            "start": self._start,
            "status": self._status,
            "fetch": self._fetch,
            "verify": self._verify,
            "replace": self._replace,
            "stop": self._stop,
        }
        if operation not in handlers:
            raise RuntimeFailure(f"Unknown operation: {operation}")
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.state_dir / "command.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeFailure(
                    "Another project command is running; wait for it to finish."
                ) from error
            return handlers[operation]()

    def _diagnostic(self, message: str) -> None:
        """Send human-readable progress or captured command chatter to stderr."""
        print(message, file=sys.stderr)

    def _run(
        self, *args: str, check: bool = True, input: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Capture an external command with Homebrew mutation safeguards.

        Captured stdout remains available for parsing; callers explicitly send
        informational output to diagnostics rather than the JSON result stream.
        """
        env = dict(
            os.environ,
            HOMEBREW_NO_AUTO_UPDATE="1",
            HOMEBREW_NO_INSTALL_UPGRADE="1",
            HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK="1",
            HOMEBREW_NO_INSTALL_CLEANUP="1",
        )
        try:
            result = subprocess.run(
                args, text=True, capture_output=True, env=env, input=input, check=False
            )
        except OSError as error:
            raise RuntimeFailure(f"{args[0]} unavailable: {error}") from error
        if check and result.returncode:
            raise RuntimeFailure(
                f"{args[0]} failed ({result.returncode}): {result.stderr.strip()}"
            )
        return result

    def _save(self, name: str, value: Any) -> None:
        """Atomically persist private JSON audit or ownership state."""
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = self.state_dir / name
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(target)

    def _image(self) -> dict[str, Any]:
        """Read this checkout's pinned image configuration only when needed."""
        return json.loads((self.root / "config/image.json").read_text())

    def _packages(self) -> dict[str, str]:
        """Inventory installed Homebrew formula versions for mutation checks."""
        return dict(
            line.split(" ", 1)
            for line in self._run(
                "brew", "list", "--formula", "--versions"
            ).stdout.splitlines()
        )

    def _bootstrap(self) -> None:
        """Install only missing Podman after recording and checking host changes."""
        if not shutil.which("brew"):
            raise RuntimeFailure(
                "Homebrew is required; install it from https://brew.sh first."
            )
        before = self._packages()
        self._save(
            "host-latest.json"
            if (self.state_dir / "host-before.json").exists()
            else "host-before.json",
            {
                "packages": before,
                "casks": self._run("brew", "list", "--cask", "--versions").stdout,
                "dependencies": self._run("brew", "deps", "--installed").stdout,
                "podman_formula": self._run(
                    "brew", "info", "--json=v2", "podman"
                ).stdout,
            },
        )
        if "podman" in before:
            return
        result = self._run("brew", "install", "--dry-run", "podman")
        preview = result.stdout + result.stderr
        self._diagnostic(preview)
        self._save("install-preview.json", {"output": preview})
        planned = set(re.findall(r"(?m)^([a-zA-Z0-9@+_.-]+)\s+\d", preview))
        if (
            planned.intersection(before)
            or "upgrade" in preview.lower()
            or "reinstall" in preview.lower()
        ):
            raise RuntimeFailure(
                "Installation preview changes existing packages; user approval required."
            )
        try:
            if "podman" not in before:
                self._diagnostic(self._run("brew", "install", "podman").stdout)
        finally:
            after = self._packages()
            previous = self.state_dir / "host-additions.json"
            additions = json.loads(previous.read_text()) if previous.exists() else {}
            additions.update(
                {name: version for name, version in after.items() if name not in before}
            )
            self._save("host-additions.json", additions)
        changed = [name for name in before if before[name] != after.get(name)]
        if changed:
            raise RuntimeFailure(
                "Unexpected host package changes: " + ", ".join(changed)
            )

    def _json_run(self, *args: str) -> Any:
        """Decode JSON returned by a checked external command."""
        return json.loads(self._run(*args).stdout)

    def _inventory(self, resources: bool = False) -> dict[str, Any]:
        """Inventory Podman without starting machines or changing defaults."""
        if not shutil.which("podman"):
            return {"machines": [], "connections": [], "podman": "not installed"}
        snapshot = {
            "machines": self._json_run("podman", "machine", "list", "--format", "json"),
            "connections": self._json_run(
                "podman", "system", "connection", "list", "--format", "json"
            ),
        }
        if resources:
            snapshot["resources"] = {}
            running = {m["Name"] for m in snapshot["machines"] if m.get("Running")}
            for connection in snapshot["connections"]:
                name = connection["Name"]
                if not connection.get("IsMachine", False) or (
                    name not in running and name.removesuffix("-root") not in running
                ):
                    snapshot["resources"][name] = (
                        "Not queried: machine stopped or non-machine connection."
                    )
                    continue
                results = {}
                for kind, args in [
                    ("containers", ("ps", "--all", "--format", "json")),
                    ("volumes", ("volume", "ls", "--format", "json")),
                    ("images", ("images", "--format", "json")),
                ]:
                    result = self._run(
                        "podman", "--connection", name, *args, check=False
                    )
                    results[kind] = (
                        json.loads(result.stdout)
                        if result.returncode == 0
                        else "Unavailable"
                    )
                snapshot["resources"][name] = results
        return snapshot

    def _setup(self) -> dict[str, Any]:
        """Create a rootless project VM without starting or adopting resources."""
        snapshot = self._inventory(resources=True)
        if not (self.state_dir / "owner.json").exists():
            if any(m["Name"] == MACHINE for m in snapshot["machines"]):
                raise RuntimeFailure(
                    "Refusing unowned machine rss-aggregator; do not delete or adopt it automatically."
                )
            if any(
                c["Name"] in (MACHINE, MACHINE + "-root")
                for c in snapshot["connections"]
            ):
                raise RuntimeFailure(
                    "Refusing unowned project connection; resolve the name collision manually."
                )
        else:
            self._owned()
        self._save(
            "podman-latest.json"
            if (self.state_dir / "podman-before.json").exists()
            else "podman-before.json",
            snapshot,
        )
        self._bootstrap()
        snapshot = self._inventory(resources=True)
        self._save("podman-pre-machine.json", snapshot)
        created = False
        if not (self.state_dir / "owner.json").exists():
            # A newly installed CLI can reveal resources absent from the first inventory.
            if any(m["Name"] == MACHINE for m in snapshot["machines"]) or any(
                c["Name"] in (MACHINE, MACHINE + "-root")
                for c in snapshot["connections"]
            ):
                raise RuntimeFailure(
                    "Refusing unowned project machine/connection discovered after CLI installation."
                )
            self._run(
                "podman",
                "machine",
                "init",
                "--cpus",
                "2",
                "--memory",
                "2048",
                "--disk-size",
                "20",
                "--rootful=false",
                "--update-connection=false",
                MACHINE,
            )
            machine = self._json_run("podman", "machine", "inspect", MACHINE)[0]
            connections = self._inventory()["connections"]
            for existing in snapshot["connections"]:
                current = next(
                    (c for c in connections if c["Name"] == existing["Name"]), {}
                )
                if any(
                    existing.get(key) != current.get(key)
                    for key in ("URI", "Identity", "Default")
                ):
                    raise RuntimeFailure(
                        "Podman changed a pre-existing connection/default; inspect locally before continuing."
                    )
            connection = next(c for c in connections if c["Name"] == MACHINE)
            self._save(
                "owner.json",
                {
                    "token": str(uuid.uuid4()),
                    "machine": machine,
                    "connection": connection,
                },
            )
            created = True
        self._diagnostic("Project machine is set up. Setup does not start or stop it.")
        return {"machine": MACHINE, "created": created}

    def _owned(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Verify recorded VM and connection identity before using resources."""
        path = self.state_dir / "owner.json"
        if not path.exists():
            raise RuntimeFailure("Project is not set up. Run scripts/rss setup.")
        owner = json.loads(path.read_text())
        current = self._json_run("podman", "machine", "inspect", MACHINE)[0]
        for key in ("Name", "Created", "ConfigDir", "SSHConfig", "Rootful"):
            if current.get(key) != owner["machine"].get(key):
                raise RuntimeFailure(
                    "Project machine identity changed; refusing to modify it."
                )
        connections = self._inventory()["connections"]
        connection = next((c for c in connections if c["Name"] == MACHINE), {})
        for key in ("URI", "Identity"):
            if connection.get(key) != owner["connection"].get(key):
                raise RuntimeFailure(
                    "Project connection identity changed; refusing to modify it."
                )
        return owner, current

    def _start(self) -> dict[str, Any]:
        """Start serving only after ownership, image, and effective PHP gates."""
        owner, machine = self._owned()
        if machine["State"] not in ("stopped", "running"):
            raise RuntimeFailure(
                "Project VM is "
                + machine["State"]
                + "; wait and inspect before retrying."
            )
        try:
            port = int(os.environ.get("RSS_PORT", "8080"))
        except ValueError as error:
            raise RuntimeFailure(
                "RSS_PORT must be an integer between 1024 and 65535."
            ) from error
        if not 1024 <= port <= 65535:
            raise RuntimeFailure("RSS_PORT must be between 1024 and 65535.")
        if machine["State"] != "running":
            conflicts = [
                m["Name"]
                for m in self._inventory()["machines"]
                if m["Name"] != MACHINE and m.get("Running")
            ]
            if conflicts:
                raise RuntimeFailure(
                    "Another machine is running: "
                    + ", ".join(conflicts)
                    + ". Stop it yourself if appropriate; it has not been changed."
                )
            self._diagnostic(
                self._run(
                    "podman", "machine", "start", "--update-connection=false", MACHINE
                ).stdout
            )
        # Collisions must be checked before touching the image or application.
        app = (
            self._check_label("container", APP, owner)
            if self._exists("container", APP)
            else None
        )
        if self._exists("volume", VOLUME):
            self._check_label("volume", VOLUME, owner)
        if not app or not app["State"]["Running"]:
            with socket.socket() as listener:
                # Permit our previous TCP TIME_WAIT, not an active listener.
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    listener.bind(("127.0.0.1", port))
                except OSError as error:
                    raise RuntimeFailure(
                        f"Loopback port {port} is occupied; choose RSS_PORT or stop its owner yourself."
                    ) from error
        self._security(owner, target=CHECK)
        if not self._exists("volume", VOLUME):
            self._podman(
                "volume", "create", "--label", LABEL + "=" + owner["token"], VOLUME
            )
        if app:
            verified = json.loads((self.state_dir / "security.json").read_text())
            expected = verified["image_id"]
            binding = app["HostConfig"]["PortBindings"].get("80/tcp", [])
            if (
                app["Image"].removeprefix("sha256:") != expected.removeprefix("sha256:")
                or binding != [{"HostIp": "127.0.0.1", "HostPort": str(port)}]
                or app["Config"]["Entrypoint"] != ["./Docker/entrypoint.sh"]
                or app["Config"]["Cmd"] != verified["official_command"]
                or "LISTEN=127.0.0.1:8088" not in app["Config"]["Env"]
                or not any(
                    m.get("Name") == VOLUME
                    and m["Destination"] == "/var/www/FreshRSS/data"
                    for m in app["Mounts"]
                )
            ):
                raise RuntimeFailure(
                    "Existing application image/port/entrypoint differs; use replace after checking configuration."
                )
            if app["State"]["Running"]:
                self._podman("stop", "--time", "60", APP)
        else:
            image = self._image()
            self._podman(
                "create",
                "--name",
                APP,
                "--pull=never",
                "--platform",
                image["platform"],
                "--label",
                LABEL + "=" + owner["token"],
                "--restart=no",
                "--publish",
                f"127.0.0.1:{port}:80",
                "--mount",
                f"type=volume,source={VOLUME},destination=/var/www/FreshRSS/data",
                "--env",
                "CRON_MIN=*/20",
                "--env",
                "TZ=UTC",
                "--env",
                "ENABLE_ACCESS_LOG=0",
                "--env",
                "LISTEN=127.0.0.1:8088",
                "--env",
                "TRUSTED_PROXY=0",
                "--env",
                "COPY_LOG_TO_SYSLOG=Off",
                "--env",
                "COPY_SYSLOG_TO_STDERR=Off",
                image["image"],
            )
        # Repeat both copies while stopped, including after a partial first start.
        # These are new-user templates, never existing users' settings or feeds.
        self._podman(
            "cp",
            str(self.root / "config/config-user.custom.php"),
            APP + ":/var/www/FreshRSS/data/config-user.custom.php",
        )
        self._podman(
            "cp",
            str(self.root / "config/public-feed.opml"),
            APP + ":/var/www/FreshRSS/data/opml.xml",
        )
        self._podman("start", APP)
        security = self._security(owner, target=APP)
        self._ready(port)
        scheduler = self._scheduler(owner)
        fetch = self._fetch()
        url = f"http://127.0.0.1:{port}"
        self._diagnostic(
            f"FreshRSS: {url} (form login; complete installer locally on first run)."
        )
        return {
            "machine": "running",
            "url": url,
            "security": security,
            "scheduler": scheduler,
            "fetch": fetch,
        }

    def _podman(
        self, *args: str, check: bool = True, input: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Target the project connection explicitly for every resource command."""
        return self._run(
            "podman", "--connection", MACHINE, *args, check=check, input=input
        )

    def _exists(self, kind: str, name: str) -> bool:
        """Distinguish an absent resource from a failed Podman query."""
        result = self._podman(kind, "exists", name, check=False)
        if result.returncode not in (0, 1):
            raise RuntimeFailure(
                f"Cannot determine {kind} state: {result.stderr.strip()}"
            )
        return result.returncode == 0

    def _check_label(
        self, kind: str, name: str, owner: dict[str, Any]
    ) -> dict[str, Any]:
        """Refuse resources whose ownership token differs from the project."""
        item = json.loads(self._podman(kind, "inspect", name).stdout)[0]
        labels = (
            item.get("Labels")
            if kind == "volume"
            else item.get("Config", {}).get("Labels")
        )
        if (labels or {}).get(LABEL) != owner["token"]:
            raise RuntimeFailure(f"Refusing unowned {kind} {name}.")
        return item

    def _security(self, owner: dict[str, Any], target: str = CHECK) -> dict[str, Any]:
        """Verify pinned image and effective Apache PHP before publishing.

        The disposable preflight never publishes ports or mounts project data.
        The application stays on internal port 8088 until its probe is removed.
        """
        image_config = self._image()
        image = image_config["image"]
        result = self._podman("image", "inspect", image, check=False)
        if result.returncode:
            self._diagnostic("Pulling pinned official FreshRSS image...")
            self._podman("pull", "--platform", image_config["platform"], image)
            result = self._podman("image", "inspect", image)
        info = json.loads(result.stdout)[0]
        labels = info.get("Labels", {})
        if (
            info["Os"] + "/" + info["Architecture"] != image_config["platform"]
            or image not in info["RepoDigests"]
            or image.split("@")[0] + "@" + image_config["platform_manifest"]
            not in info["RepoDigests"]
            or labels.get("org.opencontainers.image.version") != image_config["version"]
            or labels.get("org.opencontainers.image.revision")
            != image_config["revision"]
            or info["Config"]["Entrypoint"] != ["./Docker/entrypoint.sh"]
        ):
            raise RuntimeFailure(
                "Pinned official image identity/platform/entrypoint mismatch."
            )
        if target == CHECK and self._exists("container", CHECK):
            self._check_label("container", CHECK, owner)
            self._podman("rm", "--force", "--volumes", CHECK)
        name = ".project-security-" + uuid.uuid4().hex + ".php"
        try:
            if target == CHECK:
                self._podman(
                    "run",
                    "--detach",
                    "--name",
                    CHECK,
                    "--pull=never",
                    "--platform",
                    image_config["platform"],
                    "--label",
                    LABEL + "=" + owner["token"],
                    "--env",
                    "CRON_MIN=",
                    "--env",
                    "ENABLE_ACCESS_LOG=0",
                    image,
                )
            else:
                self._check_label("container", APP, owner)
            self._podman(
                "cp",
                str(self.root / "config/security-probe.php"),
                target + ":/var/www/FreshRSS/p/" + name,
            )
            internal_port = 80 if target == CHECK else 8088
            code = f'echo file_get_contents("http://127.0.0.1:{internal_port}/{name}");'
            data = None
            for _ in range(30):
                result = self._podman("exec", target, "php", "-r", code, check=False)
                try:
                    data = json.loads(result.stdout)
                    break
                except ValueError:
                    time.sleep(1)
            if data is None:
                raise RuntimeFailure(
                    "Apache PHP security probe did not become ready; application not published."
                )
            version = tuple(int(n) for n in data["php"].split("-")[0].split(".")[:3])
            minimum = tuple(int(n) for n in image_config["php_minimum"].split("."))
            if version[:2] != minimum[:2] or version < minimum:
                raise RuntimeFailure("Unsupported/unfixed PHP runtime: " + data["php"])
            if data["sapi"] != "apache2handler" or data["upload_progress"] not in (
                "",
                "0",
            ):
                raise RuntimeFailure(
                    "Effective Apache PHP session upload progress is not disabled."
                )
            record = {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "image": image,
                "image_id": info["Id"],
                "platform": image_config["platform"],
                "platform_manifest": image_config["platform_manifest"],
                "freshrss": image_config["version"],
                "advisory_review_date": image_config["security_checked"],
                "web": data,
                "checked_container": target,
                "official_command": info["Config"]["Cmd"],
                "cli_php": self._podman(
                    "exec", target, "php", "-r", "echo PHP_VERSION;"
                ).stdout,
            }
            self._save("security.json", record)
        finally:
            # Only the disposable check container's anonymous volumes are removed.
            if target == CHECK:
                if self._exists("container", CHECK):
                    self._check_label("container", CHECK, owner)
                    self._podman("rm", "--force", "--volumes", CHECK)
            else:
                self._clear_probes()
        if target == APP:
            # Gracefully open port 80 only after the effective PHP probe is removed.
            code = (
                '$p="/etc/apache2/sites-available/FreshRSS.Apache.conf";'
                "$s=file_get_contents($p);"
                'if(substr_count($s,"Listen 127.0.0.1:8088")!==1){exit(1);}'
                'file_put_contents($p,str_replace("Listen 127.0.0.1:8088","Listen 80",$s));'
            )
            self._podman("exec", APP, "php", "-r", code)
            self._podman(
                "exec", APP, "sh", "-c", ". /etc/apache2/envvars; apache2 -k graceful"
            )
        self._diagnostic(
            "Verified official FreshRSS "
            + image_config["version"]
            + ", Apache PHP "
            + data["php"]
            + ", upload progress disabled ("
            + target
            + ", probe removed before serving)."
        )
        return record

    def _clear_probes(self) -> None:
        """Remove current and stale project probes, verifying cleanup before serving."""
        code = (
            '$dir="/var/www/FreshRSS/p/";'
            '$pattern="/^\\\\.project-security-[a-f0-9]{32}\\\\.php$/D";'
            "$files=scandir($dir);if($files===false){exit(1);}"
            "foreach($files as $file){if(preg_match($pattern,$file)"
            "&&!unlink($dir.$file)){exit(1);}}"
            "clearstatcache();$files=scandir($dir);if($files===false){exit(1);}"
            "foreach($files as $file){if(preg_match($pattern,$file)){exit(1);}}"
        )
        self._podman("exec", APP, "php", "-r", code)

    def _stop(self) -> dict[str, Any]:
        """Stop project writers and VM, refusing unrelated running containers."""
        owner, machine = self._owned()
        if machine["State"] == "stopped":
            self._diagnostic("Project VM already stopped.")
            return {"machine": "stopped", "already_stopped": True, "backup": "deferred"}
        if machine["State"] != "running":
            raise RuntimeFailure(
                "Project VM is "
                + machine["State"]
                + "; wait and inspect before retrying."
            )
        # machine ssh uses the already verified VM/SSH identity, not a potentially
        # redirected root connection. Rootful workloads are never project-owned.
        rootful = self._json_run(
            "podman",
            "machine",
            "ssh",
            MACHINE,
            "sudo",
            "podman",
            "ps",
            "--format",
            "json",
        )
        containers = json.loads(self._podman("ps", "--format", "json").stdout)
        if not isinstance(rootful, list) or not isinstance(containers, list):
            raise RuntimeFailure(
                "Cannot inspect both container engines; VM left running."
            )
        if rootful:
            raise RuntimeFailure(
                "Unrelated running rootful containers; project VM left running."
            )
        for item in containers:
            name = (
                item["Names"][0] if isinstance(item["Names"], list) else item["Names"]
            )
            if name not in (APP, CHECK):
                raise RuntimeFailure(
                    "Unrelated running container " + name + "; project VM left running."
                )
            self._check_label("container", name, owner)
        for name in (APP, CHECK):
            if self._exists("container", name):
                item = self._check_label("container", name, owner)
                if item["State"]["Running"]:
                    self._podman("stop", "--time", "60", name)
                    if self._check_label("container", name, owner)["State"]["Running"]:
                        raise RuntimeFailure(
                            "Application did not stop; VM left running."
                        )
        self._diagnostic(self._run("podman", "machine", "stop", MACHINE).stdout)
        self._diagnostic(
            "Project application writers and VM stopped. No external backup (issue #2 deferred)."
        )
        return {"machine": "stopped", "already_stopped": False, "backup": "deferred"}

    def _status(self) -> dict[str, Any]:
        """Report owned VM/application state and check running service readiness."""
        if not (self.state_dir / "owner.json").exists():
            self._diagnostic("FreshRSS is not set up. Run scripts/rss setup.")
            return {"machine": "absent", "application": "absent"}
        owner, machine = self._owned()
        result = {"machine": machine["State"], "application": "unknown"}
        self._diagnostic("Project VM: " + machine["State"])
        if machine["State"] == "running":
            if self._exists("container", APP):
                app = self._check_label("container", APP, owner)
                result["application"] = app["State"]["Status"]
                self._diagnostic("FreshRSS: " + app["State"]["Status"])
                if app["State"]["Running"]:
                    result["scheduler"] = self._scheduler(owner)
                    bindings = app["HostConfig"]["PortBindings"].get("80/tcp", [])
                    if len(bindings) != 1 or bindings[0]["HostIp"] != "127.0.0.1":
                        raise RuntimeFailure(
                            "Application loopback publication differs from the project configuration."
                        )
                    self._ready(int(bindings[0]["HostPort"]))
                    result["http_ready"] = True
                    self._diagnostic("Loopback HTTP: ready.")
            else:
                result["application"] = "absent"
                self._diagnostic("FreshRSS container absent.")
        path = self.state_dir / "security.json"
        if path.exists():
            record = json.loads(path.read_text())
            result["security"] = record
            self._diagnostic(
                "Last security check: "
                + record["checked_at"]
                + ", PHP "
                + record["web"]["php"]
                + " ("
                + record["checked_container"]
                + ")"
            )
        return result

    def _ready(self, port: int) -> None:
        """Retry transient HTTP failures up to 30 times, retaining failed VMs."""
        for _ in range(30):
            try:
                with urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                    if response.status == 200:
                        return
            except (URLError, TimeoutError, ConnectionError):
                time.sleep(1)
        raise RuntimeFailure(
            "FreshRSS HTTP did not become ready; VM retained for diagnosis."
        )

    def _scheduler(self, owner: dict[str, Any]) -> dict[str, Any]:
        """Verify the effective 20-minute fetching job and live cron process."""
        self._check_label("container", APP, owner)
        crontab = self._podman("exec", APP, "crontab", "-l").stdout
        if not any(
            line.startswith("*/20 * * * * ") and "actualize_script.php" in line
            for line in crontab.splitlines()
        ):
            raise RuntimeFailure("Effective 20-minute cron job missing.")
        self._podman(
            "exec",
            APP,
            "sh",
            "-c",
            'test -r /run/crond.pid && kill -0 "$(cat /run/crond.pid)"',
        )
        self._diagnostic("Scheduler: cron running, */20 (UTC minutes 00, 20, 40).")
        return {"running": True, "cron": "*/20 * * * *", "timezone": "UTC"}

    def _fetch(self) -> dict[str, Any]:
        """Fetch installed users' feeds without bypassing upstream cache or TTL."""
        owner, machine = self._owned()
        if machine["State"] != "running":
            raise RuntimeFailure("Start the project VM before fetching.")
        self._check_label("container", APP, owner)
        installed = self._podman(
            "exec",
            APP,
            "test",
            "-f",
            "/var/www/FreshRSS/data/applied_migrations.txt",
            check=False,
        )
        if installed.returncode == 1:
            self._diagnostic(
                "FreshRSS installer pending; after local installation, run scripts/rss fetch for the preconfigured public feed."
            )
            return {"status": "installer_pending"}
        if installed.returncode:
            raise RuntimeFailure("Could not determine FreshRSS installation state.")
        result = self._podman(
            "exec",
            "--user",
            "www-data",
            APP,
            "php",
            "app/actualize_script.php",
            check=False,
        )
        if result.returncode:
            raise RuntimeFailure(
                "Immediate feed fetching failed; inspect local FreshRSS logs (may contain sensitive URLs)."
            )
        self._diagnostic(
            "Immediate feed actualization completed (upstream cache/TTL/backoff still apply)."
        )
        return {"status": "completed"}

    def _replace(self) -> dict[str, Any]:
        """Replace only the owned application container, preserving named data."""
        owner, machine = self._owned()
        if machine["State"] != "running":
            raise RuntimeFailure("Start the project before replacing its container.")
        self._check_label("volume", VOLUME, owner)
        if self._exists("container", APP):
            self._check_label("container", APP, owner)
            self._podman("stop", "--time", "60", APP)
            self._podman("rm", APP)  # Never --volumes: the named data volume survives.
        return {**self._start(), "replaced": True}

    def _verify(self) -> dict[str, Any]:
        """Reopen serving only after rechecking effective application Apache PHP."""
        _owner, machine = self._owned()
        if machine["State"] != "running":
            raise RuntimeFailure("Start the project before verification.")
        return {**self._start(), "verified": True}
