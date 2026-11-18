"""Explicit, preview-first removal of the owned VM and inactive data volumes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from rss_aggregator.runtime import Runtime


STOCK_NETWORK = {
    "name": "podman",
    "id": "2f259bab93aaaaa2542ba43ef33eb990d0999ee1b9924b557b7be53c0b7a1bb9",
    "driver": "bridge",
    "network_interface": "podman0",
    "subnets": [{"subnet": "10.88.0.0/16", "gateway": "10.88.0.1"}],
    "ipv6_enabled": False,
    "internal": False,
    "dns_enabled": False,
    "ipam_options": {"driver": "host-local"},
}


def _network_time(value: str) -> tuple[datetime, int]:
    """Parse strict RFC3339Nano without losing precision on Python 3.10."""
    match = re.fullmatch(
        r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})"
        r"(?:\.([0-9]{1,9}))?(Z|[+-][0-9]{2}:[0-9]{2})",
        value,
    )
    if match is None:
        raise ValueError("Invalid native network timestamp")
    seconds, fraction, offset = match.groups()
    if offset == "Z":
        offset = "+00:00"
    elif int(offset[1:3]) > 23 or int(offset[4:6]) > 59:
        raise ValueError("Invalid native network offset")
    return datetime.fromisoformat(seconds + offset), int((fraction or "").ljust(9, "0"))


def confirmation(plan: dict[str, Any], identity: Any) -> str:
    """Bind destructive consent to the observed plan and private resource identity."""
    return hashlib.sha256(
        json.dumps([plan, identity], sort_keys=True).encode()
    ).hexdigest()


class Cleanup:
    """Keep preview and destruction under Runtime's existing command lease."""

    def __init__(self, runtime: Runtime) -> None:
        """Bind resource names without making new ownership claims."""
        from rss_aggregator.runtime import APP, CHECK, MACHINE, RuntimeFailure

        self.runtime = runtime
        self.machine, self.app, self.check = MACHINE, APP, CHECK
        self.failure = RuntimeFailure
        self.guest_user = ""

    def _json(
        self, *args: str, object_result: bool = False, quiet: bool = False
    ) -> Any:
        """Read typed metadata; optionally refuse even warning diagnostics."""
        result = self.runtime._run("podman", *args, check=False)
        if result.returncode or (quiet and result.stderr):
            raise self.failure(
                "Cleanup inspection failed; resources retained. Inspect the explicitly named VM locally; recover an unavailable source without deleting it."
            )
        try:
            records = json.loads(result.stdout)
        except ValueError:
            raise self.failure(
                "Malformed cleanup inventory; resources retained."
            ) from None
        valid = (
            isinstance(records, dict)
            if object_result
            else isinstance(records, list)
            and all(isinstance(item, dict) for item in records)
        )
        if not valid:
            raise self.failure("Malformed cleanup inventory; resources retained.")
        return records

    def _inventory(self) -> dict[str, Any]:
        """List machine and connection identities without contacting other engines."""
        return {
            "machines": self._json("machine", "list", "--format", "json"),
            "connections": self._json(
                "system", "connection", "list", "--format", "json"
            ),
        }

    def _owner(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Translate native ownership failures into secret-safe recovery guidance."""
        try:
            return self.runtime._owned()
        except (self.failure, OSError, ValueError, KeyError, IndexError) as error:
            raise self.failure(
                "Cannot verify project machine/connection ownership; resources retained. Inspect locally; never replace ownership records to adopt resources."
            ) from error

    def _jobs_absent(self) -> None:
        """Require removal of opted-in jobs before dismantling their environment."""
        from rss_aggregator.backup_schedule import BackupSchedule
        from rss_aggregator.supervision import Supervision

        for record, job in (
            ("supervision.json", Supervision(self.runtime)._job()),
            ("backup-schedule.json", BackupSchedule(self.runtime).job),
        ):
            if os.path.lexists(self.runtime.state_dir / record) or os.path.lexists(job):
                raise self.failure(
                    "Remove owned supervision and disable daily backups with their existing commands before cleanup; do not delete registrations by hand."
                )

    def _backup_directory(self) -> str:
        """Validate the configured external destination without creating it in preview."""
        value = os.environ.get("RSS_BACKUP_DIR", "")
        path = Path(value).expanduser()
        if not value or not path.is_absolute():
            raise self.failure(
                "Set an absolute private RSS_BACKUP_DIR outside Git before VM removal."
            )
        path = path.resolve()
        if (
            path == self.runtime.root
            or self.runtime.root in path.parents
            or any((parent / ".git").exists() for parent in (path, *path.parents))
        ):
            raise self.failure("RSS_BACKUP_DIR must be outside every Git checkout.")
        if path.exists() and (
            not path.is_dir()
            or path.stat().st_uid != os.getuid()
            or path.stat().st_mode & 0o077
        ):
            raise self.failure(
                "RSS_BACKUP_DIR must be a private user-owned directory (0700)."
            )
        return str(path)

    def _label(self, kind: str, name: str, owner: dict[str, Any]) -> dict[str, Any]:
        """Inspect ownership without exposing an engine's diagnostic contents."""
        from rss_aggregator.runtime import LABEL

        info = self._json("--connection", self.machine, kind, "inspect", name)[0]
        labels = (
            info.get("Labels")
            if kind == "volume"
            else info.get("Config", {}).get("Labels")
        )
        if (labels or {}).get(LABEL) != owner["token"]:
            raise self.failure("Resource ownership changed; cleanup refused.")
        return info

    def _guest(self, rootful: bool, *args: str, native: bool = True) -> tuple[str, ...]:
        """Target the named guest, quoting arguments for native SSH shell joining."""
        user = ("sudo", "-n", "--") if rootful else ()
        engine = ("podman", "--log-level=warn", "--remote=false") if native else ()
        return (
            "machine",
            "ssh",
            "--username",
            self.guest_user,
            self.machine,
            *user,
            *engine,
            *(shlex.quote(arg) for arg in args),
        )

    def _empty_guest(self, rootful: bool, *args: str, native: bool = True) -> None:
        """Require a successful, diagnostic-free, exactly empty metadata listing."""
        result = self.runtime._run(
            "podman", *self._guest(rootful, *args, native=native), check=False
        )
        if result.returncode or result.stderr:
            raise self.failure("Cleanup inspection failed; resources retained.")
        if result.stdout:
            raise self.failure(
                "Unbacked guest resources or malformed metadata; VM removal refused. Retain the VM for separate owner-reviewed recovery."
            )

    def _api_context(self, rootful: bool, local: dict[str, Any]) -> dict[str, Any]:
        """Refuse absent or unequal storage identities on the actual API service."""
        args = (
            self._guest(
                True,
                "podman",
                "--log-level=warn",
                "--remote",
                "--url",
                "unix:///run/podman/podman.sock",
                "info",
                "--format",
                "json",
                native=False,
            )
            if rootful
            else ("--connection", self.machine, "info", "--format", "json")
        )
        remote = self._json(*args, object_result=True, quiet=True)
        identities = []
        for info, is_remote in ((local, False), (remote, True)):
            host, store, version = (
                info.get("host"),
                info.get("store"),
                info.get("version"),
            )
            if (
                not isinstance(host, dict)
                or not isinstance(store, dict)
                or not isinstance(version, dict)
                or version.get("Version") != "6.2.0"
                or host.get("serviceIsRemote") is not is_remote
                or not isinstance(host.get("security"), dict)
                or host["security"].get("rootless") is not (not rootful)
            ):
                raise self.failure(
                    "Unverifiable API engine identity; resources retained."
                )
            for key in ("graphRoot", "runRoot", "volumePath", "imageCopyTmpDir"):
                if (
                    not isinstance(store.get(key), str)
                    or not PurePosixPath(store[key]).is_absolute()
                ):
                    raise self.failure(
                        "Unverifiable API storage identity; resources retained."
                    )
            if (
                store.get("transientStore") is not False
                or not isinstance(store.get("graphOptions"), dict)
                or not isinstance(store.get("graphDriverName"), str)
                or not store["graphDriverName"]
                or any(
                    not isinstance(host.get(key), str) or not host[key]
                    for key in ("databaseBackend", "networkBackend", "eventLogger")
                )
            ):
                raise self.failure(
                    "Unverifiable API storage identity; resources retained."
                )
            identities.append(
                {
                    "store": {
                        key: store[key]
                        for key in (
                            "graphRoot",
                            "runRoot",
                            "volumePath",
                            "graphDriverName",
                            "graphOptions",
                            "transientStore",
                            "imageCopyTmpDir",
                        )
                    },
                    "host": {
                        key: host[key]
                        for key in ("databaseBackend", "networkBackend", "eventLogger")
                    },
                    "rootless": not rootful,
                }
            )
        if json.dumps(identities[0], sort_keys=True) != json.dumps(
            identities[1], sort_keys=True
        ):
            raise self.failure(
                "Selected API service and local inspection contexts differ; VM removal refused. Retain the source for owner-reviewed recovery."
            )
        return identities[0]

    def _guest_baseline(self, rootful: bool) -> tuple[str, dict[str, Any]]:
        """Verify local/API storage identities and the synthetic network baseline."""
        info = self._json(
            *self._guest(rootful, "info", "--format", "json"),
            object_result=True,
            quiet=True,
        )
        host = info.get("host", {})
        if (
            not isinstance(host, dict)
            or host.get("serviceIsRemote") is not False
            or not isinstance(host.get("security"), dict)
            or host["security"].get("rootless") is not (not rootful)
            or host.get("networkBackend") != "netavark"
            or not isinstance(info.get("version"), dict)
            or info["version"].get("Version") != "6.2.0"
        ):
            raise self.failure(
                "Unverified guest engine mode/version/backend; VM removal refused."
            )
        identity = self._api_context(rootful, info)
        store = info.get("store")
        if (
            not isinstance(store, dict)
            or store.get("transientStore") is not False
            or not isinstance(store.get("imageCopyTmpDir"), str)
            or not PurePosixPath(store["imageCopyTmpDir"]).is_absolute()
            or not isinstance(store.get("graphRoot"), str)
            or not PurePosixPath(store["graphRoot"]).is_absolute()
        ):
            raise self.failure("Unverified guest storage metadata; VM removal refused.")
        # Buildah RUN --mount=type=cache is neither an image nor a volume.
        # Inspect names only in the current effective temp dir and stock paths.
        for directory in sorted({store["imageCopyTmpDir"], "/var/tmp", "/tmp"}):
            self._empty_guest(
                rootful,
                "find",
                "-H",
                directory,
                "-mindepth",
                "1",
                "-maxdepth",
                "1",
                "-name",
                "buildah-cache-*",
                "-print",
                "-quit",
                native=False,
            )
        created = []
        # v6.2.0 Netavark synthesizes its absent stock default with time.Now()
        # per local process. Persisted replacements retain Created, even with
        # the same name, fixed ID and configuration. Never accept name alone.
        for _ in range(2):
            networks = self._json(
                *self._guest(rootful, "network", "ls", "--format", "json"),
                quiet=True,
            )
            if len(networks) != 1:
                raise self.failure("Unrelated networks exist; VM removal refused.")
            network = networks[0].copy()
            timestamp = network.pop("created", None)
            if json.dumps(network, sort_keys=True) != json.dumps(
                STOCK_NETWORK, sort_keys=True
            ) or not isinstance(timestamp, str):
                raise self.failure("Unverified network baseline; VM removal refused.")
            try:
                moment = _network_time(timestamp)
            except ValueError:
                raise self.failure(
                    "Malformed network metadata; resources retained."
                ) from None
            created.append(moment)
        if created[1] <= created[0]:
            raise self.failure(
                "Persisted or unverified default network; VM removal refused."
            )
        return store["graphRoot"], identity

    def _resources(self, owner: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        """Reject independent unbacked guest resources and foreign instance state."""
        runtime = self.runtime
        connection = ("--connection", self.machine)
        username = owner["machine"].get("SSHConfig", {}).get("RemoteUsername")
        if (
            not isinstance(username, str)
            or not username
            or username == "root"
            or urlsplit(owner["connection"]["URI"]).username != username
        ):
            raise self.failure(
                "Unverified rootless guest identity; resources retained."
            )
        self.guest_user = username
        contexts = {rootful: self._guest_baseline(rootful) for rootful in (True, False)}
        for args in (
            ("ps", "--all", "--external"),
            ("volume", "ls"),
            ("images", "--all"),
        ):
            if self._json(*self._guest(True, *args, "--format", "json")):
                raise self.failure(
                    "Unrelated rootful resources exist; VM removal refused."
                )
        # The instance archive covers no engine secrets. List metadata only;
        # never inspect payloads or include secret names in reports/receipts.
        for rootful in (True, False):
            for args in (
                ("secret", "ls", "--quiet"),
                ("artifact", "ls", "--quiet"),
                ("pod", "ps", "--quiet"),
            ):
                self._empty_guest(rootful, *args)
            # Native secret List can swallow a failed database stat. Require
            # the just-opened manager directory to contain only its lockfile;
            # even empty/stale DBs and orphaned drivers require owner review.
            self._empty_guest(
                rootful,
                "find",
                "-H",
                str(PurePosixPath(contexts[rootful][0]) / "secrets"),
                "-mindepth",
                "1",
                "-maxdepth",
                "1",
                "!",
                "-name",
                "secrets.lock",
                "-print",
                "-quit",
                native=False,
            )
        containers = self._json(
            *connection, "ps", "--all", "--external", "--format", "json"
        )
        volumes = self._json(*connection, "volume", "ls", "--format", "json")
        images = self._json(*connection, "images", "--all", "--format", "json")
        details = []
        names = []
        for item in sorted(containers, key=lambda item: str(item["Names"])):
            name = (
                item["Names"][0] if isinstance(item["Names"], list) else item["Names"]
            )
            if name not in (self.app, self.check):
                raise self.failure("Unrelated container exists; VM removal refused.")
            info = self._label("container", name, owner)
            details.append(
                {
                    key: info.get(key)
                    for key in ("Id", "ID", "Created", "Image", "Mounts", "Config")
                }
            )
            names.append(name)
        volume_names = sorted(item["Name"] for item in volumes)
        if volume_names != [runtime.volume]:
            raise self.failure(
                "VM removal requires exactly the selected volume for a fresh full backup; additional volumes or missing source require explicit recovery first."
            )
        volume = self._label("volume", runtime.volume, owner)
        pinned = self._json(*connection, "image", "inspect", runtime._image()["image"])[
            0
        ]
        image_ids = []
        for image in images:
            image_id = image.get("Id", image.get("ID"))
            if not image_id or image_id.removeprefix("sha256:") != pinned[
                "Id"
            ].removeprefix("sha256:"):
                raise self.failure("Unrecognized image exists; VM removal refused.")
            image_ids.append(image_id)
        return {
            "containers": sorted(names),
            "volumes": volume_names,
            "images": sorted(set(image_ids)),
            "networks": {"rootless": ["podman"], "rootful": ["podman"]},
        }, [
            details,
            [contexts[rootful][1] for rootful in (True, False)],
            {
                key: volume.get(key)
                for key in (
                    "Name",
                    "Driver",
                    "Labels",
                    "CreatedAt",
                    "Options",
                    "Scope",
                    "Anonymous",
                    "UID",
                    "GID",
                )
            },
            pinned,
        ]

    def _connections(
        self, owner: dict[str, Any], inventory: dict[str, Any]
    ) -> list[str]:
        """Check root connection derivation and preserve every foreign connection/default."""
        names = []
        original = urlsplit(owner["connection"]["URI"])
        foreign = [
            c
            for c in inventory["connections"]
            if c["Name"] not in (self.machine, self.machine + "-root")
        ]
        for item in inventory["connections"]:
            if item["Name"] not in (self.machine, self.machine + "-root"):
                continue
            if item["Name"] == self.machine + "-root":
                uri = urlsplit(item["URI"])
                if (
                    uri.scheme != "ssh"
                    or uri.username != "root"
                    or uri.hostname != original.hostname
                    or uri.port != original.port
                    or uri.path != "/run/podman/podman.sock"
                    or uri.password
                    or uri.query
                    or uri.fragment
                    or item.get("Identity") != owner["connection"]["Identity"]
                    or item.get("IsMachine") is not True
                ):
                    raise self.failure(
                        "Root connection ownership changed; VM removal refused."
                    )
            if foreign and item.get("Default"):
                raise self.failure(
                    "Project connection is default among unrelated connections; explicitly choose the intended retained default before cleanup."
                )
            names.append(item["Name"])
        return sorted(names)

    def _vm_plan(self) -> dict[str, Any]:
        """Inspect exact disposable resources without stopping writers or making backups."""
        self._jobs_absent()
        inventory = self._inventory()
        present = any(m["Name"] == self.machine for m in inventory["machines"])
        if not present:
            if any(
                c["Name"] in (self.machine, self.machine + "-root")
                for c in inventory["connections"]
            ):
                raise self.failure(
                    "Machine absent but project connections remain; inspect partial removal locally without adopting/deleting connections."
                )
            return {"machine": self.machine, "status": "absent", "preview": True}
        owner, machine = self._owner()
        if machine["State"] != "running":
            raise self.failure(
                "Fresh backup requires an accessible running owned VM. Inspect rss status, then start the owned VM before retrying; no deletion override exists."
            )
        resources, identity = self._resources(owner)
        plan = {
            "preview": True,
            "targets": {
                "machine": self.machine,
                "connections": self._connections(owner, inventory),
                **resources,
            },
            "ownership": "verified project machine, connection and resource labels",
            "backup_directory": self._backup_directory(),
            "backup_required": "fresh verified full instance backup before deletion",
            "packages": "retained; inspect host-additions.json and use remove-package separately",
            "retained": [
                "external backups",
                "Git checkout",
                "ownership and audit records",
                "Homebrew packages",
                "Tailscale installation and unrelated services",
                "unrelated Podman resources and defaults",
            ],
        }
        plan["confirmation"] = confirmation(
            plan, [owner, identity, inventory, self.runtime._image()]
        )
        return plan

    def remove_vm(self, confirm: str | None) -> dict[str, Any]:
        """Preview VM removal; applying requires a matching inspected plan."""
        plan = self._vm_plan()
        if plan.get("status") == "absent":
            self._resume_absent()
            return plan
        if confirm is None:
            return plan
        from rss_aggregator.backup_schedule import BackupSchedule
        from rss_aggregator.recovery import Recovery

        if confirm != plan["confirmation"]:
            raise self.failure(
                "Cleanup plan changed or confirmation is invalid; preview again. Nothing removed."
            )
        runtime = self.runtime
        owner, _machine = self._owner()
        maintenance = BackupSchedule(runtime)
        maintenance.begin(owner, "stop")
        recovery = Recovery(runtime)
        try:
            maintenance.phase("quiescing")
            recovery._engines_idle(owner, stop=True)
            maintenance.phase("snapshot")
            backup = recovery.snapshot(owner)
            self._verify_backup(backup, plan["backup_directory"])
            if self._vm_plan()["confirmation"] != confirm:
                raise self.failure(
                    "Resources changed during backup; VM retained. Preview again."
                )
        except Exception:
            # A completed failed export has released its inherited lease. Abrupt
            # parent death instead leaves both the lease and durable hold intact.
            recovery._engines_idle(owner, stop=False)
            maintenance.finish()
            raise self.failure(
                "VM removal backup/inspection failed; no resources deleted. Writers remain stopped, VM remains running, prior archives retained. Inspect locally and retry."
            ) from None
        foreign = [
            c
            for c in self._inventory()["connections"]
            if c["Name"] not in (self.machine, self.machine + "-root")
        ]
        receipt = {
            "version": 1,
            "status": "removing",
            "owner": owner,
            "plan": plan,
            "backup": backup,
            "foreign_connections": foreign,
        }
        runtime._save("removed-vm.json", receipt)
        try:
            # Identity/plan checks do not establish continued quiescence. A
            # native client may have resumed writers since the volume export.
            recovery._engines_idle(owner, stop=False)
        except Exception:
            raise self.failure(
                "Final idle-engine check failed; VM and data retained with maintenance. Inspect locally, use guarded backup-reconcile, then obtain a new preview and fresh quiesced capture before removal. Do not reuse the earlier archive to authorize deletion."
            ) from None
        maintenance.phase("stopping_vm")
        result = runtime._run("podman", "machine", "stop", self.machine, check=False)
        if result.returncode:
            raise self.failure(
                "VM stop failed; no removal attempted. Inspect locally and reconcile maintenance."
            )
        self._owner()
        self._verify_backup(backup, plan["backup_directory"])
        result = runtime._run(
            "podman", "machine", "rm", self.machine, input="y\n", check=False
        )
        if result.returncode:
            raise self.failure(
                "VM removal failed or is partial; backup retained. Inspect the exact machine/connections and maintenance locally; never broaden cleanup."
            )
        self._finish_removal(receipt)
        return {
            "preview": False,
            "removed": True,
            "targets": plan["targets"],
            "backup": backup,
            "retained": plan["retained"],
        }

    def _resume_absent(self) -> None:
        """Finish only our recorded removal after process death with the VM now absent."""
        from rss_aggregator.backup_schedule import BackupSchedule

        maintenance = BackupSchedule(self.runtime)
        if not maintenance.pending():
            return
        path = self.runtime.state_dir / "removed-vm.json"
        owner_path = self.runtime.state_dir / "owner.json"
        if not path.is_file() or not owner_path.is_file():
            raise self.failure(
                "Unresolved maintenance without a removal receipt; preserve records for inspection."
            )
        receipt = json.loads(path.read_text())
        marker = json.loads((self.runtime.state_dir / "maintenance.json").read_text())
        owner = json.loads(owner_path.read_text())
        if (
            receipt.get("version") != 1
            or receipt.get("status") not in ("removing", "removed")
            or receipt.get("owner") != owner
            or marker.get("version") != 1
            or marker.get("owner") != owner["token"]
            or marker.get("operation") != "stop"
            or marker.get("phase") != "stopping_vm"
        ):
            raise self.failure(
                "Unrecognized removal receipt/maintenance; preserve records for inspection."
            )
        self._verify_backup(receipt["backup"], receipt["plan"]["backup_directory"])
        self._finish_removal(receipt)

    def prepare_setup(self, inventory: dict[str, Any]) -> None:
        """Archive only a confirmed removed generation before creating fresh ownership."""
        path = self.runtime.state_dir / "removed-vm.json"
        owner_path = self.runtime.state_dir / "owner.json"
        if not path.exists() or not owner_path.exists():
            return
        receipt = json.loads(path.read_text())
        owner = json.loads(owner_path.read_text())
        if receipt.get("owner") != owner:
            return  # A later, still-owned generation is not retired again.
        if receipt.get("version") != 1 or receipt.get("status") != "removed":
            raise self.failure(
                "Removal is incomplete; inspect and finish removal before recreating."
            )
        if any(m["Name"] == self.machine for m in inventory["machines"]) or any(
            c["Name"] in (self.machine, self.machine + "-root")
            for c in inventory["connections"]
        ):
            raise self.failure(
                "Removed resource name has reappeared; refusing adoption during recreation."
            )
        retired = self.runtime.state_dir / ("retired-" + confirmation({}, owner)[:16])
        retired.mkdir(mode=0o700, exist_ok=True)
        if (
            retired.is_symlink()
            or retired.stat().st_uid != os.getuid()
            or retired.stat().st_mode & 0o077
        ):
            raise self.failure(
                "Retired audit directory ownership is unsafe; inspect locally."
            )
        for name in (
            "selected-volume.json",
            "recovery-volumes.json",
            "security.json",
            "serve.json",
            "operator-intent.json",
            "supervision-recovery.json",
            "owner.json",
        ):
            source = self.runtime.state_dir / name
            if os.path.lexists(source):
                target = retired / name
                if source.is_symlink() or os.path.lexists(target):
                    raise self.failure(
                        "Retiring ownership would overwrite or follow ambiguous state; inspect locally."
                    )
                source.rename(target)

    def _volume_plan(self, volume: str) -> dict[str, Any]:
        """Preview one unselected, unattached owned volume, never an engine sweep."""
        from rss_aggregator.recovery import volume_name

        volume = volume_name(volume)
        owner, machine = self._owner()
        if machine["State"] != "running":
            raise self.failure(
                "Inspect and start the owned VM before previewing volume deletion."
            )
        result = self.runtime._podman("volume", "exists", volume, check=False)
        if result.returncode == 1:
            return {"preview": True, "volume": volume, "status": "absent"}
        if result.returncode:
            raise self.failure("Cannot inspect volume existence; nothing deleted.")
        info = self._label("volume", volume, owner)
        if volume == self.runtime.volume:
            raise self.failure(
                "Selected data volume is not inactive; it cannot be permanently deleted by this command."
            )
        if info.get("MountCount", 0):
            raise self.failure("Mounted data volume cannot be deleted.")
        containers = self._json(
            "--connection", self.machine, "ps", "--all", "--format", "json"
        )
        for container in containers:
            names = container["Names"]
            name = names[0] if isinstance(names, list) else names
            item = self._json(
                "--connection", self.machine, "container", "inspect", name
            )[0]
            if any(mount.get("Name") == volume for mount in item["Mounts"]):
                raise self.failure(
                    "Volume is attached to a container, even if stopped; nothing deleted."
                )
        plan = {
            "preview": True,
            "volume": volume,
            "ownership": "verified project label",
            "irreversible": True,
            "backup_created": False,
            "retained": [
                "selected data volume",
                "all other volumes",
                "external backups",
                "containers",
                "machine",
                "host packages",
            ],
        }
        plan["confirmation"] = confirmation(plan, [owner, info, self.runtime.volume])
        return plan

    def delete_volume(self, volume: str | None, confirm: str | None) -> dict[str, Any]:
        """Permanently delete only a named inactive volume after exact-plan consent."""
        from rss_aggregator.recovery import RECOVERY_RECORD

        if volume is None:
            raise ValueError("Delete-volume requires an explicit volume name.")
        plan = self._volume_plan(volume)
        if confirm is None or plan.get("status") == "absent":
            return plan
        if (
            confirm != plan["confirmation"]
            or self._volume_plan(volume)["confirmation"] != confirm
        ):
            raise self.failure(
                "Volume plan changed or confirmation is invalid; preview again."
            )
        result = self.runtime._podman("volume", "rm", volume, check=False)
        if result.returncode:
            raise self.failure(
                "Volume removal refused or failed; inspect locally. No forced removal is attempted."
            )
        path = self.runtime.state_dir / RECOVERY_RECORD
        if path.exists():
            records = json.loads(path.read_text())
            records.pop(volume, None)
            self.runtime._save(RECOVERY_RECORD, records)
        return {**plan, "preview": False, "deleted": True}

    def _verify_backup(self, backup: dict[str, Any], directory: str) -> None:
        """Independently validate the freshly published external archive before deletion."""
        from rss_aggregator.archives import validate_archive

        path = Path(backup["archive"])
        if backup.get("verified") is not True or path.resolve().parent != Path(
            directory
        ):
            raise self.failure("Fresh external verified backup required.")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != backup["sha256"]:
            raise self.failure("Backup integrity changed; VM retained.")
        with tempfile.TemporaryDirectory(
            prefix=".rss-removal-check-", dir=directory
        ) as temporary:
            validate_archive(path, Path(temporary), self.runtime._image())

    def _finish_removal(self, receipt: dict[str, Any]) -> None:
        """Confirm absent targets and unchanged foreign identities before retiring the hold."""
        from rss_aggregator.backup_schedule import BackupSchedule

        inventory = self._inventory()
        if any(m["Name"] == self.machine for m in inventory["machines"]) or any(
            c["Name"] in (self.machine, self.machine + "-root")
            for c in inventory["connections"]
        ):
            raise self.failure(
                "VM removal is incomplete; preserve receipt, backup and maintenance for local inspection."
            )
        if sorted(inventory["connections"], key=lambda item: item["Name"]) != sorted(
            receipt["foreign_connections"], key=lambda item: item["Name"]
        ):
            raise self.failure(
                "Retained connections/defaults changed during removal; preserve backup and receipt for local inspection."
            )
        self.runtime._save("removed-vm.json", {**receipt, "status": "removed"})
        BackupSchedule(self.runtime).finish()
