"""Private-access behavior at project-command and callable runtime boundaries."""

import copy
import io
import json
import socket
import subprocess
from http.client import BadStatusLine
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from unittest.mock import MagicMock
from urllib.error import HTTPError, URLError
from urllib.request import OpenerDirector

import pytest
from click.testing import CliRunner
from test_commands import CommandFixture
from test_runtime_recovery import ROOT, Engine

from rss_aggregator.cli import main
from rss_aggregator.runtime import Runtime, RuntimeFailure, ServeConfigRejected
from rss_aggregator.serve_config import ServeConfigClient


@pytest.fixture
def private_command(tmp_path):
    command = CommandFixture(tmp_path)
    command.machine(running=True)
    command.env["RSS_TAILSCALE"] = "1"
    config = {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {
            "reader.example.ts.net:443": {
                "Handlers": {"/": {"Proxy": "http://127.0.0.1:9000"}}
            }
        },
    }

    class LocalAPI(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/localapi/v0/serve-config"
            self.send_response(200)
            self.send_header("Etag", "fixture-unowned")
            self.end_headers()
            self.wfile.write(json.dumps(config).encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), LocalAPI)
    thread = Thread(target=server.serve_forever)
    thread.start()
    command.env["FIXTURE_LOCALAPI_PORT"] = str(server.server_port)
    command.tool(
        "tailscale",
        """import json, os, sys
args = sys.argv[1:]
if args == ['status', '--json']:
 print(json.dumps({'BackendState':'Running','Self':{'DNSName':'reader.example.ts.net.'},'CertDomains':['reader.example.ts.net']}))
elif args == ['debug', 'local-creds']:
 print('curl -u:fixture http://localhost:' + os.environ['FIXTURE_LOCALAPI_PORT'] + '/localapi/v0/status')
else: raise SystemExit('UNEXPECTED TAILSCALE MUTATION')
""",
    )
    try:
        yield command
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_start_refuses_unowned_serve_listener_before_changing_application(
    private_command,
):
    result = private_command.run("start")
    assert result.returncode == 1
    assert "Serve HTTPS port 443 is not exclusively project-owned" in result.stderr
    assert "UNEXPECTED" not in result.stderr
    assert not (private_command.state / "serve.json").exists()


class PrivateEngine(Engine):
    def __init__(self, runtime, port):
        super().__init__(runtime, port)
        self.machine = {"Name": "rss-aggregator", "State": "running"}
        self.connection = {
            "Name": "rss-aggregator",
            "URI": "fixture",
            "Identity": "fixture",
        }
        self.config = {
            "TCP": {"8443": {"HTTPS": True}},
            "Web": {
                "other.example.ts.net:8443": {"Handlers": {"/": {"Text": "unrelated"}}}
            },
        }
        self.original = copy.deepcopy(self.config)
        self.tailscale_calls = []
        self.installed = True
        self.fail_serve = False
        self.before_mutation = None
        self.fail_security = False
        self.reconfigured = []
        self.backend = "Running"
        self.cert_domains = ["reader.example.ts.net"]
        self.policy_ports = [{"First": 443, "Last": 443}]
        self.settings = {
            "api_enabled": False,
            "auth_type": "form",
            "allow_anonymous": False,
            "allow_anonymous_refresh": False,
            "base_url": "http://127.0.0.1:" + str(port),
        }
        self.settings_override = {}

    def read(self):
        return copy.deepcopy(self.config), json.dumps(self.config, sort_keys=True)

    def write(self, config, etag):
        if self.before_mutation:
            self.before_mutation()
        if etag != json.dumps(self.config, sort_keys=True):
            raise ServeConfigRejected(
                "Serve configuration changed concurrently; no change applied."
            )
        assert self.published and not self.probes
        if self.fail_serve:
            raise RuntimeFailure(
                "Project Serve change failed; inspect authorization locally."
            )
        self.config = copy.deepcopy(config)

    def run(self, *args, check=True, input=None):
        if args[0] == "tailscale":
            self.tailscale_calls.append(args[1:])
            tail = args[1:]
            status = 0
            if tail == ("status", "--json"):
                output = {
                    "BackendState": self.backend,
                    "Self": {"DNSName": "reader.example.ts.net."},
                    "CertDomains": self.cert_domains,
                }
            elif tail == ("serve", "status", "--json"):
                output = self.config
            elif tail == ("debug", "netmap"):
                output = {
                    "PacketFilter": [
                        {
                            "IPProto": [6],
                            "Srcs": ["fixture-reader"],
                            "Dsts": [{"Ports": port} for port in self.policy_ports],
                        }
                    ]
                }
            else:
                raise AssertionError(f"Unexpected Tailscale command: {tail}")
            return subprocess.CompletedProcess(args, status, json.dumps(output), "")
        if args == ("podman", "machine", "inspect", "rss-aggregator"):
            return subprocess.CompletedProcess(args, 0, json.dumps([self.machine]), "")
        if args == ("podman", "machine", "list", "--format", "json"):
            return subprocess.CompletedProcess(args, 0, "[]", "")
        if args == ("podman", "system", "connection", "list", "--format", "json"):
            return subprocess.CompletedProcess(
                args, 0, json.dumps([self.connection]), ""
            )
        if args == (
            "podman",
            "machine",
            "start",
            "--update-connection=false",
            "rss-aggregator",
        ):
            self.machine["State"] = "running"
            return subprocess.CompletedProcess(args, 0, "", "")
        if args == ("podman", "machine", "stop", "rss-aggregator"):
            self.machine["State"] = "stopped"
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:4] == ("podman", "--connection", "rss-aggregator", "exec"):
            if "applied_migrations.txt" in args[-1]:
                return subprocess.CompletedProcess(
                    args, 0 if self.installed else 1, "", ""
                )
            if "cli/reconfigure.php" in args:
                self.reconfigured.append(args)
                # PHP getopt optional values must use '='; an unconsumed
                # positional 'false' ends parsing, even with later flags present.
                flags = iter(args[args.index("cli/reconfigure.php") + 1 :])
                for flag in flags:
                    if not flag.startswith("--"):
                        break
                    key, sep, value = flag[2:].partition("=")
                    if key in ("base-url", "auth-type"):
                        self.settings[key.replace("-", "_")] = (
                            value if sep else next(flags)
                        )
                    else:
                        self.settings[key.replace("-", "_")] = (
                            value == "true" if sep else True
                        )
                return subprocess.CompletedProcess(args, 0, "", "")
            if (
                "FreshRSS_Context::systemConf()" in args[-1]
                and "api_enabled" in args[-1]
            ):
                return subprocess.CompletedProcess(
                    args, 0, json.dumps({**self.settings, **self.settings_override}), ""
                )
            if args[-1] == "-l":
                return subprocess.CompletedProcess(
                    args, 0, "*/20 * * * * php app/actualize_script.php", ""
                )
            if "crond.pid" in args[-1] or args[-1] == "app/actualize_script.php":
                return subprocess.CompletedProcess(args, 0, "", "")
            if self.fail_security and 'file_get_contents("http:' in args[-1]:
                return subprocess.CompletedProcess(
                    args,
                    0,
                    json.dumps(
                        {
                            "php": "8.4.27",
                            "sapi": "apache2handler",
                            "upload_progress": "1",
                        }
                    ),
                    "",
                )
        return super().run(*args, check=check, input=input)


