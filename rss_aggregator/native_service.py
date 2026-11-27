"""Supervise native web and feed writers with inherited kernel leases."""

import fcntl
import json
import os
import re
import select
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.client import RemoteDisconnected
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

PERIOD = 20 * 60
FETCH_TIMEOUT = 600
FETCH_TIMED_OUT = 2
_FETCH_DRAIN_GRACE = 120
_FETCH_TERM_GRACE = 10
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
    import tempfile

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


@contextmanager
def lease(path: Path) -> Iterator[int | None]:
    """Hold a nonblocking, inherited flock; close instead of explicitly unlocking."""
    fd = os.open(path, os.O_NOFOLLOW | os.O_CREAT | os.O_RDWR | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
        ):
            raise ValueError("Unexpected managed lease file; inspect local ownership.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
        else:
            yield fd
    finally:
        os.close(fd)


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
    mode: str, *args: str, fds: tuple[int, ...]
) -> tuple[subprocess.Popen[bytes], int]:
    """Start a detached writer parent and return the caller's sole pipe write end."""
    listening, owner = os.pipe()
    try:
        return subprocess.Popen(
            [sys.executable, "-I", "-B", str(Path(__file__).resolve()), mode, *args],
            stdin=listening,
            stdout=subprocess.DEVNULL,
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
    web: subprocess.Popen[bytes] | None,
    fetch: subprocess.Popen[bytes] | None,
    fetch_deadline: float,
) -> str | None:
    """Await the web caretaker and drain fetching without pausing its deadline."""
    now = time.monotonic()
    fetch_term_at = now + _FETCH_DRAIN_GRACE
    fetch_stop_at = min(fetch_term_at + _FETCH_TERM_GRACE, fetch_deadline)
    fetch_term_sent = False
    last_fetch = None
    while web is not None or fetch is not None:
        now = time.monotonic()
        if fetch is not None:
            if (code := fetch.poll()) is not None:
                last_fetch = "completed" if code == 0 else "failed"
                fetch = None
            elif now >= fetch_stop_at:
                _kill_and_reap(fetch)
                last_fetch = "failed"
                fetch = None
            elif now >= fetch_term_at and not fetch_term_sent:
                fetch.terminate()
                fetch_term_sent = True
        if web is not None and web.poll() is not None:
            web = None
        if web is not None or fetch is not None:
            time.sleep(0.25)
    return last_fetch


def _fetch(
    binary: Path, source: Path, env: dict[str, str], *fds: int
) -> subprocess.Popen[bytes]:
    """Start upstream feed fetching with all serialization leases inherited."""
    return subprocess.Popen(
        [str(binary), "php-cli", "app/actualize_script.php"],
        cwd=source,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        pass_fds=fds,
    )


def foreground_fetch(state: Path, data: Path, fetch_fd: int, timeout: int) -> int:
    """Await a detached fetch caretaker without giving it the caller's command lock."""
    deadline = time.monotonic() + timeout
    fetch, owner = _caretaker(
        "fetch", str(state), str(data), str(fetch_fd), str(deadline), fds=(fetch_fd,)
    )
    try:
        return fetch.wait()
    finally:
        os.close(owner)


def _watch_fetch(state: Path, data: Path, fetch_fd: int, deadline: float) -> int:
    """Enforce the original deadline, also bounding the drain after caller loss."""
    if time.monotonic() >= deadline:
        return FETCH_TIMED_OUT
    if _owner_gone(0):
        return 1
    fetch = _fetch(
        state / "runtime/frankenphp",
        state / "runtime/FreshRSS",
        environment(state, data),
        fetch_fd,
    )
    orphan_term_at = None
    terminated = False
    try:
        while (code := fetch.poll()) is None:
            now = time.monotonic()
            if now >= deadline:
                return FETCH_TIMED_OUT
            if orphan_term_at is None:
                if _owner_gone(min(0.25, deadline - now)):
                    orphan_term_at = time.monotonic() + _FETCH_DRAIN_GRACE
                    deadline = min(deadline, orphan_term_at + _FETCH_TERM_GRACE)
            else:
                if now >= orphan_term_at and not terminated:
                    fetch.terminate()
                    terminated = True
                time.sleep(min(0.25, deadline - now))
        return int(code != 0)
    finally:
        if fetch.poll() is None:
            _kill_and_reap(fetch)


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
    source = state / "runtime/FreshRSS"
    binary = state / "runtime/frankenphp"
    data = state / "instances" / plan["instance"] / "data"
    env = environment(state, data)
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
    web_owner = None
    fetch_deadline = 0.0
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
            if fetch is not None:
                outcome = None
                if (code := fetch.poll()) is not None:
                    outcome = "completed" if code == 0 else "failed"
                elif time.monotonic() >= fetch_deadline:
                    _kill_and_reap(fetch)
                    outcome = "failed"
                    # Do not replay a slot reached while the timed-out writer
                    # was still being stopped (including wall-clock jumps).
                    slot = max(slot, int(time.time()) // PERIOD)
                if outcome is not None:
                    status["last_fetch"] = outcome
                    save(receipt, {**status, "web": "started", "scheduler": "running"})
                    fetch = None
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
                    with lease(state / "fetch.lock") as fetch_fd:
                        if fetch_fd is not None:
                            fetch_deadline = time.monotonic() + FETCH_TIMEOUT
                            fetch = _fetch(binary, source, env, writer_fd, fetch_fd)
            time.sleep(0.25)
    finally:
        # Close the ordinary HTTP gate before draining existing writers.
        try:
            acknowledgement.unlink(missing_ok=True)
        finally:
            if web_owner is not None:
                os.close(web_owner)
            if (outcome := _drain(web, fetch, fetch_deadline)) is not None:
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
    elif sys.argv[1] == "fetch":
        sys.exit(
            _watch_fetch(
                Path(sys.argv[2]),
                Path(sys.argv[3]),
                int(sys.argv[4]),
                float(sys.argv[5]),
            )
        )
    else:
        try:
            serve(Path(sys.argv[1]), int(sys.argv[2]))
        finally:
            # Controller-only lease; never inherited by children.
            os.close(int(sys.argv[3]))
