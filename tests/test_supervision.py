"""CLI/Runtime production-operation tests using only disposable external state."""

import fcntl
import json
import os
import plistlib
import socket
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest
from test_backups import APP, BackupProject
from test_commands import ROOT, CommandFixture

from rss_aggregator import Runtime


def test_explicit_stop_persists_across_processes_even_when_already_stopped(tmp_path):
    command = CommandFixture(tmp_path)
    command.machine()
    first = command.run("stop")
    assert first.returncode == 0, first.stderr
    status = command.run("supervision-status")
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["intent"] == "stopped"
    assert json.loads(command.run("supervision-status").stdout)["intent"] == "stopped"
    assert not (tmp_path / "Library/LaunchAgents").exists()


def registration(tmp_path):
    command = CommandFixture(tmp_path)
    command.machine()
    command.tool(
        "launchctl",
        """import json, os, plistlib, sys
from pathlib import Path
path = Path(os.environ['HOME']) / 'loaded-job.json'
args = sys.argv[1:]
if args[0] == 'print':
 if '/' not in args[1].removeprefix('gui/'): pass
 elif path.exists():
  loaded = json.loads(path.read_text()); job = Path(loaded['path'])
  definition = plistlib.loads(job.read_bytes())
  print('path = ' + str(job))
  print('program = ' + loaded.get('program', definition['ProgramArguments'][0]))
  print('arguments = {\\n' + '\\n'.join(loaded.get('arguments', definition['ProgramArguments'])) + '\\n}')
  print('environment = {\\n' + '\\n'.join(key + ' => ' + value for key,value in loaded.get('environment', definition['EnvironmentVariables']).items()) + '\\n}')
 else: raise SystemExit(113)
elif args[0] == 'bootstrap': path.write_text(json.dumps({'path': args[2]}))
elif args[0] == 'bootout': path.unlink(missing_ok=True)
else: raise SystemExit('unexpected launchctl call')
""",
    )
    return command


def invoke(command, *args):
    # The launchctl/Podman executables and HOME are disposable even on Linux CI.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.platform='darwin'; from rss_aggregator.cli import main; main()",
            *args,
        ],
        env=command.env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result


def test_opt_in_registration_is_repeatable_and_removal_preserves_other_jobs(tmp_path):
    command = registration(tmp_path)
    jobs = tmp_path / "Library/LaunchAgents"
    jobs.mkdir(parents=True)
    unrelated = jobs / "other.plist"
    unrelated.write_bytes(b"unrelated job")
    command.env["RSS_PASSWORD"] = "synthetic-secret"
    installed = invoke(command, "supervision-install")
    assert installed.returncode == 0, installed.stderr
    result = json.loads(installed.stdout)
    assert result["enabled"] is True
    assert result["intent"] == "unset"
    job = Path(result["job"])
    assert b"synthetic-secret" not in job.read_bytes()
    assert "synthetic-secret" not in (tmp_path / "supervision.json").read_text()
    definition = plistlib.loads(job.read_bytes())
    assert definition["RunAtLoad"] is True
    assert definition["StartInterval"] == 60
    assert definition["ProgramArguments"][-1] == "supervision-tick"
    assert definition["EnvironmentVariables"]["RSS_STATE_DIR"] == str(
        tmp_path.resolve()
    )
    before = job.read_bytes()
    assert invoke(command, "supervision-install").returncode == 0
    assert job.read_bytes() == before
    assert unrelated.read_bytes() == b"unrelated job"
    removed = invoke(command, "supervision-remove")
    assert removed.returncode == 0, removed.stderr
    assert not job.exists()
    assert unrelated.read_bytes() == b"unrelated job"
    assert invoke(command, "supervision-remove").returncode == 0


