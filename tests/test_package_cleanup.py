"""Package removal through public Runtime/CLI and disposable native executables."""

import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from rss_aggregator import Runtime, RuntimeFailure

ROOT = Path(__file__).resolve().parents[1]


class PackageProject:
    def __init__(self, tmp_path):
        self.state_dir = tmp_path / "state"
        self.state_dir.mkdir()
        self.native_state = tmp_path / "native.json"
        self.native_state.write_text(
            json.dumps(
                {
                    "packages": {"podman": "6.2.0", "libkrun": "1.0", "shared": "2.0"},
                    "dependents": {},
                    "machines": [],
                    "connections": [],
                    "calls": [],
                }
            )
        )
        self.write_ledger("host-before.json", {"packages": {"shared": "2.0"}})
        self.write_ledger("host-additions.json", {"podman": "6.2.0", "libkrun": "1.0"})
        self.bin_dir = tmp_path / "bin"
        self.bin_dir.mkdir()
        source = (ROOT / "tests/fake_cleanup_brew.py").read_text()
        for name in ("brew", "podman"):
            executable = self.bin_dir / name
            executable.write_text("#!" + sys.executable + "\n" + source)
            executable.chmod(0o755)
        self.env = dict(
            os.environ,
            PATH=str(self.bin_dir),
            RSS_STATE_DIR=str(self.state_dir),
            FAKE_PACKAGE_STATE=str(self.native_state),
            HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK="1",
        )

    def write_ledger(self, name, value):
        (self.state_dir / name).write_text(json.dumps(value))

    def state(self):
        return json.loads(self.native_state.read_text())

    def update(self, **values):
        state = self.state()
        state.update(values)
        self.native_state.write_text(json.dumps(state))

    def run(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "rss_aggregator", *args],
            env=self.env,
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def preview(self, package="podman"):
        result = self.run("remove-package", package)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)


@pytest.fixture
def project(tmp_path):
    return PackageProject(tmp_path)


def test_preview_is_stable_and_does_not_remove_any_package_or_ledger(project):
    before = project.state()["packages"]
    ledgers = {
        path.name: path.read_bytes() for path in project.state_dir.glob("*.json")
    }
    report = project.preview()
    assert report["preview"] is True
    assert report["targets"] == {"package": "podman", "version": "6.2.0"}
    assert len(report["confirmation"]) == 64
    assert report["confirmation"] == project.preview()["confirmation"]
    assert report["dependents"] == []
    assert report["retain"] == []
    assert report["ownership"]["baseline"] is False
    assert report["ownership"]["addition"] is True
    assert project.state()["packages"] == before
    assert {
        path.name: path.read_bytes() for path in project.state_dir.glob("*.json")
    } == ledgers
    assert not any(call[1:2] == ["uninstall"] for call in project.state()["calls"])


def test_apply_requires_candidate_specific_external_use_acknowledgment(project):
    report = project.preview()
    refused = project.run(
        "remove-package", "podman", "--confirm", report["confirmation"]
    )
    assert refused.returncode != 0
    assert "unused-elsewhere" in refused.stderr
    assert "podman" in project.state()["packages"]
    removed = project.run(
        "remove-package",
        "podman",
        "--confirm",
        report["confirmation"],
        "--unused-elsewhere",
    )
    assert removed.returncode == 0, removed.stderr
    result = json.loads(removed.stdout)
    assert result["status"] == "removed"
    assert result["preview"] is False
    assert project.state()["packages"] == {"libkrun": "1.0", "shared": "2.0"}
    assert project.preview()["status"] == "absent"
    assert json.loads((project.state_dir / "host-additions.json").read_text()) == {
        "podman": "6.2.0",
        "libkrun": "1.0",
    }
    assert [
        call for call in project.state()["calls"] if call[1:2] == ["uninstall"]
    ] == [
        ["brew", "uninstall", "--formula", "podman"],
    ]


@pytest.mark.parametrize("package", ["podman", "libkrun"])
def test_missing_podman_cli_cannot_authorize_removal_while_podman_is_installed(
    project, package
):
    (project.bin_dir / "podman").unlink()
    report = project.preview(package)
    assert report["retain"]
    refused = project.run(
        "remove-package",
        package,
        "--confirm",
        report["confirmation"],
        "--unused-elsewhere",
    )
    assert refused.returncode != 0
    assert package in project.state()["packages"]


def test_dependency_can_be_removed_after_podman_and_its_cli_are_absent(project):
    (project.bin_dir / "podman").unlink()
    project.update(packages={"libkrun": "1.0", "shared": "2.0"})
    report = project.preview("libkrun")
    assert report["retain"] == []
    removed = project.run(
        "remove-package",
        "libkrun",
        "--confirm",
        report["confirmation"],
        "--unused-elsewhere",
    )
    assert removed.returncode == 0, removed.stderr
    assert project.state()["packages"] == {"shared": "2.0"}


