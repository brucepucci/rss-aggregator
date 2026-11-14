"""Machine-readable results through the public project process seam."""

import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from rss_aggregator import Runtime, RuntimeFailure

ROOT = Path(__file__).resolve().parents[1]


def test_absent_status_is_indented_json(tmp_path):
    env = dict(os.environ, RSS_STATE_DIR=str(tmp_path), PYTHONPATH=str(ROOT / "src"))
    result = subprocess.run(
        [sys.executable, "-m", "rss_aggregator", "status"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("{\n")
    assert json.loads(result.stdout)["machine"] == "absent"
    assert not (tmp_path / "owner.json").exists()


def test_callable_instances_resolve_current_environment_independently(
    tmp_path, monkeypatch
):
    first_state, second_state = tmp_path / "first", tmp_path / "second"
    monkeypatch.setenv("RSS_STATE_DIR", str(first_state))
    first = Runtime(root=ROOT)
    monkeypatch.setenv("RSS_STATE_DIR", str(second_state))
    second = Runtime(root=ROOT)
    assert first.run("status")["machine"] == "absent"
    assert second.run("status")["machine"] == "absent"
    assert (first_state / "command.lock").exists()
    assert (second_state / "command.lock").exists()
    assert not (first_state / "owner.json").exists()
    assert not (second_state / "owner.json").exists()


def test_callable_operations_share_the_process_command_lock(tmp_path):
    with (tmp_path / "command.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeFailure, match="Another project command"):
            Runtime(root=ROOT, state_dir=tmp_path).run("status")


def test_unknown_callable_operation_fails_without_native_effects(tmp_path):
    state = tmp_path / "not-created"
    with pytest.raises(RuntimeFailure, match="Unknown operation"):
        Runtime(root=ROOT, state_dir=state).run("unknown")
    assert not state.exists()
