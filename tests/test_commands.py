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
            PYTHONPATH=str(ROOT),
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
        self,
        *,
        running=False,
        other=False,
        replaced=False,
        extra="",
        state=None,
        rootful="[]",
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
elif args == ['machine', 'ssh', 'rss-aggregator', 'sudo', 'podman', 'ps', '--format', 'json']:
 if {rootful!r} is None: raise SystemExit('rootful inspection failed')
 print({rootful!r})
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
        "Would reinstall shared 1.0",
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
elif args == ['list', '--cask', '--versions']: print('healthy 2.0')
elif args == ['info', '--json=v2', '--installed', '--cask'] or args == ['list', '--cask']:
 raise SystemExit('FALLBACK MUST NOT RUN')
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
    inventory = json.loads(initial)
    assert inventory["casks"] == "healthy 2.0\n"
    assert "cask_names" not in inventory
    assert "cask_inventory_warning" not in inventory
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


@pytest.fixture
def incomplete_cask_setup(command):
    packages = command.state / "packages.json"
    packages.write_text(
        json.dumps(
            {
                "packages": {"shared": "1.0"},
                "casks": {"discord": "0.0.398.upgrading", "healthy": "2.0"},
            }
        )
    )
    podman = command.state / "podman-state.json"
    podman.write_text(
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
    command.env["FAKE_BREW_INSTALLED"] = str(packages)
    command.env["FAKE_PODMAN_STATE"] = str(podman)
    command.tool("brew", (ROOT / "tests/fake_bootstrap_brew.py").read_text())
    command.tool("podman", (ROOT / "tests/fake_bootstrap_podman.py").read_text())
    return command


def test_setup_preserves_incomplete_casks_with_read_only_fallback(
    incomplete_cask_setup,
):
    command = incomplete_cask_setup
    result = command.run("setup")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"machine": "rss-aggregator", "created": True}
    warning = "Error: Cask 'discord' is not installed."
    assert warning in result.stderr
    initial = (command.state / "host-before.json").read_bytes()
    inventory = json.loads(initial)
    assert inventory["casks"] == "healthy 2.0\n"
    assert inventory["cask_names"] == "discord\nhealthy\n"
    assert warning in inventory["cask_inventory_warning"]
    assert inventory["packages"] == {"shared": "1.0"}
    assert inventory["dependencies"] == "shared:\n"
    assert json.loads((command.state / "host-additions.json").read_text()) == {
        "podman": "6.2.0",
        "vm-runtime": "1.0",
    }
    host = json.loads(Path(command.env["FAKE_BREW_INSTALLED"]).read_text())
    assert host == {
        "packages": {"shared": "1.0", "podman": "6.2.0", "vm-runtime": "1.0"},
        "casks": {"discord": "0.0.398.upgrading", "healthy": "2.0"},
    }
    podman = json.loads(Path(command.env["FAKE_PODMAN_STATE"]).read_text())
    assert podman["machines"][0] == {"Name": "personal", "Running": False}
    assert podman["connections"][0] == {
        "Name": "personal",
        "URI": "ssh://personal",
        "Identity": "/personal/key",
        "Default": True,
    }
    assert not any(machine["Running"] for machine in podman["machines"])
    second = command.run("setup")
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["created"] is False
    assert (command.state / "host-before.json").read_bytes() == initial
    latest = json.loads((command.state / "host-latest.json").read_text())
    assert latest["cask_names"] == inventory["cask_names"]
    assert latest["cask_inventory_warning"] == inventory["cask_inventory_warning"]


@pytest.mark.parametrize("query", ["JSON", "NAMES"])
def test_setup_aborts_before_mutation_if_cask_fallback_fails(
    incomplete_cask_setup, query
):
    command = incomplete_cask_setup
    command.env["FAKE_CASK_FAIL"] = query
    paths = [
        Path(command.env[key]) for key in ("FAKE_BREW_INSTALLED", "FAKE_PODMAN_STATE")
    ]
    before = [path.read_bytes() for path in paths]
    result = command.run("setup")
    assert result.returncode == 1
    assert "Cask 'discord' is not installed" in result.stderr
    assert f"Cask {query} query failed" in result.stderr
    assert [path.read_bytes() for path in paths] == before
    for name in (
        "host-before.json",
        "host-additions.json",
        "install-preview.json",
        "owner.json",
    ):
        assert not (command.state / name).exists()


