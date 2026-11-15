"""Real Mac/Podman fixtures, invoked only by explicitly selected live tests."""

import html
import http.cookiejar
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LABEL = "io.rss-aggregator.test"
IMAGE = json.loads((ROOT / "config/image.json").read_text())["image"]


def expect(condition, message):
    if not condition:
        raise AssertionError(message)


def project(*args):
    result = subprocess.run(
        [sys.executable, "-m", "rss_aggregator", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        assert result.stdout.startswith("{\n"), (
            "Project command did not emit indented JSON"
        )
        json.loads(result.stdout)
    else:
        assert not result.stdout, "Failed command emitted a result"
    return result


class Browser:
    def __init__(self, base):
        self.base = base
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def request(self, path="/i/", fields=None):
        data = None if fields is None else urllib.parse.urlencode(fields).encode()
        with self.opener.open(self.base + path, data=data, timeout=90) as response:
            return response.read().decode()

    @staticmethod
    def csrf(page):
        match = re.search(r'name="_csrf"\s+value="([^"]+)"', page)
        if not match:
            match = re.search(r'"csrf"\s*:\s*"([^"]+)"', page)
        assert match, "FreshRSS CSRF token missing"
        return html.unescape(match[1])


class InstanceFixture:
    def __init__(self, temp):
        self.token = secrets.token_hex(8)
        self.name = "rss-aggregator-test-" + self.token
        self.volume = self.name + "-data"
        self.temp = temp
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def call(self, *args):
        return subprocess.run(
            ["podman", "--connection", "rss-aggregator", *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def podman(self, *args, input=None):
        result = subprocess.run(
            ["podman", "--connection", "rss-aggregator", *args],
            input=input,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"Podman fixture operation failed: {args[0]} ({result.returncode})"
        )
        return result.stdout

    def install(self):
        self.podman(
            "volume", "create", "--label", LABEL + "=" + self.token, self.volume
        )
        self.start()
        browser = Browser(self.base)
        browser.request("/i/index.php")
        browser.request("/i/index.php?step=0", {"language": "en"})
        page = browser.request("/i/index.php?step=2", {"type": "sqlite"})
        assert 'name="default_user"' in page, "HTTP SQLite installer step failed"
        self.password = secrets.token_urlsafe(32)
        page = browser.request(
            "/i/index.php?step=3",
            {
                "default_user": "testreader",
                "auth_type": "form",
                "passwordPlain": self.password,
            },
        )
        assert "installation process was successful" in page, (
            "HTTP installer did not complete"
        )
        browser.request("/i/index.php?step=5")
        self.login()

    def login(self):
        self.browser = Browser(self.base)
        page = self.browser.request()
        assert 'id="passwordPlain"' in page, (
            "Unauthenticated HTTP did not require login"
        )
        nonce = json.loads(
            self.browser.request("/i/?c=javascript&a=nonce&user=testreader")
        )
        challenge = self.podman(
            "exec",
            "--interactive",
            self.name,
            "php",
            "-r",
            "$v=json_decode(stream_get_contents(STDIN),true);"
            '$s=crypt($v["password"],$v["salt1"]);'
            'echo password_hash($s.$v["nonce"], PASSWORD_BCRYPT, ["cost"=>4]);',
            input=json.dumps(dict(nonce, password=self.password)),
        )
        page = self.browser.request(
            "/i/?c=auth&a=login",
            {
                "username": "testreader",
                "challenge": challenge,
                "_csrf": Browser.csrf(page),
            },
        )
        assert 'id="passwordPlain"' not in page, "HTTP login did not succeed"

    def start(self):
        self.podman(
            "create",
            "--name",
            self.name,
            "--label",
            LABEL + "=" + self.token,
            "--pull=never",
            "--publish",
            f"127.0.0.1:{self.port}:80",
            "--mount",
            f"type=volume,source={self.volume},destination=/var/www/FreshRSS/data",
            "--env",
            "CRON_MIN=*/20",
            "--env",
            "ENABLE_ACCESS_LOG=0",
            "--env",
            "COPY_LOG_TO_SYSLOG=Off",
            "--env",
            "COPY_SYSLOG_TO_STDERR=Off",
            IMAGE,
        )
        self.podman(
            "cp",
            str(ROOT / "config/config-user.custom.php"),
            self.name + ":/var/www/FreshRSS/data/config-user.custom.php",
        )
        self.podman(
            "cp",
            str(ROOT / "config/public-feed.opml"),
            self.name + ":/var/www/FreshRSS/data/opml.xml",
        )
        custom = self.temp / "config.custom.php"
        custom.write_text(
            "<?php return ['internal_host_allowlist' => ['127.0.0.1:80']];\n"
        )
        self.podman(
            "cp", str(custom), self.name + ":/var/www/FreshRSS/data/config.custom.php"
        )
        publisher = self.temp / "fixture-publisher.conf"
        publisher.write_text(
            '<Files "project-fixture.atom">\nExpiresActive Off\n'
            'Header unset Cache-Control\nHeader always set Cache-Control "no-cache, max-age=0"\n</Files>\n'
        )
        self.podman(
            "cp",
            str(publisher),
            self.name + ":/etc/apache2/conf-enabled/fixture-publisher.conf",
        )
        self.podman("start", self.name)
        for _ in range(30):
            try:
                Browser(self.base).request()
                return
            except (OSError, TimeoutError):
                time.sleep(1)
        raise AssertionError("Fixture HTTP not ready")

    def cleanup(self):
        for kind, name in [("container", self.name), ("volume", self.volume)]:
            existence = self.call(kind, "exists", name)
            if existence.returncode == 1:
                continue
            assert existence.returncode == 0, (
                "Fixture cleanup could not determine resource state"
            )
            info = json.loads(self.podman(kind, "inspect", name))[0]
            labels = (
                info.get("Labels", {})
                if kind == "volume"
                else info["Config"].get("Labels", {})
            )
            assert (
                name.startswith("rss-aggregator-test-")
                and labels.get(LABEL) == self.token
            ), "Refusing fixture cleanup: ownership mismatch"
            if kind == "container":
                self.podman("stop", "--time", "60", name)
                self.podman("rm", name)
            else:
                self.podman("volume", "rm", name)

    def read_and_starred(self, entry_id):
        page = self.browser.request("/i/?a=normal&get=s&state=1")
        assert 'id="flux_' + entry_id + '"' in page, (
            "Read/starred article state not preserved"
        )

    def controlled_feed(self, number):
        path = self.temp / "controlled.atom"
        entries = "".join(
            f"<entry><id>urn:project:fixture:{i}</id><title>Controlled article {i}</title>"
            "<updated>2026-11-14T00:00:00Z</updated><content>Deterministic test content</content>"
            "</entry>"
            for i in range(1, number + 1)
        )
        path.write_text(
            '<feed xmlns="http://www.w3.org/2005/Atom"><id>urn:project:fixture</id>'
            "<title>Controlled fixture</title><updated>2026-11-14T00:00:00Z</updated>"
            + entries
            + "</feed>"
        )
        stamp = time.time() + number
        os.utime(path, (stamp, stamp))
        self.podman(
            "cp", str(path), self.name + ":/var/www/FreshRSS/p/project-fixture.atom"
        )
        with Browser(self.base).opener.open(
            self.base + "/project-fixture.atom", timeout=5
        ) as response:
            publisher = response.read().decode()
            assert response.headers.get("Cache-Control") == "no-cache, max-age=0", (
                "Controlled publisher must not advertise asset caching"
            )
        assert f"Controlled article {number}" in publisher, (
            "Controlled publisher did not expose new content"
        )

    def subscribe_controlled(self):
        opml = self.temp / "controlled.opml"
        opml.write_text(
            '<opml version="2.0"><head><title>Test</title></head><body>'
            '<outline text="Controlled fixture" type="rss" '
            'xmlUrl="http://127.0.0.1/project-fixture.atom" /></body></opml>'
        )
        self.podman("cp", str(opml), self.name + ":/tmp/controlled.opml")
        self.podman(
            "exec",
            "--user",
            "www-data",
            self.name,
            "php",
            "cli/import-for-user.php",
            "--user",
            "testreader",
            "--filename",
            "/tmp/controlled.opml",
        )

    def wait_article(self, title):
        for _ in range(30):
            if title in self.browser.request("/i/?a=normal&get=a&state=3"):
                return
            time.sleep(1)
        raise AssertionError("Fetching did not collect " + title)
