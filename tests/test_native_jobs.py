"""Exercise native user-session orchestration through CLI commands, never real launchd."""

import fcntl
import json
import plistlib
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from rss_aggregator.archives import create_archive, write_data_tar
from rss_aggregator.native_install import manifest_identity
from tests.test_native_install import CLI_IN_CHECKOUT, Checkout

LAUNCHCTL = """
import json
import plistlib
import sys
from pathlib import Path

home = Path.home()
verb, domain, *rest = sys.argv[1:]
services = home / 'fake-launchd'
services.mkdir(exist_ok=True)
if verb == 'print' and domain.count('/') == 1:
    raise SystemExit(0 if not (home / 'session-unavailable').exists() else 3)
if verb == 'bootstrap':
    path = Path(rest[0])
    definition = plistlib.loads(path.read_bytes())
    label = definition['Label']
else:
    label = domain.split('/')[-1]
assert label.startswith('io.rss-aggregator.native.')
state = services / label
if verb == 'print':
    if not state.exists():
        raise SystemExit(113)
    job = json.loads(state.read_text())
    definition = job['definition']
    print('path = ' + job['path'])
    print('program = ' + definition['ProgramArguments'][0])
    print('arguments = {\\n' + '\\n'.join(definition['ProgramArguments']) + '\\n}')
    print('environment = {\\n' + '\\n'.join(k + ' => ' + v for k, v in definition['EnvironmentVariables'].items()) + '\\n}')
elif verb == 'bootstrap':
    assert not state.exists()
    state.write_text(json.dumps({'path': str(path), 'definition': definition}))
elif verb == 'bootout':
    if (home / 'bootout-refused').exists():
        raise SystemExit(5)
    state.unlink()
else:
    raise AssertionError(sys.argv)
"""


CLOCKED_CLI = f"""
import os
from datetime import datetime, timedelta
from unittest.mock import patch

from rss_aggregator import backup_schedule, supervision

instant = datetime.fromisoformat('2026-01-02T04:00:00+00:00') + timedelta(
    seconds=int(os.environ.get('SYNTHETIC_SECONDS', '0'))
)

class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return instant.astimezone(tz)

with patch.object(backup_schedule, 'datetime', Clock), patch.object(supervision.time, 'time', instant.timestamp):
    exec({CLI_IN_CHECKOUT!r})
"""


class JobCheckout(Checkout):
    def run(self, *args):
        return subprocess.run(
            (sys.executable, "-c", CLOCKED_CLI, *args),
            cwd=self.root,
            env=self.environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )


def _tool(path, body):
    path.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(body)} "$@"\n'
    )
    path.chmod(0o700)


def _success(checkout, *args):
    result = checkout.run(*args)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _refused(checkout, *args):
    result = checkout.run(*args)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert not result.stdout
    return result.stderr


def _ledger(checkout, name, value):
    path = checkout.state / name
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


