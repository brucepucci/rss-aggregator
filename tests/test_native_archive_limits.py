"""Native restore resource limits through the public CLI."""

import hashlib
import io
import json
import tarfile
import zipfile

from test_native_archives import installed as installed
from test_native_archives import project as project
from test_native_archives import run

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
