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
from tests.test_native_install import CHECKOUT_IMPORT, CLI_IN_CHECKOUT, Checkout

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


CLOCKED_CLI = (
    CHECKOUT_IMPORT
    + f"""
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from rss_aggregator import backup_schedule, recovery, supervision

instant = datetime.fromisoformat('2026-01-02T04:00:00+00:00') + timedelta(
    seconds=int(os.environ.get('SYNTHETIC_SECONDS', '0'))
)

class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return instant.astimezone(tz)

unlink = Path.unlink
replace = os.replace

def fault_at_publication(source, target, *args, **kwargs):
    if os.environ.get('SYNTHETIC_REPLACE_FAILURE') == Path(target).name:
        raise PermissionError('synthetic record publication failure')
    replace(source, target, *args, **kwargs)
    if os.environ.get('SYNTHETIC_EXIT_AFTER_REPLACE') == Path(target).name:
        os._exit(71)

def fail_requested_unlink(path, *args, **kwargs):
    target = os.environ.get('SYNTHETIC_UNLINK_FAILURE')
    if target == str(path):
        raise PermissionError('synthetic unlink failure')
    return unlink(path, *args, **kwargs)

with (
    patch.object(backup_schedule, 'datetime', Clock),
    patch.object(recovery, 'datetime', Clock),
    patch.object(supervision.time, 'time', instant.timestamp),
    patch.object(Path, 'unlink', fail_requested_unlink),
    patch.object(os, 'replace', fault_at_publication),
):
    exec({CLI_IN_CHECKOUT!r})
"""
)


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
def _owned_writer(checkout, *, instance=None):
    nonce = "e" * 32
    selection = checkout.state / "selected-instance.json"
    if instance is None:
        instance = (
            json.loads(selection.read_text())["instance"]
            if selection.exists()
            else "default"
        )
    _ledger(
        checkout,
        "service.json",
        {
            "nonce": nonce,
            "instance": instance,
            "url": "http://127.0.0.1:8080",
            "port": 8080,
            "private": False,
        },
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
    for name in tuple(checkout.environment):
        if name.startswith("SYNTHETIC_"):
            del checkout.environment[name]
    checkout.environment.update(
        RSS_PORT="8080", SYNTHETIC_SECONDS="0", SYNTHETIC_SECRET="do-not-capture"
    )
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
    library = home / "Library"
    library.mkdir(mode=0o700)
    directory = library / "LaunchAgents"
    directory.mkdir(mode=0o700)
    legacy = [
        directory / f"io.rss-aggregator.{kind}.plist"
        for kind in ("supervision", "backup")
    ]
    for path in legacy:
        path.write_bytes(b"legacy fixture must remain byte-exact")
        path.chmod(0o600)
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
        environment = changed["definition"]["EnvironmentVariables"]
        environment["RSS_PORT"] = str(int(environment["RSS_PORT"]) + 1)
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


def test_backup_disable_without_plist_requires_owned_ledger_and_loaded_identity(jobs):
    plist = Path(_success(jobs, "backup-enable")["job"])
    loaded = Path(jobs.environment["HOME"]) / "fake-launchd" / plist.stem
    ledger = jobs.state / "backup-schedule.json"
    plist.unlink()
    original_ledger = ledger.read_bytes()
    for change, diagnostic in [
        ({"owner": "foreign"}, "ownership"),
        ({"hour": 4}, "ledger"),
    ]:
        ledger.write_text(json.dumps({**json.loads(original_ledger), **change}))
        assert diagnostic in _refused(jobs, "backup-disable")
        assert loaded.is_file()
    ledger.write_bytes(original_ledger)

    original_loaded = loaded.read_bytes()
    changed_loaded = json.loads(original_loaded)
    changed_loaded["path"] = str(plist.with_name("other.plist"))
    loaded.write_text(json.dumps(changed_loaded))
    assert "foreign/changed loaded native job" in _refused(jobs, "backup-disable")
    assert json.loads(loaded.read_bytes()) == changed_loaded
    loaded.write_bytes(original_loaded)

    assert _success(jobs, "backup-disable")["archives_preserved"] is True
    assert not loaded.exists()
    assert _success(jobs, "backup-status")["enabled"] is False
    assert _success(jobs, "backup-disable")["enabled"] is False


@pytest.mark.parametrize("stage", ["plist", "ledger"])
def test_backup_disable_retries_an_unlink_failure_without_touching_foreign_file(
    jobs, stage
):
    plist = Path(_success(jobs, "backup-enable")["job"])
    loaded = Path(jobs.environment["HOME"]) / "fake-launchd" / plist.stem
    ledger = jobs.state / "backup-schedule.json"
    plist_contents = plist.read_bytes()
    jobs.environment["SYNTHETIC_UNLINK_FAILURE"] = str(
        plist if stage == "plist" else ledger
    )
    assert "synthetic unlink failure" in _refused(jobs, "backup-disable")
    assert not loaded.exists()
    assert plist.exists() is (stage == "plist")
    assert ledger.is_file()
    del jobs.environment["SYNTHETIC_UNLINK_FAILURE"]

    plist.write_bytes(b"unrelated definition at the same path")
    assert "changed" in _refused(jobs, "backup-disable")
    assert plist.read_bytes() == b"unrelated definition at the same path"
    assert ledger.is_file()
    if stage == "plist":
        plist.write_bytes(plist_contents)
    else:
        plist.unlink()
    assert _success(jobs, "backup-disable")["archives_preserved"] is True
    assert not plist.exists() and not ledger.exists()


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


def test_backup_status_keeps_evidence_when_the_runtime_layout_drifts(jobs):
    _restore_fixture(jobs, "source")
    _success(jobs, "select-instance", "source")
    _success(jobs, "backup-enable", "--hour", "4")
    archive = _success(jobs, "backup-due")["backup"]
    assert _success(jobs, "backup-status")["protected"] is True
    (jobs.state / "runtime/unexpected").write_text("preserve layout drift")
    damaged = _success(jobs, "backup-status")
    assert damaged["latest_attempt"]["outcome"] == "success"
    assert damaged["protected"] is False and damaged["scheduler_problem"]
    _refused(jobs, "stop")

    evidence = _success(jobs, "backup-status")
    assert evidence["enabled"] is True
    assert evidence["latest_attempt"]["outcome"] == "failed"
    assert evidence["latest_success"]["sha256"] == archive["sha256"]
    assert evidence["archive_available"] is True
    assert evidence["protected"] is False
    assert evidence["scheduler_problem"]
    check = jobs.run("backup-status", "--check")
    assert check.returncode == 1, check.stderr
    assert json.loads(check.stdout) == evidence


@pytest.mark.parametrize("fault", ["quiescence_failure", "interruption"])
def test_failed_shutdown_stays_unprotected_until_verified_retry(jobs, fault):
    _restore_fixture(jobs, "reader")
    _success(jobs, "select-instance", "reader")
    _success(jobs, "backup-enable", "--hour", "4")
    _success(jobs, "backup-due")
    before = _success(jobs, "backup-status", "--check")
    archives = set(jobs.backups.glob("*.zip"))

    if fault == "quiescence_failure":
        # An unreadable forwarding record prevents withdrawal, not stopping writers.
        serve_record = _ledger(jobs, "serve.json", {})
        with _owned_writer(jobs):
            _refused(jobs, "stop")
        serve_record.unlink()
        outcome = "failed"
    else:
        # A process loss immediately after the writer stop request leaves no
        # opportunity to enter capture or run exception cleanup.
        jobs.environment["SYNTHETIC_EXIT_AFTER_REPLACE"] = "stop-service.json"
        with _owned_writer(jobs):
            assert jobs.run("stop").returncode == 71
        del jobs.environment["SYNTHETIC_EXIT_AFTER_REPLACE"]
        outcome = "interrupted"

    failed = _success(jobs, "backup-status")
    assert failed["maintenance_required"] is True
    assert failed["latest_attempt"] == before["latest_attempt"]
    assert failed["latest_success"] == before["latest_success"]
    assert failed["unresolved_shutdown"]["outcome"] == outcome
    assert set(jobs.backups.glob("*.zip")) == archives
    assert _success(jobs, "backup-reconcile")["resumed"] is False

    _success(jobs, "backup")
    jobs.environment["SYNTHETIC_SECONDS"] = "86400"
    daily = _success(jobs, "backup-due")["backup"]
    check = jobs.run("backup-status", "--check")
    assert check.returncode == 1, check.stdout
    report = json.loads(check.stdout)
    assert report["maintenance_required"] is False
    assert report["latest_success"]["archive"] == daily["archive"]
    assert report["latest_attempt"]["outcome"] == "success"
    assert report["unresolved_shutdown"]["outcome"] == outcome
    assert report["protected"] is False

    retry = _success(jobs, "stop")["backup"]
    assert retry["verified"] is True and retry["archive"] != daily["archive"]
    report = _success(jobs, "backup-status", "--check")
    assert report["latest_success"]["archive"] == retry["archive"]
    assert report["unresolved_shutdown"] is None
    assert report["protected"] is True


@pytest.mark.parametrize("advance_first_generation", [False, True])
def test_only_the_affected_instance_shutdown_resolves_failed_capture(
    jobs, advance_first_generation
):
    _restore_fixture(jobs, "first", "second")
    _success(jobs, "select-instance", "first")
    _refused(jobs, "start")  # The disposable PHP stub refuses application work.
    _success(jobs, "backup-enable", "--hour", "4")
    _success(jobs, "backup-due")
    initial = _success(jobs, "backup-status", "--check")
    generation = initial["latest_success"]["writer_generation"]
    assert generation is not None

    destination = jobs.environment.pop("RSS_BACKUP_DIR")
    _refused(jobs, "stop")
    _success(jobs, "select-instance", "second")
    _refused(jobs, "stop")
    both = _success(jobs, "backup-status")
    jobs.environment["RSS_BACKUP_DIR"] = destination
    assert _success(jobs, "stop")["backup"]["verified"] is True
    _success(jobs, "select-instance", "first")
    if advance_first_generation:
        _refused(jobs, "start")
    manual = _success(jobs, "backup")
    check = jobs.run("backup-status", "--check")
    assert check.returncode == 1, check.stdout
    report = json.loads(check.stdout)
    assert report["latest_success"]["archive"] == manual["archive"]
    captured_generation = report["latest_success"]["writer_generation"]
    assert captured_generation is not None
    assert (captured_generation != generation) is advance_first_generation
    assert report["unresolved_shutdown"]["instance"] == "first"
    assert report["unresolved_shutdown"]["writer_generation"] == generation
    assert [r["instance"] for r in report["unresolved_shutdowns"]] == ["first"]
    assert {r["instance"] for r in both["unresolved_shutdowns"]} == {"first", "second"}

    assert _success(jobs, "stop")["backup"]["verified"] is True
    healthy = _success(jobs, "backup-status", "--check")
    assert healthy["unresolved_shutdown"] is None
    assert healthy["unresolved_shutdowns"] == []


def test_reconcile_retains_other_shutdowns_when_stop_ledger_write_fails(jobs):
    _restore_fixture(jobs, "first", "second")
    _success(jobs, "select-instance", "first")
    destination = jobs.environment.pop("RSS_BACKUP_DIR")
    _refused(jobs, "stop")
    jobs.environment["RSS_BACKUP_DIR"] = destination
    _success(jobs, "select-instance", "second")
    jobs.environment["SYNTHETIC_REPLACE_FAILURE"] = "backup-shutdown.json"
    with _owned_writer(jobs):
        _refused(jobs, "stop")
    del jobs.environment["SYNTHETIC_REPLACE_FAILURE"]

    assert _success(jobs, "backup-reconcile")["resumed"] is False
    report = _success(jobs, "backup-status")
    assert [
        (item["instance"], item["outcome"]) for item in report["unresolved_shutdowns"]
    ] == [
        ("second", "interrupted"),
        ("first", "failed"),
    ]
    assert _success(jobs, "stop")["backup"]["verified"] is True
    remaining = _success(jobs, "backup-status")["unresolved_shutdowns"]
    assert [item["instance"] for item in remaining] == ["first"]


def test_shutdown_cannot_clear_a_known_generation_if_identity_was_lost(jobs):
    _restore_fixture(jobs, "reader")
    _success(jobs, "select-instance", "reader")
    _refused(jobs, "start")  # The disposable PHP stub refuses application work.
    _success(jobs, "backup-enable", "--hour", "4")
    _success(jobs, "backup-due")
    healthy = _success(jobs, "backup-status", "--check")
    generation = healthy["latest_success"]["writer_generation"]
    assert generation is not None
    destination = jobs.environment.pop("RSS_BACKUP_DIR")
    _refused(jobs, "stop")
    jobs.environment["RSS_BACKUP_DIR"] = destination
    (jobs.state / "instances/reader/writer-generation.json").unlink()

    assert "shutdown identity could not be confirmed" in _refused(jobs, "stop")
    check = jobs.run("backup-status", "--check")
    assert check.returncode == 1, check.stdout
    report = json.loads(check.stdout)
    assert report["latest_success"]["via"] == "stop"
    assert report["unresolved_shutdown"]["writer_generation"] == generation


@pytest.mark.parametrize("historical", [False, True])
def test_unidentified_shutdown_is_not_cleared_by_a_verified_capture(jobs, historical):
    _restore_fixture(jobs, "reader")
    _success(jobs, "select-instance", "reader")
    _success(jobs, "backup-enable", "--hour", "4")
    _success(jobs, "backup-due")
    if historical:
        owner = json.loads((jobs.state / "native-owner.json").read_text())
        _ledger(
            jobs,
            "backup-shutdown.json",
            {
                "version": 1,
                "owner": owner["token"],
                "started_at": "2026-01-02T04:00:00+00:00",
                "outcome": "failed",
            },
        )
    else:
        (jobs.state / "selected-instance.json").write_text("{")
        with _owned_writer(jobs, instance=""):
            _refused(jobs, "stop")
        _success(jobs, "select-instance", "reader")

    assert _success(jobs, "stop")["backup"]["verified"] is True
    check = jobs.run("backup-status", "--check")
    assert check.returncode == 1, check.stdout
    report = json.loads(check.stdout)
    assert report["latest_success"]["via"] == "stop"
    assert report["unresolved_shutdown"]["instance"] is None
    assert report["unresolved_shutdown"]["outcome"] == "failed"
    assert "inspect" in report["unresolved_shutdown"]["action"].lower()
    assert report["evidence_problems"] == {}
    assert report["protected"] is False


def test_owned_writer_identifies_failed_stop_when_selection_is_unreadable(jobs):
    _restore_fixture(jobs, "reader")
    _success(jobs, "select-instance", "reader")
    _success(jobs, "backup-enable", "--hour", "4")
    _success(jobs, "backup-due")
    (jobs.state / "selected-instance.json").write_text("{")
    with _owned_writer(jobs, instance="reader"):
        _refused(jobs, "stop")
    report = _success(jobs, "backup-status")
    assert report["unresolved_shutdown"]["instance"] == "reader"
    assert report["protected"] is False

    _success(jobs, "select-instance", "reader")
    assert _success(jobs, "stop")["backup"]["verified"] is True
    assert _success(jobs, "backup-status", "--check")["unresolved_shutdown"] is None


def test_interrupted_shutdown_reports_archive_published_before_finalization(jobs):
    _restore_fixture(jobs, "reader")
    _success(jobs, "select-instance", "reader")
    _success(jobs, "backup-enable", "--hour", "4")
    daily = _success(jobs, "backup-due")["backup"]
    jobs.environment["SYNTHETIC_EXIT_AFTER_REPLACE"] = "shutdown-backup-success.json"
    assert jobs.run("stop").returncode == 71
    del jobs.environment["SYNTHETIC_EXIT_AFTER_REPLACE"]

    before = _success(jobs, "backup-status")
    assert before["latest_success"]["via"] == "stop"
    assert before["latest_success"]["archive"] != daily["archive"]
    assert before["archive_available"] is True
    assert before["latest_attempt"]["outcome"] == "interrupted"
    assert before["unresolved_shutdown"]["outcome"] == "interrupted"
    assert _success(jobs, "backup-reconcile")["resumed"] is False
    after = _success(jobs, "backup-status")
    assert after["latest_success"] == before["latest_success"]
    assert after["unresolved_shutdown"] == before["unresolved_shutdown"]
    assert after["protected"] is False


def test_failed_stop_intent_write_quiesces_and_blocks_supervision(jobs):
    _restore_fixture(jobs, "reader")
    _success(jobs, "select-instance", "reader")
    _success(jobs, "supervision-install")
    _success(jobs, "backup-enable", "--hour", "4")
    before = _success(jobs, "backup-due")["backup"]
    _success(jobs, "backup-status", "--check")
    _ledger(jobs, "operator-intent.json", {"version": 1, "intent": "running"})
    with (jobs.state / "command.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert "Another project command" in _refused(jobs, "stop")
    assert _success(jobs, "supervision-status")["intent"] == "running"
    _success(jobs, "backup-status", "--check")

    jobs.environment["SYNTHETIC_REPLACE_FAILURE"] = "operator-intent.json"
    with _owned_writer(jobs):
        diagnostic = _refused(jobs, "stop")
    del jobs.environment["SYNTHETIC_REPLACE_FAILURE"]
    assert "running intent may still be in force" in diagnostic
    assert _success(jobs, "status")["application"] == "stopped"
    supervision = _success(jobs, "supervision-status")
    assert supervision["intent"] == "running" and supervision["state"] == "maintenance"
    assert "maintenance" in _refused(jobs, "supervision-tick")
    check = jobs.run("backup-status", "--check")
    assert check.returncode == 1, check.stdout
    report = json.loads(check.stdout)
    assert report["latest_success"]["archive"] == before["archive"]
    assert report["maintenance_required"] is True
    assert report["unresolved_shutdown"]["outcome"] == "failed"
    assert report["protected"] is False

    assert "backup-reconcile" in _refused(jobs, "stop")
    assert _success(jobs, "supervision-status")["intent"] == "stopped"
    assert _success(jobs, "backup-reconcile")["resumed"] is False
    assert _success(jobs, "supervision-tick")["state"] == "intentionally_stopped"
    assert _success(jobs, "stop")["backup"]["verified"] is True
    _success(jobs, "backup-status", "--check")


@pytest.mark.parametrize("ledger", ["backup-shutdown.json", "maintenance.json"])
def test_shutdown_bookkeeping_write_failure_still_quiesces_and_is_visible(jobs, ledger):
    _restore_fixture(jobs, "reader")
    _success(jobs, "select-instance", "reader")
    _success(jobs, "backup-enable", "--hour", "4")
    before = _success(jobs, "backup-due")["backup"]
    jobs.environment["SYNTHETIC_REPLACE_FAILURE"] = ledger
    with _owned_writer(jobs):
        assert "synthetic record publication failure" in _refused(jobs, "stop")
    del jobs.environment["SYNTHETIC_REPLACE_FAILURE"]

    held = ledger == "backup-shutdown.json"
    assert _success(jobs, "backup-status")["maintenance_required"] is held
    assert _success(jobs, "backup-reconcile")["reconciled"] is held
    check = jobs.run("backup-status", "--check")
    assert check.returncode == 1, check.stdout
    report = json.loads(check.stdout)
    assert report["unresolved_shutdown"]["outcome"] == (
        "interrupted" if held else "failed"
    )
    assert report["latest_success"]["archive"] == before["archive"]


def test_no_data_shutdown_does_not_clear_an_earlier_failure(jobs):
    assert _success(jobs, "stop")["backup"] == {"status": "no_data"}
    serve_record = _ledger(jobs, "serve.json", {})
    _refused(jobs, "stop")
    serve_record.unlink()
    assert _success(jobs, "backup-reconcile")["reconciled"] is True
    assert _success(jobs, "stop")["backup"] == {"status": "no_data"}
    report = _success(jobs, "backup-status")
    assert report["latest_attempt"]["outcome"] == "no_data"
    assert report["unresolved_shutdown"]["outcome"] == "failed"
    assert report["protected"] is False


@pytest.mark.parametrize(
    ("record", "damage"),
    [
        ("backup-attempt.json", "{"),
        ("backup-attempt.json", "null"),
        ("backup-attempt.json", '{"outcome": "success"}'),
        ("backup-success.json", '{"completed_at": "2026-01-02T04:00:00"}'),
        ("backup-daily.json", "null"),
        ("backup-defer.json", "{"),
    ],
)
def test_status_returns_readable_backup_evidence_despite_damage(jobs, record, damage):
    _restore_fixture(jobs, "reader")
    _success(jobs, "select-instance", "reader")
    _success(jobs, "backup-enable", "--hour", "4")
    _success(jobs, "backup-due")
    healthy = _success(jobs, "backup-status", "--check")
    corrupted = jobs.state / record
    corrupted.write_text(damage)

    report = _success(jobs, "backup-status")
    assert record in report["evidence_problems"]
    assert report["protected"] is False
    assert report["daily_archive_available"] is (record != "backup-daily.json")
    if record == "backup-attempt.json":
        assert report["latest_attempt"] is None
        assert report["latest_success"] == healthy["latest_success"]
        assert report["archive_available"] is True
        with _owned_writer(jobs):
            assert "writers remain stopped" in _refused(jobs, "stop")
        _refused(jobs, "backup")
        report = _success(jobs, "backup-status")
        assert report["unresolved_shutdown"]["outcome"] == "failed"
        assert report["latest_success"] == healthy["latest_success"]
    elif record == "backup-success.json":
        assert report["latest_attempt"] == healthy["latest_attempt"]
        assert report["latest_success"] is None
        assert report["age_seconds"] is None
        assert report["archive_available"] is False
    else:
        unavailable = {
            "backup-daily.json": "daily_attempt",
            "backup-defer.json": "latest_defer",
        }[record]
        assert report[unavailable] is None
        assert report["latest_attempt"] == healthy["latest_attempt"]
        assert report["latest_success"] == healthy["latest_success"]
    check = jobs.run("backup-status", "--check")
    assert check.returncode == 1, check.stderr
    assert json.loads(check.stdout) == report
    assert corrupted.read_text() == damage


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


def test_reconcile_releases_only_owned_idle_hold_when_runtime_is_missing(jobs):
    owner = json.loads((jobs.state / "native-owner.json").read_text())
    release = jobs.state / "runtime"
    parked = jobs.state / "runtime-retained"
    release.rename(parked)
    hold = {
        "version": 1,
        "owner": "unrecognized",
        "operation": "backup",
        "phase": "quiescing",
    }
    marker = _ledger(jobs, "maintenance.json", hold)
    assert "Unrecognized maintenance owner" in _refused(jobs, "backup-reconcile")
    assert json.loads(marker.read_text()) == hold
    _ledger(jobs, "maintenance.json", {**hold, "owner": owner["token"]})
    # Malformed capture history must not block recovery of an independently owned hold.
    history = jobs.state / "backup-attempt.json"
    history.write_text("{")
    history.chmod(0o600)
    assert "Maintenance is unresolved" in _refused(jobs, "setup")
    assert _success(jobs, "backup-reconcile")["reconciled"] is True
    assert not marker.exists() and not release.exists() and parked.is_dir()
    assert not (jobs.state / "operator-intent.json").exists()
    assert history.read_text() == "{"
    _success(jobs, "setup")
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
    report = _success(jobs, "backup-status")
    assert report["latest_attempt"] is None
    assert report["unresolved_shutdown"]["outcome"] == "failed"


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


def test_end_of_day_slot_is_caught_after_midnight_and_superseded_by_next_slot(jobs):
    _restore_fixture(jobs, "reader")
    _success(jobs, "select-instance", "reader")
    jobs.environment["SYNTHETIC_SECONDS"] = "71520"  # Jan 2, 23:52 UTC
    enabled = _success(jobs, "backup-enable", "--hour", "23", "--minute", "59")
    target = "2026-01-02T23:59:00+00:00"
    assert enabled["next_expected_attempt"] == target
    assert _success(jobs, "backup-due")["outcome"] == "not_due"
    assert _success(jobs, "backup-status")["daily_attempt"] is None

    jobs.environment["SYNTHETIC_SECONDS"] = "72420"  # Jan 3, 00:07 UTC
    report = _success(jobs, "backup-status")
    assert report["daily_due"] is True
    assert report["next_expected_attempt"] == target
    assert _success(jobs, "backup-due")["outcome"] == "success"
    assert _success(jobs, "backup-status", "--check")["daily_attempt"]["slot"] == target
    assert _success(jobs, "backup-due")["outcome"] == "not_due"

    for elapsed, age, overdue in [(158820, 86400, False), (169620, 97200, True)]:
        jobs.environment["SYNTHETIC_SECONDS"] = str(elapsed)
        freshness = _success(jobs, "backup-status")
        assert freshness["age_seconds"] == age
        assert freshness["overdue"] is overdue

    jobs.environment["SYNTHETIC_SECONDS"] = "332220"  # Jan 6, 00:17 UTC
    report = _success(jobs, "backup-status")
    assert report["missed"] is True
    assert report["next_expected_attempt"] == "2026-01-05T23:59:00+00:00"
    assert _success(jobs, "backup-due")["outcome"] == "success"
    report = _success(jobs, "backup-status", "--check")
    assert report["daily_attempt"]["slot"] == "2026-01-05T23:59:00+00:00"
    assert report["daily_attempt"]["count"] == 1
    assert report["next_expected_attempt"] == "2026-01-06T23:59:00+00:00"
    assert _success(jobs, "backup-due")["outcome"] == "not_due"


def test_enabling_past_target_waits_for_first_post_registration_slot(jobs):
    enabled = _success(jobs, "backup-enable", "--hour", "3")
    target = "2026-01-03T03:00:00+00:00"
    assert enabled["next_expected_attempt"] == target
    assert _success(jobs, "backup-due") == {
        "outcome": "not_due",
        "next_expected_attempt": target,
    }
    report = _success(jobs, "backup-status")
    assert report["daily_due"] is False
    assert report["missed"] is False
    assert report["daily_attempt"] is None
    assert report["latest_attempt"] is None


def test_elapsed_slot_retries_across_midnight_but_does_not_exceed_budget(jobs):
    _success(jobs, "backup-enable", "--hour", "23", "--minute", "59")
    target = "2026-01-02T23:59:00+00:00"
    for elapsed, count, following in [
        (71940, 1, "2026-01-03T00:14:00+00:00"),
        (72840, 2, "2026-01-03T00:29:00+00:00"),
        (73740, 3, "2026-01-03T23:59:00+00:00"),
    ]:
        jobs.environment["SYNTHETIC_SECONDS"] = str(elapsed)
        assert "Daily backup failed" in _refused(jobs, "backup-due")
        report = _success(jobs, "backup-status")
        assert report["daily_attempt"]["slot"] == target
        assert report["daily_attempt"]["count"] == count
        assert report["next_expected_attempt"] == following
        assert _success(jobs, "backup-due") == {
            "outcome": "not_due",
            "next_expected_attempt": following,
        }

    jobs.environment["SYNTHETIC_SECONDS"] = "158340"  # Jan 3, 23:59 UTC
    assert "Daily backup failed" in _refused(jobs, "backup-due")
    daily = _success(jobs, "backup-status")["daily_attempt"]
    assert daily["slot"] == "2026-01-03T23:59:00+00:00"
    assert daily["count"] == 1


def test_daily_failed_attempt_is_not_protection_and_does_not_resume_or_retry_early(
    jobs,
):
    _success(jobs, "backup-enable", "--hour", "4")
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


def test_identical_recapture_requires_its_own_daily_success(jobs):
    _restore_fixture(jobs, "reader")
    _success(jobs, "select-instance", "reader")
    _success(jobs, "backup-enable", "--hour", "4")
    first = _success(jobs, "backup-due")["backup"]
    before = _success(jobs, "backup-status", "--check")
    _success(jobs, "backup-enable", "--hour", "4")
    repeated = _success(jobs, "backup-status", "--check")
    assert repeated["registration_id"] == before["registration_id"]

    _success(jobs, "backup-disable")
    _success(jobs, "backup-enable", "--hour", "4")
    result = jobs.run("backup-status", "--check")
    assert result.returncode == 1, result.stdout
    report = json.loads(result.stdout)
    assert report["registered"] is True
    assert report["registered_at"] == before["registered_at"]
    assert report["registration_id"] != before["registration_id"]
    assert report["daily_attempt"]["registration_id"] == before["registration_id"]
    assert report["daily_archive_available"] is False
    assert report["daily_attempt"]["archive"] == first["archive"]
    assert Path(first["archive"]).is_file()
    second = _success(jobs, "backup-due")["backup"]
    assert second["archive"] != first["archive"]
    assert _success(jobs, "backup-status", "--check")["protected"] is True


def test_manual_archive_cannot_replace_missing_daily_archive_in_protection(jobs):
    _restore_fixture(jobs, "reader")
    _success(jobs, "select-instance", "reader")
    _success(jobs, "backup-enable", "--hour", "4")
    daily = _success(jobs, "backup-due")["backup"]
    scheduled = jobs.environment["RSS_BACKUP_DIR"]
    manual_destination = Path(jobs.environment["HOME"]) / "manual-backups"
    manual_destination.mkdir(mode=0o700)
    jobs.environment["RSS_BACKUP_DIR"] = str(manual_destination)
    jobs.environment["SYNTHETIC_SECONDS"] = "60"
    manual = _success(jobs, "backup")
    jobs.environment["RSS_BACKUP_DIR"] = scheduled
    assert _success(jobs, "backup-status", "--check")["protected"] is True

    manual_path = Path(manual["archive"])
    moved = manual_path.with_suffix(".retained")
    manual_path.rename(moved)
    report = _success(jobs, "backup-status", "--check")
    assert report["archive_available"] is False
    assert report["daily_archive_available"] is True
    moved.rename(manual_path)

    Path(daily["archive"]).unlink()
    assert manual_path.is_file()
    result = jobs.run("backup-status", "--check")
    assert result.returncode == 1, result.stdout
    report = json.loads(result.stdout)
    assert report["archive_available"] is True
    assert report["latest_success"]["archive"] == manual["archive"]
    assert report["latest_success"]["via"] == "backup"
    assert "writer_generation" in report["latest_success"]
    assert report["daily_archive_available"] is False
    assert report["daily_attempt"]["archive"] == daily["archive"]
    assert report["daily_attempt"]["sha256"] == daily["sha256"]
    assert report["protected"] is False


@pytest.mark.parametrize("inaccessible", ["latest", "daily", "both"])
def test_inaccessible_archives_leave_independent_evidence_and_fail_check(
    jobs, inaccessible
):
    _restore_fixture(jobs, "reader")
    _success(jobs, "select-instance", "reader")
    _success(jobs, "backup-enable", "--hour", "4")
    _success(jobs, "backup-due")
    scheduled = jobs.environment["RSS_BACKUP_DIR"]
    manual_destination = Path(jobs.environment["HOME"]) / "manual-backups"
    manual_destination.mkdir(mode=0o700)
    jobs.environment["RSS_BACKUP_DIR"] = str(manual_destination)
    _success(jobs, "backup")
    jobs.environment["RSS_BACKUP_DIR"] = scheduled
    healthy = _success(jobs, "backup-status", "--check")

    daily_blocked = inaccessible in ("daily", "both")
    latest_blocked = inaccessible in ("latest", "both")
    directories = {
        "daily_attempt": (Path(scheduled), daily_blocked),
        "latest_success": (manual_destination, latest_blocked),
    }
    try:
        for path, blocked in directories.values():
            if blocked:
                path.chmod(0o600)  # Cannot traverse this disposable archive directory.
        report = _success(jobs, "backup-status")
        check = jobs.run("backup-status", "--check")
    finally:
        for path, _ in directories.values():
            path.chmod(0o700)

    assert check.returncode == 1, check.stdout
    assert json.loads(check.stdout) == report
    assert report["latest_success"] == healthy["latest_success"]
    assert report["daily_attempt"] == healthy["daily_attempt"]
    assert report["latest_attempt"] == healthy["latest_attempt"]
    assert report["archive_available"] is not latest_blocked
    assert report["daily_archive_available"] is not daily_blocked
    assert set(report["archive_problems"]) == {
        key for key, (_, blocked) in directories.items() if blocked
    }
    assert all(report["archive_problems"].values())
    assert report["evidence_problems"] == {}
    assert report["protected"] is False


def test_daily_archive_publication_remains_visible_when_resumption_fails(jobs):
    _restore_fixture(jobs, "first", "second")
    _success(jobs, "select-instance", "first")
    _success(jobs, "backup-enable", "--hour", "4")
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
