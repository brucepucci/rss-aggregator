"""Exercise writer and captured supervision lifetimes across process exits."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryFile

import pytest

from rss_aggregator.archives import create_archive, write_data_tar
from rss_aggregator.native_install import manifest_identity
from rss_aggregator.native_service import approval_path, lease
from tests.test_native_install import CHECKOUT_IMPORT, CLI_IN_CHECKOUT, ROOT, Checkout
from tests.test_native_jobs import LAUNCHCTL, _success, _tool

# The binary boundary is disposable. The web child provides a live readiness
# handshake over local IPC (no IP listeners); the fetch child waits on a gate.
BINARY = """
import json
import os
import signal
import socketserver
import sys
import time
from pathlib import Path

root = Path(__file__).resolve().parents[2] / 'checkout'
control = Path.home()
args = sys.argv[1:]
if args == ['version']:
    print('FrankenPHP v1.14.0 PHP 8.6.0 Caddy v2.11.9')
elif args[:3] == ['php-cli', '-n', '-r']:
    print('8.6.0')
elif args[0] == 'php-cli' and args[1] in ['cli/prepare.php', 'cli/reconfigure.php']:
    operation = Path(args[1]).stem
    if (control / ('hold-' + operation)).exists():
        signal.alarm(60)
        (control / (operation + '-pid')).write_text(str(os.getpid()))
        (control / (operation + '-started')).touch()
        while not (control / (operation + '-release')).exists():
            time.sleep(0.02)
    if operation == 'reconfigure':
        (control / 'base-url').write_text(args[args.index('--base-url') + 1])
elif args[:2] == ['php-cli', '-r']:
    print(json.dumps(['form', False, False, True, (control / 'base-url').read_text(), True, 'sqlite']))
elif args == ['php-cli', 'app/actualize_script.php']:
    if (control / 'next-slot').exists() or (control / 'hold-foreground').exists():
        signal.alarm(60)
        if (control / 'hold-foreground').exists():
            print('synthetic-private-feed', flush=True)
            print('synthetic-private-feed', file=sys.stderr, flush=True)
        (control / 'fetch-pid').write_text(str(os.getpid()))
        (control / 'fetch-started').touch()
        while not (control / 'fetch-release').exists():
            time.sleep(0.02)
        (Path(os.environ['DATA_PATH']) / 'feed-cache').write_text('fetched update\\n')
        (control / 'fetch-finished').touch()