@pytest.fixture
def private_runtime(tmp_path, monkeypatch):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    runtime = Runtime(root=ROOT, state_dir=tmp_path)
    engine = PrivateEngine(runtime, port)
    engine.run("podman", "--connection", "rss-aggregator", "create")
    engine.app["State"].update(Running=True, Status="running")
    (tmp_path / "owner.json").write_text(
        json.dumps(
            {
                "token": "test-owner",
                "machine": engine.machine,
                "connection": engine.connection,
            }
        )
    )
    response = MagicMock()
    response.__enter__.return_value.status = 200
    monkeypatch.setattr(
        "rss_aggregator.runtime.urlopen", MagicMock(return_value=response)
    )
    monkeypatch.setattr(runtime, "_run", engine.run)
    monkeypatch.setattr(
        "rss_aggregator.private_access.ServeConfigClient", lambda runtime: engine
    )
    monkeypatch.setenv("RSS_PORT", str(port))
    monkeypatch.setenv("RSS_TAILSCALE", "1")
    return runtime, engine


@pytest.mark.parametrize("funnel", [False, True])
@pytest.mark.parametrize("installed", [False, True])
@pytest.mark.parametrize("machine_state", ["running", "stopped"])
def test_foreground_443_conflict_refused_before_application_changes(
    private_runtime, funnel, installed, machine_state
):
    runtime, engine = private_runtime
    engine.installed = installed
    engine.machine["State"] = machine_state
    engine.app["State"]["Running"] = machine_state == "running"
    engine.config["Foreground"] = {
        "other-session": {
            "TCP": {"443": {"HTTPS": True}},
            "Web": {
                "reader.example.ts.net:443": {
                    "Handlers": {"/": {"Proxy": f"http://127.0.0.1:{engine.port}"}}
                }
            },
            "AllowFunnel": {"reader.example.ts.net:443": funnel},
        }
    }
    before = copy.deepcopy(engine.config)
    app_before = copy.deepcopy(engine.app)
    with pytest.raises(RuntimeFailure, match="project-owned"):
        runtime.run("start")
    assert engine.config == before
    assert engine.machine["State"] == machine_state
    assert engine.app == app_before
    assert not engine.reconfigured
    assert not engine.probes
    assert not engine.published
    assert not (runtime.state_dir / "serve.json").exists()


@pytest.mark.parametrize("foreground", [False, True])
@pytest.mark.parametrize("installed", [False, True])
@pytest.mark.parametrize("machine_state", ["running", "stopped"])
def test_non443_funnel_to_backend_refused_before_application_changes(
    private_runtime, foreground, installed, machine_state
):
    runtime, engine = private_runtime
    engine.installed = installed
    engine.machine["State"] = machine_state
    engine.app["State"]["Running"] = machine_state == "running"
    public = {
        "TCP": {"8443": {"HTTPS": True}},
        "Web": {
            "reader.example.ts.net:8443": {
                "Handlers": {"/": {"Proxy": f"http://127.0.0.1:{engine.port}"}}
            }
        },
        "AllowFunnel": {"reader.example.ts.net:8443": True},
    }
    if foreground:
        engine.config["Foreground"] = {"other-session": public}
    else:
        engine.config.update(public)
    before = copy.deepcopy(engine.config)
    app_before = copy.deepcopy(engine.app)
    with pytest.raises(RuntimeFailure, match="Funnel.*backend"):
        runtime.run("start")
    assert engine.config == before
    assert engine.machine["State"] == machine_state
    assert engine.app == app_before
    assert not engine.reconfigured
    assert not engine.probes
    assert not engine.published
    assert not (runtime.state_dir / "serve.json").exists()