@pytest.fixture
def supervised(tmp_path):
    command = registration(tmp_path)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    command.env["RSS_PORT"] = str(server.server_port)
    command.env["NO_PROXY"] = "127.0.0.1"
    command.env["RSS_TAILSCALE"] = "0"
    owner = json.loads((tmp_path / "owner.json").read_text())
    owner["machine"]["State"] = "running"
    pin = json.loads((ROOT / "config/image.json").read_text())
    engine = tmp_path / "engine.json"
    engine.write_text(
        json.dumps(
            {
                "machine": owner["machine"],
                "connection": owner["connection"],
                "calls": [],
                "checks": {},
                "image": {
                    "Id": "sha256:fixture-image",
                    "Os": "linux",
                    "Architecture": "arm64",
                    "RepoDigests": [
                        pin["image"],
                        pin["image"].split("@")[0] + "@" + pin["platform_manifest"],
                    ],
                    "Labels": {
                        "org.opencontainers.image.version": pin["version"],
                        "org.opencontainers.image.revision": pin["revision"],
                    },
                    "Config": {
                        "Entrypoint": ["./Docker/entrypoint.sh"],
                        "Cmd": ["run"],
                    },
                },
                "app": {
                    "Image": "sha256:fixture-image",
                    "Config": {
                        "Labels": {"io.rss-aggregator.owner": "fixture-owner"},
                        "Entrypoint": ["./Docker/entrypoint.sh"],
                        "Cmd": ["run"],
                        "Env": ["LISTEN=127.0.0.1:8088"],
                    },
                    "HostConfig": {
                        "PortBindings": {
                            "80/tcp": [
                                {
                                    "HostIp": "127.0.0.1",
                                    "HostPort": str(server.server_port),
                                }
                            ]
                        }
                    },
                    "Mounts": [
                        {
                            "Name": "rss-aggregator-data",
                            "Destination": "/var/www/FreshRSS/data",
                        }
                    ],
                    "State": {"Running": True, "Status": "running"},
                },
            }
        )
    )
    command.tool("podman", (ROOT / "tests/fake_supervision_podman.py").read_text())
    try:
        start = command.run("start")
        assert start.returncode == 0, start.stderr
        installed = invoke(command, "supervision-install")
        assert installed.returncode == 0, installed.stderr
        yield command, engine
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_healthy_checks_do_not_restart_application(supervised):
    command, engine = supervised
    before = json.loads(engine.read_text())
    for _ in range(2):
        tick = command.run("supervision-tick")
        assert tick.returncode == 0, tick.stderr
        assert json.loads(tick.stdout)["state"] == "healthy"
    after = json.loads(engine.read_text())
    assert after["app"] == before["app"]
    assert not any(
        call[2] in ("stop", "start", "cp", "run", "rm")
        for call in after["calls"][len(before["calls"]) :]
        if call[:2] == ["--connection", "rss-aggregator"]
    )


def test_unexpected_application_failure_recovers_through_normal_gates(
    supervised, monkeypatch
):
    command, engine = supervised
    for key, value in command.env.items():
        monkeypatch.setenv(key, value)
    state = json.loads(engine.read_text())
    state["app"]["State"] = {"Status": "exited", "Running": False}
    engine.write_text(json.dumps(state))

    class FixtureSocket(socket.socket):
        def bind(self, address):
            # Disposable HTTP readiness stands in for Podman's publication. The
            # existing command suite separately tests real port conflict refusal.
            if address != ("127.0.0.1", int(command.env["RSS_PORT"])):
                return super().bind(address)

    monkeypatch.setattr("rss_aggregator.runtime.socket.socket", FixtureSocket)
    tick = Runtime(root=ROOT, state_dir=command.state).run("supervision-tick")
    assert tick["state"] == "recovered"
    assert json.loads(engine.read_text())["app"]["State"]["Running"] is True
    status = json.loads(command.run("supervision-status").stdout)
    assert status["observed"]["availability"] == "healthy"
    assert status["intent"] == "running"


