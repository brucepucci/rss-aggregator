"""Native archives and the temporary legacy importer through the public CLI."""

import fcntl
import hashlib
import io
import json
import platform
import selectors
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import threading
import urllib.request
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest
from click.testing import CliRunner
from test_private_access import ADDRESS, BACKEND, HOST, PORT, LocalAPI, route

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
    monkeypatch.setenv("RSS_PORT", "8080")
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


@pytest.mark.parametrize("damage", ["payload-digest", "truncated"])
def test_restore_refuses_damaged_native_archive_without_publishing(installed, damage):
    cli, state, backups = installed
    archive = legacy_archive(backups.parent / "legacy.zip")
    run(cli, "import-legacy", str(archive), "--instance", "source")
    run(cli, "select-instance", "source")
    saved = run(cli, "backup")
    native = Path(saved["archive"])
    damaged = backups.parent / "damaged.zip"
    if damage == "truncated":
        damaged.write_bytes(native.read_bytes()[:-32])
    else:
        with (
            zipfile.ZipFile(native) as original,
            zipfile.ZipFile(damaged, "w") as altered,
        ):
            payload = original.read("data.tar")
            altered.writestr("manifest.json", original.read("manifest.json"))
            # Change regular file contents, leaving tar structure and ZIP CRC valid.
            data = DATA["users/alice/db.sqlite"]
            assert data in payload
            altered.writestr("data.tar", payload.replace(data, b"X" + data[1:], 1))

    refused = cli.invoke(main, ["restore", str(damaged), "--instance", "damaged"])
    assert refused.exit_code == 1, refused.output
    assert "Invalid backup archive" in refused.stderr
    assert not (state / "instances/damaged").exists()
    assert run(cli, "status")["instance"] == "source"
    assert hashlib.sha256(native.read_bytes()).hexdigest() == saved["sha256"]


@pytest.mark.parametrize(
    "destination",
    [
        "state",
        "state/new-backup-directory",
        "state/runtime/not-created/backup",
        "state/service/not-created/backup",
        "state/instances/source/data/backup",
        "state/outside-alias/not-created/backup",
        "checkout/not-created/backup",
        "checkout-parent",
        ".",
        "foreign-checkout/not-created/backup",
        "alias/not-created/backup",
    ],
)
def test_backup_destination_must_not_overlap_state_or_checkout(
    installed, monkeypatch, destination
):
    cli, state, backups = installed
    root = backups.parent
    archive = legacy_archive(root / "legacy.zip")
    run(cli, "import-legacy", str(archive), "--instance", "source")
    run(cli, "select-instance", "source")
    foreign = root / "foreign-checkout"
    foreign.mkdir()
    (foreign / ".git").write_text("gitdir: synthetic-worktree\n")
    (root / "alias").symlink_to(state, target_is_directory=True)
    (state / "outside-alias").symlink_to(backups, target_is_directory=True)
    if destination == "checkout-parent":
        parent = root / "checkout-parent"
        parent.mkdir(mode=0o700)
        checkout = parent / "checkout"
        (root / "checkout").rename(checkout)
        monkeypatch.setattr(command, "Runtime", lambda: Runtime(root=checkout))
    target = root / destination
    present = target.exists()
    monkeypatch.setenv("RSS_BACKUP_DIR", str(target))

    result = cli.invoke(main, ["backup"])
    assert result.exit_code == 1, result.output
    assert "RSS_BACKUP_DIR must be outside" in result.stderr
    assert target.exists() == present
    assert not list(target.glob("backup-*.zip"))


@pytest.mark.parametrize(
    "destination",
    [
        "other-state",
        "other-state/runtime/archives",
        "alias-to-other/runtime/archives",
        "other-state/alias-to-outside/archives",
    ],
)
def test_backup_refuses_destinations_in_another_native_state(
    installed, monkeypatch, destination
):
    cli, _, backups = installed
    root = backups.parent
    archive = legacy_archive(root / "legacy.zip")
    run(cli, "import-legacy", str(archive), "--instance", "source")
    run(cli, "select-instance", "source")
    other = root / "other-state"
    with monkeypatch.context() as env:
        env.setenv("RSS_STATE_DIR", str(other))
        run(cli, "setup")
    (root / "alias-to-other").symlink_to(other, target_is_directory=True)
    outside = root / "outside"
    outside.mkdir(mode=0o700)
    (other / "alias-to-outside").symlink_to(outside, target_is_directory=True)
    target = root / destination
    before = set(target.iterdir()) if target.exists() else None

    with monkeypatch.context() as env:
        env.setenv("RSS_BACKUP_DIR", str(target))
        refused = cli.invoke(main, ["backup"])
    assert refused.exit_code == 1, refused.output
    assert "outside every native state directory" in refused.stderr
    assert (set(target.iterdir()) if target.exists() else None) == before
    with monkeypatch.context() as env:
        env.setenv("RSS_STATE_DIR", str(other))
        assert run(cli, "status")["runtime"] == "installed"


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


