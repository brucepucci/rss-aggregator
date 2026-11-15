"""Expose the FreshRSS lifecycle and recovery as JSON-producing commands."""

import json
from pathlib import Path
from typing import Any

import click

from rss_aggregator.runtime import Operation, Runtime, RuntimeFailure


def _execute(operation: Operation, **options: Any) -> None:
    """Emit one successful result or translate runtime/configuration errors."""
    try:
        result = Runtime().run(operation, **options)
    except (
        RuntimeFailure,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
    ) as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(result, indent=2))


@click.group()
def main() -> None:
    """Manage FreshRSS and optional private HTTPS with guarded lifecycle commands."""


@main.command()
def setup() -> None:
    """Set up the owned rootless VM without starting services."""
    _execute("setup")


@main.command()
def start() -> None:
    """Start FreshRSS after ownership and effective PHP security checks."""
    _execute("start")


@main.command()
def status() -> None:
    """Report project machine, application, and available readiness metadata."""
    _execute("status")


@main.command()
def fetch() -> None:
    """Fetch feeds immediately while respecting upstream cache and TTL rules."""
    _execute("fetch")


@main.command()
def verify() -> None:
    """Recheck effective Apache PHP security before reopening service."""
    _execute("verify")


@main.command()
def replace() -> None:
    """Replace the application container while preserving its named data volume."""
    _execute("replace")


@main.command()
def stop() -> None:
    """Back up stopped writers before stopping the project VM."""
    _execute("stop")


@main.command("api-check")
@click.option(
    "--loopback",
    is_flag=True,
    help="Use only the owned 127.0.0.1 backend; does NOT verify HTTPS. Default: trusted HTTPS.",
)
def api_check(loopback: bool) -> None:
    """Prompt locally for API credentials; emit only sanitized transport/API results."""
    if loopback:
        click.echo(
            "Loopback authentication diagnostic only; HTTPS will NOT be verified.",
            err=True,
        )
    username = click.prompt("FreshRSS user", err=True)
    password = click.prompt("Separate API password", hide_input=True, err=True)
    _execute("api-check", username=username, api_password=password, loopback=loopback)


@main.command()
def backup() -> None:
    """Create a verified external backup, preserving the prior running state."""
    _execute("backup")


@main.command()
@click.argument("archive", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--volume",
    required=True,
    help="Fresh target volume name; never overwrites or activates.",
)
def restore(archive: Path, volume: str) -> None:
    """Validate ARCHIVE and restore its data into a fresh owned volume."""
    _execute("restore", archive=archive, volume=volume)


@main.command("select-volume")
@click.argument("volume")
def select_volume(volume: str) -> None:
    """Explicitly select an owned data volume while the application is stopped."""
    _execute("select-volume", volume=volume)
