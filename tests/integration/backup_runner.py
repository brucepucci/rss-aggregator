"""Run production recovery commands with only reserved fixture names redirected."""

import json
import os
import sys

import click

from rss_aggregator import private_access, recovery, runtime, supervision

from .replacement_support import owned_runtime, replacement_names
from .support import expect


def run():
    request = json.load(sys.stdin)
    token = request["token"]
    app, source, check = replacement_names(token)
    target = source + "-recovered"
    controller, _owner = owned_runtime()
    expect(
        os.environ.get("RSS_STATE_DIR") == str(controller.state_dir),
        "Use the real existing owner directory",
    )
    expect(
        request["operation"] in {"backup", "restore", "select-volume", "start"},
        "Unsupported recovery test operation",
    )
    runtime.APP, runtime.VOLUME, runtime.CHECK = app, source, check
    # Keep ownership/lock/security enforcement real, but never change the user's
    # selected volume, recovery records or backup locator. No method is replaced.
    runtime.SELECTION = "test-" + token + "-selection.json"
    recovery.RECOVERY_RECORD = "test-" + token + "-recovery.json"
    recovery.BACKUP_RECORD = "test-" + token + "-last-backup.json"
    private_access.SERVE_RECORD = "test-" + token + "-serve.json"
    supervision.INTENT = "test-" + token + "-operator-intent.json"
    supervision.REGISTRATION = "test-" + token + "-supervision.json"
    supervision.RECOVERY_STATE = "test-" + token + "-supervision-recovery.json"
    os.environ["RSS_TAILSCALE"] = "0"
    operation = request["operation"]
    args = [operation]
    if operation == "restore":
        args += [request["archive"], "--volume", target]
    elif operation == "select-volume":
        args += [target]
    from rss_aggregator.cli import main

    main(args=args, prog_name="rss")


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