@pytest.mark.parametrize("variant", ["cross_scope", "port_only"])
@pytest.mark.parametrize("installed", [False, True])
@pytest.mark.parametrize("machine_state", ["running", "stopped"])
def test_supported_public_routes_refused_before_backend_changes(
    private_runtime, variant, installed, machine_state
):
    runtime, engine = private_runtime
    engine.installed = installed
    engine.machine["State"] = machine_state
    engine.app["State"]["Running"] = machine_state == "running"
    address = "reader.example.ts.net:8443"
    handler = {
        "TCP": {"8443": {"HTTPS": True}},
        "Web": {address: {"Handlers": {"/": {"Proxy": str(engine.port)}}}},
    }
    engine.config = {"AllowFunnel": {address: True}}
    if variant == "cross_scope":
        handler["Web"][address]["Handlers"]["/"]["Proxy"] = (
            f"http://127.0.0.1:{engine.port}"
        )
        engine.config["Foreground"] = {"other-session": handler}
    else:
        engine.config.update(handler)
    before = copy.deepcopy(engine.config)
    app_before = copy.deepcopy(engine.app)
    with pytest.raises(RuntimeFailure, match="Funnel.*backend"):
        runtime.run("start")
    assert engine.config == before
    assert engine.app == app_before
    assert engine.machine["State"] == machine_state
    assert not engine.published
    assert not engine.probes
    assert not engine.reconfigured
    assert not (runtime.state_dir / "serve.json").exists()


@pytest.mark.parametrize(
    "tcp,target",
    [
        (False, "http://127.0.0.1:{port}/subpath"),
        (False, "http://localhost:{port}"),
        (False, "127.0.0.1:{port}"),
        (False, "localhost:{port}"),
        (False, "{port}"),
        (False, ":{port}"),
        (False, "http://:{port}"),
        (False, "https+insecure://[::1]:{port}/subpath"),
        (False, "http://[::ffff:127.0.0.1]:{port}"),
        (False, "http://[::ffff:7f00:1]:{port}/subpath"),
        (False, "http://0.0.0.0:{port}"),
        (True, "127.0.0.1:{port}"),
        (True, ":{port}"),
        (True, "[::1]:{port}"),
        (True, "[::ffff:127.0.0.1]:{port}"),
        (True, "[::ffff:7f00:1]:{port}"),
        (True, "0.0.0.0:{port}"),
    ],
)
def test_public_backend_detection_checks_endpoint_not_handler_path(
    private_runtime, target, tcp
):
    runtime, engine = private_runtime
    target = target.format(port=engine.port)
    engine.config["AllowFunnel"] = {"other.example.ts.net:8443": True}
    if tcp:
        engine.config["TCP"]["8443"] = {"TCPForward": target}
    else:
        engine.config["Web"]["other.example.ts.net:8443"]["Handlers"] = {
            "/nested": {"Proxy": target}
        }
    before = copy.deepcopy(engine.config)
    with pytest.raises(RuntimeFailure, match="Funnel.*backend"):
        runtime.run("start")
    assert engine.config == before
    assert not engine.published
    assert not engine.reconfigured


@pytest.mark.parametrize("foreground", [False, True])
@pytest.mark.parametrize("funnel", [False, True])
def test_private_start_preserves_unrelated_funnel_or_private_backend_route(
    private_runtime, foreground, funnel
):
    runtime, engine = private_runtime
    # Enabled public forwarding points elsewhere; forwarding to this backend
    # on a different port is acceptable only when it remains private.
    port = (1024 if engine.port != 1024 else 1025) if funnel else engine.port
    other = {
        "TCP": {"8443": {"HTTPS": True}},
        "Web": {
            "other.example.ts.net:8443": {
                "Handlers": {"/": {"Proxy": f"http://127.0.0.1:{port}"}}
            }
        },
        "AllowFunnel": {"other.example.ts.net:8443": funnel},
    }
    if foreground:
        engine.config["Foreground"] = {"other-session": other}
    else:
        engine.config.update(other)
    before = copy.deepcopy(engine.config)
    runtime.run("start")
    engine.volumes.clear()
    runtime.run("stop")
    assert engine.config == before


@pytest.mark.parametrize("tcp_forward", [False, True])
def test_funnel_authorization_and_handlers_can_span_foreground_sessions(
    private_runtime, tcp_forward
):
    runtime, engine = private_runtime
    address = "reader.example.ts.net:8443"
    engine.config = {
        "AllowFunnel": {address: False},
        "Foreground": {
            "authorization": {"AllowFunnel": {address: True}},
            "listener": {"TCP": {"8443": {"HTTPS": True}}},
            "handler": {
                "Web": {address: {"Handlers": {"/": {"Proxy": str(engine.port)}}}}
            },
        },
    }
    if tcp_forward:
        engine.config["Foreground"]["listener"]["TCP"]["8443"] = {
            "TCPForward": f"127.0.0.1:{engine.port}"
        }
    before = copy.deepcopy(engine.config)
    with pytest.raises(RuntimeFailure, match="Funnel.*backend"):
        runtime.run("start")
    assert engine.config == before
    assert not engine.published
    assert not engine.reconfigured


