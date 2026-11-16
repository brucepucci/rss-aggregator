"""Long-lived VM helpers must not retain short-lived command leases."""

import json
import os
import socket
from contextlib import contextmanager
from pathlib import Path

from rss_aggregator import Runtime
from tests.test_backups import BackupProject
from tests.test_supervision import supervised as supervised


@contextmanager
def vm_launcher_helper(tool, tmp_path):
    """Model a native VM launcher daemonizing with every inherited descriptor."""
    gate = tmp_path / "vm-helper-release.fifo"
    pidfile = tmp_path / "vm-helper.pid"
    os.mkfifo(gate, 0o600)
    helper = tmp_path / "vm-helper.py"
    helper.write_text(
        "import os, sys\nfrom pathlib import Path\n"
        + f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
        + "os.write(int(sys.argv[1]), b'1'); os.close(int(sys.argv[1]))\n"
        + f"with Path({str(gate)!r}).open('rb', buffering=0) as gate: gate.read(1)\n"
    )
    shebang, original = tool.read_text().split("\n", 1)
    tool.write_text(
        shebang
        + "\nimport os, subprocess, sys\n"
        + "if sys.argv[1:3] == ['machine', 'start']:\n"
        + " read_fd, write_fd = os.pipe()\n"
        + " os.set_inheritable(write_fd, True)\n"
        + f" subprocess.Popen([sys.executable, {str(helper)!r}, str(write_fd)], close_fds=False, start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        + " os.close(write_fd)\n"
        + " assert os.read(read_fd, 1) == b'1'\n"
        + " os.close(read_fd)\n"
        + original
    )
    try:
        yield pidfile
    finally:
        if pidfile.exists():
            with gate.open("wb", buffering=0) as release:
                release.write(b"1")


def test_vm_launcher_daemon_cannot_block_commands_after_explicit_start(
    request, monkeypatch, tmp_path
):
    command, engine = request.getfixturevalue("supervised")
    state = json.loads(engine.read_text())
    state["machine"]["State"] = "stopped"
    state["app"]["State"] = {"Running": False, "Status": "exited"}
    engine.write_text(json.dumps(state))
    for name, value in command.env.items():
        monkeypatch.setenv(name, value)

    class FixtureSocket(socket.socket):
        def bind(self, address):
            if address != ("127.0.0.1", int(command.env["RSS_PORT"])):
                return super().bind(address)

    monkeypatch.setattr("rss_aggregator.runtime.socket.socket", FixtureSocket)
    with vm_launcher_helper(command.bin / "podman", tmp_path) as pidfile:
        assert Runtime(state_dir=command.state).run("start")["machine"] == "running"
        os.kill(int(pidfile.read_text()), 0)
        status = command.run("status")
        assert status.returncode == 0, status.stderr
        assert json.loads(status.stdout)["http_ready"] is True


def test_backup_vm_launcher_daemon_cannot_retain_the_command_lock(tmp_path):
    project = BackupProject(tmp_path)
    machine = project.engine()["machine"]
    machine["State"] = "stopped"
    project.update(machine=machine)
    with vm_launcher_helper(Path(project.env["PATH"]) / "podman", tmp_path) as pidfile:
        backup = project.run("backup")
        assert backup.returncode == 0, backup.stderr
        assert json.loads(backup.stdout)["verified"] is True
        os.kill(int(pidfile.read_text()), 0)
        status = project.run("status")
        assert status.returncode == 0, status.stderr
        assert json.loads(status.stdout)["machine"] == "stopped"
