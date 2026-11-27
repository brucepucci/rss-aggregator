"""Opt-in daily backup orchestration and secret-safe protection evidence."""

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from rss_aggregator.private_access import PrivateAccess
from rss_aggregator.supervision import MAINTENANCE, NativeJob, Supervision

if TYPE_CHECKING:
    from rss_aggregator.runtime import Runtime

ATTEMPT = "backup-attempt.json"
SUCCESS = "backup-success.json"
SCHEDULE = "backup-schedule.json"
DAILY = "backup-daily.json"
DEFER = "backup-defer.json"
RETRY_SECONDS = 900
MAX_ATTEMPTS = 3


class BackupSchedule:
    """Keep daily backup policy behind the runtime's public command lock."""

    def __init__(self, runtime: "Runtime", now: datetime | None = None) -> None:
        """Bind one runtime and an optional controlled aware orchestration clock."""
        self.runtime = runtime
        self._job = NativeJob(runtime, "backup", "backup-due")
        self.controlled_clock = now is not None
        self.now = now if now is not None else datetime.now(timezone.utc)
        if self.now.tzinfo is None:
            raise ValueError("Backup clock must include a time zone.")
        self.now = self.now.astimezone(timezone.utc)

    def _read(self, name: str) -> Any:
        """Read private managed state without treating malformed state as absence."""
        path = self.runtime.state_dir / name
        if path.is_symlink():
            raise ValueError("Refusing symlinked backup or maintenance state.")
        return json.loads(path.read_text()) if path.exists() else None

    def backup(self) -> dict[str, Any]:
        """Record an explicit backup's outcome separately from verified publication."""
        from rss_aggregator.recovery import Recovery

        return self._attempt(Recovery(self.runtime).backup, "backup")

    def shutdown(self) -> dict[str, Any]:
        """Record normal-stop backup outcomes without changing its failure semantics."""
        from rss_aggregator.recovery import Recovery

        return self._attempt(Recovery(self.runtime).shutdown, "stop")

    def _attempt(
        self, operation: Callable[[], dict[str, Any]], kind: str
    ) -> dict[str, Any]:
        """Keep effective capture evidence; an idempotent stop is not a new capture."""
        previous = self._read(ATTEMPT) if kind == "stop" else None
        attempt = {
            "started_at": self.now.isoformat(),
            "outcome": "running",
            "kind": kind,
            "instance": self.runtime.instance,
        }
        self.runtime._save(ATTEMPT, attempt)
        try:
            result = operation()
        except Exception:
            resumption = "unresolved" if self.pending() else "preserved"
            if self.pending():
                try:
                    marker = self._read(MAINTENANCE)
                    if isinstance(marker, dict) and marker.get("phase") == "resuming":
                        resumption = "failed"
                except (OSError, ValueError):
                    # Preserve the original failure and the unresolved hold.
                    pass
            self.runtime._save(
                ATTEMPT,
                {
                    **attempt,
                    "outcome": "failed",
                    "resumption": resumption,
                    "action": "Inspect backup configuration and maintenance status locally; reconcile interrupted maintenance before retrying.",
                },
            )
            raise
        archive = result if kind == "backup" else result["backup"]
        if (
            kind == "stop"
            and archive.get("status") == "already_stopped"
            and previous is not None
        ):
            # Retain failures too: a no-op cannot repair a failed capture/resumption.
            self.runtime._save(ATTEMPT, previous)
            return result
        if self.controlled_clock and archive.get("verified"):
            # Recovery owns publication evidence. Only align the clock for this
            # exact success under an explicitly injected Runtime clock.
            success = self._read(SUCCESS)
            if (
                isinstance(success, dict)
                and success.get("archive") == archive["archive"]
                and success.get("sha256") == archive["sha256"]
            ):
                self.runtime._save(
                    SUCCESS, {**success, "completed_at": self.now.isoformat()}
                )
        self.runtime._save(
            ATTEMPT,
            {
                **attempt,
                "outcome": "success" if archive.get("verified") else archive["status"],
                "resumption": "preserved",
            },
        )
        return result

    @property
    def job(self) -> Path:
        """Locate this user's backup LaunchAgent for the selected ownership directory."""
        return self._job.path

    def _config(self) -> dict[str, Any] | None:
        """Require the owned ledger and an internally consistent captured policy."""
        config = self._job.read(SCHEDULE)
        if config is None:
            return None
        environment = config["environment"]
        launchctl = Path(config["dependencies"]["launchctl"])
        if (
            type(config.get("active")) is not bool
            or config.get("timezone") != "UTC"
            or type(config.get("hour")) is not int
            or not 0 <= config["hour"] <= 23
            or type(config.get("minute")) is not int
            or not 0 <= config["minute"] <= 59
            or not isinstance(environment, dict)
            or not isinstance(environment.get("PATH"), str)
            or config.get("destination") != environment.get("RSS_BACKUP_DIR")
            or not Path(config["destination"]).is_absolute()
            or launchctl.name != "launchctl"
            or not launchctl.is_absolute()
            or str(launchctl.parent) not in environment["PATH"].split(os.pathsep)
            or config.get("policy_id") != self._policy_id(config)
        ):
            raise ValueError("Invalid backup schedule ledger; inspect locally.")
        if "registration_id" in config or "registered_at" in config:
            if (
                not config["active"]
                or not isinstance(config.get("registration_id"), str)
                or not config["registration_id"]
                or datetime.fromisoformat(config["registered_at"]).utcoffset()
                != timedelta(0)
            ):
                raise ValueError(
                    "Invalid backup registration evidence; inspect locally."
                )
        return config

    def _policy_id(self, config: dict[str, Any]) -> str:
        """Identify the captured definition, UTC target and launcher/manifest pins."""
        encoded = self._job.definition(config, RETRY_SECONDS)
        policy = f"{config['hour']}:{config['minute']}:{config['owner']}:{config['manifest_sha256']}:{config['python_sha256']}"
        return hashlib.sha256(encoded + policy.encode()).hexdigest()

    def _job_matches(self, config: dict[str, Any]) -> bool:
        """Verify the exact owned definition without adopting another job."""
        return self._job.matches(self._job.definition(config, RETRY_SECONDS))

    def enable(self, hour: int = 3, minute: int = 0) -> dict[str, Any]:
        """Explicitly install a private per-user launchd wakeup with a daily UTC policy."""
        from rss_aggregator.recovery import Recovery
        from rss_aggregator.runtime import RuntimeFailure

        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("Daily UTC hour/minute must be 0–23 and 0–59.")
        directory = Recovery(self.runtime)._directory()
        owner, _installed = self.runtime._owned()
        info = self.runtime.state_dir.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise RuntimeFailure(
                "Scheduling requires a private owned RSS_STATE_DIR (0700); fix permissions explicitly."
            )
        existing = self._config()
        captured = self._job.capture(owner, directory)
        self._job.check(captured)
        encoded = self._job.definition(captured, RETRY_SECONDS)
        config = {
            **captured,
            "hour": hour,
            "minute": minute,
            "timezone": "UTC",
            "destination": str(directory),
        }
        config["policy_id"] = self._policy_id(config)
        if (
            existing
            and {
                k: v
                for k, v in existing.items()
                if k not in ("active", "registered_at", "registration_id")
            }
            != config
        ):
            raise RuntimeFailure(
                "Backup schedule settings changed; explicitly disable the owned schedule before re-enabling."
            )
        if os.path.lexists(self.job) and (
            not existing or not self._job_matches(existing)
        ):
            raise RuntimeFailure(
                "Refusing conflicting/unowned backup LaunchAgent definition."
            )
        if self._job.launch(config, "print", self._job.domain).returncode:
            raise RuntimeFailure(
                "Normal user GUI session unavailable; log in/unlock and retry."
            )
        loaded = self._job.loaded(config)
        if loaded and not existing:
            raise RuntimeFailure("Refusing unowned loaded backup LaunchAgent.")
        if loaded and not self.job.exists():
            raise RuntimeFailure(
                "Loaded backup job definition is missing; preserve the registration and inspect locally."
            )
        if not loaded:
            self.runtime._save(SCHEDULE, {**config, "active": False})
            if not self.job.exists():
                self._job.write(encoded)
            if self._job.launch(
                config, "bootstrap", self._job.domain, str(self.job)
            ).returncode:
                raise RuntimeFailure(
                    "Backup LaunchAgent activation failed; inspect the normal user's GUI session and retry enable. Configuration is not protection."
                )
        if (
            loaded
            and existing
            and existing["active"]
            and existing.get("registration_id")
        ):
            config = existing
        else:
            config["active"] = True
            config["registration_id"] = uuid4().hex
            config["registered_at"] = (
                self.now if self.controlled_clock else datetime.now(timezone.utc)
            ).isoformat()
            self.runtime._save(SCHEDULE, config)
        return {
            "enabled": True,
            "job": str(self.job),
            "daily_utc": f"{hour:02}:{minute:02}",
            "next_expected_attempt": self._next(config, self._read(DAILY)).isoformat(),
        }

    def disable(self) -> dict[str, Any]:
        """Remove only the exact owned job, retaining evidence and all archives."""
        from rss_aggregator.runtime import RuntimeFailure

        config = self._config()
        if config is None:
            if os.path.lexists(self.job):
                raise RuntimeFailure("Backup job is unowned; nothing removed.")
            return {"enabled": False}
        self._removable_definition(config)
        if (
            self._job.loaded(config)
            and self._job.launch(
                config, "bootout", self._job.domain + "/" + self._job.label
            ).returncode
        ):
            raise RuntimeFailure(
                "Backup job removal failed; configuration retained for inspection."
            )
        self._unlink_durable(self._removable_definition(config))
        self._unlink_durable(self.runtime.state_dir / SCHEDULE)
        return {"enabled": False, "archives_preserved": True}

    def _removable_definition(self, config: dict[str, Any]) -> Path:
        """Allow a missing plist, but never unlink an altered or foreign definition."""
        from rss_aggregator.runtime import RuntimeFailure

        path = self.job
        if os.path.lexists(path) and not self._job_matches(config):
            raise RuntimeFailure("Backup job definition changed; refusing removal.")
        return path

    @staticmethod
    def _unlink_durable(path: Path) -> None:
        """Persist unlink order, allowing an already completed stage on retry."""
        path.unlink(missing_ok=True)
        try:
            descriptor = os.open(path.parent, os.O_RDONLY)
        except FileNotFoundError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _slot(self, config: dict[str, Any]) -> datetime:
        """Identify the latest elapsed UTC slot, even on the other side of midnight."""
        today = self.now.replace(
            hour=config["hour"], minute=config["minute"], second=0, microsecond=0
        )
        return today if today <= self.now else today - timedelta(days=1)

    def _eligible(self, config: dict[str, Any], slot: datetime) -> bool:
        """Exclude all targets before activation, including one earlier the same day."""
        registered_at = config.get("registered_at")
        return bool(
            config["active"]
            and registered_at
            and datetime.fromisoformat(registered_at) <= slot
        )

    def _same_registration(
        self, daily: dict[str, Any] | None, config: dict[str, Any]
    ) -> bool:
        """Bind daily evidence to one successful registration and selected instance."""
        return bool(
            daily
            and config["active"]
            and config.get("registration_id")
            and daily.get("registration_id") == config["registration_id"]
            and daily.get("policy_id") == config["policy_id"]
            and daily.get("instance") == self.runtime.instance
        )

    def _same_target(
        self, daily: dict[str, Any] | None, config: dict[str, Any], slot: datetime
    ) -> bool:
        """Apply the current attempt budget only to this registration's elapsed slot."""
        return bool(
            daily
            and daily.get("slot") == slot.isoformat()
            and self._same_registration(daily, config)
        )

    def _next(self, config: dict[str, Any], daily: dict[str, Any] | None) -> datetime:
        """Describe the earliest daily/retry target (wakeup may lag 15 minutes)."""
        slot = self._slot(config)
        following = slot + timedelta(days=1)
        if not self._eligible(config, slot):
            return following
        if self._same_target(daily, config, slot):
            if daily.get("outcome") == "success" or daily["count"] >= MAX_ATTEMPTS:
                return following
            return min(
                following,
                datetime.fromisoformat(daily["attempted_at"])
                + timedelta(seconds=RETRY_SECONDS),
            )
        return slot

    def deferred(self) -> None:
        """Record contention independently without touching the active attempt."""
        descriptor, name = tempfile.mkstemp(
            prefix=".backup-defer-", dir=self.runtime.state_dir
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w") as output:
                json.dump(
                    {
                        "outcome": "deferred",
                        "at": self.now.isoformat(),
                        "action": "Another project command holds the lock. Next scheduler wakeup retries; inspect stale protection if contention persists.",
                    },
                    output,
                )
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(self.runtime.state_dir / DEFER)
        finally:
            temporary.unlink(missing_ok=True)

    def _selectors_match(self, config: dict[str, Any]) -> bool:
        """Require manual due invocations to use the selected runtime and destination."""
        expected = config["environment"]
        backup = os.environ.get("RSS_BACKUP_DIR", "")
        destination = Path(backup).expanduser()
        return (
            os.environ.get("RSS_TAILSCALE", "0") == expected["RSS_TAILSCALE"]
            and str(self.runtime._port()) == expected["RSS_PORT"]
            and destination.is_absolute()
            and str(destination.resolve()) == expected["RSS_BACKUP_DIR"]
        )

    def due(self) -> dict[str, Any]:
        """Try at most three times per daily target, with no overlapping backlog."""
        from rss_aggregator.runtime import RuntimeFailure

        config = self._config()
        if not config or not config["active"]:
            return {"outcome": "disabled"}
        if not self._job_matches(config) or not self._job.loaded(config):
            raise RuntimeFailure(
                "Backup job missing, changed or not registered; inspect backup-status and the owned schedule locally."
            )
        if not config.get("registration_id"):
            raise RuntimeFailure(
                "Backup registration lacks activation evidence; run rss backup-enable with the captured settings."
            )
        try:
            if not self._selectors_match(config):
                raise ValueError(
                    "Backup runtime settings differ from captured configuration."
                )
            self._job.check(config)
        except (RuntimeFailure, OSError, ValueError, KeyError, TypeError):
            self.runtime._save(
                ATTEMPT,
                {
                    "started_at": self.now.isoformat(),
                    "kind": "backup",
                    "outcome": "failed",
                    "resumption": "preserved",
                    "action": "Restore captured settings, launcher, native manifest and dependencies, or explicitly disable/re-enable the owned schedule.",
                },
            )
            raise RuntimeFailure(
                "Backup job settings or captured dependencies changed; nothing started. Inspect rss backup-status."
            ) from None
        slot = self._slot(config)
        daily = self._read(DAILY)
        same_target = self._same_target(daily, config, slot)
        next_attempt = self._next(config, daily)
        if self.now < next_attempt:
            return {
                "outcome": "not_due",
                "next_expected_attempt": next_attempt.isoformat(),
            }
        daily = {
            "slot": slot.isoformat(),
            "count": daily["count"] + 1 if same_target else 1,
            "attempted_at": self.now.isoformat(),
            "outcome": "running",
            "instance": self.runtime.instance,
            "policy_id": config["policy_id"],
            "registration_id": config["registration_id"],
        }
        self.runtime._save(DAILY, daily)
        try:
            with self._job.environment(config):
                result = self.backup()
        except Exception:
            self.runtime._save(DAILY, {**daily, "outcome": "failed"})
            raise RuntimeFailure(
                "Daily backup failed. Inspect rss backup-status, destination/dependencies and maintenance locally; no private diagnostics are emitted by this command."
            ) from None
        self.runtime._save(
            DAILY,
            {
                **daily,
                "outcome": "success",
                "archive": result["archive"],
                "sha256": result["sha256"],
            },
        )
        return {"outcome": "success", "backup": result}

    def pending(self) -> bool:
        """Treat any durable maintenance presence, including malformed data, as a hold."""
        return os.path.lexists(self.runtime.state_dir / MAINTENANCE)

    def begin(self, owner: dict[str, Any], operation: str) -> None:
        """Persist maintenance before any forwarding, data or writer mutation."""
        from rss_aggregator.runtime import RuntimeFailure

        if self.pending():
            raise RuntimeFailure("Unresolved maintenance; run rss backup-reconcile.")
        self.runtime._save(
            MAINTENANCE,
            {
                "version": 1,
                "owner": owner["token"],
                "operation": operation,
                "phase": "preparing",
                "started_at": self.now.isoformat(),
            },
        )

    def phase(self, phase: str) -> None:
        """Durably advance the maintenance diagnostic without granting restart authority."""
        self.runtime._save(MAINTENANCE, {**self._read(MAINTENANCE), "phase": phase})

    def finish(self) -> None:
        """Retire maintenance only after safe completion or explicit reconciliation."""
        (self.runtime.state_dir / MAINTENANCE).unlink()
        descriptor = os.open(self.runtime.state_dir, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def may_resume(self) -> bool:
        """Honor stopped intent; absence preserves observed state without enabling intent."""
        return Supervision(self.runtime).intent() != "stopped"

    def _maintenance_owner(self) -> dict[str, Any]:
        """Prove the selected runtime owns this recognized maintenance record."""
        from rss_aggregator.runtime import RuntimeFailure

        marker = self._read(MAINTENANCE)
        owner, _installed = self.runtime._owned(require_current=False)
        if (
            not isinstance(marker, dict)
            or marker.get("version") != 1
            or marker.get("owner") != owner["token"]
            or marker.get("operation") not in ("backup", "stop", "restore")
        ):
            raise RuntimeFailure(
                "Unrecognized maintenance owner/schema; preserve records and inspect locally."
            )
        return owner

    def reconcile(self) -> dict[str, Any]:
        """Release the hold only after read-only proof of idle owned writers/forwarding."""
        from rss_aggregator.runtime import RuntimeFailure

        if not self.pending():
            return {
                "reconciled": False,
                "resumed": False,
                "action": "No maintenance hold exists.",
            }
        owner = self._maintenance_owner()
        if not self.runtime._idle() or PrivateAccess(self.runtime).status(owner)[
            "status"
        ] not in ("unconfigured", "stopped"):
            raise RuntimeFailure(
                "Interrupted maintenance still has active writers or forwarding. Run rss stop to quiesce owned resources, then retry rss backup-reconcile."
            )
        attempt = self._read(ATTEMPT)
        if attempt and attempt["outcome"] == "running":
            self.runtime._save(
                ATTEMPT,
                {**attempt, "outcome": "interrupted", "resumption": "not_resumed"},
            )
        daily = self._read(DAILY)
        if daily and daily["outcome"] == "running":
            self.runtime._save(DAILY, {**daily, "outcome": "interrupted"})
        self.finish()
        return {
            "reconciled": True,
            "resumed": False,
            "action": "Writers and owned forwarding confirmed idle; hold released without changing intent. A separately enabled supervisor may honor existing running intent. Inspect backup-status before explicit startup.",
        }

    def status(self, *, busy: bool = False) -> dict[str, Any]:
        """Report evidence and freshness; configuration alone never proves protection."""
        from rss_aggregator.runtime import RuntimeFailure

        schedule = self._config()
        attempt = self._read(ATTEMPT)
        success = self._read(SUCCESS)
        age = (
            max(
                0,
                (
                    self.now - datetime.fromisoformat(success["completed_at"])
                ).total_seconds(),
            )
            if success
            else None
        )
        overdue = age is None or age > 26 * 3600
        archive_available = bool(success and Path(success["archive"]).is_file())
        maintenance = self.pending()
        if attempt and attempt["outcome"] == "running" and not busy:
            attempt = {
                **attempt,
                "outcome": "interrupted",
                "action": "Run rss backup-reconcile before retrying; no automatic resumption.",
            }
        daily = self._read(DAILY)
        slot = self._slot(schedule) if schedule else None
        same_target = bool(
            schedule and slot and self._same_target(daily, schedule, slot)
        )
        daily_due = bool(
            slot
            and self._eligible(schedule, slot)
            and not (same_target and daily.get("outcome") == "success")
        )
        missed = bool(
            daily_due
            and self.now > slot + timedelta(seconds=RETRY_SECONDS)
            and not same_target
        )
        daily_archive = (
            Path(daily["archive"])
            if schedule
            and self._same_registration(daily, schedule)
            and daily.get("outcome") == "success"
            and daily.get("archive")
            and daily.get("sha256")
            else None
        )
        daily_archive_available = bool(
            daily_archive and not daily_archive.is_symlink() and daily_archive.is_file()
        )
        daily_fresh = bool(
            daily_archive_available
            and 0
            <= (
                self.now - datetime.fromisoformat(daily["attempted_at"])
            ).total_seconds()
            <= 26 * 3600
        )
        registered = False
        problem = None
        if schedule:
            try:
                registered = self._job_matches(schedule) and self._job.loaded(schedule)
                self._job.check(schedule)
                if not registered:
                    problem = "Backup job missing, changed or not registered; inspect/re-enable the owned schedule explicitly."
                elif not schedule.get("registration_id"):
                    problem = "Backup registration lacks activation evidence; run rss backup-enable with the captured settings."
            except (RuntimeFailure, OSError, ValueError, KeyError, TypeError):
                problem = "Backup scheduler unavailable/conflicting; inspect job, launcher, native manifest and captured dependencies locally."
        return {
            "enabled": bool(schedule and schedule["active"]),
            "registered": registered,
            "registered_at": schedule.get("registered_at") if schedule else None,
            "registration_id": schedule.get("registration_id") if schedule else None,
            "busy": busy,
            "scheduler_problem": problem,
            "next_expected_attempt": self._next(schedule, daily).isoformat()
            if schedule and schedule.get("registration_id")
            else None,
            "daily_attempt": daily,
            "daily_archive_available": daily_archive_available,
            "daily_due": daily_due,
            "missed": missed,
            "latest_defer": self._read(DEFER),
            "latest_attempt": attempt,
            "latest_success": success,
            "resumption": attempt.get(
                "resumption", "unresolved" if maintenance else "not_attempted"
            )
            if attempt
            else "not_attempted",
            "age_seconds": age,
            "overdue": overdue,
            "archive_available": archive_available,
            "maintenance_required": maintenance,
            "protected": bool(
                schedule
                and schedule["active"]
                and registered
                and not problem
                and daily_fresh
                and not daily_due
                and not maintenance
                and attempt
                and attempt["outcome"] == "success"
                and attempt.get("instance") == self.runtime.instance
            ),
        }