@pytest.mark.parametrize("separation", ["hostname", "service", "foreground_shadow"])
def test_public_scanner_preserves_independent_or_shadowed_handlers(
    private_runtime, separation
):
    runtime, engine = private_runtime
    address = "other.example.ts.net:8443"
    backend_handler = {"Handlers": {"/": {"Proxy": str(engine.port)}}}
    engine.config["AllowFunnel"] = {address: True}
    if separation == "hostname":
        engine.config["Web"]["different.example.ts.net:8443"] = backend_handler
    elif separation == "service":
        engine.config["Services"] = {
            "svc:other": {
                "TCP": {"8443": {"HTTPS": True}},
                "Web": {address: backend_handler},
            }
        }
    else:
        engine.config["Web"][address] = backend_handler
        engine.config["Foreground"] = {
            "other-session": {
                "Web": {address: {"Handlers": {"/": {"Text": "unrelated"}}}}
            }
        }
    before = copy.deepcopy(engine.config)
    runtime.run("start")
    engine.volumes.clear()
    runtime.run("stop")
    assert engine.config == before


@pytest.mark.parametrize("snapshot_number", [1, 2])
def test_funnel_added_before_final_snapshot_is_also_refused(
    private_runtime, monkeypatch, snapshot_number
):
    runtime, engine = private_runtime
    original_read = engine.read
    snapshots = []

    def read():
        if len(snapshots) == snapshot_number:
            engine.config["AllowFunnel"] = {"other.example.ts.net:8443": True}
            engine.config["Web"]["other.example.ts.net:8443"]["Handlers"]["/"] = {
                "Proxy": f"http://127.0.0.1:{engine.port}"
            }
        snapshot = original_read()
        snapshots.append(snapshot)
        return snapshot

    monkeypatch.setattr(engine, "read", read)
    with pytest.raises(RuntimeFailure, match="Funnel.*backend"):
        runtime.run("start")
    assert len(snapshots) == snapshot_number + 1
    assert engine.config == snapshots[-1][0]
    assert "443" not in engine.config["TCP"]
    assert not (runtime.state_dir / "serve.json").exists()
    assert not engine.app["State"]["Running"]
    assert (
        "podman",
        "--connection",
        "rss-aggregator",
        "stop",
        "--time",
        "60",
        "rss-aggregator-freshrss",
    ) in engine.calls


@pytest.mark.parametrize("failure", [OSError, ValueError])
def test_final_private_start_persistence_error_closes_backend(
    private_runtime, monkeypatch, failure
):
    runtime, engine = private_runtime
    original_save = runtime._save

    def save(name, value):
        if name == "serve.json":
            # A client exposes the backend just before intent persistence fails.
            engine.config["AllowFunnel"] = {"other.example.ts.net:8443": True}
            engine.config["Web"]["other.example.ts.net:8443"]["Handlers"]["/"] = {
                "Proxy": str(engine.port)
            }
            raise failure("Fixture intent persistence failed")
        return original_save(name, value)

    monkeypatch.setattr(runtime, "_save", save)
    with pytest.raises(failure, match="intent persistence failed"):
        runtime.run("start")
    assert not engine.app["State"]["Running"]
    assert engine.config["AllowFunnel"]["other.example.ts.net:8443"]
    assert "443" not in engine.config["TCP"]
    assert not (runtime.state_dir / "serve.json").exists()


@pytest.mark.parametrize("cleanup_failure", ["stop", "stop_oserror", "ownership"])
def test_late_public_conflict_reports_unconfirmed_backend_shutdown(
    private_runtime, monkeypatch, cleanup_failure
):
    runtime, engine = private_runtime
    original_read = engine.read
    snapshots = []

    def read():
        if len(snapshots) == 2:
            engine.config["AllowFunnel"] = {"other.example.ts.net:8443": True}
            engine.config["Web"]["other.example.ts.net:8443"]["Handlers"]["/"] = {
                "Proxy": str(engine.port)
            }
            if cleanup_failure == "ownership":
                engine.app["Config"]["Labels"]["io.rss-aggregator.owner"] = (
                    "foreign-owner"
                )
        snapshot = original_read()
        snapshots.append(snapshot)
        return snapshot

    def run(*args, **kwargs):
        if (
            cleanup_failure in ("stop", "stop_oserror")
            and len(snapshots) == 3
            and args[3:4] == ("stop",)
        ):
            error = OSError if cleanup_failure == "stop_oserror" else RuntimeFailure
            raise error("Injected private engine diagnostic")
        return engine.run(*args, **kwargs)

    monkeypatch.setattr(engine, "read", read)
    monkeypatch.setattr(runtime, "_run", run)
    with pytest.raises(RuntimeFailure, match="could not be confirmed") as error:
        runtime.run("start")
    assert "may still be exposed" in str(error.value)
    assert "private engine diagnostic" not in str(error.value)
    assert engine.app["State"]["Running"]
    assert engine.config == snapshots[-1][0]
    assert "443" not in engine.config["TCP"]
    assert not (runtime.state_dir / "serve.json").exists()


