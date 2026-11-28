"""Exercise daily identity and slot progress through the isolated, clocked CLI."""

import json
from pathlib import Path

import pytest

from tests import test_native_jobs
from tests.test_native_jobs import _refused, _restore_fixture, _success

jobs = test_native_jobs.jobs


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("registration_id", None),
        ("registration_id", "not-a-registration"),
        ("policy_id", None),
        ("policy_id", "f" * 64),
        ("instance", None),
        ("instance", "../reader"),
    ],
)
def test_daily_refuses_bad_legacy_target_identity_before_replacing_evidence(
    jobs, field, bad
):
    _restore_fixture(jobs, "reader")
    _success(jobs, "select-instance", "reader")
    _success(jobs, "backup-enable", "--hour", "4")
    _success(jobs, "backup-due")
    before = _success(jobs, "backup-status", "--check")
    daily = jobs.state / "backup-daily.json"
    flat = before["daily_attempt"].copy()  # Historical unversioned shape.
    if bad is None:
        del flat[field]
    else:
        flat[field] = bad
    damaged = json.dumps(flat).encode()
    daily.write_bytes(damaged)
    archives = set(jobs.backups.glob("*.zip"))

    check = jobs.run("backup-status", "--check")
    assert check.returncode == 1, check.stdout
    report = json.loads(check.stdout)
    assert "backup-daily.json" in report["evidence_problems"]
    assert report["daily_attempt"] is None
    assert report["latest_attempt"] == before["latest_attempt"]
    assert report["latest_success"] == before["latest_success"]
    assert "preserve" in _refused(jobs, "backup-due")
    assert daily.read_bytes() == damaged
    assert set(jobs.backups.glob("*.zip")) == archives
    assert not (jobs.state / "stop-service.json").exists()
    assert not (jobs.state / "maintenance.json").exists()
    if field == "policy_id" and bad is not None:
        schedule = jobs.state / "backup-schedule.json"
        original = schedule.read_bytes()
        # Removing the old schedule would erase the comparison proving this
        # otherwise well-formed policy ID is invalid for the registration.
        assert "preserve" in _refused(jobs, "backup-disable")
        assert schedule.read_bytes() == original
        assert _success(jobs, "backup-status")["registered"] is True
        assert daily.read_bytes() == damaged


def test_daily_completion_survives_reselection_and_backward_utc(jobs):
    _restore_fixture(jobs, "reader", "standby")
    _success(jobs, "select-instance", "reader")
    _success(jobs, "backup-enable", "--hour", "4")
    _success(jobs, "backup-due")
    first = _success(jobs, "backup-status", "--check")["daily_attempt"]
    # The old flat record remains valid progress when another target is added.
    (jobs.state / "backup-daily.json").write_text(json.dumps(first))
    _success(jobs, "select-instance", "standby")
    _success(jobs, "backup-due")
    other = _success(jobs, "backup-status", "--check")["daily_attempt"]
    assert other["instance"] == "standby"

    _success(jobs, "select-instance", "reader")
    archives = set(jobs.backups.glob("*.zip"))
    assert _success(jobs, "backup-due") == {
        "outcome": "not_due",
        "next_expected_attempt": "2026-01-03T04:00:00+00:00",
    }
    assert set(jobs.backups.glob("*.zip")) == archives
    report = _success(jobs, "backup-status", "--check")
    assert report["daily_attempt"] == other  # Latest global daily operation.
    assert report["daily_target_attempt"] == first
    assert report["daily_archive_available"] is True
    assert report["daily_due"] is False

    archive = Path(first["archive"])
    archive.chmod(0o000)
    try:
        inaccessible = _success(jobs, "backup-status")
        assert inaccessible["daily_archive_available"] is False
        assert set(inaccessible["archive_problems"]) == {"daily_target_attempt"}
        assert inaccessible["protected"] is False
        assert inaccessible["latest_success"]["instance"] == "standby"
    finally:
        archive.chmod(0o600)

    _success(jobs, "select-instance", "standby")
    jobs.backups.chmod(0o750)
    try:
        _refused(jobs, "backup")
    finally:
        jobs.backups.chmod(0o700)
    _success(jobs, "select-instance", "reader")
    failed = _success(jobs, "backup-status")
    assert failed["latest_attempt"]["outcome"] == "failed"
    assert failed["latest_attempt"]["instance"] == "standby"
    assert failed["daily_archive_available"] is True
    assert failed["protected"] is False

    jobs.environment["SYNTHETIC_SECONDS"] = "86400"
    _success(jobs, "backup-due")
    archives = set(jobs.backups.glob("*.zip"))
    for elapsed in ("1200", "86400"):
        jobs.environment["SYNTHETIC_SECONDS"] = elapsed
        assert _success(jobs, "backup-due") == {
            "outcome": "not_due",
            "next_expected_attempt": "2026-01-04T04:00:00+00:00",
        }
    assert set(jobs.backups.glob("*.zip")) == archives

    jobs.environment["SYNTHETIC_SECONDS"] = "1200"
    report = _success(jobs, "backup-status")
    assert report["daily_due"] is False and report["missed"] is False
    assert report["daily_target_attempt"]["slot"] == "2026-01-03T04:00:00+00:00"
    assert report["daily_archive_available"] is True
    assert report["protected"] is False  # The recorded attempt is in the future.