elif args[0] == 'run':
    signal.alarm(60)
    nonce = Path(args[2]).name.removeprefix('Caddyfile-')
    os.chdir(root)

    def stop_web(_number, _frame):
        if (control / 'hold-web').exists():
            # An HTTP request is still draining; the test will release it.
            (control / 'web-draining').touch()
        else:
            raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop_web)

    class Ready(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.sendall(nonce.encode())

    with socketserver.UnixStreamServer('.ready-' + nonce, Ready) as server:
        (control / ('web-pid-' + nonce)).write_text(str(os.getpid()))
        server.timeout = 0.05
        while not (control / ('web-release-' + nonce)).exists():
            server.handle_request()
else:
    raise AssertionError(args)
"""

LAUNCHER = f"""
import json
import os
import runpy
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Import from this disposable checkout just as an editable install would.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
if sys.argv[1:4] == ['-I', '-B', '-c']:
    exec(sys.argv[4])
    raise SystemExit(0)
if sys.argv[1:4] == ['-I', '-m', 'rss_aggregator.native_service']:
    control = Path(sys.argv[4]) / 'service'
    (control / 'controller-pid').write_text(str(os.getpid()))
    sys.argv = sys.argv[3:]
    with (
        patch('time.time', lambda: 1200 if (control / 'next-slot').exists() else 0),
        patch.object(sys, 'executable', __file__),
    ):
        runpy.run_module('rss_aggregator.native_service', run_name='__main__', alter_sys=True)
    raise SystemExit(0)
if sys.argv[1:3] == ['-I', '-B'] and Path(sys.argv[3]).name == 'native_service.py':
    sys.argv = sys.argv[3:]
    control = Path(sys.argv[2]) / 'service'
    monotonic = time.monotonic
    clock = control / 'clock'
    scheduled = sys.argv[1] == 'fetch' and len(sys.argv) > 6

    def caretaker_clock():
        if clock.exists():
            return json.loads(clock.read_text())[1]
        offset = control / 'advance-deadline'
        return monotonic() + (int(offset.read_text()) if offset.exists() else 0)

    class Process(subprocess.Popen):
        def __init__(self, args, **kwargs):
            super().__init__(args, **kwargs)
            if scheduled and clock.exists():
                with (control / 'scheduled-pids').open('a') as history:
                    print(self.pid, file=history)

    with patch('time.monotonic', caretaker_clock), patch.object(subprocess, 'Popen', Process):
        runpy.run_path(sys.argv[0], run_name='__main__')
    raise SystemExit(0)
assert sys.argv[1:5] == ['-I', '-B', '-m', 'rss_aggregator']
sys.argv = ['rss', *sys.argv[5:]]

from rss_aggregator import native_service, runtime

state = Path(os.environ['RSS_STATE_DIR'])
foreground_control = state / 'service/hold-foreground'
foreground_timeout = int(foreground_control.read_text()) if foreground_control.exists() else native_service.FETCH_TIMEOUT

class FreePort:
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass
    def setsockopt(self, *_):
        pass
    def bind(self, address):
        assert address == ('127.0.0.1', 18080)


def web_check(port, path, *, host=None):
    assert port == 18080
    nonce = json.loads((state / 'service.json').read_text())['nonce']
    try:
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(1)
            sock.connect('.ready-' + nonce)
            with sock.makefile('rb') as response:
                body = response.read()
    except (ConnectionError, FileNotFoundError, TimeoutError):
        return 0, b''
    assert body == nonce.encode()
    if path == '/__rss_ready':
        assert host == 'rss-' + nonce + '.invalid'
        return 200, body
    if path == '/__rss_probe.php':
        assert host == 'rss-' + nonce + '.invalid'
        assert (state / 'service/__rss_probe.php').is_file()
        return 200, json.dumps({{
            'sapi': 'frankenphp', 'php': '8.6.0', 'sqlite': True,
            'upload_progress': '0', 'expose_php': '0', 'display_errors': '0',
            'loaded_ini': str(state / 'service/php.ini'),
            'initialized': True, 'authenticated_settings': True,
        }}).encode()
    if path == '/i/':
        return 200, b''
    assert path == '/api/greader.php/reader/api/0/subscription/list'
    return 403, b''

with (
    patch.object(runtime, 'socket', SimpleNamespace(
        socket=FreePort, SOL_SOCKET=socket.SOL_SOCKET, SO_REUSEADDR=socket.SO_REUSEADDR,
    )),
    patch.object(native_service, 'web_check', web_check),
    patch.object(native_service, 'FETCH_TIMEOUT', foreground_timeout),
    patch.object(sys, 'executable', __file__),
):
    exec({CLI_IN_CHECKOUT!r})
"""

CONTROLLER = (
    CHECKOUT_IMPORT
    + """
import runpy
from unittest.mock import patch

slot = Path(sys.argv[1]) / 'service/next-slot'
with patch('time.time', lambda: 1200 if slot.exists() else 0):
    runpy.run_module('rss_aggregator.native_service', run_name='__main__', alter_sys=True)
"""
)


class ProcessCheckout(Checkout):
    @property
    def launcher(self):
        return self.root / ".venv/bin/python"

    @property
    def control(self):
        return self.state / "service"

    def advance_deadline(self, seconds):
        temporary = self.control / "advance-deadline.tmp"
        temporary.write_text(str(seconds))
        temporary.replace(self.control / "advance-deadline")

    def popen(self, *args):
        return subprocess.Popen(
            (self.launcher, "-I", "-B", "-m", "rss_aggregator", *args),
            cwd=self.root,
            env=self.environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def run(self, *args):
        return subprocess.run(
            (self.launcher, "-I", "-B", "-m", "rss_aggregator", *args),
            cwd=self.root,
            env=self.environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )


@pytest.fixture
def process_checkout(tmp_path):
    checkout = ProcessCheckout(tmp_path.resolve())
    # The shebang interpreter runs before the shim sees the CLI's -I/-B
    # arguments. Isolate the disposable launcher and its fake tools alike.
    checkout.environment = {
        key: value
        for key, value in checkout.environment.items()
        if not key.startswith("PYTHON")
    }
    checkout.environment.update(RSS_PORT="18080", PYTHONDONTWRITEBYTECODE="1")
    checkout.prepare_source()
    (checkout.root / "config/security-probe.php").write_bytes(
        (ROOT / "config/security-probe.php").read_bytes()
    )
    checkout._save_download("runtime", f"#!{sys.executable}\n{BINARY}".encode())
    _tool(tmp_path / "launchctl", LAUNCHCTL)
    checkout.launcher.parent.mkdir(parents=True)
    # The shim needs its own path in argv[0], unlike the launchctl -c wrapper.
    checkout.launcher.write_text(f"#!{sys.executable}\n{LAUNCHER}")
    checkout.launcher.chmod(0o700)
    _success(checkout, "setup")

    data = tmp_path / "seed"
    data.mkdir(mode=0o700)
    for name in ("applied_migrations.txt", "config.php", "feed-cache"):
        (data / name).write_text("synthetic reading state\n")
        (data / name).chmod(0o600)
    payload = write_data_tar(data, checkout.backups)
    archive = create_archive(
        payload, checkout.backups, runtime=manifest_identity(checkout.manifest)
    )
    _success(checkout, "restore", archive["archive"], "--instance", "reader")
    _success(checkout, "select-instance", "reader")
    Path(archive["archive"]).unlink()
    return checkout


def test_process_launcher_ignores_ambient_python_options(monkeypatch, request):
    monkeypatch.setenv("PYTHONWARNDEFAULTENCODING", "1")
    monkeypatch.setenv("PYTHONSAFEPATH", "1")
    # Create the fixture only after setting the options inherited by its shim.
    checkout = request.getfixturevalue("process_checkout")
    observed = checkout.run("status")
    assert observed.returncode == 0, observed.stderr
    assert json.loads(observed.stdout)["runtime"] == "installed"
    assert not observed.stderr


def _wait_for(predicate, description):
    deadline = time.monotonic() + 10
    while not predicate():
        assert time.monotonic() < deadline, description
        time.sleep(0.02)


def _exited(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_inflight_fetch_prevents_backup_after_controller_death(process_checkout):
    checkout = process_checkout
    nonce = "a" * 32
    checkout.control.mkdir(mode=0o700)
    plan = checkout.state / "service.json"
    plan.write_text(json.dumps({"nonce": nonce, "instance": "reader", "port": 18080}))
    plan.chmod(0o600)
    approval_path(checkout.control, nonce).touch(mode=0o600)

    with (
        lease(checkout.state / "writers.lock") as writer,
        lease(checkout.state / "controller.lock") as controller,
    ):
        assert writer is not None and controller is not None
        process = subprocess.Popen(
            (
                sys.executable,
                "-I",
                "-B",
                "-c",
                CONTROLLER,
                str(checkout.state),
                str(writer),
                str(controller),
            ),
            cwd=checkout.root,
            env=checkout.environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(writer, controller),
        )
    web_pid = checkout.control / ("web-pid-" + nonce)
    release_web = checkout.control / ("web-release-" + nonce)
    release_fetch = checkout.control / "fetch-release"
    try:
        _wait_for(
            lambda: web_pid.exists() and web_pid.stat().st_size,
            "web writer did not start",
        )
        (checkout.control / "next-slot").touch()
        _wait_for(
            (checkout.control / "fetch-started").exists, "scheduled fetch did not start"
        )
        # Reap the web child first so its lease cannot hide a missing fetch lease.
        release_web.touch()
        pid = int(web_pid.read_text())
        _wait_for(lambda: _exited(pid), "controller did not reap the web writer")
        process.kill()
        assert process.wait(timeout=5) == -signal.SIGKILL

        result = checkout.run("backup")
        assert result.returncode == 1 and not result.stdout, (
            result.stdout,
            result.stderr,
        )
        assert "writer state is unconfirmed" in result.stderr
        assert not list(checkout.backups.glob("*.zip"))
    finally:
        release_fetch.touch()
        release_web.touch()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        _wait_for(
            lambda: _success(checkout, "status")["application"] == "stopped",
            "disposable writer did not exit",
        )

    assert (checkout.control / "fetch-finished").exists()
    saved = _success(checkout, "backup")
    assert saved["verified"] is True
    assert Path(saved["archive"]).is_file()


@pytest.mark.parametrize("damage", ["other_instance", "unreadable"])
def test_live_selection_disagreement_refuses_capture_without_switching(
    process_checkout, damage
):
    checkout = process_checkout
    source = _success(checkout, "backup")["archive"]
    _success(checkout, "restore", source, "--instance", "standby")
    _success(checkout, "select-instance", "standby")
    selection = checkout.state / "selected-instance.json"
    standby = selection.read_bytes()
    _success(checkout, "select-instance", "reader")
    reader = selection.read_bytes()
    _success(checkout, "start")
    plan = checkout.state / "service.json"
    live = plan.read_bytes()
    archives = set(checkout.backups.glob("*.zip"))
    broken_selection = standby if damage == "other_instance" else b"{"
    selection.write_bytes(broken_selection)
    try:
        if damage == "other_instance":
            mismatch = _success(checkout, "status")
            assert mismatch["instance"] == "standby"
            assert mismatch["live_instance"] == "reader"
            assert mismatch["http_ready"] is False
        else:
            assert checkout.run("status").returncode == 1
        for operation in ("fetch", "backup", "verify"):
            result = checkout.run(operation)
            assert result.returncode == 1 and not result.stdout, result.stdout
            assert "selection" in result.stderr or "selected instance" in result.stderr
        assert plan.read_bytes() == live
        assert set(checkout.backups.glob("*.zip")) == archives

        stopped = _success(checkout, "stop")
        assert stopped["backup"]["verified"] is True
        evidence = _success(checkout, "backup-status")
        assert evidence["latest_attempt"]["instance"] == "reader"
        assert evidence["latest_success"]["instance"] == "reader"
        assert evidence["unresolved_shutdowns"] == []
        assert selection.read_bytes() == broken_selection
    finally:
        selection.write_bytes(reader)
        if _success(checkout, "status")["application"] != "stopped":
            _success(checkout, "stop")


def test_scheduled_fetch_deadline_survives_controller_death(process_checkout):
    checkout = process_checkout
    assert _success(checkout, "start")["application"] == "running"
    nonce = json.loads((checkout.state / "service.json").read_text())["nonce"]
    controller = int((checkout.control / "controller-pid").read_text())
    release_web = checkout.control / ("web-release-" + nonce)
    try:
        (checkout.control / "next-slot").touch()
        _wait_for(
            (checkout.control / "fetch-started").exists,
            "scheduled fetch did not start",
        )
        fetch = int((checkout.control / "fetch-pid").read_text())
        os.kill(controller, signal.SIGKILL)
        web = int((checkout.control / ("web-pid-" + nonce)).read_text())
        _wait_for(lambda: _exited(web), "the web caretaker did not reap its child")

        result = checkout.run("backup")
        assert result.returncode == 1 and not result.stdout
        assert "writer state is unconfirmed" in result.stderr
        assert not _exited(fetch)
        assert not list(checkout.backups.glob("*.zip"))

        checkout.advance_deadline(601)
        _wait_for(lambda: _exited(fetch), "the scheduled fetch deadline was lost")
        assert not (checkout.control / "fetch-finished").exists()
        saved = _success(checkout, "backup")
        assert saved["verified"] is True
        assert Path(saved["archive"]).is_file()
    finally:
        release_web.touch()
        (checkout.control / "fetch-release").touch()
        _wait_for(
            lambda: _success(checkout, "status")["application"] == "stopped",
            "disposable writer did not exit",
        )


@pytest.mark.parametrize("generation_matches", [True, False])
def test_stop_reaps_acknowledged_web_after_controller_death(
    process_checkout, generation_matches
):
    checkout = process_checkout
    assert _success(checkout, "start")["application"] == "running"
    assert _success(checkout, "status")["http_ready"] is True
    nonce = json.loads((checkout.state / "service.json").read_text())["nonce"]
    controller = int((checkout.control / "controller-pid").read_text())
    web = int((checkout.control / ("web-pid-" + nonce)).read_text())
    release_web = checkout.control / ("web-release-" + nonce)
    (checkout.control / "hold-web").touch()
    if not generation_matches:
        generation = checkout.state / "instances/reader/writer-generation.json"
        generation.write_text(json.dumps({"writer_generation": None}))
    try:
        os.kill(controller, signal.SIGKILL)
        _wait_for(
            (checkout.control / "web-draining").exists,
            "the approved writer outlived its controller without a shutdown path",
        )
        assert not list(checkout.control.glob("acknowledged-*.json"))
        assert not _exited(web)
        assert _success(checkout, "status")["application"] == "unconfirmed"
        with checkout.popen("stop") as stop:
            try:
                _wait_for(
                    lambda: (checkout.state / "backup-shutdown.json").exists()
                    or stop.poll() is not None,
                    "stop did not reach the shutdown guard",
                )
                assert stop.poll() is None, stop.communicate()
                evidence = _success(checkout, "backup-status")
                assert evidence["busy"] is True
                expected_instance = "reader" if generation_matches else None
                assert evidence["unresolved_shutdown"]["instance"] == expected_instance
                assert not _exited(web)
                assert not list(checkout.backups.glob("*.zip"))
                release_web.touch()
                out, err = stop.communicate(timeout=5)
            finally:
                if stop.poll() is None:
                    stop.kill()
        assert _exited(web), "stop published before the draining writer was reaped"
        evidence = _success(checkout, "backup-status")
        assert evidence["latest_success"]["instance"] == "reader"
        if generation_matches:
            assert stop.returncode == 0, err
            result = json.loads(out)
            assert result["application"] == "stopped"
            assert result["already_stopped"] is False
            assert result["backup"]["verified"] is True
            assert evidence["unresolved_shutdowns"] == []
        else:
            assert stop.returncode == 1 and not out
            assert "shutdown identity could not be confirmed" in err
            assert evidence["unresolved_shutdown"]["instance"] is None
        assert not list(checkout.control.glob("acknowledged-*.json"))
        assert _success(checkout, "status")["http_ready"] is False
    finally:
        release_web.touch()
        _wait_for(
            lambda: _success(checkout, "status")["application"] == "stopped",
            "disposable web did not exit",
        )


def test_stop_waits_for_foreground_fetch_deadline_after_cli_death(process_checkout):
    checkout = process_checkout
    assert _success(checkout, "start")["application"] == "running"
    nonce = json.loads((checkout.state / "service.json").read_text())["nonce"]
    gate = checkout.control / ("acknowledged-" + nonce + ".json")
    release_web = checkout.control / ("web-release-" + nonce)
    (checkout.control / "hold-foreground").write_text("5")
    try:
        with checkout.popen("fetch") as fetch:
            try:
                _wait_for(
                    (checkout.control / "fetch-started").exists,
                    "manual fetch did not start",
                )
                fetch_pid = int((checkout.control / "fetch-pid").read_text())
            finally:
                if fetch.poll() is None:
                    fetch.kill()
            assert fetch.wait(timeout=5) == -signal.SIGKILL

        assert _success(checkout, "status")["http_ready"] is True
        competing = checkout.run("fetch")
        assert competing.returncode == 1 and not competing.stdout
        assert "A feed fetch is already in progress" in competing.stderr

        with checkout.popen("stop") as stop:
            try:
                _wait_for(lambda: not gate.exists(), "stop did not close the HTTP gate")
                assert stop.poll() is None and not _exited(fetch_pid)
                assert not list(checkout.backups.glob("*.zip"))
                out, err = stop.communicate(timeout=10)
            finally:
                if stop.poll() is None:
                    stop.kill()
            assert stop.returncode == 0, err
        assert _exited(fetch_pid), "stop published before the fetch child was reaped"
        assert not (checkout.control / "fetch-finished").exists()
        result = json.loads(out)
        assert result["application"] == "stopped"
        assert result["backup"]["verified"] is True
    finally:
        (checkout.control / "fetch-release").touch()
        release_web.touch()
        _wait_for(
            lambda: _success(checkout, "status")["application"] == "stopped",
            "disposable writers did not exit",
        )


@pytest.mark.parametrize("operation", ["prepare", "reconfigure"])
def test_configuration_remains_excluded_and_bounded_after_start_dies(
    process_checkout, operation
):
    checkout = process_checkout
    if operation == "prepare":
        data = checkout.state / "instances/reader/data"
        for name in ("applied_migrations.txt", "config.php"):
            (data / name).unlink()
    checkout.control.mkdir(mode=0o700)
    (checkout.control / f"hold-{operation}").touch()
    try:
        with checkout.popen("start") as start:
            try:
                _wait_for(
                    (checkout.control / f"{operation}-started").exists,
                    f"{operation} did not start",
                )
                pid = int((checkout.control / f"{operation}-pid").read_text())
            finally:
                if start.poll() is None:
                    start.kill()
            assert start.wait(timeout=5) == -signal.SIGKILL

        status = _success(checkout, "status")
        assert (status["application"], status["http_ready"]) == ("unconfirmed", False)
        result = checkout.run("backup")
        assert result.returncode == 1 and not result.stdout
        assert "writer state is unconfirmed" in result.stderr
        assert not list(checkout.backups.glob("*.zip"))

        with checkout.popen("stop") as stop:
            try:
                _wait_for(
                    lambda: (checkout.state / "backup-shutdown.json").exists()
                    or stop.poll() is not None,
                    "stop did not reach the shutdown guard",
                )
                assert stop.poll() is None and not _exited(pid)
                assert not list(checkout.backups.glob("*.zip"))
                checkout.advance_deadline(121)
                out, err = stop.communicate(timeout=5)
            finally:
                if stop.poll() is None:
                    stop.kill()
            assert _exited(pid), f"{operation} deadline was lost"
            assert stop.returncode == 0, err
            stopped = json.loads(out)
            assert stopped["backup"]["verified"] is True
            report = _success(checkout, "backup-status")
            assert report["latest_success"]["instance"] == "reader"
            assert report["unresolved_shutdowns"] == []
    finally:
        (checkout.control / f"{operation}-release").touch()


def test_startup_fetch_blocks_backup_even_without_a_web_generation(process_checkout):
    checkout = process_checkout
    checkout.control.mkdir(mode=0o700)
    (checkout.control / "hold-foreground").write_text("5")
    try:
        with checkout.popen("start") as start:
            try:
                _wait_for(
                    (checkout.control / "fetch-started").exists,
                    "the startup fetch did not start",
                )
                fetch_pid = int((checkout.control / "fetch-pid").read_text())
            finally:
                if start.poll() is None:
                    start.kill()
            assert start.wait(timeout=5) == -signal.SIGKILL

        status = _success(checkout, "status")
        assert (status["application"], status["http_ready"]) == ("unconfirmed", False)
        result = checkout.run("backup")
        assert result.returncode == 1 and not result.stdout
        assert "writer state is unconfirmed" in result.stderr
        assert not list(checkout.backups.glob("*.zip"))

        stopped = _success(checkout, "stop")
        assert _exited(fetch_pid), "startup fetch deadline was lost"
        assert stopped["backup"]["verified"] is True
        report = _success(checkout, "backup-status")
        assert report["latest_success"]["instance"] == "reader"
        assert report["unresolved_shutdowns"] == []
    finally:
        (checkout.control / "fetch-release").touch()
        _wait_for(
            lambda: _success(checkout, "status")["application"] == "stopped",
            "disposable startup fetch did not exit",
        )


def test_foreground_timeout_reports_safe_error_after_reaping_writer(process_checkout):
    checkout = process_checkout
    assert _success(checkout, "start")["application"] == "running"
    try:
        (checkout.control / "hold-foreground").write_text("2")
        result = checkout.run("fetch")
        assert result.returncode == 1 and not result.stdout
        assert "FreshRSS PHP operation timed out after 2 seconds" in result.stderr
        assert "synthetic-private-feed" not in result.stderr
        assert _exited(int((checkout.control / "fetch-pid").read_text()))
        assert _success(checkout, "status")["http_ready"] is True
    finally:
        (checkout.control / "fetch-release").touch()
        assert _success(checkout, "stop")["application"] == "stopped"


@pytest.mark.parametrize(
    "name", ["writers.lock", "controller.lock", "fetch.lock", "writer-leases.json"]
)
@pytest.mark.parametrize("replaced", [False, True], ids=["missing", "replaced"])
def test_damaged_installed_lease_is_not_idle_evidence(process_checkout, name, replaced):
    checkout = process_checkout
    assert _success(checkout, "start")["application"] == "running"
    initial = _success(checkout, "stop")
    archives = {Path(initial["backup"]["archive"])}
    path = checkout.state / name
    path.rename(checkout.control / "previous-lease")
    if replaced:
        path.touch(mode=0o600)
    assert _success(checkout, "status")["application"] == "unconfirmed"
    for command in ("setup", "backup", "stop"):
        result = checkout.run(command)
        assert result.returncode == 1 and not result.stdout
        assert "unconfirmed" in result.stderr
        assert set(checkout.backups.glob("*.zip")) == archives
        assert path.exists() == replaced, "inspection must never recreate a lease"


def test_independent_held_controller_prevents_idle_backup(process_checkout):
    checkout = process_checkout
    assert _success(checkout, "status")["application"] == "stopped"
    with lease(checkout.state / "controller.lock") as controller:
        assert controller is not None
        assert _success(checkout, "status")["application"] == "unconfirmed"
        for command in ("backup", "stop"):
            result = checkout.run(command)
            assert result.returncode == 1 and not result.stdout
            assert "unconfirmed" in result.stderr
            assert not list(checkout.backups.glob("*.zip"))


def _run_captured_job(definition):
    root = definition["WorkingDirectory"]
    # File capture lets us observe job-parent exit independently of descendant I/O.
    with (
        TemporaryFile(mode="w+", dir=root) as output,
        TemporaryFile(mode="w+", dir=root) as errors,
        subprocess.Popen(
            definition["ProgramArguments"],
            cwd=root,
            env=definition["EnvironmentVariables"],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=errors,
            start_new_session=True,
            umask=definition["Umask"],
        ) as job,
    ):
        try:
            code = job.wait(timeout=20)
            errors.seek(0)
            assert code == 0, errors.read()
        finally:
            if job.poll() is None:
                job.kill()
                job.wait(timeout=5)
            # Model the scoped cleanup that launchd would otherwise apply at exit.
            if not definition.get("AbandonProcessGroup", False):
                try:
                    os.killpg(job.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        output.seek(0)
        return json.load(output)


def test_supervision_recovery_survives_captured_job_exit_and_healthy_tick(
    process_checkout,
):
    checkout = process_checkout
    plan = checkout.state / "service.json"

    def generation():
        return json.loads(plan.read_text())["nonce"]

    def ready():
        status = _success(checkout, "status")
        assert status["application"] == "running"
        assert status["http_ready"] is True
        assert status["scheduler"]["running"] is True

    try:
        _success(checkout, "start")
        original = generation()
        path = Path(_success(checkout, "supervision-install")["job"])
        definition = json.loads(
            (
                Path(checkout.environment["HOME"]) / "fake-launchd" / path.stem
            ).read_text()
        )["definition"]
        # Simulate an unexpected web exit, not an operator stop; running intent remains.
        (checkout.control / ("web-release-" + original)).touch()
        _wait_for(
            lambda: _success(checkout, "status")["application"] == "stopped",
            "initial service did not exit",
        )

        recovered = _run_captured_job(definition)
        assert recovered["state"] == "recovered"
        assert recovered["recovery"]["history"][-1]["outcome"] == "recovered"
        ready()  # This fresh command runs only after the captured job exits.
        current = generation()
        assert current != original

        assert _run_captured_job(definition)["state"] == "healthy"
        ready()
        assert generation() == current
    finally:
        if plan.exists():
            (checkout.control / ("web-release-" + generation())).touch()
            _wait_for(
                lambda: _success(checkout, "status")["application"] == "stopped",
                "recovered service did not exit after release",
            )
