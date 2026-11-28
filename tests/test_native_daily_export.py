"""Exercise daily capture and supervision of running writers with disposable jobs."""

import fcntl
import platform
import subprocess
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import Popen
from subprocess import run as run_subprocess

import pytest
from test_private_access import PORT, initialized_web
from test_private_access import controlled_web as controlled_web
from test_private_access import installed_checkout as installed_checkout
from test_private_access import native as native

from rss_aggregator.runtime import Runtime, RuntimeFailure
from tests.test_native_jobs import LAUNCHCTL, _tool


@pytest.fixture
def running_jobs(
    controlled_web,  # noqa: F811 - pytest injects the shared fixture.
    installed_checkout,  # noqa: F811 - pytest injects the shared fixture.
    tmp_path,
    monkeypatch,
):
    checkout, _ = installed_checkout
    web = controlled_web
    monkeypatch.setenv("RSS_TAILSCALE", "0")
    monkeypatch.setattr(platform, "system", lambda: "Darwin")

    launchctl = tmp_path / "launchctl"
    _tool(launchctl, LAUNCHCTL)
    tailscale = tmp_path / "tailscale"
    _tool(tailscale, "raise AssertionError('unexpected Tailscale call')")
    launcher = checkout.root / ".venv/bin/python"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    _tool(launcher, f"print({str(checkout.root)!r})")
    run_web_command = subprocess.run

    def run_command(args, **kwargs):
        if args[0] not in (str(launchctl), str(launcher)):
            return run_web_command(args, **kwargs)
        with monkeypatch.context() as local:
            local.setattr(subprocess, "Popen", Popen)
            return run_subprocess(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run_command)
    return Runtime(root=checkout.root, state_dir=web.state), web


def test_daily_export_failure_resumes_ready_writers_without_recovery_tick(
    running_jobs, monkeypatch
):
    runtime, web = running_jobs
    runtime.run("start")
    first_slot = datetime(2026, 1, 2, 4, tzinfo=timezone.utc)
    runtime.run("backup-enable", hour=4, now=first_slot)
    saved = runtime.run("backup-due", now=first_slot)
    assert saved["outcome"] == "success"
    archive = Path(saved["backup"]["archive"])
    original = archive.read_bytes()
    before = runtime.run("backup-status", now=first_slot)
    assert before["protected"] is True

    runtime.run("supervision-install")
    ready = runtime.run("status")
    assert ready["http_ready"] is True
    supervision = runtime.run("supervision-status")
    assert supervision["state"] == "healthy"

    capture_started = False

    def fail_tar_write(_tar, _member, _fileobj=None):
        nonlocal capture_started
        # No early scheduler/quiescence failure can satisfy this test: capture
        # has reached the tar output with both former writer leases released.
        for name in ("writers.lock", "controller.lock"):
            with (web.state / name).open("rb") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        capture_started = True
        raise OSError("synthetic destination write failure")

    next_slot = first_slot + timedelta(days=1)
    with monkeypatch.context() as export:
        export.setattr(tarfile.TarFile, "addfile", fail_tar_write)
        with pytest.raises(RuntimeFailure, match="Daily backup failed"):
            runtime.run("backup-due", now=next_slot)

    assert capture_started
    assert list(archive.parent.iterdir()) == [archive]
    assert archive.read_bytes() == original
    # These inspection commands cannot reopen the service or clear a hold.
    resumed = runtime.run("status")
    assert resumed["application"] == "running"
    assert resumed["http_ready"] is True
    assert resumed["scheduler"]["running"] is True
    assert resumed["instance"] == ready["instance"]
    assert resumed["url"] == ready["url"]
    assert runtime.run("supervision-status") == supervision

    report = runtime.run("backup-status", now=next_slot)
    assert report["latest_success"] == before["latest_success"]
    assert report["archive_available"] is True
    assert report["latest_attempt"]["outcome"] == "failed"
    assert report["daily_attempt"]["outcome"] == "failed"
    assert report["daily_attempt"]["slot"] == next_slot.isoformat()
    assert report["resumption"] == "preserved"
    assert report["maintenance_required"] is False
    assert report["protected"] is False
    assert not web.api.posts


@pytest.mark.parametrize("selector", ["port", "access"])
def test_supervision_install_refuses_endpoint_different_from_live_service(
    running_jobs, monkeypatch, selector
):
    runtime, web = running_jobs
    runtime.run("start")
    before = runtime.run("status")
    assert before["http_ready"] is True
    key, value = (
        ("RSS_PORT", str(PORT + 1)) if selector == "port" else ("RSS_TAILSCALE", "1")
    )
    with monkeypatch.context() as incompatible:
        incompatible.setenv(key, value)
        with pytest.raises(ValueError, match="configuration_conflict"):
            runtime.run("supervision-install")

    assert runtime.run("supervision-status")["enabled"] is False
    assert runtime.run("status") == before
    assert web.launches == 1 and not web.api.posts


@pytest.mark.parametrize("selector", ["port", "access"])
def test_supervision_tick_refuses_different_live_endpoint_without_spending_retries(
    running_jobs, monkeypatch, selector
):
    runtime, web = running_jobs
    if selector == "access":
        runtime.run("start")
        initialized_web(web)
        runtime.run("stop")
    with monkeypatch.context() as captured:
        if selector == "port":
            captured.setenv("RSS_PORT", str(PORT + 1))
        installed = runtime.run("supervision-install")
        assert installed["state"] == (
            "intentionally_stopped" if selector == "access" else "intent_unset"
        )
    if selector == "access":
        monkeypatch.setenv("RSS_TAILSCALE", "1")
    runtime.run("start")
    before = runtime.run("status")
    assert before["http_ready"] is True and before["port"] == PORT
    posts = list(web.api.posts)
    assert len(posts) == (1 if selector == "access" else 0)
    launches = web.launches

    supervision = runtime.run("supervision-status")
    assert supervision["state"] == "configuration_conflict"
    assert supervision["observed"]["application"] == "running"
    assert supervision["recovery"]["attempts"] == 0
    with pytest.raises(ValueError, match="configuration_conflict"):
        runtime.run("supervision-tick")
    assert runtime.run("supervision-status") == supervision
    assert runtime.run("status") == before
    assert web.launches == launches and web.api.posts == posts
