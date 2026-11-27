"""Opt-in daily backup orchestration and secret-safe protection evidence."""

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from rss_aggregator import native_service
from rss_aggregator.private_access import PrivateAccess
from rss_aggregator.supervision import MAINTENANCE, NativeJob, Supervision

if TYPE_CHECKING:
    from rss_aggregator.runtime import Runtime

ATTEMPT = "backup-attempt.json"
SHUTDOWN = "backup-shutdown.json"
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
        return native_service.read(self.runtime.state_dir / name)

    def backup(self) -> dict[str, Any]:
        """Record an explicit backup's outcome separately from verified publication."""
        from rss_aggregator.recovery import Recovery

        self.runtime._ownership_receipt()
        return self._attempt(Recovery(self.runtime).backup, "backup")

    @staticmethod
    def _dated(record: dict[str, Any], key: str) -> bool:
        """Require an aware date that can be compared to the orchestration clock."""
        return datetime.fromisoformat(record[key]).utcoffset() is not None

    @staticmethod
    def _archive_fields(record: dict[str, Any]) -> bool:
        """Recognize a published archive pointer and its whole-archive digest."""
        path = record["archive"]
        return (
            isinstance(path, str)
            and "\0" not in path
            and Path(path).is_absolute()
            and re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is not None
        )

    @staticmethod
    def _valid_generation(value: Any) -> bool:
        """Accept a recorded writer generation or a known absence of writers."""
        return value is None or (
            isinstance(value, str)
            and re.fullmatch(r"[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}", value)
            is not None
        )

    def _valid_shutdown(self, record: dict[str, Any]) -> bool:
        """Validate one unresolved shutdown, without inventing a missing identity."""
        from rss_aggregator.recovery import instance_name

        instance = record["instance"]
        if instance is not None:
            instance_name(instance)
        return (
            record.get("outcome") in ("running", "failed", "interrupted")
            and self._dated(record, "started_at")
            and (
                "supersedes_hold" not in record
                or re.fullmatch(r"[0-9a-f]{64}", record["supersedes_hold"]) is not None
            )
            and (
                "writer_generation" not in record
                or (
                    instance is not None
                    and self._valid_generation(record["writer_generation"])
                )
            )
        )

    @staticmethod
    def _daily_ledger(record: dict[str, Any]) -> dict[str, Any]:
        """Promote a legacy flat attempt in memory, without rewriting it on read."""
        if "version" in record:
            return record
        instance = record["instance"]
        return {
            "version": 2,
            "registration_id": record["registration_id"],
            "policy_id": record["policy_id"],
            "latest_instance": instance,
            "targets": {
                instance: {
                    key: value
                    for key, value in record.items()
                    if key not in ("instance", "registration_id", "policy_id")
                }
            },
        }

    def _valid_daily(
        self, record: dict[str, Any], schedule: dict[str, Any] | None
    ) -> bool:
        """Require complete, consistent evidence for all targets before replacement."""
        from rss_aggregator.recovery import instance_name

        if (
            set(record)
            != {"version", "registration_id", "policy_id", "latest_instance", "targets"}
            or type(record["version"]) is not int
            or record["version"] != 2
            or record["registration_id"] is None
            or not self._valid_generation(record["registration_id"])
            or not re.fullmatch(r"[0-9a-f]{64}", record["policy_id"])
        ):
            return False
        targets = record["targets"]
        if (
            not isinstance(targets, dict)
            or instance_name(record["latest_instance"]) not in targets
        ):
            return False
        current = schedule is not None and self._same_registration(record, schedule)
        if current and record["policy_id"] != schedule["policy_id"]:
            return False  # One registration cannot identify two captured policies.
        registered_at = (
            datetime.fromisoformat(schedule["registered_at"]) if current else None
        )
        for instance, attempt in targets.items():
            instance_name(instance)
            if not self._valid_daily_attempt(attempt):
                return False
            slot = datetime.fromisoformat(attempt["slot"])
            if current and (
                slot < registered_at
                or (slot.hour, slot.minute) != (schedule["hour"], schedule["minute"])
            ):
                return False
        return True

    def _valid_daily_attempt(self, attempt: Any) -> bool:
        """Require a real UTC slot, charged attempt and honest publication fields."""
        fields = {"slot", "count", "attempted_at", "outcome"}
        if not isinstance(attempt, dict):
            return False
        outcome = attempt["outcome"]
        if outcome == "success":
            fields.update(("archive", "sha256"))
        if (
            set(attempt) != fields
            or outcome not in ("running", "success", "failed", "interrupted")
            or type(attempt["count"]) is not int
            or not 1 <= attempt["count"] <= MAX_ATTEMPTS
            or (outcome == "success" and not self._archive_fields(attempt))
        ):
            return False
        slot = datetime.fromisoformat(attempt["slot"])
        attempted = datetime.fromisoformat(attempt["attempted_at"])
        return (
            slot.utcoffset() == attempted.utcoffset() == timedelta(0)
            and slot.second == slot.microsecond == 0
            and timedelta(0) <= attempted - slot < timedelta(days=1)
        )

    def _valid_evidence(
        self,
        name: str,
        record: dict[str, Any],
        *,
        schedule: dict[str, Any] | None = None,
    ) -> bool:
        """Validate fields used in each evidence view before using them."""
        outcome = record.get("outcome")
        if name == ATTEMPT:
            return (
                record.get("kind") in ("backup", "stop")
                and outcome
                in ("running", "success", "failed", "interrupted", "no_data")
                and (outcome != "success" or isinstance(record.get("instance"), str))
                and self._dated(record, "started_at")
            )
        if name == SUCCESS:
            return (
                record.get("verified") is True
                and record.get("via") in ("backup", "stop")
                and isinstance(record.get("instance"), str)
                and isinstance(record.get("manifest"), dict)
                and self._archive_fields(record)
                and self._dated(record, "completed_at")
            )
        if name == SHUTDOWN:
            owner = record.get("owner")
            version = record.get("version")
            if version != 3 and (not isinstance(owner, str) or not owner):
                return False
            if version == 1:
                return self._valid_shutdown(
                    {
                        "instance": None,
                        "outcome": outcome,
                        "started_at": record["started_at"],
                    }
                )
            pending = record.get("pending")
            return (
                version in (2, 3)
                and isinstance(pending, list)
                and bool(pending)
                and all(
                    isinstance(item, dict) and self._valid_shutdown(item)
                    for item in pending
                )
                and len({item["instance"] for item in pending}) == len(pending)
                and (
                    version == 2
                    or (
                        owner is None
                        and len(pending) == 1
                        and pending[0]["instance"] is None
                    )
                )
            )
        if name == DAILY:
            return self._valid_daily(record, schedule)
        return outcome == "deferred" and self._dated(record, "at")

    def _evidence(
        self, name: str, *, schedule: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """Distinguish absent backup evidence from unreadable or invalid records."""
        try:
            record = self._read(name)
            if record is None and not os.path.lexists(self.runtime.state_dir / name):
                return None
            if isinstance(record, dict):
                if name == DAILY:
                    record = self._daily_ledger(record)
                if self._valid_evidence(name, record, schedule=schedule):
                    return record
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            pass
        raise ValueError(
            f"Invalid or unreadable {name}; preserve it and inspect locally."
        )

    def _reported_evidence(
        self,
        name: str,
        problems: dict[str, str],
        *,
        schedule: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Keep one damaged evidence record from hiding other readable records."""
        try:
            return self._evidence(name, schedule=schedule)
        except ValueError as error:
            problems[name] = str(error)
            return None

    @staticmethod
    def _shutdown_records(ledger: dict[str, Any] | None) -> list[dict[str, Any]]:
        """Read a legacy unresolved stop as unidentified, never as the current selection."""
        if ledger is None:
            return []
        if ledger["version"] == 1:
            return [
                {
                    "instance": None,
                    "started_at": ledger["started_at"],
                    "outcome": ledger["outcome"],
                }
            ]
        return ledger["pending"]

    def _owned_shutdowns(self, owner: dict[str, Any]) -> list[dict[str, Any]]:
        """Read unresolved shutdowns without trusting another owner's evidence."""
        from rss_aggregator.runtime import RuntimeFailure

        ledger = self._evidence(SHUTDOWN)
        if ledger is not None and ledger.get("owner") not in (None, owner["token"]):
            raise RuntimeFailure(
                "Unrecognized shutdown evidence owner; inspect locally."
            )
        return self._shutdown_records(ledger)

    def require_upgrade_shutdown(
        self, owner: dict[str, Any], instance: str, generation: str | None
    ) -> dict[str, Any]:
        """Require consistent shutdown evidence for the selected instance only."""
        from rss_aggregator.recovery import SHUTDOWN_SUCCESS
        from rss_aggregator.runtime import RuntimeFailure

        if any(
            entry["instance"] in (None, instance)
            for entry in self._owned_shutdowns(owner)
        ):
            raise RuntimeFailure(
                "A verified shutdown archive is required; shutdown evidence is unresolved for the selected instance or an unidentified instance. "
                "Inspect rss backup-status and retry rss stop if the instance is identified."
            )
        latest = self._evidence(SUCCESS)
        try:
            success = self._read(SHUTDOWN_SUCCESS)
            if (
                isinstance(success, dict)
                and success.get("via") == "stop"
                and success.get("instance") == instance
                and "writer_generation" in success
                and success["writer_generation"] == generation
                and self._valid_evidence(SUCCESS, success)
                and (
                    latest is None
                    or latest["instance"] != instance
                    or (
                        "writer_generation" in latest
                        and latest["writer_generation"] == generation
                    )
                )
            ):
                return success
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            pass
        raise RuntimeFailure(
            "A verified shutdown archive covering the selected instance's last writer generation is required; fix the backup and retry rss stop."
        )

    def _save_shutdowns(
        self, owner: dict[str, Any], pending: list[dict[str, Any]]
    ) -> None:
        """Persist every affected instance together, without changing unrelated entries."""
        if pending:
            self.runtime._save(
                SHUTDOWN, {"version": 2, "owner": owner["token"], "pending": pending}
            )
        else:
            self._unlink_durable(self.runtime.state_dir / SHUTDOWN)

    def _unidentified_shutdown(self) -> dict[str, Any]:
        """Prepare an unresolved stop without inferring its affected instance."""
        return {
            "instance": None,
            "started_at": self.now.isoformat(),
            "outcome": "running",
        }

    def _identify_shutdown(
        self,
        owner: dict[str, Any],
        installed: dict[str, Any],
        *,
        receipt_lost: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        """Identify the writer independently of selection when its evidence matches."""
        from rss_aggregator.recovery import Recovery, instance_name
        from rss_aggregator.runtime import RuntimeFailure

        record = self._unidentified_shutdown()
        already_stopped = False
        try:
            already_stopped = self.runtime._idle()
            recovery = Recovery(self.runtime)
            if already_stopped:
                instance = self.runtime.instance
                if not receipt_lost:
                    record["instance"] = instance
                generation = recovery.shutdown_generation(
                    instance, owner, installed, require_record=receipt_lost
                )
                record.update(instance=instance, writer_generation=generation)
            else:
                live = self.runtime._live()
                plan = live[0] if live else self.runtime._draining_writer_plan()
                if plan is None:
                    return record, already_stopped
                instance = instance_name(plan["instance"])
                if "writer_generation" in plan and plan.get("owner") == owner["token"]:
                    generation = recovery.shutdown_generation(
                        instance, owner, installed, require_record=True
                    )
                    if (
                        generation is not None
                        and generation == plan["writer_generation"]
                    ):
                        record.update(instance=instance, writer_generation=generation)
                elif live and not receipt_lost and "writer_generation" not in plan:
                    # Old plans had no durable generation binding. Keep their
                    # active controller's instance, but require the same selection
                    # before treating its generation as covered by a shutdown.
                    record["instance"] = instance
                    if instance == self.runtime.instance:
                        record["writer_generation"] = recovery.shutdown_generation(
                            instance, owner, installed
                        )
        except (
            RuntimeFailure,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
        ):
            # Identification is evidence only; it must never gate quiescence.
            pass
        return record, already_stopped

    def _has_intent(self, intent: str) -> bool:
        """Require the same readable operator intent that supervision would honor."""
        try:
            return Supervision(self.runtime).intent() == intent
        except (OSError, ValueError):
            return False

    def _set_stopped_intent(self) -> None:
        """Attempt intent even when an earlier bookkeeping stage failed."""
        try:
            Supervision(self.runtime).set_intent("stopped")
        except OSError:
            self.runtime._diagnostic(
                "Could not persist stopped intent; running intent may still be in force. "
                "Fix the write failure and retry rss stop before reconciling maintenance."
            )
            raise

    def _covered_shutdown(
        self, shutdown: dict[str, Any], running: dict[str, Any], archive: dict[str, Any]
    ) -> bool:
        """Match this capture to the same instance and its trusted current generation."""
        from rss_aggregator.recovery import SHUTDOWN_SUCCESS

        if shutdown["instance"] is None or "writer_generation" not in shutdown:
            return False
        evidence = self._read(SHUTDOWN_SUCCESS)
        generation = shutdown["writer_generation"]
        # Native starts advance in place; a verified capture of the current known
        # generation covers earlier writes. A missing generation cannot do so.
        return (
            isinstance(evidence, dict)
            and evidence.get("via") == "stop"
            and evidence.get("verified") is True
            and evidence.get("archive") == archive["archive"]
            and evidence.get("sha256") == archive["sha256"]
            and evidence.get("instance") == shutdown["instance"]
            and "writer_generation" in evidence
            and evidence["writer_generation"] == generation
            and (generation is not None or running.get("writer_generation") is None)
        )

    def _record_receipt_loss(self) -> None:
        """Preserve failure without accepting candidate evidence as shutdown authority."""
        from rss_aggregator.recovery import Recovery
        from rss_aggregator.runtime import RuntimeFailure

        runtime = self.runtime
        recovery = Recovery(runtime)
        prior = self._evidence(SHUTDOWN)
        identity = None
        try:
            plan = runtime._draining_writer_plan()
            # Selection may establish which installation owns recorded data, but
            # cannot identify an active writer if the generation evidence is absent.
            candidate = plan["instance"] if plan is not None else runtime.instance
            identity = recovery.receipt_loss_identity(candidate)
        except (RuntimeFailure, OSError, ValueError, KeyError, TypeError):
            pass
        if prior is None and identity is None:
            try:
                native_service.WriterLeases(runtime.state_dir)
            except (OSError, ValueError):
                # A fresh, unrelated or unrecognizable root supplies neither
                # established lease evidence nor owned instance provenance.
                return
        record = self._unidentified_shutdown()
        owner = prior.get("owner") if prior else None
        if identity is not None and owner in (None, identity[0]["token"]):
            record, _idle = self._identify_shutdown(*identity, receipt_lost=True)
            owner = identity[0]["token"]
        record["outcome"] = "failed"
        try:
            marker = self._read(MAINTENANCE)
            if isinstance(marker, dict):
                record["supersedes_hold"] = self._hold_identity(marker)
        except (OSError, ValueError, TypeError):
            # A bad hold already prevents reconciliation; preserve failure too.
            pass
        pending = [
            entry
            for entry in self._shutdown_records(prior)
            if entry["instance"] != record["instance"]
        ]
        if owner is None:
            # An ownerless entry must stay unidentified even after a receipt returns.
            runtime._save(SHUTDOWN, {"version": 3, "owner": None, "pending": [record]})
        else:
            self._save_shutdowns({"token": owner}, [*pending, record])

    def shutdown(self) -> dict[str, Any]:
        """Keep unresolved stop evidence separate from archive and maintenance state."""
        from rss_aggregator.recovery import Recovery
        from rss_aggregator.runtime import RuntimeFailure

        runtime = self.runtime
        try:
            owner, installed = runtime._ownership_receipt()
        except (RuntimeFailure, OSError, ValueError):
            try:
                self._record_receipt_loss()
            except (RuntimeFailure, OSError, ValueError, KeyError, TypeError):
                runtime._diagnostic(
                    "Could not persist receipt-loss shutdown evidence; inspect locally."
                )
            finally:
                # Revoke existing permission only, without controlling writers or
                # creating intent in a fresh path that has never granted it.
                if self._has_intent("running"):
                    self._set_stopped_intent()
            raise
        recorded = False
        settled = False
        try:
            try:
                try:
                    shutdown, already_stopped = self._identify_shutdown(
                        owner, installed
                    )
                    try:
                        if self.pending():
                            _, marker = self._owned_maintenance()
                            shutdown["supersedes_hold"] = self._hold_identity(marker)
                        self.begin(owner, "stop", shutdown=shutdown)
                    finally:
                        previous = self._owned_shutdowns(owner)
                        identity = shutdown["instance"]
                        others = [
                            entry for entry in previous if entry["instance"] != identity
                        ]
                        prior = next(
                            (
                                entry
                                for entry in previous
                                if entry["instance"] == identity
                            ),
                            None,
                        )
                        running = shutdown.copy()
                        if (
                            prior
                            and prior.get("writer_generation")
                            and not running.get("writer_generation")
                        ):
                            running["writer_generation"] = prior["writer_generation"]
                        self._save_shutdowns(owner, [*others, running])
                        recorded = True
                finally:
                    self._set_stopped_intent()
                self.phase("quiescing")
            finally:
                # A bookkeeping failure must not prevent closing owned resources.
                runtime._quiesce(owner)
            try:
                self.phase("snapshot")
                instance = (
                    shutdown["instance"] if "writer_generation" in shutdown else None
                )
                result = self._attempt(
                    lambda: Recovery(runtime).capture_shutdown(
                        already_stopped=already_stopped, instance=instance
                    ),
                    "stop",
                    instance=instance,
                )
                archive = result["backup"]
                if archive.get("verified") and not self._covered_shutdown(
                    shutdown, running, archive
                ):
                    raise RuntimeFailure(
                        "Verified archive published, but its shutdown identity could not be confirmed. "
                        "Inspect the unresolved record locally."
                    )
                settled = True
            except (RuntimeFailure, OSError, ValueError, KeyError, TypeError) as error:
                if not runtime._idle():
                    raise
                settled = True
                raise RuntimeFailure(
                    "Shutdown backup failed; application writers remain stopped. "
                    "Fix the backup problem and retry rss stop. " + str(error)
                ) from error
        except Exception:
            if recorded:
                try:
                    self._save_shutdowns(
                        owner, [*others, {**running, "outcome": "failed"}]
                    )
                except OSError:
                    # The earlier durable record still blocks protection.
                    runtime._diagnostic(
                        "Could not update unresolved shutdown evidence."
                    )
            raise
        finally:
            try:
                if settled:
                    if not runtime._idle():
                        raise RuntimeFailure(
                            "Cannot confirm writers are stopped; reconcile maintenance."
                        )
                    self.finish()
            finally:
                if self.pending():
                    self.notice()
        # Release the hold before clearing evidence: interruption at any prior
        # stage stays unprotected, even if archive publication already succeeded.
        self._save_shutdowns(owner, others if archive.get("verified") else previous)
        return result

    def _attempt(
        self,
        operation: Callable[[], dict[str, Any]],
        kind: str,
        *,
        instance: str | None = None,
    ) -> dict[str, Any]:
        """Keep capture and resumption evidence even if selection is unreadable."""
        self._evidence(ATTEMPT)
        attempt = {
            "started_at": self.now.isoformat(),
            "outcome": "running",
            "kind": kind,
            "instance": None,
        }
        try:
            attempt["instance"] = (
                instance if instance is not None else self.runtime.instance
            )
            self.runtime._save(ATTEMPT, attempt)
            result = operation()
        except Exception:
            resumption = "preserved"
            if kind == "backup" and self.pending():
                resumption = "unresolved"
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
                or config.get("registration_id") is None
                or not self._valid_generation(config["registration_id"])
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

        job = self.job
        had_definition = os.path.lexists(job)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("Daily UTC hour/minute must be 0–23 and 0–59.")
        directory = Recovery(self.runtime)._directory()
        owner, _installed = self.runtime._owned()
        selected = self.runtime.instance
        info = self.runtime.state_dir.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise RuntimeFailure(
                "Scheduling requires a private owned RSS_STATE_DIR (0700); fix permissions explicitly."
            )
        existing = self._config()
        daily = self._evidence(DAILY, schedule=existing)
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
        if had_definition and (not existing or not self._job_matches(existing)):
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
        if loaded and not had_definition:
            raise RuntimeFailure(
                "Loaded backup job definition is missing; preserve the registration and inspect locally."
            )
        if not loaded:
            self.runtime._save(SCHEDULE, {**config, "active": False})
            if not had_definition:
                self._job.write(encoded)
        if not self._job.matches(encoded):
            raise RuntimeFailure(
                "Backup job definition is missing or changed; registration retained. Inspect locally."
            )
        if (
            not loaded
            and self._job.launch(
                config, "bootstrap", self._job.domain, str(job)
            ).returncode
        ):
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
            "next_expected_attempt": self._next(
                config, self._target_attempt(daily, config, selected)
            ).isoformat(),
        }

    def disable(self) -> dict[str, Any]:
        """Remove only the exact owned job, retaining evidence and all archives."""
        from rss_aggregator.runtime import RuntimeFailure

        config = self._config()
        if config is None:
            if os.path.lexists(self.job):
                raise RuntimeFailure("Backup job is unowned; nothing removed.")
            return {"enabled": False}
        # A structurally damaged daily ledger remains detectable without the
        # schedule. Do not discard the only evidence of a cross-record conflict.
        daily = self._reported_evidence(DAILY, {})
        if daily and not self._valid_daily(daily, config):
            raise RuntimeFailure(
                "Daily evidence conflicts with this backup schedule; preserve both records and inspect locally before disabling."
            )
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

    @staticmethod
    def _same_registration(
        daily: dict[str, Any] | None, config: dict[str, Any]
    ) -> bool:
        """Match the activation ID independently of policy consistency validation."""
        return bool(
            daily
            and config["active"]
            and daily["registration_id"] == config.get("registration_id")
        )

    @staticmethod
    def _daily_report(daily: dict[str, Any], instance: str) -> dict[str, Any]:
        """Render one attempt with the same flat fields as the legacy record."""
        return {
            **daily["targets"][instance],
            "instance": instance,
            "policy_id": daily["policy_id"],
            "registration_id": daily["registration_id"],
        }

    def _target_attempt(
        self, daily: dict[str, Any] | None, config: dict[str, Any], selected: str | None
    ) -> dict[str, Any] | None:
        """Use only progress from the active registration for the selected instance."""
        if (
            daily
            and self._same_registration(daily, config)
            and selected in daily["targets"]
        ):
            return self._daily_report(daily, selected)
        return None

    def _next(self, config: dict[str, Any], attempt: dict[str, Any] | None) -> datetime:
        """Predict the next slot or retry without reopening any older elapsed slot."""
        slot = self._slot(config)
        if attempt:
            recorded = datetime.fromisoformat(attempt["slot"])
            if slot <= recorded:
                following = recorded + timedelta(days=1)
                if attempt["outcome"] == "success" or attempt["count"] >= MAX_ATTEMPTS:
                    return following
                return min(
                    following,
                    datetime.fromisoformat(attempt["attempted_at"])
                    + timedelta(seconds=RETRY_SECONDS),
                )
        if not self._eligible(config, slot):
            activation = datetime.fromisoformat(config["registered_at"])
            first = activation.replace(
                hour=config["hour"], minute=config["minute"], second=0, microsecond=0
            )
            return first if first >= activation else first + timedelta(days=1)
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
        daily = self._evidence(DAILY, schedule=config)
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
            self.runtime._owned(require_current=False, verify=False)
            selected = self.runtime.instance
        except (RuntimeFailure, OSError, ValueError, KeyError, TypeError):
            self._evidence(ATTEMPT)
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
        previous = self._target_attempt(daily, config, selected)
        next_attempt = self._next(config, previous)
        if self.now < next_attempt:
            return {
                "outcome": "not_due",
                "next_expected_attempt": next_attempt.isoformat(),
            }
        same_slot = previous and datetime.fromisoformat(previous["slot"]) == slot
        attempt = {
            "slot": slot.isoformat(),
            "count": previous["count"] + 1 if same_slot else 1,
            "attempted_at": self.now.isoformat(),
            "outcome": "running",
        }
        # A new activation supersedes only validated evidence, and only when
        # it starts work. Retain one progress entry per target in this registration.
        daily = {
            "version": 2,
            "policy_id": config["policy_id"],
            "registration_id": config["registration_id"],
            "latest_instance": selected,
            "targets": {
                **(daily["targets"] if self._same_registration(daily, config) else {}),
                selected: attempt,
            },
        }
        self.runtime._save(DAILY, daily)
        try:
            with self._job.environment(config):
                result = self.backup()
        except Exception:
            attempt["outcome"] = "failed"
            self.runtime._save(DAILY, daily)
            raise RuntimeFailure(
                "Daily backup failed. Inspect rss backup-status, destination/dependencies and maintenance locally; no private diagnostics are emitted by this command."
            ) from None
        attempt.update(
            outcome="success", archive=result["archive"], sha256=result["sha256"]
        )
        self.runtime._save(DAILY, daily)
        return {"outcome": "success", "backup": result}

    def _interrupt_daily(self) -> None:
        """Finalize a recorded daily interruption, leaving damaged evidence untouched."""
        from rss_aggregator.runtime import RuntimeFailure

        try:
            daily = self._evidence(DAILY, schedule=self._config())
        except (RuntimeFailure, OSError, ValueError, KeyError, TypeError):
            # Bad scheduling evidence does not prevent safe hold reconciliation.
            return
        if daily:
            latest = daily["targets"][daily["latest_instance"]]
            if latest["outcome"] == "running":
                latest["outcome"] = "interrupted"
                self.runtime._save(DAILY, daily)

    def pending(self) -> bool:
        """Treat any durable maintenance presence, including malformed data, as a hold."""
        return os.path.lexists(self.runtime.state_dir / MAINTENANCE)

    def begin(
        self,
        owner: dict[str, Any],
        operation: str,
        *,
        shutdown: dict[str, Any] | None = None,
    ) -> None:
        """Persist maintenance before mutation, including independent stop identity."""
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
                "hold_id": uuid4().hex,
                **({"shutdown": shutdown} if shutdown is not None else {}),
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

    def notice(self) -> None:
        """Point operators to the supported inspection and reconciliation path."""
        self.runtime._diagnostic(
            "Backup maintenance remains unresolved; run rss backup-status and rss backup-reconcile. Archive success and resumption are separate."
        )

    def may_resume(self) -> bool:
        """Honor stopped intent; absence preserves observed state without enabling intent."""
        return Supervision(self.runtime).intent() != "stopped"

    def _owned_maintenance(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Authenticate a maintenance hold against the receipt, not released files."""
        from rss_aggregator.runtime import RuntimeFailure

        marker = self._read(MAINTENANCE)
        owner, _installed = self.runtime._ownership_receipt()
        if (
            not isinstance(marker, dict)
            or marker.get("version") != 1
            or marker.get("owner") != owner["token"]
            or marker.get("operation") not in ("backup", "stop", "restore")
        ):
            raise RuntimeFailure(
                "Unrecognized maintenance owner/schema; preserve records and inspect locally."
            )
        return owner, marker

    @staticmethod
    def _hold_identity(marker: dict[str, Any]) -> str:
        """Bind a later stop to the retained hold, including pre-ID legacy holds."""
        return hashlib.sha256(json.dumps(marker, sort_keys=True).encode()).hexdigest()

    def reconcile(self) -> dict[str, Any]:
        """Release the hold only after read-only proof of idle owned writers/forwarding."""
        from rss_aggregator.runtime import RuntimeFailure

        if not self.pending():
            return {
                "reconciled": False,
                "resumed": False,
                "action": "No maintenance hold exists.",
            }
        owner, marker = self._owned_maintenance()
        problems: dict[str, str] = {}
        ledger = self._reported_evidence(SHUTDOWN, problems)
        owned_ledger = SHUTDOWN not in problems and (
            ledger is None or ledger.get("owner") in (None, owner["token"])
        )
        pending = self._shutdown_records(ledger) if owned_ledger else []
        hold = self._hold_identity(marker)
        requires_stopped = (
            marker["operation"] == "stop"
            or not owned_ledger
            or any(entry.get("supersedes_hold") == hold for entry in pending)
        )
        if requires_stopped and not self._has_intent("stopped"):
            raise RuntimeFailure(
                "Cannot exclude an explicit stop request without durable stopped intent; retry rss stop before rss backup-reconcile."
            )
        if not self.runtime._idle() or PrivateAccess(self.runtime).status(owner)[
            "status"
        ] not in ("unconfigured", "stopped"):
            raise RuntimeFailure(
                "Interrupted maintenance still has active writers or forwarding. Run rss stop to quiesce owned resources, then retry rss backup-reconcile."
            )
        if owned_ledger:
            interrupted = [
                {**item, "outcome": "interrupted"}
                if item["outcome"] == "running"
                else item
                for item in pending
            ]
            if marker["operation"] == "stop":
                # A crash or failed write after begin() may leave only the hold.
                shutdown = marker.get("shutdown")
                try:
                    if not isinstance(shutdown, dict) or not self._valid_shutdown(
                        shutdown
                    ):
                        raise ValueError
                except (ValueError, KeyError, TypeError, OverflowError):
                    shutdown = {"instance": None, "started_at": self.now.isoformat()}
                    try:
                        if self._dated(marker, "started_at"):
                            shutdown["started_at"] = marker["started_at"]
                    except (ValueError, KeyError, TypeError, OverflowError):
                        pass
                if not any(
                    item["instance"] == shutdown["instance"] for item in interrupted
                ):
                    interrupted.append({**shutdown, "outcome": "interrupted"})
            if interrupted != pending:
                self._save_shutdowns(owner, interrupted)
        attempt = self._reported_evidence(ATTEMPT, problems)
        if attempt and attempt["outcome"] == "running":
            self.runtime._save(
                ATTEMPT,
                {**attempt, "outcome": "interrupted", "resumption": "not_resumed"},
            )
        self._interrupt_daily()
        self.finish()
        action = "Writers and owned forwarding confirmed idle; hold released without changing intent."
        if not requires_stopped:
            action += (
                " A separately enabled supervisor may honor existing running intent."
            )
        return {
            "reconciled": True,
            "resumed": False,
            "action": action
            + " Inspect backup-status and retry rss stop if shutdown evidence is unresolved.",
        }

    @staticmethod
    def _archive_available(
        path: Path | None,
        evidence: str,
        problems: dict[str, str],
        *,
        follow_symlinks: bool,
    ) -> bool:
        """Check that a recorded archive can be opened for reading, not its contents."""
        if path is None:
            return False
        try:
            if not stat.S_ISREG(path.stat(follow_symlinks=follow_symlinks).st_mode):
                return False
            # A raced replacement with a FIFO must not block the probe.
            flags = os.O_RDONLY | os.O_NONBLOCK
            if not follow_symlinks:
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path, flags)
            try:
                return stat.S_ISREG(os.fstat(descriptor).st_mode)
            finally:
                os.close(descriptor)
        except (FileNotFoundError, NotADirectoryError):
            return False
        except OSError:
            problems[evidence] = (
                "Cannot inspect the recorded archive; check the destination and its access locally."
            )
            return False

    def status(self, *, busy: bool = False) -> dict[str, Any]:
        """Report evidence and freshness; configuration alone never proves protection."""
        from rss_aggregator.runtime import RuntimeFailure

        problem = None
        try:
            schedule = self._config()
        except (RuntimeFailure, OSError, ValueError, KeyError, TypeError):
            schedule = None
            problem = (
                "Backup schedule ledger is unavailable or invalid; inspect locally."
            )
        selected = None
        if schedule:
            try:
                selected = self.runtime.instance
            except RuntimeFailure:
                problem = "Selected instance is unavailable; inspect and explicitly reselect it."
        evidence_problems: dict[str, str] = {}
        attempt = self._reported_evidence(ATTEMPT, evidence_problems)
        success = self._reported_evidence(SUCCESS, evidence_problems)
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
        archive_problems: dict[str, str] = {}
        archive_available = self._archive_available(
            Path(success["archive"]) if success else None,
            "latest_success",
            archive_problems,
            follow_symlinks=True,
        )
        maintenance = self.pending()
        ledger = self._reported_evidence(SHUTDOWN, evidence_problems)
        shutdowns = [
            {
                "instance": record["instance"],
                "started_at": record["started_at"],
                "outcome": "interrupted"
                if record["outcome"] == "running" and not busy
                else record["outcome"],
                **(
                    {"writer_generation": record["writer_generation"]}
                    if "writer_generation" in record
                    else {}
                ),
                "action": "Inspect unidentified shutdown evidence locally; unrelated captures cannot resolve it."
                if record["instance"] is None
                else "Inspect shutdown state and reconcile any hold, then select this instance and retry rss stop to capture a verified archive.",
            }
            for record in reversed(self._shutdown_records(ledger))
        ]
        if attempt and attempt["outcome"] == "running" and not busy:
            attempt = {
                **attempt,
                "outcome": "interrupted",
                "action": "Run rss backup-reconcile before retrying; no automatic resumption.",
            }
        daily = self._reported_evidence(DAILY, evidence_problems, schedule=schedule)
        latest_daily = (
            self._daily_report(daily, daily["latest_instance"]) if daily else None
        )
        daily_target = (
            self._target_attempt(daily, schedule, selected) if schedule else None
        )
        defer = self._reported_evidence(DEFER, evidence_problems)
        slot = (
            self._slot(schedule)
            if schedule and DAILY not in evidence_problems
            else None
        )
        recorded_slot = (
            datetime.fromisoformat(daily_target["slot"]) if daily_target else None
        )
        unattempted = slot is not None and (
            recorded_slot is None or recorded_slot < slot
        )
        daily_due = bool(
            slot
            and self._eligible(schedule, slot)
            and (
                unattempted
                or (recorded_slot == slot and daily_target["outcome"] != "success")
            )
        )
        missed = bool(
            daily_due
            and unattempted
            and self.now > slot + timedelta(seconds=RETRY_SECONDS)
        )
        daily_archive = (
            Path(daily_target["archive"])
            if daily_target and daily_target["outcome"] == "success"
            else None
        )
        daily_archive_available = self._archive_available(
            daily_archive,
            "daily_attempt" if daily_target == latest_daily else "daily_target_attempt",
            archive_problems,
            follow_symlinks=False,
        )
        daily_fresh = bool(
            daily_archive_available
            and 0
            <= (
                self.now - datetime.fromisoformat(daily_target["attempted_at"])
            ).total_seconds()
            <= 26 * 3600
        )
        registered = False
        if schedule:
            try:
                registered = self._job_matches(schedule) and self._job.loaded(schedule)
                self._job.check(schedule)
                if not registered:
                    problem = "Backup job missing, changed or not registered; inspect/re-enable the owned schedule explicitly."
                elif not schedule.get("registration_id"):
                    problem = "Backup registration lacks activation evidence; run rss backup-enable with the captured settings."
                self.runtime._owned(require_current=False, verify=False)
            except (RuntimeFailure, OSError, ValueError, KeyError, TypeError):
                problem = "Backup scheduler unavailable/conflicting; inspect runtime, job, launcher, native manifest and captured dependencies locally."
        return {
            "enabled": bool(schedule and schedule["active"]),
            "registered": registered,
            "registered_at": schedule.get("registered_at") if schedule else None,
            "registration_id": schedule.get("registration_id") if schedule else None,
            "busy": busy,
            "scheduler_problem": problem,
            "next_expected_attempt": self._next(schedule, daily_target).isoformat()
            if slot is not None
            and selected is not None
            and schedule.get("registration_id")
            else None,
            "daily_attempt": latest_daily,
            "daily_target_attempt": daily_target,
            "daily_archive_available": daily_archive_available,
            "daily_due": daily_due,
            "missed": missed,
            "latest_defer": defer,
            "latest_attempt": attempt,
            "latest_success": success,
            "unresolved_shutdown": shutdowns[0] if shutdowns else None,
            "unresolved_shutdowns": shutdowns,
            "evidence_problems": evidence_problems,
            "archive_problems": archive_problems,
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
                and not evidence_problems
                and not archive_problems
                and not shutdowns
                and not busy
                and daily_fresh
                and not daily_due
                and not maintenance
                and attempt
                and attempt["outcome"] == "success"
            ),
        }
