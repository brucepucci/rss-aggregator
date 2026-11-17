"""Combined supervisor/backup behavior at the approved CLI and Runtime seams."""

import io
import json
import os
import signal
import socket
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from rss_aggregator import Runtime, RuntimeFailure
from tests.test_supervision import supervised as supervised

NOW = datetime(2026, 11, 16, 3, tzinfo=timezone.utc)


@pytest.fixture
def combined(request, monkeypatch, tmp_path):
    command, engine = request.getfixturevalue("supervised")
    command.state.chmod(0o700)
    command.env["RSS_BACKUP_DIR"] = str(tmp_path / "private-backups")
    # Each installed label has independent state, just as real launchd does.
    command.tool(
        "launchctl",
        """import json, os, plistlib, sys
from pathlib import Path
args = sys.argv[1:]
home = Path(os.environ['HOME'])
label = args[1].split('/')[-1] if args[0] != 'bootstrap' else plistlib.loads(Path(args[2]).read_bytes())['Label']
loaded = home / ('loaded-job.json' if label == 'io.rss-aggregator.supervision' else 'loaded-backup-job.json')
if args[0] == 'print':
 if args[1].count('/') == 1: pass
 elif loaded.exists():
  record = json.loads(loaded.read_text())
  job = Path(record['path'])
  definition = record['definition']
  print('path = ' + str(job))
  print('program = ' + definition['ProgramArguments'][0])
  print('arguments = {\\n' + '\\n'.join(definition['ProgramArguments']) + '\\n}')
  print('environment = {\\n' + '\\n'.join(k + ' => ' + v for k,v in definition['EnvironmentVariables'].items()) + '\\n}')
 else: raise SystemExit(113)
elif args[0] == 'bootstrap': loaded.write_text(json.dumps({'path': args[2], 'definition': plistlib.loads(Path(args[2]).read_bytes())}))
elif args[0] == 'bootout': loaded.unlink()
else: raise SystemExit(99)
""",
    )
    payload = tmp_path / "source.tar"
    with tarfile.open(payload, "w") as archive:
        content = b"synthetic instance state"
        info = tarfile.TarInfo("users/alice/db.sqlite")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    state = json.loads(engine.read_text())
    state["payload"] = str(payload)
    engine.write_text(json.dumps(state))
    for name, value in command.env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "platform", "darwin")

    class FixtureSocket(socket.socket):
        def bind(self, address):
            # The disposable HTTP server substitutes for Podman's listener.
            if address != ("127.0.0.1", int(command.env["RSS_PORT"])):
                return super().bind(address)

    monkeypatch.setattr("rss_aggregator.runtime.socket.socket", FixtureSocket)
    runtime = Runtime(state_dir=command.state)
    runtime.run("backup-enable", now=NOW)
    return runtime, command, engine


def test_daily_backup_and_supervisor_share_intent_and_leave_healthy_app_running(
    combined,
):
    runtime, command, engine = combined
    assert runtime.run("supervision-tick")["state"] == "healthy"
    result = runtime.run("backup-due", now=NOW)
    assert result["outcome"] == "success"
    assert result["backup"]["verified"] is True
    assert Path(result["backup"]["archive"]).is_file()
    assert json.loads(engine.read_text())["app"]["State"]["Running"] is True
    assert runtime.run("supervision-status")["intent"] == "running"
    assert runtime.run("backup-status", now=NOW)["protected"] is True
    before = len(json.loads(engine.read_text())["calls"])
    assert runtime.run("supervision-tick")["state"] == "healthy"
    calls = json.loads(engine.read_text())["calls"][before:]
    assert not any(
        call[:2] == ["--connection", "rss-aggregator"]
        and call[2] in ("stop", "start", "run", "rm", "cp")
        for call in calls
    )
    assert json.loads((command.state / "operator-intent.json").read_text()) == {
        "version": 1,
        "intent": "running",
    }


