"""Private forwarding and credential guards without a daemon or live traffic."""

import copy
import fcntl
import io
import json
import os
import platform
import shutil
import signal
import socket
import stat
import subprocess
import sys
from functools import partial
from http.client import BadStatusLine, RemoteDisconnected
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError
from urllib.parse import parse_qs
from urllib.request import HTTPHandler, HTTPSHandler
from urllib.response import addinfourl

import pytest
from click.testing import CliRunner
from test_native_install import Checkout
from test_serve_config import ControlledSocket, reply

from rss_aggregator import native_service
from rss_aggregator import runtime as native_runtime
from rss_aggregator.cli import main
from rss_aggregator.native_install import manifest_identity
from rss_aggregator.private_access import PrivateAccess
from rss_aggregator.runtime import (
    PublicBackendExposed,
    Runtime,
    RuntimeFailure,
    ServeConfigRejected,
)
from tests.test_native_process_lifetime import _exited, _wait_for
from tests.test_native_process_lifetime import (
    process_checkout as shared_process_checkout,
)

process_checkout = shared_process_checkout

HOST = "reader.example.ts.net"
ORIGIN = f"https://{HOST}"
PORT = 18080
BACKEND = f"http://127.0.0.1:{PORT}"
ADDRESS = f"{HOST}:443"
OTHER = "other.example.ts.net:8443"
LOCAL_SECRET = "synthetic-localapi-password"
REMOTE_SECRET = "synthetic-private-remote-diagnostic"
API_PASSWORD = "synthetic-api-password"
API_TOKEN = "synthetic-api-token"
PRIVATE_FEED = "https://feeds.invalid/subscriber-only"


def route(address=OTHER, target=BACKEND, *, funnel=False):
    port = address.rsplit(":", 1)[1]
    return {
        "TCP": {port: {"HTTPS": True}},
        "Web": {address: {"Handlers": {"/": {"Proxy": target}}}},
        **({"AllowFunnel": {address: True}} if funnel else {}),
    }


class LocalAPI:
    def __init__(self):
        self.config = route(target="http://127.0.0.1:8001")
        self.etag = '"snapshot-A"'
        self.sockets = []
        self.gets = []
        self.posts = []
        self.before_get = None
        self.before_post = None
        self.next_get_status = 200
        self.post_status = 200
        self.fail_connect_at = None
        self.commands = {
            ("status", "--json"): (
                0,
                json.dumps(
                    {
                        "BackendState": "Running",
                        "Self": {"DNSName": HOST + "."},
                        "CertDomains": [HOST],
                    }
                ),
            ),
            ("debug", "local-creds"): (
                0,
                f"curl -u:{LOCAL_SECRET} http://localhost:41112/localapi/v0/status\n",
            ),
        }
        self.grant(443)

    def grant(self, port):
        self.commands["debug", "netmap"] = (
            0,
            json.dumps(
                {
                    "PacketFilter": [
                        {
                            "IPProto": [6],
                            "Srcs": ["fixture-reader"],
                            "Dsts": [{"Ports": {"First": port, "Last": port}}],
                        }
                    ]
                }
            ),
        )

    def run_cli(self, args, **_kwargs):
        if args[0] == "/bin/df":
            return subprocess.CompletedProcess(args, 0, "Filesystem\n/dev/disk1\n", "")
        assert args[0] == "tailscale", f"Unexpected external command: {args[0]}"
        command = tuple(args[1:])
        if command == ("serve", "status", "--json"):
            code, stdout = 0, json.dumps(self.config)
        else:
            assert command in self.commands, f"Unexpected Tailscale command: {command}"
            code, stdout = self.commands[command]
        return subprocess.CompletedProcess(args, code, stdout, REMOTE_SECRET)

    def connect(self, address, timeout, source_address):
        assert address == ("127.0.0.1", 41112)
        assert source_address is None
        assert timeout > 0
        if self.fail_connect_at == len(self.sockets):
            self.sockets.append(None)
            raise ConnectionRefusedError(REMOTE_SECRET)
        sock = LocalAPISocket(self)
        self.sockets.append(sock)
        return sock

    def respond(self, request):
        line, headers, body = request
        assert headers["Host"] == "local-tailscaled.sock"
        if line == "GET /localapi/v0/serve-config HTTP/1.1":
            assert not body
            self.gets.append(self.etag)
            if self.before_get:
                self.before_get()
            return reply(
                json.dumps(self.config).encode(),
                status=self.next_get_status,
                headers=[("ETag", self.etag)],
            )
        assert line == "POST /localapi/v0/serve-config HTTP/1.1"
        replacement = json.loads(body)
        self.posts.append((headers.get("If-Match"), replacement))
        if self.before_post:
            self.before_post()
        if headers.get("If-Match") != self.etag:
            return reply(REMOTE_SECRET.encode(), status=412)
        if self.post_status == 200:
            self.config = replacement
            self.etag = '"snapshot-B"'
            return reply(b"")
        return reply(REMOTE_SECRET.encode(), status=self.post_status)


class LocalAPISocket(ControlledSocket):
    def __init__(self, api):
        super().__init__(b"")
        self.api = api

    def makefile(self, mode):
        self.response = self.api.respond(self.request())
        return super().makefile(mode)


@pytest.fixture(scope="module")
def installed_checkout(tmp_path_factory):
    checkout = Checkout(tmp_path_factory.mktemp("private-install"))
    checkout.prepare_source()
    checkout.prepare_runtime()
    config = Path(__file__).resolve().parents[1] / "config"
    for name in ("security-probe.php", "config-user.custom.php", "public-feed.opml"):
        shutil.copyfile(config / name, checkout.root / "config" / name)
    installed = checkout.run("setup")
    assert installed.returncode == 0, installed.stderr
    return checkout, json.loads(installed.stdout)