def test_recovery_is_bounded_persisted_redacted_and_explicit_start_resets_budget(
    supervised, monkeypatch
):
    command, engine = supervised
    for key, value in command.env.items():
        monkeypatch.setenv(key, value)
    state = json.loads(engine.read_text())
    state.update(fail_scheduler=True, fail_image=True)
    engine.write_text(json.dumps(state))
    now = [1000.0]
    monkeypatch.setattr("rss_aggregator.supervision.time.time", lambda: now[0])
    runtime = Runtime(root=ROOT, state_dir=command.state)
    with pytest.raises(ValueError, match="Guarded recovery failed"):
        runtime.run("supervision-tick")
    status = runtime.run("supervision-status")
    assert status["recovery"]["attempts"] == 1
    assert status["recovery"]["history"][-1]["reason"] == "external_command_failed"
    assert status["observed"]["reason"] == "external_command_failed"
    assert status["recovery"]["next_retry_at"] == 1060.0
    before = json.loads(engine.read_text())
    with pytest.raises(ValueError, match="backoff"):
        runtime.run("supervision-tick")
    after = json.loads(engine.read_text())
    assert after["app"] == before["app"]
    for clock in (1060.0, 1360.0):
        now[0] = clock
        with pytest.raises(ValueError, match="Guarded recovery failed"):
            runtime.run("supervision-tick")
    status = runtime.run("supervision-status")
    assert status["state"] == "retry_exhausted"
    assert status["recovery"]["attempts"] == 3
    assert status["recovery"]["next_retry_at"] is None
    assert [item["outcome"] for item in status["recovery"]["history"]] == ["failed"] * 3
    assert "synthetic-secret" not in json.dumps(status)
    assert "synthetic-secret" not in command.run("supervision-tick").stderr
    now[0] = 100000.0
    with pytest.raises(ValueError, match="retry_exhausted"):
        Runtime(root=ROOT, state_dir=command.state).run("supervision-tick")
    state = json.loads(engine.read_text())
    state.update(fail_scheduler=False, fail_image=False)
    engine.write_text(json.dumps(state))
    assert runtime.run("start")["machine"] == "running"
    assert runtime.run("supervision-status")["recovery"]["attempts"] == 0


@pytest.mark.parametrize("marker", [{"version": 1, "phase": "snapshot"}, "malformed"])
def test_maintenance_never_authorizes_recovery_or_explicit_start(supervised, marker):
    command, engine = supervised
    hold = command.state / "maintenance.json"
    hold.write_text(json.dumps(marker))
    before = json.loads(engine.read_text())
    assert command.run("supervision-tick").returncode != 0
    assert command.run("start").returncode != 0
    assert (
        json.loads(command.run("supervision-status").stdout)["state"] == "maintenance"
    )
    assert json.loads(engine.read_text())["app"] == before["app"]
    assert hold.exists()