def test_killed_export_keeps_last_archive_and_requires_reconciliation(installed):
    cli, _, backups = installed
    source = legacy_archive(backups.parent / "legacy.zip")
    run(cli, "import-legacy", str(source), "--instance", "source")
    run(cli, "select-instance", "source")
    saved = Path(run(cli, "backup")["archive"])
    before = saved.read_bytes()

    exporter = """
import sys
import zipfile
from pathlib import Path
from rss_aggregator.runtime import Runtime

original = zipfile.ZipFile.open

def pause_at_zip_write(self, member, mode='r', *args, **kwargs):
    stream = original(self, member, mode, *args, **kwargs)
    if mode == 'w' and member.filename == 'data.tar':
        self.fp.flush()
        print('staging', flush=True)
        sys.stdin.buffer.read(1)
        raise AssertionError('Exporter was not terminated by the test')
    return stream

zipfile.ZipFile.open = pause_at_zip_write
Runtime(root=Path(sys.argv[1])).run('backup')
"""
    with (
        subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                exporter,
                str(backups.parent / "checkout"),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process,
        selectors.DefaultSelector() as ready,
    ):
        try:
            ready.register(process.stdout, selectors.EVENT_READ)
            assert ready.select(timeout=10), "Exporter did not reach ZIP staging"
            assert process.stdout.readline() == b"staging\n"
            active = run(cli, "backup-status")
            assert active["busy"] is True
            assert active["latest_success"]["archive"] == str(saved)
        finally:
            if process.poll() is None:
                process.kill()
            _, diagnostics = process.communicate(timeout=5)
        assert process.returncode == -signal.SIGKILL, diagnostics

    assert list(backups.glob("backup-*.zip")) == [saved]
    assert saved.read_bytes() == before
    status = run(cli, "backup-status")
    assert status["latest_success"]["archive"] == str(saved)
    assert status["latest_attempt"]["outcome"] == "interrupted"
    assert status["maintenance_required"] is True
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


