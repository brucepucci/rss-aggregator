"""Exercise stop and upgrade evidence through the disposable native CLI."""

import fcntl
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from tests import test_native_install, test_native_jobs
from tests.test_native_jobs import _ledger, _refused, _restore_fixture, _success
from tests.test_native_process_lifetime import _wait_for
from tests.test_native_process_lifetime import (
    process_checkout as shared_process_checkout,
)

checkout = test_native_install.checkout
jobs = test_native_jobs.jobs
process_checkout = shared_process_checkout


@pytest.mark.parametrize("mismatched_generation", [False, True])
def test_lost_fetch_lease_only_blocks_capture_and_unidentified_shutdown(
    process_checkout, mismatched_generation
):
    checkout = process_checkout
    assert _success(checkout, "start")["application"] == "running"
    lease = checkout.state / "fetch.lock"
    parked = lease.rename(checkout.control / "parked-fetch")
    generation = checkout.state / "instances/reader/writer-generation.json"
    original = generation.read_bytes()
    if mismatched_generation:
        generation.write_text(json.dumps({"writer_generation": uuid4().hex}))
    try:
        assert "writer state is unconfirmed" in _refused(checkout, "stop")
        assert not lease.exists()
        assert _success(checkout, "status")["application"] == "unconfirmed"
        assert not list(checkout.backups.glob("*.zip"))
    finally:
        parked.rename(lease)
        generation.write_bytes(original)
        try:
            status = _success(checkout, "status")
            assert (status["application"], status["http_ready"]) == (
                ("running", True) if mismatched_generation else ("stopped", False)
            )
        finally:
            # Release only the disposable generation, including on regression.
            nonce = json.loads((checkout.state / "service.json").read_text())["nonce"]
            (checkout.control / f"web-release-{nonce}").touch()


def test_idle_startup_claim_does_not_attribute_an_unidentified_fetch(process_checkout):
    checkout = process_checkout
    _success(checkout, "start")
    first = _success(checkout, "stop")
    assert first["backup"]["verified"] is True
    with (checkout.state / "fetch.lock").open("rb") as writer:
        fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with checkout.popen("stop") as stop:
            try:
                shutdown = checkout.state / "backup-shutdown.json"
                _wait_for(
                    lambda: shutdown.exists() or stop.poll() is not None,
                    "stop did not observe the independent fetch lease",
                )
                assert stop.poll() is None
                pending = _success(checkout, "backup-status")["unresolved_shutdown"]
                assert pending["instance"] is None
                fcntl.flock(writer, fcntl.LOCK_UN)
                out, err = stop.communicate(timeout=5)
                assert stop.returncode == 1 and not out
                assert "shutdown identity could not be confirmed" in err
            finally:
                if stop.poll() is None:
                    stop.kill()


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
        assert {path.name for path in jobs.state.iterdir()} == before | {
            "backup-shutdown.json"
        }
        if damage == "missing":
            assert not receipt.exists()
        else:
            assert receipt.read_text() == "{"
    receipt.write_bytes(original)
    receipt.chmod(0o600)

    result = _success(jobs, "supervision-tick")
    assert result["state"] == "intentionally_stopped"
    assert result["recovery"]["attempts"] == 0
    assert _success(jobs, "stop")["backup"]["verified"] is True
    pending = _success(jobs, "backup-status")["unresolved_shutdown"]
    assert (pending["instance"], pending["outcome"]) == (None, "failed")


@pytest.mark.parametrize("damaged_hold", [False, True])
def test_receipt_loss_blocks_protection_until_the_verified_stop_retry(
    jobs, damaged_hold
):
    _restore_fixture(jobs, "reader")
    _success(jobs, "select-instance", "reader")
    _success(jobs, "backup-enable", "--hour", "4")
    _refused(jobs, "start")
    _success(jobs, "backup-due")
    before = _success(jobs, "backup-status", "--check")
    generation = before["latest_success"]["writer_generation"]
    assert generation is not None
    data = jobs.state / "instances/reader/data/feed-cache"
    data.write_bytes(b"changed synthetic reading state")
    nonce = uuid4().hex
    receipt = jobs.state / "native-owner.json"
    _ledger(
        jobs,
        "service.json",
        {
            "nonce": nonce,
            "instance": "reader",
            "owner": json.loads(receipt.read_text())["token"],
            "writer_generation": generation,
        },
    )
    _ledger(jobs, "service-status.json", {"nonce": nonce, "web": "started"})
    bad_hold = jobs.state / "maintenance.json"
    if damaged_hold:
        bad_hold.write_text("{")
        bad_hold.chmod(0o600)
    parked = receipt.rename(jobs.state.parent / "parked-ownership.json")
    with (
        (jobs.state / "writers.lock").open("rb") as writer,
        (jobs.state / "controller.lock").open("rb") as controller,
    ):
        for lock in (writer, controller):
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            _refused(jobs, "stop")
            assert not (jobs.state / "stop-service.json").exists()
        finally:
            parked.rename(receipt)
        check = jobs.run("backup-status", "--check")
        assert check.returncode == 1, check.stdout
        failed = json.loads(check.stdout)
        assert failed["protected"] is False
        assert failed["unresolved_shutdown"]["outcome"] == "failed"
        assert failed["unresolved_shutdown"]["instance"] == "reader"
        assert failed["unresolved_shutdown"]["writer_generation"] == generation
        assert failed["latest_success"] == before["latest_success"]

    if damaged_hold:
        _refused(jobs, "backup-reconcile")
        assert bad_hold.read_text() == "{"
        bad_hold.rename(jobs.state.parent / "preserved-hold.json")
    stopped = _success(jobs, "stop")["backup"]
    assert stopped["verified"] is True
    report = _success(jobs, "backup-status", "--check")
    assert report["unresolved_shutdowns"] == []
    assert report["latest_success"]["archive"] == stopped["archive"]


