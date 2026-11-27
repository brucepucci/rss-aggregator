"""Native archives and the temporary legacy importer through the public CLI."""

import fcntl
import hashlib
import io
import json
import platform
import stat
import tarfile
import threading
import urllib.request
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from rss_aggregator import cli as command
from rss_aggregator.cli import main
from rss_aggregator.runtime import Runtime

ROOT = Path(__file__).resolve().parents[1]
INCLUDES = "complete FreshRSS data directory; no extensions in initial deployment"
DATA = {
    "config.php": b"synthetic configuration\n",
    "users/alice/db.sqlite": b"synthetic database\0\x01",
    "cache/feed.xml": b"synthetic feed cache\n",
    ".hidden": b"synthetic hidden file\n",
}


@pytest.fixture
def project(tmp_path, monkeypatch):
    state = tmp_path / "state"
    backups = tmp_path / "backups"
    monkeypatch.setenv("RSS_STATE_DIR", str(state))
    monkeypatch.setenv("RSS_BACKUP_DIR", str(backups))
    monkeypatch.setenv("RSS_TAILSCALE", "0")
    return CliRunner(), state, backups


@pytest.fixture
def installed(project, tmp_path, monkeypatch):
    cli, state, backups = project
    checkout = tmp_path / "checkout"
    config = checkout / "config"
    config.mkdir(parents=True)
    (config / "image.json").write_bytes((ROOT / "config/image.json").read_bytes())
    pin = json.loads((ROOT / "config/native.json").read_text())
    source, runtime = pin["freshrss"], pin["runtime"]
    root = "FreshRSS-" + source["revision"]
    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
        for name in (root, root + "/p", root + "/app", root + "/cli"):
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            archive.addfile(member)
        for name, body in {
            "constants.php": f"const FRESHRSS_VERSION = '{source['version']}';\n".encode(),
            "p/index.php": b"synthetic test input",
            "app/actualize_script.php": b"synthetic test input",
            "cli/do-install.php": b"synthetic test input",
            "cli/create-user.php": b"synthetic test input",
        }.items():
            member = tarfile.TarInfo(root + "/" + name)
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
    executable = (
        "#!/bin/sh\n"
        'case "$1" in\n'
        f"  version) printf '%s\\n' 'FrankenPHP v{runtime['version']} PHP {runtime['php']} Caddy v{runtime['caddy']}' ;;\n"
        f"  php-cli) printf '%s\\n' '{runtime['php']}' ;;\n"
        "  *) exit 1 ;;\nesac\n"
    ).encode()
    fixture_assets = {
        source["url"]: archive_bytes.getvalue(),
        runtime["url"]: executable,
    }
    for artifact in (source, runtime):
        artifact["sha256"] = hashlib.sha256(fixture_assets[artifact["url"]]).hexdigest()
    (config / "native.json").write_text(json.dumps(pin))
    monkeypatch.setattr(command, "Runtime", lambda: Runtime(root=checkout))
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    monkeypatch.setattr(platform, "mac_ver", lambda: ("14.0.0", ("", "", ""), "arm64"))
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda url, **kwargs: io.BytesIO(fixture_assets[url])
    )
    result = cli.invoke(main, ["setup"])
    assert result.exit_code == 0, result.output
    return cli, state, backups


