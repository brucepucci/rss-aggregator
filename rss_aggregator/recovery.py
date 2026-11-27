"""Quiesced backups and explicit, non-overwriting native instance recovery."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import stat
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from rss_aggregator import native_service
from rss_aggregator.archives import (
    create_archive,
    sync_directory,
    unpack_data,
    validate_archive,
    write_data_tar,
)
from rss_aggregator.backup_schedule import SUCCESS, BackupSchedule
from rss_aggregator.private_access import PrivateAccess

if TYPE_CHECKING:
    from rss_aggregator.runtime import Runtime

BACKUP_RECORD = "last-backup.json"
SHUTDOWN_SUCCESS = "shutdown-backup-success.json"
SELECTION = "selected-instance.json"
INITIAL_INSTANCE = "default"
INSTANCE_RECORD = "instance.json"
_INITIAL_PROVISIONED = "default-provisioned.json"
_SELECTION_REQUIRED = "selection-required.json"
_WRITER_GENERATION = "writer-generation.json"
_DATA_IDENTITY = ("freshrss_version", "freshrss_sha256", "platform")


def instance_name(value: str) -> str:
    """Validate one explicit instance name rather than a path or option."""
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{0,47}", value
    ):
        raise ValueError(
            "Instance must be 1–48 lowercase letters, digits or hyphens, starting with a letter or digit."
        )
    return value


def selected_instance(state: Path) -> str:
    """Read explicit selection, failing closed once a selection was attempted."""
    from rss_aggregator.runtime import RuntimeFailure

    path = state / SELECTION
    try:
        selection = native_service.read(path)
        if selection is not None or os.path.lexists(path):
            return instance_name(selection["instance"])
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeFailure(
            "Invalid native instance selection; inspect locally and explicitly select the intended instance."
        ) from None
    if os.path.lexists(state / _SELECTION_REQUIRED):
        raise RuntimeFailure(
            "Instance selection is missing; inspect locally and explicitly select the intended instance."
        )
    return INITIAL_INSTANCE


def _contains(directory: Path, location: Path) -> bool:
    """Check ancestry even when the filesystem accepts case or Unicode aliases."""
    exists = directory.exists()
    return any(
        candidate == directory
        or (exists and candidate.exists() and candidate.samefile(directory))
        for candidate in (location, *location.parents)
    )


def _rename_exclusive(source: Path, target: Path) -> None:
    """Publish a directory atomically, refusing even an empty existing target."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = libc.renamex_np
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        result = rename(os.fsencode(source), os.fsencode(target), 0x4)  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        # Support synthetic recovery checks off macOS, not production deployment.
        rename = libc.renameat2
        rename.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        result = rename(-100, os.fsencode(source), -100, os.fsencode(target), 1)
    else:
        raise OSError("Exclusive instance publication is unavailable on this platform.")
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


