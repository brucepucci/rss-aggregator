"""Narrow installed-state replacement fixture using the existing machine owner."""

import json
import os
import re
import subprocess
import sys

from rss_aggregator import runtime

from .support import LABEL, ROOT, InstanceFixture, expect


def replacement_names(token):
    expect(
        isinstance(token, str) and re.fullmatch(r"[0-9a-f]{16}", token),
        "Invalid replacement fixture token",
    )
    app = "rss-aggregator-test-" + token
    names = app, app + "-data", app + "-check"
    expect(
        not set(names).intersection((runtime.APP, runtime.VOLUME, runtime.CHECK)),
        "Refusing production replacement resource names",
    )
    return names


def owned_runtime():
    controller = runtime.Runtime()
    expect(controller.root == ROOT, "Replacement runner imported another checkout")
    owner, machine = controller._owned()
    expect(machine["State"] == "running", "Start the owned VM before this live test")
    expect(
        isinstance(owner.get("token"), str) and bool(owner["token"]),
        "Existing owner token is missing",
    )
    return controller, owner


def volume_identity(info):
    return {key: info[key] for key in ("Name", "CreatedAt", "Mountpoint")}


def checked_volume(controller, owner, token, expected_identity=None):
    _, name, _ = replacement_names(token)
    info = controller._check_label("volume", name, owner)
    expect(
        info["Name"] == name and (info.get("Labels") or {}).get(LABEL) == token,
        "Refusing replacement fixture volume: test ownership mismatch",
    )
    if expected_identity is not None:
        expect(
            volume_identity(info) == expected_identity,
            "Replacement fixture volume identity changed",
        )
    return info


def checked_application(controller, owner, token, port, require_test_label=False):
    name, volume, _ = replacement_names(token)
    info = controller._check_label("container", name, owner)
    labels = info["Config"].get("Labels") or {}
    expect(
        info["Name"].lstrip("/") == name
        and labels.get(LABEL) in (None, token)
        and (not require_test_label or labels.get(LABEL) == token),
        "Refusing replacement fixture container: name/test ownership mismatch",
    )
    mounts = [
        mount
        for mount in info["Mounts"]
        if mount["Destination"] == "/var/www/FreshRSS/data"
    ]
    expect(
        len(mounts) == 1
        and mounts[0].get("Type") == "volume"
        and mounts[0].get("Name") == volume
        and not any(mount.get("Name") == runtime.VOLUME for mount in info["Mounts"]),
        "Refusing replacement fixture container: data mount mismatch",
    )
    expect(
        info["HostConfig"]["PortBindings"].get("80/tcp")
        == [{"HostIp": "127.0.0.1", "HostPort": str(port)}],
        "Refusing replacement fixture container: loopback publication mismatch",
    )
    return info


class ReplacementInstanceFixture(InstanceFixture):
    def __init__(self, temp):
        self.controller, self.owner = owned_runtime()
        super().__init__(temp)
        self.name, self.volume, self.check = replacement_names(self.token)
        self.reserved = False
        self.volume_record = None

    def podman(self, *args, input=None):
        # Only the existing fixture's initial create calls gain a second label.
        # Production replacement never calls this test helper.
        volume_create = args[:2] == ("volume", "create") and args[-1] == self.volume
        app_create = args[:3] == ("create", "--name", self.name)
        if volume_create or app_create:
            args = (
                *args[:-1],
                "--label",
                runtime.LABEL + "=" + self.owner["token"],
                args[-1],
            )
        result = super().podman(*args, input=input)
        if volume_create:
            self.volume_record = volume_identity(
                checked_volume(self.controller, self.owner, self.token)
            )
        return result

    def check_names(self):
        expect(
            (self.name, self.volume, self.check) == replacement_names(self.token),
            "Replacement fixture resource names changed",
        )

    def install(self):
        self.check_names()
        for kind, name in (
            ("container", self.name),
            ("container", self.check),
            ("volume", self.volume),
        ):
            expect(
                not self.controller._exists(kind, name),
                "Replacement fixture name already exists; nothing was adopted",
            )
        self.reserved = True
        super().install()

    def resources(self):
        volume = checked_volume(
            self.controller, self.owner, self.token, self.volume_record
        )
        app = checked_application(self.controller, self.owner, self.token, self.port)
        return app, volume

    def replace(self):
        result = subprocess.run(
            [sys.executable, "-m", "tests.integration.replace_runner"],
            cwd=ROOT,
            env=dict(
                os.environ,
                RSS_STATE_DIR=str(self.controller.state_dir),
                RSS_PORT=str(self.port),
            ),
            input=json.dumps(
                {
                    "token": self.token,
                    "port": self.port,
                    "volume": self.volume_record,
                }
            ),
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            expect(not result.stdout, "Failed replacement emitted a result")
        return result

    def cleanup(self):
        if not self.reserved:
            return
        self.check_names()
        controller, owner = owned_runtime()
        expect(
            controller.state_dir == self.controller.state_dir
            and owner["token"] == self.owner["token"],
            "Refusing fixture cleanup: existing ownership state changed",
        )
        if not controller._exists("volume", self.volume):
            expect(
                not controller._exists("container", self.name)
                and not controller._exists("container", self.check),
                "Refusing fixture cleanup without the retained test-labelled volume",
            )
            return
        # The retained test lease anchors cleanup even if the subprocess failed
        # after creating a production-labelled container but before returning JSON.
        checked_volume(controller, owner, self.token, self.volume_record)
        for name in (self.check, self.name):
            if not controller._exists("container", name):
                continue
            if name == self.name:
                info = checked_application(controller, owner, self.token, self.port)
            else:
                info = controller._check_label("container", name, owner)
                expect(
                    info["Name"].lstrip("/") == self.check
                    and (info["Config"].get("Labels") or {}).get(LABEL)
                    in (None, self.token)
                    and not info["HostConfig"].get("PortBindings")
                    and not any(
                        mount.get("Name") in (self.volume, runtime.VOLUME)
                        for mount in info["Mounts"]
                    ),
                    "Refusing fixture cleanup: disposable check container differs",
                )
            if info["State"]["Running"]:
                self.podman("stop", "--time", "60", name)
            self.podman("rm", *(["--volumes"] if name == self.check else []), name)
        checked_volume(controller, owner, self.token, self.volume_record)
        self.podman("volume", "rm", self.volume)
        for suffix in ("selection.json", "recovery.json"):
            (controller.state_dir / ("test-" + self.token + "-" + suffix)).unlink(
                missing_ok=True
            )
