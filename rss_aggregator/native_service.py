"""Supervise native web and feed writers with inherited kernel leases."""

import fcntl
import json
import os
import re
import select
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.client import RemoteDisconnected
from pathlib import Path
from typing import Any, BinaryIO
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

PERIOD = 20 * 60
FETCH_TIMEOUT = 600
FETCH_TIMED_OUT = 2
_FETCH_DRAIN_GRACE = 120
_FETCH_TERM_GRACE = 10
WRITER_LEASES = ("writers.lock", "controller.lock", "fetch.lock")
WRITER_LEASE_RECORD = "writer-leases.json"
INI = """\
session.upload_progress.enabled = Off
expose_php = Off
display_errors = Off
log_errors = On
cgi.fix_pathinfo = 0
opcache.enable = 0
max_execution_time = 120
memory_limit = 256M
post_max_size = 32M
upload_max_filesize = 24M
variables_order = 'EGPCS'
date.timezone = UTC
"""


class _NoRedirect(HTTPRedirectHandler):
    """Never follow redirects outside the local diagnostic endpoint."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        """Expose the redirect itself to the caller."""
        return None


def web_check(port: int, path: str, *, host: str | None = None) -> tuple[int, bytes]:
    """Read a bounded loopback response without ambient proxy settings.

    Report transport loss as an unsuccessful probe so startup can retry it.
    """
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    request = Request(
        f"http://127.0.0.1:{port}{path}", headers={"Host": host} if host else {}
    )
    try:
        try:
            response = opener.open(request, timeout=2)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.read(128 * 1024)
    except (URLError, TimeoutError, RemoteDisconnected, ConnectionResetError):
        return 0, b""


def save(target: Path, value: Any) -> None:
    """Persist a private complete record across crashes and readers."""
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, target)
        fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        Path(name).unlink(missing_ok=True)


def read(target: Path) -> Any:
    """Read a record; only genuine absence counts as missing."""
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as record:
        info = os.fstat(record.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
        ):
            raise ValueError("Unexpected managed state file; inspect local ownership.")
        return json.load(record)


def _lease_identity(info: os.stat_result, expected: tuple[int, int] | None) -> None:
    """Reject foreign lease files and check a recorded file when one is expected."""
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or (
            expected is not None
            and (
                stat.S_IMODE(info.st_mode) != 0o600
                or (info.st_dev, info.st_ino) != expected
            )
        )
    ):
        raise ValueError("Unexpected managed lease file; inspect local ownership.")


@contextmanager
def lease(
    path: Path, *, expected: tuple[int, int] | None = None
) -> Iterator[int | None]:
    """Hold an inherited flock without creating an already registered lease."""
    flags = os.O_NOFOLLOW | os.O_RDWR | os.O_NONBLOCK
    if expected is None:
        flags |= os.O_CREAT
    else:
        _lease_identity(path.lstat(), expected)
    fd = os.open(path, flags, 0o600)
    try:
        _lease_identity(os.fstat(fd), expected)
        locked = False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError:
            pass
        if expected is not None:
            # Never infer a lock result for a different file installed during open.
            _lease_identity(path.lstat(), expected)
        yield fd if locked else None
    finally:
        # The descriptor may be shared with descendants; do not explicitly unlock it.
        os.close(fd)


def stage_writer_leases(staging: Path) -> None:
    """Prepare stable lease files and their identities for first setup publication."""
    identities = {}
    for name in WRITER_LEASES:
        fd = os.open(staging / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fchmod(fd, 0o600)
            identities[name] = os.fstat(fd).st_ino
            os.fsync(fd)
        finally:
            os.close(fd)
    save(staging / WRITER_LEASE_RECORD, identities)


class WriterLeases:
    """Inspect installed writer leases independently of generation/PID metadata."""

    def __init__(self, state: Path, *, names: tuple[str, ...] = WRITER_LEASES) -> None:
        """Require a complete record and validate the requested private paths.

        Capture uses the default complete set. A restricted set only proves the
        named leases, for example when requesting an identified controller to stop.
        """
        self.state = state
        record = read(state / WRITER_LEASE_RECORD)
        if (
            not isinstance(record, dict)
            or record.keys() != set(WRITER_LEASES)
            or not set(names) <= record.keys()
        ):
            raise ValueError(
                "Native writer lease identity record is missing or invalid."
            )
        # Device IDs may change on reboot or remount. Check that each lease is
        # on the *current* state device while requiring the installed inode.
        device = state.stat().st_dev
        self.identities: dict[str, tuple[int, int]] = {}
        for name in names:
            inode = record[name]
            if type(inode) is not int or inode <= 0:
                raise ValueError("Invalid native writer lease identity.")
            expected = (device, inode)
            _lease_identity((state / name).lstat(), expected)
            self.identities[name] = expected

    @contextmanager
    def hold(self, name: str) -> Iterator[int | None]:
        """Open a registered lease without creating or accepting a replacement."""
        with lease(self.state / name, expected=self.identities[name]) as fd:
            yield fd


def environment(state: Path, data: Path) -> dict[str, str]:
    """Set an explicit runtime environment without carrying interactive secrets."""
    store = state / "service"
    return {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(store),
        "TMPDIR": str(store),
        "XDG_CONFIG_HOME": str(store),
        "XDG_DATA_HOME": str(store),
        "PHPRC": str(store / "php.ini"),
        "PHP_INI_SCAN_DIR": "",
        "DATA_PATH": str(data),
        "TZ": "UTC",
        "TRUSTED_PROXY": "0",
    }


def initialized(data: Path) -> bool:
    """Distinguish the FreshRSS installer from a partially damaged data directory."""
    complete = (data / "applied_migrations.txt").is_file()
    if complete != (data / "config.php").is_file():
        raise ValueError(
            "FreshRSS configuration is incomplete; inspect the selected data locally."
        )
    return complete


@contextmanager
def startup_writer_claim(state: Path, identity: dict[str, Any]) -> Iterator[int]:
    """Publish attribution on the same locked file inherited by a startup writer."""
    fd, temporary = tempfile.mkstemp(dir=state, prefix=".startup-writer-")
    with os.fdopen(fd, "w") as claim:
        try:
            os.fchmod(fd, 0o600)
            json.dump(identity, claim)
            claim.flush()
            os.fsync(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.replace(temporary, state / "startup-writer.json")
            # Never unlink the published claim on parent exit. A detached child
            # may still be draining. After unlock the record is inert.
            yield fd
        finally:
            Path(temporary).unlink(missing_ok=True)


def draining_startup_writer(state: Path) -> dict[str, Any] | None:
    """Read startup attribution only from the actively locked published file."""
    path = state / "startup-writer.json"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as stream:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            current = path.lstat()
            if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
                identity = json.load(stream)
                if (
                    isinstance(identity, dict)
                    and identity.keys() == {"instance", "owner", "writer_generation"}
                    and all(isinstance(value, str) for value in identity.values())
                    and re.fullmatch(r"[0-9a-f]{32}", identity["writer_generation"])
                ):
                    return identity
        return None


def approval_path(home: Path, nonce: str) -> Path:
    """Locate the parent's request to approve this generation, outside its web root."""
    return home / f"approved-{nonce}.json"