@pytest.mark.parametrize("rejected_status", [401, 403, 412])
@pytest.mark.parametrize("prior_phase", [None, "inactive"])
def test_rejected_start_does_not_authorize_identical_foreign_route(
    private_runtime, monkeypatch, prior_phase, rejected_status
):
    runtime, engine = private_runtime
    record_path = runtime.state_dir / "serve.json"
    previous = {
        "owner": "test-owner",
        "host": "reader.example.ts.net",
        "target": f"http://127.0.0.1:{engine.port}",
        "phase": prior_phase,
    }
    if prior_phase:
        record_path.write_text(json.dumps(previous))
    discovery = MagicMock()
    discovery._run.return_value = subprocess.CompletedProcess(
        [], 0, "curl -u:fixture http://localhost:1234/localapi/v0/status\n", ""
    )
    client = ServeConfigClient(discovery)
    statuses = []
    competing = []

    def respond(method, body=None, etag=None):
        assert method == "POST"
        if not statuses:
            # A concurrent winner can cause 412; after an authorization refusal,
            # an authorized client can independently create that identical route.
            competing.append(json.loads(body))
            if rejected_status == 412:
                engine.config = copy.deepcopy(competing[0])
            statuses.append(rejected_status)
            return rejected_status, [], b""
        engine.config = json.loads(body)
        statuses.append(200)
        return 200, [], b""

    monkeypatch.setattr(client, "_request", respond)
    monkeypatch.setattr(
        engine, "read", lambda: (copy.deepcopy(engine.config), '"fixture-snapshot"')
    )
    monkeypatch.setattr(engine, "write", client.write)
    with pytest.raises(RuntimeFailure, match="concurrent|authorization|unknown"):
        runtime.run("start")
    after_rejection = (
        json.loads(record_path.read_text()) if record_path.exists() else None
    )
    if rejected_status != 412:
        assert engine.config == engine.original
        engine.config = copy.deepcopy(competing[0])
    try:
        forwarding = runtime.run("status")["forwarding"]["status"]
    except RuntimeFailure:
        forwarding = "refused"
    engine.volumes.clear()
    try:
        runtime.run("stop")
    except RuntimeFailure as error:
        assert "project-owned" in str(error)
    assert statuses == [rejected_status]
    assert engine.config == competing[0]
    assert forwarding != "running"
    assert after_rejection == (previous if prior_phase else None)


@pytest.mark.parametrize("verification", ["read", "unrelated", "recreated"])
def test_confirmed_removal_retires_authority_before_verification(
    private_runtime, monkeypatch, verification
):
    runtime, engine = private_runtime
    runtime.run("start")
    owned_config = copy.deepcopy(engine.config)
    record_path = runtime.state_dir / "serve.json"
    original_read, original_write = engine.read, engine.write
    reads, writes = [], []

    def read():
        reads.append(True)
        if len(reads) == 2:
            if verification == "read":
                raise RuntimeFailure("Verification GET failed; inspect locally.")
            if verification == "unrelated":
                engine.config["Web"]["other.example.ts.net:8443"]["Handlers"]["/"] = {
                    "Text": "concurrent unrelated change"
                }
            else:
                engine.config = copy.deepcopy(owned_config)
        return original_read()

    def write(config, etag):
        original_write(config, etag)
        writes.append(copy.deepcopy(config))

    monkeypatch.setattr(engine, "read", read)
    monkeypatch.setattr(engine, "write", write)
    engine.volumes.clear()
    with pytest.raises(
        RuntimeFailure, match="GET failed|changed concurrently|did not stop"
    ):
        runtime.run("stop")
    record_after_failure = json.loads(record_path.read_text())
    assert "443" not in writes[0]["TCP"]
    # A foreign client recreates the exact old listener after confirmed removal.
    engine.config = copy.deepcopy(owned_config)
    try:
        forwarding = runtime.run("status")["forwarding"]["status"]
    except RuntimeFailure:
        forwarding = "refused"
    try:
        runtime.run("stop")
    except RuntimeFailure as error:
        assert "project-owned" in str(error)
    assert len(writes) == 1
    assert engine.config == owned_config
    assert forwarding != "running"
    assert record_after_failure["phase"] == "inactive"


def test_retirement_persistence_failure_prevents_removal_request(
    private_runtime, monkeypatch
):
    runtime, engine = private_runtime
    runtime.run("start")
    before = copy.deepcopy(engine.config)
    record_path = runtime.state_dir / "serve.json"
    record_before = json.loads(record_path.read_text())
    original_save, original_write = runtime._save, engine.write
    writes = []

    def save(name, value):
        if name == "serve.json" and value["phase"] == "inactive":
            raise OSError("Fixture retirement persistence failed")
        return original_save(name, value)

    def write(config, etag):
        writes.append(copy.deepcopy(config))
        original_write(config, etag)

    monkeypatch.setattr(runtime, "_save", save)
    monkeypatch.setattr(engine, "write", write)
    engine.volumes.clear()
    with pytest.raises(OSError, match="retirement persistence failed"):
        runtime.run("stop")
    assert not writes
    assert engine.config == before
    assert json.loads(record_path.read_text()) == record_before
    monkeypatch.setattr(runtime, "_save", original_save)
    runtime.run("stop")
    assert len(writes) == 1
    assert json.loads(record_path.read_text())["phase"] == "inactive"


