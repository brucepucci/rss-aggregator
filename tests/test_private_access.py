"""Private-access behavior at project-command and callable runtime boundaries."""

import copy
import io
import json
import socket
import subprocess
from http.client import BadStatusLine
from unittest.mock import MagicMock
from urllib.error import HTTPError, URLError
from urllib.request import OpenerDirector

import pytest
from click.testing import CliRunner
from test_commands import CommandFixture
from test_runtime_recovery import ROOT, Engine

from rss_aggregator.cli import main
from rss_aggregator.runtime import Runtime, RuntimeFailure


@pytest.fixture
def private_command(tmp_path):
    command = CommandFixture(tmp_path)
    command.machine(running=True)
    command.env["RSS_TAILSCALE"] = "1"
    command.tool(
        "tailscale",
        """import json, sys
args = sys.argv[1:]
if args == ['status', '--json']:
 print(json.dumps({'BackendState':'Running','Self':{'DNSName':'reader.example.ts.net.'},'CertDomains':['reader.example.ts.net']}))
elif args == ['serve', 'status', '--json']:
 print(json.dumps({'TCP':{'443':{'HTTPS':True}},'Web':{'reader.example.ts.net:443':{'Handlers':{'/':{'Proxy':'http://127.0.0.1:9000'}}}}}))
else: raise SystemExit('UNEXPECTED TAILSCALE MUTATION')
""",
    )
    return command


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
            elif tail == (
                "serve",
                "--bg",
                "--https=443",
                "--set-path=/",
                f"http://127.0.0.1:{self.port}",
            ):
                assert self.published and not self.probes
                if self.fail_serve:
                    return subprocess.CompletedProcess(
                        args, 1, "", "private-admin-url secret"
                    )
                self.config.setdefault("TCP", {})["443"] = {"HTTPS": True}
                self.config.setdefault("Web", {})["reader.example.ts.net:443"] = {
                    "Handlers": {"/": {"Proxy": f"http://127.0.0.1:{self.port}"}}
                }
                output = {}
            elif tail == ("serve", "--https=443", "--set-path=/", "off"):
                self.config["TCP"].pop("443", None)
                self.config["Web"].pop("reader.example.ts.net:443", None)
                output = {}
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
    monkeypatch.setenv("RSS_PORT", str(port))
    monkeypatch.setenv("RSS_TAILSCALE", "1")
    return runtime, engine


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


@pytest.mark.parametrize("conflict", ["handler", "target", "funnel", "owner"])
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
