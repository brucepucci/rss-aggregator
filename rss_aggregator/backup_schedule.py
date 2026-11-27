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
        if archive.get("verified"):
            completed = (
                self.now if self.controlled_clock else datetime.now(timezone.utc)
            )
            self.runtime._save(
                SUCCESS,
                {
                    **archive,
                    "completed_at": completed.isoformat(),
                    "instance": attempt["instance"],
                },
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
        """Require a native schedule owned by the selected checkout and state."""
        return self._job.read(SCHEDULE)

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
        policy = f"{hour}:{minute}:{captured['owner']}:{captured['manifest_sha256']}:{captured['python_sha256']}"
        config = {
            **captured,
            "hour": hour,
            "minute": minute,
            "timezone": "UTC",
            "destination": str(directory),
            "policy_id": hashlib.sha256(encoded + policy.encode()).hexdigest(),
            "active": False,
        }
        if existing and {**existing, "active": False} != config:
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
        self.runtime._save(SCHEDULE, config)
        if not self.job.exists():
            self._job.write(encoded)
        if (
            not loaded
            and self._job.launch(
                config, "bootstrap", self._job.domain, str(self.job)
            ).returncode
        ):
            raise RuntimeFailure(
                "Backup LaunchAgent activation failed; inspect the normal user's GUI session and retry enable. Configuration is not protection."
            )
        config["active"] = True
        self.runtime._save(SCHEDULE, config)
        return {
            "enabled": True,
            "job": str(self.job),
            "daily_utc": f"{hour:02}:{minute:02}",
            "next_expected_attempt": self._next(config).isoformat(),
        }

    def disable(self) -> dict[str, Any]:
        """Remove only the exact owned job, retaining evidence and all archives."""
        from rss_aggregator.runtime import RuntimeFailure

        config = self._config()
        if config is None:
            if os.path.lexists(self.job):
                raise RuntimeFailure("Backup job is unowned; nothing removed.")
            return {"enabled": False}
        if not self._job_matches(config):
            raise RuntimeFailure(
                "Backup job changed; nothing removed. Inspect locally."
            )
        if (
            self._job.loaded(config)
            and self._job.launch(
                config, "bootout", self._job.domain + "/" + self._job.label
            ).returncode
        ):
            raise RuntimeFailure(
                "Backup job removal failed; configuration retained for inspection."
            )
        self.job.unlink()
        (self.runtime.state_dir / SCHEDULE).unlink()
        return {"enabled": False, "archives_preserved": True}

    def _slot(self, config: dict[str, Any]) -> datetime:
        """Identify today's UTC daily target; no pre-target catch-up on first enable."""
        return self.now.replace(
            hour=config["hour"], minute=config["minute"], second=0, microsecond=0
        )

    def _same_target(
        self, daily: dict[str, Any] | None, config: dict[str, Any]
    ) -> bool:
        """Bind daily eligibility to the exact captured policy and current instance."""
        return bool(
            daily
            and daily.get("slot") == self._slot(config).isoformat()
            and daily.get("policy_id") == config["policy_id"]
            and daily.get("instance") == self.runtime.instance
        )

    def _next(self, config: dict[str, Any]) -> datetime:
        """Describe the earliest daily/retry target (wakeup may lag 15 minutes)."""
        slot = self._slot(config)
        daily = self._read(DAILY)
        if self.now < slot:
            return slot
        if self._same_target(daily, config):
            if daily.get("outcome") == "success" or daily["count"] >= MAX_ATTEMPTS:
                return slot + timedelta(days=1)
            return datetime.fromisoformat(daily["attempted_at"]) + timedelta(
                seconds=RETRY_SECONDS
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
        same_target = self._same_target(daily, config)
        if self.now < slot or (
            same_target
            and (
                daily.get("outcome") == "success"
                or daily["count"] >= MAX_ATTEMPTS
                or self.now
                < datetime.fromisoformat(daily["attempted_at"])
                + timedelta(seconds=RETRY_SECONDS)
            )
        ):
            return {
                "outcome": "not_due",
                "next_expected_attempt": self._next(config).isoformat(),
            }
        daily = {
            "slot": slot.isoformat(),
            "count": daily["count"] + 1 if same_target else 1,
            "attempted_at": self.now.isoformat(),
            "outcome": "running",
            "instance": self.runtime.instance,
            "policy_id": config["policy_id"],
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
        self.runtime._save(DAILY, {**daily, "outcome": "success"})
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
        same_target = bool(schedule and self._same_target(daily, schedule))
        daily_due = bool(
            slot
            and self.now >= slot
            and not (same_target and daily.get("outcome") == "success")
        )
        missed = bool(
            daily_due
            and self.now > slot + timedelta(seconds=RETRY_SECONDS)
            and not same_target
        )
        daily_fresh = bool(
            daily
            and schedule
            and daily.get("policy_id") == schedule["policy_id"]
            and daily.get("instance") == self.runtime.instance
            and daily.get("outcome") == "success"
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
            except (RuntimeFailure, OSError, ValueError, KeyError, TypeError):
                problem = "Backup scheduler unavailable/conflicting; inspect job, launcher, native manifest and captured dependencies locally."
        return {
            "enabled": bool(schedule and schedule["active"]),
            "registered": registered,
            "busy": busy,
            "scheduler_problem": problem,
            "next_expected_attempt": self._next(schedule).isoformat()
            if schedule
            else None,
            "daily_attempt": daily,
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
                and success
                and success.get("instance") == self.runtime.instance
                and archive_available
                and not daily_due
                and not overdue
                and not maintenance
                and attempt
                and attempt["outcome"] == "success"
                and attempt.get("instance") == self.runtime.instance
            ),
        }