def acknowledgement_path(home: Path, nonce: str) -> Path:
    """Locate the controller's approval acknowledgement that opens ordinary HTTP."""
    return home / f"acknowledged-{nonce}.json"


def _caddyfile(plan: dict[str, Any]) -> str:
    """Gate ordinary traffic until verified, allowing generation-bound diagnostics."""
    port, nonce = plan["port"], plan["nonce"]
    approval = acknowledgement_path(Path("../../service"), nonce)
    return f"""\
{{
    admin off
    persist_config off
    auto_https off
    frankenphp
}}
http://:{port} {{
    bind 127.0.0.1
    @probe {{
        host rss-{nonce}.invalid
        path /__rss_probe.php
    }}
    handle @probe {{
        root * ../../service
        php_server
    }}
    @ready {{
        host rss-{nonce}.invalid
        path /__rss_ready
    }}
    handle @ready {{
        respond "{nonce}" 200
    }}
    @private path_regexp internal (^|/)[.]
    handle @private {{
        respond 404
    }}
    handle /Web.config {{
        respond 404
    }}
    handle {{
        @unapproved {{
            not {{
                host rss-{nonce}.invalid
                method GET
                path /i/ /api/greader.php/reader/api/0/subscription/list
            }}
            not {{
                file {{
                    root {approval.parent}
                    try_files /{approval.name}
                }}
            }}
        }}
        route {{
            respond @unapproved 503
            root * ./p
            php_server
        }}
    }}
}}
"""


def _kill_and_reap(process: subprocess.Popen[bytes]) -> None:
    """Force and confirm exit of an owned direct child, not a recorded PID."""
    process.kill()
    process.wait()


