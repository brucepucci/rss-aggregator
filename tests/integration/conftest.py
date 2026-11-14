"""Explicit live prerequisites and failure-safe, test-owned resources."""

import json
import shutil
import sys

import pytest

from .support import InstanceFixture, project


@pytest.fixture(scope="session")
def live_project():
    if sys.platform != "darwin":
        pytest.fail(
            "These live checks require the Mac Podman VM; #7 owns native Linux integration."
        )
    if shutil.which("podman") is None:
        pytest.fail(
            "Podman CLI unavailable; follow README prerequisites before selecting -m live."
        )
    result = project("status")
    if result.returncode or json.loads(result.stdout).get("machine") != "running":
        pytest.fail(
            "Start the owned project before selecting -m live; use the existing RSS_STATE_DIR, not a new ownership record."
        )


@pytest.fixture
def instance(live_project, tmp_path):
    resource = InstanceFixture(tmp_path)
    try:
        resource.install()
        yield resource
    finally:
        resource.cleanup()
