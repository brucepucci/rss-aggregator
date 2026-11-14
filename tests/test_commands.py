"""Process-contract regression coverage with isolated external executables."""

import fcntl
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


class CommandFixture:
    def __init__(self, state):
        self.state = state
        self.bin = state / "bin"
        self.bin.mkdir()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.env = dict(
            os.environ,
            HOME=str(state),
            RSS_STATE_DIR=str(state),
            RSS_PORT=str(port),
            PATH=str(self.bin),
            PYTHONPATH=str(ROOT / "src"),
        )
        self.tool(
            "podman",
            "import sys\nif sys.argv[1:3] in (['machine', 'list'], ['system', 'connection']): print('[]')\nelse: raise SystemExit('unexpected Podman operation')\n",
        )
        self.tool("brew", "raise SystemExit('unexpected Homebrew operation')\n")

    def run(self, *args):
        result = subprocess.run(
            [sys.executable, "-m", "rss_aggregator", *args],
            env=self.env,
            cwd=ROOT,
            check=False,
            text=True,
            capture_output=True,
        )
        if result.returncode == 0:
            assert result.stdout.startswith("{\n"), result.stdout
            assert isinstance(json.loads(result.stdout), dict)
        else:
            assert result.stdout == "", "Failure contaminated the JSON result stream"
        return result

    def tool(self, name, source):
        path = self.bin / name
        path.write_text("#!" + sys.executable + "\n" + source)
        path.chmod(0o755)

    def machine(
        self, *, running=False, other=False, replaced=False, extra="", state=None
    ):
        machine = {
            "Name": "rss-aggregator",
            "Created": "fixture-creation",
            "ConfigDir": {"Path": "/project"},
            "Rootful": False,
            "SSHConfig": {"Port": 50001, "IdentityPath": "/project/key"},
            "State": state or ("running" if running else "stopped"),
        }
        connection = {
            "Name": "rss-aggregator",
            "URI": "ssh://core@localhost:50001/run/user/501/podman/podman.sock",
            "Identity": "/project/key",
        }
        (self.state / "owner.json").write_text(
            json.dumps(
                {"machine": machine, "connection": connection, "token": "fixture-owner"}
            )
        )
        if replaced:
            machine["Created"] = "someone-elses-machine"
        machines = [{"Name": "rss-aggregator", "Running": running}]
        if other:
            machines.append({"Name": "personal", "Running": True})
        self.tool(
            "podman",
            f"""import json, sys
args = sys.argv[1:]
if args == ['machine', 'list', '--format', 'json']: print({json.dumps(machines)!r})
elif args == ['system', 'connection', 'list', '--format', 'json']: print({json.dumps([connection])!r})
elif args == ['machine', 'inspect', 'rss-aggregator']: print({json.dumps([machine])!r})
{extra}
else: raise SystemExit('UNEXPECTED MUTATION: ' + ' '.join(args))
""",
        )


@pytest.fixture
def command(tmp_path):
    return CommandFixture(tmp_path)


@pytest.mark.parametrize(
    "preview",
    [
        "Would upgrade shared 1.0 -> 2.0",
        "==> Would install dependencies for podman:\nshared 2.0\n",
    ],
)
def test_bootstrap_refuses_upgrade_or_reinstall_preview(command, preview):
    command.tool(
        "brew",
        f"""import sys
args = sys.argv[1:]
if args == ['list', '--formula', '--versions']: print('shared 1.0')
elif args[:2] == ['install', '--dry-run']: sys.stderr.write({preview!r})
elif args[:1] == ['install']: raise SystemExit('INSTALL MUST NOT RUN')
else: print('{{}}')
""",
    )
    result = command.run("setup")
    assert result.returncode != 0
    assert "approval" in result.stderr
    assert "INSTALL MUST NOT RUN" not in result.stderr
    assert (command.state / "host-before.json").exists()


def test_simulated_clean_setup_preserves_dependencies_defaults_and_inventory(command):
    external = command.state / "podman-state.json"
    external.write_text(
        json.dumps(
            {
                "machines": [{"Name": "personal", "Running": False}],
                "details": {},
                "connections": [
                    {
                        "Name": "personal",
                        "URI": "ssh://personal",
                        "Identity": "/personal/key",
                        "Default": True,
                    }
                ],
            }
        )
    )
    command.env["FAKE_PODMAN_STATE"] = str(external)
    command.env["FAKE_BREW_INSTALLED"] = str(command.state / "packages.json")
    Path(command.env["FAKE_BREW_INSTALLED"]).write_text(json.dumps({"shared": "1.0"}))
    command.tool("podman", (ROOT / "tests/fake_bootstrap_podman.py").read_text())
    command.tool(
        "brew",
        """import json, os, sys
from pathlib import Path
path = Path(os.environ['FAKE_BREW_INSTALLED'])
packages = json.loads(path.read_text())
args = sys.argv[1:]
if args == ['list', '--formula', '--versions']:
 print('\\n'.join(name + ' ' + version for name, version in packages.items()))
elif args == ['install', '--dry-run', 'podman']:
 print('Would install:\\npodman 6.2.0\\nvm-runtime 1.0')
elif args == ['install', 'podman']:
 packages.update({'podman': '6.2.0', 'vm-runtime': '1.0'}); path.write_text(json.dumps(packages)); print('installed helper')
else: print('{}')
""",
    )
    first = command.run("setup")
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["created"] is True
    assert "installed helper" in first.stderr
    initial = (command.state / "host-before.json").read_bytes()
    second = command.run("setup")
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["created"] is False
    assert (command.state / "host-before.json").read_bytes() == initial
    assert json.loads((command.state / "host-additions.json").read_text()) == {
        "podman": "6.2.0",
        "vm-runtime": "1.0",
    }
    state = json.loads(external.read_text())
    assert state["details"]["rss-aggregator"]["Resources"] == {
        "CPUs": 2,
        "Memory": 2048,
        "DiskSize": 20,
    }
    assert state["details"]["rss-aggregator"]["Rootful"] is False
    assert not any(m["Running"] for m in state["machines"])
    assert [c["Name"] for c in state["connections"] if c["Default"]] == ["personal"]


