"""Explicit Apple Silicon check with synthetic data and real pinned PHP/Caddy."""

import json
import os
import platform
import re
import socket
import subprocess
import sys
import time
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Event, Thread
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

import pytest

pytestmark = pytest.mark.live
ROOT = Path(__file__).resolve().parents[2]
FEED_TTL = 1  # Seconds; used by both FreshRSS and the synthetic publisher's cache.
FETCH_PERIOD = 20 * 60  # Seconds between UTC scheduler slots.
FIXTURE_USER = "synthetic"
WEB_PASSWORD = "fixture-web-only"
API_PASSWORD = "fixture-api-only"
WEB_FORM_CHALLENGE = (
    "$v = json_decode(stream_get_contents(STDIN), true, flags: JSON_THROW_ON_ERROR);"
    '$hash = crypt($v["password"], $v["salt1"]);'
    "if (strlen($hash) !== 60) { exit(1); }"
    'echo password_hash($hash . $v["nonce"], PASSWORD_BCRYPT, ["cost" => 4]);'
)
OBSERVE_STARTUP_GATE = """
from rss_aggregator import native_service
from rss_aggregator.cli import main

web_check = native_service.web_check

def observe(port, path, *, host=None):
    if path == '/__rss_probe.php':
        assert web_check(port, '/i/')[0] == 503, 'Web traffic reached unapproved startup'
        assert web_check(port, '/api/greader.php/reader/api/0/subscription/list')[0] == 503
    return web_check(port, path, host=host)

native_service.web_check = observe
main()
"""