class Recovery:
    """Keep native instance recovery within the runtime's ownership and lock."""

    def __init__(self, runtime: Runtime) -> None:
        """Bind the runtime for one CLI operation."""
        from rss_aggregator.runtime import RuntimeFailure

        self.runtime = runtime
        self.failure = RuntimeFailure

    def _require_lock(self) -> None:
        """Never mutate or copy data outside the serialized command boundary."""
        if self.runtime._command_lock_fd is None:
            raise self.failure("Native recovery requires the project command lock.")

    def _private(self, path: Path, *, directory: bool) -> None:
        """Reject links, unsupported types, foreign owners and public modes."""
        info = path.lstat()
        right_type = (
            stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        )
        if (
            not right_type
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or (not directory and info.st_nlink != 1)
        ):
            raise self.failure(
                "Managed recovery paths must be private, owned files and directories; links are refused."
            )

    def _directory(self) -> Path:
        """Require a private destination separate from checkout and native state."""
        setting = os.environ.get("RSS_BACKUP_DIR")
        if not setting or not Path(setting).expanduser().is_absolute():
            raise self.failure(
                "Set RSS_BACKUP_DIR to an absolute private directory outside the checkout."
            )
        raw = Path(setting).expanduser()
        if raw.is_symlink():
            raise self.failure("RSS_BACKUP_DIR must not be a symlink.")
        path = raw.resolve()
        checkout, state = self.runtime.root, self.runtime.state_dir
        for candidate in dict.fromkeys((Path(os.path.abspath(raw)), path)):
            if _contains(state, candidate) or _contains(candidate, state):
                raise self.failure(
                    "RSS_BACKUP_DIR must be outside and not contain RSS_STATE_DIR."
                )
            if _contains(checkout, candidate) or _contains(candidate, checkout):
                raise self.failure(
                    "RSS_BACKUP_DIR must be outside and not contain the checkout."
                )
            if any(
                os.path.lexists(parent / ".git")
                for parent in (candidate, *candidate.parents)
            ):
                raise self.failure("RSS_BACKUP_DIR must be outside every Git checkout.")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self._private(path, directory=True)
        except self.failure:
            raise self.failure(
                "RSS_BACKUP_DIR must be owned by you with private permissions (0700)."
            ) from None
        return path

    def _instances(self, *, create: bool = False) -> Path:
        """Locate the real private instance namespace without following links."""
        state = self.runtime.state_dir
        self._private(state, directory=True)
        directory = state / "instances"
        if create and not os.path.lexists(directory):
            directory.mkdir(mode=0o700)
            sync_directory(state)
        if os.path.lexists(directory):
            self._private(directory, directory=True)
        return directory

    def _data(
        self, instance: str, owner: dict[str, Any], identity: dict[str, Any]
    ) -> Path | None:
        """Find complete data only when its private record matches owner and pin."""
        base = self._instances() / instance_name(instance)
        if not os.path.lexists(base):
            return None
        # Case-insensitive filesystems must not silently select a differently
        # spelled directory, even when its data have the same owner and pin.
        self._private(base, directory=True)
        record_path = base / INSTANCE_RECORD
        try:
            self._private(record_path, directory=False)
            record = json.loads(record_path.read_text())
            self._private(base / "data", directory=True)
        except (OSError, ValueError):
            raise self.failure(
                "Instance is incomplete or unreadable; preserve it and inspect recovery state locally."
            ) from None
        if (
            not isinstance(record, dict)
            or type(record.get("version")) is not int
            or record["version"] != 1
            or record.get("instance") != instance
            or record.get("owner") != owner["token"]
        ):
            raise self.failure("Instance record does not establish managed ownership.")
        stored = record.get("identity")
        if (
            not isinstance(stored, dict)
            or stored.keys() != identity.keys()
            or any(stored[key] != identity[key] for key in _DATA_IDENTITY)
        ):
            raise self.failure(
                "Instance requires its recorded FreshRSS release, source SHA-256 and platform before activation."
            )
        return base / "data"

    def _initial_absent(self, instance: str) -> bool:
        """Distinguish pristine state from a lost or interrupted instance."""
        state = self.runtime.state_dir
        return instance == INITIAL_INSTANCE and not any(
            os.path.lexists(state / name) for name in (SELECTION, _INITIAL_PROVISIONED)
        )

    def pristine(self) -> bool:
        """Return whether no default was attempted and no instance state exists."""
        self._require_lock()
        instances = self._instances()
        return self._initial_absent(self.runtime.instance) and (
            not instances.exists() or not any(instances.iterdir())
        )

    def _remember_default(self, instance: str) -> None:
        """Keep durable default provenance outside the instance tree."""
        if instance == INITIAL_INSTANCE and not os.path.lexists(
            self.runtime.state_dir / _INITIAL_PROVISIONED
        ):
            self.runtime._save(_INITIAL_PROVISIONED, {"version": 1})

    def check_activation(self) -> None:
        """Require selected data to be complete, private, owned and compatible."""
        runtime = self.runtime
        owner, _installed = runtime._owned()
        instance = runtime.instance
        if self._data(
            instance, owner, runtime._identity()
        ) is None and not self._initial_absent(instance):
            raise self.failure(
                "Selected instance data are absent; refusing activation."
            )

    def ensure_data(self) -> Path:
        """Return selected data, creating only the pristine initial instance."""
        self._require_lock()
        runtime = self.runtime
        owner, _installed = runtime._owned()
        identity = runtime._identity()
        instance = runtime.instance
        data = self._data(instance, owner, identity)
        if data is not None:
            self._remember_default(instance)
            return data
        if not self._initial_absent(instance):
            raise self.failure(
                "Selected instance data are absent; refusing activation."
            )
        self._publish(instance, owner, identity)
        return runtime.data_dir

    def _selected_data(self) -> Path:
        """Require an owned selected instance compatible with the installed pin."""
        self._require_lock()
        owner, installed = self.runtime._owned(require_current=False, verify=False)
        data = self._data(self.runtime.instance, owner, installed)
        if data is None:
            raise self.failure("Selected instance data are absent.")
        return data

    def _read_generation(self, data: Path) -> str | None:
        """Read a private generation record next to already validated instance data."""
        path = data.parent / _WRITER_GENERATION
        if not os.path.lexists(path):
            return None
        try:
            self._private(path, directory=False)
            record = native_service.read(path)
            if not isinstance(record, dict) or record.keys() != {"writer_generation"}:
                raise ValueError
            generation = record["writer_generation"]
            if (
                not isinstance(generation, str)
                or uuid.UUID(generation, version=4).hex != generation
            ):
                raise ValueError
        except (OSError, ValueError):
            raise self.failure(
                "Invalid writer generation record; preserve it and inspect selected instance state locally."
            ) from None
        return generation

    def mark_writer_start(self) -> None:
        """Durably invalidate earlier captures before a new writer may touch data."""
        data = self._selected_data()
        self._read_generation(data)
        native_service.save(
            data.parent / _WRITER_GENERATION, {"writer_generation": uuid.uuid4().hex}
        )

    def writer_generation(self) -> str | None:
        """Return the selected generation, or None for complete data with no record."""
        return self._read_generation(self._selected_data())

    def _publish(
        self,
        instance: str,
        owner: dict[str, Any],
        identity: dict[str, Any],
        *,
        payload: Path | None = None,
        legacy_image: dict[str, Any] | None = None,
    ) -> None:
        """Durably stage private data and provenance before exclusive publication."""
        # Record the provisioning attempt before creating data. An interruption
        # may require recovery, but must never authorize silent reinitialization.
        self._remember_default(instance)
        directory = self._instances(create=True)
        target = directory / instance
        record = {
            "version": 1,
            "instance": instance,
            "owner": owner["token"],
            "identity": identity,
        }
        if legacy_image is not None:
            record["legacy_image"] = legacy_image
        with tempfile.TemporaryDirectory(
            prefix=".restore-", dir=directory
        ) as temporary:
            stage = Path(temporary) / "instance"
            stage.mkdir(mode=0o700)
            if payload is None:
                (stage / "data").mkdir(mode=0o700)
                sync_directory(stage / "data")
            else:
                unpack_data(payload, stage / "data")
            descriptor = os.open(
                stage / INSTANCE_RECORD, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "w") as output:
                json.dump(record, output, allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            sync_directory(stage)
            try:
                _rename_exclusive(stage, target)
            except OSError as error:
                if error.errno in (errno.EEXIST, errno.ENOTEMPTY):
                    raise self.failure(
                        "Instance target already exists; choose a fresh name. Nothing overwritten."
                    ) from None
                raise self.failure(
                    "Could not publish the native instance atomically."
                ) from None
            sync_directory(directory)

    def _quiesce(self, owner: dict[str, Any]) -> bool:
        """Stop all writers and report whether owned forwarding was withdrawn."""
        self._require_lock()
        try:
            withdrawn = PrivateAccess(self.runtime).stop(owner)
        finally:
            self.runtime._stop_writers()
        if not self.runtime._idle():
            raise self.failure("FreshRSS writers are still running; snapshot refused.")
        return withdrawn

    def _snapshot(
        self,
        data: Path,
        directory: Path,
        instance: str,
        identity: dict[str, Any],
        *,
        via: Literal["stop", "backup"],
    ) -> dict[str, Any]:
        """Publish verified data and record success independently of resumption."""
        runtime = self.runtime
        if not runtime._idle():
            raise self.failure("FreshRSS writers are still running; snapshot refused.")
        generation = self._read_generation(data)
        with tempfile.TemporaryDirectory(
            prefix=".rss-export-", dir=directory
        ) as temporary:
            payload = write_data_tar(data, Path(temporary))
            result = create_archive(payload, directory, runtime=identity)
        runtime._save(
            BACKUP_RECORD,
            {"archive": result["archive"], "sha256": result["sha256"]},
        )
        evidence = {
            **result,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "instance": instance,
            "writer_generation": generation,
            "via": via,
        }
        runtime._save(SUCCESS, evidence)
        if via == "stop":
            runtime._save(SHUTDOWN_SUCCESS, evidence)
        return result

    def backup(self) -> dict[str, Any]:
        """Back up complete data and resume only previously running, allowed web."""
        self._require_lock()
        runtime = self.runtime
        directory = self._directory()
        owner, _installed = runtime._owned()
        instance, identity = runtime.instance, runtime._identity()
        data = self._data(instance, owner, identity)
        if data is None:
            raise self.failure("Selected instance has no data to back up.")
        self._remember_default(instance)
        status = runtime._status()
        if status["application"] not in ("running", "stopped"):
            raise self.failure(
                "FreshRSS writer state is unconfirmed; stop and inspect before backup."
            )
        was_running = status["application"] == "running"
        port = status.get("port") if was_running else None
        was_private = False
        if was_running:
            live = runtime._live()
            if (
                live is None
                or type(port) is not int
                or not 1024 <= port <= 65535
                or live[0].get("port") != port
                or type(live[0].get("private")) is not bool
            ):
                raise self.failure(
                    "Cannot confirm the active FreshRSS backend or its access mode; backup refused."
                )
            was_private = live[0]["private"]
        maintenance = BackupSchedule(runtime)
        maintenance.begin(owner, "backup")
        settled = False
        try:
            maintenance.phase("quiescing")
            withdrawn = self._quiesce(owner)
            maintenance.phase("snapshot")
            try:
                result = self._snapshot(
                    data, directory, instance, identity, via="backup"
                )
            except Exception:
                settled = True
                raise
            settled = True
            return result
        finally:
            try:
                if settled:
                    if not runtime._idle():
                        raise self.failure(
                            "Cannot confirm writers are stopped; reconcile maintenance."
                        )
                    if was_running and maintenance.may_resume():
                        maintenance.phase("resuming")
                        runtime._start(
                            private_https=withdrawn,
                            port_override=port,
                            check_private_backend=was_private and not withdrawn,
                        )
                    maintenance.finish()
            finally:
                if maintenance.pending():
                    maintenance.notice()

    def capture_shutdown(self, *, already_stopped: bool) -> dict[str, Any]:
        """Capture the shutdown archive after explicit stop proved writers idle."""
        self._require_lock()
        runtime = self.runtime
        if not runtime._idle():
            raise self.failure("FreshRSS writers are still running; snapshot refused.")
        owner, installed = runtime._ownership_receipt()
        instance = runtime.instance
        data = self._data(instance, owner, installed)
        if data is None:
            if not self._initial_absent(instance):
                raise self.failure("Selected instance data are absent.")
            backup = {"status": "no_data"}
        else:
            self._remember_default(instance)
            if runtime._owned(require_current=False) != (owner, installed):
                raise self.failure(
                    "Installed runtime identity changed during shutdown."
                )
            backup = self._snapshot(
                data, self._directory(), instance, installed, via="stop"
            )
        if not runtime._idle():
            raise self.failure(
                "Cannot confirm writers are stopped; reconcile maintenance."
            )
        return {
            "application": "stopped",
            "already_stopped": already_stopped,
            "backup": backup,
        }

    def _restore(self, archive: Path, instance: str, *, legacy: bool) -> dict[str, Any]:
        """Validate one format and publish a fresh owned instance without selection."""
        self._require_lock()
        runtime = self.runtime
        instance = instance_name(instance)
        if instance.casefold() == runtime.instance.casefold():
            raise self.failure(
                "Restore target is currently selected; choose a different fresh name. Nothing overwritten."
            )
        with tempfile.TemporaryDirectory(prefix="rss-restore-") as temporary:
            path = Path(temporary)
            if legacy:
                image = json.loads((runtime.root / "config/image.json").read_text())
                manifest, payload = validate_archive(
                    archive, path, image, native_paths=True
                )
                identity = runtime._identity()
                if manifest["image"]["version"] != identity["freshrss_version"]:
                    raise self.failure(
                        "Legacy import requires the same FreshRSS version as the native pin."
                    )
            else:
                identity = runtime._identity()
                manifest, payload = validate_archive(archive, path, runtime=identity)
            owner, _installed = runtime._owned()
            target = self._instances() / instance
            if os.path.lexists(target):
                raise self.failure(
                    "Instance target already exists; choose a fresh name. Nothing overwritten."
                )
            self._publish(
                instance,
                owner,
                identity,
                payload=payload,
                legacy_image=manifest.get("image"),
            )
        result = {
            "restored": True,
            "instance": instance,
            "selected": False,
            "manifest": manifest,
            "next_step": "Stop the application, then run rss select-instance NAME and rss start to activate explicitly.",
        }
        if legacy:
            result["imported"] = True
        return result

    def restore(self, archive: Path, instance: str) -> dict[str, Any]:
        """Restore only an exactly compatible v2 native archive."""
        return self._restore(archive, instance, legacy=False)

    def import_legacy(self, archive: Path, instance: str) -> dict[str, Any]:
        """Import a matching v1 archive; never access old Podman resources."""
        return self._restore(archive, instance, legacy=True)

    def select_instance(self, instance: str) -> dict[str, Any]:
        """Select a complete owned instance only while all writers are idle."""
        self._require_lock()
        runtime = self.runtime
        instance = instance_name(instance)
        owner, _installed = runtime._owned()
        if BackupSchedule(runtime).pending() or not runtime._idle():
            raise self.failure(
                "Stop FreshRSS and reconcile any maintenance before selecting an instance."
            )
        if self._data(instance, owner, runtime._identity()) is None:
            raise self.failure("Instance is absent; select a complete owned instance.")
        self._remember_default(instance)
        # Write the independent guard first: an interrupted first selection
        # requires explicit repair, never implicit activation of the default.
        marker = runtime.state_dir / _SELECTION_REQUIRED
        if not os.path.lexists(marker):
            runtime._save(_SELECTION_REQUIRED, {"version": 1})
        runtime._save(SELECTION, {"instance": instance})
        return {
            "instance": instance,
            "selected": True,
            "next_step": "Run rss start to activate the selection. Existing instance data are preserved.",
        }