def test_setup_refuses_an_unowned_machine_before_mutation(command):
    command.tool(
        "podman",
        """import json, sys
args = sys.argv[1:]
if args == ['machine', 'list', '--format', 'json']: print('[{"Name":"rss-aggregator","Running":false}]')
elif args == ['system', 'connection', 'list', '--format', 'json']: print('[]')
else: raise SystemExit('MUTATION MUST NOT RUN')
""",
    )
    result = command.run("setup")
    assert result.returncode != 0
    assert "unowned" in result.stderr
    assert not (command.state / "owner.json").exists()


@pytest.mark.parametrize(
    "web",
    [
        {"sapi": "apache2handler", "php": "8.4.26", "upload_progress": ""},
        {"sapi": "apache2handler", "php": "8.4.27", "upload_progress": "1"},
        {"sapi": "cli", "php": "8.4.27", "upload_progress": "0"},
    ],
)
def test_security_failure_never_publishes_application(command, web):
    command.machine(
        running=True,
        extra="""elif '--connection' in args:
 args = args[2:]
 if args[:2] == ['image', 'inspect']:
  print(json.dumps([{'Id': 'verified-id', 'Os': 'linux', 'Architecture': 'arm64',
   'RepoDigests': ['docker.io/freshrss/freshrss@sha256:af44385229541bee24aa9148d0ff8f3b40e095373c63a2c0f74e09a27d3c59df', 'docker.io/freshrss/freshrss@sha256:c889a3e8baddfb73dc1cecc553697f250c70cbfc7dbac6212626bf5cbccfc797'],
   'Labels': {'org.opencontainers.image.version': '1.30.4', 'org.opencontainers.image.revision': '08f97bd497ace733c151ea1c709d85cd5b65cc53'},
   'Config': {'Entrypoint': ['./Docker/entrypoint.sh']}}]))
 elif args[:2] in (['container', 'exists'], ['volume', 'exists']): raise SystemExit(1)
 elif args[0] == 'run':
  if '--publish' in args: raise SystemExit('UNSAFE PUBLICATION')
  print('preflight-id')
 elif args[0] in ['cp', 'rm']: pass
 elif args[0] == 'exec':
  import os
  print(os.environ['FAKE_WEB_RESULT'])
 else: raise SystemExit('UNEXPECTED MUTATION: ' + ' '.join(args))""",
    )
    command.env["FAKE_WEB_RESULT"] = json.dumps(web)
    result = command.run("start")
    assert result.returncode != 0
    assert "PHP" in result.stderr
    assert "UNSAFE PUBLICATION" not in result.stderr


def test_start_preserves_another_running_machine(command):
    command.machine(other=True)
    result = command.run("start")
    assert result.returncode != 0
    assert "personal" in result.stderr and "running" in result.stderr
    assert "UNEXPECTED MUTATION" not in result.stderr


def test_stop_is_repeatable_when_already_stopped(command):
    command.machine()
    for _ in range(2):
        result = command.run("stop")
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["already_stopped"] is True


@pytest.mark.parametrize("operation", ["stop", "setup"])
def test_replaced_machine_identity_is_refused(command, operation):
    command.machine(replaced=True)
    result = command.run(operation)
    assert result.returncode != 0
    assert "identity" in result.stderr
    assert "UNEXPECTED MUTATION" not in result.stderr


