"""Daily protection through the approved Runtime and installed CLI seams."""

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from rss_aggregator import Runtime, RuntimeFailure
from tests.test_backups import ROOT, BackupProject


@pytest.fixture
def project(tmp_path):
    return BackupProject(tmp_path)


@pytest.fixture
def scheduled(project, monkeypatch, tmp_path):
    project.state_dir.chmod(0o700)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    project.env["HOME"] = str(home)
    launch = tmp_path / "launch.json"
    launch.write_text("{}")
    project.env["BACKUP_TEST_LAUNCH"] = str(launch)
    shim = tmp_path / "bin" / "launchctl"
    shim.write_text(
        "#!"
        + sys.executable
        + "\n"
        + """
import json, os, plistlib, sys
from pathlib import Path
state = Path(os.environ["BACKUP_TEST_LAUNCH"])
record = json.loads(state.read_text())
args = sys.argv[1:]
if args[0] == "print":
    if not record: sys.exit(113)
    print("path = " + record["path"])
    print("program = " + record["program"])
    print("arguments = {\\n" + "\\n".join(record.get("arguments", [])) + "\\n}")
    print("environment = {\\n" + "\\n".join(k + " => " + v for k, v in record.get("environment", {}).items()) + "\\n}")
elif args[0] == "bootstrap":
    if record: sys.exit(5)
    value = plistlib.loads(Path(args[-1]).read_bytes())
    state.write_text(json.dumps({"path": args[-1], "program": value["ProgramArguments"][0], "arguments": value["ProgramArguments"], "environment": value["EnvironmentVariables"]}))
elif args[0] == "bootout":
    state.write_text("{}")
else: sys.exit(99)
"""
    )
    shim.chmod(0o755)
    for name, value in project.env.items():
        monkeypatch.setenv(name, value)
    # Platform discovery is a native boundary; launchctl itself is always disposable.
    monkeypatch.setattr(sys, "platform", "darwin")
    return Runtime(state_dir=project.state_dir), project, launch


def test_opt_in_daily_schedule_is_idempotent_and_captures_noninteractive_runtime(
    scheduled,
):
    import plistlib

    runtime, project, launch = scheduled
    now = datetime(2026, 11, 16, 2, tzinfo=timezone.utc)
    assert runtime.run("backup-due", now=now)["outcome"] == "disabled"
    assert not project.backups.exists()
    enabled = runtime.run("backup-enable", now=now)
    assert enabled["enabled"] is True
    assert runtime.run("backup-enable", now=now)["enabled"] is True
    plist = plistlib.loads(Path(enabled["job"]).read_bytes())
    assert plist["ProgramArguments"] == [
        str(ROOT / ".venv/bin/python"),
        "-I",
        "-m",
        "rss_aggregator",
        "backup-due",
    ]
    assert plist["EnvironmentVariables"]["RSS_BACKUP_DIR"] == str(project.backups)
    assert plist["EnvironmentVariables"]["RSS_STATE_DIR"] == str(project.state_dir)
    assert plist["StartInterval"] == 900
    assert plist["RunAtLoad"] is True
    assert plist["WorkingDirectory"] == str(ROOT)
    assert (
        "StandardOutPath" not in plist
    )  # no archived diagnostics/content in scheduler logs
    assert runtime.run("backup-status", now=now)["protected"] is False
    runtime.run("backup", now=now)
    assert runtime.run("backup-status", now=now)["protected"] is False
    missed = runtime.run("backup-status", now=now + timedelta(hours=2))
    assert missed["missed"] is True
    assert missed["next_expected_attempt"] == "2026-11-16T03:00:00+00:00"
    assert runtime.run("backup-due", now=now)["outcome"] == "not_due"
    result = runtime.run("backup-due", now=now + timedelta(hours=1))
    assert result["outcome"] == "success"
    assert result["backup"]["verified"] is True
    assert (
        runtime.run("backup-due", now=now + timedelta(hours=2))["outcome"] == "not_due"
    )
    status = runtime.run("backup-status", now=now + timedelta(hours=2))
    assert status["protected"] is True
    assert status["next_expected_attempt"] == "2026-11-17T03:00:00+00:00"
    assert runtime.run("backup-disable")["enabled"] is False
    assert runtime.run("backup-disable")["enabled"] is False
    assert json.loads(launch.read_text()) == {}
    assert list(project.backups.glob("*.zip"))


