"""Exercise interrupted native LaunchAgent publication through the controlled CLI."""

import json
import plistlib
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tests import test_native_jobs
from tests.test_native_jobs import CLOCKED_CLI, _refused, _success

jobs = test_native_jobs.jobs

FAULTED_CLI = f"""
import json
import os
import plistlib
import stat
from pathlib import Path
from unittest.mock import patch

sync = os.fsync
fault = os.environ['SYNTHETIC_PUBLICATION_FAULT']
ledger = Path(os.environ['RSS_STATE_DIR']) / os.environ['SYNTHETIC_JOB_LEDGER']
launch_agents = Path.home() / 'Library/LaunchAgents'

def sync_plist(fd):
    info = os.fstat(fd)
    if (fault == 'early-conflict' and stat.S_ISDIR(info.st_mode) and ledger.is_file()
        and os.path.samestat(info, ledger.parent.stat())):
        sync(fd)
        final = Path(json.loads(ledger.read_text())['job'])
        definition = plistlib.dumps(dict(
            Label=final.stem,
            ProgramArguments=['/not-the-captured-launcher'],
            EnvironmentVariables=dict(),
        ))
        launch_agents.mkdir(parents=True, exist_ok=True)
        with final.open('xb') as writer:
            writer.write(definition)
        return
    if stat.S_ISREG(info.st_mode) and launch_agents.exists() and any(
        os.path.samestat(info, path.lstat()) for path in launch_agents.iterdir()
    ):
        if fault == 'interrupt':
            # Leave an incomplete file without unwinding the process.
            os.ftruncate(fd, info.st_size // 2)
            os._exit(71)
        assert stat.S_IMODE(info.st_mode) == 0o600
        sync(fd)
        final = Path(json.loads(ledger.read_text())['job'])
        final.symlink_to(Path.home() / 'foreign-definition')
        return
    sync(fd)

with patch.object(os, 'fsync', sync_plist):
    exec({CLOCKED_CLI!r})
"""


JOB_TYPES = pytest.mark.parametrize(
    ("enable", "disable", "ledger_name", "command"),
    [
        (
            "supervision-install",
            "supervision-remove",
            "supervision.json",
            "supervision-tick",
        ),
        ("backup-enable", "backup-disable", "backup-schedule.json", "backup-due"),
    ],
    ids=["supervision", "backup"],
)


def _faulted_enable(jobs, enable, ledger_name, fault):
    return subprocess.run(
        (sys.executable, "-c", FAULTED_CLI, enable),
        cwd=jobs.root,
        env={
            **jobs.environment,
            "SYNTHETIC_JOB_LEDGER": ledger_name,
            "SYNTHETIC_PUBLICATION_FAULT": fault,
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@JOB_TYPES
def test_interrupted_plist_write_keeps_final_path_absent_and_enable_retryable(
    jobs, enable, disable, ledger_name, command
):
    result = _faulted_enable(jobs, enable, ledger_name, "interrupt")
    assert result.returncode == 71, (result.stdout, result.stderr)
    ledger = jobs.state / ledger_name
    path = Path(json.loads(ledger.read_text())["job"])
    loaded = Path(jobs.environment["HOME"]) / "fake-launchd" / path.stem
    assert not path.exists() and not loaded.exists()

    assert _success(jobs, enable)["job"] == str(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    definition = plistlib.loads(path.read_bytes())
    assert definition["Label"] == path.stem
    assert definition["ProgramArguments"][-1] == command
    assert loaded.is_file()

    _success(jobs, disable)
    assert not path.exists() and not loaded.exists() and not ledger.exists()


@JOB_TYPES
def test_plist_publication_never_replaces_a_foreign_entry(
    jobs, enable, disable, ledger_name, command
):
    home = Path(jobs.environment["HOME"])
    foreign = home / "foreign-definition"
    foreign.write_bytes(b"foreign contents must survive unchanged")
    result = _faulted_enable(jobs, enable, ledger_name, "conflict")
    assert result.returncode == 1 and not result.stdout, result.stderr
    assert "conflicting native LaunchAgent definition" in result.stderr
    assert "registration retained" in result.stderr

    ledger = jobs.state / ledger_name
    path = Path(json.loads(ledger.read_text())["job"])
    loaded = home / "fake-launchd" / path.stem
    assert not loaded.exists()
    assert "changed" in _refused(jobs, disable)
    assert path.readlink() == foreign
    assert foreign.read_bytes() == b"foreign contents must survive unchanged"
    assert list(path.parent.iterdir()) == [path]

    path.unlink()
    assert _success(jobs, enable)["job"] == str(path)
    assert plistlib.loads(path.read_bytes())["ProgramArguments"][-1] == command


@JOB_TYPES
def test_enable_does_not_bootstrap_a_plist_that_appears_as_the_ledger_is_saved(
    jobs, enable, disable, ledger_name, command
):
    result = _faulted_enable(jobs, enable, ledger_name, "early-conflict")
    path = Path(json.loads((jobs.state / ledger_name).read_text())["job"])
    loaded = Path(jobs.environment["HOME"]) / "fake-launchd" / path.stem
    assert not loaded.exists()
    assert result.returncode == 1 and not result.stdout, (result.stdout, result.stderr)
    assert plistlib.loads(path.read_bytes())["ProgramArguments"] == [
        "/not-the-captured-launcher"
    ]
