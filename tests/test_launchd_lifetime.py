"""Execute captured jobs before modelling launchd's documented exit cleanup."""

import errno
import hashlib
import json
import os
import plistlib
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest

from rss_aggregator import RuntimeFailure
from tests.test_supervised_backups import NOW
from tests.test_supervised_backups import combined as combined
from tests.test_supervision import invoke, registration
from tests.test_supervision import supervised as supervised


@contextmanager
def native_vm_service(command, tmp_path):
    """Launch a finite HTTP helper in the job's group, without inheriting its lease."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    ready = tmp_path / "helper-ready"
    release = tmp_path / "helper-release"
    ended = tmp_path / "helper-ended"
    activate = tmp_path / "helper-activate"
    helper = tmp_path / "native-service.py"
    helper.write_text(
        "from http.server import HTTPServer, BaseHTTPRequestHandler\n"
        "from pathlib import Path\nimport os, time\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        " def do_GET(self): self.send_response(200); self.end_headers()\n"
        " def log_message(self, *args): pass\n"
        f"Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        "deadline = time.monotonic() + 30\n"
        f"while not Path({str(activate)!r}).exists() and not Path({str(release)!r}).exists() and time.monotonic() < deadline: time.sleep(0.01)\n"
        f"server = HTTPServer(('127.0.0.1', {port}), Handler)\n"
        "server.timeout = 0.05\n"
        f"while not Path({str(release)!r}).exists() and time.monotonic() < deadline:\n"
        " server.handle_request()\n"
        "server.server_close()\n"
        f"Path({str(ended)!r}).touch()\n"
    )
    tool = command.bin / "podman"
    shebang, original = tool.read_text().split("\n", 1)
    tool.write_text(
        shebang
        + "\nimport subprocess, sys, time\nfrom pathlib import Path\n"
        + "if sys.argv[1:3] == ['machine', 'start']:\n"
        + f" subprocess.Popen([sys.executable, {str(helper)!r}], close_fds=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        + " deadline = time.monotonic() + 5\n"
        + f" while not Path({str(ready)!r}).exists():\n"
        + "  assert time.monotonic() < deadline\n  time.sleep(0.01)\n"
        + "if sys.argv[1:5] == ['--connection', 'rss-aggregator', 'start', 'rss-aggregator-freshrss']:\n"
        + f" Path({str(activate)!r}).touch()\n"
        + "if sys.argv[1:3] == ['machine', 'stop']:\n"
        + f" Path({str(release)!r}).touch()\n"
        + original
    )
    try:
        yield port, ready
    finally:
        release.touch()
        if ready.exists():
            deadline = time.monotonic() + 5
            while not ended.exists():
                try:
                    os.kill(int(ready.read_text()), 0)
                except ProcessLookupError:
                    break
                assert time.monotonic() < deadline, "Disposable helper did not exit"
                time.sleep(0.01)


def run_captured_job(job, *, interrupt=None, definition=None):
    """Execute the loaded snapshot when supplied, not a possibly changed disk file."""
    if definition is None:
        definition = plistlib.loads(job.read_bytes())
    # A surviving native export can retain stdout/stderr as well as its lease.
    # Files allow observing parent exit without waiting for descendant pipe EOF.
    output = job.with_suffix(".stdout")
    errors = job.with_suffix(".stderr")
    with output.open("w") as stdout, errors.open("w") as stderr:
        child = subprocess.Popen(
            definition["ProgramArguments"],
            cwd=definition["WorkingDirectory"],
            env=definition["EnvironmentVariables"],
            start_new_session=True,
            stdout=stdout,
            stderr=stderr,
            text=True,
        )
    try:
        if interrupt is not None:
            deadline = time.monotonic() + 10
            while not interrupt.exists():
                assert child.poll() is None, errors.read_text()
                assert time.monotonic() < deadline
                time.sleep(0.01)
            child.kill()
            assert child.wait(timeout=5) == -signal.SIGKILL
            return {"interrupted": True}
        assert child.wait(timeout=15) == 0, errors.read_text()
        return json.loads(output.read_text())
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        # launchd's default kills remaining members after the job parent exits.
        # This PGID belongs exclusively to this disposable invocation.
        if not definition.get("AbandonProcessGroup", False):
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def available(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


@pytest.mark.parametrize("missing_loaded_policy", [None, "legacy", "fixed", "unloaded"])
def test_recovery_remains_available_after_captured_job_exits(
    request, tmp_path, missing_loaded_policy
):
    command, engine = request.getfixturevalue("supervised")
    assert invoke(command, "supervision-remove").returncode == 0
    with native_vm_service(command, tmp_path) as (port, ready):
        command.env["RSS_PORT"] = str(port)
        state = json.loads(engine.read_text())
        state["machine"]["State"] = "stopped"
        state["app"]["State"] = {"Running": False, "Status": "exited"}
        state["app"]["HostConfig"]["PortBindings"]["80/tcp"][0]["HostPort"] = str(port)
        engine.write_text(json.dumps(state))
        installed = invoke(command, "supervision-install")
        assert installed.returncode == 0, installed.stderr
        job = Path(json.loads(installed.stdout)["job"])
        loaded_path = command.state / "loaded-job.json"
        if missing_loaded_policy == "unloaded":
            job.unlink()
            loaded_path.unlink()
            installed = invoke(command, "supervision-install")
            assert installed.returncode == 0, installed.stderr
        elif missing_loaded_policy is not None:
            loaded = json.loads(loaded_path.read_text())
            if missing_loaded_policy == "legacy":
                del loaded["definition"]["AbandonProcessGroup"]
            loaded_path.write_text(json.dumps(loaded))
            job.unlink()
            before_loaded = loaded_path.read_bytes()
            ledger = command.state / "supervision.json"
            before_ledger = ledger.read_bytes()
            refused = invoke(command, "supervision-install")
            assert refused.returncode == 1, (
                "Must not silently retain a loaded job's old policy"
            )
            assert "supervision-remove" in refused.stderr
            assert not job.exists()
            assert loaded_path.read_bytes() == before_loaded
            assert ledger.read_bytes() == before_ledger
            assert json.loads(engine.read_text())["app"] == state["app"]
            assert command.run("supervision-tick").returncode == 1
            removed = invoke(command, "supervision-remove")
            assert removed.returncode == 0, removed.stderr
            assert not loaded_path.exists()
            installed = invoke(command, "supervision-install")
            assert installed.returncode == 0, installed.stderr
            assert ledger.read_bytes() == before_ledger
        definition = json.loads(loaded_path.read_text())["definition"]
        result = run_captured_job(job, definition=definition)
        assert result["state"] == "recovered"
        assert ready.exists()
        # Parent exit and the cleanup policy have already happened. In-job health
        # and a zero parent exit alone would pass even with the original defect.
        assert available(port), "Recovered service died after the job exited"
        before = json.loads(engine.read_text())
        for _ in range(2):
            assert run_captured_job(job, definition=definition)["state"] == "healthy"
            assert available(port)
        after = json.loads(engine.read_text())
        assert after["app"] == before["app"]
        assert after.get("app_generation") == before.get("app_generation")
        # VM helpers survive without holding the command lease forever.
        assert command.run("status").returncode == 0


def test_interrupted_captured_backup_preserves_native_export_lease(request, tmp_path):
    runtime, command, _engine = request.getfixturevalue("combined")
    # Recapture a midnight UTC policy: the real captured command is always due,
    # without a test-only clock argument or reliance on the invocation's hour.
    runtime.run("backup-disable")
    enabled = runtime.run("backup-enable", hour=0, minute=0)
    job = Path(enabled["job"])
    previous = runtime.run("backup")
    archive = Path(previous["archive"])
    previous_bytes = archive.read_bytes()
    gate = tmp_path / "native-export.fifo"
    os.mkfifo(gate, 0o600)
    tool = command.bin / "podman"
    shebang, original = tool.read_text().split("\n", 1)
    tool.write_text(
        shebang
        + "\nimport os, signal\n"
        + "signal.alarm(20)\n"
        + f"os.environ['BACKUP_TEST_GATE'] = {str(gate)!r}\n"
        + original
    )
    try:
        assert run_captured_job(job, interrupt=gate.with_suffix(".ready")) == {
            "interrupted": True
        }
        for operation in ("supervision-tick", "backup-reconcile", "start"):
            blocked = command.run(operation)
            assert blocked.returncode == 1, blocked.stdout
            assert "Another project command" in blocked.stderr
        assert archive.read_bytes() == previous_bytes
    finally:
        # Nonblocking release also handles the pre-fix control where cleanup
        # killed the only reader. Never hang or signal an unrelated process.
        try:
            descriptor = os.open(gate, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as error:
            if error.errno != errno.ENXIO:
                raise
        else:
            os.write(descriptor, b"x")
            os.close(descriptor)
    subprocess.run(
        [
            str(runtime.root / ".venv/bin/python"),
            "-c",
            "import fcntl, sys; lock=open(sys.argv[1], 'a'); fcntl.flock(lock, fcntl.LOCK_EX)",
            str(command.state / "command.lock"),
        ],
        check=True,
        timeout=5,
    )
    assert runtime.run("supervision-status")["state"] == "maintenance"
    assert runtime.run("backup-reconcile")["resumed"] is False
    assert archive.read_bytes() == previous_bytes


@pytest.mark.parametrize("removal_failure", [False, True])
def test_pre_fix_supervision_can_only_be_removed_then_deliberately_recaptured(
    tmp_path, removal_failure
):
    command = registration(tmp_path)
    installed = invoke(command, "supervision-install")
    assert installed.returncode == 0, installed.stderr
    job = Path(json.loads(installed.stdout)["job"])
    definition = plistlib.loads(job.read_bytes())
    del definition["AbandonProcessGroup"]
    legacy = plistlib.dumps(definition, sort_keys=True)
    job.write_bytes(legacy)
    loaded_path = command.state / "loaded-job.json"
    loaded = json.loads(loaded_path.read_text())
    loaded["definition"] = definition
    loaded_path.write_text(json.dumps(loaded))
    ledger = command.state / "supervision.json"
    old_record = ledger.read_bytes()
    owner = (command.state / "owner.json").read_bytes()
    command.run("stop")
    intent = (command.state / "operator-intent.json").read_bytes()
    assert invoke(command, "supervision-install").returncode == 1
    assert command.run("supervision-tick").returncode == 1
    assert job.read_bytes() == legacy
    if removal_failure:
        launchctl = command.bin / "launchctl"
        original = launchctl.read_text()
        shebang, body = original.split("\n", 1)
        launchctl.write_text(
            shebang + "\nimport sys\nif sys.argv[1] == 'bootout': sys.exit(5)\n" + body
        )
        assert invoke(command, "supervision-remove").returncode == 1
        assert job.read_bytes() == legacy
        assert ledger.read_bytes() == old_record
        launchctl.write_text(original)
    removed = invoke(command, "supervision-remove")
    assert removed.returncode == 0, removed.stderr
    assert not job.exists()
    assert not ledger.exists()
    recaptured = invoke(command, "supervision-install")
    assert recaptured.returncode == 0, recaptured.stderr
    assert plistlib.loads(job.read_bytes())["AbandonProcessGroup"] is True
    assert (
        json.loads(ledger.read_text())["environment"]
        == json.loads(old_record)["environment"]
    )
    assert (command.state / "operator-intent.json").read_bytes() == intent
    assert (command.state / "owner.json").read_bytes() == owner
    assert command.run("supervision-tick").returncode == 0


def test_pre_fix_backup_removal_and_recapture_preserve_policy_settings_and_history(
    request,
):
    runtime, command, _engine = request.getfixturevalue("combined")
    runtime.run("backup-disable")
    enabled = runtime.run("backup-enable", hour=5, minute=17)
    job = Path(enabled["job"])
    definition = plistlib.loads(job.read_bytes())
    del definition["AbandonProcessGroup"]
    legacy = plistlib.dumps(definition, sort_keys=True)
    job.write_bytes(legacy)
    loaded_path = command.state / "loaded-backup-job.json"
    loaded = json.loads(loaded_path.read_text())
    loaded["definition"] = definition
    loaded_path.write_text(json.dumps(loaded))
    ledger = command.state / "backup-schedule.json"
    config = json.loads(ledger.read_text())
    config["job_sha256"] = hashlib.sha256(legacy).hexdigest()
    config["policy_id"] = hashlib.sha256(legacy + b"5:17").hexdigest()
    ledger.write_text(json.dumps(config))
    # Seed a genuine previous-policy daily success through the public operation.
    runtime.run("backup-due", now=NOW + timedelta(hours=3))
    retained = {
        p: p.read_bytes() for p in command.state.glob("backup-*.json") if p != ledger
    }
    archives = {
        p: p.read_bytes() for p in Path(command.env["RSS_BACKUP_DIR"]).glob("*.zip")
    }
    with pytest.raises(RuntimeFailure, match="settings changed"):
        runtime.run("backup-enable", hour=5, minute=17)
    assert job.read_bytes() == legacy
    assert runtime.run("backup-disable")["enabled"] is False
    assert not job.exists()
    runtime.run("backup-enable", hour=5, minute=17)
    updated = json.loads(ledger.read_text())
    assert updated["policy_id"] != config["policy_id"]
    for name in ("hour", "minute", "environment", "destination", "dependencies"):
        assert updated[name] == config[name]
    assert all(p.read_bytes() == content for p, content in retained.items())
    assert all(p.read_bytes() == content for p, content in archives.items())
    assert (
        runtime.run("backup-status", now=NOW + timedelta(hours=3))["protected"] is False
    )


def test_captured_backup_restores_originally_stopped_vm_and_intent(request, tmp_path):
    runtime, command, engine = request.getfixturevalue("combined")
    runtime.run("stop")
    runtime.run("backup-disable")
    enabled = runtime.run("backup-enable", hour=0, minute=0)
    with native_vm_service(command, tmp_path) as (_port, ready):
        result = run_captured_job(Path(enabled["job"]))
        assert result["outcome"] == "success"
        assert result["backup"]["verified"] is True
        assert ready.exists(), "Backup must exercise the stopped-VM launcher"
        assert json.loads(engine.read_text())["machine"]["State"] == "stopped"
        assert json.loads(engine.read_text())["app"]["State"]["Running"] is False
        assert runtime.run("supervision-tick")["state"] == "intentionally_stopped"
    assert (tmp_path / "helper-ended").exists()