@pytest.mark.parametrize(
    "key,value", [("RSS_PORT", "invalid"), ("RSS_TAILSCALE", "invalid")]
)
def test_invalid_captured_runtime_settings_never_install_a_job(
    scheduled, monkeypatch, key, value
):
    runtime, _project, launch = scheduled
    monkeypatch.setenv(key, value)
    with pytest.raises((RuntimeFailure, ValueError), match=key):
        runtime.run("backup-enable")
    assert json.loads(launch.read_text()) == {}
    assert not (
        Path.home() / "Library/LaunchAgents/io.rss-aggregator.backup.plist"
    ).exists()


def test_daily_failures_are_secret_safe_bounded_and_previous_success_is_retained(
    scheduled,
):
    runtime, project, _launch = scheduled
    now = datetime(2026, 11, 16, 3, tzinfo=timezone.utc)
    runtime.run("backup-enable", now=now)
    runtime.run("backup-due", now=now)
    previous = {path: path.read_bytes() for path in project.backups.glob("*.zip")}
    project.update(fail_export=True)
    tomorrow = now + timedelta(days=1)
    for minute in (0, 15, 30):
        with pytest.raises(RuntimeFailure, match="Daily backup failed"):
            runtime.run("backup-due", now=tomorrow + timedelta(minutes=minute))
    assert (
        runtime.run("backup-due", now=tomorrow + timedelta(minutes=45))["outcome"]
        == "not_due"
    )
    status = runtime.run("backup-status", now=tomorrow + timedelta(hours=3))
    assert status["daily_attempt"]["count"] == 3
    assert status["latest_attempt"]["outcome"] == "failed"
    assert status["latest_success"]["verified"] is True
    assert status["overdue"] is True
    assert status["protected"] is False
    assert "sensitive filename" not in json.dumps(status)
    assert {
        path: path.read_bytes() for path in project.backups.glob("*.zip")
    } == previous


def test_reconfigured_destination_requires_new_daily_evidence(scheduled, monkeypatch):
    runtime, project, _launch = scheduled
    now = datetime(2026, 11, 16, 3, tzinfo=timezone.utc)
    runtime.run("backup-enable", now=now)
    first = runtime.run("backup-due", now=now)["backup"]
    runtime.run("backup-disable")
    destination = project.backups.parent / "new-private-destination"
    monkeypatch.setenv("RSS_BACKUP_DIR", str(destination))
    runtime.run("backup-enable", now=now)
    assert runtime.run("backup-status", now=now)["protected"] is False
    result = runtime.run("backup-due", now=now)
    assert result["outcome"] == "success"
    assert Path(result["backup"]["archive"]).parent == destination
    assert Path(first["archive"]).is_file()


