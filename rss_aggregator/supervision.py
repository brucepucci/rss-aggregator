"""Opt-in user-session supervision, separate from operator lifecycle intent."""

import hashlib
import json
import os
import plistlib
import re
import shutil
import sys
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
JOB_LABEL = "io.rss-aggregator.supervision"
RECOVERY_STATE = "supervision-recovery.json"


class Supervision:
    """Coordinate persisted intent and noninterrupting recovery under Runtime's lock."""

    def __init__(self, runtime: "Runtime") -> None:
        """Bind the selected checkout and ownership directory without side effects."""
        self.runtime = runtime

    def intent(self) -> str:
        """Read fail-closed operator intent; absence never authorizes recovery."""
        path = self.runtime.state_dir / INTENT
        if not path.exists():
            return "unset"
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
            self._configuration(record)
            executable = record["dependencies"]["launchctl"]
            session = self.runtime._run(
                executable, "print", self._domain(), check=False
            )
            if session.returncode:
                return {
                    **result,
                    "state": "session_unavailable",
                    "session": "unavailable",
                    "action": "Log in/unlock the normal Mac user session; do not change security settings.",
                }
            result["session"] = "available"
            if not self._loaded(executable, Path(record["job"]), record):
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
        with self._environment(record):
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
            with self._environment(record):
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

    def _configuration(self, record: dict[str, Any]) -> None:
        """Refuse moved code, pins or missing captured executables without fallback."""
        self._check_job(record)
        if (
            record["root"] != str(self.runtime.root)
            or record["image_sha256"]
            != hashlib.sha256(
                (self.runtime.root / "config/image.json").read_bytes()
            ).hexdigest()
        ):
            raise ValueError("Captured checkout or image pin changed.")
        for executable in (record["python"], *record["dependencies"].values()):
            if not Path(executable).is_file() or not os.access(executable, os.X_OK):
                raise ValueError("Captured executable unavailable.")
        for name, executable in record["dependencies"].items():
            if shutil.which(name, path=record["environment"]["PATH"]) != executable:
                raise ValueError(
                    "Captured dependency lookup differs; no fallback is allowed."
                )
        expected = os.environ.get("RSS_SUPERVISION_ROOT")
        if expected is not None and expected != record["root"]:
            raise ValueError("Background checkout identity differs.")

    @contextmanager
    def _environment(self, record: dict[str, Any]) -> Iterator[None]:
        """Use only captured runtime selectors and suppress sensitive engine chatter."""
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

    def _observe(self) -> dict[str, Any]:
        """Check existing readiness without calling start or altering forwarding."""
        from rss_aggregator.runtime import RuntimeFailure

        try:
            result = self.runtime._status()
            ready = result.get("http_ready") is True
            if PrivateAccess(self.runtime).enabled():
                ready = (
                    ready and result.get("forwarding", {}).get("status") == "running"
                )
            return {
                "availability": "healthy" if ready else "unhealthy",
                "machine": result["machine"],
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
                "Preserve resources and inspect machine/connection/container ownership locally; never relabel or adopt foreign resources.",
            ),
            (
                (
                    "loopback port",
                    "publication differs",
                    "image/port/entrypoint differs",
                ),
                "configuration_conflict",
                "Inspect the captured loopback port and owned container configuration; do not stop foreign listeners. Remove/reinstall supervision before deliberate configuration changes.",
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
                    "image identity",
                    "image differs",
                ),
                "security_gate_failed",
                "Inspect the reviewed image/PHP and probe-cleanup gate locally; restore matching configuration and never bypass security verification.",
            ),
            (
                ("http", "readiness"),
                "readiness_failed",
                "Check loopback HTTP and the owned application locally; trusted client HTTPS remains a separate check.",
            ),
            (
                ("cron", "scheduler"),
                "fetching_unhealthy",
                "Inspect the installed fetching job and daemon with rss status; after fixing the cause, use explicit start for guarded recovery.",
            ),
            (
                ("unavailable", " failed ("),
                "external_command_failed",
                "Inspect captured Podman/Tailscale executables, VM connection and local permissions; restore dependencies before explicit start.",
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
        """Read the registration ledger, refusing unknown ownership or formats."""
        path = self.runtime.state_dir / REGISTRATION
        if not path.exists():
            return None
        record = json.loads(path.read_text())
        owner = json.loads((self.runtime.state_dir / "owner.json").read_text())
        if (
            not isinstance(record, dict)
            or record.get("version") != 1
            or record.get("owner") != owner["token"]
            or record.get("state_dir") != str(self.runtime.state_dir)
            or record.get("job") != str(self._job())
        ):
            raise ValueError(
                "Supervision registration ownership differs; inspect locally."
            )
        return record

    def _job(self) -> Path:
        """Use one user-owned label, preventing duplicate supervisors for this VM."""
        directory = Path.home() / "Library/LaunchAgents"
        if directory.is_symlink() or directory.parent.is_symlink():
            raise ValueError(
                "Refusing symlinked service directories; preserve and resolve ownership locally."
            )
        return directory / (JOB_LABEL + ".plist")

    def _domain(self) -> str:
        """Target only the current normal user's GUI session, never system launchd."""
        return f"gui/{os.getuid()}"

    def _loaded(self, executable: str, job: Path, record: dict[str, Any]) -> bool:
        """Refuse loaded jobs whose path, command or captured environment differs."""
        result = self.runtime._run(
            executable, "print", self._domain() + "/" + JOB_LABEL, check=False
        )
        if result.returncode:
            # launchctl uses 113 for a missing service and 3 for a missing domain.
            if result.returncode in (3, 113):
                return False
            raise ValueError(
                "Cannot inspect launchd registration; inspect the user session."
            )
        lines = {line.strip() for line in result.stdout.splitlines()}
        arguments = re.search(r"(?ms)^\s*arguments = \{\n(.*?)^\s*\}", result.stdout)
        environment = re.search(
            r"(?ms)^\s*environment = \{\n(.*?)^\s*\}", result.stdout
        )
        expected = plistlib.loads(self._definition(record))["ProgramArguments"]
        if (
            "path = " + str(job) not in lines
            or "program = " + record["python"] not in lines
            or arguments is None
            or [line.strip() for line in arguments[1].splitlines() if line.strip()]
            != expected
            or environment is None
            or not {
                key + " => " + value for key, value in record["environment"].items()
            }.issubset({line.strip() for line in environment[1].splitlines()})
        ):
            raise ValueError(
                "Refusing foreign/changed loaded supervision job; inspect launchd locally."
            )
        return True

    def _definition(self, record: dict[str, Any]) -> bytes:
        """Build a secret-free fixed command; discard raw command output."""
        return plistlib.dumps(
            {
                "Label": JOB_LABEL,
                "ProgramArguments": [
                    record["python"],
                    "-I",
                    "-B",
                    "-m",
                    "rss_aggregator",
                    "supervision-tick",
                ],
                "WorkingDirectory": record["root"],
                "EnvironmentVariables": record["environment"],
                "RunAtLoad": True,
                "StartInterval": 60,
                "ProcessType": "Background",
                # VM helpers outlive this bounded invocation. Descriptor inheritance
                # remains independently restricted at the VM launcher boundary.
                "AbandonProcessGroup": True,
                "StandardOutPath": "/dev/null",
                "StandardErrorPath": "/dev/null",
            },
            sort_keys=True,
        )

    def _check_job(
        self, record: dict[str, Any], *, missing: bool = False, removal: bool = False
    ) -> None:
        """Accept the exact pre-lifetime-policy definition only for owned removal."""
        job = Path(record["job"])
        definitions = [self._definition(record)]
        if removal:
            legacy = plistlib.loads(definitions[0])
            del legacy["AbandonProcessGroup"]
            definitions.append(plistlib.dumps(legacy, sort_keys=True))
        if job.is_symlink() or (job.exists() and job.read_bytes() not in definitions):
            raise ValueError(
                "Refusing changed/unowned supervision job. An unchanged pre-fix owned job may be removed with supervision-remove, then explicitly reinstalled; never overwrite or hand-edit it."
            )
        if not missing and not job.exists():
            raise ValueError(
                "Supervision job is missing; rerun supervision-install from the selected checkout."
            )

    def _capture(self, token: str) -> dict[str, Any]:
        """Capture a stable environment instead of relying on a shell or dot-env."""
        root = self.runtime.root
        python = root / ".venv/bin/python"
        if (
            root != Path(__file__).resolve().parents[1]
            or not python.is_file()
            or not os.access(python, os.X_OK)
            or Path(sys.executable).absolute() != python
        ):
            raise ValueError(
                "Run supervision-install with the selected checkout's .venv/bin/python (uv run --locked rss)."
            )
        port = int(os.environ.get("RSS_PORT", "8080"))
        private = os.environ.get("RSS_TAILSCALE", "0")
        if not 1024 <= port <= 65535 or private not in ("0", "1"):
            raise ValueError(
                "Set valid RSS_PORT (1024–65535) and RSS_TAILSCALE (0 or 1)."
            )
        dependencies = {}
        names = ["launchctl", "podman"]
        if private == "1" or PrivateAccess(self.runtime).path.exists():
            names.append("tailscale")
        for name in names:
            executable = shutil.which(name)
            if not executable:
                raise ValueError(
                    f"{name} unavailable; install/configure it explicitly before supervision-install."
                )
            dependencies[name] = str(Path(executable).absolute())
        environment = {
            "HOME": str(Path.home()),
            "PATH": os.pathsep.join(
                dict.fromkeys(
                    [str(Path(p).parent) for p in dependencies.values()]
                    + ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
                )
            ),
            "RSS_STATE_DIR": str(self.runtime.state_dir),
            "RSS_PORT": str(port),
            "RSS_TAILSCALE": private,
            "RSS_SUPERVISION_ROOT": str(root),
        }
        backup = os.environ.get("RSS_BACKUP_DIR")
        if backup:
            destination = Path(backup).expanduser()
            if not destination.is_absolute():
                raise ValueError(
                    "RSS_BACKUP_DIR must be absolute when captured for supervision."
                )
            environment["RSS_BACKUP_DIR"] = str(destination.resolve())
        return {
            "version": 1,
            "owner": token,
            "root": str(root),
            "state_dir": str(self.runtime.state_dir),
            "python": str(python),
            "dependencies": dependencies,
            "environment": environment,
            "job": str(self._job()),
            "image_sha256": hashlib.sha256(
                (root / "config/image.json").read_bytes()
            ).hexdigest(),
        }

    def install(self) -> dict[str, Any]:
        """Explicitly register and activate only an owned normal-session LaunchAgent."""
        if sys.platform != "darwin" or os.getuid() == 0:
            raise ValueError(
                "Supervision requires a normal macOS user's session; no privileged installation is supported."
            )
        owner, _machine = self.runtime._owned()
        record = self._record()
        captured = self._capture(owner["token"])
        if record and record != captured:
            raise ValueError(
                "Captured supervision configuration differs; remove it first, then explicitly install the reviewed configuration."
            )
        job = self._job()
        if not record and (job.exists() or job.is_symlink()):
            raise ValueError(
                "Refusing unowned supervision job; preserve and resolve it locally."
            )
        executable = captured["dependencies"]["launchctl"]
        loaded = self._loaded(executable, job, captured)
        if loaded and not record:
            raise ValueError("Refusing a loaded job without our registration ledger.")
        session = self.runtime._run(executable, "print", self._domain(), check=False)
        if session.returncode:
            raise ValueError(
                "Normal user GUI session is unavailable; log in/unlock and retry installation."
            )
        if record:
            self._check_job(record, missing=True)
        self.runtime._save(REGISTRATION, captured)
        if not job.exists():
            job.parent.mkdir(parents=True, exist_ok=True)
            with job.open("xb") as stream:
                os.chmod(job, 0o600)
                stream.write(self._definition(captured))
        if not loaded:
            result = self.runtime._run(
                executable, "bootstrap", self._domain(), str(job), check=False
            )
            if result.returncode:
                raise ValueError(
                    "Supervision activation failed; registration retained. Inspect the user session and retry supervision-install or remove."
                )
        return self.status()

    def remove(self) -> dict[str, Any]:
        """Unload and remove only the unchanged owned job; preserve intent and data."""
        record = self._record()
        if not record:
            if self._job().exists() or self._job().is_symlink():
                raise ValueError(
                    "Refusing unowned supervision job; no resources were removed."
                )
            return self.status()
        self._check_job(record, missing=True, removal=True)
        executable = record["dependencies"]["launchctl"]
        if self._loaded(executable, Path(record["job"]), record):
            result = self.runtime._run(
                executable, "bootout", self._domain() + "/" + JOB_LABEL, check=False
            )
            if result.returncode:
                raise ValueError(
                    "Supervision removal failed; registration retained for inspection/retry."
                )
        Path(record["job"]).unlink(missing_ok=True)
        (self.runtime.state_dir / REGISTRATION).unlink()
        return self.status()
