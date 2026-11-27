"""Exercise stop and upgrade evidence through the disposable native CLI."""

import fcntl
import os
from pathlib import Path

import pytest

from tests import test_native_install, test_native_jobs
from tests.test_native_jobs import _refused, _restore_fixture, _success

checkout = test_native_install.checkout
jobs = test_native_jobs.jobs


@pytest.mark.parametrize(
    ("damage", "intent_mode"),
    [("missing", 0o600), ("malformed", 0o600), ("missing", 0o660)],
)
def test_stop_preserves_restart_veto_without_a_readable_receipt(
    jobs, damage, intent_mode
):
    _restore_fixture(jobs, "reader")
    _success(jobs, "select-instance", "reader")
    _success(jobs, "supervision-install")
    _refused(jobs, "start")  # The synthetic PHP command refuses application work.
    (jobs.state / "operator-intent.json").chmod(intent_mode)
    assert _success(jobs, "supervision-status")["intent"] == "running"
    receipt = jobs.state / "native-owner.json"
    original = receipt.read_bytes()
    if damage == "missing":
        receipt.unlink()
    else:
        receipt.write_text("{")

    with (jobs.state / "writers.lock").open("rb") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = {path.name for path in jobs.state.iterdir()}
        _refused(jobs, "stop")
        assert {path.name for path in jobs.state.iterdir()} == before
        if damage == "missing":
            assert not receipt.exists()
        else:
            assert receipt.read_text() == "{"
    receipt.write_bytes(original)
    receipt.chmod(0o600)

    result = _success(jobs, "supervision-tick")
    assert result["state"] == "intentionally_stopped"
    assert result["recovery"]["attempts"] == 0


def test_stop_does_not_dirty_a_fresh_setup_path(checkout):
    _refused(checkout, "stop")
    assert {path.name for path in checkout.state.iterdir()} == {"command.lock"}
    _success(checkout, "setup")


@pytest.mark.parametrize(
    "intent_state", ["running", "unset", "malformed", "null", "fifo"]
)
def test_interrupted_stop_hold_requires_durable_stopped_intent(jobs, intent_state):
    _restore_fixture(jobs, "reader")
    _success(jobs, "select-instance", "reader")
    _success(jobs, "supervision-install")
    _refused(jobs, "start")
    jobs.environment["SYNTHETIC_EXIT_AFTER_REPLACE"] = "maintenance.json"
    assert jobs.run("stop").returncode == 71
    del jobs.environment["SYNTHETIC_EXIT_AFTER_REPLACE"]
    assert _success(jobs, "supervision-status")["intent"] == "running"
    intent = jobs.state / "operator-intent.json"
    if intent_state == "unset":
        intent.unlink()
    elif intent_state == "malformed":
        intent.write_text("{")
    elif intent_state == "null":
        intent.write_text("null")
        assert "Invalid operator-intent.json" in _refused(jobs, "supervision-status")
    elif intent_state == "fifo":
        intent.unlink()
        os.mkfifo(intent, 0o600)
    marker = jobs.state / "maintenance.json"
    before = marker.read_bytes()

    assert "retry rss stop" in _refused(jobs, "backup-reconcile")
    assert marker.read_bytes() == before
    if intent_state == "running":
        assert "maintenance" in _refused(jobs, "supervision-tick")
        assert _success(jobs, "supervision-status")["recovery"]["attempts"] == 0

    # An explicit retry records stopped intent without releasing the hold.
    assert "backup-reconcile" in _refused(jobs, "stop")
    result = _success(jobs, "backup-reconcile")
    assert result["reconciled"] is True and result["resumed"] is False
    assert "honor existing running intent" not in result["action"]
    assert _success(jobs, "supervision-tick")["state"] == "intentionally_stopped"