def _caretaker(
    mode: str,
    *args: str,
    fds: tuple[int, ...],
    stdout: int | BinaryIO = subprocess.DEVNULL,
) -> tuple[subprocess.Popen[bytes], int]:
    """Start a detached writer parent and return the caller's sole pipe write end."""
    listening, owner = os.pipe()
    try:
        return subprocess.Popen(
            [sys.executable, "-I", "-B", str(Path(__file__).resolve()), mode, *args],
            stdin=listening,
            stdout=stdout,
            stderr=subprocess.DEVNULL,
            pass_fds=fds,
            start_new_session=True,
        ), owner
    except BaseException:
        os.close(owner)
        raise
    finally:
        os.close(listening)


def _owner_gone(timeout: float) -> bool:
    """Wait for EOF on the private stdin pipe, on which the caller never writes."""
    return bool(select.select([0], [], [], timeout)[0])


def _watch_web(state: Path, nonce: str, instance: str, writer_fd: int) -> None:
    """Close this generation's gate and reap its web writer when the controller exits."""
    home = state / "service"
    config = home / ("Caddyfile-" + nonce)
    web = None
    try:
        if _owner_gone(0):
            return
        web = subprocess.Popen(
            [
                str(state / "runtime/frankenphp"),
                "run",
                "--config",
                str(config),
                "--adapter",
                "caddyfile",
            ],
            cwd=state / "runtime/FreshRSS",
            env=environment(state, state / "instances" / instance / "data"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(writer_fd,),
        )
        while web.poll() is None and not _owner_gone(0.25):
            pass
    finally:
        try:
            acknowledgement_path(home, nonce).unlink(missing_ok=True)
        finally:
            if web is not None and web.poll() is None:
                try:
                    web.terminate()
                    web.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    _kill_and_reap(web)
        config.unlink(missing_ok=True)
        approval_path(home, nonce).unlink(missing_ok=True)


def _drain(
    web: subprocess.Popen[bytes] | None, fetch: subprocess.Popen[bytes] | None
) -> str | None:
    """Reap caretakers after closing their owner pipes, never killing their timers."""
    if web is not None:
        web.wait()
    if fetch is not None:
        return "completed" if fetch.wait() == 0 else "failed"
    return None


def _start_fetch(
    state: Path, data: Path, fetch_fd: int, deadline: float, *writer_fds: int
) -> tuple[subprocess.Popen[bytes], int]:
    """Start the independent fetch deadline with the caller's writer leases only."""
    return _caretaker(
        "fetch",
        str(state),
        str(data),
        str(fetch_fd),
        str(deadline),
        *(str(fd) for fd in writer_fds),
        fds=(fetch_fd, *writer_fds),
    )


def foreground_fetch(
    state: Path, data: Path, fetch_fd: int, timeout: int, *, claim_fd: int | None = None
) -> int:
    """Await a detached fetch, carrying attribution only for standalone startup."""
    claims = () if claim_fd is None else (claim_fd,)
    fetch, owner = _start_fetch(
        state, data, fetch_fd, time.monotonic() + timeout, *claims
    )
    try:
        return fetch.wait()
    finally:
        os.close(owner)


def foreground_php(
    state: Path,
    data: Path,
    writer_fd: int,
    timeout: int,
    args: tuple[str, ...],
    *,
    claim_fd: int,
    output: int | BinaryIO = subprocess.DEVNULL,
) -> int:
    """Wait for independent preparation or configuration under its writer leases."""
    operation, owner = _caretaker(
        "php",
        str(state),
        str(data),
        str(writer_fd),
        str(time.monotonic() + timeout),
        str(claim_fd),
        *args,
        fds=(writer_fd, claim_fd),
        stdout=output,
    )
    try:
        return operation.wait()
    finally:
        os.close(owner)


def _watch_php(
    state: Path,
    data: Path,
    deadline: float,
    fds: tuple[int, ...],
    args: tuple[str, ...],
    *,
    stdout: int = subprocess.DEVNULL,
) -> int:
    """Enforce the original deadline, also bounding the drain after caller loss."""
    if time.monotonic() >= deadline:
        return FETCH_TIMED_OUT
    if _owner_gone(0):
        return 1
    writer = subprocess.Popen(
        [str(state / "runtime/frankenphp"), "php-cli", *args],
        cwd=state / "runtime/FreshRSS",
        env=environment(state, data),
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=subprocess.DEVNULL,
        pass_fds=fds,
    )
    orphan_term_at = None
    terminated = False
    try:
        while (code := writer.poll()) is None:
            now = time.monotonic()
            if now >= deadline:
                return FETCH_TIMED_OUT
            if orphan_term_at is None:
                if _owner_gone(min(0.25, deadline - now)):
                    orphan_term_at = time.monotonic() + _FETCH_DRAIN_GRACE
                    deadline = min(deadline, orphan_term_at + _FETCH_TERM_GRACE)
            else:
                if now >= orphan_term_at and not terminated:
                    writer.terminate()
                    terminated = True
                time.sleep(min(0.25, deadline - now))
        return int(code != 0)
    finally:
        if writer.poll() is None:
            _kill_and_reap(writer)


def _scheduled(now: float, last_slot: int) -> int | None:
    """Return a newly due UTC slot, skipping missed slots rather than replaying."""
    slot = int(now) // PERIOD
    return slot if slot > last_slot else None


def serve(state: Path, writer_fd: int) -> None:
    """Control the web generation and run its scheduled feed fetch children."""
    plan = read(state / "service.json")
    if not plan or not re.fullmatch(r"[a-f0-9]{32}", plan["nonce"]):
        raise ValueError("Native service generation changed before startup.")
    nonce = plan["nonce"]
    home = state / "service"
    data = state / "instances" / plan["instance"] / "data"
    config = home / ("Caddyfile-" + nonce)
    receipt = state / "service-status.json"
    request = state / "stop-service.json"
    slot = int(time.time()) // PERIOD
    status = {
        "nonce": nonce,
        "web": "starting",
        "scheduler": "starting",
        "last_fetch": None,
    }
    web = fetch = None
    web_owner = fetch_owner = None
    stopping = False
    approved = False
    acknowledgement = acknowledgement_path(home, nonce)
    approval_deadline = time.monotonic() + 30
    try:
        with config.open("x") as stream:
            stream.write(_caddyfile(plan))
        web, web_owner = _caretaker(
            "web", str(state), nonce, plan["instance"], str(writer_fd), fds=(writer_fd,)
        )
        save(receipt, {**status, "web": "started", "scheduler": "running"})
        while web.poll() is None:
            stop = read(request)
            if stop is not None and stop["nonce"] == nonce:
                stopping = True
                break
            if fetch is not None and (code := fetch.poll()) is not None:
                os.close(fetch_owner)
                fetch = fetch_owner = None
                if code == FETCH_TIMED_OUT:
                    # Do not replay a slot reached while the timed-out writer
                    # was still being stopped (including wall-clock jumps).
                    slot = max(slot, int(time.time()) // PERIOD)
                status["last_fetch"] = "completed" if code == 0 else "failed"
                save(receipt, {**status, "web": "started", "scheduler": "running"})
            if not approved:
                if time.monotonic() >= approval_deadline:
                    break
                if approval_path(home, nonce).is_file():
                    save(acknowledgement, {"nonce": nonce})
                    approved = True
            due = _scheduled(time.time(), slot)
            if due is not None:
                slot = due
                if approved and fetch is None and initialized(data):
                    with WriterLeases(state).hold("fetch.lock") as fetch_fd:
                        if fetch_fd is not None:
                            fetch, fetch_owner = _start_fetch(
                                state,
                                data,
                                fetch_fd,
                                time.monotonic() + FETCH_TIMEOUT,
                                writer_fd,
                            )
            time.sleep(0.25)
    finally:
        # Close the ordinary HTTP gate before draining existing writers.
        try:
            acknowledgement.unlink(missing_ok=True)
        finally:
            if web_owner is not None:
                os.close(web_owner)
            if fetch_owner is not None:
                os.close(fetch_owner)
            if (outcome := _drain(web, fetch)) is not None:
                status["last_fetch"] = outcome
        config.unlink(missing_ok=True)
        approval_path(home, nonce).unlink(missing_ok=True)
        save(
            receipt,
            {
                **status,
                "web": "stopped" if stopping else "failed",
                "scheduler": "stopped",
            },
        )


if __name__ == "__main__":
    os.umask(0o077)
    if sys.argv[1] == "web":
        _watch_web(Path(sys.argv[2]), sys.argv[3], sys.argv[4], int(sys.argv[5]))
    elif sys.argv[1] in ("fetch", "php"):
        fetch = sys.argv[1] == "fetch"
        sys.exit(
            _watch_php(
                Path(sys.argv[2]),
                Path(sys.argv[3]),
                float(sys.argv[5]),
                (int(sys.argv[4]), *map(int, sys.argv[6:] if fetch else sys.argv[6:7])),
                ("app/actualize_script.php",) if fetch else tuple(sys.argv[7:]),
                stdout=subprocess.DEVNULL if fetch else 1,
            )
        )
    else:
        try:
            serve(Path(sys.argv[1]), int(sys.argv[2]))
        finally:
            # Controller-only lease; never inherited by children.
            os.close(int(sys.argv[3]))
