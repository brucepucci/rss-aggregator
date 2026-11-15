"""Archive resource limits through the installed restore command."""

import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

import pytest

from tests.test_backups import ROOT, VOLUME, BackupProject

RSS = Path(sys.executable).with_name("rss")
MEASURE_CLI = """
import json
import runpy
import sys
import tracemalloc
from pathlib import Path

entry, metrics, *arguments = sys.argv[1:]
sys.argv = [entry, *arguments]
tracemalloc.start()
try:
    runpy.run_path(entry, run_name="__main__")
finally:
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    Path(metrics).write_text(json.dumps({"peak_bytes": peak}))
"""


def instance_backup(project, names):
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        for name in names:
            member = tarfile.TarInfo(name)
            if name.endswith("/"):
                member.type = tarfile.DIRTYPE
            archive.addfile(member)
    data = payload.getvalue()
    manifest = {
        "format": "rss-aggregator-backup",
        "format_version": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "image": project.engine()["image"],
        "includes": "complete FreshRSS data directory; no extensions in initial deployment",
        "payload": {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()},
    }
    target = project.state_dir.parent / "untrusted.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("data.tar", data)
    return target


def restore(project, archive, metrics=None):
    command = [str(RSS), "restore", str(archive), "--volume", "fresh"]
    if metrics is not None:
        command = [
            sys.executable,
            "-c",
            MEASURE_CLI,
            str(RSS),
            str(metrics),
            *command[1:],
        ]
    return subprocess.run(
        command,
        env=project.env,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def assert_refused_without_engine_mutation(project, result):
    assert result.returncode == 1, result.stderr
    assert result.stdout == ""
    assert "Invalid backup archive" in result.stderr
    assert project.engine()["calls"] == []
    assert set(project.engine()["volumes"]) == {VOLUME}
    assert not (project.state_dir / "selected-volume.json").exists()


def test_restore_bounds_deep_path_allocation_before_engine_mutation(tmp_path):
    project = BackupProject(tmp_path)
    archive = instance_backup(
        project,
        (f"root-{index}/" + "a/" * 1800 + "file" for index in range(20)),
    )
    assert archive.stat().st_size < 128 * 1024
    with tempfile.NamedTemporaryFile(dir=tmp_path, suffix=".json") as metrics:
        result = restore(project, archive, Path(metrics.name))
        peak = json.loads(Path(metrics.name).read_text())["peak_bytes"]
    print(f"Deep-path restore peak Python allocation: {peak} bytes")
    # Includes CLI imports/startup; the original ~113 KiB ZIP allocated ~69 MiB.
    assert peak < 16 * 1024 * 1024
    assert_refused_without_engine_mutation(project, result)


@pytest.mark.parametrize("entries", ["long-shallow", "ancestors", "empty"])
def test_restore_bounds_cumulative_path_metadata_before_engine_mutation(
    tmp_path, entries
):
    project = BackupProject(tmp_path)
    if entries == "long-shallow":
        names = (f"file-{index:04d}-" + "a" * 1000 for index in range(2200))
    elif entries == "ancestors":
        names = (f"root-{index:04d}/" + "a/" * 62 + "file" for index in range(300))
    else:
        names = (f"f{index}" for index in range(33000))
    archive = instance_backup(project, names)
    result = restore(project, archive)
    assert_refused_without_engine_mutation(project, result)


@pytest.mark.parametrize("depth", [64, 65])
def test_restore_applies_depth_limit_after_normalization(tmp_path, depth):
    project = BackupProject(tmp_path)
    nested = "./" + "/./".join(["users", *(["a"] * (depth - 2)), "db.sqlite"])
    archive = instance_backup(project, ["./", "users/", nested])
    result = restore(project, archive)
    if depth == 65:
        assert_refused_without_engine_mutation(project, result)
    else:
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["selected"] is False
        assert project.engine()["volumes"]["fresh"]["restored"] is True
        assert not (project.state_dir / "selected-volume.json").exists()
