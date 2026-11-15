"""Recover authenticated state with the source fixture's entire volume absent."""

import json
import os
import re
import subprocess
import sys

import pytest

from rss_aggregator import runtime

from .replacement_support import (
    ReplacementInstanceFixture,
    owned_runtime,
    volume_identity,
)
from .support import ROOT, Browser, expect, project
from .test_live_replace import Article, assert_persisted_http

pytestmark = pytest.mark.live


def recovery_command(fixture, directory, operation, archive=None):
    result = subprocess.run(
        [sys.executable, "-m", "tests.integration.backup_runner"],
        cwd=ROOT,
        env=dict(
            os.environ,
            RSS_STATE_DIR=str(fixture.controller.state_dir),
            RSS_PORT=str(fixture.port),
            RSS_BACKUP_DIR=str(directory),
        ),
        input=json.dumps(
            {"token": fixture.token, "operation": operation, "archive": archive}
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    expect(
        result.returncode == 0, "Production recovery operation failed: " + result.stderr
    )
    expect(result.stdout.startswith("{\n"), "Recovery did not emit indented JSON")
    return json.loads(result.stdout)


def test_backup_recovers_http_state_after_source_volume_is_removed(
    live_project, tmp_path
):
    fixture = ReplacementInstanceFixture(tmp_path)
    controller, owner = owned_runtime()
    target = fixture.volume + "-recovered"
    selection = controller.state_dir / ("test-" + fixture.token + "-selection.json")
    records = controller.state_dir / ("test-" + fixture.token + "-recovery.json")
    fixture_locator = controller.state_dir / (
        "test-" + fixture.token + "-last-backup.json"
    )
    locator = controller.state_dir / "last-backup.json"
    original_locator = locator.read_bytes() if locator.exists() else None
    original_selection = (
        controller.selected_volume_path.read_bytes()
        if controller.selected_volume_path.exists()
        else None
    )
    original_app = (
        controller._check_label("container", runtime.APP, owner)
        if controller._exists("container", runtime.APP)
        else None
    )
    original_running = bool(original_app and original_app["State"]["Running"])
    source_removed = False
    target_record = None
    # tmp_path is a private host directory outside the checkout, never an artifact.
    directory = tmp_path / "private-backups"
    try:
        expect(
            not controller._exists("volume", target),
            "Recovery test target already exists",
        )
        if original_running:
            controller._podman("stop", "--time", "60", runtime.APP)
        fixture.install()
        replaced = fixture.replace()
        expect(
            replaced.returncode == 0,
            "Could not prepare production-gated fixture: " + replaced.stderr,
        )
        fixture.login()
        page = fixture.browser.request("/i/?a=normal&get=a&state=3")
        entries = re.findall(r'id="flux_(\d+)"', page)
        expect(
            len(entries) >= 2, "Need multiple public articles for read/unread recovery"
        )
        read_id, unread_id = entries[:2]
        snapshot = Article(page, read_id).snapshot
        unread_snapshot = Article(page, unread_id).snapshot
        csrf = Browser.csrf(page)
        fixture.browser.request(
            "/i/?c=entry&a=bookmark", {"id": read_id, "is_favorite": "1", "_csrf": csrf}
        )
        fixture.browser.request(
            "/i/?c=entry&a=read", {"id": read_id, "is_read": "1", "_csrf": csrf}
        )
        fixture.browser.request(
            "/i/?c=entry&a=read", {"id": unread_id, "is_read": "0", "_csrf": csrf}
        )
        assert_persisted_http(fixture, read_id, snapshot)

        report = recovery_command(fixture, directory, "backup")
        expect(report["verified"], "Backup was not verified")
        fixture.login()
        assert_persisted_http(fixture, read_id, snapshot)
        app = controller._check_label("container", fixture.name, owner)
        expect(
            app["State"]["Running"], "Explicit backup did not resume the running source"
        )
        fixture.podman("stop", "--time", "60", fixture.name)
        stopped = recovery_command(fixture, directory, "backup")
        expect(stopped["verified"], "Stopped-source backup failed")
        expect(
            not controller._check_label("container", fixture.name, owner)["State"][
                "Running"
            ],
            "Backup started a stopped source",
        )

        # Destroy only the leased disposable source. Successful recovery can no
        # longer accidentally read its databases, files or original container.
        fixture.cleanup()
        source_removed = True
        expect(
            not controller._exists("volume", fixture.volume),
            "Source volume is still available",
        )
        restored = recovery_command(fixture, directory, "restore", report["archive"])
        expect(
            restored["restored"] and not restored["selected"],
            "Restore activated itself",
        )
        info = controller._check_label("volume", target, owner)
        target_record = volume_identity(info)
        current_selection = (
            controller.selected_volume_path.read_bytes()
            if controller.selected_volume_path.exists()
            else None
        )
        expect(
            current_selection == original_selection,
            "Recovery changed the user's selected volume",
        )
        recovery_command(fixture, directory, "select-volume")
        recovery_command(fixture, directory, "start")
        fixture.login()
        assert_persisted_http(fixture, read_id, snapshot)
        unread = fixture.browser.request("/i/?a=normal&get=a&state=2")
        restored_unread = Article(unread, unread_id)
        expect(
            restored_unread.snapshot == unread_snapshot
            and "not_read" in restored_unread.classes,
            "Unread article content/state was not recovered",
        )
    finally:
        if not source_removed:
            fixture.cleanup()
        elif controller._exists("volume", target):
            info = controller._check_label("volume", target, owner)
            expect(
                records.exists() and target in json.loads(records.read_text()),
                "Missing recovery test lease",
            )
            if target_record is not None:
                expect(
                    volume_identity(info) == target_record,
                    "Recovered volume identity changed",
                )
            if controller._exists("container", fixture.name):
                app = controller._check_label("container", fixture.name, owner)
                expect(
                    any(
                        m.get("Name") == target
                        and m["Destination"] == "/var/www/FreshRSS/data"
                        for m in app["Mounts"]
                    ),
                    "Refusing non-recovery container cleanup",
                )
                expect(
                    app["HostConfig"]["PortBindings"].get("80/tcp")
                    == [{"HostIp": "127.0.0.1", "HostPort": str(fixture.port)}],
                    "Recovery fixture port changed",
                )
                controller._podman("stop", "--time", "60", fixture.name)
                controller._podman("rm", fixture.name)
            expect(
                not controller._exists("container", fixture.check),
                "Interrupted preflight retained; inspect before manual cleanup",
            )
            controller._podman("volume", "rm", target)
        selection.unlink(missing_ok=True)
        records.unlink(missing_ok=True)
        fixture_locator.unlink(missing_ok=True)
        if original_running:
            result = project("start")
            expect(
                result.returncode == 0,
                "Could not restore the original application after recovery test",
            )
        expect(
            (locator.read_bytes() if locator.exists() else None) == original_locator,
            "Recovery fixture changed the user's backup locator",
        )
