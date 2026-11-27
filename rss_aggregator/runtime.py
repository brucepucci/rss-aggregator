"""Guarded Apple Silicon FreshRSS lifecycle, independent of Click."""

import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from rss_aggregator import native_service as service
from rss_aggregator.backup_schedule import BackupSchedule
from rss_aggregator.private_access import PrivateAccess
from rss_aggregator.recovery import (
    INITIAL_INSTANCE,
    SELECTION,
    SHUTDOWN_SUCCESS,
    Recovery,
    instance_name,
)
from rss_aggregator.supervision import Supervision

Operation = Literal[
    "setup",
    "start",
    "status",
    "fetch",
    "verify",
    "stop",
    "backup",
    "restore",
    "select-instance",
    "import-legacy",
    "api-check",
    "supervision-status",
    "supervision-install",
    "supervision-remove",
    "supervision-tick",
    "backup-status",
    "backup-reconcile",
    "backup-enable",
    "backup-disable",
    "backup-due",
    "uninstall",
    "upgrade",
]


class RuntimeFailure(Exception):
    """Report a lifecycle guard or external command failure without CLI coupling."""


class PublicBackendExposed(RuntimeFailure):
    """Report confirmed public forwarding to the specific requested backend."""


class ServeConfigRejected(RuntimeFailure):
    """Report a conditional Serve write known not to have applied any change."""


