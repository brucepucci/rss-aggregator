"""Native capture and restore resource limits through the public CLI."""

import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path

from test_native_archives import installed as installed
from test_native_archives import project as project
from test_native_archives import run
from test_private_access import controlled_web as controlled_web
from test_private_access import installed_checkout as installed_checkout
from test_private_access import native as native
from test_private_access import result_json

from rss_aggregator.cli import main
from rss_aggregator.native_install import manifest_identity


def write_archive(path, identity, count):
    payload = io.BytesIO()
    # Two filesystem-safe path components and empty files.
    parent = "a" * 240
    with tarfile.open(fileobj=payload, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for index in range(count):
            leaf = f"member-{index:04d}-" + "b" * 228
            tar.addfile(tarfile.TarInfo(f"{parent}/{leaf}"))
    data = payload.getvalue()
    manifest = {
        "format": "rss-aggregator-backup",
        "format_version": 2,
        "created_at": "2026-01-01T00:00:00Z",
        "includes": "complete FreshRSS data directory; no extensions in initial deployment",
        "runtime": identity,
        "payload": {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()},
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("data.tar", data)


def test_native_restore_refuses_cumulative_path_metadata_without_publishing(
    installed,  # noqa: F811 - pytest injects the shared fixture.
    tmp_path,
):
    cli, state, _ = installed
    pin = json.loads((tmp_path / "checkout/config/native.json").read_text())
    identity = manifest_identity(pin)

    small = tmp_path / "within-budget.zip"
    write_archive(small, identity, count=1)
    assert run(cli, "restore", str(small), "--instance", "source")["selected"] is False
    run(cli, "select-instance", "source")
    assert run(cli, "status")["instance"] == "source"

    # The same shallow shape is valid above; only the aggregate exceeds 8 MiB.
    large = tmp_path / "over-budget.zip"
    write_archive(large, identity, count=2600)
    result = cli.invoke(main, ["restore", str(large), "--instance", "fresh"])
    assert result.exit_code == 1, result.output
    assert not result.stdout
    assert "Invalid backup archive" in result.stderr
    assert {path.name for path in (state / "instances").iterdir()} == {"source"}
    assert run(cli, "status")["instance"] == "source"


def test_native_capture_refuses_the_over_budget_member_before_staging_it(
    controlled_web,  # noqa: F811 - pytest injects the shared fixture.
    monkeypatch,
):
    web = controlled_web
    monkeypatch.setenv("RSS_TAILSCALE", "0")
    result_json(web.run(["start"]))
    assert result_json(web.run(["status"]))["http_ready"] is True
    saved = Path(result_json(web.run(["backup"]))["archive"])
    original = saved.read_bytes()

    # Neither subtree exceeds the limit alone. Each 240-byte leaf under a
    # 240-byte parent costs 3,396 bytes including the shared ancestor. After
    # the two directories (and small existing files), 2,469 leaves fit in 8 MiB.
    parents = {letter * 240 for letter in ("a", "b")}
    for name in parents:
        parent = web.data / name
        parent.mkdir(mode=0o700)
        for index in range(1250):
            (parent / (f"{index:04d}" + "c" * 236)).touch(mode=0o600)

    staged_leaves = 0
    capture_tar = None
    addfile = tarfile.TarFile.addfile

    def count_staged_leaf(archive, member, fileobj=None):
        nonlocal staged_leaves, capture_tar
        if capture_tar is None:
            capture_tar = archive
        if (
            archive is capture_tar
            and member.name.partition("/")[0] in parents
            and member.isfile()
        ):
            staged_leaves += 1
        return addfile(archive, member, fileobj)

    with monkeypatch.context() as export:
        export.setattr(tarfile.TarFile, "addfile", count_staged_leaf)
        failed = web.run(["backup"])

    assert failed.exit_code == 1, failed.output
    assert not failed.stdout
    assert staged_leaves == 2469
    assert "exceeds the backup path metadata limit" in failed.stderr
    assert list(saved.parent.iterdir()) == [saved]
    assert saved.read_bytes() == original
    status = result_json(web.run(["status"]))
    assert status["application"] == "running"
    assert status["http_ready"] is True
    report = result_json(web.run(["backup-status"]))
    assert report["latest_attempt"]["outcome"] == "failed"
    assert report["latest_success"]["archive"] == str(saved)
    assert report["maintenance_required"] is False
