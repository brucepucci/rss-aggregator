"""Acceptance checks use only disposable Git repositories and local bare remotes."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from rss_aggregator.development import check, fix, format_code, setup

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def tooling_environment(tmp_path_factory):
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_AUTHOR_NAME="Development test",
        GIT_AUTHOR_EMAIL="development@example.invalid",
        GIT_COMMITTER_NAME="Development test",
        GIT_COMMITTER_EMAIL="development@example.invalid",
        UV_OFFLINE="1",
        UV_PYTHON_DOWNLOADS="never",
        PYTHONDONTWRITEBYTECODE="1",
        PRE_COMMIT_HOME=str(tmp_path_factory.mktemp("pre-commit-cache")),
    )
    for variable in ("UV_PROJECT", "UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV"):
        environment.pop(variable, None)
    return environment


@pytest.fixture
def project(tmp_path, tooling_environment):
    project = tmp_path / "project"
    project.mkdir()
    for name in (
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        ".pre-commit-config.yaml",
    ):
        shutil.copyfile(ROOT / name, project / name)
    package = project / "src/rss_aggregator"
    package.mkdir(parents=True)
    shutil.copyfile(
        ROOT / "src/rss_aggregator/development.py", package / "development.py"
    )
    (package / "__init__.py").write_text('"""Disposable acceptance package."""\n')
    tests = project / "tests"
    tests.mkdir()
    (tests / "test_sample.py").write_text("def test_sample():\n    assert True\n")
    (project / ".gitignore").write_text(
        ".venv/\n__pycache__/\n.pytest_cache/\n.ruff_cache/\n"
    )

    def run(*arguments):
        return subprocess.run(
            arguments,
            cwd=project,
            env=tooling_environment,
            text=True,
            capture_output=True,
            check=False,
        )

    for arguments in (
        ("git", "init", "--initial-branch=main"),
        ("uv", "sync", "--locked", "--offline"),
        ("git", "add", "."),
        ("git", "commit", "-m", "Disposable baseline"),
    ):
        result = run(*arguments)
        assert result.returncode == 0, result.stdout + result.stderr
    return project, run


def invoke(run, command):
    result = run("uv", "run", "--locked", "--offline", "rss-dev", command)
    return result, json.loads(result.stdout)


def add_remote(project, run):
    remote = project.parent / "remote.git"
    result = run("git", "init", "--bare", str(remote))
    assert result.returncode == 0, result.stderr
    result = run("git", "remote", "add", "disposable", str(remote))
    assert result.returncode == 0, result.stderr
    return remote


def tracked_snapshot(project, run):
    result = run("git", "ls-files", "-z")
    assert result.returncode == 0, result.stderr
    files = {
        name: (project / name).read_bytes()
        for name in result.stdout.split("\0")
        if name
    }
    status = run("git", "status", "--porcelain=v1", "--untracked-files=all")
    assert status.returncode == 0, status.stderr
    return files, status.stdout


def test_no_remote_then_setup_and_successful_actual_push(project):
    directory, run = project
    result, payload = invoke(run, "setup")
    assert result.returncode == 0
    assert payload["ok"] and not payload["active"]
    assert "rerun" in payload["next_step"]
    hook = directory / ".git/hooks/pre-push"
    assert not hook.exists()

    add_remote(directory, run)
    result, payload = invoke(run, "setup")
    assert result.returncode == 0, result.stderr
    assert payload == {"ok": True, "active": True}
    assert hook.is_file()
    original = hook.read_bytes()
    result, payload = invoke(run, "setup")
    assert result.returncode == 0, result.stderr
    assert payload["active"]
    assert hook.read_bytes() == original
    assert not hook.with_suffix(".legacy").exists()

    before = tracked_snapshot(directory, run)
    result = run("git", "push", "disposable", "HEAD:refs/heads/main")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Complete development checks" in result.stdout + result.stderr
    assert tracked_snapshot(directory, run) == before
    result = run("git", "ls-remote", "disposable", "refs/heads/main")
    assert result.returncode == 0 and result.stdout.strip()


@pytest.mark.parametrize(
    ("failure", "contents", "failed_check"),
    [
        ("pytest", "def test_sample():\n    assert False\n", "pytest"),
        (
            "lint",
            "import os\n\n\ndef test_sample():\n    assert True\n",
            "lint",
        ),
        ("format", "def test_sample( ):\n    assert True\n", "format"),
    ],
)
def test_check_and_actual_push_reject_failures_without_fixes(
    project, failure, contents, failed_check
):
    directory, run = project
    add_remote(directory, run)
    result, _ = invoke(run, "setup")
    assert result.returncode == 0, result.stderr
    (directory / "tests/test_sample.py").write_text(contents)
    result = run("git", "add", ".")
    assert result.returncode == 0
    result = run("git", "commit", "-m", f"Introduce {failure} failure")
    assert result.returncode == 0, result.stderr
    before = tracked_snapshot(directory, run)
    result, payload = invoke(run, "check")
    assert result.returncode != 0
    assert not payload["ok"]
    statuses = {item["name"]: item["returncode"] for item in payload["checks"]}
    assert set(statuses) == {"pytest", "lint", "format"}
    assert statuses[failed_check] != 0
    assert tracked_snapshot(directory, run) == before

    result = run("git", "push", "disposable", "HEAD:refs/heads/main")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "Complete development checks" in result.stdout + result.stderr
    assert tracked_snapshot(directory, run) == before
    result = run("git", "ls-remote", "disposable", "refs/heads/main")
    assert result.returncode == 0 and not result.stdout.strip()


@pytest.mark.parametrize(
    "arrangement",
    ["foreign", "legacy", "symlink", "dangling", "hooks-symlink", "custom"],
)
def test_setup_preserves_conflicting_hook_arrangements(project, arrangement):
    directory, run = project
    add_remote(directory, run)
    hooks = directory / ".git/hooks"
    hook = hooks / "pre-push"
    foreign = b"#!/bin/sh\n# Keep this developer-owned hook.\nexit 0\n"
    target = directory.parent / "external-hook"
    target.write_bytes(foreign)
    if arrangement == "foreign":
        hook.write_bytes(foreign)
    elif arrangement == "legacy":
        (hooks / "pre-push.legacy").write_bytes(foreign)
    elif arrangement in ("symlink", "dangling"):
        hook.symlink_to(
            target if arrangement == "symlink" else target.with_suffix(".missing")
        )
    elif arrangement == "hooks-symlink":
        hooks.rename(directory.parent / "external-hooks")
        hooks.symlink_to(directory.parent / "external-hooks", target_is_directory=True)
    else:
        custom = directory.parent / "custom-hooks"
        custom.mkdir()
        (custom / "pre-push").write_bytes(foreign)
        result = run("git", "config", "core.hooksPath", str(custom))
        assert result.returncode == 0

    before = {
        str(path): (path.is_symlink(), path.read_bytes())
        for path in directory.parent.rglob("*")
        if path.is_file() and ".venv" not in path.parts
    }
    result, payload = invoke(run, "setup")
    assert result.returncode != 0
    assert not payload["ok"] and not payload["active"]
    assert "manually" in payload["next_step"]
    assert target.read_bytes() == foreign
    for name, (symlink, content) in before.items():
        path = Path(name)
        assert path.is_symlink() == symlink
        assert path.read_bytes() == content
    if arrangement in ("symlink", "dangling"):
        assert hook.is_symlink()
    if arrangement == "hooks-symlink":
        assert hooks.is_symlink()
    if arrangement in ("custom", "legacy", "hooks-symlink"):
        assert not hook.exists()


def test_setup_preserves_extensions_to_an_existing_pre_commit_hook(project):
    directory, run = project
    add_remote(directory, run)
    result, payload = invoke(run, "setup")
    assert result.returncode == 0 and payload["active"]
    hook = directory / ".git/hooks/pre-push"
    extended = hook.read_bytes() + b"\n# Preserve this developer-owned extension.\n"
    hook.write_bytes(extended)
    result, payload = invoke(run, "setup")
    assert result.returncode == 0 and payload["active"]
    assert hook.read_bytes() == extended


def test_explicit_fix_and_format_are_separate(project):
    directory, run = project
    sample = directory / "tests/test_sample.py"
    sample.write_text("import os\n\n\ndef test_sample( ):\n    assert True\n")
    result, payload = invoke(run, "fix")
    assert result.returncode == 0, result.stderr
    assert payload["ok"]
    assert "import os" not in sample.read_text()
    assert "test_sample( )" in sample.read_text()
    result, payload = invoke(run, "format")
    assert result.returncode == 0, result.stderr
    assert payload["ok"]
    assert "test_sample()" in sample.read_text()
    result, payload = invoke(run, "check")
    assert result.returncode == 0, result.stderr
    assert payload["ok"]


def test_fix_does_not_apply_unsafe_changes_even_if_project_config_enables_them(project):
    directory, run = project
    configuration = directory / "pyproject.toml"
    configuration.write_text(
        configuration.read_text().replace(
            '[tool.ruff]\ntarget-version = "py310"',
            '[tool.ruff]\ntarget-version = "py310"\nunsafe-fixes = true',
        )
    )
    sample = directory / "tests/test_sample.py"
    original = 'def test_sample():\n    value = "%s" % object()\n    assert value\n'
    sample.write_text(original)
    result, payload = invoke(run, "fix")
    assert result.returncode != 0
    assert not payload["ok"]
    assert "UP031" in result.stderr
    assert sample.read_text() == original


def test_public_operations_work_without_click(project):
    directory, _ = project
    assert check(directory)["ok"]
    inactive = setup(directory)
    assert inactive["ok"] and not inactive["active"]
    sample = directory / "tests/test_sample.py"
    sample.write_text("import os\n\n\ndef test_sample( ):\n    assert True\n")
    assert fix(directory)["ok"]
    assert "import os" not in sample.read_text()
    assert "test_sample( )" in sample.read_text()
    assert format_code(directory)["ok"]
    assert "test_sample()" in sample.read_text()


def test_live_tests_are_explicitly_selected_not_part_of_default_gate(project):
    directory, run = project
    (directory / "tests/test_live_sample.py").write_text(
        "import pytest\n\n\n@pytest.mark.live\n"
        "def test_live_sample():\n    assert False\n"
    )
    result, payload = invoke(run, "check")
    assert result.returncode == 0, result.stderr
    assert payload["ok"]
    assert "1 deselected" in result.stderr
    result = run(
        "uv", "run", "--locked", "--offline", "python", "-m", "pytest", "-m", "live"
    )
    assert result.returncode != 0
    assert "1 failed, 1 deselected" in result.stdout


def test_project_option_works_outside_selected_repository(project, tooling_environment):
    directory, _ = project
    result = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(ROOT),
            "--locked",
            "--offline",
            "rss-dev",
            "check",
            "--project",
            str(directory),
        ],
        cwd=directory.parent,
        env=tooling_environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ok"]