def test_daily_retry_budget_survives_interruption_reselection_and_rollback(jobs):
    _restore_fixture(jobs, "reader", "standby")
    _success(jobs, "select-instance", "reader")
    _success(jobs, "backup-enable", "--hour", "4")
    _success(jobs, "backup-due")
    jobs.environment["SYNTHETIC_SECONDS"] = "86400"
    jobs.environment["SYNTHETIC_EXIT_AFTER_REPLACE"] = "backup-daily.json"
    interrupted = jobs.run("backup-due")
    del jobs.environment["SYNTHETIC_EXIT_AFTER_REPLACE"]
    assert interrupted.returncode == 71
    _success(jobs, "select-instance", "standby")
    _success(jobs, "backup-due")
    _success(jobs, "select-instance", "reader")
    archives = set(jobs.backups.glob("*.zip"))

    for elapsed in ("1200", "87299"):  # Older slot, then 1s before retry.
        jobs.environment["SYNTHETIC_SECONDS"] = elapsed
        assert _success(jobs, "backup-due") == {
            "outcome": "not_due",
            "next_expected_attempt": "2026-01-03T04:15:00+00:00",
        }
        report = _success(jobs, "backup-status")
        assert report["daily_target_attempt"]["count"] == 1
        assert report["daily_target_attempt"]["outcome"] == "interrupted"
        assert report["daily_attempt"]["instance"] == "standby"

    jobs.backups.chmod(0o750)  # Refuse capture, not the captured launchd job.
    try:
        for elapsed, count in (("87300", 2), ("88200", 3)):
            jobs.environment["SYNTHETIC_SECONDS"] = elapsed
            assert "Daily backup failed" in _refused(jobs, "backup-due")
            assert (
                _success(jobs, "backup-status")["daily_target_attempt"]["count"]
                == count
            )
    finally:
        jobs.backups.chmod(0o700)
    for elapsed in ("1200", "86400", "90000"):
        jobs.environment["SYNTHETIC_SECONDS"] = elapsed
        assert _success(jobs, "backup-due") == {
            "outcome": "not_due",
            "next_expected_attempt": "2026-01-04T04:00:00+00:00",
        }
    assert set(jobs.backups.glob("*.zip")) == archives