def test_explicit_stop_survives_daily_backup_and_a_new_supervisor_process(combined):
    runtime, command, engine = combined
    stopped = runtime.run("stop", now=NOW)
    assert stopped["machine"] == "stopped"
    assert stopped["backup"]["verified"] is True
    assert runtime.run("backup-status", now=NOW)["latest_attempt"]["kind"] == "stop"
    result = runtime.run("backup-due", now=NOW)
    assert result["outcome"] == "success"
    assert json.loads(engine.read_text())["machine"]["State"] == "stopped"
    assert json.loads(engine.read_text())["app"]["State"]["Running"] is False
    for _ in range(2):
        tick = command.run("supervision-tick")
        assert tick.returncode == 0, tick.stderr
        assert json.loads(tick.stdout)["state"] == "intentionally_stopped"
    assert runtime.run("supervision-status")["intent"] == "stopped"
    assert runtime.run("backup-status", now=NOW)["protected"] is True
    assert runtime.run("start")["machine"] == "running"
    assert runtime.run("supervision-tick")["state"] == "healthy"


@pytest.mark.parametrize("stop_before_reconcile", [False, True])
def test_killed_daily_export_holds_supervisor_until_reconcile_then_honors_intent(
    combined, tmp_path, stop_before_reconcile
):
    runtime, command, engine = combined
    previous = runtime.run("backup", now=NOW)
    previous_bytes = Path(previous["archive"]).read_bytes()
    gate = tmp_path / "export.fifo"
    os.mkfifo(gate, 0o600)
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from datetime import datetime, timezone; from rss_aggregator import Runtime; "
            "Runtime().run('backup-due', now=datetime(2026,11,16,3,tzinfo=timezone.utc))",
        ],
        env=dict(command.env, BACKUP_TEST_GATE=str(gate)),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    orphan = None
    try:
        deadline = time.monotonic() + 10
        while not gate.with_suffix(".ready").exists():
            assert child.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        orphan = int(gate.with_suffix(".ready").read_text())
        # Another launchd wakeup and supervision both defer during live capture.
        for operation in ("supervision-tick", "backup-reconcile", "backup-due"):
            with pytest.raises(RuntimeFailure, match="Another project command"):
                runtime.run(operation, now=NOW)
        assert runtime.run("backup-status", now=NOW)["busy"] is True
        child.kill()
        assert child.wait(timeout=5) == -signal.SIGKILL
        for operation in ("supervision-tick", "backup-reconcile", "start", "stop"):
            blocked = command.run(operation)
            assert blocked.returncode == 1
            assert "Another project command" in blocked.stderr
        with gate.open("wb") as barrier:
            barrier.write(b"x")
        child.communicate(timeout=5)
        # Pipe EOF can precede the orphan interpreter's final FD teardown.
        # Synchronize on the native lock itself rather than racing process exit.
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import fcntl, sys; lock=open(sys.argv[1], 'a'); "
                "fcntl.flock(lock, fcntl.LOCK_EX)",
                str(command.state / "command.lock"),
            ],
            check=True,
            timeout=5,
        )
        orphan = None
        assert runtime.run("supervision-status")["state"] == "maintenance"
        assert runtime.run("supervision-status")["recovery"]["attempts"] == 0
        with pytest.raises(ValueError, match="maintenance"):
            runtime.run("supervision-tick")
        assert json.loads(engine.read_text())["app"]["State"]["Running"] is False
        if stop_before_reconcile:
            with pytest.raises(RuntimeFailure, match="backup-reconcile"):
                runtime.run("stop")
            assert runtime.run("supervision-status")["intent"] == "stopped"
            with pytest.raises(RuntimeFailure, match="backup-reconcile"):
                runtime.run("start")
            assert runtime.run("supervision-status")["intent"] == "stopped"
        reconciled = runtime.run("backup-reconcile")
        assert reconciled["reconciled"] is True
        assert reconciled["resumed"] is False
        assert json.loads(engine.read_text())["app"]["State"]["Running"] is False
        report = runtime.run("backup-status", now=NOW)
        assert report["latest_attempt"]["outcome"] == "interrupted"
        assert report["protected"] is False
        assert report["latest_success"]["archive"] == previous["archive"]
        assert Path(previous["archive"]).read_bytes() == previous_bytes
        tick = runtime.run("supervision-tick")
        assert tick["state"] == (
            "intentionally_stopped" if stop_before_reconcile else "recovered"
        )
        assert json.loads(engine.read_text())["app"]["State"]["Running"] is (
            not stop_before_reconcile
        )
        assert (
            runtime.run("backup-due", now=NOW + timedelta(minutes=15))["outcome"]
            == "success"
        )
        assert runtime.run("supervision-status")["intent"] == (
            "stopped" if stop_before_reconcile else "running"
        )
    finally:
        if child.poll() is None:
            child.kill()
        if orphan is not None:
            try:
                os.kill(orphan, signal.SIGKILL)
            except ProcessLookupError:
                pass
        child.communicate(timeout=5)