def test_contention_defers_without_changing_resources_or_operator_intent(supervised):
    command, engine = supervised
    before = engine.read_bytes()
    with (command.state / "command.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert "Another project command" in command.run("supervision-tick").stderr
        assert "Another project command" in command.run("stop").stderr
    assert engine.read_bytes() == before
    assert json.loads(command.run("supervision-status").stdout)["intent"] == "running"


def test_interrupted_runtime_keeps_orphan_native_work_serialized(tmp_path):
    command = registration(tmp_path)
    signal = tmp_path / "signal"
    release = tmp_path / "release"
    os.mkfifo(signal)
    os.mkfifo(release)
    command.tool(
        "podman",
        f"""from pathlib import Path
with Path({str(signal)!r}).open('wb', buffering=0) as stream: stream.write(b'1')
with Path({str(release)!r}).open('rb', buffering=0) as stream: stream.read(1)
""",
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "rss_aggregator", "start"],
        env=command.env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with signal.open("rb", buffering=0) as stream:
            assert stream.read(1) == b"1"
        process.kill()
        process.wait(timeout=5)
        tick = command.run("supervision-tick")
        assert tick.returncode != 0
        assert "Another project command" in tick.stderr
    finally:
        with release.open("wb", buffering=0) as stream:
            stream.write(b"1")
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize("foreign", ["file", "symlink", "loaded"])
def test_registration_refuses_foreign_jobs_without_adoption(tmp_path, foreign):
    command = registration(tmp_path)
    job = tmp_path / "Library/LaunchAgents/io.rss-aggregator.supervision.plist"
    job.parent.mkdir(parents=True)
    target = tmp_path / "foreign.plist"
    target.write_bytes(b"foreign")
    if foreign == "file":
        job.write_bytes(b"foreign")
    elif foreign == "symlink":
        job.symlink_to(target)
    else:
        (tmp_path / "loaded-job.json").write_text(json.dumps({"path": str(target)}))
    assert invoke(command, "supervision-install").returncode != 0
    assert not (tmp_path / "supervision.json").exists()
    assert target.read_bytes() == b"foreign"
    if foreign != "loaded":
        assert job.read_bytes() == b"foreign"


def test_captured_settings_ignore_interactive_environment_and_do_not_copy_secrets(
    supervised,
):
    command, _engine = supervised
    command.env["RSS_PORT"] = "not-an-integer"
    command.env["RSS_TAILSCALE"] = "invalid"
    command.env["RSS_PASSWORD"] = "synthetic-secret"
    assert command.run("supervision-tick").returncode == 0
    record = (command.state / "supervision.json").read_text()
    assert "synthetic-secret" not in record
    assert "RSS_PASSWORD" not in record
    job = command.state / "Library/LaunchAgents/io.rss-aggregator.supervision.plist"
    assert b"synthetic-secret" not in job.read_bytes()


@pytest.mark.parametrize("failure", ["dependency", "job", "session", "inactive"])
def test_status_reports_unavailable_registration_without_mutating_application(
    supervised, failure
):
    command, engine = supervised
    before = json.loads(engine.read_text())["app"]
    if failure == "dependency":
        (command.bin / "podman").unlink()
    elif failure == "job":
        job = command.state / "Library/LaunchAgents/io.rss-aggregator.supervision.plist"
        job.write_bytes(b"changed foreign definition")
    elif failure == "session":
        command.tool("launchctl", "raise SystemExit(3)")
    else:
        (command.state / "loaded-job.json").unlink()
    status = command.run("supervision-status")
    assert status.returncode == 0, status.stderr
    expected = {
        "dependency": "configuration_unavailable",
        "job": "configuration_unavailable",
        "session": "session_unavailable",
        "inactive": "inactive",
    }
    assert json.loads(status.stdout)["state"] == expected[failure]
    assert command.run("supervision-tick").returncode != 0
    assert json.loads(engine.read_text())["app"] == before
    if failure == "job":
        assert invoke(command, "supervision-remove").returncode != 0
        assert job.read_bytes() == b"changed foreign definition"


def test_failed_stop_records_intent_and_failed_start_does_not_claim_health(tmp_path):
    command = CommandFixture(tmp_path)
    command.machine(replaced=True)
    failed = command.run("stop")
    assert failed.returncode != 0
    assert json.loads(command.run("supervision-status").stdout)["intent"] == "stopped"
    failed = command.run("start")
    assert failed.returncode != 0
    status = json.loads(command.run("supervision-status").stdout)
    assert status["intent"] == "running"
    assert status["observed"]["availability"] == "unknown"
    assert not (command.state / "Library/LaunchAgents").exists()


def test_manual_backup_never_resumes_an_intentionally_stopped_application(tmp_path):
    project = BackupProject(tmp_path)
    state = project.engine()
    state["containers"][APP]["State"]["Running"] = True
    state["rootful"] = [{"Names": ["foreign"]}]
    project.update(**state)
    assert project.run("stop").returncode != 0
    state = project.engine()
    state["rootful"] = []
    project.update(**state)
    backup = project.run("backup")
    assert backup.returncode == 0, backup.stderr
    assert project.engine()["containers"][APP]["State"]["Running"] is False
    status = json.loads(project.run("supervision-status").stdout)
    assert status["intent"] == "stopped"


def test_partial_registration_is_actionable_repeatable_and_removable(tmp_path):
    command = registration(tmp_path)
    good = (command.bin / "launchctl").read_text().split("\n", 1)[1]
    command.tool(
        "launchctl",
        "import sys\nif sys.argv[1] == 'bootstrap': raise SystemExit('synthetic-secret')\n"
        + good,
    )
    result = invoke(command, "supervision-install")
    assert result.returncode != 0
    assert "synthetic-secret" not in result.stderr
    assert (tmp_path / "supervision.json").exists()
    status = json.loads(command.run("supervision-status").stdout)
    assert status["enabled"] is True
    assert status["state"] == "inactive"
    command.tool("launchctl", good)
    assert invoke(command, "supervision-install").returncode == 0
    assert invoke(command, "supervision-remove").returncode == 0


@pytest.mark.parametrize("foreign", ["container", "machine"])
def test_supervisor_never_adopts_foreign_runtime_resources(supervised, foreign):
    command, engine = supervised
    state = json.loads(engine.read_text())
    if foreign == "container":
        state["app"]["Config"]["Labels"]["io.rss-aggregator.owner"] = "foreign"
    else:
        state["machine"]["Created"] = "foreign-creation"
    engine.write_text(json.dumps(state))
    before = json.loads(engine.read_text())
    assert command.run("supervision-tick").returncode != 0
    after = json.loads(engine.read_text())
    assert after["app"] == before["app"]
    assert after["machine"] == before["machine"]
    assert after["checks"] == before["checks"]
    status = json.loads(command.run("supervision-status").stdout)
    assert status["state"] == "recovery_failed"
    assert status["recovery"]["attempts"] == 1


@pytest.mark.parametrize("changed", ["program", "arguments", "environment"])
def test_supervision_refuses_changed_loaded_identity_even_with_unchanged_job(
    supervised, changed
):
    command, engine = supervised
    loaded = command.state / "loaded-job.json"
    state = json.loads(loaded.read_text())
    state[changed] = (
        {"HOME": "/foreign"}
        if changed == "environment"
        else ["foreign"]
        if changed == "arguments"
        else "/foreign/program"
    )
    loaded.write_text(json.dumps(state))
    original = loaded.read_bytes()
    before = json.loads(engine.read_text())["app"]
    assert command.run("supervision-tick").returncode != 0
    status = json.loads(command.run("supervision-status").stdout)
    assert status["state"] == "configuration_unavailable"
    assert invoke(command, "supervision-remove").returncode != 0
    assert loaded.read_bytes() == original
    assert json.loads(engine.read_text())["app"] == before


def test_registration_refuses_symlinked_service_directory(tmp_path):
    command = registration(tmp_path)
    library = tmp_path / "Library"
    library.mkdir()
    foreign = tmp_path / "foreign-jobs"
    foreign.mkdir()
    (library / "LaunchAgents").symlink_to(foreign, target_is_directory=True)
    installed = invoke(command, "supervision-install")
    assert installed.returncode != 0
    assert list(foreign.iterdir()) == []
    assert not (tmp_path / "supervision.json").exists()


def test_recovery_fixture_start_does_not_change_real_operator_intent(tmp_path):
    project = BackupProject(tmp_path)
    real_intent = project.state_dir / "operator-intent.json"
    original = b'{"version": 1, "intent": "stopped"}\n'
    real_intent.write_bytes(original)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        project.env["RSS_PORT"] = str(listener.getsockname()[1])
    result = subprocess.run(
        [sys.executable, "-m", "tests.integration.backup_runner"],
        input=json.dumps({"token": "0123456789abcdef", "operation": "start"}),
        env=project.env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    # A missing disposable app can fail start, but must never authorize real intent.
    assert result.returncode != 0
    assert real_intent.read_bytes() == original
    assert (
        json.loads(
            (
                project.state_dir / "test-0123456789abcdef-operator-intent.json"
            ).read_text()
        )["intent"]
        == "running"
    )


def test_disabled_and_intentionally_stopped_ticks_never_start_resources(tmp_path):
    command = registration(tmp_path)
    disabled = command.run("supervision-tick")
    assert disabled.returncode == 0, disabled.stderr
    assert json.loads(disabled.stdout)["state"] == "disabled"
    assert invoke(command, "supervision-install").returncode == 0
    unset = command.run("supervision-tick")
    assert json.loads(unset.stdout)["state"] == "intent_unset"
    assert command.run("stop").returncode == 0
    for _ in range(2):
        tick = command.run("supervision-tick")
        assert tick.returncode == 0, tick.stderr
        assert json.loads(tick.stdout)["state"] == "intentionally_stopped"
    assert json.loads(command.run("supervision-status").stdout)["intent"] == "stopped"
