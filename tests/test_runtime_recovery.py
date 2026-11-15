"""Exercise retry behavior through the callable lifecycle and a controlled engine."""

import json
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import MagicMock
from urllib.request import ProxyHandler, build_opener

import pytest

from rss_aggregator.runtime import APP, CHECK, LABEL, VOLUME, Runtime, RuntimeFailure

ROOT = Path(__file__).resolve().parents[1]


class Engine:
    def __init__(self, runtime, port=8080):
        self.runtime = runtime
        self.port = port
        self.app = None
        self.volume = False
        self.probes = set()
        self.templates = {}
        self.calls = []
        self.fail_copy = None
        self.fail_cleanup = False
        self.ignore_cleanup = False
        self.published = False
        image = runtime._image()
        self.image = {
            "Id": "sha256:verified-image",
            "Os": "linux",
            "Architecture": "arm64",
            "RepoDigests": [
                image["image"],
                image["image"].split("@")[0] + "@" + image["platform_manifest"],
            ],
            "Labels": {
                "org.opencontainers.image.version": image["version"],
                "org.opencontainers.image.revision": image["revision"],
            },
            "Config": {"Entrypoint": ["./Docker/entrypoint.sh"], "Cmd": ["run"]},
        }

    def run(self, *args, check=True, input=None):
        self.calls.append(args)
        assert args[:3] == ("podman", "--connection", "rss-aggregator")
        args = args[3:]
        output = ""
        status = 0
        if args[:2] == ("image", "inspect"):
            output = json.dumps([self.image])
        elif args[:2] == ("container", "exists"):
            status = 0 if args[2] == APP and self.app else 1
        elif args[:2] == ("volume", "exists"):
            status = 0 if self.volume else 1
        elif args[:2] == ("volume", "create"):
            self.volume = True
        elif args[:2] == ("volume", "inspect"):
            output = json.dumps([{"Labels": {LABEL: "test-owner"}}])
        elif args[:2] == ("container", "inspect"):
            assert args[2] == APP
            output = json.dumps([self.app])
        elif args[0] == "create":
            self.app = {
                "Config": {
                    **self.image["Config"],
                    "Labels": {LABEL: "test-owner"},
                    "Env": ["LISTEN=127.0.0.1:8088"],
                },
                "Image": self.image["Id"],
                "State": {"Running": False},
                "HostConfig": {
                    "PortBindings": {
                        "80/tcp": [
                            {"HostIp": "127.0.0.1", "HostPort": str(self.port)},
                        ]
                    }
                },
                "Mounts": [{"Name": VOLUME, "Destination": "/var/www/FreshRSS/data"}],
            }
        elif args[0] in ("run", "rm"):
            assert CHECK in args
        elif args[0] == "cp":
            destination = args[2]
            if destination.startswith(APP + ":"):
                if "/p/.project-security-" in destination:
                    self.probes.add(destination.split("/p/")[1])
                elif self.fail_copy == Path(args[1]).name:
                    self.fail_copy = None
                    status = 125
                else:
                    self.templates[Path(destination).name] = Path(args[1]).read_bytes()
        elif args[0] == "start":
            self.app["State"]["Running"] = True
            self.published = False
        elif args[0] == "stop":
            self.app["State"]["Running"] = False
        elif args[0] == "exec":
            code = args[-1]
            if 'file_get_contents("http:' in code:
                output = json.dumps(
                    {
                        "php": "8.4.27",
                        "sapi": "apache2handler",
                        "upload_progress": "0",
                    }
                )
            elif code == "echo PHP_VERSION;":
                output = "8.4.27"
            elif "scandir" in code:
                if self.fail_cleanup:
                    self.fail_cleanup = False
                    status = 125
                elif self.ignore_cleanup:
                    # Model a cleanup that cannot establish absence, not success.
                    status = 1
                else:
                    self.probes = {
                        p
                        for p in self.probes
                        if not (
                            p.startswith(".project-security-") and p.endswith(".php")
                        )
                    }
            elif "Listen 80" in code:
                self.published = True
                assert not any(p.startswith(".project-security-") for p in self.probes)
            elif "apache2 -k graceful" not in code:
                raise AssertionError(f"Unexpected engine execution: {args}")
        else:
            raise AssertionError(f"Unexpected engine operation: {args}")
        if status and check:
            raise RuntimeFailure("Injected transport/cleanup failure")
        return subprocess.CompletedProcess(args, status, output, "injected failure")


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    runtime = Runtime(root=ROOT, state_dir=tmp_path)
    engine = Engine(runtime)
    # This suite tests lifecycle recovery, not the separately covered port guard.
    monkeypatch.setattr("rss_aggregator.runtime.socket.socket", MagicMock())
    monkeypatch.setattr(runtime, "_run", engine.run)
    monkeypatch.setattr(
        runtime, "_owned", lambda: ({"token": "test-owner"}, {"State": "running"})
    )
    monkeypatch.setattr(runtime, "_ready", lambda port: None)
    monkeypatch.setattr(runtime, "_scheduler", lambda owner: {"running": True})
    monkeypatch.setattr(runtime, "_fetch", lambda: {"status": "completed"})
    monkeypatch.setenv("RSS_PORT", "8080")
    return runtime, engine