def test_occupied_port_preserves_its_owner(command):
    command.machine(
        running=True,
        extra="""elif '--connection' in args:
 if args[2:4] in (['container', 'exists'], ['volume', 'exists']): raise SystemExit(1)
 raise SystemExit('UNEXPECTED MUTATION')""",
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", int(command.env["RSS_PORT"])))
        listener.listen()
        result = command.run("start")
        assert result.returncode != 0
        assert "occupied" in result.stderr
        assert "UNEXPECTED MUTATION" not in result.stderr


def test_time_wait_does_not_block_restart(command):
    command.machine(
        running=True,
        extra="""elif '--connection' in args:
 args = args[2:]
 if args[:2] in (['container', 'exists'], ['volume', 'exists']): raise SystemExit(1)
 elif args[:2] == ['image', 'inspect'] or args[0] == 'pull': raise SystemExit('REACHED_IMAGE_GATE')
 else: raise SystemExit('UNEXPECTED MUTATION')""",
    )
    with socket.socket() as listener, socket.socket() as client:
        # Match a reusable server listener on both Linux and macOS.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", int(command.env["RSS_PORT"])))
        listener.listen()
        client.connect(listener.getsockname())
        connection, _ = listener.accept()
        connection.close()
        client.recv(1)
    result = command.run("start")
    assert "occupied" not in result.stderr
    assert "REACHED_IMAGE_GATE" in result.stderr


def test_lock_prevents_concurrent_changes(command):
    with (command.state / "command.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = command.run("setup")
    assert result.returncode != 0
    assert "Another project command" in result.stderr


def test_unowned_volume_is_not_adopted(command):
    command.machine(
        running=True,
        extra="""elif '--connection' in args:
 args = args[2:]
 if args[:2] == ['container', 'exists']: raise SystemExit(1)
 elif args[:2] == ['volume', 'exists']: pass
 elif args[:2] == ['volume', 'inspect']: print('[{"Labels":{"io.rss-aggregator.owner":"someone-else"}}]')
 else: raise SystemExit('UNEXPECTED MUTATION')""",
    )
    result = command.run("start")
    assert result.returncode != 0
    assert "unowned volume" in result.stderr
    assert "UNEXPECTED MUTATION" not in result.stderr


def test_stop_failure_leaves_vm_running(command):
    command.machine(
        running=True,
        extra="""elif '--connection' in args:
 args = args[2:]
 if args[0] == 'ps': print('[{"Names":["rss-aggregator-freshrss"]}]')
 elif args[:2] == ['container', 'exists']: pass
 elif args[:2] == ['container', 'inspect']:
  print('[{"Config":{"Labels":{"io.rss-aggregator.owner":"fixture-owner"}},"State":{"Running":true}}]')
 elif args[0] == 'stop': sys.stderr.write('simulated stop failure'); raise SystemExit(125)
 else: raise SystemExit('UNEXPECTED MUTATION')""",
    )
    result = command.run("stop")
    assert result.returncode != 0
    assert "simulated stop failure" in result.stderr
    assert "UNEXPECTED MUTATION" not in result.stderr


@pytest.mark.parametrize("port", ["80", "65536", "not-a-number"])
def test_invalid_port_rejected_before_vm_start(command, port):
    command.machine()
    command.env["RSS_PORT"] = port
    result = command.run("start")
    assert result.returncode != 0
    assert "RSS_PORT" in result.stderr
    assert "UNEXPECTED MUTATION" not in result.stderr


def test_stop_preserves_unrelated_running_containers(command):
    command.machine(
        running=True,
        extra="""elif '--connection' in args:
 if args[2] == 'ps': print('[{"Names":["personal-workload"]}]')
 else: raise SystemExit('UNEXPECTED MUTATION')""",
    )
    result = command.run("stop")
    assert result.returncode != 0
    assert "personal-workload" in result.stderr
    assert "UNEXPECTED MUTATION" not in result.stderr


def test_transitioning_vm_is_not_reported_stopped(command):
    command.machine(state="starting")
    result = command.run("stop")
    assert result.returncode != 0
    assert "starting" in result.stderr
    assert "already stopped" not in result.stdout


def test_fetch_waits_for_completed_installer(command):
    command.machine(
        running=True,
        extra="""elif '--connection' in args:
 args = args[2:]
 if args[:2] == ['container', 'inspect']:
  print('[{"Config":{"Labels":{"io.rss-aggregator.owner":"fixture-owner"}}}]')
 elif args[0] == 'exec' and args[2] == 'test':
  raise SystemExit(1 if args[-1].endswith('applied_migrations.txt') else 0)
 else: raise SystemExit('UNEXPECTED MUTATION')""",
    )
    result = command.run("fetch")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "installer_pending"


def test_unreachable_engine_is_not_absent_container(command):
    command.machine(
        running=True,
        extra="""elif '--connection' in args:
 if args[2:4] == ['container', 'exists']:
  sys.stderr.write('engine unreachable'); raise SystemExit(125)
 else: raise SystemExit('UNEXPECTED MUTATION')""",
    )
    result = command.run("start")
    assert result.returncode != 0
    assert "Cannot determine container state" in result.stderr
    assert "engine unreachable" in result.stderr
    assert "UNEXPECTED MUTATION" not in result.stderr


def test_stopped_status_does_not_invent_application_absence(command):
    command.machine()
    result = command.run("status")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["application"] == "unknown"


def test_absent_status_does_not_create_owned_resources(command):
    result = command.run("status")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["machine"] == "absent"
    assert not (command.state / "owner.json").exists()