def test_stop_does_not_dirty_a_fresh_setup_path(checkout):
    _refused(checkout, "stop")
    assert {path.name for path in checkout.state.iterdir()} == {"command.lock"}
    _success(checkout, "setup")


def test_stop_does_not_adopt_an_unrelated_runtime_without_a_receipt(checkout):
    checkout.state.mkdir(mode=0o700)
    unrelated = checkout.state / "runtime"
    unrelated.mkdir(mode=0o700)
    marker = unrelated / "existing-data"
    marker.write_text("unrelated state")
    _refused(checkout, "stop")
    assert {path.name for path in checkout.state.iterdir()} == {"runtime"}
    assert marker.read_text() == "unrelated state"
    assert "Refusing to adopt" in _refused(checkout, "setup")


def test_receipt_loss_in_an_established_empty_installation_stays_unidentified(jobs):
    receipt = jobs.state / "native-owner.json"
    parked = receipt.rename(jobs.state.parent / "parked-ownership.json")
    try:
        _refused(jobs, "stop")
        assert not receipt.exists()
        assert not (jobs.state / "operator-intent.json").exists()
    finally:
        parked.rename(receipt)
    pending = _success(jobs, "backup-status")["unresolved_shutdown"]
    assert (pending["instance"], pending["outcome"]) == (None, "failed")
    assert _success(jobs, "stop")["backup"] == {"status": "no_data"}
    assert _success(jobs, "backup-status")["unresolved_shutdown"] == pending


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


def test_later_stop_vetoes_reconciliation_of_an_existing_backup_hold(jobs):
    _restore_fixture(jobs, "reader")
    _success(jobs, "select-instance", "reader")
    _success(jobs, "supervision-install")
    _refused(jobs, "start")  # The synthetic PHP command refuses application work.
    jobs.environment["SYNTHETIC_EXIT_AFTER_REPLACE"] = "maintenance.json"
    assert jobs.run("backup").returncode == 71
    jobs.environment["SYNTHETIC_EXIT_AFTER_REPLACE"] = "backup-shutdown.json"
    assert jobs.run("stop").returncode == 71
    del jobs.environment["SYNTHETIC_EXIT_AFTER_REPLACE"]

    assert (
        _success(jobs, "backup-status")["unresolved_shutdown"]["instance"] == "reader"
    )
    assert "retry rss stop" in _refused(jobs, "backup-reconcile")
    assert "maintenance" in _refused(jobs, "supervision-tick")
    assert _success(jobs, "supervision-status")["recovery"]["attempts"] == 0

    assert "backup-reconcile" in _refused(jobs, "stop")
    result = _success(jobs, "backup-reconcile")
    assert result["reconciled"] is True and result["resumed"] is False
    assert "honor existing running intent" not in result["action"]
    assert _success(jobs, "supervision-tick")["state"] == "intentionally_stopped"

    # The prior failed shutdown is still unresolved, but the explicit restart
    # and a separate hold must not inherit its veto (even on the same clock tick).
    _refused(jobs, "start")
    jobs.environment["SYNTHETIC_EXIT_AFTER_REPLACE"] = "maintenance.json"
    assert jobs.run("backup").returncode == 71
    del jobs.environment["SYNTHETIC_EXIT_AFTER_REPLACE"]
    assert (
        "honor existing running intent" in _success(jobs, "backup-reconcile")["action"]
    )
    assert _success(jobs, "supervision-status")["intent"] == "running"


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
    # Isolate the generation check without requesting a new shutdown, which
    # would now durably preserve the failed attempt even if the receipt were lost.
    _ledger(jobs, "operator-intent.json", {"version": 1, "intent": "stopped"})
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