@pytest.mark.parametrize("template", ["config-user.custom.php", "public-feed.opml"])
def test_start_resumes_both_template_copies_after_partial_initialization(
    lifecycle, template
):
    runtime, engine = lifecycle
    engine.fail_copy = template
    with pytest.raises(RuntimeFailure, match="Injected"):
        runtime.run("start")
    assert engine.app is not None
    assert not engine.app["State"]["Running"]
    assert not engine.published
    engine.calls.clear()
    assert runtime.run("start")["machine"] == "running"
    assert engine.templates == {
        "config-user.custom.php": (ROOT / "config/config-user.custom.php").read_bytes(),
        "opml.xml": (ROOT / "config/public-feed.opml").read_bytes(),
    }
    assert not any(call[3] == "create" for call in engine.calls)
    assert [
        call[5] for call in engine.calls if call[3] == "cp" and "/data/" in call[5]
    ] == [
        APP + ":/var/www/FreshRSS/data/config-user.custom.php",
        APP + ":/var/www/FreshRSS/data/opml.xml",
    ]
    assert engine.published


def test_failed_probe_cleanup_is_recovered_before_retry_publication(lifecycle):
    runtime, engine = lifecycle
    engine.fail_cleanup = True
    with pytest.raises(RuntimeFailure, match="Injected"):
        runtime.run("start")
    assert engine.probes and not engine.published
    engine.probes.add("unrelated.php")
    assert runtime.run("start")["machine"] == "running"
    assert engine.probes == {"unrelated.php"}
    assert engine.published


def test_unverifiable_probe_cleanup_never_publishes(lifecycle):
    runtime, engine = lifecycle
    engine.ignore_cleanup = True
    for _ in range(2):
        with pytest.raises(RuntimeFailure, match="Injected"):
            runtime.run("start")
        assert not engine.published
        assert engine.probes


@pytest.fixture
def http_readiness(tmp_path, monkeypatch):
    requests = {"attempts": 0, "disconnects": 1}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests["attempts"] += 1
            if requests["attempts"] <= requests["disconnects"]:
                self.close_connection = True
                return
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        port = server.server_address[1]
        runtime = Runtime(root=ROOT, state_dir=tmp_path)
        engine = Engine(runtime, port=port)
        # Model an already running owned container, so start reaches readiness
        # without treating our disposable HTTP server as a port collision.
        engine.run("podman", "--connection", "rss-aggregator", "create")
        engine.app["State"]["Running"] = True
        monkeypatch.setattr(runtime, "_run", engine.run)
        monkeypatch.setattr(
            runtime, "_owned", lambda: ({"token": "test-owner"}, {"State": "running"})
        )
        scheduler = MagicMock(return_value={"running": True})
        fetch = MagicMock(return_value={"status": "completed"})
        sleep = MagicMock()
        monkeypatch.setattr(runtime, "_scheduler", scheduler)
        monkeypatch.setattr(runtime, "_fetch", fetch)
        # Keep the controlled loopback server independent of ambient proxies.
        monkeypatch.setattr(
            "rss_aggregator.runtime.urlopen", build_opener(ProxyHandler({})).open
        )
        monkeypatch.setattr("rss_aggregator.runtime.time.sleep", sleep)
        monkeypatch.setenv("RSS_PORT", str(port))
        yield runtime, engine, requests, scheduler, fetch, sleep
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_start_retries_real_http_disconnect_before_scheduler_and_fetch(http_readiness):
    runtime, engine, requests, scheduler, fetch, sleep = http_readiness
    result = runtime.run("start")
    assert requests["attempts"] == 2
    sleep.assert_called_once_with(1)
    scheduler.assert_called_once()
    fetch.assert_called_once()
    assert result["fetch"] == {"status": "completed"}
    assert engine.published


def test_start_bounds_persistent_http_disconnects_and_retains_vm(http_readiness):
    runtime, engine, requests, scheduler, fetch, sleep = http_readiness
    requests["disconnects"] = 30
    with pytest.raises(RuntimeFailure, match="VM retained for diagnosis"):
        runtime.run("start")
    assert requests["attempts"] == 30
    assert sleep.call_count == 30
    scheduler.assert_not_called()
    fetch.assert_not_called()
    assert engine.app["State"]["Running"]
    assert not any(call[1:3] == ("machine", "stop") for call in engine.calls)


@pytest.mark.parametrize("disconnects", [1, 30])
def test_start_retries_connection_resets_with_the_same_bound(
    http_readiness, monkeypatch, disconnects
):
    runtime, engine, requests, scheduler, fetch, sleep = http_readiness
    requests["disconnects"] = 0
    from rss_aggregator.runtime import urlopen

    attempts = []

    def reset_then_open(*args, **kwargs):
        attempts.append(args)
        if len(attempts) <= disconnects:
            raise ConnectionResetError("Controlled connection reset")
        return urlopen(*args, **kwargs)

    monkeypatch.setattr("rss_aggregator.runtime.urlopen", reset_then_open)
    if disconnects == 30:
        with pytest.raises(RuntimeFailure, match="VM retained for diagnosis"):
            runtime.run("start")
        assert len(attempts) == 30
        assert sleep.call_count == 30
        scheduler.assert_not_called()
        fetch.assert_not_called()
    else:
        assert runtime.run("start")["fetch"] == {"status": "completed"}
        assert len(attempts) == 2
        sleep.assert_called_once_with(1)
        scheduler.assert_called_once()
        fetch.assert_called_once()
    assert engine.published
