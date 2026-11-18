"""Selected-service identity and native timestamp contracts at the CLI boundary."""

import json
import subprocess
from pathlib import Path

import pytest

from tests.test_backups import VOLUME, BackupProject
from tests.test_cleanup import preview


@pytest.fixture
def project(tmp_path):
    fixture = BackupProject(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    fixture.env["HOME"] = str(home)
    return fixture


def api_info(project, engine):
    prefix = (
        ["--connection", "rss-aggregator"]
        if engine == "rootless"
        else [
            "machine",
            "ssh",
            "--username",
            "core",
            "rss-aggregator",
            "sudo",
            "-n",
            "--",
            "podman",
            "--log-level=warn",
            "--remote",
            "--url",
            "unix:///run/podman/podman.sock",
        ]
    )
    result = subprocess.run(
        [
            str(Path(project.env["PATH"]) / "podman"),
            *prefix,
            "info",
            "--format",
            "json",
        ],
        env=project.env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("engine", ["rootless", "rootful"])
@pytest.mark.parametrize("stage", ["preview", "apply", "export"])
@pytest.mark.parametrize("fault", ["different_store", "unavailable", "malformed"])
def test_selected_api_context_must_match_local_inspection(
    project, engine, stage, fault
):
    info = api_info(project, engine)
    plan = preview(project, "remove-vm") if stage != "preview" else None
    added = {
        "selected_api_secrets": [
            {"ID": "synthetic-secret", "payload": "sensitive sentinel"}
        ]
    }
    if fault == "different_store":
        info["store"]["graphRoot"] += "/selected-service"
        added[engine + "_api_info"] = info
    elif fault == "unavailable":
        added["fail_api_info"] = engine
    else:
        added["malformed_api_info"] = engine
    project.update(
        **({"guest_state_during_export": added} if stage == "export" else added)
    )
    result = project.run(
        "remove-vm", *(["--confirm", plan["confirmation"]] if plan else [])
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout == "" and "sensitive" not in result.stderr
    after = project.engine()
    assert after["selected_api_secrets"] == added["selected_api_secrets"]
    assert after["machine"]["State"] == "running"
    assert VOLUME in after["volumes"]
    assert not any(
        call[:2] in (["machine", "stop"], ["machine", "rm"]) for call in after["calls"]
    )
    if stage == "export":
        locator = json.loads((project.state_dir / "last-backup.json").read_text())
        assert Path(locator["archive"]).is_file()
    else:
        assert not project.backups.exists()


@pytest.mark.parametrize("engine", ["rootless", "rootful"])
@pytest.mark.parametrize(
    "field",
    [
        "runRoot",
        "volumePath",
        "graphDriverName",
        "graphOptions",
        "transientStore",
        "imageCopyTmpDir",
    ],
)
def test_selected_api_storage_fields_are_not_assumed_equivalent(project, engine, field):
    info = api_info(project, engine)
    value = info["store"][field]
    info["store"][field] = (
        value + "/different"
        if isinstance(value, str)
        else {"overlay.mountopt": "different"}
        if isinstance(value, dict)
        else True
    )
    project.update(**{engine + "_api_info": info})
    result = project.run("remove-vm")
    assert result.returncode == 1
    assert result.stdout == ""
    assert project.engine()["machine"]["State"] == "running"


@pytest.mark.parametrize("engine", ["rootless", "rootful"])
@pytest.mark.parametrize(
    "field",
    [
        "graphRoot",
        "runRoot",
        "volumePath",
        "graphDriverName",
        "graphOptions",
        "transientStore",
        "imageCopyTmpDir",
    ],
)
def test_missing_selected_storage_identity_refuses(project, engine, field):
    info = api_info(project, engine)
    del info["store"][field]
    project.update(**{engine + "_api_info": info})
    result = project.run("remove-vm")
    assert result.returncode == 1 and result.stdout == ""
    assert project.engine()["machine"]["State"] == "running"


@pytest.mark.parametrize("engine", ["rootless", "rootful"])
@pytest.mark.parametrize(
    "field", ["serviceIsRemote", "databaseBackend", "networkBackend", "eventLogger"]
)
def test_selected_api_mode_and_database_metadata_must_match(project, engine, field):
    info = api_info(project, engine)
    info["host"][field] = (
        False if field == "serviceIsRemote" else "sensitive-other-backend"
    )
    project.update(**{engine + "_api_info": info})
    result = project.run("remove-vm")
    assert result.returncode == 1 and result.stdout == ""
    assert "sensitive" not in result.stderr


def test_service_context_is_bound_to_confirmation(project):
    plan = preview(project, "remove-vm")
    remote = api_info(project, "rootless")
    remote["store"]["imageCopyTmpDir"] = "/var/tmp/changed"
    local = json.loads(json.dumps(remote))
    local["host"]["serviceIsRemote"] = False
    project.update(rootless_info=local, rootless_api_info=remote)
    changed = preview(project, "remove-vm")
    assert changed["confirmation"] != plan["confirmation"]
    result = project.run("remove-vm", "--confirm", plan["confirmation"])
    assert result.returncode == 1
    assert not project.backups.exists()


@pytest.mark.parametrize("engine", ["rootless", "rootful"])
@pytest.mark.parametrize(
    "pair",
    [
        ["2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z"],
        ["2026-01-01T00:00:00.1Z", "2026-01-01T00:00:00.2Z"],
        ["2026-01-01T00:00:00.123456Z", "2026-01-01T00:00:00.123457Z"],
        ["2026-01-01T00:00:00.123456700Z", "2026-01-01T00:00:00.123456701Z"],
        ["2026-01-01T01:00:00.123456700+01:00", "2026-01-01T00:00:00.123456701+00:00"],
    ],
)
def test_native_rfc3339nano_network_times_allow_preview(project, engine, pair):
    project.update(network_creation_times={engine: pair})
    assert preview(project, "remove-vm")["preview"] is True


@pytest.mark.parametrize(
    "stamp",
    [
        "2026-01-01T00:00:00",
        "2026-01-01 00:00:00Z",
        "2026-01-01T00:00:00.1234567890Z",
        "2026-01-01T00:00:00.Z",
        "2026-01-01T00:00:00+00:60",
        "2026-01-01T00:00:00+24:00",
        "2026-02-30T00:00:00Z",
        "sensitive-invalid-time",
    ],
)
def test_malformed_native_network_times_refuse_without_diagnostics(project, stamp):
    project.update(network_creation_times={"rootless": [stamp, "2027-01-01T00:00:00Z"]})
    result = project.run("remove-vm")
    assert result.returncode == 1
    assert result.stdout == "" and "sensitive" not in result.stderr
    assert project.engine()["machine"]["State"] == "running"


def test_nanosecond_stock_network_replacement_retains_persisted_safeguard(project):
    project.update(
        network_creation_times={"rootless": ["2026-01-01T00:00:00.123456789Z"]}
    )
    result = project.run("remove-vm")
    assert result.returncode == 1
    assert project.engine()["machine"]["State"] == "running"
