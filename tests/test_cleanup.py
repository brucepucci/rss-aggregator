"""Destructive cleanup through the command boundary with disposable engines."""

import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from tests.test_backups import APP, VOLUME, BackupProject


@pytest.fixture
def project(tmp_path):
    project = BackupProject(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    project.env["HOME"] = str(home)
    return project


def preview(project, *command):
    result = project.run(*command)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_native_drill_bootstraps_a_machine_within_podmans_name_limit(tmp_path):
    import subprocess
    import sys

    from tests.test_commands import ROOT, CommandFixture

    command = CommandFixture(tmp_path)
    native = tmp_path / "podman-state.json"
    native.write_text(json.dumps({"machines": [], "details": {}, "connections": []}))
    packages = tmp_path / "packages.json"
    packages.write_text(json.dumps({"packages": {"podman": "6.2.0"}}))
    command.env.update(FAKE_PODMAN_STATE=str(native), FAKE_BREW_INSTALLED=str(packages))
    command.tool("brew", (ROOT / "tests/fake_bootstrap_brew.py").read_text())
    command.tool(
        "podman",
        "import sys\n"
        "if sys.argv[1:3] == ['machine', 'start']: raise SystemExit('controlled drill start boundary')\n"
        + (ROOT / "tests/fake_bootstrap_podman.py").read_text(),
    )
    # Import under the real platform first: selecting Darwin before urllib is
    # imported on Linux would request the unavailable native _scproxy module.
    script = """
import sys
from pathlib import Path
import pytest
from rss_aggregator import RuntimeFailure
from tests.integration.test_live_cleanup import test_disposable_vm_remove_recreate_restore_preserves_authenticated_state as drill
sys.platform = 'darwin'
with pytest.MonkeyPatch.context() as patch:
    try:
        drill(Path(sys.argv[1]), patch)
    except RuntimeFailure as error:
        assert 'controlled drill start boundary' in str(error), str(error)
    else:
        raise AssertionError('Controlled native fixture unexpectedly started a VM')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "drill")],
        cwd=ROOT,
        env=command.env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    machine = json.loads(native.read_text())["machines"]
    assert len(machine) == 1 and len(machine[0]["Name"]) <= 30
    assert machine[0]["Running"] is False


def test_vm_preview_lists_exact_targets_without_mutation(project):
    before = project.engine()
    report = preview(project, "remove-vm")
    assert report["preview"] is True
    assert len(report["confirmation"]) == 64
    assert report["targets"]["machine"] == "rss-aggregator"
    assert report["targets"]["volumes"] == [VOLUME]
    assert report["targets"]["containers"] == [APP]
    assert report["backup_directory"] == str(project.backups)
    assert not project.backups.exists()
    after = project.engine()
    assert {k: v for k, v in after.items() if k != "calls"} == {
        k: v for k, v in before.items() if k != "calls"
    }
    assert not any(call[0:2] == ["machine", "rm"] for call in after["calls"])
    assert [
        "--connection",
        "rss-aggregator",
        "secret",
        "ls",
        "--format",
        "json",
    ] in after["calls"]
    assert [
        "machine",
        "ssh",
        "rss-aggregator",
        "sudo",
        "podman",
        "secret",
        "ls",
        "--format",
        "json",
    ] in after["calls"]


def test_remove_vm_makes_fresh_backup_and_preserves_unrelated_state_and_repeats(
    project,
):
    before = project.engine()
    sentinel = {
        "Name": "unrelated",
        "URI": "ssh://sentinel",
        "Identity": "/sentinel",
        "Default": True,
    }
    project.update(
        other_machines=[{"Name": "unrelated", "Running": False}],
        other_connections=[sentinel],
    )
    report = preview(project, "remove-vm")
    result = project.run("remove-vm", "--confirm", report["confirmation"])
    assert result.returncode == 0, result.stderr
    removed = json.loads(result.stdout)
    assert removed["removed"] is True
    assert removed["backup"]["verified"] is True
    assert Path(removed["backup"]["archive"]).is_file()
    after = project.engine()
    assert after["machine"] is None
    assert after["other_connections"] == [sentinel]
    assert after["other_machines"] == [{"Name": "unrelated", "Running": False}]
    assert after["payload"] == before["payload"]
    assert (project.state_dir / "owner.json").exists()
    assert (project.state_dir / "removed-vm.json").exists()
    assert not (project.state_dir / "maintenance.json").exists()
    assert preview(project, "remove-vm")["status"] == "absent"


@pytest.mark.parametrize(
    "fault",
    [
        "fail_export",
        "invalid_archive",
        "unresponsive",
        "extra_volume",
        "foreign_container",
        "foreign_image",
        "rootful_volume",
        "stale_plan",
        "stopped_source",
        "maintenance",
    ],
)
def test_vm_removal_failures_preserve_all_resources_and_prior_archives(project, fault):
    old = preview(project, "backup")
    archive = Path(old["archive"])
    original = archive.read_bytes()
    plan = preview(project, "remove-vm")
    state = project.engine()
    if fault in ("fail_export", "unresponsive"):
        state[fault] = True
    elif fault == "invalid_archive":
        Path(state["payload"]).write_bytes(b"invalid tar")
    elif fault == "extra_volume":
        state["volumes"]["retained-recovery"] = state["volumes"][VOLUME]
    elif fault == "foreign_container":
        state["containers"]["unrelated"] = state["containers"][APP]
    elif fault == "foreign_image":
        state["images"] = [{"Id": "other-image"}]
    elif fault == "rootful_volume":
        state["rootful_volumes"] = [{"Name": "rootful-sentinel"}]
    elif fault == "stale_plan":
        state["connection"]["Default"] = True
    elif fault == "stopped_source":
        state["machine"]["State"] = "stopped"
    elif fault == "maintenance":
        (project.state_dir / "maintenance.json").write_text("invalid hold")
    project.update(**state)
    before = project.engine()
    result = project.run("remove-vm", "--confirm", plan["confirmation"])
    assert result.returncode == 1
    after = project.engine()
    assert after["machine"] == before["machine"]
    assert after["volumes"] == before["volumes"]
    assert set(after["containers"]) == set(before["containers"])
    assert archive.read_bytes() == original
    assert "sensitive" not in result.stdout + result.stderr
    assert not any(call[:2] == ["machine", "rm"] for call in after["calls"])


@pytest.mark.parametrize("engine", ["rootless", "rootful"])
@pytest.mark.parametrize("stage", ["preview", "apply", "export"])
@pytest.mark.parametrize("fault", ["present", "inspection_failure", "malformed"])
def test_vm_removal_preserves_unrelated_secrets(project, engine, stage, fault):
    plan = preview(project, "remove-vm") if stage != "preview" else None
    key = "secrets" if engine == "rootless" else "rootful_secrets"
    payload_key = (
        "secret_payloads" if engine == "rootless" else "rootful_secret_payloads"
    )
    secret_state = {
        key: [{"ID": "synthetic-secret-id", "Name": "sensitive-secret-name"}],
        payload_key: {"synthetic-secret-id": "sensitive-secret-payload"},
    }
    if fault == "inspection_failure":
        secret_state["fail_secret_inventory"] = engine
    elif fault == "malformed":
        secret_state["malformed_secret_inventory"] = engine
    if stage == "export":
        project.update(secret_state_during_export=secret_state)
    else:
        project.update(**secret_state)
    before = project.engine()
    source = Path(before["payload"]).read_bytes()
    result = project.run(
        "remove-vm", *(["--confirm", plan["confirmation"]] if plan else [])
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout == ""
    assert "sensitive" not in result.stderr
    after = project.engine()
    assert after[key] == secret_state[key]
    assert after[payload_key] == secret_state[payload_key]
    assert after["machine"] == before["machine"]
    assert after["volumes"] == before["volumes"]
    assert after["containers"] == before["containers"]
    assert Path(after["payload"]).read_bytes() == source
    assert not any(
        call[:2] in (["machine", "stop"], ["machine", "rm"]) for call in after["calls"]
    )
    secret_calls = [call for call in after["calls"] if "secret" in call]
    expected = (
        ["--connection", "rss-aggregator"]
        if engine == "rootless"
        else ["machine", "ssh", "rss-aggregator", "sudo", "podman"]
    ) + ["secret", "ls", "--format", "json"]
    assert expected in secret_calls
    assert all(
        call[-4:] == ["secret", "ls", "--format", "json"] for call in secret_calls
    )
    if stage == "export":
        locator = json.loads((project.state_dir / "last-backup.json").read_text())
        assert Path(locator["archive"]).is_file()
    else:
        assert not project.backups.exists()


def test_delete_volume_is_separate_previewed_confirmed_and_never_prunes_backups(
    project,
):
    state = project.engine()
    state["volumes"]["old-recovery"] = state["volumes"][VOLUME]
    project.update(**state)
    backup = preview(project, "backup")
    archive = Path(backup["archive"])
    report = preview(project, "delete-volume", "old-recovery")
    assert report["preview"] is True
    assert "old-recovery" in project.engine()["volumes"]
    result = project.run(
        "delete-volume", "old-recovery", "--confirm", report["confirmation"]
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["deleted"] is True
    assert "old-recovery" not in project.engine()["volumes"]
    assert VOLUME in project.engine()["volumes"]
    assert archive.is_file()
    assert preview(project, "delete-volume", "old-recovery")["status"] == "absent"


@pytest.mark.parametrize(
    "fault", ["selected", "attached", "foreign", "stale", "mounted"]
)
def test_permanent_deletion_refuses_active_foreign_or_changed_volume(project, fault):
    state = project.engine()
    state["volumes"]["old"] = dict(state["volumes"][VOLUME])
    project.update(**state)
    plan = preview(project, "delete-volume", "old")
    state = project.engine()
    name = "old"
    if fault == "selected":
        name = VOLUME
    elif fault == "attached":
        state["containers"][APP]["Mounts"].append({"Name": "old"})
    elif fault == "foreign":
        state["volumes"]["old"]["Labels"] = {}
    elif fault == "stale":
        state["volumes"]["old"]["CreatedAt"] = "different identity"
    else:
        state["volumes"]["old"]["MountCount"] = 1
    project.update(**state)
    result = project.run("delete-volume", name, "--confirm", plan["confirmation"])
    assert result.returncode == 1
    assert project.engine()["volumes"] == state["volumes"]


def test_cleanup_partial_setup_and_missing_source_never_expands_scope(project):
    project.update(machine=None, connection=None, volumes={}, containers={})
    (project.state_dir / "owner.json").unlink()
    assert preview(project, "remove-vm")["status"] == "absent"
    assert not (project.state_dir / "removed-vm.json").exists()


@pytest.mark.parametrize(
    "command", [("remove-vm",), ("delete-volume", "absent-recovery")]
)
def test_absent_cleanup_targets_ignore_invalid_confirmation_without_mutation(
    project, command
):
    if command == ("remove-vm",):
        project.update(machine=None, connection=None, volumes={}, containers={})
    before = project.engine()
    ledgers = {
        path.name: path.read_bytes() for path in project.state_dir.glob("*.json")
    }
    result = project.run(*command, "--confirm", "old-confirmation")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "absent"
    assert report["preview"] is True
    assert {
        key: value for key, value in project.engine().items() if key != "calls"
    } == {key: value for key, value in before.items() if key != "calls"}
    assert {
        path.name: path.read_bytes() for path in project.state_dir.glob("*.json")
    } == ledgers
    assert not project.backups.exists()


def test_recreate_after_removal_retires_only_generation_bound_records(project):
    from rss_aggregator.runtime import Runtime

    report = preview(project, "remove-vm")
    result = project.run("remove-vm", "--confirm", report["confirmation"])
    assert result.returncode == 0, result.stderr
    (project.state_dir / "selected-volume.json").write_text(
        json.dumps({"volume": "old-selected"})
    )
    sentinel = project.state_dir / "unrelated.json"
    sentinel.write_text("preserved")
    # Setup is a public command; this substitute still observes all preceding
    # native ownership checks, then stops at unavailable host bootstrap.
    result = project.run("setup")
    assert result.returncode == 1
    assert "Homebrew" in result.stderr
    assert not (project.state_dir / "owner.json").exists()
    assert not (project.state_dir / "selected-volume.json").exists()
    assert sentinel.read_text() == "preserved"
    assert list(project.state_dir.glob("retired-*/owner.json"))
    assert Runtime(state_dir=project.state_dir).volume == VOLUME


def test_removed_vm_can_be_bootstrapped_then_restored_before_any_empty_app_start(
    project, tmp_path
):
    import sys

    plan = preview(project, "remove-vm")
    result = project.run("remove-vm", "--confirm", plan["confirmation"])
    assert result.returncode == 0, result.stderr
    backup = json.loads(result.stdout)["backup"]
    old_owner = (project.state_dir / "owner.json").read_bytes()
    brew_state = tmp_path / "brew.json"
    brew_state.write_text(
        json.dumps({"packages": {"podman": "6.2.0", "pre-existing": "1.0"}})
    )
    project.env["FAKE_BREW_INSTALLED"] = str(brew_state)
    brew = Path(project.env["PATH"]) / "brew"
    brew.write_text(
        "#!"
        + sys.executable
        + "\n"
        + (Path(__file__).parent / "fake_bootstrap_brew.py").read_text()
    )
    brew.chmod(0o755)
    setup = preview(project, "setup")
    assert setup["created"] is True
    assert (project.state_dir / "owner.json").read_bytes() != old_owner
    assert project.engine()["machine"]["State"] == "stopped"
    assert not project.engine()["volumes"]
    restored = preview(
        project, "restore", backup["archive"], "--volume", "recreated-data"
    )
    assert restored["restored"] is True and restored["selected"] is False
    assert not project.engine()["containers"]
    assert preview(project, "select-volume", "recreated-data")["selected"] is True
    assert Path(backup["archive"]).is_file()


def test_absent_vm_retry_finishes_only_owned_removal_hold(project):
    plan = preview(project, "remove-vm")
    result = project.run("remove-vm", "--confirm", plan["confirmation"])
    assert result.returncode == 0, result.stderr
    receipt_path = project.state_dir / "removed-vm.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["status"] = "removing"
    receipt_path.write_text(json.dumps(receipt))
    hold = project.state_dir / "maintenance.json"
    hold.write_text(
        json.dumps(
            {
                "version": 1,
                "owner": receipt["owner"]["token"],
                "operation": "stop",
                "phase": "stopping_vm",
            }
        )
    )
    assert preview(project, "remove-vm")["status"] == "absent"
    assert not hold.exists()
    assert json.loads(receipt_path.read_text())["status"] == "removed"
    hold.write_text(
        json.dumps(
            {
                "version": 1,
                "owner": "foreign",
                "operation": "stop",
                "phase": "stopping_vm",
            }
        )
    )
    refused = project.run("remove-vm")
    assert refused.returncode == 1
    assert hold.exists()


@pytest.mark.parametrize(
    "fault",
    [
        "default",
        "root_identity",
        "registered_job",
        "rootful_container",
        "rootful_image",
        "bad_inventory",
    ],
)
def test_vm_preview_refuses_changed_connections_jobs_and_unrelated_guest_resources(
    project, fault
):
    state = project.engine()
    if fault == "default":
        state["connection"]["Default"] = True
        state["other_connections"] = [{"Name": "other", "Default": False}]
    elif fault == "root_identity":
        state["other_connections"] = [
            {
                "Name": "rss-aggregator-root",
                "URI": "ssh://root@foreign/run/podman/podman.sock",
                "Identity": "/foreign",
                "IsMachine": True,
            }
        ]
    elif fault == "registered_job":
        (project.state_dir / "supervision.json").write_text("{}")
    elif fault == "rootful_container":
        state["rootful"] = [{"Names": ["unrelated-stopped"]}]
    elif fault == "rootful_image":
        state["rootful_images"] = [{"Id": "foreign"}]
    else:
        state["rootful_volumes"] = {}  # Not a valid empty resource-list response.
    project.update(**state)
    refused = project.run("remove-vm")
    assert refused.returncode == 1
    assert not project.backups.exists()
    assert project.engine()["volumes"] == state["volumes"]


def test_native_removal_failure_retains_fresh_backup_source_and_durable_hold(project):
    project.update(fail_rm=True)
    plan = preview(project, "remove-vm")
    refused = project.run("remove-vm", "--confirm", plan["confirmation"])
    assert refused.returncode == 1
    assert project.engine()["machine"]["State"] == "stopped"
    assert VOLUME in project.engine()["volumes"]
    assert (project.state_dir / "maintenance.json").exists()
    receipt = json.loads((project.state_dir / "removed-vm.json").read_text())
    assert receipt["status"] == "removing"
    assert Path(receipt["backup"]["archive"]).is_file()
    assert not project.run("start").returncode == 0


def test_vm_removal_refuses_resources_added_during_export(project):
    plan = preview(project, "remove-vm")
    project.update(add_volume_during_export=True)
    result = project.run("remove-vm", "--confirm", plan["confirmation"])
    assert result.returncode == 1
    assert project.engine()["machine"] is not None
    assert "appeared-during-export" in project.engine()["volumes"]
    assert (project.state_dir / "last-backup.json").exists()


@pytest.mark.parametrize("stage", ["export", "final_inventory"])
def test_vm_removal_refuses_resumed_writer_and_preserves_post_capture_data(
    project, stage
):
    plan = preview(project, "remove-vm")
    before = project.engine()
    project.update(resume_writer_at=stage)
    result = project.run("remove-vm", "--confirm", plan["confirmation"])
    after = project.engine()
    assert after["writer_resumed"] is True
    if stage == "final_inventory":
        calls = after["calls"]
        export_index = next(
            i
            for i, call in enumerate(calls)
            if call[:4] == ["--connection", "rss-aggregator", "volume", "export"]
        )
        resumed_index = after["writer_resumed_call_index"]
        reads = [
            i
            for i in range(export_index + 1, resumed_index + 1)
            if calls[i] == ["system", "connection", "list", "--format", "json"]
        ]
        assert len(reads) == 3 and reads[-1] == resumed_index
        assert any(
            call[:4] == ["--connection", "rss-aggregator", "image", "inspect"]
            for call in calls[reads[1] : reads[2]]
        )
        assert (project.state_dir / "removed-vm.json").exists()
        assert calls[resumed_index + 1] == [
            "machine",
            "ssh",
            "rss-aggregator",
            "sudo",
            "podman",
            "ps",
            "--format",
            "json",
        ]
    with tarfile.open(after["payload"]) as source:
        assert source.extractfile("users/alice/post-capture.txt").read() == (
            b"synthetic post-capture change"
        )
    locator = json.loads((project.state_dir / "last-backup.json").read_text())
    archive = Path(locator["archive"])
    with zipfile.ZipFile(archive) as backup:
        with tarfile.open(fileobj=io.BytesIO(backup.read("data.tar"))) as captured:
            assert "users/alice/post-capture.txt" not in captured.getnames()
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout == ""
    assert after["machine"] == before["machine"]
    assert after["volumes"] == before["volumes"]
    assert after["containers"][APP]["State"]["Running"] is True
    assert not any(
        call[:2] in (["machine", "stop"], ["machine", "rm"]) for call in after["calls"]
    )
    assert not any(
        call[:3] == ["--connection", "rss-aggregator", "stop"]
        for call in after["calls"]
    )
    hold = project.state_dir / "maintenance.json"
    assert hold.exists()
    assert project.run("start").returncode == 1
    retained = archive.read_bytes()
    assert project.run("remove-vm", "--confirm", plan["confirmation"]).returncode == 1
    assert archive.read_bytes() == retained
    assert hold.exists()
    assert "fresh quiesced capture" in result.stderr

    # Reconciliation stops the owned resumed writer but cannot make the old
    # archive current. A new apply must export the intervening source changes.
    assert project.run("backup-reconcile").returncode == 0
    assert not hold.exists()
    fresh_plan = preview(project, "remove-vm")
    retry = project.run("remove-vm", "--confirm", fresh_plan["confirmation"])
    assert retry.returncode == 0, retry.stderr
    report = json.loads(retry.stdout)
    fresh_archive = Path(report["backup"]["archive"])
    assert fresh_archive != archive
    assert archive.read_bytes() == retained
    with zipfile.ZipFile(fresh_archive) as backup:
        with tarfile.open(fileobj=io.BytesIO(backup.read("data.tar"))) as captured:
            assert captured.extractfile("users/alice/post-capture.txt").read() == (
                b"synthetic post-capture change"
            )
    assert project.engine()["writers_at_vm_stop"] == []
    assert project.engine()["machine"] is None


def test_running_source_can_be_quiesced_without_invalidating_immutable_volume_identity(
    project,
):
    state = project.engine()
    state["containers"][APP]["State"]["Running"] = True
    state["volumes"][VOLUME]["MountCount"] = 1
    project.update(**state)
    plan = preview(project, "remove-vm")
    result = project.run("remove-vm", "--confirm", plan["confirmation"])
    assert result.returncode == 0, result.stderr
    assert project.engine()["machine"] is None
