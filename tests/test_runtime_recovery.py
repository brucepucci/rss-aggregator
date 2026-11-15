"""Exercise retry behavior through the callable lifecycle and a controlled engine."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from rss_aggregator.runtime import APP, CHECK, LABEL, VOLUME, Runtime, RuntimeFailure

ROOT = Path(__file__).resolve().parents[1]


class Engine:
    def __init__(self, runtime):
        self.runtime = runtime
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
                            {"HostIp": "127.0.0.1", "HostPort": "8080"},
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
