"""Fixture runners preserve shared safety and private evidence through real CLI calls."""

import fcntl
import json
import subprocess
import sys

import pytest

from tests.integration.replacement_support import replacement_names
from tests.test_backups import APP, ROOT, VOLUME, BackupProject

TOKEN = "0123456789abcdef"
PREFIX = "test-" + TOKEN + "-"


@pytest.fixture
def fixture_project(tmp_path):
    project = BackupProject(tmp_path)
    app, volume, _check = replacement_names(TOKEN)
    state = project.engine()
    container = state["containers"].pop(APP)
    container.update(
        Name=app,
        HostConfig={
            "PortBindings": {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18080"}]}
        },
    )
    container["Config"]["Labels"]["io.rss-aggregator.test"] = TOKEN
    container["Mounts"][0].update(Name=volume, Type="volume")
    state["containers"][app] = container
    info = state["volumes"].pop(VOLUME)
    info.update(Name=volume, CreatedAt="fixture-created", Mountpoint="/fixture/data")
    info["Labels"]["io.rss-aggregator.test"] = TOKEN
    state["volumes"][volume] = info
    project.update(**state)
    project.env.update(HOME=str(tmp_path), RSS_PORT="18080", RSS_TAILSCALE="0")
    # Extend only the external executable seam. Preflight refuses deliberately:
    # intent isolation can be checked without an HTTP server or real resources.
    podman = tmp_path / "bin/podman"
    original = podman.read_text().split("\n", 1)[1]
    podman.write_text(
        "#!"
        + sys.executable
        + "\n"
        + """import json, os, sys
from pathlib import Path
args = sys.argv[1:]
path = Path(os.environ["FAKE_BACKUP_ENGINE"])
state = json.loads(path.read_text())
if args == ["system", "connection", "list", "--format", "json"] and os.environ.get("FIXTURE_TEST_LATE_HOLD"):
    (Path(os.environ["RSS_STATE_DIR"]) / "maintenance.json").write_text("late shared hold")
if args[:3] == ["--connection", "rss-aggregator", "image"] and args[3] == "inspect":
    state["calls"].append(args); path.write_text(json.dumps(state))
    pin = state["image"]
    print(json.dumps([{
        "Id": "fixture-image", "Os": "linux", "Architecture": "arm64",
        "RepoDigests": [pin["image"], pin["image"].split("@")[0] + "@" + pin["platform_manifest"]],
        "Labels": {"org.opencontainers.image.version": pin["version"], "org.opencontainers.image.revision": pin["revision"]},
        "Config": {"Entrypoint": ["./Docker/entrypoint.sh"]},
    }]))
    raise SystemExit(0)
if args[:3] == ["--connection", "rss-aggregator", "exec"]:
    state["calls"].append(args); path.write_text(json.dumps(state))
    if args[4:] == ["test", "-f", "/var/www/FreshRSS/data/applied_migrations.txt"]:
        raise SystemExit(0)
    raise SystemExit("unexpected fixture exec")
if args[:3] == ["--connection", "rss-aggregator", "run"]:
    state["calls"].append(args); path.write_text(json.dumps(state))
    raise SystemExit("controlled fixture preflight refusal")
"""
        + original
    )
    return project


def invoke(project, runner="backup", operation="backup"):
    return subprocess.run(
        [sys.executable, "-m", "tests.integration." + runner + "_runner"],
        input=json.dumps(
            {
                "token": TOKEN,
                "operation": operation,
                "port": 18080,
                "volume": {
                    "Name": replacement_names(TOKEN)[1],
                    "CreatedAt": "fixture-created",
                    "Mountpoint": "/fixture/data",
                },
            }
        ),
        env=project.env,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )


@pytest.mark.parametrize("late_hold", [False, True])
@pytest.mark.parametrize(
    "runner,operation",
    [
        ("backup", "backup"),
        ("backup", "start"),
        ("replace", "replace"),
        ("supervision", "start"),
    ],
)
def test_fixture_writers_cannot_redirect_away_from_shared_maintenance(
    fixture_project, runner, operation, late_hold
):
    project = fixture_project
    marker = project.state_dir / "maintenance.json"
    state = project.engine()
    state["containers"][replacement_names(TOKEN)[0]]["State"]["Running"] = True
    project.update(**state)
    expected_marker = b"unresolved owner maintenance\n"
    if late_hold:
        # Arrives through the executable seam during owner validation, after any
        # initial runner check but before Runtime acquires command.lock.
        project.env["FIXTURE_TEST_LATE_HOLD"] = "1"
        expected_marker = b"late shared hold"
    else:
        marker.write_bytes(expected_marker)
    owner = (project.state_dir / "owner.json").read_bytes()

    result = invoke(project, runner, operation)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "Maintenance is unresolved" in result.stderr
    assert marker.read_bytes() == expected_marker
    assert (project.state_dir / "owner.json").read_bytes() == owner
    assert not project.backups.exists()
    assert not (project.state_dir / (PREFIX + "backup-attempt.json")).exists()
    assert not any(
        args[:3] == ["--connection", "rss-aggregator", "stop"]
        for args in project.engine()["calls"]
    )


REAL_RECORDS = (
    "owner.json",
    "selected-volume.json",
    "recovery-volumes.json",
    "last-backup.json",
    "backup-attempt.json",
    "backup-success.json",
    "backup-daily.json",
    "backup-schedule.json",
    "backup-defer.json",
    "operator-intent.json",
    "supervision.json",
    "supervision-recovery.json",
    "serve.json",
)


def snapshot(project):
    return {
        name: (project.state_dir / name).read_bytes()
        if (project.state_dir / name).exists()
        else None
        for name in REAL_RECORDS
    }


@pytest.mark.parametrize("existing_records", [False, True])
@pytest.mark.parametrize(
    "runner,operation",
    [
        ("backup", "backup"),
        ("backup", "start"),
        ("replace", "replace"),
        ("supervision", "start"),
    ],
)
def test_fixture_cli_preserves_real_intent_and_backup_evidence(
    fixture_project, runner, operation, existing_records
):
    project = fixture_project
    if existing_records:
        for name in REAL_RECORDS:
            if name != "owner.json":
                (project.state_dir / name).write_bytes(
                    b'{"preserve":"real evidence"}\n'
                )
        (project.state_dir / "operator-intent.json").write_bytes(
            b'{"version":1,"intent":"stopped"}\n'
        )
    before = snapshot(project)
    lock = project.state_dir / "command.lock"
    lock.write_bytes(b"retained command lock\n")
    inode = lock.stat().st_ino
    state = project.engine()
    if runner != "backup" or operation == "start":
        state["containers"][replacement_names(TOKEN)[0]]["State"]["Running"] = True
        project.update(**state)

    result = invoke(project, runner, operation)

    if operation == "backup":
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["verified"] is True
        for name in ("backup-attempt.json", "backup-success.json", "last-backup.json"):
            assert (project.state_dir / (PREFIX + name)).is_file()
    else:
        # Deliberate external preflight refusal keeps this deterministic and
        # exercises public start/replace intent handling without bypassing PHP.
        assert result.returncode != 0
        assert result.stdout == ""
        assert "controlled fixture preflight refusal" in result.stderr
        if runner == "backup":
            assert (
                json.loads(
                    (project.state_dir / (PREFIX + "operator-intent.json")).read_text()
                )["intent"]
                == "running"
            )
    assert snapshot(project) == before
    assert lock.read_bytes() == b"retained command lock\n"
    assert lock.stat().st_ino == inode
    assert not (project.state_dir / "maintenance.json").exists()


def test_supervision_runner_accepts_owner_label_after_guarded_fixture_replacement(
    fixture_project,
):
    project = fixture_project
    state = project.engine()
    app = state["containers"][replacement_names(TOKEN)[0]]
    del app["Config"]["Labels"]["io.rss-aggregator.test"]
    app["State"]["Running"] = True
    project.update(**state)
    before = snapshot(project)
    result = invoke(project, "supervision", "start")
    assert result.returncode != 0
    assert "controlled fixture preflight refusal" in result.stderr
    assert snapshot(project) == before


@pytest.mark.parametrize("lease", [None, "another-fixture"])
def test_supervision_runner_requires_test_volume_lease_without_container_test_label(
    fixture_project, lease
):
    project = fixture_project
    state = project.engine()
    app_name, volume, _check = replacement_names(TOKEN)
    del state["containers"][app_name]["Config"]["Labels"]["io.rss-aggregator.test"]
    labels = state["volumes"][volume]["Labels"]
    if lease is None:
        del labels["io.rss-aggregator.test"]
    else:
        labels["io.rss-aggregator.test"] = lease
    project.update(**state)
    before = snapshot(project)
    result = invoke(project, "supervision", "start")
    assert result.returncode != 0
    assert "test ownership mismatch" in result.stderr
    assert not (project.state_dir / (PREFIX + "operator-intent.json")).exists()
    assert snapshot(project) == before


@pytest.mark.parametrize("fixture_intent", ["running", "stopped"])
def test_fixture_backup_resumption_uses_fixture_intent_and_retains_shared_hold(
    fixture_project, fixture_intent
):
    project = fixture_project
    state = project.engine()
    app = replacement_names(TOKEN)[0]
    state["containers"][app]["State"]["Running"] = True
    project.update(**state)
    real_intent = "stopped" if fixture_intent == "running" else "running"
    for name, intent in (
        ("operator-intent.json", real_intent),
        (PREFIX + "operator-intent.json", fixture_intent),
    ):
        (project.state_dir / name).write_text(
            json.dumps({"version": 1, "intent": intent})
        )
    for name in ("backup-attempt.json", "backup-success.json", "last-backup.json"):
        (project.state_dir / name).write_bytes(b'{"preserve":"real evidence"}\n')
    before = snapshot(project)

    result = invoke(project)
    status = invoke(project, operation="backup-status")

    assert status.returncode == 0, status.stderr
    report = json.loads(status.stdout)
    assert report["latest_success"]["verified"] is True
    assert report["latest_success"]["volume"] == replacement_names(TOKEN)[1]
    assert project.engine()["containers"][app]["State"]["Running"] is False
    if fixture_intent == "stopped":
        assert result.returncode == 0, result.stderr
        assert report["resumption"] == "preserved"
        assert report["maintenance_required"] is False
    else:
        assert result.returncode != 0
        assert "controlled fixture preflight refusal" in result.stderr
        assert report["resumption"] == "failed"
        assert report["maintenance_required"] is True
        marker = project.state_dir / "maintenance.json"
        retained = marker.read_bytes()
        # Both ordinary production CLI and redirected fixture CLI see the same
        # interrupted-fixture hold; neither can silently reopen writers.
        for refused in (project.run("start"), invoke(project, operation="start")):
            assert refused.returncode != 0
            assert refused.stdout == ""
            assert "Maintenance is unresolved" in refused.stderr
        assert marker.read_bytes() == retained
    assert snapshot(project) == before


@pytest.mark.parametrize("runner", ["backup", "replace", "supervision"])
def test_fixture_cli_retains_real_command_lock(fixture_project, runner):
    project = fixture_project
    state = project.engine()
    state["containers"][replacement_names(TOKEN)[0]]["State"]["Running"] = True
    project.update(**state)
    before = snapshot(project)
    with (project.state_dir / "command.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = invoke(project, runner)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "Another project command is running" in result.stderr
    assert snapshot(project) == before
    assert not project.backups.exists()