@pytest.mark.parametrize(
    "rejected,applied", [(False, False), (False, True), (True, False)]
)
def test_removal_failure_preserves_intent_without_new_deletion_authority(
    private_runtime, monkeypatch, rejected, applied
):
    runtime, engine = private_runtime
    runtime.run("start")
    before = copy.deepcopy(engine.config)
    record_path = runtime.state_dir / "serve.json"
    record_before = json.loads(record_path.read_text())
    original_write = engine.write
    writes = []

    def write(config, etag):
        writes.append(copy.deepcopy(config))
        if rejected:
            raise ServeConfigRejected("Fixture definite rejection")
        if applied:
            original_write(config, etag)
        raise RuntimeFailure("Fixture unknown removal outcome")

    monkeypatch.setattr(engine, "write", write)
    engine.volumes.clear()
    with pytest.raises(RuntimeFailure, match="rejection|unknown removal"):
        runtime.run("stop")
    record_after_failure = json.loads(record_path.read_text())
    if rejected:
        assert record_after_failure == record_before
        assert engine.config == before
    else:
        assert record_after_failure == {**record_before, "phase": "inactive"}
        # Whether removal applied or not, an identical route might now be foreign.
        engine.config = copy.deepcopy(before)
        with pytest.raises(RuntimeFailure, match="project-owned"):
            runtime.run("status")
        with pytest.raises(RuntimeFailure, match="project-owned"):
            runtime.run("stop")
        assert len(writes) == 1
        assert engine.config == before


@pytest.mark.parametrize("applied", [False, True])
def test_uncertain_start_retains_pending_recovery_intent(
    private_runtime, monkeypatch, applied
):
    runtime, engine = private_runtime
    original_write = engine.write

    def uncertain_write(config, etag):
        if applied:
            original_write(config, etag)
        raise RuntimeFailure("Write outcome may be unknown; inspect locally.")

    monkeypatch.setattr(engine, "write", uncertain_write)
    with pytest.raises(RuntimeFailure, match="unknown"):
        runtime.run("start")
    record = json.loads((runtime.state_dir / "serve.json").read_text())
    assert record["phase"] == "pending"
    assert not engine.app["State"]["Running"]
    assert runtime.run("status")["forwarding"]["status"] == (
        "running" if applied else "stopped"
    )
    monkeypatch.setattr(engine, "write", original_write)
    engine.volumes.clear()
    runtime.run("stop")
    assert engine.config == engine.original


def test_private_start_preserves_unrelated_foreground_and_virtual_services(
    private_runtime,
):
    runtime, engine = private_runtime
    engine.config["Foreground"] = {
        "other-session": {
            "TCP": {"8443": {"HTTPS": True}},
            "Web": {
                "reader.example.ts.net:8443": {"Handlers": {"/": {"Text": "other"}}}
            },
            "Services": {
                "svc:other": {
                    "TCP": {"443": {"HTTPS": True}},
                    "Web": {
                        "other.example.ts.net:443": {
                            "Handlers": {"/": {"Text": "service"}}
                        }
                    },
                }
            },
        }
    }
    engine.config["Services"] = copy.deepcopy(
        engine.config["Foreground"]["other-session"]["Services"]
    )
    before = copy.deepcopy(engine.config)
    runtime.run("start")
    engine.volumes.clear()
    runtime.run("stop")
    assert engine.config == before


def test_private_start_effectively_enables_api_and_disables_anonymous_access(
    private_runtime,
):
    runtime, engine = private_runtime
    runtime.run("start")
    assert engine.settings == {
        "api_enabled": True,
        "auth_type": "form",
        "allow_anonymous": False,
        "allow_anonymous_refresh": False,
        "base_url": "https://reader.example.ts.net",
    }


@pytest.mark.parametrize(
    "key,value",
    [
        ("api_enabled", False),
        ("auth_type", "none"),
        ("allow_anonymous", True),
        ("allow_anonymous_refresh", True),
        ("base_url", "http://127.0.0.1:8080"),
    ],
)
def test_private_start_refuses_ineffective_configuration_before_forwarding(
    private_runtime, key, value
):
    runtime, engine = private_runtime
    engine.settings_override = {key: value}
    with pytest.raises(RuntimeFailure, match="effective HTTPS/API configuration"):
        runtime.run("start")
    assert engine.config == engine.original


def test_repeat_start_and_stop_manage_only_project_forwarding(private_runtime):
    runtime, engine = private_runtime
    for _ in range(2):
        result = runtime.run("start")
        assert result["url"] == "https://reader.example.ts.net"
        assert result["forwarding"]["status"] == "running"
        assert "--api-enabled=true" in engine.reconfigured[-1]
        assert "--auth-type" in engine.reconfigured[-1]
        assert (
            engine.config["Web"]["other.example.ts.net:8443"]
            == engine.original["Web"]["other.example.ts.net:8443"]
        )
    # No source data: stop can test forwarding lifecycle without a synthetic archive.
    engine.volumes.clear()
    for _ in range(2):
        assert runtime.run("stop")["machine"] == "stopped"
        assert engine.config == engine.original


def test_replace_rejects_invalid_private_setting_before_any_mutation(
    private_runtime, monkeypatch
):
    runtime, engine = private_runtime
    runtime.run("start")
    before = copy.deepcopy(engine.config)
    monkeypatch.setenv("RSS_TAILSCALE", "invalid")
    with pytest.raises(RuntimeFailure, match="RSS_TAILSCALE"):
        runtime.run("replace")
    assert engine.config == before
    assert engine.app["State"]["Running"]


