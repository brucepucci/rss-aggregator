"""Run production recovery commands with only reserved fixture names redirected."""

import json
import os
import sys

import click

from rss_aggregator import runtime

from .replacement_support import (
    owned_runtime,
    redirect_fixture_names,
    replacement_names,
)
from .support import expect


def run():
    request = json.load(sys.stdin)
    token = request["token"]
    _app, source, _check = replacement_names(token)
    target = source + "-recovered"
    controller, _owner = owned_runtime()
    expect(
        os.environ.get("RSS_STATE_DIR") == str(controller.state_dir),
        "Use the real existing owner directory",
    )
    expect(
        request["operation"]
        in {"backup", "backup-status", "restore", "select-volume", "start"},
        "Unsupported recovery test operation",
    )
    # Ownership, locking, security and the global maintenance hold stay real.
    # All fixture evidence/intent aliases match the replacement/supervision runners.
    redirect_fixture_names(token)
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
