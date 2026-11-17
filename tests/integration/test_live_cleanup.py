"""Opt-in full VM recreation drill; never use the owner's existing project VM."""

import re
import secrets
import shutil
import sys
from pathlib import Path

import pytest

from rss_aggregator import Runtime, backup_schedule, runtime, supervision

from .support import Browser, InstanceFixture, expect
from .test_live_replace import Article, assert_persisted_http

pytestmark = pytest.mark.live


class CleanupInstance(InstanceFixture):
    """Install a synthetic user only inside this drill's newly owned machine."""

    def __init__(self, directory, controller):
        super().__init__(directory)
        self.controller = controller
        self.name, self.volume = runtime.APP, runtime.VOLUME

    def podman(self, *args, input=None):
        if args[:2] == ("volume", "create") or args[:1] == ("create",):
            owner, _machine = self.controller._owned()
            args = (
                *args[:-1],
                "--label",
                runtime.LABEL + "=" + owner["token"],
                args[-1],
            )
        result = self.controller._podman(*args, input=input, check=False)
        expect(
            result.returncode == 0,
            "Disposable VM fixture operation failed; inspect retained state locally",
        )
        return result.stdout


def test_disposable_vm_remove_recreate_restore_preserves_authenticated_state(
    tmp_path, monkeypatch
):
    expect(sys.platform == "darwin", "This explicitly selected drill requires macOS")
    expect(
        all(shutil.which(name) for name in ("podman", "brew")),
        "This drill requires preinstalled Podman and Homebrew; it never installs host packages",
    )
    # Refuse before mutation rather than stopping or borrowing the real VM.
    controller = Runtime(state_dir=tmp_path / "ownership")
    initial = controller._inventory()
    expect(
        not any(m.get("Running") for m in initial["machines"]),
        "Coordinate an idle-host window: another VM is running; nothing was stopped or created",
    )
    expect(
        "podman" in controller._packages(),
        "Podman must already be installed through Homebrew",
    )
    token = secrets.token_hex(8)
    name = "rss-aggregator-test-cleanup-" + token
    expect(
        not any(m["Name"] == name for m in initial["machines"]),
        "Disposable machine name collision",
    )
    expect(
        not any(c["Name"] in (name, name + "-root") for c in initial["connections"]),
        "Disposable connection collision",
    )
    for key, value in {
        "MACHINE": name,
        "APP": name + "-app",
        "CHECK": name + "-check",
        "VOLUME": name + "-data",
    }.items():
        monkeypatch.setattr(runtime, key, value)
    # No real LaunchAgents are installed/removed or mistaken for fixture jobs.
    monkeypatch.setattr(
        supervision, "JOB_LABEL", "io.rss-aggregator.test." + token + ".supervision"
    )
    monkeypatch.setattr(
        backup_schedule, "LABEL", "io.rss-aggregator.test." + token + ".backup"
    )
    monkeypatch.setenv("RSS_TAILSCALE", "0")
    monkeypatch.setenv("RSS_BACKUP_DIR", str(tmp_path / "private-backups"))
    fixture = CleanupInstance(tmp_path, controller)
    monkeypatch.setenv("RSS_PORT", str(fixture.port))
    completed = False
    try:
        expect(controller.run("setup")["created"], "Did not create a fresh owned VM")
        controller.run("start")
        # Replace only the fresh empty app with the HTTP installer fixture. The
        # named volume already exists from production startup; retain its label.
        owner, _machine = controller._owned()
        controller._check_label("container", runtime.APP, owner)
        fixture.podman("stop", "--time", "60", runtime.APP)
        fixture.podman("rm", runtime.APP)
        fixture.start()
        browser = Browser(fixture.base)
        browser.request("/i/index.php")
        browser.request("/i/index.php?step=0", {"language": "en"})
        page = browser.request("/i/index.php?step=2", {"type": "sqlite"})
        expect('name="default_user"' in page, "Synthetic HTTP installer failed")
        fixture.password = secrets.token_urlsafe(32)
        page = browser.request(
            "/i/index.php?step=3",
            {
                "default_user": "testreader",
                "auth_type": "form",
                "passwordPlain": fixture.password,
            },
        )
        expect(
            "installation process was successful" in page,
            "Synthetic user installation failed",
        )
        browser.request("/i/index.php?step=5")
        controller.run("replace")
        fixture.login()
        page = fixture.browser.request("/i/?a=normal&get=a&state=3")
        entries = re.findall(r'id="flux_(\d+)"', page)
        expect(
            len(entries) >= 2,
            "Need multiple fixture articles for read/unread verification",
        )
        read_id, unread_id = entries[:2]
        snapshot, unread_snapshot = (
            Article(page, read_id).snapshot,
            Article(page, unread_id).snapshot,
        )
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
        plan = controller.run("remove-vm")
        removed = controller.run("remove-vm", confirm=plan["confirmation"])
        expect(removed["backup"]["verified"], "Missing verified external archive")
        expect(
            controller.run("remove-vm")["status"] == "absent",
            "Source VM is still present",
        )
        original_owner = owner["token"]
        controller.run("setup")
        restored = controller.run(
            "restore",
            archive=Path(removed["backup"]["archive"]),
            volume=name + "-restored",
        )
        expect(
            restored["restored"] and not restored["selected"],
            "Restore activated itself",
        )
        controller.run("select-volume", volume=name + "-restored")
        controller.run("start")
        new_owner, _machine = controller._owned()
        expect(
            new_owner["token"] != original_owner,
            "Recreation reused removed VM ownership",
        )
        fixture.login()
        assert_persisted_http(fixture, read_id, snapshot)
        unread = Article(
            fixture.browser.request("/i/?a=normal&get=a&state=2"), unread_id
        )
        expect(
            unread.snapshot == unread_snapshot
            and "not_read" in unread.classes
            and "favorite" not in unread.classes,
            "Unread/unstarred content/state was not restored",
        )
        plan = controller.run("remove-vm")
        controller.run("remove-vm", confirm=plan["confirmation"])
        final = controller._inventory()
        expect(
            final["connections"] == initial["connections"],
            "Unrelated connections/defaults changed",
        )
        expect(
            sorted(m["Name"] for m in final["machines"])
            == sorted(m["Name"] for m in initial["machines"]),
            "Unrelated machine inventory changed",
        )
        completed = True
    finally:
        if not completed:
            # A failed destructive drill is not permission to skip backup or
            # broaden cleanup. Retain namespaced VM/state/archives for inspection.
            print(
                "Disposable cleanup drill incomplete; retain and inspect its VM and private ownership directory locally: "
                + str(controller.state_dir),
                file=sys.stderr,
            )
