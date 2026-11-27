"""Exercise native installation through the public CLI with isolated downloads."""

import fcntl
import hashlib
import io
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from click.testing import CliRunner

from rss_aggregator.cli import main


def test_setup_rejects_wrong_download_before_publishing_runtime(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("RSS_STATE_DIR", str(state))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    monkeypatch.setattr(platform, "mac_ver", lambda: ("14.0.0", ("", "", ""), "arm64"))
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(b"wrong download")
    )

    result = CliRunner().invoke(main, ["setup"])

    assert result.exit_code != 0
    assert result.stdout == ""
    assert "SHA-256" in result.stderr
    assert not (state / "runtime").exists()
    assert not (state / "native-owner.json").exists()
    assert not list(state.glob(".native-install-*"))
    assert not (state / "instances").exists()


ROOT = Path(__file__).resolve().parents[1]
# The controlled child runs with the fixture checkout as cwd; don't rely on Python
# adding cwd implicitly (e.g. when PYTHONSAFEPATH is set).
CHECKOUT_IMPORT = """
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
"""
CLI_IN_CHECKOUT = (
    CHECKOUT_IMPORT
    + """
import json
import os
import platform
import urllib.request
from unittest.mock import patch

from click.testing import CliRunner
from rss_aggregator.cli import main


def download(url, **kwargs):
    manifest = json.loads(Path('config/native.json').read_text())
    for key in ('freshrss', 'runtime'):
        if url == manifest[key]['url']:
            late_entry = os.environ.get('SYNTHETIC_LATE_SETUP_ENTRY')
            if key == 'freshrss' and late_entry:
                (Path(os.environ['RSS_STATE_DIR']) / late_entry).mkdir(mode=0o700)
            return Path(key + '.download').open('rb')
    raise AssertionError('unexpected download URL')


with (
    patch.object(platform, 'system', return_value='Darwin'),
    patch.object(platform, 'machine', return_value='arm64'),
    patch.object(platform, 'mac_ver', return_value=('14.0.0', ('', '', ''), 'arm64')),
    patch.object(urllib.request, 'urlopen', download),
):
    result = CliRunner().invoke(main, sys.argv[1:], catch_exceptions=False)

sys.stdout.write(result.stdout)
sys.stderr.write(result.stderr)
raise SystemExit(result.exit_code)
"""
)


