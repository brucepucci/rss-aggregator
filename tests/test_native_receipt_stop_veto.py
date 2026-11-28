"""Test receipt-loss stop and restart authorization through the disposable CLI."""

import json

import pytest

from tests import test_native_jobs
from tests.test_native_jobs import (
    _ledger,
    _owned_writer,
    _refused,
    _restore_fixture,
    _success,
)

jobs = test_native_jobs.jobs


@pytest.mark.parametrize("legacy", [False, True], ids=["current", "legacy-intent"])
def test_receipt_loss_stop_veto_survives_exit_at_first_durable_write(jobs, legacy):
    _restore_fixture(jobs, "reader")
    _success(jobs, "select-instance", "reader")
    _success(jobs, "supervision-install")
    _refused(jobs, "start")  # Disposable PHP refuses actual application work.
    if legacy:
        _ledger(jobs, "operator-intent.json", {"version": 1, "intent": "running"})
    assert _success(jobs, "supervision-status")["state"] == "unhealthy"
    receipt = jobs.state / "native-owner.json"
    parked = receipt.rename(jobs.state.parent / "parked-receipt.json")
    try:
        jobs.environment["SYNTHETIC_EXIT_AFTER_REPLACE"] = "backup-shutdown.json"
        assert jobs.run("stop").returncode == 71
        assert not (jobs.state / "stop-service.json").exists()
    finally:
        del jobs.environment["SYNTHETIC_EXIT_AFTER_REPLACE"]
        parked.rename(receipt)

    report = _success(jobs, "backup-status")
    assert report["unresolved_shutdown"]["outcome"] == "failed"
    assert report["maintenance_required"] is False
    supervisor = _success(jobs, "supervision-status")
    assert supervisor["intent"] == "running"  # The second write never ran.
    assert "stop_requested" in _refused(jobs, "supervision-tick")
    assert _success(jobs, "supervision-status")["recovery"]["attempts"] == 0
    assert _success(jobs, "supervision-status")["state"] == "stop_requested"

    # An explicit start can supersede the stop authorization, not the separate
    # unresolved shutdown record. The deterministic clock has not advanced.
    _refused(jobs, "start")
    assert _success(jobs, "supervision-status")["state"] == "unhealthy"
    check = jobs.run("backup-status", "--check")
    assert check.returncode == 1
    assert (
        json.loads(check.stdout)["unresolved_shutdown"] == report["unresolved_shutdown"]
    )

    assert _success(jobs, "stop")["backup"]["verified"] is True
    assert _success(jobs, "supervision-tick")["state"] == "intentionally_stopped"


def test_shutdown_record_vetoes_restart_if_maintenance_could_not_be_written(jobs):
    _restore_fixture(jobs, "reader")
    _success(jobs, "select-instance", "reader")
    _success(jobs, "supervision-install")
    _refused(jobs, "start")
    jobs.environment["SYNTHETIC_REPLACE_FAILURE"] = "maintenance.json"
    jobs.environment["SYNTHETIC_EXIT_AFTER_REPLACE"] = "backup-shutdown.json"
    assert jobs.run("stop").returncode == 71
    del jobs.environment["SYNTHETIC_REPLACE_FAILURE"]
    del jobs.environment["SYNTHETIC_EXIT_AFTER_REPLACE"]

    assert not (jobs.state / "maintenance.json").exists()
    assert "stop_requested" in _refused(jobs, "supervision-tick")
    assert _success(jobs, "supervision-status")["recovery"]["attempts"] == 0
    assert _success(jobs, "stop")["backup"]["verified"] is True
    assert _success(jobs, "supervision-tick")["state"] == "intentionally_stopped"


def test_receipt_loss_stop_veto_does_not_resume_during_later_backup(jobs):
    _restore_fixture(jobs, "reader")
    _success(jobs, "select-instance", "reader")
    _refused(jobs, "start")
    with _owned_writer(jobs):
        receipt = jobs.state / "native-owner.json"
        parked = receipt.rename(jobs.state.parent / "parked-receipt.json")
        try:
            jobs.environment["SYNTHETIC_EXIT_AFTER_REPLACE"] = "backup-shutdown.json"
            assert jobs.run("stop").returncode == 71
            assert not (jobs.state / "stop-service.json").exists()
        finally:
            del jobs.environment["SYNTHETIC_EXIT_AFTER_REPLACE"]
            parked.rename(receipt)

        assert _success(jobs, "status")["application"] == "running"
        # A manual capture may stop the identified writer, but may not launch
        # a new one without an unrevoked authorization.
        assert _success(jobs, "backup")["verified"] is True
    report = _success(jobs, "backup-status")
    assert report["unresolved_shutdown"]["outcome"] == "failed"
    assert report["maintenance_required"] is False
    assert _success(jobs, "status")["application"] == "stopped"