class Runtime:
    """Manage one private native installation under an independent command lock."""

    def __init__(self, root: Path | None = None, state_dir: Path | None = None) -> None:
        """Resolve configured paths without touching the instance or starting jobs."""
        self.root = (root or Path(__file__).resolve().parents[1]).expanduser().resolve()
        path = Path(
            state_dir or os.environ.get("RSS_STATE_DIR", self.root / ".local/native")
        ).expanduser()
        if path.is_symlink():
            raise RuntimeFailure("RSS_STATE_DIR must not be a symlink.")
        self.state_dir = path.resolve()
        self._command_lock_fd: int | None = None

    @property
    def instance(self) -> str:
        """Read the explicit data selection or the initial default name."""
        try:
            selection = service.read(self.state_dir / SELECTION)
            return instance_name(
                selection["instance"] if selection is not None else INITIAL_INSTANCE
            )
        except (ValueError, KeyError, TypeError):
            raise RuntimeFailure(
                "Invalid native instance selection; inspect local ownership."
            ) from None

    @property
    def data_dir(self) -> Path:
        """Locate the full local data directory of the selected FreshRSS instance."""
        return self.state_dir / "instances" / self.instance / "data"

    def run(
        self,
        operation: Operation,
        *,
        archive: Path | None = None,
        instance: str | None = None,
        username: str | None = None,
        api_password: str | None = None,
        loopback: bool = False,
        now: datetime | None = None,
        hour: int = 3,
        minute: int = 0,
        confirm: str | None = None,
    ) -> dict[str, Any]:
        """Execute one public operation with nonblocking serialization and durable intent."""
        from rss_aggregator.native_install import NativeInstall

        recovery = Recovery(self)
        backups = BackupSchedule(self, now)
        supervision = Supervision(self)
        if operation in ("restore", "import-legacy") and (
            archive is None or instance is None
        ):
            raise RuntimeFailure("An archive and a fresh named target are required.")
        if operation == "select-instance" and instance is None:
            raise RuntimeFailure("Select-instance requires an explicit target.")
        handlers = {
            "setup": NativeInstall(self).install,
            "uninstall": lambda: self._runtime_change("uninstall", confirm),
            "upgrade": lambda: self._runtime_change("upgrade", confirm),
            "backup": backups.backup,
            "backup-status": backups.status,
            "backup-reconcile": backups.reconcile,
            "backup-enable": lambda: backups.enable(hour, minute),
            "backup-disable": backups.disable,
            "backup-due": backups.due,
            "restore": lambda: recovery.restore(archive, instance),
            "select-instance": lambda: recovery.select_instance(instance),
            "import-legacy": lambda: recovery.import_legacy(archive, instance),
            "api-check": lambda: self._api_check(
                username, api_password, loopback=loopback
            ),
            "start": self._start,
            "status": self._status,
            "fetch": self._fetch,
            "verify": self._verify,
            "stop": backups.shutdown,
            "supervision-status": supervision.status,
            "supervision-install": supervision.install,
            "supervision-remove": supervision.remove,
            "supervision-tick": supervision.tick,
        }
        if operation not in handlers:
            raise RuntimeFailure(f"Unknown operation: {operation}")
        self._state_guard()
        with service.lease(self.state_dir / "command.lock") as lock:
            if lock is None:
                if operation == "backup-status":
                    return backups.status(busy=True)
                if operation == "backup-due":
                    backups.deferred()
                raise RuntimeFailure(
                    "Another project command is running; wait for it to finish."
                )
            self._command_lock_fd = lock
            try:
                if operation == "stop":
                    supervision.set_intent("stopped")
                if backups.pending() and operation not in (
                    "status",
                    "backup-status",
                    "backup-reconcile",
                    "backup-disable",
                    "supervision-status",
                    "supervision-tick",
                    "supervision-remove",
                ):
                    if operation == "stop":
                        self._quiesce()
                    raise RuntimeFailure(
                        "Maintenance is unresolved; inspect rss backup-status, then rss backup-reconcile before operating writers."
                    )
                if operation == "start":
                    supervision.set_intent("running")
                return handlers[operation]()
            finally:
                self._command_lock_fd = None

    def _state_guard(self) -> None:
        """Refuse legacy, public, foreign, synced-default or network state."""
        if os.getuid() == 0:
            raise RuntimeFailure("Use a normal non-root user account for FreshRSS.")
        home = Path.home().resolve()
        synced = (home / "Library/CloudStorage", home / "Library/Mobile Documents")
        if any(self.state_dir.is_relative_to(directory) for directory in synced):
            raise RuntimeFailure("Use local, non-synced SQLite storage.")
        ancestor = next(
            p for p in (self.state_dir, *self.state_dir.parents) if p.exists()
        )
        if sys.platform == "darwin":
            disk = self._run("/bin/df", "-P", str(ancestor), check=False)
            lines = disk.stdout.splitlines()
            if (
                disk.returncode
                or len(lines) < 2
                or not re.match(r"/dev/disk\d", lines[1])
            ):
                raise RuntimeFailure("Use a local macOS filesystem for RSS_STATE_DIR.")
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.state_dir.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise RuntimeFailure(
                "RSS_STATE_DIR must be a private directory (0700) owned by this user."
            )
        owner = service.read(self.state_dir / "owner.json")
        if owner is not None and (
            not isinstance(owner, dict) or "machine" in owner or "connection" in owner
        ):
            raise RuntimeFailure(
                "Legacy Podman ownership found; use a separate private native RSS_STATE_DIR."
            )

    def _diagnostic(self, message: str) -> None:
        """Keep diagnostics separate from JSON results."""
        print(message, file=sys.stderr)

    def _run(
        self,
        *args: str,
        check: bool = True,
        input: str | None = None,
        inherit_lock: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run external host tooling without echoing its potentially private output."""
        try:
            result = subprocess.run(
                args,
                text=True,
                capture_output=True,
                input=input,
                check=False,
                pass_fds=(self._command_lock_fd,)
                if inherit_lock and self._command_lock_fd is not None
                else (),
            )
        except OSError as error:
            raise RuntimeFailure(
                f"Unable to run {Path(args[0]).name}; check the selected executable."
            ) from error
        if check and result.returncode:
            raise RuntimeFailure(
                f"{Path(args[0]).name} failed ({result.returncode}); inspect locally."
            )
        return result

    def _save(self, name: str, value: Any) -> None:
        """Atomically persist a private ownership or orchestration record."""
        service.save(self.state_dir / name, value)

    def _manifest(self) -> dict[str, Any]:
        """Read the checkout's reviewed runtime manifest without touching installation."""
        from rss_aggregator.native_install import read_manifest

        return read_manifest(self.root)

    def _identity(self) -> dict[str, str]:
        """Return the paths-independent native archive compatibility pin."""
        from rss_aggregator.native_install import manifest_identity

        return manifest_identity(self._manifest())

    def _owned(
        self, *, require_current: bool = True, verify: bool = True
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Bind operations to the native installation's ownership and pin."""
        from rss_aggregator.native_install import NativeInstall

        return NativeInstall(self).owned(require_current=require_current, verify=verify)

    def _ownership_receipt(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Identify the owner for shutdown independently of runtime layout drift."""
        from rss_aggregator.native_install import NativeInstall

        return NativeInstall(self).receipt()

    def _port(self) -> int:
        """Parse the non-privileged local listener port before starting writers."""
        try:
            port = int(os.environ.get("RSS_PORT", "8080"))
        except ValueError:
            port = 0
        if not 1024 <= port <= 65535:
            raise RuntimeFailure("RSS_PORT must be an integer between 1024 and 65535.")
        return port

    def _idle(self) -> bool:
        """Prove the service controller and all inherited writer children are gone."""
        with service.lease(self.state_dir / "writers.lock") as writer:
            return writer is not None

    def _live(self) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Read the current generation only with a held writer lease and matching evidence."""
        if self._idle():
            return None
        with service.lease(self.state_dir / "controller.lock") as controller:
            if controller is not None:
                return None
        plan = service.read(self.state_dir / "service.json")
        receipt = service.read(self.state_dir / "service-status.json")
        if (
            plan
            and receipt
            and receipt.get("nonce") == plan.get("nonce")
            and receipt.get("web") == "started"
        ):
            return plan, receipt
        return None

    def _stop_writers(self) -> None:
        """Request stop for this generation; success requires the kernel lease to release."""
        if self._idle():
            return
        plan = service.read(self.state_dir / "service.json")
        if not plan:
            raise RuntimeFailure(
                "Unidentified native writer lease; inspect the instance locally."
            )
        self._save("stop-service.json", {"nonce": plan["nonce"]})
        deadline = time.monotonic() + 165
        while time.monotonic() < deadline:
            if self._idle():
                return
            time.sleep(0.2)
        raise RuntimeFailure(
            "Native writers did not confirm shutdown; no archive or further mutation is safe. Inspect the own user processes locally."
        )

    def _quiesce(self) -> None:
        """Close writers even if conditional unpublication fails; preserve foreign routes."""
        owner, _ = self._ownership_receipt()
        try:
            PrivateAccess(self).stop(owner)
        finally:
            self._stop_writers()

    def _service_home(self, *, create: bool = False) -> Path:
        """Locate owned private configuration without following a directory link."""
        home = self.state_dir / "service"
        if create:
            home.mkdir(mode=0o700, exist_ok=True)
        info = home.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise RuntimeFailure(
                "Native service configuration is not private and owned."
            )
        return home

    @staticmethod
    def _write_service_file(path: Path, payload: bytes) -> None:
        """Replace one generated file without following links."""
        path.unlink(missing_ok=True)
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)

    def _prepare(self) -> None:
        """Configure explicit PHP settings and prepare only an uninitialized data target."""
        home = self._service_home(create=True)
        self._write_service_file(home / "php.ini", service.INI.encode())
        if not service.initialized(self.data_dir):
            self._php("cli/prepare.php")
            for filename, template in (
                ("config-user.custom.php", "config-user.custom.php"),
                ("opml.xml", "public-feed.opml"),
            ):
                path = self.data_dir / filename
                if not os.path.lexists(path):
                    with path.open("xb") as target:
                        target.write((self.root / "config" / template).read_bytes())

    def _php(self, *args: str) -> bytes:
        """Run bounded native PHP with a lease and never surface upstream output in errors."""
        binary = self.state_dir / "runtime/frankenphp"
        with service.lease(self.state_dir / "fetch.lock") as operation_lock:
            if operation_lock is None:
                raise RuntimeFailure(
                    "A feed fetch is already in progress; retry after it completes."
                )
            result = subprocess.run(
                [str(binary), "php-cli", *args],
                cwd=self.state_dir / "runtime/FreshRSS",
                env=service.environment(self.state_dir, self.data_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=120,
                check=False,
                pass_fds=tuple(
                    fd
                    for fd in (self._command_lock_fd, operation_lock)
                    if fd is not None
                ),
            )
        if result.returncode:
            raise RuntimeFailure(
                "FreshRSS PHP operation failed; inspect the instance locally (logs may contain secrets)."
            )
        return result.stdout

    def _configure(self, origin: str) -> None:
        """Enforce form authentication, the API and SQLite before opening the listener."""
        self._php(
            "cli/reconfigure.php",
            "--base-url",
            origin,
            "--auth-type",
            "form",
            "--allow-anonymous=false",
            "--allow-anonymous-refresh=false",
            "--api-enabled=true",
            "--disable-update=true",
        )
        code = (
            'require "cli/_cli.php"; $c=FreshRSS_Context::systemConf();'
            "echo json_encode([$c->auth_type,$c->allow_anonymous,$c->allow_anonymous_refresh,"
            '$c->api_enabled,$c->base_url,$c->disable_update,$c->db["type"]??null]);'
        )
        if json.loads(self._php("-r", code)) != [
            "form",
            False,
            False,
            True,
            origin,
            True,
            "sqlite",
        ]:
            raise RuntimeFailure(
                "Effective FreshRSS authentication, API, update or SQLite settings differ; no Serve forwarding changed."
            )

    def _launch(self, port: int, origin: str, private: bool) -> dict[str, Any]:
        """Spawn a detached lease-holding worker and require generation readiness."""
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind(("127.0.0.1", port))
            except OSError:
                raise RuntimeFailure(
                    f"Loopback port {port} is occupied; no foreign listener was stopped."
                ) from None
        nonce = uuid.uuid4().hex
        plan = {
            "nonce": nonce,
            "instance": self.instance,
            "port": port,
            "url": origin,
            "private": private,
            "runtime": self._identity(),
        }
        with (
            service.lease(self.state_dir / "writers.lock") as writer,
            service.lease(self.state_dir / "controller.lock") as controller,
        ):
            if writer is None or controller is None:
                raise RuntimeFailure(
                    "A native writer or controller lease is still held; no new writers were started."
                )
            self._save("service.json", plan)
            self._worker = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-m",
                    "rss_aggregator.native_service",
                    str(self.state_dir),
                    str(writer),
                    str(controller),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                pass_fds=(writer, controller),
                start_new_session=True,
            )
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if self._idle():
                break
            live = self._live()
            if live and self._ready(plan):
                break
            time.sleep(0.2)
        else:
            raise RuntimeFailure(
                "Owned PHP web service or scheduler did not become ready on loopback."
            )
        if self._idle() or not self._ready(plan):
            raise RuntimeFailure(
                "Native web service exited before loopback readiness; inspect the port and runtime pin."
            )
        return plan

    def _check_web(self, plan: dict[str, Any], initialized: bool) -> dict[str, Any]:
        """Check fresh effective web settings, readiness and anonymous API refusal."""
        port, nonce = plan["port"], plan["nonce"]
        probe = self._service_home() / "__rss_probe.php"
        source = (self.root / "config/security-probe.php").read_bytes()
        try:
            self._write_service_file(probe, source)
            code, body = service.web_check(
                port, "/__rss_probe.php", host="rss-" + nonce + ".invalid"
            )
            result = json.loads(body) if code == 200 else None
        finally:
            probe.unlink(missing_ok=True)
        if (
            not isinstance(result, dict)
            or result.get("sapi") != "frankenphp"
            or result.get("php") != plan["runtime"]["php"]
            or result.get("upload_progress") not in ("", "0")
            or result.get("expose_php") not in ("", "0")
            or result.get("display_errors") not in ("", "0")
            or result.get("sqlite") is not True
            or result.get("loaded_ini") != str(self.state_dir / "service/php.ini")
            or result.get("scanned_ini") not in ("", False, None)
            or result.get("initialized") is not initialized
            or (initialized and result.get("authenticated_settings") is not True)
        ):
            raise RuntimeFailure(
                "Effective web PHP settings or SQLite support failed verification."
            )
        if service.web_check(port, "/i/")[0] not in (200, 302, 303):
            raise RuntimeFailure(
                "The FreshRSS application did not pass loopback readiness."
            )
        if initialized and service.web_check(
            port, "/api/greader.php/reader/api/0/subscription/list"
        )[0] not in (401, 403):
            raise RuntimeFailure(
                "The unauthenticated Google Reader-compatible API was not refused."
            )
        return {**result, "verified": True}

    def _ready(self, plan: dict[str, Any]) -> bool:
        """Confirm the selected listener responds for this exact service generation."""
        code, reply = service.web_check(
            plan["port"], "/__rss_ready", host="rss-" + plan["nonce"] + ".invalid"
        )
        return code == 200 and reply == plan["nonce"].encode()

    def _retained_security(self, plan: dict[str, Any]) -> dict[str, Any] | None:
        """Attest fresh settings for retention or request a guarded restart instead."""
        try:
            initialized = service.initialized(self.data_dir)
            record = service.read(self.state_dir / "security.json")
            previous = record.get("effective") if isinstance(record, dict) else None
            if (
                not isinstance(previous, dict)
                or previous.get("initialized") is not initialized
                or (plan["private"] and not initialized)
            ):
                return None
            security = self._check_web(plan, initialized)
            self._save("security.json", {"nonce": plan["nonce"], "effective": security})
            return security
        except (RuntimeFailure, OSError, ValueError, KeyError, TypeError):
            # The guarded restart withdraws owned forwarding before opening writers.
            return None

    def _start(
        self,
        *,
        private_https: bool | None = None,
        port_override: int | None = None,
        recheck: bool = False,
    ) -> dict[str, Any]:
        """Start or recheck owned serving, failing closed before enabling private forwarding."""
        owner, _ = self._owned()
        port = self._port() if port_override is None else port_override
        access = PrivateAccess(self)
        private = access.enabled() if private_https is None else private_https
        live = self._live()
        try:
            origin = (
                access.prepare(owner, port) if private else f"http://127.0.0.1:{port}"
            )
        except PublicBackendExposed as error:
            if live is None or live[0].get("port") != port:
                raise
            # Leave the foreign route alone; close only the owned backend it reaches.
            try:
                self._stop_writers()
            except (RuntimeFailure, OSError, ValueError, KeyError, TypeError):
                raise RuntimeFailure(
                    f"{error} Shutdown of the owned backend could not be confirmed."
                ) from None
            raise RuntimeFailure(f"{error} Owned backend shutdown confirmed.") from None
        recovery = Recovery(self)
        recovery.check_activation()
        if live and not recheck:
            plan, receipt = live
            status = self._status()
            if (
                status["http_ready"]
                and plan["port"] == port
                and plan["url"] == origin
                and plan["private"] == private
                and plan["instance"] == self.instance
                and receipt.get("scheduler") == "running"
                and (
                    status["forwarding"]["status"] == "running"
                    if private
                    else status["forwarding"]["status"] in ("stopped", "unconfigured")
                )
            ):
                security = self._retained_security(plan)
                if security is not None:
                    return {
                        **status,
                        "security": security,
                        "fetch": {"status": "already_running"},
                    }
        try:
            self._quiesce()
            recovery.ensure_data()
            recovery.mark_writer_start()
            self._prepare()
            installed = service.initialized(self.data_dir)
            if private and not installed:
                raise RuntimeFailure(
                    "Complete the FreshRSS installer on loopback first (RSS_TAILSCALE=0); no Serve route was published."
                )
            fetch = {"status": "installer_pending"}
            if installed:
                self._configure(origin)
                fetch = self._actualize()
            plan = self._launch(port, origin, private)
            security = self._check_web(plan, installed)
            current, receipt = self._live() or ({}, {})
            if (
                current.get("nonce") != plan["nonce"]
                or receipt.get("scheduler") != "running"
            ):
                raise RuntimeFailure(
                    "Native feed scheduling did not pass the startup gate."
                )
            self._save("security.json", {"nonce": plan["nonce"], "effective": security})
            forwarding = access.start(owner, port, origin) if private else None
        except Exception:
            try:
                self._stop_writers()
            except Exception:
                raise RuntimeFailure(
                    "Startup failed and stopping native writers could not be confirmed; inspect potential remaining access. No foreign routes were changed."
                ) from None
            raise
        return {
            "runtime": "installed",
            "application": "running",
            "url": origin,
            "instance": self.instance,
            "security": security,
            "scheduler": self._scheduler(receipt),
            "fetch": fetch,
            **({"forwarding": forwarding} if forwarding else {}),
        }

    def _scheduler(self, receipt: dict[str, Any]) -> dict[str, Any]:
        """Report whether the owned native scheduler is running and its last attempt."""
        return {
            "running": receipt.get("scheduler") == "running",
            "cron": "*/20 * * * *",
            "timezone": "UTC",
            "last_fetch": receipt.get("last_fetch"),
        }

    def _status(self) -> dict[str, Any]:
        """Report installed pin, selected data, writer readiness and private forwarding."""
        if not os.path.lexists(self.state_dir / "runtime"):
            return {
                "runtime": "absent",
                "application": "stopped" if self._idle() else "unconfirmed",
                "http_ready": False,
            }
        owner, _ = self._owned(require_current=False, verify=False)
        live = self._live()
        plan, receipt = live or ({}, {})
        security = service.read(self.state_dir / "security.json")
        ready = bool(
            live
            and isinstance(security, dict)
            and security.get("nonce") == plan["nonce"]
            and plan.get("runtime") == self._identity()
            and self._ready(plan)
            and service.web_check(plan["port"], "/i/")[0] in (200, 302, 303)
        )
        return {
            "runtime": "installed",
            "application": "running"
            if live
            else "stopped"
            if self._idle()
            else "unconfirmed",
            "instance": self.instance,
            "http_ready": ready,
            "scheduler": self._scheduler(receipt),
            "forwarding": PrivateAccess(self).status(owner),
            **({"url": plan["url"], "port": plan["port"]} if live else {}),
        }

    def _actualize(self) -> dict[str, Any]:
        """Run the upstream fetcher without bypassing TTL, backoff or cache."""
        self._php("app/actualize_script.php")
        return {"status": "completed"}

    def _fetch(self) -> dict[str, Any]:
        """Fetch immediately for the selected and verified running instance."""
        self._owned()
        if not self._status()["http_ready"]:
            raise RuntimeFailure("Start a verified native service before fetching.")
        if not service.initialized(self.data_dir):
            return {"status": "installer_pending"}
        return self._actualize()

    def _verify(self) -> dict[str, Any]:
        """Recheck security and readiness by reopening the currently owned listener."""
        live = self._live()
        if live is None:
            raise RuntimeFailure("Start the owned service before verifying it.")
        return {
            **self._start(
                private_https=live[0]["private"],
                port_override=live[0]["port"],
                recheck=True,
            ),
            "verified": True,
        }

    def _api_check(
        self, username: str | None, password: str | None, *, loopback: bool = False
    ) -> dict[str, Any]:
        """Send prompted API credentials only to a verified owned origin."""
        owner, _ = self._owned()
        status = self._status()
        if not username or not password or not status["http_ready"]:
            raise RuntimeFailure(
                "A verified running instance and a separate API credential are required."
            )
        private = PrivateAccess(self)
        if loopback:
            origin = f"http://127.0.0.1:{status['port']}"
        else:
            forwarding = private.status(owner)
            if forwarding["status"] != "running":
                raise RuntimeFailure(
                    "Start with RSS_TAILSCALE=1 before testing trusted HTTPS."
                )
            origin = forwarding["url"]
        return {
            **private.check_api(origin, username, password),
            "transport": "loopback" if loopback else "https",
            "https_verified": not loopback,
        }

    def _runtime_change(self, operation: str, confirm: str | None) -> dict[str, Any]:
        """Require a stopped owned generation, no registered jobs and exact preview approval."""
        from rss_aggregator.native_install import NativeInstall

        owner, installed = self._owned(require_current=False)
        if not self._idle() or Supervision(self).intent() == "running":
            raise RuntimeFailure(
                "Run rss stop first; runtime changes require stopped intent and idle writers."
            )
        if any(
            os.path.lexists(self.state_dir / name)
            for name in ("supervision.json", "backup-schedule.json")
        ):
            raise RuntimeFailure(
                "Remove owned supervision and disable daily backup before changing the runtime."
            )
        if PrivateAccess(self).status(owner)["status"] not in (
            "stopped",
            "unconfigured",
        ):
            raise RuntimeFailure(
                "Private forwarding is not confirmed stopped; runtime retained."
            )
        plan = {
            "operation": operation,
            "runtime_path": str(self.state_dir / "runtime"),
            "installed": installed,
            "preserves_data_and_archives": True,
        }
        if operation == "upgrade":
            plan["reviewed_manifest"] = self._manifest()
        nonce = hashlib.sha256(
            json.dumps([owner["token"], plan], sort_keys=True).encode()
        ).hexdigest()
        if confirm is None:
            return {**plan, "confirmation": nonce, "status": "preview"}
        if confirm != nonce:
            raise RuntimeFailure(
                "Confirmation does not match the current preview; nothing was changed."
            )
        install = NativeInstall(self)
        if operation == "uninstall":
            install.uninstall()
            return {"status": "uninstalled", "preserves_data_and_archives": True}
        self._require_upgrade_backup(installed)
        result = install.upgrade(expected_manifest=plan["reviewed_manifest"])
        return {"status": "installed", "manifest": result["manifest"]}

    def _require_upgrade_backup(self, installed: dict[str, Any]) -> None:
        """Limit upgrades to runtime-only and independently revalidate the last backup."""
        import tempfile

        from rss_aggregator.archives import validate_archive

        current = self._identity()
        if any(
            installed[key] != current[key]
            for key in ("freshrss_version", "freshrss_sha256", "platform")
        ):
            raise RuntimeFailure(
                "Only PHP/web runtime upgrades of the same FreshRSS release are supported. Preserve data and plan FreshRSS upgrades separately."
            )
        recovery = Recovery(self)
        if recovery.pristine():
            return
        generation = recovery.writer_generation()
        success = service.read(self.state_dir / SHUTDOWN_SUCCESS)
        if (
            not isinstance(success, dict)
            or success.get("instance") != self.instance
            or "writer_generation" not in success
            or success["writer_generation"] != generation
        ):
            raise RuntimeFailure(
                "A verified shutdown archive covering the selected instance's last writer generation is required; fix the backup and retry rss stop."
            )
        archive = Path(success["archive"])
        digest = hashlib.sha256()
        with archive.open("rb") as candidate:
            for block in iter(lambda: candidate.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != success["sha256"]:
            raise RuntimeFailure(
                "The recorded upgrade backup changed; preserve it and create a fresh verified archive."
            )
        with tempfile.TemporaryDirectory(prefix="rss-upgrade-check-") as scratch:
            validate_archive(archive, Path(scratch), runtime=installed)