@contextmanager
def _owned_writer(checkout):
    nonce = "synthetic-writer"
    _ledger(
        checkout,
        "service.json",
        {"nonce": nonce, "url": "http://127.0.0.1:8080", "port": 8080},
    )
    _ledger(checkout, "service-status.json", {"nonce": nonce, "web": "started"})
    stop = checkout.state / "stop-service.json"
    with (
        (checkout.state / "writers.lock").open("a") as lease,
        (checkout.state / "controller.lock").open("a") as controller,
    ):
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(controller, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def settle_at_stop():
            deadline = time.monotonic() + 10
            while not stop.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            acknowledged = stop.exists() and json.loads(stop.read_text()) == {
                "nonce": nonce
            }
            fcntl.flock(controller, fcntl.LOCK_UN)
            fcntl.flock(lease, fcntl.LOCK_UN)
            return acknowledged

        with ThreadPoolExecutor() as executor:
            release = executor.submit(settle_at_stop)
            yield
            assert release.result()


def _restore_fixture(checkout, *instances):
    fixture = checkout.root / "synthetic-data"
    fixture.mkdir(mode=0o700)
    data = fixture / "feed-cache"
    data.write_bytes(b"synthetic reading state")
    data.chmod(0o600)
    destination = Path(checkout.environment["RSS_BACKUP_DIR"])
    payload = write_data_tar(fixture, destination)
    archive = create_archive(
        payload, destination, runtime=manifest_identity(checkout.manifest)
    )
    for instance in instances:
        assert (
            _success(checkout, "restore", archive["archive"], "--instance", instance)[
                "selected"
            ]
            is False
        )


@pytest.fixture
def jobs(tmp_path):
    checkout = JobCheckout(tmp_path)
    checkout.prepare_source()
    checkout.prepare_runtime()
    checkout.environment["SYNTHETIC_SECRET"] = "do-not-capture"
    _tool(tmp_path / "launchctl", LAUNCHCTL)
    launcher = checkout.root / ".venv/bin/python"
    launcher.parent.mkdir(parents=True)
    _tool(
        launcher,
        f"import sys\nassert sys.argv[1:4] == ['-I', '-B', '-c']\nprint({str(checkout.root)!r})",
    )
    _success(checkout, "setup")
    return checkout


def test_native_jobs_are_independent_per_user_state_and_never_adopt_legacy(jobs):
    home = Path(jobs.environment["HOME"])
    directory = home / "Library/LaunchAgents"
    directory.mkdir(parents=True)
    legacy = [
        directory / f"io.rss-aggregator.{kind}.plist"
        for kind in ("supervision", "backup")
    ]
    for path in legacy:
        path.write_bytes(b"legacy fixture must remain byte-exact")
    assert _success(jobs, "supervision-status")["state"] == "disabled"
    assert _success(jobs, "backup-due")["outcome"] == "disabled"

    supervisor = Path(_success(jobs, "supervision-install")["job"])
    assert _success(jobs, "supervision-tick")["state"] == "intent_unset"
    assert not _success(jobs, "backup-status")["enabled"]
    daily = Path(_success(jobs, "backup-enable", "--hour", "0")["job"])
    assert supervisor != daily
    assert len(supervisor.stem.split(".")[-1]) == 64
    assert supervisor.stem.split(".")[-1] == daily.stem.split(".")[-1]
    for path, command, interval in [
        (supervisor, "supervision-tick", 60),
        (daily, "backup-due", 900),
    ]:
        definition = plistlib.loads(path.read_bytes())
        assert definition["Label"] == path.stem
        assert definition["AbandonProcessGroup"] is True
        assert definition["StartInterval"] == interval
        assert definition["ProgramArguments"][1:] == [
            "-I",
            "-B",
            "-m",
            "rss_aggregator",
            command,
        ]
        assert definition["EnvironmentVariables"]["RSS_STATE_DIR"] == str(jobs.state)
        assert "SYNTHETIC_SECRET" not in definition["EnvironmentVariables"]
        assert not path.stat().st_mode & 0o077
    assert _success(jobs, "supervision-install")["job"] == str(supervisor)

    first_state = jobs.state
    jobs.state = home / "other-state"
    jobs.environment["RSS_STATE_DIR"] = str(jobs.state)
    _success(jobs, "setup")
    other = Path(_success(jobs, "supervision-install")["job"])
    assert other not in [supervisor, daily]
    assert all(path.is_file() for path in [other, supervisor, daily, *legacy])
    _success(jobs, "supervision-remove")
    assert not other.exists() and supervisor.exists() and daily.exists()

    jobs.state = first_state
    jobs.environment["RSS_STATE_DIR"] = str(first_state)
    assert _success(jobs, "backup-disable")["archives_preserved"] is True
    assert _success(jobs, "supervision-tick")["state"] == "intent_unset"
    _success(jobs, "supervision-remove")
    for path in legacy:
        assert path.read_bytes() == b"legacy fixture must remain byte-exact"


def test_owned_jobs_refuse_loaded_conflicts_and_keep_ledgers_on_removal_failure(jobs):
    home = Path(jobs.environment["HOME"])
    supervisor = Path(_success(jobs, "supervision-install")["job"])
    daily = Path(_success(jobs, "backup-enable")["job"])
    for path, options in [
        (supervisor, ["supervision-remove"]),
        (daily, ["backup-disable"]),
    ]:
        loaded = home / "fake-launchd" / path.stem
        original = loaded.read_bytes()
        changed = json.loads(original)
        changed["definition"]["EnvironmentVariables"]["RSS_PORT"] = "8029"
        loaded.write_text(json.dumps(changed))
        assert "foreign/changed loaded native job" in _refused(jobs, *options)
        assert path.is_file()
        assert json.loads(loaded.read_bytes()) == changed

        loaded.write_bytes(original)
        (home / "bootout-refused").touch()
        assert "retained" in _refused(jobs, *options)
        assert loaded.read_bytes() == original
        assert path.is_file()
        (home / "bootout-refused").unlink()
        _success(jobs, *options)
        assert not path.exists() and not loaded.exists()


@pytest.mark.parametrize("drift", ["launcher", "manifest"])
def test_native_jobs_refuse_pin_drift_but_owned_removal_remains_possible(jobs, drift):
    supervisor = Path(_success(jobs, "supervision-install")["job"])
    daily = Path(_success(jobs, "backup-enable", "--hour", "0")["job"])
    if drift == "launcher":
        target = jobs.root / ".venv/bin/python"
    else:
        target = jobs.manifest_path
    target.write_bytes(target.read_bytes() + b"\n")
    assert _success(jobs, "supervision-status")["state"] == "configuration_unavailable"
    assert "configuration_unavailable" in _refused(jobs, "supervision-tick")
    assert _success(jobs, "backup-status")["scheduler_problem"]
    assert "nothing started" in _refused(jobs, "backup-due")
    assert not (jobs.state / "backup-daily.json").exists()
    assert _success(jobs, "backup-status")["latest_attempt"]["outcome"] == "failed"
    _success(jobs, "backup-disable")
    _success(jobs, "supervision-remove")
    assert not supervisor.exists() and not daily.exists()


def test_supervision_reserves_bounded_retries_and_explicit_start_resets_budget(jobs):
    _success(jobs, "supervision-install")
    _refused(jobs, "start")
    for elapsed, attempt in [(0, 1), (60, 2), (360, 3)]:
        jobs.environment["SYNTHETIC_SECONDS"] = str(elapsed)
        assert "Guarded recovery failed" in _refused(jobs, "supervision-tick")
        status = _success(jobs, "supervision-status")
        assert status["intent"] == "running"
        assert status["recovery"]["attempts"] == attempt
        assert status["recovery"]["history"][-1]["outcome"] == "failed"
        reason = "backoff active" if attempt < 3 else "retry_exhausted"
        assert reason in _refused(jobs, "supervision-tick")
    _refused(jobs, "start")
    budget = _success(jobs, "supervision-status")["recovery"]
    assert budget["attempts"] == 0 and budget["next_retry_at"] is None
    assert len(budget["history"]) == 3


def test_reconciliation_is_read_only_until_idle_and_never_changes_intent(jobs):
    owner = json.loads((jobs.state / "native-owner.json").read_text())
    _success(jobs, "supervision-install")
    # Simulate interruption with retained intent and a kernel lease still held by a writer.
    intent = _ledger(jobs, "operator-intent.json", {"version": 1, "intent": "running"})
    marker = _ledger(
        jobs,
        "maintenance.json",
        {
            "version": 1,
            "owner": owner["token"],
            "operation": "backup",
            "phase": "quiescing",
        },
    )
    before = marker.read_bytes()
    previous = intent.read_bytes()
    assert _success(jobs, "supervision-status")["state"] == "maintenance"
    assert "maintenance" in _refused(jobs, "supervision-tick")
    with (jobs.state / "writers.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert "rss stop" in _refused(jobs, "backup-reconcile")
        assert marker.read_bytes() == before
        assert intent.read_bytes() == previous
        assert not (jobs.state / "stop-service.json").exists()
    assert _success(jobs, "backup-reconcile")["resumed"] is False
    assert not marker.exists()
    assert intent.read_bytes() == previous
    assert _success(jobs, "status")["application"] == "stopped"


def test_explicit_stop_settles_interrupted_writers_and_retains_stopped_intent(jobs):
    owner = json.loads((jobs.state / "native-owner.json").read_text())
    _success(jobs, "supervision-install")
    marker = _ledger(
        jobs,
        "maintenance.json",
        {
            "version": 1,
            "owner": owner["token"],
            "operation": "backup",
            "phase": "quiescing",
        },
    )
    _ledger(jobs, "operator-intent.json", {"version": 1, "intent": "running"})
    with _owned_writer(jobs):
        assert "backup-reconcile" in _refused(jobs, "stop")
    assert marker.is_file()
    assert _success(jobs, "supervision-status")["intent"] == "stopped"
    assert _success(jobs, "backup-reconcile")["resumed"] is False
    assert _success(jobs, "supervision-tick")["state"] == "intentionally_stopped"
    assert _success(jobs, "status")["application"] == "stopped"
    assert _success(jobs, "backup-status")["latest_attempt"] is None


def test_reconcile_refuses_active_owned_route_even_with_idle_writers(jobs):
    owner = json.loads((jobs.state / "native-owner.json").read_text())
    marker = _ledger(
        jobs,
        "maintenance.json",
        {
            "version": 1,
            "owner": owner["token"],
            "operation": "backup",
            "phase": "resuming",
        },
    )
    host = "synthetic.invalid.ts.net"
    target = "http://127.0.0.1:8080"
    _ledger(
        jobs,
        "serve.json",
        {"owner": owner["token"], "phase": "active", "host": host, "target": target},
    )
    route = {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {host + ":443": {"Handlers": {"/": {"Proxy": target}}}},
    }
    _tool(
        Path(jobs.environment["HOME"]) / "tailscale",
        f"import json, sys\nassert sys.argv[1:] == ['serve', 'status', '--json']\nprint(json.dumps({route!r}))",
    )
    assert "active writers or forwarding" in _refused(jobs, "backup-reconcile")
    assert marker.is_file()
    assert not (jobs.state / "operator-intent.json").exists()


def test_daily_failed_attempt_is_not_protection_and_does_not_resume_or_retry_early(
    jobs,
):
    _success(jobs, "backup-enable", "--hour", "0")
    with (jobs.state / "command.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert "Another project command" in _refused(jobs, "backup-due")
        busy = _success(jobs, "backup-status")
        assert busy["busy"] is True
        assert busy["latest_defer"]["outcome"] == "deferred"
        assert busy["latest_attempt"] is None
    assert "Daily backup failed" in _refused(jobs, "backup-due")
    first = _success(jobs, "backup-status")
    assert first["registered"] is True
    assert first["daily_attempt"]["count"] == 1
    assert first["latest_attempt"]["outcome"] == "failed"
    assert first["latest_success"] is None
    assert first["resumption"] == "preserved"
    assert first["protected"] is False
    assert _success(jobs, "backup-due")["outcome"] == "not_due"
    assert _success(jobs, "status")["application"] == "stopped"
    assert not (jobs.state / "operator-intent.json").exists()


def test_daily_archive_publication_remains_visible_when_resumption_fails(jobs):
    _restore_fixture(jobs, "first", "second")
    _success(jobs, "select-instance", "first")
    _success(jobs, "backup-enable", "--hour", "0")
    first = _success(jobs, "backup-due")["backup"]
    assert first["verified"] is True
    assert _success(jobs, "backup-status")["protected"] is True
    previous = Path(first["archive"]).read_bytes()
    assert _success(jobs, "status")["application"] == "stopped"

    _success(jobs, "select-instance", "second")
    _ledger(jobs, "operator-intent.json", {"version": 1, "intent": "running"})
    # The synthetic FrankenPHP reports its approved version but refuses PHP work.
    with _owned_writer(jobs):
        assert "Daily backup failed" in _refused(jobs, "backup-due")
    report = _success(jobs, "backup-status")
    assert report["latest_success"]["verified"] is True
    assert report["latest_success"]["instance"] == "second"
    assert report["latest_attempt"]["outcome"] == "failed"
    assert report["daily_attempt"]["outcome"] == "failed"
    assert report["resumption"] == "failed"
    assert report["maintenance_required"] is True
    assert report["protected"] is False
    assert Path(first["archive"]).read_bytes() == previous
    assert _success(jobs, "backup-reconcile")["resumed"] is False
    assert _success(jobs, "status")["application"] == "stopped"
