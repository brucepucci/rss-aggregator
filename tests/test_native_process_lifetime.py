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
from tests.test_native_install import CLI_IN_CHECKOUT, ROOT, Checkout
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
elif args[:2] == ['php-cli', 'cli/reconfigure.php']:
    (control / 'base-url').write_text(args[args.index('--base-url') + 1])
elif args[:2] == ['php-cli', '-r']:
    print(json.dumps(['form', False, False, True, (control / 'base-url').read_text(), True, 'sqlite']))
elif args == ['php-cli', 'app/actualize_script.php']:
    if (control / 'next-slot').exists():
        signal.alarm(60)
        (control / 'fetch-started').touch()
        while not (control / 'fetch-release').exists():
            time.sleep(0.02)
        (Path(os.environ['DATA_PATH']) / 'feed-cache').write_text('fetched update\\n')
        (control / 'fetch-finished').touch()
elif args[0] == 'run':
    signal.alarm(60)
    nonce = Path(args[2]).name.removeprefix('Caddyfile-')
    os.chdir(root)

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
import sys
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
    sys.argv = sys.argv[3:]
    runpy.run_module('rss_aggregator.native_service', run_name='__main__', alter_sys=True)
    raise SystemExit(0)
assert sys.argv[1:5] == ['-I', '-B', '-m', 'rss_aggregator']
sys.argv = ['rss', *sys.argv[5:]]

from rss_aggregator import native_service, runtime

state = Path(os.environ['RSS_STATE_DIR'])

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
    patch.object(sys, 'executable', __file__),
):
    exec({CLI_IN_CHECKOUT!r})
"""

CONTROLLER = """
import runpy
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path.cwd()))
slot = Path(sys.argv[1]) / 'service/next-slot'
with patch('time.time', lambda: 1200 if slot.exists() else 0):
    runpy.run_module('rss_aggregator.native_service', run_name='__main__', alter_sys=True)
"""


class ProcessCheckout(Checkout):
    @property
    def launcher(self):
        return self.root / ".venv/bin/python"

    @property
    def control(self):
        return self.state / "service"

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
    checkout.environment.pop("PYTHONHOME", None)
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
