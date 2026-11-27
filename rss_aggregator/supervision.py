"""Opt-in user-session supervision, separate from operator lifecycle intent."""

import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import stat
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rss_aggregator.private_access import PrivateAccess

if TYPE_CHECKING:
    from rss_aggregator.runtime import Runtime

INTENT = "operator-intent.json"
MAINTENANCE = "maintenance.json"
REGISTRATION = "supervision.json"
RECOVERY_STATE = "supervision-recovery.json"
JOB_VERSION = 2


class NativeJob:
    """Capture and verify a single native user-session job without adopting it."""

    def __init__(self, runtime: "Runtime", kind: str, command: str) -> None:
        """Bind a role and command to the canonical state directory."""
        self.runtime = runtime
        self.command = command
        digest = hashlib.sha256(os.fsencode(runtime.state_dir)).hexdigest()
        self.label = f"io.rss-aggregator.native.{kind}.{digest}"
        self.domain = f"gui/{os.getuid()}"

    @property
    def path(self) -> Path:
        """Locate the native definition in the user-owned LaunchAgents directory."""
        directory = Path.home() / "Library/LaunchAgents"
        if directory.is_symlink() or directory.parent.is_symlink():
            raise ValueError("Refusing symlinked LaunchAgents directory or parent.")
        if directory.exists():
            info = directory.stat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o022
            ):
                raise ValueError(
                    "LaunchAgents directory is unsafe; inspect it locally."
                )
        return directory / (self.label + ".plist")

    def capture(self, owner: dict[str, Any], backup: Path | None) -> dict[str, Any]:
        """Capture the checkout launcher, manifest, and relevant selectors."""
        if platform.system() != "Darwin" or os.getuid() == 0:
            raise ValueError("Native jobs require a normal macOS user GUI session.")
        root = self.runtime.root
        python = root / ".venv/bin/python"
        if not python.is_file() or not os.access(python, os.X_OK):
            raise ValueError(
                "Selected checkout launcher unavailable; run uv sync --locked there."
            )
        access = PrivateAccess(self.runtime)
        private = access.enabled()
        names = ["launchctl"]
        if private or os.path.lexists(access.path):
            names.append("tailscale")
        dependencies = {}
        for name in names:
            executable = shutil.which(name)
            if not executable or not Path(executable).is_absolute():
                raise ValueError(
                    f"Captured {name} unavailable; select an absolute executable PATH."
                )
            dependencies[name] = executable
        environment = {
            "HOME": str(Path.home()),
            "PATH": os.pathsep.join(
                dict.fromkeys(
                    [
                        *(str(Path(path).parent) for path in dependencies.values()),
                        "/usr/bin",
                        "/bin",
                        "/usr/sbin",
                        "/sbin",
                    ]
                )
            ),
            "RSS_STATE_DIR": str(self.runtime.state_dir),
            "RSS_PORT": str(self.runtime._port()),
            "RSS_TAILSCALE": "1" if private else "0",
        }
        if backup is not None:
            environment["RSS_BACKUP_DIR"] = str(backup)
        return {
            "version": JOB_VERSION,
            "owner": owner["token"],
            "root": str(root),
            "state_dir": str(self.runtime.state_dir),
            "job": str(self.path),
            "label": self.label,
            "python": str(python),
            "python_target": str(python.resolve(strict=True)),
            "python_sha256": self._hash(python),
            "manifest_sha256": self._manifest_hash(),
            "environment": environment,
            "dependencies": dependencies,
        }

    @staticmethod
    def _hash(path: Path) -> str:
        """Digest a captured file without interpreting or exposing its contents."""
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _manifest_hash(self) -> str:
        """Pin the manifest bytes, including edits that leave parsed JSON unchanged."""
        return self._hash(self.runtime.root / "config/native.json")

    def read(self, name: str) -> dict[str, Any] | None:
        """Read only a known ledger for this native label, owner, and checkout."""
        path = self.runtime.state_dir / name
        if not os.path.lexists(path):
            return None
        details = path.lstat()
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o600
        ):
            raise ValueError(
                "Native job ledger must be a private user-owned regular file."
            )
        record = json.loads(path.read_text())
        owner, _installed = self.runtime._ownership_receipt()
        if (
            not isinstance(record, dict)
            or record.get("version") != JOB_VERSION
            or record.get("owner") != owner["token"]
            or record.get("root") != str(self.runtime.root)
            or record.get("state_dir") != str(self.runtime.state_dir)
            or record.get("label") != self.label
            or record.get("job") != str(self.path)
            or record.get("python") != str(self.runtime.root / ".venv/bin/python")
        ):
            raise ValueError(
                "Native job registration ownership or format differs; inspect locally."
            )
        return record

    def check(self, record: dict[str, Any]) -> None:
        """Refuse drift before a background operation can run."""
        python = Path(record["python"])
        if (
            self._manifest_hash() != record["manifest_sha256"]
            or not python.is_file()
            or not os.access(python, os.X_OK)
            or str(python.resolve(strict=True)) != record["python_target"]
            or self._hash(python) != record["python_sha256"]
        ):
            raise ValueError(
                "Captured launcher or native manifest changed; restore it or explicitly reconfigure the owned job."
            )
        dependencies = record["dependencies"]
        if (
            record["environment"]["RSS_TAILSCALE"] == "1"
            or os.path.lexists(PrivateAccess(self.runtime).path)
        ) and "tailscale" not in dependencies:
            raise ValueError(
                "Tailscale is now required; explicitly reconfigure the owned job."
            )
        for name, executable in dependencies.items():
            path = Path(executable)
            if (
                not path.is_absolute()
                or not path.is_file()
                or not os.access(path, os.X_OK)
                or shutil.which(name, path=record["environment"]["PATH"]) != executable
            ):
                raise ValueError(
                    "Captured executable unavailable or dependency lookup changed; no fallback is allowed."
                )
        probe = self.runtime._run(
            str(python),
            "-I",
            "-B",
            "-c",
            "from pathlib import Path; import rss_aggregator.runtime as r; print(Path(r.__file__).resolve().parents[1])",
            check=False,
        )
        if probe.returncode or probe.stdout.strip() != record["root"]:
            raise ValueError(
                "Captured launcher cannot import this checkout's runtime; inspect uv sync --locked locally."
            )

    def arguments(self, record: dict[str, Any]) -> list[str]:
        """Generate the same isolated entrypoint for disk and loaded verification."""
        return [record["python"], "-I", "-B", "-m", "rss_aggregator", self.command]

    def definition(self, record: dict[str, Any], interval: int) -> bytes:
        """Build a bounded wakeup without killing independently tracked writers."""
        return plistlib.dumps(
            {
                "Label": self.label,
                "ProgramArguments": self.arguments(record),
                "WorkingDirectory": record["root"],
                "EnvironmentVariables": record["environment"],
                "RunAtLoad": True,
                "StartInterval": interval,
                "ProcessType": "Background",
                "AbandonProcessGroup": True,
                "Umask": 0o077,
                "StandardOutPath": "/dev/null",
                "StandardErrorPath": "/dev/null",
            },
            sort_keys=True,
        )

    def launch(self, record: dict[str, Any], *args: str) -> Any:
        """Use captured launchctl without printing its raw output."""
        executable = record["dependencies"]["launchctl"]
        if not Path(executable).is_absolute():
            raise ValueError("Refusing relative captured launchctl path.")
        return self.runtime._run(executable, *args, check=False)

    def loaded(self, record: dict[str, Any]) -> bool:
        """Accept only a matching native loaded job, never a legacy service."""
        result = self.launch(record, "print", self.domain + "/" + self.label)
        if result.returncode:
            if result.returncode in (3, 113):
                return False
            raise ValueError(
                "Cannot inspect native launchd registration; inspect the user session."
            )
        fields = {line.strip() for line in result.stdout.splitlines()}
        arguments = re.search(r"(?ms)^\s*arguments = \{\n(.*?)^\s*\}", result.stdout)
        environment = re.search(
            r"(?ms)^\s*environment = \{\n(.*?)^\s*\}", result.stdout
        )
        if (
            "path = " + record["job"] not in fields
            or "program = " + record["python"] not in fields
            or arguments is None
            or [line.strip() for line in arguments[1].splitlines() if line.strip()]
            != self.arguments(record)
            or environment is None
            or not self._environment_matches(environment[1], record["environment"])
        ):
            raise ValueError(
                "Refusing foreign/changed loaded native job; inspect launchd locally."
            )
        return True

    @staticmethod
    def _environment_matches(observed: str, expected: dict[str, str]) -> bool:
        """Compare runtime selectors exactly while allowing launchd's own variables."""
        relevant = {line.strip() for line in observed.splitlines() if line.strip()}
        captured = {f"{key} => {value}" for key, value in expected.items()}
        selectors = {line for line in relevant if line.startswith("RSS_")}
        return captured <= relevant and selectors == {
            line for line in captured if line.startswith("RSS_")
        }

    def matches(self, expected: bytes) -> bool:
        """Verify the exact private regular definition without following symlinks."""
        path = self.path
        if path.is_symlink() or not path.is_file():
            return False
        info = path.stat()
        return (
            info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o600
            and path.read_bytes() == expected
        )

    def write(self, definition: bytes) -> None:
        """Stage a private definition before publishing it without replacement."""
        path = self.path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        stage = tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".rss-native-job-", delete=False
        )
        temporary = Path(stage.name)
        try:
            with stage:
                os.fchmod(stage.fileno(), 0o600)
                if stage.write(definition) != len(definition):
                    raise OSError(
                        "Short write while staging native LaunchAgent definition."
                    )
                stage.flush()
                os.fsync(stage.fileno())
            os.link(temporary, path)
        except FileExistsError:
            raise ValueError(
                "Refusing conflicting native LaunchAgent definition; registration retained. Inspect locally before retrying."
            ) from None
        finally:
            temporary.unlink(missing_ok=True)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @contextmanager
    def environment(self, record: dict[str, Any]) -> Iterator[None]:
        """Use captured runtime selectors without leaking background diagnostics."""
        keys = set(record["environment"]) | {"RSS_BACKUP_DIR"}
        previous = {key: os.environ.get(key) for key in keys}
        try:
            for key in keys:
                os.environ.pop(key, None)
            os.environ.update(record["environment"])
            with open(os.devnull, "w") as sink, redirect_stderr(sink):
                yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class Supervision:
    """Coordinate persisted intent and noninterrupting recovery under Runtime's lock."""

    def __init__(self, runtime: "Runtime") -> None:
        """Bind the selected checkout and ownership directory without side effects."""
        self.runtime = runtime
        self.job = NativeJob(runtime, "supervision", "supervision-tick")

    def intent(self) -> str:
        """Read fail-closed operator intent; absence never authorizes recovery."""
        path = self.runtime.state_dir / INTENT
        if not os.path.lexists(path):
            return "unset"
        if path.is_symlink():
            raise ValueError("Refusing symlinked operator intent; inspect locally.")
        value = json.loads(path.read_text())
        if (
            not isinstance(value, dict)
            or value.get("version") != 1
            or value.get("intent")
            not in (
                "running",
                "stopped",
            )
        ):
            raise ValueError(
                "Invalid operator-intent.json; inspect locally before starting."
            )
        return value["intent"]

    def set_intent(self, intent: str) -> None:
        """Record an explicit operator decision, never temporary backup quiescing."""
        self.runtime._save(INTENT, {"version": 1, "intent": intent})
        if intent == "running":
            self._reset_attempts()

    def status(self) -> dict[str, Any]:
        """Report registration, session, intent, observed health and sanitized history."""
        from rss_aggregator.runtime import RuntimeFailure

        record = self._record()
        result: dict[str, Any] = {
            "enabled": record is not None,
            "intent": self.intent(),
            "maintenance": os.path.lexists(self.runtime.state_dir / MAINTENANCE),
            "recovery": self._attempts(),
            "session": "not_checked",
            "observed": {"availability": "unknown"},
            "action": "Use supervision-install only after a verified backup and owner approval.",
        }
        if not record:
            return {**result, "state": "disabled"}
        result.update(
            job=record["job"], root=record["root"], state_dir=record["state_dir"]
        )
        if result["maintenance"]:
            return {
                **result,
                "state": "maintenance",
                "action": "Inspect interrupted maintenance; use backup-reconcile when available. To remain stopped, use rss stop before reconciling: retained running intent allows later supervision recovery. Never delete the marker to bypass it.",
            }
        try:
            self._check_job(record)
            self.job.check(record)
            self.runtime._owned(require_current=False, verify=False)
            session = self.job.launch(record, "print", self.job.domain)
            if session.returncode:
                return {
                    **result,
                    "state": "session_unavailable",
                    "session": "unavailable",
                    "action": "Log in/unlock the normal Mac user session; do not change security settings.",
                }
            result["session"] = "available"
            if not self.job.loaded(record):
                return {
                    **result,
                    "state": "inactive",
                    "action": "Inspect the normal user session and rerun supervision-install from the selected checkout.",
                }
        except (RuntimeFailure, OSError, ValueError, KeyError, TypeError):
            # Do not persist arbitrary exception text or executable output.
            return {
                **result,
                "state": "configuration_unavailable",
                "action": "Restore the captured checkout, dependencies and unchanged owned job, or remove/reinstall supervision explicitly.",
            }
        with self.job.environment(record):
            result["observed"] = self._observe()
        if result["intent"] != "running":
            state = (
                "intentionally_stopped"
                if result["intent"] == "stopped"
                else "intent_unset"
            )
            return {
                **result,
                "state": state,
                "action": "Only explicit rss start enables running intent; backups and supervision do not.",
            }
        if result["observed"]["availability"] == "healthy":
            return {
                **result,
                "state": "healthy",
                "action": "No action required; health checks do not restart FreshRSS.",
            }
        attempts = result["recovery"]["attempts"]
        state = (
            "retry_exhausted"
            if attempts >= 3
            else "recovery_failed"
            if attempts
            else "unhealthy"
        )
        return {
            **result,
            "state": state,
            "action": result["observed"].get(
                "action",
                "Inspect ownership/security/dependencies and forwarding locally; after fixing the cause, explicit rss start resets the retry budget. Never bypass TLS or adopt foreign resources.",
            ),
        }

    def _attempts(self) -> dict[str, Any]:
        """Read bounded recovery state, retaining recent secret-free outcomes."""
        path = self.runtime.state_dir / RECOVERY_STATE
        if not path.exists():
            return {"version": 1, "attempts": 0, "next_retry_at": None, "history": []}
        value = json.loads(path.read_text())
        if (
            not isinstance(value, dict)
            or value.get("version") != 1
            or type(value.get("attempts")) is not int
            or not 0 <= value["attempts"] <= 3
            or not isinstance(value.get("history"), list)
            or "next_retry_at" not in value
        ):
            raise ValueError(
                "Invalid supervision recovery state; inspect locally before resetting with rss start."
            )
        return value

    def _reset_attempts(self) -> None:
        """Reset the bounded retry budget without discarding recent failure history."""
        value = self._attempts()
        self.runtime._save(
            RECOVERY_STATE, {**value, "attempts": 0, "next_retry_at": None}
        )

    def tick(self) -> dict[str, Any]:
        """Perform one health reconciliation, with at most three guarded attempts."""
        result = self.status()
        state = result["state"]
        if state in ("disabled", "intentionally_stopped", "intent_unset"):
            return result
        if state == "healthy":
            self._reset_attempts()
            return result
        if state not in ("unhealthy", "recovery_failed"):
            raise ValueError(
                f"Supervision {state}; run rss supervision-status for the next operator action."
            )
        value = self._attempts()
        now = time.time()
        if value["next_retry_at"] is not None and now < value["next_retry_at"]:
            raise ValueError(
                "Supervision backoff active; inspect rss supervision-status for next_retry_at."
            )
        attempt = value["attempts"] + 1
        event = {"at": now, "attempt": attempt, "outcome": "interrupted_or_in_progress"}
        value = {
            "version": 1,
            "attempts": attempt,
            "next_retry_at": now + (60 if attempt == 1 else 300)
            if attempt < 3
            else None,
            "history": [*value["history"], event][-10:],
        }
        # Reserve the attempt before mutation so process death cannot reset retries.
        self.runtime._save(RECOVERY_STATE, value)
        record = self._record()
        assert record is not None
        from rss_aggregator.runtime import RuntimeFailure

        try:
            with self.job.environment(record):
                self.runtime._start()
                observed = self._observe()
                if observed["availability"] != "healthy":
                    raise ValueError("Post-recovery readiness failed.")
        except (
            RuntimeFailure,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            IndexError,
        ) as error:
            value["history"][-1] = {
                **event,
                "outcome": "failed",
                **self._failure(error),
            }
            self.runtime._save(RECOVERY_STATE, value)
            raise ValueError(
                "Guarded recovery failed; inspect rss supervision-status and resolve the cause locally. No security gate was bypassed."
            ) from None
        value["history"][-1] = {**event, "outcome": "recovered"}
        self.runtime._save(
            RECOVERY_STATE, {**value, "attempts": 0, "next_retry_at": None}
        )
        return {
            **result,
            "state": "recovered",
            "observed": observed,
            "recovery": self._attempts(),
        }

    def _observe(self) -> dict[str, Any]:
        """Check existing readiness without calling start or altering forwarding."""
        from rss_aggregator.runtime import RuntimeFailure

        try:
            result = self.runtime._status()
            ready = (
                result["runtime"] == "installed"
                and result["application"] == "running"
                and result.get("http_ready") is True
                and result.get("scheduler", {}).get("running") is True
            )
            if PrivateAccess(self.runtime).enabled():
                ready = (
                    ready and result.get("forwarding", {}).get("status") == "running"
                )
            return {
                "availability": "healthy" if ready else "unhealthy",
                "runtime": result["runtime"],
                "application": result["application"],
            }
        except (
            RuntimeFailure,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            IndexError,
        ) as error:
            return {"availability": "unhealthy", **self._failure(error)}

    def _failure(self, error: Exception) -> dict[str, str]:
        """Classify known gates without retaining arbitrary sensitive error text."""
        message = str(error).lower()
        categories = (
            (
                ("unowned", "identity changed", "ownership", "not set up"),
                "ownership_conflict",
                "Preserve resources and inspect native runtime and writer ownership locally; never relabel or adopt foreign resources.",
            ),
            (
                (
                    "loopback port",
                    "listener differs",
                    "runtime configuration differs",
                ),
                "configuration_conflict",
                "Inspect the captured loopback port and owned native configuration; do not stop foreign listeners. Remove/reinstall supervision before deliberate configuration changes.",
            ),
            (
                (
                    "tailscale",
                    "serve ",
                    "funnel",
                    "https",
                    "authentication",
                    "anonymous",
                    "access/forwarding",
                    "access settings",
                ),
                "private_access_refused",
                "Inspect owned forwarding, Tailscale permissions/policy and authenticated FreshRSS settings locally; preserve foreign routes and never weaken TLS.",
            ),
            (
                (
                    "php",
                    "security",
                    "probe",
                    "pinned",
                    "manifest",
                    "checksum",
                ),
                "security_gate_failed",
                "Inspect the reviewed runtime/PHP and probe-cleanup gates locally; restore matching configuration and never bypass security verification.",
            ),
            (
                ("http", "readiness"),
                "readiness_failed",
                "Check loopback HTTP and the owned application locally; trusted client HTTPS remains a separate check.",
            ),
            (
                ("fetch", "scheduler"),
                "fetching_unhealthy",
                "Inspect the installed fetching job and daemon with rss status; after fixing the cause, use explicit start for guarded recovery.",
            ),
            (
                ("unavailable", " failed ("),
                "external_command_failed",
                "Inspect native runtime and captured Tailscale dependencies and local permissions; restore dependencies before explicit start.",
            ),
        )
        for needles, reason, action in categories:
            if any(needle in message for needle in needles):
                return {"reason": reason, "action": action}
        return {
            "reason": "recovery_guard_failed",
            "action": "Inspect ownership, storage/configuration and startup gates locally using rss status and explicit start diagnostics; share only redacted evidence.",
        }

    def _record(self) -> dict[str, Any] | None:
        """Read this owner's native registration ledger."""
        return self.job.read(REGISTRATION)

    def _check_job(self, record: dict[str, Any], *, missing: bool = False) -> None:
        """Refuse anything except our exact definition or explicitly allowed absence."""
        if missing and not os.path.lexists(self.job.path):
            return
        if not self.job.matches(self.job.definition(record, 60)):
            raise ValueError(
                "Supervision job missing, changed or unowned; inspect locally."
            )

    def install(self) -> dict[str, Any]:
        """Explicitly register and activate only an owned normal-session LaunchAgent."""
        owner, _installed = self.runtime._owned()
        record = self._record()
        backup = os.environ.get("RSS_BACKUP_DIR")
        destination = Path(backup).expanduser() if backup else None
        if destination is not None and not destination.is_absolute():
            raise ValueError(
                "RSS_BACKUP_DIR must be absolute when captured for supervision."
            )
        captured = self.job.capture(
            owner, destination.resolve() if destination else None
        )
        self.job.check(captured)
        if record and record != captured:
            raise ValueError(
                "Captured supervision configuration differs; remove it first, then explicitly install the reviewed configuration."
            )
        job = self.job.path
        had_definition = os.path.lexists(job)
        if not record and had_definition:
            raise ValueError(
                "Refusing unowned supervision job; preserve and resolve it locally."
            )
        session = self.job.launch(captured, "print", self.job.domain)
        if session.returncode:
            raise ValueError(
                "Normal user GUI session is unavailable; log in/unlock and retry installation."
            )
        loaded = self.job.loaded(captured)
        if loaded and not record:
            raise ValueError("Refusing a loaded job without our registration ledger.")
        if loaded and not had_definition:
            raise ValueError(
                "Loaded supervision job definition is missing; use supervision-remove then supervision-install. Recreating the file cannot update launchd's loaded policy. Registration retained."
            )
        if record and had_definition:
            self._check_job(record)
        self.runtime._save(REGISTRATION, captured)
        if not had_definition:
            self.job.write(self.job.definition(captured, 60))
        if not loaded:
            self._check_job(captured)
            result = self.job.launch(captured, "bootstrap", self.job.domain, str(job))
            if result.returncode:
                raise ValueError(
                    "Supervision activation failed; registration retained. Inspect the user session and retry supervision-install or remove."
                )
        return self.status()

    def remove(self) -> dict[str, Any]:
        """Unload and remove only the unchanged owned job; preserve intent and data."""
        record = self._record()
        if not record:
            if os.path.lexists(self.job.path):
                raise ValueError(
                    "Refusing unowned supervision job; no resources were removed."
                )
            return self.status()
        self._check_job(record, missing=True)
        if self.job.loaded(record):
            result = self.job.launch(
                record, "bootout", self.job.domain + "/" + self.job.label
            )
            if result.returncode:
                raise ValueError(
                    "Supervision removal failed; registration retained for inspection/retry."
                )
        Path(record["job"]).unlink(missing_ok=True)
        (self.runtime.state_dir / REGISTRATION).unlink()
        return self.status()
