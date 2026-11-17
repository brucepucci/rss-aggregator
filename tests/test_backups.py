"""Backup and recovery behavior through the installed command boundary."""

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VOLUME = "rss-aggregator-data"
APP = "rss-aggregator-freshrss"
LABEL = "io.rss-aggregator.owner"


class BackupProject:
    def __init__(self, tmp_path):
        self.state_dir = tmp_path / "state"
        self.state_dir.mkdir()
        self.backups = tmp_path / "external-backups"
        self.engine_path = tmp_path / "engine.json"
        payload = tmp_path / "source.tar"
        with tarfile.open(payload, "w") as archive:
            for name, content in {
                "config.php": b"synthetic configuration",
                "users/alice/db.sqlite": b"synthetic database",
                "cache/feed.xml": b"synthetic feed cache",
            }.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.uid = info.gid = 33
                info.mode = 0o600
                archive.addfile(info, io.BytesIO(content))
        machine = {
            "Name": "rss-aggregator",
            "Created": "fixture",
            "ConfigDir": {"Path": "/fixture"},
            "Rootful": False,
            "SSHConfig": {"Port": 12345, "IdentityPath": "/fixture/key"},
            "State": "running",
        }
        connection = {
            "Name": "rss-aggregator",
            "URI": "ssh://fixture",
            "Identity": "/fixture/key",
        }
        owner = {"token": "fixture-owner", "machine": machine, "connection": connection}
        (self.state_dir / "owner.json").write_text(json.dumps(owner))
        self.engine_path.write_text(
            json.dumps(
                {
                    "machine": machine,
                    "connection": connection,
                    "containers": {
                        APP: {
                            "Config": {"Labels": {LABEL: "fixture-owner"}},
                            "State": {"Running": False, "Status": "exited"},
                            "Mounts": [
                                {
                                    "Name": VOLUME,
                                    "Destination": "/var/www/FreshRSS/data",
                                }
                            ],
                            "Image": "fixture-image",
                        }
                    },
                    "volumes": {VOLUME: {"Labels": {LABEL: "fixture-owner"}}},
                    "payload": str(payload),
                    "image": json.loads((ROOT / "config/image.json").read_text()),
                    "calls": [],
                }
            )
        )
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        podman = bin_dir / "podman"
        podman.write_text(
            "#!"
            + sys.executable
            + "\n"
            + (ROOT / "tests/fake_backup_podman.py").read_text()
        )
        podman.chmod(0o755)
        self.env = dict(
            os.environ,
            PATH=str(bin_dir),
            RSS_STATE_DIR=str(self.state_dir),
            RSS_BACKUP_DIR=str(self.backups),
            FAKE_BACKUP_ENGINE=str(self.engine_path),
        )

    def run(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "rss_aggregator", *args],
            env=self.env,
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def engine(self):
        return json.loads(self.engine_path.read_text())

    def update(self, **values):
        state = self.engine()
        state.update(values)
        self.engine_path.write_text(json.dumps(state))


@pytest.fixture
def project(tmp_path):
    return BackupProject(tmp_path)