@contextmanager
def controlled_writers(state, port=8080, *, private=False):
    # Hold the real filesystem leases until stop is requested; never run web/PHP.
    for filename, value in {
        "service.json": {
            "nonce": "synthetic",
            "url": f"http://127.0.0.1:{port}",
            "port": port,
            "private": private,
        },
        "service-status.json": {"nonce": "synthetic", "web": "started"},
    }.items():
        path = state / filename
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    with (
        (state / "writers.lock").open("w") as writer,
        (state / "controller.lock").open("w") as controller_lease,
    ):
        (state / "writers.lock").chmod(0o600)
        fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(controller_lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        observed_stop, finished = threading.Event(), threading.Event()

        def acknowledge_stop():
            while not finished.is_set():
                if (state / "stop-service.json").exists():
                    observed_stop.set()
                    break
                finished.wait(0.01)
            fcntl.flock(controller_lease, fcntl.LOCK_UN)
            fcntl.flock(writer, fcntl.LOCK_UN)

        controller = threading.Thread(target=acknowledge_stop)
        controller.start()
        try:
            yield observed_stop
        finally:
            finished.set()
            controller.join(timeout=6)


def test_restore_preserves_running_source_and_selection_refuses_to_switch_it(installed):
    cli, state, backups = installed
    source = legacy_archive(backups.parent / "legacy.zip")
    run(cli, "import-legacy", str(source), "--instance", "source")
    run(cli, "select-instance", "source")
    native = run(cli, "backup")["archive"]

    with controlled_writers(state) as observed_stop:
        assert run(cli, "status")["application"] == "running"

        restored = run(cli, "restore", native, "--instance", "restored")
        assert restored["restored"] is True
        assert restored["instance"] == "restored"
        assert restored["selected"] is False
        data = state / "instances/restored/data"
        assert (data / ".hidden").read_bytes() == DATA[".hidden"]
        status = run(cli, "status")
        assert status["application"] == "running"
        assert status["instance"] == "source"

        refused = cli.invoke(main, ["select-instance", "restored"])
        assert refused.exit_code == 1, refused.output
        assert "Stop FreshRSS" in refused.stderr
        status = run(cli, "status")
        assert status["application"] == "running"
        assert status["instance"] == "source"
        assert not observed_stop.is_set()
        assert not (state / "stop-service.json").exists()

    assert run(cli, "select-instance", "restored")["selected"] is True


def owned_forwarding(state, monkeypatch):
    api = LocalAPI()
    for section, entries in route(ADDRESS).items():
        api.config.setdefault(section, {}).update(entries)
    owner = json.loads((state / "native-owner.json").read_text())["token"]
    record = state / "serve.json"
    record.write_text(
        json.dumps({"owner": owner, "host": HOST, "target": BACKEND, "phase": "active"})
    )
    record.chmod(0o600)
    monkeypatch.setattr(subprocess, "run", api.run_cli)
    monkeypatch.setattr(socket, "create_connection", api.connect)
    return api


def test_shutdown_quiesces_writers_and_forwarding_before_attesting_runtime_layout(
    installed, monkeypatch
):
    cli, state, backups = installed
    source = legacy_archive(backups.parent / "legacy.zip")
    run(cli, "import-legacy", str(source), "--instance", "source")
    run(cli, "select-instance", "source")
    api = owned_forwarding(state, monkeypatch)
    drift = state / "runtime/unexpected"
    drift.write_text("preserve layout drift")

    with controlled_writers(state, PORT) as observed_stop:
        result = cli.invoke(main, ["stop"])
        assert observed_stop.is_set(), result.output

    assert result.exit_code == 1, result.output
    assert "application writers remain stopped" in result.stderr
    assert "layout changed" in result.stderr
    assert api.config == route(target="http://127.0.0.1:8001")
    assert not list(backups.glob("*.zip"))
    assert drift.read_text() == "preserve layout drift"


@pytest.mark.parametrize("damage", ["bad-attempt", "bad-selection", "lost-selection"])
@pytest.mark.parametrize("older_default", [False, True])
def test_stop_closes_owned_service_before_checking_backup_bookkeeping(
    installed, monkeypatch, damage, older_default
):
    cli, state, backups = installed
    # When there is older default data, it must not become a substitute source.
    if older_default:
        backups.mkdir(mode=0o700)
        (state / "service").symlink_to(backups, target_is_directory=True)
        failure = cli.invoke(main, ["start"])
        assert "Native service configuration is not private and owned" in failure.stderr
        (state / "service").unlink()
    archive = legacy_archive(backups.parent / "legacy.zip")
    run(cli, "import-legacy", str(archive), "--instance", "source")
    run(cli, "select-instance", "source")
    baseline = run(cli, "backup")
    api = owned_forwarding(state, monkeypatch)
    path = state / (
        "backup-attempt.json" if damage == "bad-attempt" else "selected-instance.json"
    )
    if damage == "lost-selection":
        path.unlink()
    else:
        path.write_text("{")

    with controlled_writers(state, PORT, private=True) as observed_stop:
        stopped = cli.invoke(main, ["stop"])
        assert observed_stop.is_set(), stopped.output
    assert stopped.exit_code == 1, stopped.output
    assert "writers remain stopped" in stopped.stderr
    assert api.config == route(target="http://127.0.0.1:8001")
    assert list(backups.glob("*.zip")) == [Path(baseline["archive"])]
    if damage == "lost-selection":
        retry = cli.invoke(main, ["start"])
        assert retry.exit_code == 1, retry.output
        assert "Instance selection is missing" in retry.stderr
        assert (state / "instances/default").exists() is older_default
        assert run(cli, "select-instance", "source")["selected"] is True
        assert run(cli, "stop")["backup"]["verified"] is True
    elif damage == "bad-attempt":
        assert path.read_text() == "{"
    else:
        assert run(cli, "backup-status")["latest_attempt"]["outcome"] == "failed"


def test_state_selector_cannot_write_inside_an_installed_release(
    installed, monkeypatch
):
    cli, state, _backups = installed
    for redirected in (state / "runtime", state / "runtime/FreshRSS/typo"):
        with monkeypatch.context() as env:
            env.setenv("RSS_STATE_DIR", str(redirected))
            failure = cli.invoke(main, ["status"])
        assert failure.exit_code == 1, failure.output
        assert "inside an existing native state directory" in failure.stderr
        assert not (redirected / "command.lock").exists()
    assert not (state / "runtime/FreshRSS/typo").exists()
    assert run(cli, "status")["runtime"] == "installed"


@pytest.mark.parametrize(
    ("withdrawn", "diagnostic"),
    [
        (True, "Connect the existing Tailscale installation"),
        (False, "Native service configuration is not private and owned"),
    ],
    ids=["withdrawn-by-backup", "already-removed-externally"],
)
def test_backup_only_attempts_private_restart_if_it_withdrew_forwarding(
    installed, monkeypatch, withdrawn, diagnostic
):
    cli, state, backups = installed
    archive = legacy_archive(backups.parent / "legacy.zip")
    run(cli, "import-legacy", str(archive), "--instance", "source")
    run(cli, "select-instance", "source")
    api = owned_forwarding(state, monkeypatch)
    unrelated_forwarding = route(target="http://127.0.0.1:8001")
    api.commands[("status", "--json")] = (
        0,
        json.dumps({"BackendState": "Stopped"}),
    )
    if not withdrawn:
        # The route was present in CLI status but vanished by the conditional
        # LocalAPI inspection used to decide whether backup can withdraw it.
        api.before_get = lambda: setattr(api, "config", unrelated_forwarding)
    monkeypatch.setenv("RSS_TAILSCALE", "1")
    unrelated = backups.parent / "unrelated"
    unrelated.mkdir()
    (state / "service").symlink_to(unrelated, target_is_directory=True)

    with controlled_writers(state, PORT, private=True) as observed_stop:
        result = cli.invoke(main, ["backup"])
        assert observed_stop.is_set(), result.output

    assert result.exit_code == 1, result.output
    assert diagnostic in result.stderr
    assert api.config == unrelated_forwarding
    assert run(cli, "backup-status")["latest_success"]["verified"] is True


def test_backup_does_not_reopen_a_previously_private_backend_on_public_exposure(
    installed, monkeypatch
):
    cli, state, backups = installed
    archive = legacy_archive(backups.parent / "legacy.zip")
    run(cli, "import-legacy", str(archive), "--instance", "source")
    run(cli, "select-instance", "source")
    api = owned_forwarding(state, monkeypatch)
    # The fake LocalAPI snapshot loses our route but reports public forwarding
    # to the backend on another port. No real forwarding configuration is used.
    foreign_route = route(funnel=True)
    api.before_get = lambda: setattr(api, "config", foreign_route)
    with controlled_writers(state, PORT, private=True) as observed_stop:
        failure = cli.invoke(main, ["backup"])
        assert observed_stop.is_set()
    assert failure.exit_code == 1, failure.output
    assert "Existing Funnel reaches this FreshRSS backend" in failure.stderr
    assert not (state / "service").exists()
    assert api.config == foreign_route
    assert not api.posts
    assert run(cli, "status")["application"] == "stopped"
    report = run(cli, "backup-status")
    assert report["latest_success"]["verified"] is True
    assert report["resumption"] == "failed"


def test_backup_remains_verified_when_startup_after_capture_fails(
    installed, monkeypatch
):
    cli, state, backups = installed
    archive = legacy_archive(backups.parent / "legacy.zip")
    run(cli, "import-legacy", str(archive), "--instance", "source")
    run(cli, "select-instance", "source")

    # A new start would reject RSS_PORT. Resumption must use the observed port,
    # reach service preparation, and fail rather than follow a directory link.
    monkeypatch.setenv("RSS_PORT", "bad-port")
    unrelated = backups.parent / "unrelated"
    unrelated.mkdir()
    (state / "service").symlink_to(unrelated, target_is_directory=True)
    with controlled_writers(state) as observed_stop:
        result = cli.invoke(main, ["backup"])
        assert observed_stop.is_set()

    assert result.exit_code == 1, result.output
    assert "Native service configuration is not private and owned" in result.stderr
    status = run(cli, "backup-status")
    assert status["resumption"] == "failed"
    assert status["maintenance_required"] is True
    assert status["latest_success"]["verified"] is True
    assert Path(status["latest_success"]["archive"]).is_file()


def test_lost_default_is_not_reprovisioned_after_first_start_attempt(installed):
    cli, state, backups = installed
    unrelated = backups.parent / "unrelated"
    unrelated.mkdir()
    (state / "service").symlink_to(unrelated, target_is_directory=True)
    started = cli.invoke(main, ["start"])
    assert started.exit_code == 1, started.output
    assert "Native service configuration is not private and owned" in started.stderr
    default = state / "instances/default"
    preserved = backups.parent / "preserved-default"
    default.rename(preserved)

    stopped = cli.invoke(main, ["stop"])
    assert stopped.exit_code == 1, stopped.output
    assert "Selected instance data are absent" in stopped.stderr
    retry = cli.invoke(main, ["start"])
    assert retry.exit_code == 1, retry.output
    assert "Selected instance data are absent" in retry.stderr
    assert not default.exists()
    assert (preserved / "data").is_dir()


def test_backup_evidence_binds_to_selected_writer_generation(installed):
    cli, state, backups = installed
    unrelated = backups.parent / "unrelated"
    unrelated.mkdir()
    (state / "service").symlink_to(unrelated, target_is_directory=True)

    def attempt_start():
        started = cli.invoke(main, ["start"])
        assert started.exit_code == 1, started.output
        assert "Native service configuration is not private and owned" in started.stderr

    attempt_start()
    stopped = run(cli, "stop")["backup"]
    latest = run(cli, "backup-status")["latest_success"]
    first = latest["writer_generation"]
    assert uuid.UUID(first, version=4).hex == first
    assert latest["via"] == "stop"
    assert latest["instance"] == "default"
    assert latest["sha256"] == stopped["sha256"]

    attempt_start()
    run(cli, "backup")
    latest = run(cli, "backup-status")["latest_success"]
    assert latest["via"] == "backup"
    assert latest["writer_generation"] != first
    assert (
        uuid.UUID(latest["writer_generation"], version=4).hex
        == latest["writer_generation"]
    )

    run(cli, "restore", stopped["archive"], "--instance", "restored")
    run(cli, "select-instance", "restored")
    run(cli, "backup")
    latest = run(cli, "backup-status")["latest_success"]
    assert latest["instance"] == "restored"
    assert latest["writer_generation"] is None


@pytest.mark.parametrize(
    ("damage", "diagnostic"),
    [
        ("invalid-generation", "Invalid writer generation record"),
        ("generation-link", "links are refused"),
        ("instance-link", "links are refused"),
        ("namespace-link", "links are refused"),
    ],
)
def test_backup_refuses_invalid_or_symlinked_generation_state(
    installed, damage, diagnostic
):
    cli, state, backups = installed
    archive = legacy_archive(backups.parent / "legacy.zip")
    run(cli, "import-legacy", str(archive), "--instance", "source")
    run(cli, "select-instance", "source")
    namespace = state / "instances"
    instance = namespace / "source"
    record = instance / "writer-generation.json"
    generation = (
        "not-a-generation" if damage == "invalid-generation" else uuid.uuid4().hex
    )
    record.write_text(json.dumps({"writer_generation": generation}))
    record.chmod(0o600)
    if damage != "invalid-generation":
        path = {
            "generation-link": record,
            "instance-link": instance,
            "namespace-link": namespace,
        }[damage]
        preserved = backups.parent / "preserved"
        path.rename(preserved)
        path.symlink_to(preserved, target_is_directory=preserved.is_dir())

    refused = cli.invoke(main, ["backup"])
    assert refused.exit_code == 1, refused.output
    assert diagnostic in refused.stderr
    assert not list(backups.glob("backup-*.zip"))


def test_default_provisioning_failure_is_not_mistaken_for_pristine_state(installed):
    cli, state, _ = installed
    instances = state / "instances"
    instances.mkdir(mode=0o500)
    try:
        first = cli.invoke(main, ["start"])
        assert first.exit_code == 1, first.output
    finally:
        instances.chmod(0o700)
    assert not (instances / "default").exists()

    retry = cli.invoke(main, ["start"])
    assert retry.exit_code == 1, retry.output
    assert "Selected instance data are absent" in retry.stderr
    assert not (instances / "default").exists()


def test_backup_adopts_existing_owned_default_without_treating_its_loss_as_pristine(
    installed,
):
    cli, state, backups = installed
    # Model an owned default from before the durable provisioning sentinel.
    source = legacy_archive(backups.parent / "legacy.zip")
    run(cli, "import-legacy", str(source), "--instance", "source")
    default = state / "instances/default"
    (state / "instances/source").rename(default)
    record = default / "instance.json"
    record.write_text(
        json.dumps({**json.loads(record.read_text()), "instance": "default"})
    )

    assert run(cli, "backup")["verified"] is True
    preserved = backups.parent / "preserved-default"
    default.rename(preserved)
    retry = cli.invoke(main, ["start"])
    assert retry.exit_code == 1, retry.output
    assert "Selected instance data are absent" in retry.stderr
    assert not default.exists()
    assert (preserved / "data/.hidden").read_bytes() == DATA[".hidden"]


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