@pytest.mark.parametrize(
    "name",
    [
        "",
        "--force",
        "homebrew/core/podman",
        "a/b",
        "../podman",
        "Podman",
        "podman;true",
        "podman\n",
        "podman qemu",
        "a@1@2",
    ],
)
def test_callable_rejects_non_plain_formula_before_native_work(
    project, monkeypatch, name
):
    for key, value in project.env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeFailure, match="plain Homebrew formula"):
        Runtime(state_dir=project.state_dir).run(
            "remove-package",
            package=name,
            confirm=None,
            unused_elsewhere=False,
        )
    assert project.state()["calls"] == []


@pytest.mark.parametrize(
    "condition", ["baseline", "unrecorded", "changed-version", "dependents"]
)
def test_adopted_or_unowned_packages_are_retained_even_with_confirmation(
    project, condition
):
    if condition == "baseline":
        project.write_ledger("host-before.json", {"packages": {"podman": "6.2.0"}})
    elif condition == "unrecorded":
        project.write_ledger("host-additions.json", {"libkrun": "1.0"})
    elif condition == "changed-version":
        project.update(packages={"podman": "6.3.0"})
    else:
        project.update(dependents={"podman": ["a-formula", "a-cask"]})
    report = project.preview()
    assert report["retain"]
    refused = project.run(
        "remove-package",
        "podman",
        "--confirm",
        report["confirmation"],
        "--unused-elsewhere",
    )
    assert refused.returncode != 0
    assert "retained" in refused.stderr
    assert not any(call[1:2] == ["uninstall"] for call in project.state()["calls"])
    if condition == "dependents":
        assert report["dependents"] == ["a-cask", "a-formula"]


@pytest.mark.parametrize("package", ["podman", "libkrun"])
@pytest.mark.parametrize("kind", ["machines", "connections"])
def test_any_podman_resource_blocks_every_candidate_without_disclosing_inventory(
    project, package, kind
):
    project.update(
        **{
            kind: [
                {
                    "Name": "adopted",
                    "URI": "ssh://synthetic-secret",
                    "Identity": "/private/secret",
                }
            ]
        }
    )
    report = project.preview(package)
    assert report["retain"]
    serialized = json.dumps(report)
    assert "synthetic-secret" not in serialized
    assert "/private/secret" not in serialized
    assert "adopted" not in serialized
    result = project.run(
        "remove-package",
        package,
        "--confirm",
        report["confirmation"],
        "--unused-elsewhere",
    )
    assert result.returncode != 0
    assert package in project.state()["packages"]
    assert "synthetic-secret" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "change",
    [
        "version",
        "baseline",
        "additions",
        "dependents",
        "machines",
        "connections",
        "brew",
        "candidate",
    ],
)
def test_changed_evidence_invalidates_confirmation(project, change):
    report = project.preview()
    package = "podman"
    if change == "version":
        project.update(packages={"podman": "6.3.0"})
    elif change == "baseline":
        project.write_ledger("host-before.json", {"packages": {}, "context": "changed"})
    elif change == "additions":
        project.write_ledger(
            "host-additions.json", {"podman": "6.2.0", "libkrun": "2.0"}
        )
    elif change == "dependents":
        project.update(dependents={"podman": ["adopted"]})
    elif change in ("machines", "connections"):
        project.update(**{change: [{"Name": "adopted"}]})
    elif change == "brew":
        executable = project.bin_dir / "brew"
        executable.write_text(executable.read_text() + "\n# changed executable\n")
    else:
        package = "libkrun"
    result = project.run(
        "remove-package",
        package,
        "--confirm",
        report["confirmation"],
        "--unused-elsewhere",
    )
    assert result.returncode != 0
    assert "stale" in result.stderr
    assert not any(call[1:2] == ["uninstall"] for call in project.state()["calls"])


def test_full_plan_is_rechecked_immediately_before_uninstall(project):
    report = project.preview()
    project.update(
        change_after_uses={"count": 2, "values": {"packages": {"podman": "6.3.0"}}}
    )
    result = project.run(
        "remove-package",
        "podman",
        "--confirm",
        report["confirmation"],
        "--unused-elsewhere",
    )
    assert result.returncode != 0
    assert "changed" in result.stderr
    assert project.state()["packages"] == {"podman": "6.3.0"}
    assert not any(call[1:2] == ["uninstall"] for call in project.state()["calls"])


def test_late_podman_install_with_unavailable_cli_invalidates_dependency_plan(project):
    (project.bin_dir / "podman").unlink()
    project.update(packages={"libkrun": "1.0"})
    report = project.preview("libkrun")
    project.update(
        change_after_uses={
            "count": 2,
            "values": {"packages": {"libkrun": "1.0", "podman": "6.2.0"}},
        }
    )
    result = project.run(
        "remove-package",
        "libkrun",
        "--confirm",
        report["confirmation"],
        "--unused-elsewhere",
    )
    assert result.returncode != 0
    assert "libkrun" in project.state()["packages"]
    assert not any(call[1:2] == ["uninstall"] for call in project.state()["calls"])


