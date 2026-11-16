"""Opt-in daily backup orchestration and secret-safe protection evidence."""

import hashlib
import os
import plistlib
import re
import shutil
import stat
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rss_aggregator.runtime import Runtime

ATTEMPT = "backup-attempt.json"
SUCCESS = "backup-success.json"
SCHEDULE = "backup-schedule.json"
MAINTENANCE = "maintenance.json"
DAILY = "backup-daily.json"
DEFER = "backup-defer.json"
INTENT = "operator-intent.json"
LABEL = "io.rss-aggregator.backup"
RETRY_SECONDS = 900
MAX_ATTEMPTS = 3


class BackupSchedule:
    """Keep daily backup policy behind the runtime's public command lock."""

    def __init__(self, runtime: "Runtime", now: datetime | None = None) -> None:
        """Bind one runtime and an optional controlled aware orchestration clock."""
        self.runtime = runtime
        self.controlled_clock = now is not None
        self.now = now if now is not None else datetime.now(timezone.utc)
        if self.now.tzinfo is None:
            raise ValueError("Backup clock must include a time zone.")
        self.now = self.now.astimezone(timezone.utc)

    def _read(self, name: str) -> Any:
        """Read private managed state without treating malformed state as absence."""
        import json

        path = self.runtime.state_dir / name
        return json.loads(path.read_text()) if os.path.lexists(path) else None

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
        }
        self.runtime._save(ATTEMPT, attempt)
        try:
            result = operation()
        except Exception:
            marker = self._read(MAINTENANCE)
            self.runtime._save(
                ATTEMPT,
                {
                    **attempt,
                    "outcome": "failed",
                    "resumption": "failed"
                    if marker and marker.get("phase") == "resuming"
                    else "unresolved"
                    if marker
                    else "preserved",
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
            success = {
                **archive,
                "completed_at": completed.isoformat(),
                "volume": self.runtime.volume,
            }
            self.runtime._save(SUCCESS, success)
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
        """Locate only this user's explicitly opted-in backup LaunchAgent."""
        return Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")

    def _launch(self, *args: str) -> Any:
        """Query or change launchd without echoing daemon diagnostics."""
        from rss_aggregator.runtime import RuntimeFailure

        config = self._read(SCHEDULE)
        executable = (
            config["dependencies"]["launchctl"] if config else shutil.which("launchctl")
        )
        if executable is None or not Path(executable).is_absolute():
            raise RuntimeFailure(
                "Selected launchctl executable unavailable; inspect the captured configuration locally."
            )
        return self.runtime._run(executable, *args, check=False)

    def _loaded(self, config: dict[str, Any]) -> bool:
        """Reject a loaded same-label job from another path or executable."""
        from rss_aggregator.runtime import RuntimeFailure

        result = self._launch("print", f"gui/{os.getuid()}/{LABEL}")
        if result.returncode:
            # Match supervision: only missing domain/service establishes absence.
            if result.returncode in (3, 113):
                return False
            raise RuntimeFailure(
                "Cannot inspect backup launchd registration; inspect the normal user session and captured launchctl permissions. Registration retained."
            )
        fields = {line.strip() for line in result.stdout.splitlines()}
        arguments = re.search(r"(?ms)^\s*arguments = \{\n(.*?)^\s*\}", result.stdout)
        environment = re.search(
            r"(?ms)^\s*environment = \{\n(.*?)^\s*\}", result.stdout
        )
        expected_arguments = [
            config["python"],
            "-I",
            "-m",
            "rss_aggregator",
            "backup-due",
        ]
        if (
            "path = " + config["job"] not in fields
            or "program = " + config["python"] not in fields
            or arguments is None
            or [line.strip() for line in arguments[1].splitlines() if line.strip()]
            != expected_arguments
            or environment is None
            or not {
                key + " => " + value for key, value in config["environment"].items()
            }.issubset({line.strip() for line in environment[1].splitlines()})
        ):
            raise RuntimeFailure(
                "Conflicting backup LaunchAgent; inspect launchctl locally, do not replace it."
            )
        return True

    def _config(self) -> dict[str, Any] | None:
        """Require configuration to belong to the selected checkout and owner."""
        from rss_aggregator.runtime import RuntimeFailure

        config = self._read(SCHEDULE)
        if config is not None:
            owner = self._read("owner.json")
            if (
                config.get("version") != 1
                or not owner
                or config.get("owner") != owner["token"]
                or config.get("root") != str(self.runtime.root)
                or config.get("state_dir") != str(self.runtime.state_dir)
                or config.get("job") != str(self.job)
            ):
                raise RuntimeFailure(
                    "Backup schedule ownership/checkout changed; inspect private configuration locally."
                )
        return config

    def _job_matches(self, config: dict[str, Any]) -> bool:
        """Verify the private owned definition without adopting a foreign file."""
        if self.job.is_symlink() or not self.job.is_file():
            return False
        info = self.job.stat()
        return (
            info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o600
            and hashlib.sha256(self.job.read_bytes()).hexdigest()
            == config["job_sha256"]
        )

    def _dependencies(self) -> dict[str, str]:
        """Select absolute engine/launchd/forwarding executables from this invocation."""
        from rss_aggregator.runtime import RuntimeFailure

        names = ["launchctl", "podman"]
        if (
            os.environ.get("RSS_TAILSCALE") == "1"
            or (self.runtime.state_dir / "serve.json").exists()
        ):
            names.append("tailscale")
        result = {}
        for name in names:
            executable = shutil.which(name)
            if executable is None or not Path(executable).is_absolute():
                raise RuntimeFailure(
                    "Backup dependencies unavailable or relative; configure an absolute executable PATH before enabling."
                )
            result[name] = executable
        return result

    def _dependency_problem(self, config: dict[str, Any]) -> str | None:
        """Expose missing selected code/dependencies without running native services."""
        if any(
            not Path(path).is_file() or not os.access(path, os.X_OK)
            for path in [config["python"], *config["dependencies"].values()]
        ):
            return "Selected checkout Python or captured dependencies unavailable; restore them or explicitly reconfigure the owned schedule."
        probe = self.runtime._run(
            config["python"],
            "-I",
            "-c",
            "from pathlib import Path; import rss_aggregator.runtime as r; print(Path(r.__file__).resolve().parents[1])",
            check=False,
        )
        if probe.returncode or probe.stdout.strip() != config["root"]:
            return "Selected Python cannot import this checkout's installed runtime; run uv sync --locked there, then recheck."
        return None

    def _prerequisites(self) -> dict[str, str]:
        """Capture absolute dependencies and settings, never an interactive shell."""
        from rss_aggregator.runtime import RuntimeFailure

        if sys.platform != "darwin":
            raise RuntimeFailure(
                "Daily backup scheduling currently requires macOS launchd."
            )
        try:
            port = int(os.environ.get("RSS_PORT", "8080"))
        except ValueError:
            raise RuntimeFailure(
                "RSS_PORT must be an integer from 1024 to 65535."
            ) from None
        if not 1024 <= port <= 65535:
            raise RuntimeFailure("RSS_PORT must be from 1024 to 65535.")
        if os.environ.get("RSS_TAILSCALE", "0") not in ("0", "1"):
            raise RuntimeFailure("RSS_TAILSCALE must be 0 or 1.")
        python = self.runtime.root / ".venv/bin/python"
        if not python.is_file() or not os.access(python, os.X_OK):
            raise RuntimeFailure(
                "Selected checkout Python unavailable; run uv sync --locked there before enabling."
            )
        if not shutil.which("launchctl") or not shutil.which("podman"):
            raise RuntimeFailure(
                "launchctl/Podman unavailable; install/configure dependencies explicitly before enabling."
            )
        if (
            os.environ.get("RSS_TAILSCALE") == "1"
            or (self.runtime.state_dir / "serve.json").exists()
        ) and not shutil.which("tailscale"):
            raise RuntimeFailure(
                "Tailscale executable unavailable; resolve private forwarding dependencies first."
            )
        return {
            "HOME": str(Path.home()),
            "PATH": os.pathsep.join(
                dict.fromkeys(
                    [
                        *(
                            str(Path(path).parent)
                            for path in self._dependencies().values()
                        ),
                        "/usr/bin",
                        "/bin",
                        "/usr/sbin",
                        "/sbin",
                    ]
                )
            ),
            "RSS_STATE_DIR": str(self.runtime.state_dir),
            "RSS_BACKUP_DIR": str(
                Path(os.environ["RSS_BACKUP_DIR"]).expanduser().resolve()
            ),
            "RSS_PORT": os.environ.get("RSS_PORT", "8080"),
            "RSS_TAILSCALE": os.environ.get("RSS_TAILSCALE", "0"),
        }

    def enable(self, hour: int = 3, minute: int = 0) -> dict[str, Any]:
        """Explicitly install a private per-user launchd wakeup with a daily UTC policy."""
        from rss_aggregator.recovery import Recovery
        from rss_aggregator.runtime import RuntimeFailure

        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("Daily UTC hour/minute must be 0–23 and 0–59.")
        directory = Recovery(self.runtime)._directory()
        env = self._prerequisites()
        owner, _machine = self.runtime._owned()
        info = self.runtime.state_dir.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise RuntimeFailure(
                "Scheduling requires a private owned RSS_STATE_DIR (0700); fix permissions explicitly."
            )
        existing = self._config()
        python = str(self.runtime.root / ".venv/bin/python")
        definition = {
            "Label": LABEL,
            "ProgramArguments": [python, "-I", "-m", "rss_aggregator", "backup-due"],
            "WorkingDirectory": str(self.runtime.root),
            "EnvironmentVariables": env,
            "StartInterval": RETRY_SECONDS,
            "RunAtLoad": True,
            "ProcessType": "Background",
            "Umask": 0o077,
        }
        encoded = plistlib.dumps(definition, sort_keys=True)
        config = {
            "version": 1,
            "owner": owner["token"],
            "root": str(self.runtime.root),
            "state_dir": str(self.runtime.state_dir),
            "job": str(self.job),
            "python": python,
            "hour": hour,
            "minute": minute,
            "timezone": "UTC",
            "environment": env,
            "destination": str(directory),
            "job_sha256": hashlib.sha256(encoded).hexdigest(),
            "policy_id": hashlib.sha256(
                encoded + f"{hour}:{minute}".encode()
            ).hexdigest(),
            "dependencies": self._dependencies(),
            "active": False,
        }
        problem = self._dependency_problem(config)
        if problem:
            raise RuntimeFailure(problem)
        if existing and {**existing, "active": False} != config:
            raise RuntimeFailure(
                "Backup schedule settings changed; explicitly disable the owned schedule before re-enabling."
            )
        if self.job.exists() or self.job.is_symlink():
            if not existing or not self._job_matches(existing):
                raise RuntimeFailure(
                    "Refusing conflicting/unowned backup LaunchAgent definition."
                )
        loaded = self._loaded(config)
        if loaded and not existing:
            raise RuntimeFailure("Refusing unowned loaded backup LaunchAgent.")
        self.job.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = self.job.parent.stat()
        if (
            self.job.parent.is_symlink()
            or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) & 0o022
        ):
            raise RuntimeFailure(
                "LaunchAgents directory is unsafe; inspect ownership/permissions locally."
            )
        self.runtime._save(SCHEDULE, config)
        if not self.job.exists():
            descriptor = os.open(self.job, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
        if (
            not loaded
            and self._launch(
                "bootstrap", f"gui/{os.getuid()}", str(self.job)
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
            if self.job.exists() or self.job.is_symlink():
                raise RuntimeFailure("Backup job is unowned; nothing removed.")
            return {"enabled": False}
        if not self._job_matches(config):
            raise RuntimeFailure(
                "Backup job changed; nothing removed. Inspect locally."
            )
        if (
            self._loaded(config)
            and self._launch("bootout", f"gui/{os.getuid()}/{LABEL}").returncode
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
        """Bind daily eligibility to the exact captured policy, not just a calendar day."""
        return bool(
            daily
            and daily.get("slot") == self._slot(config).isoformat()
            and daily.get("policy_id") == config["policy_id"]
        )

    def _next(self, config: dict[str, Any]) -> datetime:
        """Describe the earliest expected daily/retry target (wakeup may lag 15 minutes)."""
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
        """Record lock contention independently without touching the active attempt.

        Unique staging names prevent overlapping launch/manual callers from racing
        the main writer. This record grants no archive or restart authority.
        """
        import json

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

    def due(self) -> dict[str, Any]:
        """Try at most three times per daily target, with no overlapping backlog."""
        from rss_aggregator.runtime import RuntimeFailure

        config = self._config()
        if not config or not config["active"]:
            return {"outcome": "disabled"}
        if not self._job_matches(config) or not self._loaded(config):
            raise RuntimeFailure(
                "Backup job is missing, changed or not registered; nothing started. Inspect backup-status and the owned schedule locally."
            )
        if any(
            os.environ.get(key, default) != value
            for key, value in config["environment"].items()
            if key.startswith("RSS_")
            for default in [
                "8080" if key == "RSS_PORT" else "0" if key == "RSS_TAILSCALE" else ""
            ]
        ):
            self.runtime._save(
                ATTEMPT,
                {
                    "started_at": self.now.isoformat(),
                    "kind": "backup",
                    "outcome": "failed",
                    "resumption": "preserved",
                    "action": "Runtime settings differ from the captured schedule. Restore the selected settings or explicitly disable/re-enable.",
                },
            )
            raise RuntimeFailure(
                "Backup job runtime settings differ from captured configuration; use its selected checkout/settings or disable and re-enable explicitly."
            )
        problem = self._dependency_problem(config)
        if problem or any(
            shutil.which(name) != path for name, path in config["dependencies"].items()
        ):
            self.runtime._save(
                ATTEMPT,
                {
                    "started_at": self.now.isoformat(),
                    "kind": "backup",
                    "outcome": "failed",
                    "resumption": "preserved",
                    "action": "Captured dependencies/code unavailable or changed. Restore selected executable PATH or explicitly disable/re-enable the owned schedule.",
                },
            )
            raise RuntimeFailure(
                problem
                or "Backup executable PATH differs from captured configuration; nothing started."
            )
        slot = self._slot(config)
        daily = self._read(DAILY)
        if self.now < slot or (
            self._same_target(daily, config)
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
            "count": daily["count"] + 1 if self._same_target(daily, config) else 1,
            "attempted_at": self.now.isoformat(),
            "outcome": "running",
            "volume": self.runtime.volume,
            "policy_id": config["policy_id"],
        }
        self.runtime._save(DAILY, daily)
        try:
            result = self.backup()
        except Exception:
            self.runtime._save(DAILY, {**daily, "outcome": "failed"})
            raise RuntimeFailure(
                "Daily backup failed. Inspect rss backup-status, destination/dependencies and maintenance locally; no private engine diagnostics are emitted by this command."
            ) from None
        self.runtime._save(DAILY, {**daily, "outcome": "success"})
        return {"outcome": "success", "backup": result}

    def pending(self) -> bool:
        """Treat any durable maintenance presence, including malformed data, as a hold."""
        return os.path.lexists(self.runtime.state_dir / MAINTENANCE)

    def begin(self, owner: dict[str, Any], operation: str) -> None:
        """Persist maintenance before any VM, forwarding or writer mutation."""
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
        import os

        (self.runtime.state_dir / MAINTENANCE).unlink()
        descriptor = os.open(self.runtime.state_dir, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def may_resume(self) -> bool:
        """Honor the sibling's stopped intent; malformed intent also fails closed."""
        from rss_aggregator.runtime import RuntimeFailure

        intent = self._read(INTENT)
        if intent is None:
            return True  # Preserve legacy observed state, never enable intent.
        if intent.get("version") != 1 or intent.get("intent") not in (
            "running",
            "stopped",
        ):
            raise RuntimeFailure(
                "Invalid operator intent; inspect locally before resumption."
            )
        return intent["intent"] == "running"

    def reconcile(self) -> dict[str, Any]:
        """Confirm owned writers idle under the lock, then release the restart hold.

        This never resumes an application or starts a stopped VM. The inherited
        command lock prevents reconciliation while an orphaned export still runs.
        """
        from rss_aggregator.recovery import Recovery
        from rss_aggregator.runtime import RuntimeFailure

        marker = self._read(MAINTENANCE)
        if marker is None:
            return {
                "reconciled": False,
                "resumed": False,
                "action": "No maintenance hold exists.",
            }
        owner, machine = self.runtime._owned()
        if (
            marker.get("version") != 1
            or marker.get("owner") != owner["token"]
            or marker.get("operation") not in ("backup", "stop", "restore")
        ):
            raise RuntimeFailure(
                "Unrecognized maintenance owner/schema; preserve records and inspect locally."
            )
        if machine["State"] == "running":
            Recovery(self.runtime)._engines_idle(owner, stop=True)
            Recovery(self.runtime)._engines_idle(owner, stop=False)
        elif machine["State"] != "stopped":
            raise RuntimeFailure("Wait for the VM transition before reconciliation.")
        attempt = self._read(ATTEMPT)
        if attempt and attempt["outcome"] == "running":
            self.runtime._save(
                ATTEMPT,
                {**attempt, "outcome": "interrupted", "resumption": "not_resumed"},
            )
        self.finish()
        return {
            "reconciled": True,
            "resumed": False,
            "action": "Writers confirmed idle and hold released; a separately enabled supervisor may honor existing running intent. Inspect backup-status before explicit startup.",
        }

    def status(self, *, busy: bool = False) -> dict[str, Any]:
        """Report evidence and freshness; configuration alone never proves protection."""
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
        daily_due = bool(
            slot
            and self.now >= slot
            and not (
                self._same_target(daily, schedule) and daily.get("outcome") == "success"
            )
        )
        missed = bool(
            daily_due
            and self.now > slot + timedelta(seconds=RETRY_SECONDS)
            and not self._same_target(daily, schedule)
        )
        daily_fresh = bool(
            daily
            and schedule
            and daily.get("policy_id") == schedule["policy_id"]
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
                registered = self._job_matches(schedule) and self._loaded(schedule)
                problem = self._dependency_problem(schedule)
                if not registered and problem is None:
                    problem = "Backup job is missing, changed or not registered; inspect/re-enable the owned schedule explicitly."
            except Exception:
                problem = "Backup scheduler unavailable/conflicting; inspect job and captured dependencies locally."
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
                and daily.get("volume") == self.runtime.volume
                and success.get("volume") == self.runtime.volume
                and archive_available
                and not daily_due
                and not overdue
                and not maintenance
                and attempt
                and attempt["outcome"] == "success"
            ),
        }