def test_abandoned_daily_reservations_show_interruption_without_refunding_retries(jobs):
    _restore_fixture(jobs, "reader")
    _success(jobs, "select-instance", "reader")
    _success(jobs, "backup-enable", "--hour", "4")
    _success(jobs, "backup-due")
    baseline = _success(jobs, "backup-status", "--check")
    archives = set(jobs.backups.glob("*.zip"))
    daily = jobs.state / "backup-daily.json"
    for attempt, elapsed in enumerate((86400, 87300, 88200), start=1):
        jobs.environment["SYNTHETIC_SECONDS"] = str(elapsed)
        jobs.environment["SYNTHETIC_EXIT_AFTER_REPLACE"] = daily.name
        assert jobs.run("backup-due").returncode == 71
        del jobs.environment["SYNTHETIC_EXIT_AFTER_REPLACE"]
        reserved = daily.read_bytes()

        report = _success(jobs, "backup-status")
        assert report["busy"] is False and report["maintenance_required"] is False
        assert report["protected"] is False
        assert report["latest_success"] == baseline["latest_success"]
        assert report["daily_attempt"] == report["daily_target_attempt"]
        assert report["daily_attempt"]["outcome"] == "interrupted"
        assert report["daily_attempt"]["count"] == attempt
        assert daily.read_bytes() == reserved  # The report is a projection only.

    jobs.environment["SYNTHETIC_SECONDS"] = "90000"
    assert _success(jobs, "backup-due") == {
        "outcome": "not_due",
        "next_expected_attempt": "2026-01-04T04:00:00+00:00",
    }
    assert set(jobs.backups.glob("*.zip")) == archives


def test_changed_policy_replaces_valid_progress_only_at_first_eligible_attempt(jobs):
    _restore_fixture(jobs, "reader")
    _success(jobs, "select-instance", "reader")
    _success(jobs, "backup-enable", "--hour", "4")
    _success(jobs, "backup-due")
    before = _success(jobs, "backup-status", "--check")["daily_attempt"]
    daily = jobs.state / "backup-daily.json"
    original = daily.read_bytes()

    assert "explicitly disable" in _refused(jobs, "backup-enable", "--hour", "5")
    _success(jobs, "backup-disable")
    _success(jobs, "backup-enable", "--hour", "5")
    assert _success(jobs, "backup-due") == {
        "outcome": "not_due",
        "next_expected_attempt": "2026-01-02T05:00:00+00:00",
    }
    report = _success(jobs, "backup-status")
    assert report["daily_attempt"] == before
    assert report["daily_target_attempt"] is None
    assert report["daily_archive_available"] is False
    assert daily.read_bytes() == original

    jobs.environment["SYNTHETIC_SECONDS"] = "3600"
    _success(jobs, "backup-due")
    report = _success(jobs, "backup-status", "--check")
    changed = report["daily_target_attempt"]
    assert changed["registration_id"] == report["registration_id"]
    assert changed["registration_id"] != before["registration_id"]
    assert changed["policy_id"] != before["policy_id"]
    assert changed["count"] == 1
    assert changed["slot"] == "2026-01-02T05:00:00+00:00"
    assert changed["archive"] != before["archive"]


def test_damage_to_other_targets_progress_blocks_due_and_registration_replacement(jobs):
    _restore_fixture(jobs, "reader", "standby")
    _success(jobs, "select-instance", "reader")
    enabled = _success(jobs, "backup-enable", "--hour", "4")
    _success(jobs, "backup-due")
    _success(jobs, "select-instance", "standby")
    _success(jobs, "backup-due")
    before = _success(jobs, "backup-status", "--check")
    daily = jobs.state / "backup-daily.json"
    ledger = json.loads(daily.read_text())
    del ledger["targets"]["reader"]["count"]
    damaged = json.dumps(ledger).encode()
    daily.write_bytes(damaged)
    archives = set(jobs.backups.glob("*.zip"))

    jobs.environment["SYNTHETIC_SECONDS"] = "86400"
    report = _success(jobs, "backup-status")
    assert "backup-daily.json" in report["evidence_problems"]
    assert report["next_expected_attempt"] is None
    assert report["latest_success"] == before["latest_success"]
    assert "preserve" in _refused(jobs, "backup-due")
    _success(jobs, "backup-disable")
    assert "preserve" in _refused(jobs, "backup-enable", "--hour", "5")
    assert not (jobs.state / "backup-schedule.json").exists()
    assert not Path(enabled["job"]).exists()
    assert daily.read_bytes() == damaged
    assert set(jobs.backups.glob("*.zip")) == archives