@pytest.mark.parametrize(
    "damage", ["outstanding", "lost_after_failed_stop", "lost_after_later_backup"]
)
def test_confirmed_upgrade_requires_uncontradicted_shutdown_evidence(jobs, damage):
    _restore_fixture(jobs, "reader")
    _success(jobs, "select-instance", "reader")
    first_stop = _success(jobs, "stop")["backup"]
    old_archive = Path(first_stop["archive"])
    old_archive_bytes = old_archive.read_bytes()
    assert (
        _success(jobs, "backup-status")["latest_success"]["writer_generation"] is None
    )

    intent = jobs.state / "operator-intent.json"
    stopped_intent = intent.read_bytes()
    generation = jobs.state / "instances/reader/writer-generation.json"
    parked = jobs.state.parent / "parked-generation.json"
    if damage != "outstanding":
        _refused(jobs, "start")
        assert generation.is_file()
        data = generation.parent / "data/feed-cache"
        data.write_bytes(b"new synthetic reading state")
    if damage == "lost_after_later_backup":
        assert _success(jobs, "backup")["verified"] is True
        assert _success(jobs, "backup-status")["latest_success"]["writer_generation"]
        # Isolate the generation check from the independent running-intent gate.
        intent.write_bytes(stopped_intent)
    else:
        backup_dir = jobs.environment.pop("RSS_BACKUP_DIR")
        assert "Shutdown backup failed" in _refused(jobs, "stop")
        jobs.environment["RSS_BACKUP_DIR"] = backup_dir
        pending = _success(jobs, "backup-status")["unresolved_shutdown"]
        assert pending["instance"] == "reader"
        assert (pending["writer_generation"] is None) is (damage == "outstanding")
    if damage != "outstanding":
        generation.rename(parked)

    before = jobs.binary.read_bytes()
    jobs.prepare_runtime("1.14.1")
    preview = _success(jobs, "upgrade")
    confirm = ("upgrade", "--confirm", preview["confirmation"])
    assert "shutdown archive" in _refused(jobs, *confirm)
    assert jobs.binary.read_bytes() == before
    assert old_archive.read_bytes() == old_archive_bytes

    if parked.exists():
        parked.rename(generation)
    assert _success(jobs, "stop")["backup"]["verified"] is True
    assert _success(jobs, *confirm)["manifest"]["runtime"]["version"] == "1.14.1"


def test_confirmed_upgrade_rejects_absent_generation_even_without_newer_evidence(jobs):
    _restore_fixture(jobs, "reader")
    _success(jobs, "select-instance", "reader")
    assert _success(jobs, "stop")["backup"]["verified"] is True
    old_success = _success(jobs, "backup-status")["latest_success"]
    assert old_success["writer_generation"] is None

    _refused(jobs, "start")
    instance = jobs.state / "instances/reader"
    (instance / "data/feed-cache").write_bytes(b"new synthetic reading state")
    owner = jobs.state / "native-owner.json"
    parked = owner.rename(jobs.state.parent / "parked-ownership.json")
    try:
        _refused(jobs, "stop")  # Retains stopped intent but cannot capture evidence.
    finally:
        parked.rename(owner)
    (instance / "writer-generation.json").unlink()
    report = _success(jobs, "backup-status")
    assert report["latest_success"] == old_success
    assert report["unresolved_shutdowns"] == []

    before = jobs.binary.read_bytes()
    jobs.prepare_runtime("1.14.1")
    preview = _success(jobs, "upgrade")
    refusal = _refused(jobs, "upgrade", "--confirm", preview["confirmation"])
    assert "generation" in refusal and "missing" in refusal
    assert jobs.binary.read_bytes() == before


def test_confirmed_upgrade_does_not_require_a_stop_for_unrelated_instance(jobs):
    _restore_fixture(jobs, "reader", "other")
    _success(jobs, "select-instance", "reader")
    assert _success(jobs, "stop")["backup"]["verified"] is True
    _success(jobs, "select-instance", "other")
    _refused(jobs, "start")
    assert _success(jobs, "backup")["verified"] is True
    backup_dir = jobs.environment.pop("RSS_BACKUP_DIR")
    assert "Shutdown backup failed" in _refused(jobs, "stop")
    jobs.environment["RSS_BACKUP_DIR"] = backup_dir
    _success(jobs, "select-instance", "reader")

    jobs.prepare_runtime("1.14.1")
    preview = _success(jobs, "upgrade")
    result = _success(jobs, "upgrade", "--confirm", preview["confirmation"])
    assert result["manifest"]["runtime"]["version"] == "1.14.1"
    assert _success(jobs, "backup-status")["unresolved_shutdown"]["instance"] == "other"


def test_confirmed_upgrade_cannot_attribute_unidentified_shutdown(jobs):
    _restore_fixture(jobs, "reader")
    _success(jobs, "select-instance", "reader")
    selection = jobs.state / "selected-instance.json"
    selected = selection.read_bytes()
    selection.write_text("{")
    assert "Shutdown backup failed" in _refused(jobs, "stop")
    selection.write_bytes(selected)
    assert _success(jobs, "stop")["backup"]["verified"] is True
    assert _success(jobs, "backup-status")["unresolved_shutdown"]["instance"] is None

    before = jobs.binary.read_bytes()
    jobs.prepare_runtime("1.14.1")
    preview = _success(jobs, "upgrade")
    assert "unidentified instance" in _refused(
        jobs, "upgrade", "--confirm", preview["confirmation"]
    )
    assert jobs.binary.read_bytes() == before
