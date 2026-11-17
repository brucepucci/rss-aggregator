"""Previously private supervision retains executable and LocalAPI dependencies."""

import copy
import json
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest
from test_supervision import registration

from rss_aggregator import Runtime
from rss_aggregator.serve_config import ServeConfigClient
from tests import test_private_access

private_runtime = test_private_access.private_runtime


@pytest.fixture
def loopback_supervision(private_runtime, tmp_path, monkeypatch, request):
    runtime, engine = private_runtime
    runtime.run("start")
    monkeypatch.setenv("RSS_TAILSCALE", "0")
    runtime.run("start")
    record = runtime.state_dir / "serve.json"
    assert json.loads(record.read_text())["phase"] == "inactive"
    assert engine.config == engine.original

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    command = registration(home)
    requests = []

    class LocalAPI(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            assert self.path == "/localapi/v0/serve-config"
            self.send_response(200)
            self.send_header("Etag", "fixture-loopback")
            self.end_headers()
            self.wfile.write(json.dumps(engine.config).encode())

        def log_message(self, *args):
            pass

    # Keep the synthetic LocalAPI away from the application's free loopback port.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", engine.port))
        server = HTTPServer(("127.0.0.1", 0), LocalAPI)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    command.tool(
        "tailscale",
        f"""import json, sys
args = sys.argv[1:]
if args == ['status', '--json']:
 print(json.dumps({{'BackendState':'Running','Self':{{'DNSName':'reader.example.ts.net.'}},'CertDomains':['reader.example.ts.net']}}))
elif args == ['serve', 'status', '--json']:
 print({json.dumps(engine.config)!r})
elif args == ['debug', 'local-creds']:
 print('curl -u:fixture http://localhost:{server.server_port}/localapi/v0/status')
else: raise SystemExit('UNEXPECTED TAILSCALE MUTATION')
""",
    )
    tailscale = command.bin / "tailscale"
    if getattr(request, "param", "separate") == "separate":
        directory = home / "tailscale-bin"
        directory.mkdir()
        tailscale = tailscale.rename(directory / "tailscale")
    command.env.update(
        PATH=str(tailscale.parent) + ":" + str(command.bin),
        RSS_PORT=str(engine.port),
        RSS_STATE_DIR=str(runtime.state_dir),
        RSS_TAILSCALE="0",
    )
    for key, value in command.env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("rss_aggregator.supervision.sys.platform", "darwin")
    # Only Podman stays controlled in-process. Exercise actual executable lookup,
    # the captured environment, CLI discovery and the production LocalAPI client.
    monkeypatch.setattr(
        "rss_aggregator.private_access.ServeConfigClient", ServeConfigClient
    )

    def run(*args, check=True, input=None, inherit_lock=True):
        if args[0] == "podman":
            return engine.run(*args, check=check, input=input)
        return Runtime._run(
            runtime, *args, check=check, input=input, inherit_lock=inherit_lock
        )

    monkeypatch.setattr(runtime, "_run", run)
    try:
        yield runtime, engine, tailscale, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.mark.parametrize("loopback_supervision", ["shared", "separate"], indirect=True)
def test_loopback_supervision_captures_retained_forwarding_and_recovers(
    loopback_supervision, monkeypatch
):
    runtime, engine, tailscale, requests = loopback_supervision
    record = runtime.state_dir / "serve.json"
    before = record.read_bytes()
    assert runtime.run("status")["forwarding"]["status"] == "stopped"
    assert runtime.run("supervision-install")["state"] == "healthy"
    captured = json.loads((runtime.state_dir / "supervision.json").read_text())
    assert record.read_bytes() == before

    # The interactive PATH is no longer usable: status/recovery must use capture.
    monkeypatch.setenv("PATH", "/unavailable-interactive-path")
    assert runtime.run("supervision-status")["state"] == "healthy"
    assert runtime.run("supervision-tick")["state"] == "healthy"
    engine.app["State"].update(Running=False, Status="exited")
    assert runtime.run("supervision-tick")["state"] == "recovered"
    assert engine.app["State"]["Running"] is True
    assert engine.config == engine.original
    assert json.loads(record.read_text())["phase"] == "inactive"
    assert requests and all(path == "/localapi/v0/serve-config" for path in requests)
    assert runtime.run("supervision-status")["state"] == "healthy"
    assert captured["dependencies"]["tailscale"] == str(tailscale)
    assert str(tailscale.parent) in captured["environment"]["PATH"].split(":")
    assert runtime.run("supervision-remove")["enabled"] is False


@pytest.mark.parametrize("when", ["installation", "after_capture"])
def test_loopback_supervision_refuses_missing_retained_tailscale(
    loopback_supervision, when
):
    runtime, engine, tailscale, requests = loopback_supervision
    if when == "after_capture":
        runtime.run("supervision-install")
    tailscale.unlink()
    before = copy.deepcopy(engine.app)
    record = (runtime.state_dir / "serve.json").read_bytes()
    if when == "installation":
        with pytest.raises(ValueError, match="tailscale"):
            runtime.run("supervision-install")
        assert not (runtime.state_dir / "supervision.json").exists()
    else:
        assert runtime.run("supervision-status")["state"] == "configuration_unavailable"
        with pytest.raises(ValueError):
            runtime.run("supervision-tick")
    assert engine.app == before
    assert (runtime.state_dir / "serve.json").read_bytes() == record
    assert not requests
