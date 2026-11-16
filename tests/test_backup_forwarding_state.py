"""Preserve observed forwarding and VM state across explicit instance backups."""

import copy
import io
import subprocess
import tarfile
from pathlib import Path

import pytest

from rss_aggregator.runtime import RuntimeFailure
from tests import test_private_access

private_runtime = test_private_access.private_runtime


@pytest.fixture
def backup_runtime(private_runtime, monkeypatch, tmp_path):
    # Import the existing fixture explicitly so its engine/HTTP substitutions and
    # our environment changes share pytest's function-scoped monkeypatch cleanup.
    runtime, engine = private_runtime
    directory = tmp_path / "backups"
    monkeypatch.setenv("RSS_BACKUP_DIR", str(directory))
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        content = b"synthetic configuration"
        member = tarfile.TarInfo("config.php")
        member.size = len(content)
        member.mode = 0o600
        archive.addfile(member, io.BytesIO(content))
    engine.restored[runtime.volume] = payload.getvalue()
    return runtime, engine, directory


@pytest.mark.parametrize("was_private", [False, True], ids=["loopback", "private"])
@pytest.mark.parametrize("export_fails", [False, True], ids=["success", "failure"])
def test_backup_restores_observed_forwarding_not_current_opt_in(
    backup_runtime, monkeypatch, was_private, export_fails
):
    runtime, engine, directory = backup_runtime
    monkeypatch.setenv("RSS_TAILSCALE", "1" if was_private else "0")
    runtime.run("start")
    before = copy.deepcopy(engine.config)
    settings_before = copy.deepcopy(engine.settings)
    monkeypatch.setenv("RSS_TAILSCALE", "0" if was_private else "1")
    exports = []

    def run(*args, check=True, input=None):
        if args[:5] == (
            "podman",
            "--connection",
            "rss-aggregator",
            "volume",
            "export",
        ):
            # The snapshot sees stopped writers and no project forwarding.
            assert not engine.app["State"]["Running"]
            assert engine.config == engine.original
            exports.append(args)
            if export_fails:
                return subprocess.CompletedProcess(
                    args, 1, "", "injected export failure"
                )
        return engine.run(*args, check=check, input=input)

    monkeypatch.setattr(runtime, "_run", run)
    if export_fails:
        with pytest.raises(RuntimeFailure, match="Volume export failed"):
            runtime.run("backup")
        assert not list(directory.glob("*.zip"))
    else:
        result = runtime.run("backup")
        assert result["verified"] is True
        assert Path(result["archive"]).is_file()
    assert len(exports) == 1
    assert engine.machine["State"] == "running"
    assert engine.app["State"]["Running"]
    assert engine.config == before
    assert engine.settings == settings_before
    if not was_private:
        assert engine.settings["api_enabled"] is False
        assert engine.settings["base_url"] == f"http://127.0.0.1:{engine.port}"


def test_archive_success_is_distinct_from_failed_resumption(
    backup_runtime, monkeypatch
):
    runtime, engine, _directory = backup_runtime
    runtime.run("start")
    failed = False

    def run(*args, check=True, input=None):
        nonlocal failed
        if args[:4] == ("podman", "--connection", "rss-aggregator", "start"):
            failed = True
            raise RuntimeFailure("injected start failure")
        return engine.run(*args, check=check, input=input)

    monkeypatch.setattr(runtime, "_run", run)
    with pytest.raises(RuntimeFailure, match="injected start failure"):
        runtime.run("backup")
    assert failed
    report = runtime.run("backup-status")
    assert report["latest_success"]["verified"] is True
    assert Path(report["latest_success"]["archive"]).exists()
    assert report["latest_attempt"]["outcome"] == "failed"
    assert report["resumption"] == "failed"
    assert report["maintenance_required"] is True
    assert report["protected"] is False
    assert runtime.run("backup-reconcile")["resumed"] is False
    assert not engine.app["State"]["Running"]


def test_backup_preserves_persisted_stopped_intent_without_resuming(backup_runtime):
    runtime, engine, _directory = backup_runtime
    runtime.run("start")
    intent = runtime.state_dir / "operator-intent.json"
    original = b'{"version":1,"intent":"stopped"}\n'
    intent.write_bytes(original)
    result = runtime.run("backup")
    assert result["verified"] is True
    assert not engine.app["State"]["Running"]
    assert intent.read_bytes() == original
    assert runtime.run("backup-status")["resumption"] == "preserved"


def test_backup_forwarding_query_failure_does_not_leave_stopped_vm_running(
    backup_runtime, monkeypatch
):
    runtime, engine, directory = backup_runtime
    runtime.run("start")
    # Stop without an archive to leave a realistic inactive forwarding record.
    engine.volumes.clear()
    runtime.run("stop")
    assert engine.machine["State"] == "stopped"
    before = copy.deepcopy(engine.config)

    def run(*args, check=True, input=None):
        if args == (
            "podman",
            "machine",
            "start",
            "--update-connection=false",
            "rss-aggregator",
        ):
            engine.machine["State"] = "running"
            return subprocess.CompletedProcess(args, 0, "", "")
        if args == ("tailscale", "serve", "status", "--json"):
            return subprocess.CompletedProcess(args, 1, "", "injected query failure")
        return engine.run(*args, check=check, input=input)

    monkeypatch.setattr(runtime, "_run", run)
    with pytest.raises(RuntimeFailure, match="Tailscale query failed"):
        runtime.run("backup")
    assert engine.machine["State"] == "stopped"
    assert not engine.app["State"]["Running"]
    assert engine.config == before
    assert not list(directory.glob("*.zip"))
