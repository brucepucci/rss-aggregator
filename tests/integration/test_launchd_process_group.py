"""Explicitly selected native launchd control; no Podman or production resources."""

import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

import pytest


def reachable(port):
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/", timeout=0.5
        ) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


@pytest.mark.live
@pytest.mark.parametrize("abandon", [False, True], ids=["default-cleanup", "abandon"])
def test_native_job_exit_controls_only_disposable_helper_lifetime(tmp_path, abandon):
    if sys.platform != "darwin" or os.getuid() == 0 or not shutil.which("launchctl"):
        pytest.fail(
            "Explicit native check requires macOS launchctl and a normal GUI user session"
        )
    domain = f"gui/{os.getuid()}"
    session = subprocess.run(["launchctl", "print", domain], capture_output=True)
    assert session.returncode == 0, "Log in to the normal GUI session first"
    label = "io.rss-aggregator.diagnostic." + uuid.uuid4().hex
    ready = tmp_path / "ready.json"
    checked = tmp_path / "checked"
    helper = """
import json, sys, time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers()
    def log_message(self, *args): pass
server = HTTPServer(('127.0.0.1', 0), Handler)
server.timeout = 0.05
Path(sys.argv[1]).write_text(json.dumps({'port': server.server_port}))
deadline = time.monotonic() + 10
while time.monotonic() < deadline: server.handle_request()
server.server_close()
"""
    parent = """
import json, subprocess, sys, time, urllib.request
from pathlib import Path
subprocess.Popen([sys.executable, '-I', '-c', sys.argv[1], sys.argv[2]],
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
deadline = time.monotonic() + 5
ready = Path(sys.argv[2])
while not ready.exists():
    assert time.monotonic() < deadline
    time.sleep(0.01)
port = json.loads(ready.read_text())['port']
with urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=1) as response:
    assert response.status == 200
Path(sys.argv[3]).touch()
"""
    job = tmp_path / (label + ".plist")
    job.write_bytes(
        plistlib.dumps(
            {
                "Label": label,
                "ProgramArguments": [
                    sys.executable,
                    "-I",
                    "-c",
                    parent,
                    helper,
                    str(ready),
                    str(checked),
                ],
                "RunAtLoad": True,
                "AbandonProcessGroup": abandon,
                "StandardOutPath": "/dev/null",
                "StandardErrorPath": "/dev/null",
            }
        )
    )
    job.chmod(0o600)
    target = domain + "/" + label
    port = None
    try:
        result = subprocess.run(
            ["launchctl", "bootstrap", domain, str(job)], capture_output=True
        )
        assert result.returncode == 0, "Disposable diagnostic bootstrap failed"
        deadline = time.monotonic() + 7
        while True:
            inspected = subprocess.run(
                ["launchctl", "print", target], capture_output=True, text=True
            )
            assert inspected.returncode == 0
            lines = {line.strip() for line in inspected.stdout.splitlines()}
            if (
                checked.exists()
                and "state = not running" in lines
                and "last exit code = 0" in lines
            ):
                break
            assert time.monotonic() < deadline, (
                "Diagnostic parent did not exit successfully"
            )
            time.sleep(0.02)
        port = json.loads(ready.read_text())["port"]
        # Independent observation only AFTER launchd reports parent completion.
        if abandon:
            assert reachable(port)
        else:
            deadline = time.monotonic() + 2
            while reachable(port) and time.monotonic() < deadline:
                time.sleep(0.02)
            assert not reachable(port)
    finally:
        # Unique label and exact generated definition: no real VM/helper targets.
        removed = subprocess.run(["launchctl", "bootout", target], capture_output=True)
        assert removed.returncode in (0, 3, 113), (
            "Unload only this diagnostic label; inspect locally"
        )
        if port is None and ready.exists():
            port = json.loads(ready.read_text())["port"]
        if port is not None:
            deadline = time.monotonic() + 12
            while reachable(port):
                assert time.monotonic() < deadline, (
                    "Finite diagnostic helper failed to exit"
                )
                time.sleep(0.05)