@pytest.mark.parametrize(
    ("query", "output"),
    [
        ("JSON", "not JSON"),
        ("JSON", "[]"),
        ("JSON", "{}"),
        ("JSON", '{"casks": {}}'),
        ("JSON", '{"casks": ["healthy"]}'),
        ("JSON", '{"casks": [{"installed": "2.0"}]}'),
        ("JSON", '{"casks": [{"token": "healthy"}]}'),
        ("JSON", '{"casks": [{"token": "healthy", "installed": null}]}'),
        ("JSON", '{"casks": [{"token": "healthy", "installed": ["2.0"]}]}'),
        ("JSON", '{"casks": [{"token": "healthy", "installed": ""}]}'),
        ("JSON", '{"casks": [{"token": "healthy", "installed": "2.0\\n3.0"}]}'),
        ("JSON", '{"casks": [{"token": "unknown", "installed": "2.0"}]}'),
        (
            "JSON",
            '{"casks": [{"token": "healthy", "installed": "2.0"}, {"token": "healthy", "installed": "3.0"}]}',
        ),
        ("NAMES", ""),
        ("NAMES", "discord healthy\n"),
        ("NAMES", "discord\nhealthy\nhealthy\n"),
        ("NAMES", "discord\nhealthy\n\x1b\n"),
    ],
)
def test_setup_rejects_unusable_cask_fallback(incomplete_cask_setup, query, output):
    command = incomplete_cask_setup
    command.env["FAKE_CASK_" + query] = output
    paths = [
        Path(command.env[key]) for key in ("FAKE_BREW_INSTALLED", "FAKE_PODMAN_STATE")
    ]
    before = [path.read_bytes() for path in paths]
    result = command.run("setup")
    assert result.returncode == 1, result.stderr
    assert "Unusable Homebrew cask inventory" in result.stderr
    assert [path.read_bytes() for path in paths] == before
    for name in (
        "host-before.json",
        "host-additions.json",
        "install-preview.json",
        "owner.json",
    ):
        assert not (command.state / name).exists()


@pytest.mark.parametrize(
    "preview",
    [
        "Would upgrade shared 1.0 -> 2.0",
        "Would reinstall shared 1.0",
        "Would install:\nshared 2.0\npodman 6.2.0",
    ],
)
def test_cask_fallback_does_not_bypass_installation_approval(
    incomplete_cask_setup, preview
):
    command = incomplete_cask_setup
    command.env["FAKE_INSTALL_PREVIEW"] = preview
    paths = [
        Path(command.env[key]) for key in ("FAKE_BREW_INSTALLED", "FAKE_PODMAN_STATE")
    ]
    before = [path.read_bytes() for path in paths]
    result = command.run("setup")
    assert result.returncode == 1
    assert "approval" in result.stderr
    assert [path.read_bytes() for path in paths] == before
    assert (command.state / "host-before.json").exists()
    assert not (command.state / "host-additions.json").exists()
    assert not (command.state / "owner.json").exists()


@pytest.mark.parametrize("names", ["", "discord\nhealthy\n"])
def test_cask_fallback_accepts_empty_version_inventory(incomplete_cask_setup, names):
    command = incomplete_cask_setup
    command.env["FAKE_CASK_JSON"] = '{"casks": []}'
    command.env["FAKE_CASK_NAMES"] = names
    result = command.run("setup")
    assert result.returncode == 0, result.stderr
    inventory = json.loads((command.state / "host-before.json").read_text())
    assert inventory["casks"] == ""
    assert inventory["cask_names"] == names
    assert "Cask 'discord' is not installed" in inventory["cask_inventory_warning"]


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


@pytest.mark.parametrize("namespace", ["rootless", "rootful"])
def test_stop_preserves_unrelated_running_containers(command, namespace):
    workload = '[{"Names":["personal-workload"]}]'
    command.machine(
        running=True,
        rootful=workload if namespace == "rootful" else "[]",
        extra=f"""elif '--connection' in args:
 if args[2] == 'ps': print({(workload if namespace == "rootless" else "[]")!r})
 else: raise SystemExit('UNEXPECTED MUTATION')""",
    )
    result = command.run("stop")
    assert result.returncode != 0
    assert "VM left running" in result.stderr
    assert "UNEXPECTED MUTATION" not in result.stderr


@pytest.mark.parametrize("rootful", [None, "not-json", "{}", "null"])
def test_stop_refuses_failed_rootful_inspection_before_mutations(command, rootful):
    command.machine(
        running=True,
        rootful=rootful,
        extra="""elif args == ['--connection', 'rss-aggregator', 'ps', '--format', 'json']: print('[]')""",
    )
    result = command.run("stop")
    assert result.returncode != 0
    assert "UNEXPECTED MUTATION" not in result.stderr


def test_stop_refuses_failed_rootless_inspection_before_mutations(command):
    command.machine(
        running=True,
        extra="""elif '--connection' in args:
 if args[2] == 'ps': raise SystemExit('rootless inspection failed')
 else: raise SystemExit('UNEXPECTED MUTATION')""",
    )
    result = command.run("stop")
    assert result.returncode != 0
    assert "rootless inspection failed" in result.stderr
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