def test_backup_creates_private_verified_full_state_archive_without_starting_app(
    project,
):
    result = project.run("backup")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["verified"] is True
    path = Path(report["archive"])
    assert path.parent == project.backups
    assert report["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert path.stat().st_mode & 0o777 == 0o600
    assert project.backups.stat().st_mode & 0o777 == 0o700
    with zipfile.ZipFile(path) as archive:
        assert set(archive.namelist()) == {"manifest.json", "data.tar"}
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["format"] == "rss-aggregator-backup"
        assert manifest["format_version"] == 1
        assert manifest["image"]["version"] == "1.30.4"
        with tarfile.open(fileobj=io.BytesIO(archive.read("data.tar"))) as data:
            assert (
                data.extractfile("users/alice/db.sqlite").read()
                == b"synthetic database"
            )
            assert data.extractfile("cache/feed.xml").read() == b"synthetic feed cache"
    state = project.engine()
    assert state["machine"]["State"] == "running"
    assert state["containers"][APP]["State"]["Running"] is False
    assert "synthetic configuration" not in result.stdout + result.stderr


@pytest.mark.parametrize("has_locator", [False, True])
def test_recovery_runner_preserves_real_backup_locator(project, has_locator):
    from tests.integration.replacement_support import replacement_names

    token = "0123456789abcdef"
    app, source, _check = replacement_names(token)
    state = project.engine()
    container = state["containers"].pop(APP)
    container["Mounts"][0]["Name"] = source
    state["containers"][app] = container
    state["volumes"][source] = state["volumes"].pop(VOLUME)
    project.update(**state)
    evidence = {
        name: project.state_dir / name
        for name in (
            "backup-attempt.json",
            "backup-success.json",
            "operator-intent.json",
        )
    }
    for path in evidence.values():
        path.write_bytes(b'{"preserve":"real owner evidence"}\n')
    locator = project.state_dir / "last-backup.json"
    original = b'{"archive":"/private/durable/user-backup.zip","sha256":"original"}\n'
    if has_locator:
        locator.write_bytes(original)
    result = subprocess.run(
        [sys.executable, "-m", "tests.integration.backup_runner"],
        input=json.dumps({"token": token, "operation": "backup"}),
        env=project.env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    if has_locator:
        assert locator.read_bytes() == original
    else:
        assert not locator.exists()
    for path in evidence.values():
        assert path.read_bytes() == b'{"preserve":"real owner evidence"}\n'
    fixture_locator = project.state_dir / ("test-" + token + "-last-backup.json")
    assert (
        json.loads(fixture_locator.read_text())["archive"]
        == json.loads(result.stdout)["archive"]
    )


def test_failed_restore_target_cannot_be_selected(project):
    backup = project.run("backup")
    assert backup.returncode == 0, backup.stderr
    archive = json.loads(backup.stdout)["archive"]
    project.update(fail_import=True)
    restored = project.run("restore", archive, "--volume", "recovery-partial")
    assert restored.returncode == 1
    assert "incomplete" in restored.stderr
    assert "sensitive filename" not in restored.stderr
    selected = project.run("select-volume", "recovery-partial")
    assert selected.returncode == 1
    assert "incomplete" in selected.stderr
    assert not (project.state_dir / "selected-volume.json").exists()
    assert VOLUME in project.engine()["volumes"]


@pytest.mark.parametrize("failed", [False, True])
def test_shutdown_backs_up_stopped_writers_before_vm_exit(project, failed):
    good = project.run("backup")
    assert good.returncode == 0, good.stderr
    previous = Path(json.loads(good.stdout)["archive"])
    previous_bytes = previous.read_bytes()
    state = project.engine()
    state["containers"][APP]["State"]["Running"] = True
    state["fail_export"] = failed
    project.update(**state)
    result = project.run("stop")
    state = project.engine()
    assert state["containers"][APP]["State"]["Running"] is False
    assert previous.read_bytes() == previous_bytes
    assert VOLUME in state["volumes"]
    assert "sensitive filename" not in result.stdout + result.stderr
    if failed:
        assert result.returncode == 1
        assert result.stdout == ""
        assert "VM remains running" in result.stderr
        assert state["machine"]["State"] == "running"
        assert list(project.backups.iterdir()) == [previous]
    else:
        assert result.returncode == 0, result.stderr
        assert state["machine"]["State"] == "stopped"
        assert json.loads(result.stdout)["backup"]["verified"]
        assert len(list(project.backups.glob("*.zip"))) == 2
        repeated = project.run("stop")
        assert json.loads(repeated.stdout)["already_stopped"] is True
        assert len(list(project.backups.glob("*.zip"))) == 2


@pytest.mark.parametrize("failed", [False, True])
def test_backup_of_stopped_vm_preserves_stopped_vm_and_app(project, failed):
    state = project.engine()
    state["machine"]["State"] = "stopped"
    state["fail_export"] = failed
    project.update(**state)
    result = project.run("backup")
    assert result.returncode == (1 if failed else 0), result.stderr
    state = project.engine()
    assert state["machine"]["State"] == "stopped"
    assert state["containers"][APP]["State"]["Running"] is False
    if not failed:
        assert json.loads(result.stdout)["verified"]


@pytest.mark.parametrize("destination", [None, "relative", "checkout", "public"])
def test_backup_requires_explicit_private_external_destination(project, destination):
    if destination is None:
        project.env.pop("RSS_BACKUP_DIR")
    elif destination == "checkout":
        project.env["RSS_BACKUP_DIR"] = str(ROOT / "backups")
    elif destination == "public":
        project.backups.mkdir(mode=0o755)
        project.backups.chmod(0o755)
    else:
        project.env["RSS_BACKUP_DIR"] = destination
    result = project.run("backup")
    assert result.returncode == 1
    assert "RSS_BACKUP_DIR" in result.stderr
    assert not project.engine()["calls"]


def test_restore_is_fresh_and_explicit_selection_preserves_source(project):
    backup = project.run("backup")
    assert backup.returncode == 0, backup.stderr
    archive = json.loads(backup.stdout)["archive"]
    restored = project.run("restore", archive, "--volume", "recovered")
    assert restored.returncode == 0, restored.stderr
    assert json.loads(restored.stdout)["selected"] is False
    assert project.engine()["volumes"]["recovered"]["restored"]
    assert not (project.state_dir / "selected-volume.json").exists()
    conflict = project.run("restore", archive, "--volume", "recovered")
    assert conflict.returncode == 1
    assert "already exists" in conflict.stderr
    selected = project.run("select-volume", "recovered")
    assert selected.returncode == 0, selected.stderr
    assert json.loads((project.state_dir / "selected-volume.json").read_text()) == {
        "volume": "recovered"
    }
    assert VOLUME in project.engine()["volumes"]
    # Selection is explicit, but the old stopped container still references the
    # original volume until replace. Backup must capture the selected recovery.
    second_backup = project.run("backup")
    assert second_backup.returncode == 0, second_backup.stderr


def test_shutdown_without_backup_configuration_leaves_writers_stopped(project):
    state = project.engine()
    state["containers"][APP]["State"]["Running"] = True
    project.update(**state)
    project.env.pop("RSS_BACKUP_DIR")
    result = project.run("stop")
    assert result.returncode == 1
    assert "RSS_BACKUP_DIR" in result.stderr
    assert project.engine()["machine"]["State"] == "running"
    assert project.engine()["containers"][APP]["State"]["Running"] is False
    assert VOLUME in project.engine()["volumes"]


def test_bad_export_never_publishes_or_damages_previous_backup(project):
    first = project.run("backup")
    assert first.returncode == 0, first.stderr
    previous = Path(json.loads(first.stdout)["archive"])
    previous_bytes = previous.read_bytes()
    Path(project.engine()["payload"]).write_bytes(
        b"truncated data with synthetic secret"
    )
    result = project.run("backup")
    assert result.returncode == 1
    assert "Invalid backup archive" in result.stderr
    assert "synthetic secret" not in result.stdout + result.stderr
    assert list(project.backups.iterdir()) == [previous]
    assert previous.read_bytes() == previous_bytes


def test_restore_preserves_running_source_and_selection_refuses_to_switch_it(project):
    first = project.run("backup")
    assert first.returncode == 0, first.stderr
    state = project.engine()
    state["containers"][APP]["State"]["Running"] = True
    project.update(**state)
    result = project.run(
        "restore", json.loads(first.stdout)["archive"], "--volume", "recovered"
    )
    assert result.returncode == 0, result.stderr
    result = project.run("select-volume", "recovered")
    assert result.returncode == 1
    assert "Stop the application" in result.stderr
    assert project.engine()["containers"][APP]["State"]["Running"] is True
    assert not (project.state_dir / "selected-volume.json").exists()


@pytest.mark.parametrize("operation", ["backup", "restore", "select-volume"])
def test_recovery_help_does_not_touch_the_engine(project, operation):
    result = project.run(operation, "--help")
    assert result.returncode == 0
    assert "Usage:" in result.stdout
    assert not project.engine()["calls"]


def test_backup_refuses_wrong_image_without_interrupting_running_source(project):
    state = project.engine()
    state["containers"][APP]["State"]["Running"] = True
    state["containers"][APP]["Image"] = "another-image"
    project.update(**state)
    result = project.run("backup")
    assert result.returncode == 1
    assert project.engine()["containers"][APP]["State"]["Running"] is True
    assert not any(call[2:3] == ["stop"] for call in project.engine()["calls"])


def test_restore_does_not_implicitly_activate_the_absent_default_volume(project):
    backup = project.run("backup")
    assert backup.returncode == 0, backup.stderr
    archive = json.loads(backup.stdout)["archive"]
    project.update(volumes={}, containers={}, calls=[])
    result = project.run("restore", archive, "--volume", VOLUME)
    assert result.returncode == 1
    assert "currently selected" in result.stderr
    assert not project.engine()["volumes"]


@pytest.mark.parametrize(
    "invalid",
    [
        "truncated",
        "checksum",
        "version",
        "image",
        "extra",
        "compressed",
        "traversal",
        "absolute",
        "symlink",
        "hardlink",
        "duplicate",
        "file-parent",
        "fifo",
        "pax-traversal",
        "sparse",
        "backslash",
        "normalized-duplicate",
        "truncated-tar",
        "trailing-data",
    ],
)
def test_restore_rejects_invalid_archive_before_engine_mutation(project, invalid):
    backup = project.run("backup")
    assert backup.returncode == 0, backup.stderr
    archive = Path(json.loads(backup.stdout)["archive"])
    with zipfile.ZipFile(archive) as original:
        manifest = json.loads(original.read("manifest.json"))
        data = original.read("data.tar")
    target = project.backups / "untrusted.zip"
    if invalid == "truncated":
        target.write_bytes(archive.read_bytes()[:-10])
    else:
        if invalid == "version":
            manifest["format_version"] = 99
        elif invalid == "image":
            manifest["image"]["platform"] = "linux/amd64"
        elif invalid == "checksum":
            manifest["payload"]["sha256"] = "0" * 64
        elif invalid in {
            "traversal",
            "absolute",
            "symlink",
            "hardlink",
            "duplicate",
            "file-parent",
            "fifo",
            "pax-traversal",
            "sparse",
            "backslash",
            "normalized-duplicate",
        }:
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode="w") as payload:
                member = tarfile.TarInfo(
                    {
                        "traversal": "../escape",
                        "absolute": "/escape",
                        "backslash": "..\\escape",
                    }.get(invalid, "data")
                )
                if invalid in {"symlink", "hardlink"}:
                    member.type = (
                        tarfile.SYMTYPE if invalid == "symlink" else tarfile.LNKTYPE
                    )
                    member.linkname = "../../escape"
                if invalid == "fifo":
                    member.type = tarfile.FIFOTYPE
                elif invalid == "sparse":
                    member.type = tarfile.GNUTYPE_SPARSE
                elif invalid == "pax-traversal":
                    member.pax_headers = {"path": "../escape"}
                payload.addfile(member)
                if invalid == "normalized-duplicate":
                    payload.addfile(tarfile.TarInfo("./data"))
                if invalid in {"duplicate", "file-parent"}:
                    payload.addfile(
                        tarfile.TarInfo(
                            "data" if invalid == "duplicate" else "data/child"
                        )
                    )
            data = output.getvalue()
        elif invalid == "truncated-tar":
            data = data[:700]
        elif invalid == "trailing-data":
            data += b"nonzero trailing content"
        if invalid != "checksum":
            manifest["payload"] = {
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        with zipfile.ZipFile(
            target,
            "w",
            compression=zipfile.ZIP_DEFLATED
            if invalid == "compressed"
            else zipfile.ZIP_STORED,
        ) as modified:
            modified.writestr("manifest.json", json.dumps(manifest))
            modified.writestr("data.tar", data)
            if invalid == "extra":
                modified.writestr("extra", "unexpected")
    project.update(calls=[])
    result = project.run("restore", str(target), "--volume", "must-not-exist")
    assert result.returncode == 1
    assert result.stdout == ""
    assert "Invalid backup archive" in result.stderr
    assert project.engine()["calls"] == []
    assert set(project.engine()["volumes"]) == {VOLUME}