def legacy_archive(path, *, files=DATA, image=None):
    if image is None:
        image = json.loads((ROOT / "config/image.json").read_text())
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        member = tarfile.TarInfo("./cache/empty/")
        member.type = tarfile.DIRTYPE
        archive.addfile(member)
        for name, data in files.items():
            member = tarfile.TarInfo("./" + name)
            member.size = len(data)
            member.uid = member.gid = 33
            member.mode = 0o755
            archive.addfile(member, io.BytesIO(data))
    data = payload.getvalue()
    manifest = {
        "format": "rss-aggregator-backup",
        "format_version": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "image": image,
        "includes": INCLUDES,
        "payload": {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()},
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("data.tar", data)
    return path


@pytest.mark.parametrize(
    "files",
    [
        {"../unsafe": b"x"},
        {"Users/alice/item": b"a", "users/bob/item": b"b"},
        {"users/caf\u00e9/a": b"a", "users/cafe\u0301/b": b"b"},
        {"x/" * 512 + "file": b"deep"},
    ],
    ids=["traversal", "case-alias", "unicode-alias", "excessive-depth"],
)
def test_import_legacy_refuses_unsafe_names_without_publishing(project, files):
    cli, state, backups = project
    archive = legacy_archive(backups.parent / "legacy.zip", files=files)
    result = cli.invoke(main, ["import-legacy", str(archive), "--instance", "fresh"])
    assert result.exit_code == 1, result.output
    assert "Invalid backup archive" in result.output
    assert not (state / "instances/fresh").exists()
    assert not (state / "selected-instance.json").exists()


def test_import_legacy_requires_exact_reviewed_image(project):
    cli, state, backups = project
    image = json.loads((ROOT / "config/image.json").read_text())
    image["revision"] = "unreviewed-revision"
    archive = legacy_archive(backups.parent / "legacy.zip", image=image)
    result = cli.invoke(main, ["import-legacy", str(archive), "--instance", "fresh"])
    assert result.exit_code == 1, result.output
    assert "Invalid backup archive" in result.stderr
    assert not (state / "instances/fresh").exists()


def test_native_restore_does_not_treat_legacy_backups_as_native(project):
    cli, state, backups = project
    archive = legacy_archive(backups.parent / "legacy.zip")
    result = cli.invoke(main, ["restore", str(archive), "--instance", "fresh"])
    assert result.exit_code == 1, result.output
    assert "Invalid backup archive" in result.stderr
    assert not (state / "instances/fresh").exists()


def run(cli, *args):
    result = cli.invoke(main, args)
    assert result.exit_code == 0, (result.output, result.exception)
    return json.loads(result.stdout)


def test_complete_legacy_import_and_native_backup_restore(installed):
    cli, state, backups = installed
    archive = legacy_archive(backups.parent / "legacy.zip")
    original = archive.read_bytes()
    imported = run(cli, "import-legacy", str(archive), "--instance", "imported")
    assert imported["selected"] is False
    assert run(cli, "status")["instance"] == "default"
    assert run(cli, "select-instance", "imported")["selected"] is True

    saved = run(cli, "backup")
    assert saved["verified"] is True
    assert saved["manifest"]["format_version"] == 2
    assert "image" not in saved["manifest"]
    native = Path(saved["archive"])
    assert native.parent == backups
    assert stat.S_IMODE(native.stat().st_mode) == 0o600
    assert saved["sha256"] == hashlib.sha256(native.read_bytes()).hexdigest()

    restored = run(cli, "restore", str(native), "--instance", "restored")
    assert restored["selected"] is False
    assert run(cli, "status")["instance"] == "imported"
    assert archive.read_bytes() == original
    assert run(cli, "select-instance", "restored")["selected"] is True
    for name in ("imported", "restored"):
        data = state / "instances" / name / "data"
        assert {
            path.relative_to(data).as_posix(): path.read_bytes()
            for path in data.rglob("*")
            if path.is_file()
        } == DATA
        assert (data / "cache/empty").is_dir()
        for path in (data, *data.rglob("*")):
            assert stat.S_IMODE(path.stat().st_mode) == (
                0o700 if path.is_dir() else 0o600
            )


def test_restore_refuses_selected_existing_and_wrong_runtime(installed):
    cli, state, backups = installed
    archive = legacy_archive(backups.parent / "legacy.zip")
    run(cli, "import-legacy", str(archive), "--instance", "source")
    run(cli, "select-instance", "source")
    native = Path(run(cli, "backup")["archive"])
    owned = state / "instances/source/data/.hidden"
    duplicate = cli.invoke(main, ["restore", str(native), "--instance", "source"])
    assert duplicate.exit_code == 1
    assert "currently selected" in duplicate.stderr
    assert owned.read_bytes() == DATA[".hidden"]

    collision = state / "instances/occupied"
    collision.mkdir(mode=0o700)
    duplicate = cli.invoke(main, ["restore", str(native), "--instance", "occupied"])
    assert duplicate.exit_code == 1
    assert "already exists" in duplicate.stderr
    assert list(collision.iterdir()) == []

    incompatible = backups.parent / "incompatible.zip"
    with (
        zipfile.ZipFile(native) as archive,
        zipfile.ZipFile(incompatible, "w") as altered,
    ):
        manifest = json.loads(archive.read("manifest.json"))
        manifest["runtime"]["runtime_sha256"] = "c" * 64
        altered.writestr("manifest.json", json.dumps(manifest))
        altered.writestr("data.tar", archive.read("data.tar"))
    bad = cli.invoke(main, ["restore", str(incompatible), "--instance", "fresh"])
    assert bad.exit_code == 1
    assert "Invalid backup archive" in bad.stderr
    assert not (state / "instances/fresh").exists()


def test_failed_backup_leaves_previous_verified_archive_unchanged(installed):
    cli, state, backups = installed
    archive = legacy_archive(backups.parent / "legacy.zip")
    run(cli, "import-legacy", str(archive), "--instance", "source")
    run(cli, "select-instance", "source")
    saved = Path(run(cli, "backup")["archive"])
    before = saved.read_bytes()
    outside = backups.parent / "do-not-copy"
    outside.write_bytes(b"synthetic unrelated data")
    (state / "instances/source/data/link").symlink_to(outside)

    result = cli.invoke(main, ["backup"])
    assert result.exit_code == 1
    assert "unsafe file or directory" in result.stderr
    assert list(backups.iterdir()) == [saved]
    assert saved.read_bytes() == before
    assert outside.read_bytes() == b"synthetic unrelated data"
    assert run(cli, "status")["application"] == "stopped"


def test_interrupted_export_keeps_last_archive_and_requires_reconciliation(
    installed, monkeypatch
):
    cli, _, backups = installed
    source = legacy_archive(backups.parent / "legacy.zip")
    run(cli, "import-legacy", str(source), "--instance", "source")
    run(cli, "select-instance", "source")
    saved = Path(run(cli, "backup")["archive"])
    before = saved.read_bytes()

    def stop_during_write(*args, **kwargs):
        raise KeyboardInterrupt

    with monkeypatch.context() as interrupted:
        interrupted.setattr(tarfile.TarFile, "addfile", stop_during_write)
        assert cli.invoke(main, ["backup"]).exit_code != 0
    assert list(backups.iterdir()) == [saved]
    assert saved.read_bytes() == before
    refused = cli.invoke(main, ["start"])
    assert refused.exit_code == 1 and "Maintenance is unresolved" in refused.stderr
    assert run(cli, "backup-reconcile")["resumed"] is False
    assert run(cli, "status")["application"] == "stopped"


def test_shutdown_backups_use_installed_pin_even_after_checkout_pin_change(installed):
    cli, state, backups = installed
    source = legacy_archive(backups.parent / "legacy.zip")
    run(cli, "import-legacy", str(source), "--instance", "source")
    run(cli, "select-instance", "source")
    config = backups.parent / "checkout/config/native.json"
    reviewed = json.loads(config.read_text())
    original_sha = reviewed["runtime"]["sha256"]
    reviewed["runtime"]["sha256"] = "1" * 64
    config.write_text(json.dumps(reviewed))

    stopped = run(cli, "stop")
    assert stopped["application"] == "stopped"
    assert stopped["backup"]["verified"] is True
    assert stopped["backup"]["manifest"]["runtime"]["runtime_sha256"] == original_sha
    assert (state / "instances/source/data/.hidden").read_bytes() == DATA[".hidden"]


def test_backup_remains_verified_when_startup_after_capture_fails(
    installed, monkeypatch
):
    cli, state, backups = installed
    archive = legacy_archive(backups.parent / "legacy.zip")
    run(cli, "import-legacy", str(archive), "--instance", "source")
    run(cli, "select-instance", "source")

    # Model a running controller with the real filesystem lease. Release it only
    # after receiving the stop record; no real web/fetch executable is started.
    for filename, value in {
        "service.json": {
            "nonce": "synthetic",
            "url": "http://127.0.0.1:8080",
            "port": 8080,
        },
        "service-status.json": {"nonce": "synthetic", "web": "started"},
    }.items():
        path = state / filename
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    # A new start would reject RSS_PORT. Resumption must use the observed port,
    # reach service preparation, and fail rather than follow a directory link.
    monkeypatch.setenv("RSS_PORT", "bad-port")
    unrelated = backups.parent / "unrelated"
    unrelated.mkdir()
    (state / "service").symlink_to(unrelated, target_is_directory=True)
    with (
        (state / "writers.lock").open("w") as writer,
        (state / "controller.lock").open("w") as controller_lease,
    ):
        (state / "writers.lock").chmod(0o600)
        fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(controller_lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        observed_stop = threading.Event()

        def acknowledge_stop():
            for _ in range(500):
                if (state / "stop-service.json").exists():
                    observed_stop.set()
                    break
                observed_stop.wait(0.01)
            fcntl.flock(controller_lease, fcntl.LOCK_UN)
            fcntl.flock(writer, fcntl.LOCK_UN)

        controller = threading.Thread(target=acknowledge_stop)
        controller.start()
        try:
            result = cli.invoke(main, ["backup"])
        finally:
            controller.join(timeout=6)
        assert observed_stop.is_set()

    assert result.exit_code == 1, result.output
    assert "service directory symlink" in result.stderr
    status = run(cli, "backup-status")
    assert status["resumption"] == "failed"
    assert status["maintenance_required"] is True
    assert status["latest_success"]["verified"] is True
    assert Path(status["latest_success"]["archive"]).is_file()


def test_backup_refuses_unidentified_writer_state(installed):
    cli, state, backups = installed
    archive = legacy_archive(backups.parent / "legacy.zip")
    run(cli, "import-legacy", str(archive), "--instance", "source")
    run(cli, "select-instance", "source")

    with (state / "writers.lock").open("w") as writer:
        (state / "writers.lock").chmod(0o600)
        fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = cli.invoke(main, ["backup"])
    assert result.exit_code == 1
    assert "writer state is unconfirmed" in result.stderr
    assert not list(backups.glob("*.zip"))
    assert not (state / "stop-service.json").exists()
