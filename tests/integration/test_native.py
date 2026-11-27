"""Explicit Apple Silicon check with synthetic data and real pinned PHP/Caddy."""

import json
import os
import platform
import socket
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

import pytest

pytestmark = pytest.mark.live
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def synthetic_feed():
    class Publisher(BaseHTTPRequestHandler):
        def do_GET(self):
            items = "".join(
                f"<item><title>Fixture {n}</title><guid isPermaLink='false'>item-{n}</guid>"
                f"<link>https://example.invalid/item-{n}</link><description>Content {n}</description></item>"
                for n in (1, 2)
            )
            payload = (
                "<rss version='2.0'><channel><title>Synthetic publisher</title>"
                "<link>https://example.invalid</link><description>Test fixture</description>"
                f"{items}</channel></rss>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass

    with HTTPServer(("127.0.0.1", 0), Publisher) as server:
        worker = Thread(target=server.serve_forever)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/feed"
        finally:
            server.shutdown()
            worker.join()


def test_native_pin_http_api_and_source_absent_restore(tmp_path, synthetic_feed):
    if sys.platform != "darwin" or platform.machine() != "arm64" or os.getuid() == 0:
        pytest.fail("Native integration requires a normal Apple Silicon macOS account.")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        assert port != 8080, "The disposable test must not use the default/live port."
    first = tmp_path / "first"
    second = tmp_path / "second"
    backups = tmp_path / "archives"
    backups.mkdir(mode=0o700)
    base = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(tmp_path),
        "RSS_BACKUP_DIR": str(backups),
        "RSS_PORT": str(port),
        "RSS_TAILSCALE": "0",
    }

    def invoke(state, *arguments, data=None, success=True):
        result = subprocess.run(
            [sys.executable, "-I", "-m", "rss_aggregator", *arguments],
            text=True,
            capture_output=True,
            input=data,
            cwd=ROOT,
            env={**base, "RSS_STATE_DIR": str(state)},
            timeout=210,
            check=False,
        )
        if success:
            assert result.returncode == 0, f"{arguments[0]}: {result.stderr}"
            return json.loads(result.stdout)
        return result

    def upstream(state, *args):
        data = state / "instances/default/data"
        command = subprocess.run(
            [str(state / "runtime/frankenphp"), "php-cli", *args],
            cwd=state / "runtime/FreshRSS",
            capture_output=True,
            timeout=30,
            check=False,
            env={
                **base,
                "DATA_PATH": str(data),
                "PHPRC": str(state / "service/php.ini"),
                "PHP_INI_SCAN_DIR": "",
            },
        )
        assert command.returncode == 0, (
            f"Disposable FreshRSS fixture command failed: {args[0]}"
        )

    def api(state):
        result = invoke(
            state, "api-check", "--loopback", data="synthetic\nfixture-api-only\n"
        )
        assert result["transport"] == "loopback"
        assert result["https_verified"] is False
        assert "fixture-api-only" not in json.dumps(result)

    def reader(path, *, token=None, form=None):
        headers = {"Authorization": f"GoogleLogin auth={token}"} if token else {}
        request = Request(
            f"http://127.0.0.1:{port}/api/greader.php/{path}",
            data=urlencode(form).encode() if form else None,
            headers=headers,
        )
        with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
            return response.read()

    def login():
        answer = reader(
            "accounts/ClientLogin",
            form={"Email": "synthetic", "Passwd": "fixture-api-only"},
        )
        return next(
            line.removeprefix("Auth=")
            for line in answer.decode().splitlines()
            if line.startswith("Auth=")
        )

    def items(auth):
        result = json.loads(
            reader(
                "reader/api/0/stream/contents/user/-/state/com.google/reading-list?n=20&output=json",
                token=auth,
            )
        )
        return {item["title"]: item for item in result["items"]}

    try:
        initial = invoke(first, "setup")
        assert invoke(first, "setup") == initial
        start = invoke(first, "start")
        assert start["fetch"]["status"] == "installer_pending"
        assert start["security"]["verified"] is True
        assert start["scheduler"]["cron"] == "*/20 * * * *"
        assert invoke(first, "stop")["backup"]["verified"] is True
        opml = first / "instances/default/data/opml.xml"
        opml.write_text(
            f"<opml version='1.0'><head><title>Fixture</title></head><body>"
            f"<outline title='Synthetic publisher' type='rss' xmlUrl='{synthetic_feed}'/>"
            "</body></opml>"
        )
        address = synthetic_feed.split("/", 3)[2]
        (opml.parent / "config.custom.php").write_text(
            "<?php return ['internal_host_allowlist' => ['" + address + "']];\n"
        )
        upstream(
            first,
            "cli/do-install.php",
            "--default-user",
            "synthetic",
            "--db-type",
            "sqlite",
            "--base-url",
            f"http://127.0.0.1:{port}",
            "--auth-type",
            "form",
            "--api-enabled=true",
        )
        upstream(
            first,
            "cli/create-user.php",
            "--user",
            "synthetic",
            "--password",
            "fixture-web-only",
            "--api-password",
            "fixture-api-only",
        )
        serving = invoke(first, "start")
        assert serving["security"]["authenticated_settings"] is True
        assert serving["fetch"]["status"] == "completed"
        assert invoke(first, "start")["fetch"]["status"] == "already_running"
        assert invoke(first, "fetch")["status"] == "completed"
        api(first)
        auth = login()
        subscriptions = json.loads(
            reader("reader/api/0/subscription/list?output=json", token=auth)
        )
        assert any(
            sub["url"] == synthetic_feed for sub in subscriptions["subscriptions"]
        )
        before = items(auth)
        assert {"Fixture 1", "Fixture 2"} <= before.keys()
        write_token = reader("reader/api/0/token", token=auth).decode()
        assert (
            reader(
                "reader/api/0/edit-tag",
                token=auth,
                form={
                    "i": before["Fixture 1"]["id"],
                    "T": write_token,
                    "a": "user/-/state/com.google/read",
                },
            ).strip()
            == b"OK"
        )
        assert (
            reader(
                "reader/api/0/edit-tag",
                token=auth,
                form={
                    "i": before["Fixture 1"]["id"],
                    "T": write_token,
                    "a": "user/-/state/com.google/starred",
                },
            ).strip()
            == b"OK"
        )
        capture = invoke(first, "backup")
        assert capture["verified"] is True
        assert invoke(first, "status")["http_ready"] is True
        assert invoke(first, "verify")["verified"] is True
        invoke(first, "stop")
        (opml.parent / "config.custom.php").write_text(
            "<?php return ['auth_type' => 'none', 'allow_anonymous' => true];\n"
        )
        repaired = invoke(first, "start")
        assert repaired["security"]["authenticated_settings"] is True
        invoke(first, "stop")
        (opml.parent / "applied_migrations.txt").rename(
            opml.parent / "missing-marker.fixture"
        )
        refused = invoke(first, "start", success=False)
        assert (
            refused.returncode != 0 and "configuration is incomplete" in refused.stderr
        )
        assert invoke(first, "status")["application"] == "stopped"
        first.rename(tmp_path / "source-not-at-expected-path")
        invoke(second, "setup")
        target = invoke(
            second, "restore", capture["archive"], "--instance", "recovered"
        )
        assert target["selected"] is False
        assert invoke(
            second,
            "restore",
            capture["archive"],
            "--instance",
            "recovered",
            success=False,
        ).returncode
        assert invoke(second, "select-instance", "recovered")["instance"] == "recovered"
        assert invoke(second, "start")["instance"] == "recovered"
        api(second)
        restored = items(login())
        for n in (1, 2):
            title = f"Fixture {n}"
            assert restored[title]["id"] == before[title]["id"]
            assert f"Content {n}" in restored[title]["summary"]["content"]
            categories = restored[title]["categories"]
            assert any(tag.endswith("/read") for tag in categories) is (n == 1)
            assert any(tag.endswith("/starred") for tag in categories) is (n == 1)
        shut = invoke(second, "stop")
        assert shut["backup"]["verified"] is True
        plan = invoke(second, "uninstall")
        invoke(second, "uninstall", "--confirm", plan["confirmation"])
        assert invoke(second, "status")["runtime"] == "absent"
        assert (second / "instances/recovered/data/config.php").is_file()
        assert Path(capture["archive"]).is_file()
    finally:
        for state in (first, second):
            if (state / "runtime").is_dir() and (state / "service.json").is_file():
                result = invoke(state, "stop", success=False)
                status = invoke(state, "status")
                assert status["application"] == "stopped", result.stderr