def test_native_dependency_check_protects_adoption_after_last_read(project):
    report = project.preview()
    project.update(
        change_after_uses={
            "count": 3,
            "values": {"dependents": {"podman": ["late-adopter"]}},
        }
    )
    result = project.run(
        "remove-package",
        "podman",
        "--confirm",
        report["confirmation"],
        "--unused-elsewhere",
    )
    assert result.returncode != 0
    assert "podman" in project.state()["packages"]
    assert "synthetic-secret" not in result.stdout + result.stderr


@pytest.mark.parametrize("kind", ["machines", "connections"])
def test_confirmation_binds_full_inventory_not_just_resource_names(project, kind):
    project.update(**{kind: [{"Name": "adopted", "Identity": "/private/first-secret"}]})
    first = project.preview()
    project.update(
        **{kind: [{"Name": "adopted", "Identity": "/private/second-secret"}]}
    )
    second = project.preview()
    assert first["confirmation"] != second["confirmation"]
    assert "secret" not in json.dumps([first, second])


def test_public_callable_removes_versioned_formula_with_same_guards(
    project, monkeypatch
):
    for key, value in project.env.items():
        monkeypatch.setenv(key, value)
    project.update(packages={"python@3.13": "3.13.18"})
    project.write_ledger("host-additions.json", {"python@3.13": "3.13.18"})
    runtime = Runtime(state_dir=project.state_dir)
    report = runtime.run("remove-package", package="python@3.13")
    assert report["targets"] == {"package": "python@3.13", "version": "3.13.18"}
    result = runtime.run(
        "remove-package",
        package="python@3.13",
        confirm=report["confirmation"],
        unused_elsewhere=True,
    )
    assert result["status"] == "removed"
    assert runtime.run("remove-package", package="python@3.13")["status"] == "absent"


@pytest.mark.parametrize("guard", ["lock", "maintenance"])
def test_public_runtime_retains_parent_lock_and_maintenance_guards(
    project, monkeypatch, guard
):
    for key, value in project.env.items():
        monkeypatch.setenv(key, value)
    if guard == "maintenance":
        project.write_ledger("maintenance.json", {})
    with (project.state_dir / "command.lock").open("a") as lock:
        if guard == "lock":
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeFailure, match="running|Maintenance"):
            Runtime(state_dir=project.state_dir).run(
                "remove-package",
                package="podman",
                confirm=None,
                unused_elsewhere=False,
            )
    assert project.state()["calls"] == []


@pytest.mark.parametrize(
    "failure",
    [
        "brew",
        "uses",
        "machines",
        "connections",
        "uninstall",
        "malformed-machines",
        "malformed-connections",
        "malformed-versions",
        "malformed-dependents",
        "bad-baseline",
        "bad-additions",
    ],
)
def test_native_or_evidence_failure_is_sanitized_and_preserves_packages_and_ledgers(
    project, failure
):
    report = project.preview()
    if failure == "brew":
        project.update(fail="brew")
    elif failure == "uses":
        project.update(
            fail_args=[
                "uses",
                "--installed",
                "--recursive",
                "--include-build",
                "--include-test",
                "--include-optional",
                "podman",
            ]
        )
    elif failure in ("machines", "connections"):
        args = (
            ["machine", "list", "--format", "json"]
            if failure == "machines"
            else ["system", "connection", "list", "--format", "json"]
        )
        project.update(fail_args=args)
    elif failure == "uninstall":
        project.update(fail_args=["uninstall", "--formula", "podman"])
    elif failure in ("malformed-machines", "malformed-connections"):
        project.update(**{failure.removeprefix("malformed-"): None})
    elif failure == "malformed-versions":
        project.update(packages={"--synthetic-secret": "1.0"})
    elif failure == "malformed-dependents":
        project.update(dependents={"podman": ["https://synthetic-secret"]})
    elif failure == "bad-baseline":
        (project.state_dir / "host-before.json").write_text("synthetic-secret")
    else:
        project.write_ledger("host-additions.json", {"podman": ["synthetic-secret"]})
    ledgers = {
        path.name: path.read_bytes() for path in project.state_dir.glob("*.json")
    }
    packages = project.state()["packages"]
    result = project.run(
        "remove-package",
        "podman",
        "--confirm",
        report["confirmation"],
        "--unused-elsewhere",
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "synthetic-secret" not in result.stderr
    assert "Traceback" not in result.stderr
    assert project.state()["packages"] == packages
    assert {
        path.name: path.read_bytes() for path in project.state_dir.glob("*.json")
    } == ledgers


def test_missing_brew_is_not_treated_as_absent(project):
    (project.bin_dir / "brew").unlink()
    result = project.run("remove-package", "podman")
    assert result.returncode != 0
    assert "absence cannot be verified" in result.stderr
    assert project.state()["calls"] == []


def test_absent_candidate_needs_no_dependent_query_or_apply_acknowledgment(project):
    project.update(
        packages={"shared": "2.0"},
        fail_args=[
            "uses",
            "--installed",
            "--recursive",
            "--include-build",
            "--include-test",
            "--include-optional",
            "podman",
        ],
    )
    result = project.run("remove-package", "podman", "--confirm", "old-confirmation")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "absent"
    assert not any(call[1:2] == ["uninstall"] for call in project.state()["calls"])