class Checkout:
    def __init__(self, tmp_path):
        self.root = tmp_path / "checkout"
        self.root.mkdir()
        self.state = tmp_path / "state"
        self.backups = tmp_path / "backups"
        self.backups.mkdir(mode=0o700)
        self.environment = {
            **os.environ,
            "HOME": str(tmp_path),
            "PATH": str(tmp_path),
            "RSS_STATE_DIR": str(self.state),
            "RSS_BACKUP_DIR": str(self.backups),
            "RSS_TAILSCALE": "0",
        }
        self.environment.pop("PYTHONPATH", None)
        self.environment.pop("SYNTHETIC_LATE_SETUP_ENTRY", None)
        # Use an unmodified CLI in a disposable checkout with its own reviewed pins.
        shutil.copytree(
            ROOT / "rss_aggregator",
            self.root / "rss_aggregator",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        self.manifest_path = self.root / "config/native.json"
        self.manifest_path.parent.mkdir()
        self.manifest = json.loads((ROOT / "config/native.json").read_text())

    @property
    def source(self):
        return self.state / "runtime/FreshRSS"

    @property
    def binary(self):
        return self.state / "runtime/frankenphp"

    def run(self, *args):
        return subprocess.run(
            (sys.executable, "-c", CLI_IN_CHECKOUT, *args),
            cwd=self.root,
            env=self.environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def _save_download(self, name, data):
        self.manifest[name]["sha256"] = hashlib.sha256(data).hexdigest()
        (self.root / (name + ".download")).write_bytes(data)
        self.manifest_path.write_text(json.dumps(self.manifest))

    def prepare_source(self, bad_path=None, source_version="1.30.4"):
        revision = self.manifest["freshrss"]["revision"]
        top = "FreshRSS-" + revision
        payload = io.BytesIO()
        with tarfile.open(
            mode="w:gz", fileobj=payload, pax_headers={"comment": revision}
        ) as archive:
            for part in ("", "/app", "/cli", "/p", "/data"):
                info = tarfile.TarInfo(top + part)
                info.type = tarfile.DIRTYPE
                info.mode = 0o777
                archive.addfile(info)
            for path, content in {
                "constants.php": f"<?php\nconst FRESHRSS_VERSION = '{source_version}';\n".encode(),
                "p/index.php": b"<?php /* reviewed index */\n",
                "app/actualize_script.php": b"<?php\n",
                "cli/do-install.php": b"<?php\n",
                "cli/create-user.php": b"<?php\n",
                "data/.gitignore": b"/config.php\n",
            }.items():
                info = tarfile.TarInfo(top + "/" + path)
                info.size = len(content)
                info.mode = 0o777
                archive.addfile(info, io.BytesIO(content))
            if bad_path is not None:
                relative, member_type, linkname = bad_path
                info = tarfile.TarInfo(top + "/" + relative)
                info.type = member_type
                info.linkname = linkname
                archive.addfile(info, io.BytesIO(b""))
        self._save_download("freshrss", payload.getvalue())

    def prepare_runtime(self, version="1.14.0", *, old_probe=False):
        pin = self.manifest["runtime"]
        pin["version"] = version
        pin["url"] = (
            "https://github.com/php/frankenphp/releases/download/"
            f"v{version}/frankenphp-mac-arm64"
        )
        probe_version = "1.13.6" if old_probe else version
        binary = f"""#!{sys.executable}
import sys
if sys.argv[1:] == ['version']:
    print('FrankenPHP v{probe_version} PHP 8.6.0 Caddy v2.11.9')
    raise SystemExit(0)
if sys.argv[1:4] == ['php-cli', '-n', '-r']:
    print('8.6.0')
    raise SystemExit(0)
raise SystemExit('unexpected command')
""".encode()
        self._save_download("runtime", binary)


@pytest.fixture
def checkout(tmp_path):
    checkout = Checkout(tmp_path)
    checkout.prepare_source()
    checkout.prepare_runtime()
    return checkout


def test_setup_publishes_private_pinned_runtime_idempotently(checkout):
    checkout.environment["PYTHONSAFEPATH"] = "1"
    result = checkout.run("setup")
    assert result.returncode == 0, result.stderr
    owner = json.loads(result.stdout)
    assert (
        checkout.source / "p/index.php"
    ).read_bytes() == b"<?php /* reviewed index */\n"
    assert (
        checkout.binary.read_bytes()
        == (checkout.root / "runtime.download").read_bytes()
    )
    assert stat.S_IMODE(checkout.source.stat().st_mode) == 0o700
    assert stat.S_IMODE((checkout.source / "p/index.php").stat().st_mode) == 0o600
    assert stat.S_IMODE(checkout.binary.stat().st_mode) == 0o700
    assert not (checkout.state / "instances").exists()
    (checkout.root / "runtime.download").unlink()
    (checkout.root / "freshrss.download").unlink()

    again = checkout.run("setup")

    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["token"] == owner["token"]


@pytest.mark.parametrize("command", ["status", "start"])
def test_inspection_or_refused_start_does_not_spoil_fresh_setup(checkout, command):
    before = checkout.run(command)
    if command == "status":
        assert before.returncode == 0, before.stderr
        assert json.loads(before.stdout) == {
            "runtime": "absent",
            "application": "stopped",
            "http_ready": False,
        }
    else:
        assert before.returncode == 1 and "run rss setup" in before.stderr
    assert all(p.name == "command.lock" for p in checkout.state.iterdir())

    installed = checkout.run("setup")
    assert installed.returncode == 0, installed.stderr
    assert checkout.binary.is_file()


def test_parent_setup_refusal_preserves_child_shutdown(checkout):
    parent = checkout.state
    parent.mkdir(mode=0o700)
    checkout.state = parent / "production"
    checkout.environment["RSS_STATE_DIR"] = str(checkout.state)
    installed = checkout.run("setup")
    assert installed.returncode == 0, installed.stderr

    # Control only the writer process: the CLI must observe its real filesystem
    # leases and request its shutdown for this generation.
    for name, record in {
        "service.json": {
            "nonce": "synthetic",
            "port": 18080,
            "url": "http://127.0.0.1:18080",
        },
        "service-status.json": {"nonce": "synthetic", "web": "started"},
    }.items():
        path = checkout.state / name
        path.write_text(json.dumps(record))
        path.chmod(0o600)
    with (
        ThreadPoolExecutor() as executor,
        (checkout.state / "writers.lock").open("a") as writer,
        (checkout.state / "controller.lock").open("a") as controller,
    ):
        os.fchmod(writer.fileno(), 0o600)
        os.fchmod(controller.fileno(), 0o600)
        fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(controller, fcntl.LOCK_EX | fcntl.LOCK_NB)
        status = checkout.run("status")
        assert status.returncode == 0, status.stderr
        assert json.loads(status.stdout)["application"] == "running"

        checkout.environment["RSS_STATE_DIR"] = str(parent)
        rejected = checkout.run("setup")
        assert rejected.returncode == 1, (rejected.stdout, rejected.stderr)
        assert "Unowned RSS_STATE_DIR is not empty" in rejected.stderr
        assert rejected.stdout == ""
        assert set(parent.iterdir()) == {checkout.state}

        checkout.environment["RSS_STATE_DIR"] = str(checkout.state)
        shutdown = executor.submit(checkout.run, "stop")
        request = checkout.state / "stop-service.json"
        deadline = time.monotonic() + 10
        while (
            not request.exists() and not shutdown.done() and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert request.exists(), "Stop must request shutdown of the active writer."
        assert json.loads(request.read_text()) == {"nonce": "synthetic"}
    stopped = shutdown.result()
    assert stopped.returncode == 0, stopped.stderr
    result = json.loads(stopped.stdout)
    assert result["application"] == "stopped"
    assert result["already_stopped"] is False


def test_setup_refuses_new_state_entries_before_claiming_the_root(checkout):
    entry = checkout.state / ".native-install-unrecognized"
    checkout.environment["SYNTHETIC_LATE_SETUP_ENTRY"] = entry.name

    result = checkout.run("setup")

    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "Unowned RSS_STATE_DIR is not empty" in result.stderr
    assert result.stdout == ""
    assert entry.is_dir()
    assert not (checkout.state / "native-owner.json").exists()
    assert not (checkout.state / "runtime").exists()
    assert list(checkout.state.glob(".native-install-*")) == [entry]


@pytest.mark.parametrize(
    "entry",
    [
        ("../../outside", tarfile.REGTYPE, ""),
        ("p/INDEX.PHP", tarfile.REGTYPE, ""),
        ("outside", tarfile.SYMTYPE, "../../outside"),
        ("outside", tarfile.LNKTYPE, "p/index.php"),
    ],
    ids=["traversal", "case-collision", "symlink", "hardlink"],
)
def test_setup_rejects_unsafe_pinned_source_archive(checkout, entry):
    checkout.prepare_source(bad_path=entry)

    result = checkout.run("setup")

    assert result.returncode == 1, result.stderr
    assert "FreshRSS archive" in result.stderr
    assert result.stdout == ""
    assert not checkout.source.exists()
    assert not checkout.binary.exists()
    assert not list(checkout.state.glob(".native-install-*"))


@pytest.mark.parametrize("wrong_asset", ["source", "executable"])
def test_setup_refuses_pinned_bytes_reporting_another_version(checkout, wrong_asset):
    if wrong_asset == "source":
        checkout.prepare_source(source_version="1.30.3")
    else:
        checkout.prepare_runtime(old_probe=True)

    result = checkout.run("setup")

    assert result.returncode == 1, result.stderr
    assert "version" in result.stderr
    assert result.stdout == ""
    assert not checkout.source.exists()
    assert not checkout.binary.exists()

    checkout.prepare_source()
    checkout.prepare_runtime()
    retried = checkout.run("setup")
    assert retried.returncode == 0, retried.stderr


@pytest.mark.parametrize("drift", ["source", "executable", "symlink", "extra-file"])
def test_setup_and_uninstall_refuse_changed_installed_code(checkout, drift):
    installed = checkout.run("setup")
    assert installed.returncode == 0, installed.stderr
    web_entry = checkout.source / "p/index.php"
    if drift == "source":
        web_entry.write_bytes(b"different source bytes")
    elif drift == "executable":
        checkout.binary.write_bytes(b"different executable bytes")
    elif drift == "symlink":
        web_entry.unlink()
        web_entry.symlink_to(checkout.root / "freshrss.download")
    else:
        (checkout.source / "p/unreviewed.php").touch(mode=0o600)

    for command in ("setup", "uninstall"):
        result = checkout.run(command)
        assert result.returncode == 1, result.stderr
        assert "changed" in result.stderr or "unexpected native path" in result.stderr
        assert result.stdout == ""
    assert checkout.source.is_dir()
    assert checkout.binary.is_file()


def test_pin_drift_cannot_reinstall_but_confirmed_uninstall_preserves_state(checkout):
    installed = checkout.run("setup")
    assert installed.returncode == 0, installed.stderr
    state_file = checkout.state / "instances/default/data/users/alice/db.sqlite"
    state_file.parent.mkdir(parents=True)
    state_file.write_bytes(b"synthetic instance data")
    archive = checkout.backups / "previous.zip"
    archive.write_bytes(b"synthetic backup sentinel")
    checkout.prepare_runtime(version="1.15.0")

    accidental_upgrade = checkout.run("setup")
    assert accidental_upgrade.returncode == 1, accidental_upgrade.stderr
    assert "pin changed" in accidental_upgrade.stderr
    assert "v1.14.0" in checkout.binary.read_text()

    preview = checkout.run("uninstall")
    assert preview.returncode == 0, preview.stderr
    assert checkout.binary.exists()
    confirmation = json.loads(preview.stdout)["confirmation"]
    wrong = checkout.run("uninstall", "--confirm", "0" * 64)
    assert wrong.returncode == 1, wrong.stderr
    assert checkout.binary.exists()
    removed = checkout.run("uninstall", "--confirm", confirmation)
    assert removed.returncode == 0, removed.stderr
    assert not (checkout.state / "runtime").exists()
    assert state_file.read_bytes() == b"synthetic instance data"
    assert archive.read_bytes() == b"synthetic backup sentinel"

    checkout.prepare_runtime()
    reinstalled = checkout.run("setup")
    assert reinstalled.returncode == 0, reinstalled.stderr
    assert (
        json.loads(reinstalled.stdout)["token"] == json.loads(installed.stdout)["token"]
    )


def test_setup_never_adopts_preexisting_unowned_runtime(checkout):
    checkout.state.mkdir(mode=0o700)
    checkout.binary.parent.mkdir(mode=0o700)
    checkout.binary.write_bytes(b"unrelated file")

    result = checkout.run("setup")

    assert result.returncode == 1, result.stderr
    assert "unowned native runtime" in result.stderr
    assert checkout.binary.read_bytes() == b"unrelated file"
    assert not (checkout.state / "native-owner.json").exists()


def test_upgrade_cannot_replace_old_runtime_if_replacement_probe_fails(checkout):
    installed = checkout.run("setup")
    assert installed.returncode == 0, installed.stderr
    old_binary = checkout.binary.read_bytes()
    checkout.prepare_runtime(version="1.15.0", old_probe=True)
    preview = checkout.run("upgrade")
    assert preview.returncode == 0, preview.stderr
    confirmation = json.loads(preview.stdout)["confirmation"]

    result = checkout.run("upgrade", "--confirm", confirmation)

    assert result.returncode == 1, result.stderr
    assert "version" in result.stderr
    assert checkout.binary.read_bytes() == old_binary
    assert not list(checkout.state.glob(".native-upgrade-*"))


def test_confirmed_upgrade_replaces_code_and_preserves_instance_token(checkout):
    installed = checkout.run("setup")
    assert installed.returncode == 0, installed.stderr
    old_binary = checkout.binary.read_bytes()
    checkout.prepare_runtime(version="1.15.0")
    preview = checkout.run("upgrade")
    assert preview.returncode == 0, preview.stderr
    assert checkout.binary.read_bytes() == old_binary
    confirmation = json.loads(preview.stdout)["confirmation"]

    result = checkout.run("upgrade", "--confirm", confirmation)

    assert result.returncode == 0, result.stderr
    assert (
        checkout.binary.read_bytes()
        == (checkout.root / "runtime.download").read_bytes()
    )
    assert checkout.binary.read_bytes() != old_binary
    assert not list(checkout.state.glob(".native-upgrade-*"))
    again = checkout.run("setup")
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["token"] == json.loads(installed.stdout)["token"]
