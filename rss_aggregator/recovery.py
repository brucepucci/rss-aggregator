"""Quiesced instance backups and non-overwriting recovery on the owned engine."""

import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rss_aggregator.archives import create_archive, validate_archive

if TYPE_CHECKING:
    from rss_aggregator.runtime import Runtime

RECOVERY_RECORD = "recovery-volumes.json"
BACKUP_RECORD = "last-backup.json"


def volume_name(value: str) -> str:
    """Validate a single explicit Podman volume name, never an option or path."""
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", value):
        raise ValueError("Volume must be a 1–128 character Podman name, not a path.")
    return value


class Recovery:
    """Keep recovery operations behind the runtime's ownership checks and lock."""

    def __init__(self, runtime: "Runtime") -> None:
        """Bind the active runtime and its resource names, including test fixtures."""
        from rss_aggregator.runtime import APP, CHECK, LABEL, MACHINE, RuntimeFailure

        self.runtime = runtime
        self.app, self.check, self.label, self.machine = APP, CHECK, LABEL, MACHINE
        self.failure = RuntimeFailure

    def _directory(self) -> Path:
        """Require an explicitly chosen private backup directory outside Git."""
        setting = os.environ.get("RSS_BACKUP_DIR")
        if not setting or not Path(setting).expanduser().is_absolute():
            raise self.failure(
                "Set RSS_BACKUP_DIR to an absolute private directory outside the checkout."
            )
        path = Path(setting).expanduser().resolve()
        if path == self.runtime.root or self.runtime.root in path.parents:
            raise self.failure("RSS_BACKUP_DIR must be outside the checkout.")
        if any((parent / ".git").exists() for parent in (path, *path.parents)):
            raise self.failure("RSS_BACKUP_DIR must be outside every Git checkout.")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise self.failure(
                "RSS_BACKUP_DIR must be owned by you with private permissions (0700)."
            )
        return path

    def _ensure_vm(self) -> tuple[dict[str, Any], bool]:
        """Start only an owned stopped VM when no other machine is running."""
        runtime = self.runtime
        owner, machine = runtime._owned()
        was_stopped = machine["State"] == "stopped"
        if machine["State"] not in ("running", "stopped"):
            raise self.failure(
                "Wait for the project VM transition to finish before recovery."
            )
        if was_stopped:
            if any(
                m["Name"] != self.machine and m.get("Running")
                for m in runtime._inventory()["machines"]
            ):
                raise self.failure(
                    "Another machine is running; it has not been changed."
                )
            runtime._run(
                "podman", "machine", "start", "--update-connection=false", self.machine
            )
            runtime._owned()
        return owner, was_stopped

    def _engines_idle(self, owner: dict[str, Any], *, stop: bool) -> None:
        """Refuse foreign workloads before stopping any owned application writers."""
        runtime = self.runtime
        rootful = runtime._json_run(
            "podman",
            "machine",
            "ssh",
            self.machine,
            "sudo",
            "podman",
            "ps",
            "--format",
            "json",
        )
        containers = json.loads(runtime._podman("ps", "--format", "json").stdout)
        if not isinstance(rootful, list) or not isinstance(containers, list):
            raise self.failure(
                "Cannot inspect both container engines; VM left running."
            )
        if rootful:
            raise self.failure(
                "Unrelated running rootful containers; project VM left running."
            )
        for item in containers:
            name = (
                item["Names"][0] if isinstance(item["Names"], list) else item["Names"]
            )
            if name not in (self.app, self.check):
                raise self.failure(
                    "Unrelated running container; project VM left running."
                )
            runtime._check_label("container", name, owner)
        if not stop and containers:
            raise self.failure("Writers are still running; snapshot refused.")
        if stop:
            for name in (self.app, self.check):
                if runtime._exists("container", name):
                    item = runtime._check_label("container", name, owner)
                    if item["State"]["Running"]:
                        runtime._podman("stop", "--time", "60", name)
                        if runtime._check_label("container", name, owner)["State"][
                            "Running"
                        ]:
                            raise self.failure(
                                "Application did not stop; VM left running."
                            )

    def _source_image(self, owner: dict[str, Any]) -> dict[str, Any]:
        """Record only the pinned identity actually attached to the source app."""
        runtime = self.runtime
        self.check_activation()
        image = runtime._image()
        info = runtime._json_run(
            "podman", "--connection", self.machine, "image", "inspect", image["image"]
        )[0]
        if (
            image["image"] not in info["RepoDigests"]
            or image["image"].split("@")[0] + "@" + image["platform_manifest"]
            not in info["RepoDigests"]
            or info["Os"] + "/" + info["Architecture"] != image["platform"]
            or info.get("Labels", {}).get("org.opencontainers.image.version")
            != image["version"]
            or info.get("Labels", {}).get("org.opencontainers.image.revision")
            != image["revision"]
        ):
            raise self.failure(
                "Source image differs from the pinned recovery image; reconcile configuration first."
            )
        if runtime._exists("container", self.app):
            app = runtime._check_label("container", self.app, owner)
            if any(
                m.get("Name") == runtime.volume
                and m.get("Destination") == "/var/www/FreshRSS/data"
                for m in app["Mounts"]
            ):
                if app["Image"].removeprefix("sha256:") != info["Id"].removeprefix(
                    "sha256:"
                ):
                    raise self.failure(
                        "Source container differs from the pinned image; do not mislabel a backup."
                    )
                return image
            if app["State"]["Running"]:
                raise self.failure(
                    "Source container does not mount the selected data volume."
                )
        record_path = runtime.state_dir / RECOVERY_RECORD
        records = json.loads(record_path.read_text()) if record_path.exists() else {}
        record = records.get(runtime.volume, {})
        if record.get("status") != "ready" or record.get("image") != image:
            raise self.failure(
                "Cannot establish the unattached volume's image. Start it with the reviewed matching pin before backup."
            )
        return image

    def check_activation(self) -> None:
        """Require the recorded image until a volume completes its first start.

        Older records without an activation marker are treated conservatively:
        start once with their recorded pin before attempting an image update.
        This guard never changes provenance, including on refusal.
        """
        runtime = self.runtime
        path = runtime.state_dir / RECOVERY_RECORD
        records = json.loads(path.read_text()) if path.exists() else {}
        record = records.get(runtime.volume)
        if record is None:
            return
        if record.get("status") != "ready":
            raise self.failure("Restore target is incomplete and cannot be activated.")
        if record.get("activated") is not True and record["image"] != runtime._image():
            raise self.failure(
                "First activation requires the recorded image; restore the matching pin and complete start or replace before an intentional image update."
            )

    def record_image(self, *, activated: bool = False) -> None:
        """Retain provenance; only a completed start marks first activation.

        Taking a snapshot must not discharge a pending recovery activation.
        Once activated, deliberate reviewed pin updates remain supported.
        """
        self.check_activation()
        runtime = self.runtime
        path = runtime.state_dir / RECOVERY_RECORD
        records = json.loads(path.read_text()) if path.exists() else {}
        records[runtime.volume] = {
            "status": "ready",
            "image": runtime._image(),
            "activated": activated
            or records.get(runtime.volume, {}).get("activated") is True,
        }
        runtime._save(RECOVERY_RECORD, records)

    def snapshot(
        self, owner: dict[str, Any], directory: Path | None = None
    ) -> dict[str, Any]:
        """Export a fully quiesced owned volume and publish only a verified archive."""
        runtime = self.runtime
        directory = directory if directory is not None else self._directory()
        runtime._check_label("volume", runtime.volume, owner)
        self._engines_idle(owner, stop=False)
        image = self._source_image(owner)
        self.record_image()
        with tempfile.TemporaryDirectory(
            prefix=".rss-export-", dir=directory
        ) as temporary:
            payload = Path(temporary) / "data.tar"
            payload.touch(mode=0o600)
            result = runtime._podman(
                "volume",
                "export",
                "--output",
                str(payload),
                runtime.volume,
                check=False,
            )
            if result.returncode:
                raise self.failure(
                    "Volume export failed; no archive published. Inspect the local engine without sharing sensitive output."
                )
            result = create_archive(payload, directory, image)
        runtime._save(
            BACKUP_RECORD,
            {"archive": result["archive"], "sha256": result["sha256"]},
        )
        return result

    def backup(self) -> dict[str, Any]:
        """Back up while preserving the prior VM/application running state."""
        runtime = self.runtime
        directory = self._directory()
        owner, was_stopped = self._ensure_vm()
        was_running = False
        quiesced = False
        try:
            runtime._check_label("volume", runtime.volume, owner)
            self._source_image(owner)
            if runtime._exists("container", self.app):
                was_running = runtime._check_label("container", self.app, owner)[
                    "State"
                ]["Running"]
            self._engines_idle(owner, stop=True)
            quiesced = True
            return self.snapshot(owner, directory)
        finally:
            if was_running and quiesced:
                runtime._start()
            elif was_stopped:
                self._engines_idle(owner, stop=False)
                runtime._run("podman", "machine", "stop", self.machine)

    def shutdown(self) -> dict[str, Any]:
        """Back up stopped writers before stopping the VM; fail closed on export errors."""
        runtime = self.runtime
        owner, machine = runtime._owned()
        if machine["State"] == "stopped":
            return {
                "machine": "stopped",
                "already_stopped": True,
                "backup": {"status": "already_stopped"},
            }
        if machine["State"] != "running":
            raise self.failure(
                "Project VM is "
                + machine["State"]
                + "; wait and inspect before retrying."
            )
        self._engines_idle(owner, stop=True)
        try:
            backup = (
                self.snapshot(owner)
                if runtime._exists("volume", runtime.volume)
                else {"status": "no_data"}
            )
        except (self.failure, OSError, ValueError, KeyError, TypeError) as error:
            raise self.failure(
                "Shutdown backup failed; application writers remain stopped and VM remains running. Fix RSS_BACKUP_DIR/export and retry rss stop. "
                + str(error)
            ) from error
        self._engines_idle(owner, stop=False)
        runtime._run("podman", "machine", "stop", self.machine)
        return {"machine": "stopped", "already_stopped": False, "backup": backup}

    def restore(self, archive: Path, volume: str) -> dict[str, Any]:
        """Validate first, then import into an absent labelled volume without activation."""
        runtime = self.runtime
        volume = volume_name(volume)
        # Validation is wholly local and precedes VM startup or volume creation.
        with tempfile.TemporaryDirectory(prefix="rss-restore-") as temporary:
            manifest, payload = validate_archive(
                archive, Path(temporary), runtime._image()
            )
            if volume == runtime.volume:
                raise self.failure(
                    "Restore target is currently selected; choose a different fresh name. Restore never activates itself."
                )
            owner, _was_stopped = self._ensure_vm()
            if runtime._exists("volume", volume):
                raise self.failure(
                    "Restore target already exists; choose a fresh volume. Nothing overwritten."
                )
            record_path = runtime.state_dir / RECOVERY_RECORD
            records = (
                json.loads(record_path.read_text()) if record_path.exists() else {}
            )
            records[volume] = {
                "status": "incomplete",
                "image": manifest["image"],
                "activated": False,
            }
            runtime._save(RECOVERY_RECORD, records)
            runtime._podman(
                "volume", "create", "--label", self.label + "=" + owner["token"], volume
            )
            runtime._check_label("volume", volume, owner)
            result = runtime._podman(
                "volume", "import", volume, str(payload), check=False
            )
            if result.returncode:
                raise self.failure(
                    "Restore import failed; incomplete owned target retained, not selected. Choose a new name when retrying; source data is unchanged."
                )
            records[volume]["status"] = "ready"
            runtime._save(RECOVERY_RECORD, records)
            return {
                "restored": True,
                "volume": volume,
                "selected": False,
                "manifest": manifest,
                "next_step": "Stop the application, then run rss select-volume NAME and rss replace to activate explicitly.",
            }

    def select_volume(self, volume: str) -> dict[str, Any]:
        """Select an owned recovery volume only while the application is stopped."""
        runtime = self.runtime
        volume = volume_name(volume)
        owner, machine = runtime._owned()
        if machine["State"] != "running":
            raise self.failure(
                "Start the owned VM before selecting a volume; do not start the application."
            )
        runtime._check_label("volume", volume, owner)
        record_path = runtime.state_dir / RECOVERY_RECORD
        records = json.loads(record_path.read_text()) if record_path.exists() else {}
        if volume in records and records[volume]["status"] != "ready":
            raise self.failure("Restore target is incomplete and cannot be selected.")
        if volume in records and records[volume]["image"] != runtime._image():
            raise self.failure(
                "Restore image differs from the configured image; reconcile the pin before selection."
            )
        if runtime._exists("container", self.app):
            app = runtime._check_label("container", self.app, owner)
            if app["State"]["Running"]:
                raise self.failure(
                    "Stop the application before selecting a different data volume."
                )
        runtime._save(runtime.selected_volume_path.name, {"volume": volume})
        return {
            "volume": volume,
            "selected": True,
            "next_step": "Run rss replace if an old application container exists, otherwise rss start. The original data volume is preserved.",
        }