def test_verified_archive_with_failed_resumption_keeps_supervisor_on_hold(
    combined, monkeypatch
):
    runtime, _command, engine = combined
    original = os.link

    def fail_dependency_after_publication(*args, **kwargs):
        result = original(*args, **kwargs)
        state = json.loads(engine.read_text())
        state["fail_image"] = True
        engine.write_text(json.dumps(state))
        return result

    monkeypatch.setattr(os, "link", fail_dependency_after_publication)
    with pytest.raises(RuntimeFailure, match="Daily backup failed"):
        runtime.run("backup-due", now=NOW)
    report = runtime.run("backup-status", now=NOW)
    assert report["latest_success"]["verified"] is True
    assert Path(report["latest_success"]["archive"]).is_file()
    assert report["latest_attempt"]["outcome"] == "failed"
    assert report["resumption"] == "failed"
    assert report["protected"] is False
    assert runtime.run("supervision-status")["state"] == "maintenance"
    with pytest.raises(ValueError, match="maintenance"):
        runtime.run("supervision-tick")
    assert json.loads(engine.read_text())["app"]["State"]["Running"] is False
    with pytest.raises(RuntimeFailure, match="backup-reconcile"):
        runtime.run("stop")
    assert runtime.run("backup-reconcile")["resumed"] is False
    assert runtime.run("supervision-tick")["state"] == "intentionally_stopped"


def test_failed_daily_export_restores_service_without_triggering_recovery(combined):
    runtime, _command, engine = combined
    previous = runtime.run("backup", now=NOW)
    previous_bytes = Path(previous["archive"]).read_bytes()
    state = json.loads(engine.read_text())
    state["fail_export"] = True
    engine.write_text(json.dumps(state))
    with pytest.raises(RuntimeFailure, match="Daily backup failed"):
        runtime.run("backup-due", now=NOW)
    report = runtime.run("backup-status", now=NOW)
    assert report["latest_attempt"]["outcome"] == "failed"
    assert report["resumption"] == "preserved"
    assert report["maintenance_required"] is False
    assert report["protected"] is False
    assert report["latest_success"]["archive"] == previous["archive"]
    assert Path(previous["archive"]).read_bytes() == previous_bytes
    assert runtime.run("supervision-tick")["state"] == "healthy"
    assert runtime.run("supervision-status")["recovery"]["history"] == []
    state = json.loads(engine.read_text())
    assert state["app"]["State"]["Running"] is True
    state["fail_export"] = False
    engine.write_text(json.dumps(state))
    assert (
        runtime.run("backup-due", now=NOW + timedelta(minutes=15))["outcome"]
        == "success"
    )


def test_disabling_either_job_preserves_the_other_and_operator_intent(combined):
    runtime, command, _engine = combined
    runtime.run("backup-disable")
    assert runtime.run("supervision-tick")["state"] == "healthy"
    runtime.run("backup-enable", now=NOW)
    runtime.run("supervision-remove")
    assert runtime.run("supervision-status")["enabled"] is False
    assert runtime.run("supervision-status")["intent"] == "running"
    assert runtime.run("backup-due", now=NOW)["outcome"] == "success"
    assert runtime.run("backup-status", now=NOW)["protected"] is True
    assert (
        command.state / "Library/LaunchAgents/io.rss-aggregator.backup.plist"
    ).is_file()
