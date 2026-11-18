"""Explicit, preview-first removal of the owned VM and inactive data volumes."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from rss_aggregator.runtime import Runtime


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

    def _json(self, *args: str) -> Any:
        """Read native metadata without copying sensitive native diagnostics."""
        result = self.runtime._run("podman", *args, check=False)
        if result.returncode:
            raise self.failure(
                "Cleanup inspection failed; resources retained. Inspect the explicitly named VM locally; recover an unavailable source without deleting it."
            )
        records = json.loads(result.stdout)
        if not isinstance(records, list) or any(
            not isinstance(item, dict) for item in records
        ):
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

    def _resources(self, owner: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        """Reject unbacked secrets, extra volumes, foreign containers and images."""
        runtime = self.runtime
        connection = ("--connection", self.machine)
        for args in (
            ("ps", "--all"),
            ("volume", "ls"),
            ("images",),
            ("secret", "ls"),
        ):
            if self._json(
                "machine",
                "ssh",
                self.machine,
                "sudo",
                "podman",
                *args,
                "--format",
                "json",
            ):
                raise self.failure(
                    "Unrelated rootful resources exist; VM removal refused."
                )
        # The instance archive covers no engine secrets. List metadata only;
        # never inspect payloads or include secret names in reports/receipts.
        if self._json(*connection, "secret", "ls", "--format", "json"):
            raise self.failure(
                "Unrelated rootless secrets exist; VM removal refused. The instance backup does not preserve Podman secrets."
            )
        containers = self._json(*connection, "ps", "--all", "--format", "json")
        volumes = self._json(*connection, "volume", "ls", "--format", "json")
        images = self._json(*connection, "images", "--format", "json")
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
        }, [
            details,
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