@pytest.fixture
def native(installed_checkout, tmp_path, monkeypatch):
    checkout, owner = installed_checkout
    state = tmp_path / "state"
    shutil.copytree(checkout.state, state)
    # A copied disposable fixture represents a separate initialized installation.
    # Its kernel lease files have different inodes than the template's files.
    registry = state / "writer-leases.json"
    registry.write_text(
        json.dumps(
            {
                name: (state / name).stat().st_ino
                for name in json.loads(registry.read_text())
            }
        )
    )
    assert not (state / "instances").exists()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("RSS_STATE_DIR", str(state))
    monkeypatch.setenv("RSS_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("RSS_TAILSCALE", "1")
    monkeypatch.setenv("RSS_PORT", str(PORT))
    monkeypatch.setattr(
        "rss_aggregator.cli.Runtime", partial(Runtime, root=checkout.root)
    )

    def forbid_io(*_args, **_kwargs):
        raise AssertionError("Unexpected native writer or network I/O")

    api = LocalAPI()
    monkeypatch.setattr(subprocess, "run", api.run_cli)
    monkeypatch.setattr(subprocess, "Popen", forbid_io)
    monkeypatch.setattr(socket, "create_connection", api.connect)
    monkeypatch.setattr(socket, "socket", forbid_io)
    monkeypatch.setattr(HTTPHandler, "http_open", forbid_io)
    monkeypatch.setattr(HTTPSHandler, "https_open", forbid_io)
    return SimpleNamespace(
        state=state,
        owner=owner,
        api=api,
        access=PrivateAccess(Runtime(root=checkout.root)),
        record=state / "serve.json",
        manifest=checkout.manifest,
        run=partial(CliRunner().invoke, main, catch_exceptions=False),
    )


def seed_owned_route(native, phase="active"):
    owned = route(ADDRESS)
    for section, entries in owned.items():
        native.api.config.setdefault(section, {}).update(entries)
    record = {
        "owner": native.owner["token"],
        "host": HOST,
        "target": BACKEND,
        "phase": phase,
    }
    native.record.write_text(json.dumps(record))
    return record


def refused(result, diagnostic, *secrets):
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert diagnostic in result.stderr
    for secret in (LOCAL_SECRET, REMOTE_SECRET, API_PASSWORD, API_TOKEN, *secrets):
        assert secret not in result.output


def preflight_refuses(native, diagnostic, *secrets):
    before = copy.deepcopy(native.api.config)
    refused(native.run(["start"]), diagnostic, *secrets)
    assert native.api.config == before
    assert not native.api.posts
    for path in ("instances", "service", "service.json", "security.json", "serve.json"):
        assert not (native.state / path).exists(), path


@pytest.mark.parametrize("scope", ["background", "foreground", "foreground-funnel"])
def test_start_refuses_unowned_443_before_touching_application(native, scope):
    config = route(ADDRESS, funnel=scope == "foreground-funnel")
    native.api.config = (
        config if scope == "background" else {"Foreground": {"foreign": config}}
    )
    expected = (
        "Existing Funnel reaches this FreshRSS backend"
        if scope == "foreground-funnel"
        else "Serve HTTPS port 443 is not exclusively project-owned"
    )
    preflight_refuses(native, expected)


@pytest.mark.parametrize(
    "scope",
    [
        "background",
        "foreground",
        "cross-scope",
        "service",
        "split-foreground-http",
        "split-foreground-tcp",
    ],
)
def test_start_refuses_funnel_reaching_backend_on_another_port(native, scope):
    public = route(funnel=True)
    if scope == "background":
        native.api.config = public
    elif scope == "foreground":
        native.api.config = {"Foreground": {"foreign": public}}
    elif scope == "service":
        native.api.config = {"Services": {"svc:foreign": public}}
    elif scope == "cross-scope":
        native.api.config = {
            "AllowFunnel": public.pop("AllowFunnel"),
            "Foreground": {"listener": public},
        }
    else:
        authorization = public["AllowFunnel"]
        listener = public["TCP"]
        if scope.endswith("tcp"):
            listener = {"8443": {"TCPForward": f"127.0.0.1:{PORT}"}}
        native.api.config = {
            "AllowFunnel": {OTHER: False},
            "Foreground": {
                "authorization": {"AllowFunnel": authorization},
                "listener": {"TCP": listener},
                "handler": {"Web": {OTHER: {"Handlers": {"/": {"Proxy": str(PORT)}}}}},
            },
        }
        if scope.endswith("tcp"):
            del native.api.config["Foreground"]["handler"]
    preflight_refuses(native, "Existing Funnel reaches this FreshRSS backend")


@pytest.mark.parametrize(
    "protocol,target",
    [
        ("http", "http://127.0.0.1:{port}/subpath"),
        ("http", "http://localhost:{port}"),
        ("http", "127.0.0.1:{port}"),
        ("http", "localhost:{port}"),
        ("http", "{port}"),
        ("http", ":{port}"),
        ("http", "http://:{port}"),
        ("http", "https+insecure://[::1]:{port}/subpath"),
        ("http", "http://[::ffff:127.0.0.1]:{port}"),
        ("http", "http://[::ffff:7f00:1]:{port}/subpath"),
        ("http", "http://0.0.0.0:{port}"),
        ("tcp", "127.0.0.1:{port}"),
        ("tcp", ":{port}"),
        ("tcp", "[::1]:{port}"),
        ("tcp", "[::ffff:127.0.0.1]:{port}"),
        ("tcp", "[::ffff:7f00:1]:{port}"),
        ("tcp", "0.0.0.0:{port}"),
    ],
)
def test_funnel_matches_backend_endpoint_not_handler_path(native, protocol, target):
    native.api.config = route(funnel=True)
    resolved = target.format(port=PORT)
    if protocol == "tcp":
        native.api.config["TCP"]["8443"] = {"TCPForward": resolved}
        del native.api.config["Web"]
    else:
        native.api.config["Web"][OTHER]["Handlers"] = {"/nested": {"Proxy": resolved}}
    preflight_refuses(native, "Existing Funnel reaches this FreshRSS backend")


@pytest.mark.parametrize(
    "variant",
    ["private", "different-port", "different-host", "service-namespace", "shadowed"],
)
def test_start_does_not_misidentify_unrelated_routes_as_public_backend(native, variant):
    if variant == "private":
        native.api.config = route()
    elif variant == "different-port":
        native.api.config = route(target="http://127.0.0.1:8001", funnel=True)
    elif variant == "different-host":
        native.api.config = route("different.example.ts.net:8443")
        native.api.config["AllowFunnel"] = {OTHER: True}
    elif variant == "service-namespace":
        native.api.config = route(target="http://127.0.0.1:8001", funnel=True)
        native.api.config["Services"] = {"svc:foreign": route()}
    else:
        native.api.config = route(funnel=True)
        native.api.config["Foreground"] = {
            "shadow": route(target="http://127.0.0.1:8001")
        }
    # Reaching the next preflight guard proves acceptance without opening writers.
    native.api.grant(22)
    preflight_refuses(native, "has no incoming TCP 443 grant")


@pytest.mark.parametrize(
    "failure,diagnostic",
    [
        ("invalid-setting", "RSS_TAILSCALE must be 0"),
        ("disconnected", "Connect the existing Tailscale installation"),
        ("certificate", "HTTPS certificates are not enabled"),
        ("failed-query", "Tailscale query failed"),
        ("invalid-json", "Unusable Tailscale JSON"),
        ("failed-discovery", "Unable to discover a supported Tailscale LocalAPI"),
        ("invalid-etag", "Invalid Tailscale Serve configuration response"),
    ],
)
def test_start_preflight_keeps_upstream_diagnostics_private(
    native, monkeypatch, failure, diagnostic
):
    if failure == "invalid-setting":
        monkeypatch.setenv("RSS_TAILSCALE", "invalid")
    elif failure == "invalid-etag":
        native.api.etag = "W/" + REMOTE_SECRET
    elif failure == "failed-discovery":
        _, credentials = native.api.commands["debug", "local-creds"]
        native.api.commands["debug", "local-creds"] = (1, credentials)
    else:
        command = ("status", "--json")
        _, response = native.api.commands[command]
        status = json.loads(response)
        if failure == "disconnected":
            status["BackendState"] = "Stopped"
        elif failure == "certificate":
            status["CertDomains"] = []
        native.api.commands[command] = (
            1 if failure == "failed-query" else 0,
            REMOTE_SECRET
            if failure in ("failed-query", "invalid-json")
            else json.dumps(status),
        )
    preflight_refuses(native, diagnostic)


@pytest.mark.parametrize(
    "conflict", ["handler", "target", "funnel", "owner", "foreground", "inactive"]
)
def test_stop_refuses_changed_ownership_without_touching_foreign_route(
    native, conflict
):
    record = seed_owned_route(native)
    config = native.api.config
    if conflict == "handler":
        config["Web"][ADDRESS]["Handlers"]["/other"] = {"Text": REMOTE_SECRET}
    elif conflict == "target":
        config["Web"][ADDRESS]["Handlers"]["/"]["Proxy"] = PRIVATE_FEED
    elif conflict == "funnel":
        config["AllowFunnel"] = {ADDRESS: True}
    elif conflict == "foreground":
        config["Foreground"] = {"foreign": {"TCP": {"443": {"HTTPS": True}}}}
    elif conflict == "inactive":
        record["phase"] = "inactive"
    else:
        record["owner"] = REMOTE_SECRET
    native.record.write_text(json.dumps(record))
    before = copy.deepcopy(config)
    diagnostic = (
        "Serve ownership differs"
        if conflict == "owner"
        else "not exclusively project-owned"
    )
    refused(native.run(["stop"]), diagnostic, PRIVATE_FEED)
    evidence = result_json(native.run(["backup-status"]))
    assert evidence["maintenance_required"] is True
    assert evidence["protected"] is False
    assert native.api.config == before
    assert not native.api.posts
    assert json.loads(native.record.read_text()) == record


@pytest.mark.parametrize("phase", ["pending", "active"])
def test_stop_idempotently_removes_only_owned_forwarding(native, phase):
    native.api.config["Foreground"] = {"foreign": route()}
    native.api.config["Services"] = {"svc:foreign": route("service.example.ts.net:443")}
    native.api.config["AllowFunnel"] = {OTHER: True}
    remaining = copy.deepcopy(native.api.config)
    record = seed_owned_route(native, phase)
    native.api.config["AllowFunnel"][ADDRESS] = False
    status = native.run(["status"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.stdout)["forwarding"] == {
        "status": "running",
        "url": ORIGIN,
    }

    for _ in range(2):
        stopped = native.run(["stop"])
        assert stopped.exit_code == 0, stopped.output
        assert json.loads(stopped.stdout)["application"] == "stopped"
        status = native.run(["status"])
        assert status.exit_code == 0, status.output
        assert json.loads(status.stdout)["forwarding"] == {
            "status": "stopped",
            "url": ORIGIN,
        }
    assert native.api.config == remaining
    assert native.api.posts == [('"snapshot-A"', remaining)]
    assert json.loads(native.record.read_text()) == {**record, "phase": "inactive"}


@pytest.mark.parametrize("changed", ["root", "unrelated"])
def test_stop_refuses_etag_conflict_without_overwriting_competing_route(
    native, changed
):
    record = seed_owned_route(native)

    def race():
        address = ADDRESS if changed == "root" else OTHER
        native.api.config["Web"][address]["Handlers"]["/"] = {"Text": REMOTE_SECRET}
        native.api.etag = '"competing"'

    native.api.before_post = race
    refused(native.run(["stop"]), "changed concurrently")
    assert [tag for tag, _ in native.api.posts] == ['"snapshot-A"']
    assert native.api.etag == '"competing"'
    address = ADDRESS if changed == "root" else OTHER
    assert native.api.config["Web"][address]["Handlers"]["/"] == {"Text": REMOTE_SECRET}
    assert json.loads(native.record.read_text()) == record


@pytest.mark.parametrize(
    "status,message",
    [
        (401, "authorization rejected"),
        (403, "authorization rejected"),
        (412, "changed concurrently"),
        (None, "before transmission"),
    ],
)
@pytest.mark.parametrize("prior_phase", [None, "inactive"])
def test_definitive_start_refusal_grants_no_authority_over_identical_route(
    native, status, message, prior_phase
):
    if prior_phase:
        before = copy.deepcopy(native.api.config)
        previous = seed_owned_route(native, prior_phase)
        native.api.config = before

    def competing_client():
        for section, entries in route(ADDRESS).items():
            native.api.config[section].update(entries)
        native.api.etag = '"competing"'

    if status is None:
        native.api.fail_connect_at = 2
    elif status == 412:
        # Winning concurrently must invalidate the snapshot even when the new
        # route is identical to the one the project attempted to publish.
        native.api.before_post = competing_client
    else:
        native.api.post_status = status
    with pytest.raises(ServeConfigRejected, match=message) as error:
        native.access.start(native.owner, PORT, ORIGIN)
    assert LOCAL_SECRET not in str(error.value)
    assert REMOTE_SECRET not in str(error.value)
    assert [tag for tag, _ in native.api.posts] == (
        [] if status is None else ['"snapshot-A"']
    )
    assert (ADDRESS in native.api.config["Web"]) == (status == 412)
    if prior_phase:
        assert json.loads(native.record.read_text()) == previous
    else:
        assert not native.record.exists()

    if status != 412:
        # An independently authorized client can also publish after rejection.
        competing_client()
    competing = copy.deepcopy(native.api.config)
    result = native.run(["status"])
    if prior_phase:
        refused(result, "not exclusively project-owned")
    else:
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["forwarding"] == {"status": "unconfigured"}
    stopped = native.run(["stop"])
    if prior_phase:
        refused(stopped, "not exclusively project-owned")
    else:
        assert stopped.exit_code == 0, stopped.output
    assert len(native.api.posts) == (0 if status is None else 1)
    assert native.api.config == competing


def test_repeated_forwarding_start_preserves_unrelated_routes(native):
    native.api.config["Foreground"] = {"foreign": route()}
    native.api.config["Services"] = {"svc:foreign": route("service.example.ts.net:443")}
    original = copy.deepcopy(native.api.config)
    for _ in range(2):
        assert native.access.start(native.owner, PORT, ORIGIN) == {
            "status": "running",
            "url": ORIGIN,
        }
        assert json.loads(native.record.read_text())["phase"] == "active"
    assert [tag for tag, _ in native.api.posts] == ['"snapshot-A"', '"snapshot-B"']
    stopped = native.run(["stop"])
    assert stopped.exit_code == 0, stopped.output
    assert native.api.config == original


def test_final_start_snapshot_refuses_new_funnel_without_publishing(native):
    def add_funnel():
        if len(native.api.gets) == 2:
            native.api.config = route(funnel=True)
            native.api.etag = '"with-new-funnel"'

    native.api.before_get = add_funnel
    with pytest.raises(
        RuntimeFailure, match="Existing Funnel reaches this FreshRSS backend"
    ):
        native.access.start(native.owner, PORT, ORIGIN)
    assert native.api.config == route(funnel=True)
    assert not native.api.posts
    assert not native.record.exists()


@pytest.mark.parametrize("applied", [False, True])
def test_unknown_start_retains_intent_for_owned_recovery(native, applied):
    original = copy.deepcopy(native.api.config)
    native.api.post_status = 500

    def lose_confirmation():
        if applied:
            native.api.config = copy.deepcopy(native.api.posts[-1][1])

    native.api.before_post = lose_confirmation
    with pytest.raises(RuntimeFailure, match="outcome may be unknown"):
        native.access.start(native.owner, PORT, ORIGIN)
    assert json.loads(native.record.read_text())["phase"] == "pending"
    status = native.run(["status"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.stdout)["forwarding"]["status"] == (
        "running" if applied else "stopped"
    )
    native.api.before_post = None
    native.api.post_status = 200
    stopped = native.run(["stop"])
    assert stopped.exit_code == 0, stopped.output
    assert native.api.config == original
    assert json.loads(native.record.read_text())["phase"] == "inactive"


@pytest.mark.parametrize("status", [401, 403])
def test_definitive_removal_refusal_restores_prior_intent(native, status):
    record = seed_owned_route(native)
    before = copy.deepcopy(native.api.config)
    native.api.post_status = status
    refused(native.run(["stop"]), "authorization rejected")
    assert len(native.api.posts) == 1
    assert native.api.config == before
    assert json.loads(native.record.read_text()) == record


@pytest.mark.parametrize(
    "failure,diagnostic",
    [
        ("unknown", "outcome may be unknown"),
        ("verification-get", "Unable to read Tailscale Serve configuration"),
        ("unrelated-change", "Unrelated Serve configuration changed concurrently"),
        ("recreated", "Project Serve forwarding did not stop"),
    ],
)
def test_uncertain_or_confirmed_removal_retires_authority_before_verification(
    native, failure, diagnostic
):
    record = seed_owned_route(native)
    owned = copy.deepcopy(native.api.config)

    def verification():
        if len(native.api.gets) == 2:
            if failure == "verification-get":
                native.api.next_get_status = 500
            elif failure == "unrelated-change":
                native.api.config["Web"][OTHER]["Handlers"]["/"] = {
                    "Text": REMOTE_SECRET
                }
            elif failure == "recreated":
                native.api.config = copy.deepcopy(owned)

    native.api.before_get = verification
    if failure == "unknown":
        native.api.post_status = 500
    refused(native.run(["stop"]), diagnostic)
    assert len(native.api.posts) == 1
    assert ADDRESS not in native.api.posts[0][1]["Web"]
    assert json.loads(native.record.read_text()) == {**record, "phase": "inactive"}
    native.api.config = owned
    refused(native.run(["status"]), "not exclusively project-owned")
    assert len(native.api.posts) == 1


@pytest.mark.parametrize(
    "stage", ["start-intent", "start-restoration", "stop-retirement"]
)
def test_forwarding_persistence_failure_cannot_grant_new_deletion_authority(
    native, monkeypatch, stage
):
    if stage == "start-restoration":
        native.record.write_text(
            json.dumps(
                {
                    "owner": native.owner["token"],
                    "host": HOST,
                    "target": BACKEND,
                    "phase": "inactive",
                }
            )
        )
        native.api.post_status = 403
    elif stage == "stop-retirement":
        seed_owned_route(native)
    before = native.record.read_bytes() if native.record.exists() else None
    replace = os.replace

    def storage(source, target, **kwargs):
        if target == native.record and (
            stage != "start-restoration" or native.api.posts
        ):
            raise OSError("synthetic storage failure")
        return replace(source, target, **kwargs)

    monkeypatch.setattr(os, "replace", storage)
    if stage == "start-restoration":
        with pytest.raises(
            RuntimeFailure, match="preceding intent could not be restored"
        ):
            native.access.start(native.owner, PORT, ORIGIN)
        assert len(native.api.posts) == 1
        assert not native.record.exists()
    elif stage == "start-intent":
        with pytest.raises(OSError, match="synthetic storage failure"):
            native.access.start(native.owner, PORT, ORIGIN)
        assert not native.api.posts
        assert not native.record.exists()
    else:
        refused(native.run(["stop"]), "synthetic storage failure")
        assert not native.api.posts
        assert native.record.read_bytes() == before


def http_response(request, body, status=200, headers=None):
    response = addinfourl(io.BytesIO(body), headers or {}, request.full_url, status)
    response.msg = REMOTE_SECRET
    return response


@pytest.fixture
def ready_native(native, monkeypatch):
    nonce = "a" * 32
    for name, value in {
        "service.json": {
            "nonce": nonce,
            "instance": "default",
            "port": PORT,
            "url": ORIGIN,
            "runtime": manifest_identity(native.manifest),
        },
        "service-status.json": {"nonce": nonce, "web": "started"},
        "security.json": {"nonce": nonce},
    }.items():
        (native.state / name).write_text(json.dumps(value))
    native.requests = []

    def ready(_handler, request):
        assert not request.data
        if request.full_url == BACKEND + "/i/":
            return http_response(request, b"")
        assert request.full_url == BACKEND + "/__rss_ready"
        assert request.get_header("Host") == f"rss-{nonce}.invalid"
        return http_response(request, nonce.encode())

    monkeypatch.setattr(HTTPHandler, "http_open", ready)
    with (
        (native.state / "writers.lock").open("wb") as writers,
        (native.state / "controller.lock").open("wb") as controller,
    ):
        for lock in (writers, controller):
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield native


def test_private_preflight_conflict_does_not_stop_existing_backend(
    ready_native, monkeypatch
):
    native = ready_native
    native.api.config = route(ADDRESS)
    evidence = [
        native.state / name
        for name in ("service.json", "service-status.json", "security.json")
    ]
    before = [path.read_bytes() for path in evidence]
    replace = os.replace

    def refuse_stop_request(source, target, **kwargs):
        # There is no real controller to act on a stop request; fail immediately.
        assert target != native.state / "stop-service.json"
        return replace(source, target, **kwargs)

    monkeypatch.setattr(os, "replace", refuse_stop_request)
    refused(native.run(["start"]), "not exclusively project-owned")

    assert [path.read_bytes() for path in evidence] == before
    assert not (native.state / "stop-service.json").exists()
    assert not native.record.exists()
    assert not native.api.posts
    assert native.api.config == route(ADDRESS)


@pytest.fixture
def controlled_web(native, monkeypatch):
    """Substitute only the listener, controller process and external PHP/HTTP."""
    handles = []
    elapsed = 0.0
    origin = None

    def lose_generation():
        while handles:
            os.close(handles.pop())

    native.lose_generation = lose_generation
    native.data = native.state / "instances/default/data"
    native.web_probe_overrides = {}
    native.acknowledge = True
    native.launches = 0
    native.fetch_result = lambda: 0

    def spawn(args, *, pass_fds, **kwargs):
        nonlocal origin
        if args[1:3] == ["-I", "-B"]:
            assert Path(args[3]).name == "native_service.py"
            assert args[5] == str(native.state) and args[6] == str(native.data)
            assert pass_fds == (int(args[7]),)
            assert kwargs["stderr"] == subprocess.DEVNULL
            if args[4] == "fetch":
                assert kwargs["stdout"] == subprocess.DEVNULL
                return SimpleNamespace(wait=native.fetch_result)
            assert args[4] == "php"
            operation = args[9]
            if operation == "-r":
                result = ["form", False, False, True, origin, True, "sqlite"]
                kwargs["stdout"].write(json.dumps(result).encode())
            elif operation == "cli/reconfigure.php":
                origin = args[args.index("--base-url") + 1]
            else:
                assert operation == "cli/prepare.php"
            return SimpleNamespace(wait=lambda: 0)
        assert args[2:4] == ["-m", "rss_aggregator.native_service"]
        assert not handles
        handles.extend(os.dup(fd) for fd in pass_fds)
        plan = json.loads((native.state / "service.json").read_text())
        (native.state / "service-status.json").write_text(
            json.dumps(
                {"nonce": plan["nonce"], "web": "started", "scheduler": "running"}
            )
        )
        native.launches += 1
        return object()

    def settle(seconds):
        nonlocal elapsed
        elapsed += seconds
        plan = json.loads((native.state / "service.json").read_text())
        nonce = plan["nonce"]
        service = native.state / "service"
        request = service / f"approved-{nonce}.json"
        acknowledgement = service / f"acknowledged-{nonce}.json"
        stop = native.state / "stop-service.json"
        if stop.is_file() and json.loads(stop.read_text())["nonce"] == nonce:
            acknowledgement.unlink(missing_ok=True)
            request.unlink(missing_ok=True)
            lose_generation()
        elif handles and native.acknowledge and request.is_file():
            acknowledgement.write_text(json.dumps({"nonce": nonce}))

    def web(_handler, request):
        path = request.selector
        assert request.full_url == BACKEND + path and handles
        plan = json.loads((native.state / "service.json").read_text())
        if path in ("/__rss_ready", "/__rss_probe.php"):
            assert request.get_header("Host") == "rss-" + plan["nonce"] + ".invalid"
        if path == "/__rss_ready":
            return http_response(request, plan["nonce"].encode())
        if path == "/__rss_probe.php":
            assert (native.state / "service/__rss_probe.php").is_file()
            body = json.dumps(
                {
                    "sapi": "frankenphp",
                    "php": native.manifest["runtime"]["php"],
                    "upload_progress": "0",
                    "expose_php": "0",
                    "display_errors": "0",
                    "sqlite": True,
                    "loaded_ini": str(native.state / "service/php.ini"),
                    "initialized": (native.data / "applied_migrations.txt").is_file(),
                    "authenticated_settings": True,
                    **native.web_probe_overrides,
                }
            ).encode()
            return http_response(request, body)
        if path == "/i/":
            return http_response(request, b"")
        assert path == "/api/greader.php/reader/api/0/subscription/list"
        return http_response(request, b"", 403)

    class Listener:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            pass

        def setsockopt(self, *_args):
            pass

        def bind(self, address):
            assert address == ("127.0.0.1", PORT)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(socket, "socket", Listener)
    monkeypatch.setattr(
        "rss_aggregator.runtime.time",
        SimpleNamespace(monotonic=lambda: elapsed, sleep=settle),
    )
    monkeypatch.setattr(HTTPHandler, "http_open", web)
    yield native
    while handles:
        os.close(handles.pop())


def initialized_web(native):
    config = native.data / "config.php"
    config.write_text("synthetic config")
    config.chmod(0o600)
    (native.data / "applied_migrations.txt").touch(mode=0o600)


def result_json(result):
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


@pytest.mark.parametrize("disconnect", [RemoteDisconnected, ConnectionResetError])
@pytest.mark.parametrize("persistent", [False, True], ids=["recovers", "persistent"])
def test_start_retries_loopback_readiness_disconnect(
    controlled_web, monkeypatch, disconnect, persistent
):
    native = controlled_web
    monkeypatch.setenv("RSS_TAILSCALE", "0")
    ready = HTTPHandler.http_open
    probes = 0

    def web(handler, request):
        nonlocal probes
        if request.selector == "/__rss_ready":
            probes += 1
            if persistent or probes == 1:
                raise disconnect("synthetic readiness disconnect")
        return ready(handler, request)

    monkeypatch.setattr(HTTPHandler, "http_open", web)
    result = native.run(["start"])
    if persistent:
        refused(result, "did not become ready on loopback")
        assert result_json(native.run(["status"]))["application"] == "stopped"
        assert not (native.state / "security.json").exists()
    else:
        assert result_json(result)["application"] == "running"
        assert result_json(native.run(["status"]))["http_ready"] is True
    assert probes > 1
    assert native.launches == 1
    assert not native.api.posts


def test_start_seeds_complete_private_templates_and_retries_failed_seed(
    controlled_web, monkeypatch
):
    native = controlled_web
    monkeypatch.setenv("RSS_TAILSCALE", "0")
    config = native.access.runtime.root / "config"
    original_read = Path.read_bytes

    def missing_source(path):
        if path == config / "public-feed.opml":
            raise FileNotFoundError("Synthetic inaccessible source template")
        return original_read(path)

    previous = os.umask(0o002)
    try:
        with monkeypatch.context() as io:
            io.setattr(Path, "read_bytes", missing_source)
            failure = native.run(["start"])
        assert failure.exit_code == 1, failure.output
        assert "Synthetic inaccessible source template" in failure.stderr
        assert not (native.data / "opml.xml").exists()
        result_json(native.run(["start"]))
        for name, template in (
            ("config-user.custom.php", "config-user.custom.php"),
            ("opml.xml", "public-feed.opml"),
        ):
            data = native.data / name
            assert data.read_bytes() == (config / template).read_bytes()
            assert stat.S_IMODE(data.stat().st_mode) == 0o600
        assert result_json(native.run(["stop"]))["backup"]["verified"] is True
    finally:
        os.umask(previous)


def test_occupied_replacement_port_leaves_existing_service_ready(
    controlled_web, monkeypatch
):
    native = controlled_web
    monkeypatch.setenv("RSS_TAILSCALE", "0")
    result_json(native.run(["start"]))
    initialized_web(native)
    monkeypatch.setenv("RSS_TAILSCALE", "1")
    result_json(native.run(["start"]))
    changes = len(native.api.posts)
    generation = (native.state / "service.json").read_bytes()
    original_bind = socket.socket.bind

    def occupied(listener, address):
        if address == ("127.0.0.1", PORT + 1):
            raise OSError("synthetic occupied port")
        original_bind(listener, address)

    monkeypatch.setattr(socket.socket, "bind", occupied)
    monkeypatch.setenv("RSS_PORT", str(PORT + 1))
    refused(native.run(["start"]), f"Loopback port {PORT + 1} is occupied")
    assert result_json(native.run(["status"]))["http_ready"] is True
    assert (native.state / "service.json").read_bytes() == generation
    assert len(native.api.posts) == changes


@pytest.mark.parametrize(
    "failure",
    [
        "generation-lost",
        "no-acknowledgement",
        "ordinary-http-unavailable",
        "stale-receipt",
    ],
)
def test_private_start_fails_closed_if_published_generation_is_not_ready(
    controlled_web, monkeypatch, failure
):
    native = controlled_web
    monkeypatch.setenv("RSS_TAILSCALE", "0")
    result_json(native.run(["start"]))
    result_json(native.run(["stop"]))
    initialized_web(native)
    monkeypatch.setenv("RSS_TAILSCALE", "1")
    original = copy.deepcopy(native.api.config)
    ordinary = HTTPHandler.http_open
    native.acknowledge = failure != "no-acknowledgement"

    def while_publishing():
        if ADDRESS not in native.api.posts[-1][1]["Web"]:
            return
        nonce = json.loads((native.state / "service.json").read_text())["nonce"]
        assert not (native.state / "service" / f"approved-{nonce}.json").exists()
        if failure == "generation-lost":
            native.lose_generation()

    def web(handler, request):
        if request.selector == "/i/" and request.get_header("Host") == request.host:
            if failure == "ordinary-http-unavailable":
                return http_response(request, b"", 503)
            if failure == "stale-receipt":
                (native.state / "service-status.json").write_text(
                    json.dumps(
                        {"nonce": "b" * 32, "web": "started", "scheduler": "running"}
                    )
                )
        return ordinary(handler, request)

    native.api.before_post = while_publishing
    monkeypatch.setattr(HTTPHandler, "http_open", web)
    refused(native.run(["start"]), "startup approval")
    status = result_json(native.run(["status"]))
    assert (status["application"], status["http_ready"]) == ("stopped", False)
    assert native.api.config == original
    assert ADDRESS in native.api.posts[0][1]["Web"]
    assert native.api.posts[1] == ('"snapshot-B"', original)


@pytest.mark.parametrize("failure", ["unknown-write", "competing-route"])
def test_failed_private_start_preserves_uncertain_or_replaced_route(
    controlled_web, monkeypatch, failure
):
    native = controlled_web
    monkeypatch.setenv("RSS_TAILSCALE", "0")
    result_json(native.run(["start"]))
    result_json(native.run(["stop"]))
    initialized_web(native)
    monkeypatch.setenv("RSS_TAILSCALE", "1")
    if failure == "unknown-write":
        native.api.post_status = 500

        def lose_confirmation():
            native.api.config = copy.deepcopy(native.api.posts[0][1])

        native.api.before_post = lose_confirmation
        refused(native.run(["start"]), "outcome may be unknown")
        assert len(native.api.posts) == 1
        assert native.api.config == native.api.posts[0][1]
        assert result_json(native.run(["status"]))["application"] == "stopped"
    else:
        reads_after_publication = 0

        def replace_after_readback():
            nonlocal reads_after_publication
            if len(native.api.posts) == 1:
                reads_after_publication += 1
                if reads_after_publication == 2:
                    native.api.config["Web"][ADDRESS]["Handlers"]["/"] = {
                        "Text": REMOTE_SECRET
                    }
                    native.api.etag = '"competitor"'

        native.api.before_get = replace_after_readback
        refused(
            native.run(["start"]),
            "withdrawal of startup forwarding could not be confirmed",
        )
        assert native.api.posts[1][0] == '"snapshot-B"'
        assert native.api.etag == '"competitor"'
        assert native.api.config["Web"][ADDRESS]["Handlers"]["/"] == {
            "Text": REMOTE_SECRET
        }
        # Removing the simulated competing route from inspection doesn't affect
        # the already-confirmed writer shutdown.
        native.api.config = route(target="http://127.0.0.1:8001")
        assert result_json(native.run(["status"]))["application"] == "stopped"


@pytest.mark.parametrize("exit_while_publishing", [False, True])
def test_private_start_observes_ordinary_gate_from_real_controller(
    process_checkout, monkeypatch, exit_while_publishing
):
    checkout = process_checkout
    state, home = checkout.state, checkout.control
    monkeypatch.chdir(checkout.root)
    for key in ("HOME", "PATH", "RSS_PORT", "RSS_BACKUP_DIR"):
        monkeypatch.setenv(key, checkout.environment[key])
    monkeypatch.setenv("RSS_TAILSCALE", "1")
    monkeypatch.setattr(
        native_runtime,
        "sys",
        SimpleNamespace(
            executable=str(checkout.launcher), platform=sys.platform, stderr=sys.stderr
        ),
    )
    api = LocalAPI()
    external = subprocess.run

    def run(args, **kwargs):
        return (
            api.run_cli(args, **kwargs)
            if args[0] == "tailscale"
            else external(args, **kwargs)
        )

    class PortCheck:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def setsockopt(self, *_args):
            pass

        def bind(self, address):
            assert address == ("127.0.0.1", PORT)

    def generation():
        return json.loads((state / "service.json").read_text())["nonce"]

    ordinary = []

    def probe(port, path, *, host=None):
        assert port == PORT
        nonce = generation()
        try:
            with socket.socket(socket.AF_UNIX) as sock:
                sock.settimeout(1)
                sock.connect(".ready-" + nonce)
                with sock.makefile("rb") as response:
                    assert response.read() == nonce.encode()
        except (FileNotFoundError, ConnectionError, TimeoutError):
            return 0, b""
        if path == "/__rss_ready":
            assert host == f"rss-{nonce}.invalid"
            return 200, nonce.encode()
        if path == "/__rss_probe.php":
            return 200, json.dumps(
                {
                    "sapi": "frankenphp",
                    "php": checkout.manifest["runtime"]["php"],
                    "upload_progress": "0",
                    "expose_php": "0",
                    "display_errors": "0",
                    "sqlite": True,
                    "loaded_ini": str(home / "php.ini"),
                    "initialized": True,
                    "authenticated_settings": True,
                }
            ).encode()
        if path == "/i/":
            if host is not None:
                return 200, b""
            caddy = (home / ("Caddyfile-" + nonce)).read_text()
            # Model just the generated Caddy file matcher; the actual native
            # controller, leases and the worker's Unix readiness are not mocked.
            gate = next(
                line.split()[-1] for line in caddy.splitlines() if "try_files" in line
            )
            code = 200 if (home / gate.lstrip("/")).is_file() else 503
            ordinary.append(code)
            return code, b""
        assert path == "/api/greader.php/reader/api/0/subscription/list"
        return 403, b""

    def while_publishing():
        if ADDRESS not in api.posts[-1][1]["Web"]:
            return
        probe(PORT, "/i/")
        if exit_while_publishing:
            (home / ("web-release-" + generation())).touch()
            receipt = state / "service-status.json"
            _wait_for(
                lambda: json.loads(receipt.read_text())["web"] == "failed",
                "native controller did not observe its web writer exiting",
            )

    api.before_post = while_publishing
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(socket, "create_connection", api.connect)
    monkeypatch.setattr(native_service, "web_check", probe)
    monkeypatch.setattr(
        native_runtime,
        "socket",
        SimpleNamespace(
            socket=PortCheck,
            SOL_SOCKET=socket.SOL_SOCKET,
            SO_REUSEADDR=socket.SO_REUSEADDR,
        ),
    )
    runtime = Runtime(root=checkout.root, state_dir=state)
    original = copy.deepcopy(api.config)
    try:
        if exit_while_publishing:
            with pytest.raises(RuntimeFailure, match="startup approval"):
                runtime.run("start")
            status = runtime.run("status")
            assert (status["application"], status["http_ready"]) == ("stopped", False)
            assert api.config == original
            assert ordinary == [503]
            assert len(api.posts) == 2
        else:
            assert runtime.run("start")["application"] == "running"
            assert ordinary == [503, 200]
            assert runtime.run("status")["http_ready"] is True
            assert len(api.posts) == 1
        assert not list(home.glob("approved-*.json"))
    finally:
        if (state / "service.json").exists():
            (home / ("web-release-" + generation())).touch()
            receipt = state / "service-status.json"
            _wait_for(
                lambda: json.loads(receipt.read_text())["web"] != "started",
                "disposable controller did not shut down",
            )


def test_private_start_rechecks_forwarding_after_preparation(
    controlled_web, monkeypatch
):
    native = controlled_web
    monkeypatch.setenv("RSS_TAILSCALE", "0")
    result_json(native.run(["start"]))
    result_json(native.run(["stop"]))
    initialized_web(native)
    monkeypatch.setenv("RSS_TAILSCALE", "1")
    previous_launches = native.launches
    prepare = PrivateAccess.prepare
    check_backend = PrivateAccess.check_backend
    prepared = False

    def fetch_result():
        nonlocal prepared
        prepared = True
        return 0

    def check_before_binding():
        if prepared:
            assert native.launches == previous_launches
            raise RuntimeFailure("Synthetic forwarding inspection failed")

    def prepare_access(access, owner, port):
        check_before_binding()
        return prepare(access, owner, port)

    def check_access(access, port):
        check_before_binding()
        return check_backend(access, port)

    native.fetch_result = fetch_result
    monkeypatch.setattr(PrivateAccess, "prepare", prepare_access)
    monkeypatch.setattr(PrivateAccess, "check_backend", check_access)
    refused(native.run(["start"]), "Synthetic forwarding inspection failed")
    assert result_json(native.run(["status"]))["application"] == "stopped"
    assert not native.api.posts


@pytest.mark.parametrize("intervening", [None, "start", "verify"])
def test_successive_backup_resumptions_keep_forwarding_check_without_republishing(
    controlled_web, monkeypatch, intervening
):
    native = controlled_web
    monkeypatch.setenv("RSS_TAILSCALE", "0")
    result_json(native.run(["start"]))
    result_json(native.run(["stop"]))
    initialized_web(native)
    monkeypatch.setenv("RSS_TAILSCALE", "1")
    original = copy.deepcopy(native.api.config)
    assert result_json(native.run(["start"]))["url"] == ORIGIN

    # The original owned route disappears independently. Resumption is local,
    # despite the original opt-in, and must not forget to inspect this backend.
    native.api.config = original
    result_json(native.run(["backup"]))
    status = result_json(native.run(["status"]))
    assert (status["url"], status["http_ready"]) == (BACKEND, True)
    if intervening == "start":
        monkeypatch.setenv("RSS_TAILSCALE", "0")
        assert (
            result_json(native.run(["start"]))["fetch"]["status"] == "already_running"
        )
    elif intervening == "verify":
        assert result_json(native.run(["verify"]))["url"] == BACKEND
    assert len(native.api.posts) == 1
    launches = native.launches

    def publish(source, destination, **kwargs):
        result = link(source, destination, **kwargs)
        if Path(destination).parent == Path(os.environ["RSS_BACKUP_DIR"]):
            native.api.config = route(funnel=True)
            native.api.etag = '"foreign-funnel"'
        return result

    link = os.link
    monkeypatch.setattr(os, "link", publish)
    refused(native.run(["backup"]), "Existing Funnel reaches this FreshRSS backend")
    status = result_json(native.run(["status"]))
    assert (status["application"], status["http_ready"]) == ("stopped", False)
    backup = result_json(native.run(["backup-status"]))
    assert backup["latest_success"]["verified"] is True
    assert backup["resumption"] == "failed"
    assert native.launches == launches
    assert len(native.api.posts) == 1
    assert native.api.config == route(funnel=True)


def test_stalled_discovery_is_reaped_without_holding_command_lock(process_checkout):
    checkout = process_checkout
    result = checkout.run("start")
    assert result.returncode == 0, result.stderr
    nonce = json.loads((checkout.state / "service.json").read_text())["nonce"]
    owner = json.loads((checkout.state / "native-owner.json").read_text())
    (checkout.state / "serve.json").write_text(
        json.dumps(
            {
                "owner": owner["token"],
                "host": HOST,
                "target": BACKEND,
                "phase": "active",
            }
        )
    )
    executable = Path(checkout.environment["PATH"]) / "tailscale"
    child_state = checkout.state / "discovery-child"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, signal, sys, time\n"
        "from pathlib import Path\n"
        "if sys.argv[1:] == ['serve', 'status', '--json']:\n"
        "    print('{}')\n"
        "    raise SystemExit(0)\n"
        "assert sys.argv[1:] == ['debug', 'local-creds']\n"
        "signal.alarm(45)\n"
        "state = Path(os.environ['RSS_STATE_DIR'])\n"
        "lock = (state / 'command.lock').stat()\n"
        "inherited = False\n"
        "for fd in range(3, 256):\n"
        "    try:\n"
        "        info = os.fstat(fd)\n"
        "    except OSError:\n"
        "        continue\n"
        "    inherited |= (info.st_dev, info.st_ino) == (lock.st_dev, lock.st_ino)\n"
        "progress = state / 'child.tmp'\n"
        "progress.write_text(json.dumps([os.getpid(), inherited]))\n"
        "progress.replace(state / 'discovery-child')\n"
        "time.sleep(45)\n"
    )
    executable.chmod(0o700)
    with subprocess.Popen(
        (checkout.launcher, "-I", "-B", "-m", "rss_aggregator", "stop"),
        cwd=checkout.root,
        env=checkout.environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as command:
        pid = None
        try:
            _wait_for(child_state.exists, "Tailscale discovery did not start")
            pid, inherited = json.loads(child_state.read_text())
            try:
                output, error = command.communicate(timeout=9)
            except subprocess.TimeoutExpired:
                pytest.fail("stop stalled in the read-only Tailscale process")
            assert command.returncode == 1 and not output
            assert "Private forwarding shutdown is unconfirmed" in error
            assert "Tailscale LocalAPI" in error
            assert _exited(pid), "stop returned before reaping discovery"
            assert not inherited, "Tailscale discovery inherited the command lock"
            result = checkout.run("status")
            assert result.returncode == 0, result.stderr
            status = json.loads(result.stdout)
            assert (status["application"], status["http_ready"]) == ("stopped", False)
            assert not list(checkout.backups.glob("*.zip"))
        finally:
            if pid is not None and not _exited(pid):
                os.kill(pid, signal.SIGKILL)
            if command.poll() is None:
                command.kill()
            (checkout.control / ("web-release-" + nonce)).touch()
            _wait_for(
                lambda: checkout.run("status").returncode == 0,
                "stop retry was blocked by the discovery command lock",
            )


def test_foreground_fetch_timeout_leaves_failure_evidence_for_resumption(
    controlled_web, monkeypatch
):
    native = controlled_web
    monkeypatch.setenv("RSS_TAILSCALE", "0")
    result_json(native.run(["start"]))
    initialized_web(native)
    result_json(native.run(["start"]))

    def timed_out():
        return native_service.FETCH_TIMED_OUT

    native.fetch_result = timed_out
    refused(native.run(["fetch"]), "timed out after 600 seconds")
    assert result_json(native.run(["status"]))["http_ready"] is True
    refused(native.run(["verify"]), "timed out after 600 seconds")
    assert result_json(native.run(["status"]))["application"] == "stopped"

    native.fetch_result = lambda: 0
    result_json(native.run(["start"]))
    native.fetch_result = timed_out
    refused(native.run(["backup"]), "timed out after 600 seconds")
    status = result_json(native.run(["backup-status"]))
    assert status["latest_attempt"]["outcome"] == "failed"
    assert status["resumption"] == "failed"
    assert status["latest_success"]["verified"] is True
    assert result_json(native.run(["status"]))["application"] == "stopped"


def test_repeat_start_observes_installer_completion(controlled_web, monkeypatch):
    native = controlled_web
    monkeypatch.setenv("RSS_TAILSCALE", "0")
    assert not result_json(native.run(["start"]))["security"]["initialized"]
    initialized_web(native)
    result = result_json(native.run(["start"]))
    assert result["security"]["initialized"]
    assert result["security"]["authenticated_settings"]
    assert result["fetch"]["status"] == "completed"
    assert result_json(native.run(["start"]))["fetch"]["status"] == "already_running"
    assert native.launches == 2
    assert not (native.state / "service/__rss_probe.php").exists()
    assert not native.api.posts


@pytest.mark.parametrize(
    "field,value",
    [("php", "8.5.0"), ("sapi", "cli"), ("upload_progress", "1")],
    ids=["php-version", "web-sapi", "upload-progress-enabled"],
)
def test_private_start_refuses_mismatched_effective_web_php(
    controlled_web, monkeypatch, field, value
):
    native = controlled_web
    monkeypatch.setenv("RSS_TAILSCALE", "0")
    assert not result_json(native.run(["start"]))["security"]["initialized"]
    initialized_web(native)
    monkeypatch.setenv("RSS_TAILSCALE", "1")
    original = copy.deepcopy(native.api.config)
    native.web_probe_overrides[field] = value

    refused(native.run(["start"]), "Effective web PHP settings")

    status = result_json(native.run(["status"]))
    assert (status["application"], status["http_ready"]) == ("stopped", False)
    assert status["forwarding"]["status"] == "unconfigured"
    assert not (native.state / "service/__rss_probe.php").exists()
    assert not native.api.posts
    assert native.api.config == original


@pytest.mark.parametrize("repeated", [False, True], ids=["first-start", "repeat-start"])
def test_private_start_refuses_failed_effective_authentication(
    controlled_web, monkeypatch, repeated
):
    native = controlled_web
    monkeypatch.setenv("RSS_TAILSCALE", "0")
    assert not result_json(native.run(["start"]))["security"]["initialized"]
    initialized_web(native)
    monkeypatch.setenv("RSS_TAILSCALE", "1")
    if repeated:
        assert result_json(native.run(["start"]))["security"]["initialized"]
    native.web_probe_overrides["authenticated_settings"] = False
    refused(native.run(["start"]), "Effective web PHP settings")
    status = result_json(native.run(["status"]))
    assert status["application"] == "stopped"
    assert status["forwarding"]["status"] != "running"
    assert not (native.state / "service/__rss_probe.php").exists()
    assert len(native.api.posts) == (2 if repeated else 0)


def test_verify_stops_owned_backend_on_confirmed_public_exposure(
    controlled_web, monkeypatch
):
    native = controlled_web
    monkeypatch.setenv("RSS_TAILSCALE", "0")
    result_json(native.run(["start"]))
    initialized_web(native)
    monkeypatch.setenv("RSS_TAILSCALE", "1")
    result_json(native.run(["start"]))
    observed = copy.deepcopy(native.api.config)
    writes = len(native.api.posts)

    def confirmed(_access, _owner, port):
        assert port == PORT
        raise PublicBackendExposed("Synthetic confirmed public exposure")

    monkeypatch.setattr(PrivateAccess, "prepare", confirmed)
    refused(native.run(["verify"]), "Owned backend shutdown confirmed")
    assert result_json(native.run(["status"]))["application"] == "stopped"
    assert native.api.config == observed
    assert len(native.api.posts) == writes


def test_upgrade_rejects_archive_from_before_last_running_session(
    controlled_web, monkeypatch
):
    native = controlled_web
    destination = Path(os.environ["RSS_BACKUP_DIR"])
    monkeypatch.setenv("RSS_TAILSCALE", "0")
    # The disposable setup child already uses this platform; upgrade runs here.
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    monkeypatch.setattr(platform, "mac_ver", lambda: ("14.0.0", ("", "", ""), "arm64"))
    result_json(native.run(["start"]))
    initialized_web(native)
    old_archive = Path(result_json(native.run(["stop"]))["backup"]["archive"])
    result_json(native.run(["start"]))
    changed = native.data / "new-state"
    changed.write_text("synthetic post-backup change")
    changed.chmod(0o600)
    monkeypatch.setenv("RSS_BACKUP_DIR", str(old_archive))
    failed = native.run(["stop"])
    assert failed.exit_code == 1, failed.output
    assert "Shutdown backup failed" in failed.stderr
    assert old_archive.is_file()
    preview = result_json(native.run(["upgrade"]))
    refused(
        native.run(["upgrade", "--confirm", preview["confirmation"]]),
        "shutdown archive",
    )
    monkeypatch.setenv("RSS_BACKUP_DIR", str(destination))
    result_json(native.run(["stop"]))
    assert (
        result_json(native.run(["upgrade", "--confirm", preview["confirmation"]]))[
            "status"
        ]
        == "installed"
    )
    (native.state / "instances").rename(native.state / "held-instances")
    refused(
        native.run(["upgrade", "--confirm", preview["confirmation"]]),
        "Selected instance data are absent",
    )


@pytest.mark.parametrize("flags", [[], ["--loopback"]])
def test_api_cli_refuses_unverified_instance_without_echoing_password(native, flags):
    result = native.run(["api-check", *flags], input=f"reader\n{API_PASSWORD}\n")
    refused(result, "verified running instance")
    assert "API password" in result.stderr
    assert not native.api.sockets


def test_loopback_api_does_not_send_credentials_to_wrong_service_generation(
    ready_native, monkeypatch
):
    def wrong_generation(_handler, request):
        assert request.full_url == BACKEND + "/__rss_ready"
        assert not request.data
        return http_response(request, b"stale-generation")

    monkeypatch.setattr(HTTPHandler, "http_open", wrong_generation)
    result = ready_native.run(
        ["api-check", "--loopback"], input=f"reader\n{API_PASSWORD}\n"
    )
    refused(result, "verified running instance")


@pytest.mark.parametrize("forwarding", ["missing", "foreign"])
def test_api_cli_requires_owned_https_before_sending_credentials(
    ready_native, forwarding
):
    if forwarding == "foreign":
        seed_owned_route(ready_native)
        ready_native.api.config["Web"][ADDRESS]["Handlers"]["/"]["Proxy"] = PRIVATE_FEED
    result = ready_native.run(["api-check"], input=f"reader\n{API_PASSWORD}\n")
    diagnostic = (
        "Start with RSS_TAILSCALE=1 before testing trusted HTTPS"
        if forwarding == "missing"
        else "not exclusively project-owned"
    )
    refused(result, diagnostic, PRIVATE_FEED)


@pytest.mark.parametrize("loopback", [False, True])
def test_api_cli_authenticates_only_to_verified_origin_and_returns_sanitized_counts(
    ready_native, monkeypatch, loopback
):
    native = ready_native
    if not loopback:
        seed_owned_route(native)
    origin = BACKEND if loopback else ORIGIN
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:9999")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:9999")

    def respond(handler, request):
        native.requests.append(request)
        assert request.full_url.startswith(origin + "/api/greader.php/")
        assert request.host == origin.split("://", 1)[1]
        assert API_PASSWORD not in request.full_url
        if request.full_url.endswith("/check/compatibility"):
            assert request.get_method() == "GET"
            assert request.data is None
            return http_response(request, b"PASS")
        if request.full_url.endswith("/accounts/ClientLogin"):
            assert request.get_method() == "POST"
            assert parse_qs(request.data.decode()) == {
                "Email": ["reader"],
                "Passwd": [API_PASSWORD],
            }
            return http_response(
                request, f"SID=synthetic-session\nAuth={API_TOKEN}\n".encode()
            )
        assert request.get_header("Authorization") == f"GoogleLogin auth={API_TOKEN}"
        if "subscription/list" in request.full_url:
            return http_response(
                request, json.dumps({"subscriptions": [{"id": PRIVATE_FEED}]}).encode()
            )
        assert "stream/contents/" in request.full_url
        return http_response(request, b'{"items":[{"id":"synthetic-private-article"}]}')

    if loopback:
        ready = HTTPHandler.http_open

        def dispatch(handler, request):
            if request.full_url in (BACKEND + "/__rss_ready", BACKEND + "/i/"):
                return ready(handler, request)
            return respond(handler, request)

        monkeypatch.setattr(HTTPHandler, "http_open", dispatch)
    else:
        monkeypatch.setattr(HTTPSHandler, "https_open", respond)
    flags = ["--loopback"] if loopback else []
    result = native.run(["api-check", *flags], input=f"reader\n{API_PASSWORD}\n")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "diagnostics": "PASS",
        "authenticated": True,
        "subscriptions": 1,
        "sample_articles": 1,
        "transport": "loopback" if loopback else "https",
        "https_verified": not loopback,
    }
    for value in (
        API_PASSWORD,
        API_TOKEN,
        PRIVATE_FEED,
        "synthetic-session",
        "synthetic-private-article",
    ):
        assert value not in result.output
    assert len(native.requests) == 4


@pytest.mark.parametrize(
    "failure,diagnostic,requests",
    [
        ("unauthorized", "API authentication rejected", 2),
        ("redirect", "Redirects are refused", 2),
        ("certificate", "certificate untrusted", 1),
        ("compatibility", "compatibility check failed", 1),
        ("protocol", "certificate untrusted", 2),
        ("malformed", "unusable API diagnostics", 3),
    ],
)
def test_api_cli_never_reveals_upstream_response_or_forwards_redirect(
    ready_native, monkeypatch, failure, diagnostic, requests
):
    native = ready_native
    seed_owned_route(native)
    redirect = "https://unrelated.invalid/private-target"

    def respond(_handler, request):
        native.requests.append(request)
        assert request.full_url.startswith(ORIGIN + "/api/greader.php/")
        if request.full_url.endswith("/check/compatibility"):
            if failure == "certificate":
                raise URLError(REMOTE_SECRET)
            return http_response(
                request,
                REMOTE_SECRET.encode() if failure == "compatibility" else b"PASS",
            )
        if request.full_url.endswith("/accounts/ClientLogin"):
            if failure == "protocol":
                raise BadStatusLine(REMOTE_SECRET)
            if failure == "unauthorized":
                return http_response(request, REMOTE_SECRET.encode(), 403)
            if failure == "redirect":
                return http_response(
                    request, REMOTE_SECRET.encode(), 302, {"location": redirect}
                )
            return http_response(request, f"Auth={API_TOKEN}\n".encode())
        return http_response(request, PRIVATE_FEED.encode())

    monkeypatch.setattr(HTTPSHandler, "https_open", respond)
    result = native.run(["api-check"], input=f"reader\n{API_PASSWORD}\n")
    refused(result, diagnostic, PRIVATE_FEED, redirect)
    assert len(native.requests) == requests
