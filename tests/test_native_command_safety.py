"""Exercise native command exclusion and retirement through a disposable CLI."""

import fcntl
import subprocess
import sys
import time

import pytest

from tests import test_native_install, test_native_jobs
from tests.test_native_jobs import CLOCKED_CLI, _refused, _restore_fixture, _success

checkout = test_native_install.checkout
jobs = test_native_jobs.jobs


@pytest.mark.parametrize("state", ["new", "installed", "unloaded"])
def test_contended_due_does_not_record_deferral_without_owned_registration(
    request, state
):
    target = request.getfixturevalue("checkout" if state == "new" else "jobs")
    if state == "new":
        target.state.mkdir(mode=0o700)
    elif state == "unloaded":
        _success(target, "backup-enable", "--hour", "4")
        home = target.state.parent
        for registration in (home / "fake-launchd").iterdir():
            registration.unlink()
    with (target.state / "command.lock").open("a") as lock:
        lock_path = target.state / "command.lock"
        lock_path.chmod(0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = set(target.state.iterdir())
        assert "Another project command" in _refused(target, "backup-due")
        assert set(target.state.iterdir()) == before
    assert target.run("setup").returncode == 0


def test_reconciliation_cannot_retire_an_active_capture_after_command_path_loss(jobs):
    _restore_fixture(jobs, "reader")
    _success(jobs, "select-instance", "reader")
    entered = jobs.state.parent / "capture-entered"
    capture = f"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

original_open = os.open

def pause_source_read(path, flags, *args, **kwargs):
    if path == 'feed-cache' and kwargs.get('dir_fd') is not None:
        Path({str(entered)!r}).touch()
        assert os.read(0, 1) == b'x'
    return original_open(path, flags, *args, **kwargs)

with patch.object(os, 'open', pause_source_read):
    exec({CLOCKED_CLI!r})
"""
    with subprocess.Popen(
        (sys.executable, "-c", capture, "backup"),
        cwd=jobs.root,
        env=jobs.environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as exporting:
        deadline = time.monotonic() + 10
        while not entered.exists() and exporting.poll() is None:
            if time.monotonic() >= deadline:
                exporting.kill()
                break
            time.sleep(0.01)
        try:
            assert entered.exists(), "backup never reached its data capture"
            hold = jobs.state / "maintenance.json"
            before = hold.read_bytes()
            lock_path = jobs.state / "command.lock"
            parked = lock_path.rename(jobs.state.parent / "parked-command.lock")
            try:
                assert "Another project command" in _refused(jobs, "backup-reconcile")
                assert _success(jobs, "backup-status")["busy"] is True
                assert hold.read_bytes() == before
                assert not lock_path.exists()
            finally:
                parked.replace(lock_path)
        finally:
            try:
                stdout, stderr = exporting.communicate(b"x", timeout=10)
            except subprocess.TimeoutExpired:
                exporting.kill()
                exporting.communicate()
                raise
    assert exporting.returncode == 0, (stdout, stderr)
    assert not hold.exists()


@pytest.mark.parametrize(
    ("change", "job", "remnant"),
    [
        ("uninstall", "backup", "both"),
        ("upgrade", "supervision", "loaded"),
        ("upgrade", "backup", "plist"),
    ],
)
def test_runtime_change_refuses_jobs_even_if_their_ledgers_disappear(
    jobs, change, job, remnant
):
    _success(jobs, "stop")
    confirmation = _success(jobs, change)["confirmation"]
    if job == "backup":
        _success(jobs, "backup-enable", "--hour", "4")
        ledger = "backup-schedule.json"
    else:
        _success(jobs, "supervision-install")
        ledger = "supervision.json"
    (jobs.state / ledger).rename(jobs.state.parent / "parked-job-ledger")
    (plist,) = (jobs.state.parent / "Library/LaunchAgents").glob("*.plist")
    (loaded,) = (jobs.state.parent / "fake-launchd").iterdir()
    if remnant == "loaded":
        plist.unlink()
    if remnant == "plist":
        loaded.unlink()
    retained_files = {
        path: path.read_bytes() for path in (plist, loaded) if path.exists()
    }
    original_binary = jobs.binary.read_bytes()

    assert "job" in _refused(jobs, change)
    assert "job" in _refused(jobs, change, "--confirm", confirmation)
    assert {path: path.read_bytes() for path in retained_files} == retained_files
    assert jobs.binary.read_bytes() == original_binary


def test_runtime_change_refuses_when_the_user_session_cannot_be_inspected(jobs):
    _success(jobs, "stop")
    (jobs.state.parent / "session-unavailable").touch()
    assert "Cannot prove native jobs are absent" in _refused(jobs, "uninstall")
    assert jobs.binary.is_file()
