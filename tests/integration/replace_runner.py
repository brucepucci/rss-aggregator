"""Invoke real Click replacement after validating three disposable resource names.

Private live-test subprocess entrypoint: stdin contains fixture identity only.
The existing ownership directory and command lock remain authoritative; the real
runtime writes its normal security audit there. No lifecycle method is patched.
"""

import json
import os
import sys

import click

from rss_aggregator import recovery, runtime

from .replacement_support import (
    checked_application,
    checked_volume,
    owned_runtime,
    replacement_names,
)
from .support import IMAGE, expect


def run():
    expect(len(sys.argv) == 1, "Replacement runner accepts no command arguments")
    expect(os.environ.get("RSS_STATE_DIR"), "Select the existing RSS_STATE_DIR")
    request = json.load(sys.stdin)
    token, port = request["token"], request["port"]
    app, volume, check = replacement_names(token)
    expect(
        type(port) is int
        and 1024 <= port <= 65535
        and os.environ.get("RSS_PORT") == str(port),
        "Replacement runner port does not match its fixture",
    )
    controller, owner = owned_runtime()
    expect(isinstance(request["volume"], dict), "Fixture volume identity is required")
    checked_volume(controller, owner, token, request["volume"])
    application = checked_application(
        controller, owner, token, port, require_test_label=True
    )
    expect(application["State"]["Running"], "Installed fixture must be running")
    expect(
        not controller._exists("container", check),
        "Replacement fixture check-container name is occupied",
    )
    controller._podman("image", "inspect", IMAGE)
    controller._podman(
        "exec", app, "test", "-f", "/var/www/FreshRSS/data/applied_migrations.txt"
    )

    # This is the entire runtime seam. MACHINE, LABEL, ownership checks,
    # subprocess execution, locking and replacement behavior stay unchanged.
    runtime.APP, runtime.VOLUME, runtime.CHECK = app, volume, check
    runtime.SELECTION = "test-" + token + "-selection.json"
    recovery.RECOVERY_RECORD = "test-" + token + "-recovery.json"
    from rss_aggregator.cli import main

    main(args=["replace"], prog_name="rss")


if __name__ == "__main__":
    try:
        run()
    except (
        AssertionError,
        runtime.RuntimeFailure,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
    ) as error:
        click.ClickException(str(error)).show()
        raise SystemExit(1) from error