def test_contention_is_persisted_as_deferred_not_success(scheduled):
    runtime, project, _launch = scheduled
    now = datetime(2026, 11, 16, 3, tzinfo=timezone.utc)
    runtime.run("backup-enable", now=now)
    with (project.state_dir / "command.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeFailure, match="Another project command"):
            runtime.run("backup-due", now=now)
    status = runtime.run("backup-status", now=now)
    assert status["latest_defer"]["outcome"] == "deferred"
    assert status["latest_success"] is None
    assert status["protected"] is False
    assert (
        runtime.run("backup-due", now=now + timedelta(minutes=15))["outcome"]
        == "success"
    )


@pytest.mark.parametrize(
    "changed",
    [
        "definition",
        "loaded",
        "loaded-arguments",
        "loaded-environment",
        "dependency",
        "python",
    ],
)
def test_changed_or_unavailable_scheduler_cannot_appear_protected(scheduled, changed):
    runtime, project, launch = scheduled
    now = datetime(2026, 11, 16, 3, tzinfo=timezone.utc)
    enabled = runtime.run("backup-enable", now=now)
    runtime.run("backup-due", now=now)
    job = Path(enabled["job"])
    original = job.read_bytes()
    if changed == "definition":
        job.write_bytes(b"foreign definition")
    elif changed == "loaded":
        launch.write_text(
            json.dumps({"path": "/foreign/job.plist", "program": "/foreign/python"})
        )
    elif changed.startswith("loaded-"):
        loaded = json.loads(launch.read_text())
        if changed == "loaded-arguments":
            loaded["arguments"][-1] = "stop"
        else:
            loaded["environment"]["RSS_STATE_DIR"] = "/foreign/owner"
        launch.write_text(json.dumps(loaded))
    elif changed == "dependency":
        Path(project.env["PATH"], "podman").unlink()
    else:
        # A recorded unavailable selected interpreter is a file-system fault;
        # never modify the real checkout environment used by this test process.
        config_path = project.state_dir / "backup-schedule.json"
        config = json.loads(config_path.read_text())
        config["python"] = "/unavailable/private-checkout/.venv/bin/python"
        config_path.write_text(json.dumps(config))
    status = runtime.run("backup-status", now=now)
    assert status["protected"] is False
    assert status["scheduler_problem"]
    if changed in {"definition", "loaded", "loaded-arguments", "loaded-environment"}:
        with pytest.raises(RuntimeFailure):
            runtime.run("backup-disable")
        assert job.read_bytes() == (
            b"foreign definition" if changed == "definition" else original
        )


def test_ordinary_backup_does_not_enable_daily_protection(project):
    result = project.run("backup-status")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["enabled"] is False
    assert report["latest_attempt"] is None
    assert report["latest_success"] is None
    assert report["overdue"] is True
    assert report["protected"] is False
    check = project.run("backup-status", "--check")
    assert check.returncode == 1
    assert json.loads(check.stdout)["protected"] is False
    assert not project.engine()["calls"]
    assert project.run("backup").returncode == 0
    status = json.loads(project.run("backup-status").stdout)
    assert status["enabled"] is False
    assert status["latest_attempt"]["outcome"] == "success"
    assert status["latest_success"]["verified"] is True
    assert status["protected"] is False


@pytest.mark.parametrize(
    "operation",
    [
        "start",
        "fetch",
        "verify",
        "replace",
        "backup",
        "stop",
        "restore",
        "select-volume",
    ],
)
def test_interrupted_maintenance_blocks_writers_and_reports_action(project, operation):
    (project.state_dir / "maintenance.json").write_text('{"version": 1}')
    result = project.run(
        operation,
        *(
            ["missing", "--volume", "fresh"]
            if operation == "restore"
            else ["fresh"]
            if operation == "select-volume"
            else []
        ),
    )
    assert result.returncode != 0
    if operation != "restore":  # Click rejects a missing archive before dispatch.
        assert "backup-reconcile" in result.stderr
    status = json.loads(project.run("backup-status").stdout)
    assert status["maintenance_required"] is True
    assert status["protected"] is False
    assert not project.engine()["calls"]


def test_dangling_maintenance_hold_cannot_be_bypassed(project):
    (project.state_dir / "maintenance.json").symlink_to(
        project.state_dir / "missing-hold"
    )
    result = project.run("start")
    assert result.returncode == 1
    assert "backup-reconcile" in result.stderr
    assert json.loads(project.run("backup-status").stdout)["maintenance_required"]
    assert project.run("backup-reconcile").returncode == 1
    assert not project.engine()["calls"]


@pytest.mark.parametrize("fault", ["publication", "readback"])
def test_archive_io_failure_preserves_source_and_previous_verified_copy(
    project, monkeypatch, fault
):
    for name, value in project.env.items():
        monkeypatch.setenv(name, value)
    runtime = Runtime(state_dir=project.state_dir)
    first = runtime.run("backup")
    previous = Path(first["archive"]).read_bytes()
    if fault == "publication":

        def refuse_link(*args, **kwargs):
            raise PermissionError("synthetic private feed URL must not leak")

        monkeypatch.setattr(os, "link", refuse_link)
    else:
        original = Path.open

        def refuse_readback(path, *args, **kwargs):
            if (
                path.name.startswith(".rss-backup-")
                and path.suffix == ".zip"
                and args
                and args[0] == "rb"
            ):
                raise OSError("synthetic private feed URL must not leak")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", refuse_readback)
    with pytest.raises(OSError, match="Backup archive I/O failed"):
        runtime.run("backup")
    status = runtime.run("backup-status")
    assert status["latest_attempt"]["outcome"] == "failed"
    assert status["latest_success"]["archive"] == first["archive"]
    assert status["resumption"] == "preserved"
    assert "private feed URL" not in json.dumps(status)
    assert Path(first["archive"]).read_bytes() == previous
    assert list(project.backups.iterdir()) == [Path(first["archive"])]
    assert project.engine()["volumes"]


def test_freshness_uses_controlled_public_orchestration_clock(project, monkeypatch):
    for name, value in project.env.items():
        monkeypatch.setenv(name, value)
    runtime = Runtime(state_dir=project.state_dir)
    now = datetime(2026, 11, 16, 3, tzinfo=timezone.utc)
    runtime.run("backup", now=now)
    fresh = runtime.run("backup-status", now=now + timedelta(hours=24))
    assert fresh["overdue"] is False
    assert fresh["age_seconds"] == 86400
    assert (
        runtime.run("backup-status", now=now + timedelta(hours=27))["overdue"] is True
    )


@pytest.mark.parametrize("stage", ["before", "after"])
def test_abrupt_publication_interruption_never_claims_new_success(
    project, tmp_path, stage
):
    first = project.run("backup")
    assert first.returncode == 0, first.stderr
    previous = json.loads(first.stdout)
    previous_bytes = Path(previous["archive"]).read_bytes()
    ready = tmp_path / "publication.ready"
    script = """
import os, signal
from pathlib import Path
from rss_aggregator import Runtime
original = os.link
def interrupted(source, target, *args, **kwargs):
    if os.environ["BACKUP_TEST_STAGE"] == "after":
        original(source, target, *args, **kwargs)
    Path(os.environ["BACKUP_TEST_READY"]).write_text("ready")
    os.kill(os.getpid(), signal.SIGSTOP)
    if os.environ["BACKUP_TEST_STAGE"] == "before":
        original(source, target, *args, **kwargs)
os.link = interrupted
Runtime().run("backup")
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=dict(project.env, BACKUP_TEST_STAGE=stage, BACKUP_TEST_READY=str(ready)),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert child.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        child.kill()
        child.communicate(timeout=5)
        status = json.loads(project.run("backup-status").stdout)
        assert status["latest_attempt"]["outcome"] == "interrupted"
        assert status["latest_success"]["archive"] == previous["archive"]
        assert status["maintenance_required"] is True
        assert Path(previous["archive"]).read_bytes() == previous_bytes
        published = list(project.backups.glob("backup-*.zip"))
        assert len(published) == (2 if stage == "after" else 1)
        assert project.run("backup-reconcile").returncode == 0
        if stage == "after":
            recovered = next(
                path for path in published if str(path) != previous["archive"]
            )
            assert (
                project.run(
                    "restore",
                    str(recovered),
                    "--volume",
                    "recovered-after-interruption",
                ).returncode
                == 0
            )
        assert project.run("backup").returncode == 0
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


def test_killed_backup_keeps_orphan_export_locked_and_requires_reconciliation(
    project, tmp_path
):
    first = project.run("backup")
    assert first.returncode == 0, first.stderr
    previous = {path: path.read_bytes() for path in project.backups.glob("*.zip")}
    gate = tmp_path / "export.fifo"
    os.mkfifo(gate, 0o600)
    env = dict(project.env, BACKUP_TEST_GATE=str(gate))
    child = subprocess.Popen(
        [sys.executable, "-m", "rss_aggregator", "backup"],
        env=env,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    orphan = None
    try:
        deadline = time.monotonic() + 10
        while not gate.with_suffix(".ready").exists():
            assert child.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        orphan = int(gate.with_suffix(".ready").read_text())
        child.kill()
        assert child.wait(timeout=5) == -signal.SIGKILL
        # The independently surviving export retains the project lock.
        active = project.run("backup-status")
        assert active.returncode == 0, active.stderr
        assert json.loads(active.stdout)["busy"] is True
        assert json.loads(active.stdout)["latest_attempt"]["outcome"] == "running"
        blocked = project.run("backup-reconcile")
        assert blocked.returncode == 1
        assert "Another project command" in blocked.stderr
        status = project.run("start")
        assert status.returncode == 1
        assert "Another project command" in status.stderr
        with gate.open("wb") as barrier:
            barrier.write(b"x")
        child.communicate(timeout=5)  # inherited output pipes close after export exits
        orphan = None
        report = json.loads(project.run("backup-status").stdout)
        assert report["maintenance_required"] is True
        assert report["latest_attempt"]["outcome"] == "interrupted"
        assert project.run("backup").returncode == 1
        result = project.run("backup-reconcile")
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["resumed"] is False
        assert not json.loads(project.run("backup-status").stdout)[
            "maintenance_required"
        ]
        assert {
            path: path.read_bytes() for path in project.backups.glob("*.zip")
        } == previous
        assert (
            project.engine()["containers"]["rss-aggregator-freshrss"]["State"][
                "Running"
            ]
            is False
        )
        assert project.run("backup").returncode == 0
    finally:
        if child.poll() is None:
            child.kill()
        if orphan is not None:
            try:
                os.kill(orphan, signal.SIGKILL)
            except ProcessLookupError:
                pass
        child.communicate(timeout=5)
