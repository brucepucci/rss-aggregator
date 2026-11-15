"""Real security fault injection; select with pytest -m live."""

import json
import os
import subprocess
import urllib.error
import urllib.request
import uuid
from http.client import RemoteDisconnected
from pathlib import Path

import pytest

from .support import ROOT, project

pytestmark = pytest.mark.live
APP = "rss-aggregator-freshrss"
OVERRIDE = "/etc/php/8.4/apache2/conf.d/99-project-negative-check.ini"


def command(*args):
    return subprocess.run(args, text=True, capture_output=True, check=False)


def execute(*args):
    return command("podman", "--connection", "rss-aggregator", "exec", APP, *args)


def test_effective_apache_override_blocks_serving_and_removes_probe(live_project):
    state = (
        Path(os.environ.get("RSS_STATE_DIR", ROOT / ".local")).expanduser().resolve()
    )
    owner = json.loads((state / "owner.json").read_text())
    result = command(
        "podman", "--connection", "rss-aggregator", "container", "inspect", APP
    )
    assert result.returncode == 0, "Application must exist before this test"
    info = json.loads(result.stdout)[0]
    assert info["Config"]["Labels"].get("io.rss-aggregator.owner") == owner["token"]
    assert info["State"]["Running"], "Start the project before this test"
    assert execute("test", "-e", OVERRIDE).returncode == 1, (
        "Refusing to overwrite existing PHP configuration"
    )
    try:
        result = execute(
            "php",
            "-r",
            'file_put_contents($argv[1], "session.upload_progress.enabled=1\\n");',
            OVERRIDE,
        )
        assert result.returncode == 0, "Could not inject test-only override"
        result = project("start")
        assert result.returncode != 0, (
            "Security gate incorrectly accepted upload progress"
        )
        assert "upload progress is not disabled" in result.stderr
        port = info["HostConfig"]["PortBindings"]["80/tcp"][0]["HostPort"]
        try:
            urllib.request.urlopen("http://127.0.0.1:" + port + "/", timeout=2)
        except urllib.error.HTTPError:
            pytest.fail("HTTP listener still enabled after gate failure")
        except (urllib.error.URLError, OSError, RemoteDisconnected):
            pass
        else:
            pytest.fail("Application still serving after failed security gate")
        result = execute("php", "-r", 'echo count(glob("p/.project-security-*.php"));')
        assert result.returncode == 0
        assert result.stdout == "0", "Diagnostic endpoint was left behind"
    finally:
        result = execute("rm", "-f", OVERRIDE)
        assert result.returncode == 0, "Could not remove test-only override"
        # Simulate the file left by a previous interrupted/failed cleanup. The
        # retry must remove old names, not just its newly generated probe.
        token = uuid.uuid4().hex
        stale = "/var/www/FreshRSS/p/.project-security-" + token + ".php"
        unrelated = "/var/www/FreshRSS/p/.project-security-note-" + token + ".php"
        try:
            result = execute(
                "php",
                "-r",
                'file_put_contents($argv[1], "<?php echo PHP_VERSION;");'
                'file_put_contents($argv[2], "<?php // unrelated fixture");',
                stale,
                unrelated,
            )
            assert result.returncode == 0, "Could not create stale diagnostic fixture"
            result = project("start")
            assert result.returncode == 0, (
                "Application did not recover after override removal"
            )
            result = execute("test", "-e", stale)
            assert result.returncode == 1, "Retry published a stale diagnostic endpoint"
            assert execute("test", "-e", unrelated).returncode == 0, (
                "Probe cleanup removed a file outside the generated-name pattern"
            )
        finally:
            assert execute("rm", "-f", stale, unrelated).returncode == 0
