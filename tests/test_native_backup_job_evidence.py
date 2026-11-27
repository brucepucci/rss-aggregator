"""Exercise native backup and supervision evidence through controlled CLI commands."""

import json
import os
from pathlib import Path

import pytest

from tests import test_native_jobs
from tests.test_native_jobs import (
    LAUNCHCTL,
    _refused,
    _restore_fixture,
    _success,
    _tool,
)

jobs = test_native_jobs.jobs

INSPECTION_OBSERVER = """
import json
import os
import sys
import time
from pathlib import Path

def stall(probe):
    if os.environ.get('SYNTHETIC_STALLED_PROBE') != probe:
        return
    lock = (Path(os.environ['RSS_STATE_DIR']) / 'command.lock').stat()
    inherited = False
    for fd in os.listdir('/dev/fd'):
        try:
            inherited |= os.path.samestat(lock, os.fstat(int(fd)))
        except OSError:
            pass  # The fd that listed the directory is already closed.
    Path.home().joinpath('probe-entry').write_text(json.dumps({
        'pid': os.getpid(),
        'inherited': inherited,
    }))
    print('private synthetic probe output', file=sys.stderr, flush=True)
    time.sleep(8)
"""


def _daily_success(jobs):
    _restore_fixture(jobs, "reader")
    _success(jobs, "select-instance", "reader")
    _success(jobs, "backup-enable", "--hour", "4")
    _success(jobs, "backup-due")
    return _success(jobs, "backup-status", "--check")


def test_unreadable_scheduled_archive_is_not_protection_but_keeps_evidence(jobs):
    before = _daily_success(jobs)
    archive = Path(before["daily_attempt"]["archive"])
    archive.chmod(0o000)
    try:
        assert archive.stat().st_size > 0
        check = jobs.run("backup-status", "--check")
    finally:
        archive.chmod(0o600)
    assert check.returncode == 1, (check.stdout, check.stderr)
    report = json.loads(check.stdout)
    assert report["daily_attempt"] == before["daily_attempt"]
    assert report["latest_success"] == before["latest_success"]
    assert report["daily_archive_available"] is False
    assert report["archive_available"] is False
    assert set(report["archive_problems"]) == {"daily_attempt", "latest_success"}
    assert report["evidence_problems"] == {}
    assert report["protected"] is False
    assert _success(jobs, "backup-status", "--check") == before


def test_historical_archive_can_follow_symlink_but_scheduled_archive_cannot(jobs):
    before = _daily_success(jobs)
    archive = Path(before["daily_attempt"]["archive"])
    moved = archive.rename(archive.with_suffix(".retained"))
    archive.symlink_to(moved.name)
    check = jobs.run("backup-status", "--check")
    assert check.returncode == 1, (check.stdout, check.stderr)
    report = json.loads(check.stdout)
    assert report["archive_available"] is True
    assert report["daily_archive_available"] is False
    assert report["daily_attempt"] == before["daily_attempt"]
    assert report["protected"] is False


@pytest.mark.parametrize("drift", [False, True], ids=["due", "selector-drift"])
def test_scheduler_refuses_damaged_daily_record_without_changing_evidence(jobs, drift):
    before = _daily_success(jobs)
    daily = jobs.state / "backup-daily.json"
    daily.write_text("null\n")
    jobs.environment["SYNTHETIC_SECONDS"] = "86400"
    archives = set(jobs.backups.glob("*.zip"))
    if drift:
        jobs.environment["RSS_PORT"] = "8081"

    refusal = _refused(jobs, "backup-due")
    assert "backup-daily.json" in refusal and "preserve" in refusal
    assert daily.read_text() == "null\n"
    assert set(jobs.backups.glob("*.zip")) == archives
    assert not (jobs.state / "maintenance.json").exists()
    assert not (jobs.state / "stop-service.json").exists()

    check = jobs.run("backup-status", "--check")
    assert check.returncode == 1, (check.stdout, check.stderr)
    report = json.loads(check.stdout)
    assert "backup-daily.json" in report["evidence_problems"]
    assert report["daily_attempt"] is None
    assert report["latest_attempt"] == before["latest_attempt"]
    assert report["latest_success"] == before["latest_success"]
    assert report["protected"] is False


@pytest.mark.parametrize("probe", ["loaded-job", "python-import", "gui-session"])
def test_stalled_read_only_probe_is_reaped_and_reports_unavailable(jobs, probe):
    home = Path(jobs.environment["HOME"])
    _tool(
        home / "launchctl",
        INSPECTION_OBSERVER
        + """
if sys.argv[1] == 'print':
    stall('gui-session' if sys.argv[2].count('/') == 1 else 'loaded-job')
"""
        + LAUNCHCTL,
    )
    _tool(
        jobs.root / ".venv/bin/python",
        INSPECTION_OBSERVER + "assert sys.argv[1:4] == ['-I', '-B', '-c']\n"
        f"stall('python-import')\nprint({str(jobs.root)!r})\n",
    )
    if probe == "gui-session":
        before = _success(jobs, "supervision-install")
        assert before["session"] == "available"
        args = ("supervision-status",)
    else:
        before = _daily_success(jobs)
        args = ("backup-status", "--check")
    jobs.environment["SYNTHETIC_STALLED_PROBE"] = probe

    result = jobs.run(*args)
    report = json.loads(result.stdout)
    assert "private synthetic probe output" not in result.stdout + result.stderr
    if probe == "gui-session":
        assert result.returncode == 0
        assert report["session"] != "available"
        assert report["state"] == "configuration_unavailable"
    else:
        assert result.returncode == 1
        assert report["scheduler_problem"] and report["protected"] is False
        assert report["registered"] is (probe == "python-import")
        assert report["daily_attempt"] == before["daily_attempt"]
        assert report["latest_success"] == before["latest_success"]
    observed = json.loads((home / "probe-entry").read_text())
    assert observed["inherited"] is False
    with pytest.raises(ProcessLookupError):
        os.kill(observed["pid"], 0)
    _success(jobs, "stop")


@pytest.mark.parametrize("had_plist", [True, False], ids=["replaced", "appeared"])
def test_supervision_remove_keeps_foreign_plist_and_ledger_after_bootout(
    jobs, had_plist
):
    home = Path(jobs.environment["HOME"])
    _tool(
        home / "launchctl",
        LAUNCHCTL
        + """
if verb == 'bootout':
    replacement = home / 'foreign-definition'
    if replacement.is_file():
        replacement.replace(home / 'Library/LaunchAgents' / (label + '.plist'))
""",
    )
    definition = Path(_success(jobs, "supervision-install")["job"])
    ledger = jobs.state / "supervision.json"
    original_ledger = ledger.read_bytes()
    if not had_plist:
        definition.unlink()
    foreign_bytes = b"foreign definition: preserve these bytes"
    replacement = home / "foreign-definition"
    replacement.write_bytes(foreign_bytes)
    replacement.chmod(0o600)

    assert "changed" in _refused(jobs, "supervision-remove")
    assert not (home / "fake-launchd" / definition.stem).exists()
    assert definition.read_bytes() == foreign_bytes
    assert ledger.read_bytes() == original_ledger
    assert "changed" in _refused(jobs, "supervision-remove")

    definition.unlink()  # Explicit removal of the disposable foreign fixture.
    assert _success(jobs, "supervision-remove")["state"] == "disabled"
    assert not ledger.exists()
