"""Acceptance checks use only disposable Git repositories and local bare remotes."""

import json
import os
import shutil
import subprocess
import sys
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
    for variable in (
        "UV_PROJECT",
        "UV_PROJECT_ENVIRONMENT",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "PYTHONHOME",
    ):
        environment.pop(variable, None)
    return environment


@pytest.fixture
def hook_repository(tmp_path, tooling_environment):
    directory = tmp_path / "project"
    directory.mkdir()

    def run(*arguments):
        return subprocess.run(
            arguments,
            cwd=directory,
            env=tooling_environment,
            text=True,
            capture_output=True,
            check=False,
        )

    for arguments in (
        ("git", "init", "--initial-branch=main"),
        ("git", "remote", "add", "disposable", str(tmp_path / "unused-remote.git")),
    ):
        result = run(*arguments)
        assert result.returncode == 0, result.stdout + result.stderr
    return directory, run


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
    package = project / "rss_aggregator"
    package.mkdir(parents=True)
    shutil.copyfile(ROOT / "rss_aggregator/development.py", package / "development.py")
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


@pytest.fixture
def foreign_repository(tmp_path, tooling_environment):
    directory = tmp_path / "foreign"
    for arguments in (
        ("git", "init", "--initial-branch=main", str(directory)),
        (
            "git",
            "-C",
            str(directory),
            "remote",
            "add",
            "origin",
            str(tmp_path / "unused.git"),
        ),
    ):
        result = subprocess.run(
            arguments,
            env=tooling_environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
    hook = directory / ".git/hooks/pre-push"
    hook.write_bytes(b"#!/bin/sh\n# Foreign hook must be preserved.\nexit 0\n")
    hook.chmod(0o755)
    return directory, hook


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
def test_setup_preserves_conflicting_hook_arrangements(hook_repository, arrangement):
    directory, run = hook_repository
    hooks = directory / ".git/hooks"
    hook = hooks / "pre-push"
    foreign = b"#!/bin/sh\n# Keep this developer-owned hook.\nexit 0\n"
    target = directory.parent / "external-hook"
    target.write_bytes(foreign)
    target.chmod(0o740)
    if arrangement == "foreign":
        hook.write_bytes(foreign)
        hook.chmod(0o751)
    elif arrangement == "legacy":
        legacy = hooks / "pre-push.legacy"
        legacy.write_bytes(foreign)
        legacy.chmod(0o640)
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
        (custom / "pre-push").chmod(0o750)
        result = run("git", "config", "core.hooksPath", str(custom))
        assert result.returncode == 0

    def snapshot():
        return {
            path.relative_to(directory.parent): (
                path.lstat().st_mode,
                os.readlink(path)
                if path.is_symlink()
                else path.read_bytes()
                if path.is_file()
                else None,
            )
            for path in directory.parent.rglob("*")
        }

    before = snapshot()
    status = run("git", "status", "--porcelain=v1", "--untracked-files=all")
    assert status.returncode == 0, status.stderr
    result = run(
        sys.executable,
        "-I",
        "-B",
        "-c",
        "from rss_aggregator.development import main; main()",
        "setup",
        "--project",
        str(directory),
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 1, result.stderr
    assert not payload["ok"] and not payload["active"]
    assert "manually" in payload["next_step"]
    assert target.read_bytes() == foreign
    assert snapshot() == before
    after_status = run("git", "status", "--porcelain=v1", "--untracked-files=all")
    assert after_status.returncode == 0, after_status.stderr
    assert after_status.stdout == status.stdout
    assert not (directory / ".venv").exists()
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


def test_setup_refuses_nonexecutable_generated_hook_without_changes(project):
    directory, run = project
    add_remote(directory, run)
    result, payload = invoke(run, "setup")
    assert result.returncode == 0 and payload["active"]
    hook = directory / ".git/hooks/pre-push"
    hook.chmod(0o644)
    original = hook.read_bytes(), hook.stat().st_mode

    result, payload = invoke(run, "setup")
    assert result.returncode == 1
    assert not payload["ok"] and not payload["active"]
    assert "executable" in payload["reason"]
    assert "manually" in payload["next_step"]
    assert (hook.read_bytes(), hook.stat().st_mode) == original


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


@pytest.mark.parametrize(
    "autofix", ["fix = true", "fix-only = true", "fix = true\nfix-only = true"]
)
def test_gate_and_push_disable_configured_ruff_fixes(project, autofix):
    directory, run = project
    configuration = directory / "pyproject.toml"
    original_configuration = configuration.read_text()
    ruff_section = '[tool.ruff]\ntarget-version = "py310"'
    assert original_configuration.count(ruff_section) == 1
    configuration.write_text(
        original_configuration.replace(ruff_section, f"{ruff_section}\n{autofix}")
    )
    sample = directory / "tests/test_sample.py"
    sample.write_text(
        "import os\nfrom pathlib import Path\n\n\n"
        'def test_sample():\n    assert Path(".").is_dir()\n'
    )
    add_remote(directory, run)
    result, payload = invoke(run, "setup")
    assert result.returncode == 0 and payload["active"], result.stderr
    assert run("git", "add", ".").returncode == 0
    assert (
        run(
            "git", "commit", "-m", "Enable Ruff autofix with a lint violation"
        ).returncode
        == 0
    )
    before = tracked_snapshot(directory, run)

    result, payload = invoke(run, "check")
    assert tracked_snapshot(directory, run) == before, result.stderr
    assert result.returncode == 1 and not payload["ok"], result.stderr
    assert {item["name"]: item["returncode"] for item in payload["checks"]} == {
        "pytest": 0,
        "lint": 1,
        "format": 0,
    }
    assert "F401" in result.stderr
    assert check(directory) == payload
    assert tracked_snapshot(directory, run) == before

    result = run("git", "push", "disposable", "HEAD:refs/heads/main")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "F401" in result.stdout + result.stderr
    assert tracked_snapshot(directory, run) == before
    result = run("git", "ls-remote", "disposable", "refs/heads/main")
    assert result.returncode == 0 and not result.stdout.strip()

    result, payload = invoke(run, "fix")
    assert result.returncode == 0 and payload["ok"], result.stderr
    assert "import os" not in sample.read_text()
    assert "from pathlib import Path" in sample.read_text()
    result, payload = invoke(run, "check")
    assert result.returncode == 0 and payload["ok"], result.stderr


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


@pytest.mark.parametrize("interface", ["callable", "cli"])
@pytest.mark.parametrize("variable", ["GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"])
def test_setup_uses_selected_repository_despite_inherited_git_context(
    project, foreign_repository, tooling_environment, monkeypatch, interface, variable
):
    directory, run = project
    foreign, foreign_hook = foreign_repository
    original = foreign_hook.read_bytes(), foreign_hook.stat().st_mode
    value = str(foreign if variable == "GIT_WORK_TREE" else foreign / ".git")
    monkeypatch.setenv(variable, value)
    monkeypatch.setitem(tooling_environment, variable, value)
    if interface == "callable":
        payload = setup(directory)
    else:
        result, payload = invoke(run, "setup")
        assert result.returncode == 0, result.stderr
    assert payload["ok"] and not payload["active"]
    assert "No Git remote" in payload["reason"]
    assert not (directory / ".git/hooks/pre-push").exists()
    assert (foreign_hook.read_bytes(), foreign_hook.stat().st_mode) == original


def test_check_does_not_pass_repository_selectors_into_test_subprocesses(
    project, foreign_repository, monkeypatch
):
    directory, _ = project
    foreign, foreign_hook = foreign_repository
    original = foreign_hook.read_bytes(), foreign_hook.stat().st_mode
    for variable, value in {
        "GIT_DIR": str(foreign / ".git"),
        "GIT_COMMON_DIR": str(foreign / ".git"),
        "GIT_WORK_TREE": str(foreign),
        "GIT_INDEX_FILE": str(foreign / ".git/index"),
        "GIT_OBJECT_DIRECTORY": str(foreign / ".git/objects"),
    }.items():
        monkeypatch.setenv(variable, value)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    (directory / "tests/test_sample.py").write_text(
        "import os\nimport subprocess\nfrom pathlib import Path\n\n\n"
        "def test_git_context_is_local():\n"
        '    variables = (\n        "GIT_DIR",\n        "GIT_COMMON_DIR",\n'
        '        "GIT_WORK_TREE",\n        "GIT_INDEX_FILE",\n        "GIT_OBJECT_DIRECTORY",\n    )\n'
        "    for name in variables:\n"
        "        present = name in os.environ\n"
        "        assert not present\n"
        '    assert os.environ["GIT_CONFIG_GLOBAL"] == os.devnull\n'
        "    actual = subprocess.check_output(\n"
        '        ["git", "rev-parse", "--show-toplevel"], text=True\n'
        "    ).strip()\n"
        "    assert Path(actual).resolve() == Path.cwd().resolve()\n"
        '    assert subprocess.check_output(["git", "remote"], text=True) == ""\n'
    )
    assert check(directory)["ok"]
    assert (foreign_hook.read_bytes(), foreign_hook.stat().st_mode) == original


def test_setup_still_honors_global_hook_configuration_when_git_context_is_inherited(
    project, foreign_repository, monkeypatch, tmp_path
):
    directory, run = project
    foreign, foreign_hook = foreign_repository
    add_remote(directory, run)
    global_config = tmp_path / "global-gitconfig"
    global_config.write_text(f'[core]\n\thooksPath = "{foreign_hook.parent}"\n')
    original = foreign_hook.read_bytes(), foreign_hook.stat().st_mode
    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    payload = setup(directory)
    assert not payload["ok"] and not payload["active"]
    assert "core.hooksPath" in payload["reason"]
    assert (foreign_hook.read_bytes(), foreign_hook.stat().st_mode) == original
    assert not (directory / ".git/hooks/pre-push").exists()


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


@pytest.mark.parametrize(
    ("variable", "options"),
    [
        ("PYTEST_ADDOPTS", "-k test_good"),
        ("PYTEST_ADDOPTS", "-m live"),
        ("PYTEST_PLUGINS", "nonexistent_inherited_plugin"),
    ],
)
def test_gate_and_push_ignore_inherited_pytest_overrides(
    project, tooling_environment, monkeypatch, variable, options
):
    directory, run = project
    add_remote(directory, run)
    result, payload = invoke(run, "setup")
    assert result.returncode == 0 and payload["active"]
    (directory / "tests/test_sample.py").write_text(
        "def test_good():\n    assert True\n\n\ndef test_bad():\n    assert False\n"
    )
    sentinel = directory / "live-was-run"
    (directory / "tests/test_live_sample.py").write_text(
        "from pathlib import Path\n\nimport pytest\n\n\n"
        "@pytest.mark.live\ndef test_live_sample():\n"
        '    Path("live-was-run").touch()\n'
    )
    # The gate must enforce non-live selection even without the configured default.
    configuration = directory / "pyproject.toml"
    configuration.write_text(
        configuration.read_text().replace("addopts = '-m \"not live\"'\n", "")
    )
    assert run("git", "add", ".").returncode == 0
    assert run("git", "commit", "-m", "Add controlled test failure").returncode == 0
    monkeypatch.setitem(tooling_environment, variable, options)
    before = tracked_snapshot(directory, run)

    result, payload = invoke(run, "check")
    assert result.returncode == 1 and not payload["ok"]
    assert "1 failed, 1 passed, 1 deselected" in result.stderr
    assert {item["name"]: item["returncode"] for item in payload["checks"]} == {
        "pytest": 1,
        "lint": 0,
        "format": 0,
    }
    assert not sentinel.exists()
    assert tracked_snapshot(directory, run) == before

    result = run("git", "push", "disposable", "HEAD:refs/heads/main")
    assert result.returncode != 0
    assert "1 failed, 1 passed, 1 deselected" in result.stdout + result.stderr
    assert not sentinel.exists()
    assert tracked_snapshot(directory, run) == before
    result = run("git", "ls-remote", "disposable", "refs/heads/main")
    assert result.returncode == 0 and not result.stdout.strip()


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


@pytest.fixture
def distinct_checkouts(project, tooling_environment):
    invoking, run = project
    (invoking / "rss_aggregator/checkout_sentinel.py").write_text(
        '"""Fixture-only editable-install identity and contract."""\n\n'
        'ORIGIN = "A"\nCONTRACT_PASSES = True\n'
    )
    (invoking / "tests/test_sample.py").unlink()
    (invoking / "tests/test_checkout_sentinel.py").write_text(
        "from rss_aggregator import checkout_sentinel\n\n\n"
        "def test_checkout_contract():\n"
        "    assert checkout_sentinel.CONTRACT_PASSES, (\n"
        '        f"SELECTED_CHECKOUT_SENTINEL_FAILURE:{checkout_sentinel.ORIGIN}"\n'
        "    )\n"
    )
    selected = invoking.parent / "selected"
    shutil.copytree(
        invoking,
        selected,
        ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__"),
    )

    def selected_run(*arguments):
        return subprocess.run(
            arguments,
            cwd=selected,
            env=tooling_environment,
            text=True,
            capture_output=True,
            check=False,
        )

    (selected / "rss_aggregator/checkout_sentinel.py").write_text(
        '"""Fixture-only editable-install identity and contract."""\n\n'
        'ORIGIN = "B"\nCONTRACT_PASSES = False\n'
    )
    for arguments in (
        ("git", "init", "--initial-branch=main"),
        ("uv", "sync", "--locked", "--offline"),
    ):
        result = selected_run(*arguments)
        assert result.returncode == 0, result.stdout + result.stderr
    for execute in (run, selected_run):
        assert execute("git", "add", ".").returncode == 0
        assert (
            execute("git", "commit", "-m", "Disposable contract checkout").returncode
            == 0
        )
    return invoking, run, selected, selected_run


@pytest.mark.parametrize("caller", ["cli", "callable"])
@pytest.mark.parametrize("import_override", [False, True])
def test_selected_checkout_checks_its_own_implementation(
    distinct_checkouts, tooling_environment, monkeypatch, caller, import_override
):
    invoking, run, selected, selected_run = distinct_checkouts
    assert (invoking / "tests/test_checkout_sentinel.py").read_bytes() == (
        selected / "tests/test_checkout_sentinel.py"
    ).read_bytes()
    assert (invoking / ".venv").is_dir()
    assert (selected / ".venv").is_dir()
    assert not (invoking / ".venv").samefile(selected / ".venv")
    before = tracked_snapshot(invoking, run), tracked_snapshot(selected, selected_run)
    result, payload = invoke(run, "check")
    assert result.returncode == 0 and payload["ok"], result.stderr
    result, payload = invoke(selected_run, "check")
    assert result.returncode == 1 and not payload["ok"], result.stderr
    assert "SELECTED_CHECKOUT_SENTINEL_FAILURE:B" in result.stderr
    assert {item["name"]: item["returncode"] for item in payload["checks"]} == {
        "pytest": 1,
        "lint": 0,
        "format": 0,
    }
    environment_override = ""
    if import_override:
        monkeypatch.setitem(tooling_environment, "PYTHONPATH", str(invoking))
        monkeypatch.setitem(tooling_environment, "HOME", str(invoking))
        # Set an invalid Python home after startup so only the gate's children
        # must tolerate it, not the interpreter launching the public operation.
        environment_override = (
            f"import os; os.environ['PYTHONHOME'] = {str(invoking)!r}; "
        )

    if caller == "cli":
        arguments = [
            "python",
            "-c",
            environment_override
            + "from rss_aggregator.development import main; main()",
            "check",
            "--project",
            str(selected),
        ]
    else:
        arguments = [
            "python",
            "-c",
            environment_override + "import json, sys; from pathlib import Path; "
            "from rss_aggregator.development import check; "
            "result = check(Path(sys.argv[1])); "
            "print(json.dumps(result, indent=2)); sys.exit(not result['ok'])",
            str(selected),
        ]
    result = run("uv", "run", "--locked", "--offline", *arguments)
    payload = json.loads(result.stdout)
    assert result.returncode == 1 and not payload["ok"], result.stderr
    assert "SELECTED_CHECKOUT_SENTINEL_FAILURE:B" in result.stderr
    assert {item["name"]: item["returncode"] for item in payload["checks"]} == {
        "pytest": 1,
        "lint": 0,
        "format": 0,
    }
    assert (
        tracked_snapshot(invoking, run),
        tracked_snapshot(selected, selected_run),
    ) == before


@pytest.mark.parametrize("command", ["check", "format", "fix"])
def test_unsynchronized_selected_project_does_not_use_invoking_environment(
    project, command
):
    invoking, run = project
    selected = invoking.parent / "unsynchronized"
    shutil.copytree(
        invoking,
        selected,
        ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__"),
    )
    before = {
        path.relative_to(selected): path.read_bytes()
        for path in selected.rglob("*")
        if path.is_file()
    }
    result = run(
        "uv",
        "run",
        "--locked",
        "--offline",
        "rss-dev",
        command,
        "--project",
        str(selected),
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 1 and not payload["ok"]
    assert all(item["returncode"] == 127 for item in payload["checks"])
    assert str(selected / ".venv") in result.stderr
    assert not (selected / ".venv").exists()
    assert {
        path.relative_to(selected): path.read_bytes()
        for path in selected.rglob("*")
        if path.is_file()
    } == before


def test_cross_checkout_setup_uses_selected_hook_environment(distinct_checkouts):
    invoking, run, selected, selected_run = distinct_checkouts
    add_remote(selected, selected_run)
    result = run(
        "uv",
        "run",
        "--locked",
        "--offline",
        "rss-dev",
        "setup",
        "--project",
        str(selected),
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"ok": True, "active": True}
    hook = (selected / ".git/hooks/pre-push").read_text()
    assert str(selected / ".venv") in hook
    assert str(invoking / ".venv") not in hook
    assert not (invoking / ".git/hooks/pre-push").exists()