def test_reserved_live_fixture_record_cannot_touch_real_forwarding_record(
    private_runtime, monkeypatch
):
    runtime, _engine = private_runtime
    actual = runtime.state_dir / "serve.json"
    actual.write_text('{"owner":"real-project-sentinel"}')
    monkeypatch.setattr(
        "rss_aggregator.private_access.SERVE_RECORD",
        "test-1234-serve.json",
        raising=False,
    )
    runtime.run("start")
    assert actual.read_text() == '{"owner":"real-project-sentinel"}'
    assert (
        json.loads((runtime.state_dir / "test-1234-serve.json").read_text())["phase"]
        == "active"
    )


def test_missing_tcp443_policy_fails_before_publishing_or_configuring_api(
    private_runtime,
):
    runtime, engine = private_runtime
    engine.policy_ports = [{"First": 22, "Last": 22}]
    with pytest.raises(RuntimeFailure, match="TCP 443 grant"):
        runtime.run("start")
    assert engine.config == engine.original
    assert not engine.reconfigured


def test_missing_certificate_authorization_fails_without_account_changes(
    private_runtime,
):
    runtime, engine = private_runtime
    engine.cert_domains = []
    with pytest.raises(RuntimeFailure, match="HTTPS certificates are not enabled"):
        runtime.run("start")
    assert engine.config == engine.original
    assert not engine.reconfigured


@pytest.mark.parametrize("failure", ["security", "serve", "installer", "disconnected"])
def test_failed_private_start_does_not_expose_application(private_runtime, failure):
    runtime, engine = private_runtime
    if failure == "security":
        runtime.run("start")
        engine.fail_security = True
    elif failure == "serve":
        engine.fail_serve = True
    elif failure == "installer":
        engine.installed = False
    else:
        engine.backend = "Stopped"
    with pytest.raises(RuntimeFailure) as error:
        runtime.run("start")
    assert "private-admin-url" not in str(error.value)
    assert "secret" not in str(error.value)
    assert engine.config == engine.original
    assert not engine.probes


@pytest.mark.parametrize(
    "conflict", ["handler", "target", "funnel", "owner", "foreground"]
)
def test_stop_refuses_changed_ownership_without_removing_routes(
    private_runtime, conflict
):
    runtime, engine = private_runtime
    runtime.run("start")
    if conflict == "handler":
        engine.config["Web"]["reader.example.ts.net:443"]["Handlers"]["/other"] = {
            "Text": "another project"
        }
    elif conflict == "target":
        engine.config["Web"]["reader.example.ts.net:443"]["Handlers"]["/"]["Proxy"] = (
            "http://127.0.0.1:9000"
        )
    elif conflict == "funnel":
        engine.config["AllowFunnel"] = {"reader.example.ts.net:443": True}
    elif conflict == "foreground":
        engine.config["Foreground"] = {
            "other-session": {"TCP": {"443": {"HTTPS": True}}}
        }
    else:
        path = runtime.state_dir / "serve.json"
        record = json.loads(path.read_text())
        record["owner"] = "another-project"
        path.write_text(json.dumps(record))
    before = copy.deepcopy(engine.config)
    with pytest.raises(RuntimeFailure, match="ownership|project-owned"):
        runtime.run("stop")
    assert engine.config == before
    assert engine.app["State"]["Running"]


@pytest.mark.parametrize("operation", ["start", "stop"])
@pytest.mark.parametrize("change", ["root", "unrelated"])
def test_concurrent_serve_change_is_not_overwritten(private_runtime, operation, change):
    runtime, engine = private_runtime
    if operation == "stop":
        runtime.run("start")
    raced = []

    def replace_configuration():
        if change == "root":
            engine.config.setdefault("TCP", {})["443"] = {"HTTPS": True}
            engine.config.setdefault("Web", {})["reader.example.ts.net:443"] = {
                "Handlers": {"/": {"Proxy": "http://127.0.0.1:9000"}}
            }
        else:
            engine.config["Web"]["other.example.ts.net:8443"]["Handlers"]["/"] = {
                "Text": "concurrent replacement"
            }
        raced.append(copy.deepcopy(engine.config))

    engine.before_mutation = replace_configuration
    with pytest.raises(RuntimeFailure, match="concurrent"):
        runtime.run(operation)
    assert len(raced) == 1
    assert engine.config == raced[0]
    assert not any("reset" in call for call in engine.tailscale_calls)


def test_backup_restores_private_forwarding_even_without_opt_in_environment(
    private_runtime, monkeypatch, tmp_path
):
    runtime, engine = private_runtime
    runtime.run("start")
    monkeypatch.setenv("RSS_TAILSCALE", "0")
    monkeypatch.setenv("RSS_BACKUP_DIR", str(tmp_path / "backups"))
    # Snapshot fails on our deliberately non-exportable engine fixture; the
    # recovery contract must still restore the prior application/forwarding state.
    with pytest.raises((RuntimeFailure, KeyError)):
        runtime.run("backup")
    assert engine.app["State"]["Running"]
    assert engine.config["Web"]["reader.example.ts.net:443"]["Handlers"]["/"][
        "Proxy"
    ].startswith("http://127.0.0.1:")


