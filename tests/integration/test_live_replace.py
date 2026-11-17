"""Installed-state persistence through the actual Click replacement command."""

import json
import os
import re
import subprocess
import sys
from html.parser import HTMLParser

import pytest

from .replacement_support import ReplacementInstanceFixture, volume_identity
from .support import ROOT, Browser, expect

pytestmark = pytest.mark.live


class Article(HTMLParser):
    def __init__(self, page, entry_id):
        super().__init__()
        self.entry_id = entry_id
        self.depth = 0
        self.entry_depth = None
        self.text_depth = None
        self.in_title = False
        self.classes = None
        self.title = []
        self.content = []
        self.feed(page)
        expect(self.classes is not None, "Expected article ID missing from HTTP")
        self.snapshot = (
            " ".join("".join(self.title).split()),
            " ".join("".join(self.content).split()),
        )
        expect(all(self.snapshot), "HTTP article title/content is empty")

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if tag == "div":
            self.depth += 1
            if attrs.get("id") == "flux_" + self.entry_id:
                self.entry_depth = self.depth
                self.classes = classes
            elif (
                self.entry_depth is not None
                and self.text_depth is None
                and "text" in classes
            ):
                self.text_depth = self.depth
        if (
            self.entry_depth is not None
            and self.text_depth is None
            and tag == "h1"
            and "title" in classes
        ):
            self.in_title = True

    def handle_endtag(self, tag):
        if tag == "h1":
            self.in_title = False
        if tag == "div":
            if self.text_depth == self.depth:
                self.text_depth = None
            if self.entry_depth == self.depth:
                self.entry_depth = None
            self.depth -= 1

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        if self.text_depth is not None:
            self.content.append(data)


def assert_persisted_http(fixture, entry_id, snapshot):
    expect(
        "FreshRSS releases" in fixture.browser.request("/i/?c=subscription"),
        "Public subscription missing from HTTP",
    )
    page = fixture.browser.request("/i/?a=normal&get=s&state=1")
    article = Article(page, entry_id)
    expect(article.snapshot == snapshot, "HTTP article title/content changed")
    expect("favorite" in article.classes, "HTTP article is not starred")
    expect("not_read" not in article.classes, "HTTP article is not read")
    unread = fixture.browser.request("/i/?a=normal&get=s&state=2")
    expect(
        'id="flux_' + entry_id + '"' not in unread,
        "Read article still appears in the HTTP unread filter",
    )


@pytest.fixture
def replacement_instance(live_project, tmp_path):
    fixture = ReplacementInstanceFixture(tmp_path)
    try:
        fixture.install()
        yield fixture
    finally:
        fixture.cleanup()


def test_click_replace_preserves_installed_subscription_article_and_read_star(
    replacement_instance,
):
    fixture = replacement_instance
    fixture.podman(
        "exec", "--user", "www-data", fixture.name, "php", "app/actualize_script.php"
    )
    page = fixture.browser.request("/i/?a=normal&get=a&state=3")
    match = re.search(r'id="flux_(\d+)"', page)
    expect(match is not None, "Fetched public-feed article missing from HTTP")
    entry_id = match[1]
    snapshot = Article(page, entry_id).snapshot
    csrf = Browser.csrf(page)
    for value in ("0", "1"):
        fixture.browser.request(
            "/i/?c=entry&a=bookmark",
            {"id": entry_id, "is_favorite": value, "_csrf": csrf},
        )
        fixture.browser.request(
            "/i/?c=entry&a=read",
            {"id": entry_id, "is_read": value, "_csrf": csrf},
        )
        if value == "0":
            initial = Article(
                fixture.browser.request("/i/?a=normal&get=a&state=3"), entry_id
            )
            expect(
                "not_read" in initial.classes and "favorite" not in initial.classes,
                "HTTP unread/unstarred baseline was not established",
            )
    assert_persisted_http(fixture, entry_id, snapshot)
    before_app, before_volume = fixture.resources()

    result = fixture.replace()
    expect(result.returncode == 0, "Actual Click replace failed:\n" + result.stderr)
    expect(result.stdout.startswith("{\n"), "Replacement did not emit indented JSON")
    outcome = json.loads(result.stdout)
    expect(outcome.get("replaced") is True, "Replacement result missing replaced=true")
    expect(outcome.get("url") == fixture.base, "Replacement changed the fixture URL")
    expect(
        outcome.get("fetch", {}).get("status") == "completed",
        "Replacement did not recognize and fetch the installed fixture",
    )
    after_app, after_volume = fixture.resources()
    expect(before_app["Id"] != after_app["Id"], "Replacement reused the old container")
    expect(
        volume_identity(before_volume) == volume_identity(after_volume),
        "Replacement changed the named data volume",
    )
    # login() discards the old cookie jar and authenticates a fresh HTTP session.
    fixture.login()
    assert_persisted_http(fixture, entry_id, snapshot)


def test_supervision_recovers_only_disposable_application_and_preserves_reading_state(
    replacement_instance,
):
    fixture = replacement_instance
    # Prepare the installed fixture through the production guarded CLI so its
    # container satisfies Runtime.start's listener and security requirements.
    replaced = fixture.replace()
    expect(
        replaced.returncode == 0,
        "Could not prepare production-gated fixture: " + replaced.stderr,
    )
    fixture.login()
    fixture.podman(
        "exec", "--user", "www-data", fixture.name, "php", "app/actualize_script.php"
    )
    page = fixture.browser.request("/i/?a=normal&get=a&state=3")
    match = re.search(r'id="flux_(\d+)"', page)
    expect(match is not None, "Public article missing")
    entry_id = match[1]
    snapshot = Article(page, entry_id).snapshot
    csrf = Browser.csrf(page)
    fixture.browser.request(
        "/i/?c=entry&a=bookmark", {"id": entry_id, "is_favorite": "1", "_csrf": csrf}
    )
    fixture.browser.request(
        "/i/?c=entry&a=read", {"id": entry_id, "is_read": "1", "_csrf": csrf}
    )
    before_app, before_volume = fixture.resources()
    production_intent = fixture.controller.state_dir / "operator-intent.json"
    original = production_intent.read_bytes() if production_intent.exists() else None
    result = subprocess.run(
        [sys.executable, "-m", "tests.integration.supervision_runner"],
        cwd=ROOT,
        env=dict(
            os.environ,
            RSS_STATE_DIR=str(fixture.controller.state_dir),
            RSS_PORT=str(fixture.port),
        ),
        input=json.dumps(
            {
                "token": fixture.token,
                "port": fixture.port,
                "volume": fixture.volume_record,
            }
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    expect(
        result.returncode == 0,
        "Disposable supervision recovery failed; inspect sanitized local diagnostics",
    )
    expect(
        json.loads(result.stdout) == {"healthy": "healthy", "recovery": "recovered"},
        "Unexpected recovery outcome",
    )
    after_app, after_volume = fixture.resources()
    expect(
        before_app["Id"] == after_app["Id"], "Recovery replaced the fixture container"
    )
    expect(
        volume_identity(before_volume) == volume_identity(after_volume),
        "Recovery changed the fixture volume",
    )
    expect(
        (production_intent.read_bytes() if production_intent.exists() else None)
        == original,
        "Production intent changed",
    )
    fixture.login()
    assert_persisted_http(fixture, entry_id, snapshot)
