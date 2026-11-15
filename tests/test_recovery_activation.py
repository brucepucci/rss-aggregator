"""Guard first restored-volume activation through the lifecycle commands."""

import io
import json
import shutil
import tarfile
from pathlib import Path

import pytest

from rss_aggregator.archives import create_archive
from rss_aggregator.runtime import RuntimeFailure
from tests import test_runtime_recovery

lifecycle = test_runtime_recovery.lifecycle


@pytest.fixture
def restored_project(lifecycle, tmp_path):
    runtime, engine = lifecycle
    root = tmp_path / "checkout"
    shutil.copytree(runtime.root / "config", root / "config")
    runtime.root = root
    pin_path = root / "config/image.json"
    older_pin = pin_path.read_bytes()
    newer = json.loads(older_pin)
    newer["version"] = "1.31.0"
    newer["image"] = newer["image"].split("@")[0] + "@sha256:" + "1" * 64
    newer["platform_manifest"] = "sha256:" + "2" * 64
    pin_path.write_text(json.dumps(newer))
    payload = tmp_path / "newer-data.tar"
    with tarfile.open(payload, "w") as data:
        member = tarfile.TarInfo("config.php")
        member.size = len(b"synthetic newer configuration")
        data.addfile(member, io.BytesIO(b"synthetic newer configuration"))
    archives = tmp_path / "archives"
    archives.mkdir(mode=0o700)
    report = create_archive(payload, archives, newer)
    restored = runtime.run(
        "restore", archive=Path(report["archive"]), volume="recovered"
    )
    assert restored["restored"] and not restored["selected"]
    assert runtime.run("select-volume", volume="recovered")["selected"]
    return runtime, engine, pin_path, older_pin


@pytest.mark.parametrize(
    "operation,old_container", [("start", False), ("replace", False), ("replace", True)]
)
def test_first_recovery_activation_refuses_changed_pin_without_mutation(
    restored_project, operation, old_container
):
    runtime, engine, pin_path, older_pin = restored_project
    if old_container:
        engine.run("podman", "--connection", "rss-aggregator", "create")
    old_app = engine.app
    provenance = runtime.state_dir / "recovery-volumes.json"
    previous = provenance.read_bytes()
    restored_data = dict(engine.restored)
    pin_path.write_bytes(older_pin)
    engine.calls.clear()
    with pytest.raises(RuntimeFailure, match="recorded image"):
        runtime.run(operation)
    assert provenance.read_bytes() == previous
    assert engine.restored == restored_data
    assert engine.app is old_app
    assert not engine.templates
    assert not any(
        call[3] in {"create", "cp", "start", "stop", "rm", "run"}
        for call in engine.calls
    )
    assert not engine.published


def use_configured_image(engine, image):
    engine.image["Id"] = image["platform_manifest"]
    engine.image["RepoDigests"] = [
        image["image"],
        image["image"].split("@")[0] + "@" + image["platform_manifest"],
    ]
    engine.image["Labels"]["org.opencontainers.image.version"] = image["version"]
    engine.image["Labels"]["org.opencontainers.image.revision"] = image["revision"]


def test_matching_first_activation_allows_later_reviewed_pin_update(restored_project):
    runtime, engine, pin_path, _older_pin = restored_project
    original_image = json.loads(pin_path.read_text())
    use_configured_image(engine, original_image)
    assert runtime.run("start")["machine"] == "running"
    assert engine.app["Mounts"][0]["Name"] == "recovered"
    provenance = runtime.state_dir / "recovery-volumes.json"
    assert json.loads(provenance.read_text())["recovered"]["activated"] is True
    updated_image = dict(original_image, version="1.32.0")
    updated_image["image"] = (
        original_image["image"].split("@")[0] + "@sha256:" + "3" * 64
    )
    updated_image["platform_manifest"] = "sha256:" + "4" * 64
    pin_path.write_text(json.dumps(updated_image))
    use_configured_image(engine, updated_image)
    assert runtime.run("replace")["replaced"]
    assert engine.app["State"]["Running"]
    assert json.loads(provenance.read_text())["recovered"]["image"] == updated_image


def test_failed_first_start_keeps_image_guard_on_retry(restored_project):
    runtime, engine, pin_path, older_pin = restored_project
    use_configured_image(engine, json.loads(pin_path.read_text()))
    engine.fail_copy = "public-feed.opml"
    with pytest.raises(RuntimeFailure, match="Injected"):
        runtime.run("start")
    provenance = runtime.state_dir / "recovery-volumes.json"
    previous = provenance.read_bytes()
    pin_path.write_bytes(older_pin)
    engine.calls.clear()
    with pytest.raises(RuntimeFailure, match="recorded image"):
        runtime.run("replace")
    assert provenance.read_bytes() == previous
    assert not engine.app["State"]["Running"]
    assert not any(call[3] in {"start", "stop", "rm"} for call in engine.calls)


def test_backup_before_first_start_keeps_image_guard(restored_project, monkeypatch):
    runtime, engine, pin_path, older_pin = restored_project
    use_configured_image(engine, json.loads(pin_path.read_text()))
    monkeypatch.setenv("RSS_BACKUP_DIR", str(runtime.state_dir / "private-backups"))
    assert runtime.run("backup")["verified"]
    assert engine.app is None
    pin_path.write_bytes(older_pin)
    with pytest.raises(RuntimeFailure, match="recorded image"):
        runtime.run("start")


def test_legacy_provenance_requires_a_matching_start_before_update(restored_project):
    runtime, engine, pin_path, older_pin = restored_project
    provenance = runtime.state_dir / "recovery-volumes.json"
    records = json.loads(provenance.read_text())
    records["recovered"].pop("activated")
    provenance.write_text(json.dumps(records))
    previous = provenance.read_bytes()
    pin_path.write_bytes(older_pin)
    with pytest.raises(RuntimeFailure, match="recorded image"):
        runtime.run("start")
    assert provenance.read_bytes() == previous