def test_api_check_returns_only_sanitized_diagnostics_and_authentication(
    private_runtime, monkeypatch
):
    runtime, _engine = private_runtime
    runtime.run("start")
    requests = []

    def respond(self, request, timeout):
        requests.append(request)
        assert request.full_url.startswith(
            "https://reader.example.ts.net/api/greader.php/"
        )
        assert "secret-password" not in request.full_url
        if request.full_url.endswith("/check/compatibility"):
            return io.BytesIO(b"PASS")
        if request.full_url.endswith("/accounts/ClientLogin"):
            assert request.get_method() == "POST"
            assert b"Passwd=secret-password" in request.data
            return io.BytesIO(b"SID=private-session\nAuth=private-token\n")
        assert request.get_header("Authorization") == "GoogleLogin auth=private-token"
        if "subscription/list" in request.full_url:
            return io.BytesIO(b'{"subscriptions":[{"id":"private-feed-url"}]}')
        return io.BytesIO(b'{"items":[{"id":"private-article-id"}]}')

    monkeypatch.setattr(OpenerDirector, "open", respond)
    result = runtime.run("api-check", username="reader", api_password="secret-password")
    assert result == {
        "diagnostics": "PASS",
        "authenticated": True,
        "subscriptions": 1,
        "sample_articles": 1,
        "transport": "https",
        "https_verified": True,
    }
    assert len(requests) == 4
    assert "private-" not in json.dumps(result)


def test_explicit_loopback_api_check_reports_no_https_verification(
    private_runtime, monkeypatch
):
    runtime, engine = private_runtime
    runtime.run("start")

    def respond(self, request, timeout):
        assert request.full_url.startswith(
            f"http://127.0.0.1:{engine.port}/api/greader.php/"
        )
        assert "secret-password" not in request.full_url
        if request.full_url.endswith("/check/compatibility"):
            return io.BytesIO(b"PASS")
        if request.full_url.endswith("/accounts/ClientLogin"):
            assert request.get_method() == "POST"
            return io.BytesIO(b"Auth=private-token\n")
        if "subscription/list" in request.full_url:
            return io.BytesIO(b'{"subscriptions":[{}]}')
        return io.BytesIO(b'{"items":[{}]}')

    monkeypatch.setattr(OpenerDirector, "open", respond)
    result = runtime.run(
        "api-check", username="reader", api_password="secret-password", loopback=True
    )
    assert result == {
        "diagnostics": "PASS",
        "authenticated": True,
        "subscriptions": 1,
        "sample_articles": 1,
        "transport": "loopback",
        "https_verified": False,
    }


@pytest.mark.parametrize(
    "bindings",
    [
        [],
        [{"HostIp": "0.0.0.0", "HostPort": "8080"}],
        [{"HostIp": "::1", "HostPort": "8080"}],
        [{"HostIp": "127.0.0.1", "HostPort": "bad"}],
        [{"HostIp": "127.0.0.1", "HostPort": "80"}],
        [
            {"HostIp": "127.0.0.1", "HostPort": "8080"},
            {"HostIp": "127.0.0.1", "HostPort": "8081"},
        ],
    ],
)
def test_loopback_authentication_refuses_unverified_publication_before_sending_credentials(
    private_runtime, monkeypatch, bindings
):
    runtime, engine = private_runtime
    runtime.run("start")
    engine.app["HostConfig"]["PortBindings"]["80/tcp"] = bindings

    def never_send(*args, **kwargs):
        raise AssertionError(
            "Credentials must not be sent to an unverified publication"
        )

    monkeypatch.setattr(OpenerDirector, "open", never_send)
    with pytest.raises(RuntimeFailure, match="loopback|Loopback"):
        runtime.run(
            "api-check",
            username="reader",
            api_password="secret-password",
            loopback=True,
        )


@pytest.mark.parametrize("flags", [[], ["--loopback"]])
def test_api_check_cli_hides_password_and_keeps_failure_stdout_empty(tmp_path, flags):
    result = CliRunner().invoke(
        main,
        ["api-check", *flags],
        input="reader\nnever-print-this\n",
        env={"RSS_STATE_DIR": str(tmp_path)},
    )
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "never-print-this" not in result.stderr
    assert "API password" in result.stderr


@pytest.mark.parametrize(
    "failure", ["invalid", "redirect", "certificate", "compatibility", "protocol"]
)
def test_api_failures_are_sanitized_and_never_return_credentials(
    private_runtime, monkeypatch, failure
):
    runtime, _engine = private_runtime
    runtime.run("start")
    requests = []

    def respond(self, request, timeout):
        requests.append(request)
        if request.full_url.endswith("/check/compatibility"):
            if failure == "compatibility":
                return io.BytesIO(b"private-invalid-response")
            if failure == "certificate":
                raise URLError("private-certificate-details")
            return io.BytesIO(b"PASS")
        if failure == "protocol":
            raise BadStatusLine("private-api-token")
        if failure == "invalid":
            raise HTTPError(
                request.full_url,
                403,
                "private-response",
                {},
                io.BytesIO(b"secret-token"),
            )
        raise HTTPError(
            "https://unrelated.invalid/secret",
            302,
            "private-response",
            {"Location": "https://unrelated.invalid/secret"},
            io.BytesIO(b"secret-token"),
        )

    monkeypatch.setattr(OpenerDirector, "open", respond)
    with pytest.raises(RuntimeFailure) as error:
        runtime.run("api-check", username="reader", api_password="secret-password")
    assert not any(
        value in str(error.value)
        for value in ("private-", "secret", "unrelated.invalid")
    )
    assert len(requests) == (1 if failure in ("certificate", "compatibility") else 2)