@pytest.fixture
def synthetic_feed():
    third_published = Event()

    class Publisher(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/feed":
                self.send_error(404)
                return
            origin = f"http://127.0.0.1:{self.server.server_port}"
            items = "".join(
                f"<item><title>Fixture {n}</title><guid isPermaLink='false'>item-{n}</guid>"
                f"<link>{origin}/item-{n}</link><description>Content {n}</description></item>"
                for n in ((1, 2, 3) if third_published.is_set() else (1, 2))
            )
            payload = (
                "<rss version='2.0'><channel><title>Synthetic publisher</title>"
                f"<link>{origin}</link><description>Test fixture</description>"
                f"{items}</channel></rss>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml")
            self.send_header("Cache-Control", f"max-age={FEED_TTL}")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass

    with HTTPServer(("127.0.0.1", 0), Publisher) as server:
        worker = Thread(target=server.serve_forever)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/feed", third_published.set
        finally:
            server.shutdown()
            worker.join()


def test_native_pin_http_api_and_source_absent_restore(tmp_path, synthetic_feed):
    if sys.platform != "darwin" or platform.machine() != "arm64" or os.getuid() == 0:
        pytest.fail("Native integration requires a normal Apple Silicon macOS account.")
    feed_url, publish_third = synthetic_feed
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

    def invoke(state, *arguments, data=None, success=True, umask=-1, observe=False):
        launcher = ["-c", OBSERVE_STARTUP_GATE] if observe else ["-m", "rss_aggregator"]
        result = subprocess.run(
            [sys.executable, "-I", *launcher, *arguments],
            text=True,
            capture_output=True,
            input=data,
            cwd=ROOT,
            env={**base, "RSS_STATE_DIR": str(state)},
            timeout=210,
            umask=umask,
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
            umask=0o077,
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
            state, "api-check", "--loopback", data=f"{FIXTURE_USER}\n{API_PASSWORD}\n"
        )
        assert result["transport"] == "loopback"
        assert result["https_verified"] is False
        assert API_PASSWORD not in json.dumps(result)

    def reader(path, *, token=None, form=None):
        headers = {"Authorization": f"GoogleLogin auth={token}"} if token else {}
        request = Request(
            f"http://127.0.0.1:{port}/api/greader.php/{path}",
            data=urlencode(form).encode() if form else None,
            headers=headers,
        )
        with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
            return response.read()

    def api_login():
        answer = reader(
            "accounts/ClientLogin",
            form={"Email": FIXTURE_USER, "Passwd": API_PASSWORD},
        )
        return next(
            line.removeprefix("Auth=")
            for line in answer.decode().splitlines()
            if line.startswith("Auth=")
        )

    def assert_restored_web_login(state):
        browser = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))

        def web(path, fields=None):
            data = urlencode(fields).encode() if fields is not None else None
            with browser.open(
                f"http://127.0.0.1:{port}{path}", data, timeout=5
            ) as reply:
                return reply.read().decode()

        login_form = web("/i/")
        assert 'id="passwordPlain"' in login_form
        csrf = re.search(r'name="_csrf"\s+value="([0-9a-f]+)"', login_form)
        assert csrf, "FreshRSS web login did not provide a CSRF token"
        nonce = json.loads(web(f"/i/?c=javascript&a=nonce&user={FIXTURE_USER}"))
        # Implement the pinned browser's double-bcrypt challenge without browser
        # dependencies or reading the restored password hash from disk.
        challenge = subprocess.run(
            [
                str(state / "runtime/frankenphp"),
                "php-cli",
                "-n",
                "-r",
                WEB_FORM_CHALLENGE,
            ],
            input=json.dumps({**nonce, "password": WEB_PASSWORD}),
            capture_output=True,
            text=True,
            env=base,
            timeout=30,
            check=False,
        )
        assert challenge.returncode == 0, (
            "Could not compute the synthetic web challenge"
        )
        web(
            "/i/?c=auth&a=login",
            {"username": FIXTURE_USER, "challenge": challenge.stdout, "_csrf": csrf[1]},
        )
        subscriptions = web("/i/?c=subscription&a=index")
        assert 'id="passwordPlain"' not in subscriptions
        assert "Synthetic publisher" in subscriptions

    def assert_fixture_subscription(auth):
        result = json.loads(
            reader("reader/api/0/subscription/list?output=json", token=auth)
        )
        assert [sub["url"] for sub in result["subscriptions"]] == [feed_url]

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
        # Real CLI prepare, web and startup templates must remain archivable
        # even when the operator's shell permits group writes.
        start = invoke(first, "start", umask=0o002, observe=True)
        assert start["fetch"]["status"] == "installer_pending"
        assert start["security"]["verified"] is True
        assert start["scheduler"]["cron"] == "*/20 * * * *"
        assert invoke(first, "stop")["backup"]["verified"] is True
        data = first / "instances/default/data"
        opml = data / "opml.xml"
        config = data / "config.custom.php"
        # Unlike the seeded OPML, this file is created by the fixture.
        config.touch(mode=0o600, exist_ok=False)
        opml.write_text(
            f"<opml version='1.0'><head><title>Fixture</title></head><body>"
            f"<outline title='Synthetic publisher' type='rss' xmlUrl='{feed_url}'/>"
            "</body></opml>"
        )
        address = feed_url.split("/", 3)[2]
        config.write_text(
            f"<?php return ['internal_host_allowlist' => ['{address}'], "
            f"'limits' => ['cache_duration' => {FEED_TTL}, "
            f"'cache_duration_min' => {FEED_TTL}]];\n"
        )
        upstream(
            first,
            "cli/do-install.php",
            "--default-user",
            FIXTURE_USER,
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
            FIXTURE_USER,
            "--password",
            WEB_PASSWORD,
            "--api-password",
            API_PASSWORD,
            "--feed-ttl-default",
            str(FEED_TTL),
        )
        # Leave a quiet window for startup and manual fetch, so the periodic
        # fetch cannot be mistaken for the command that collected Fixture 3.
        until_slot = FETCH_PERIOD - time.time() % FETCH_PERIOD
        if until_slot <= 15:
            time.sleep(until_slot + 0.25)
        startup_slot = int(time.time()) // FETCH_PERIOD
        serving = invoke(first, "start", observe=True)
        startup_finished = time.monotonic()
        assert serving["security"]["authenticated_settings"] is True
        assert serving["fetch"]["status"] == "completed"
        assert invoke(first, "start")["fetch"]["status"] == "already_running"
        api(first)
        auth = api_login()
        assert_fixture_subscription(auth)
        assert items(auth).keys() == {"Fixture 1", "Fixture 2"}

        # Let the real TTL and HTTP cache expire without modifying their state.
        # Upstream compares integer Unix seconds with a strict expiry boundary.
        time.sleep(max(0, startup_finished + FEED_TTL + 1 - time.monotonic()))
        publish_third()
        assert "Fixture 3" not in items(auth)
        assert invoke(first, "fetch")["status"] == "completed"
        fetched = items(auth)
        assert fetched.keys() == {"Fixture 1", "Fixture 2", "Fixture 3"}
        assert "Content 3" in fetched["Fixture 3"]["summary"]["content"]
        assert int(time.time()) // FETCH_PERIOD == startup_slot, (
            "A periodic fetch slot overlapped startup and manual fetching."
        )

        write_token = reader("reader/api/0/token", token=auth).decode()
        assert (
            reader(
                "reader/api/0/edit-tag",
                token=auth,
                form={
                    "i": fetched["Fixture 1"]["id"],
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
                    "i": fetched["Fixture 1"]["id"],
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
        config.write_text(
            "<?php return ['auth_type' => 'none', 'allow_anonymous' => true];\n"
        )
        repaired = invoke(first, "start")
        assert repaired["security"]["authenticated_settings"] is True
        invoke(first, "stop")
        (data / "applied_migrations.txt").rename(data / "missing-marker.fixture")
        refused = invoke(first, "start", success=False)
        assert (
            refused.returncode != 0 and "configuration is incomplete" in refused.stderr
        )
        assert invoke(first, "status")["application"] == "stopped"
        first.rename(tmp_path / "source-not-at-expected-path")
        assert not first.exists()
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
        assert_restored_web_login(second)
        api(second)
        restored_auth = api_login()
        assert_fixture_subscription(restored_auth)
        restored = items(restored_auth)
        for n in (1, 2, 3):
            title = f"Fixture {n}"
            assert restored[title]["id"] == fetched[title]["id"]
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
