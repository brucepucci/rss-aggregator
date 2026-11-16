"""Expose the FreshRSS lifecycle and recovery as JSON-producing commands."""

import json
from pathlib import Path
from typing import Any

import click

from rss_aggregator.runtime import Operation, Runtime, RuntimeFailure


def _execute(operation: Operation, **options: Any) -> dict[str, Any]:
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
    return result


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


@main.command("supervision-install")
def supervision_install() -> None:
    """Explicitly install/activate Mac user-session startup and recovery."""
    _execute("supervision-install")


@main.command("supervision-remove")
def supervision_remove() -> None:
    """Remove only owned supervision; preserve application, intent and data."""
    _execute("supervision-remove")


@main.command("supervision-tick")
def supervision_tick() -> None:
    """Check health once and perform bounded guarded recovery if authorized."""
    _execute("supervision-tick")


@main.command("supervision-status")
def supervision_status() -> None:
    """Report supervision, persistent intent and actionable recovery state."""
    _execute("supervision-status")


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


@main.command("backup-enable")
@click.option(
    "--hour",
    type=click.IntRange(0, 23),
    default=3,
    show_default=True,
    help="Daily UTC hour.",
)
@click.option(
    "--minute",
    type=click.IntRange(0, 59),
    default=0,
    show_default=True,
    help="Daily UTC minute.",
)
def backup_enable(hour: int, minute: int) -> None:
    """Explicitly opt into a daily per-user Mac backup LaunchAgent."""
    _execute("backup-enable", hour=hour, minute=minute)


@main.command("backup-disable")
def backup_disable() -> None:
    """Remove only the unchanged owned daily job; preserve archives and evidence."""
    _execute("backup-disable")


@main.command("backup-due")
def backup_due() -> None:
    """Run eligible daily work once; launchd wakeups are bounded and serialized."""
    _execute("backup-due")


@main.command("backup-reconcile")
def backup_reconcile() -> None:
    """Confirm owned writers idle and clear interrupted maintenance; never resume."""
    _execute("backup-reconcile")


@main.command("backup-status")
@click.option(
    "--check",
    is_flag=True,
    help="Emit status, then exit 1 unless current daily protection is healthy.",
)
def backup_status(check: bool) -> None:
    """Report daily protection, latest attempts, freshness and maintenance problems."""
    result = _execute("backup-status")
    if check and not result["protected"]:
        click.echo(
            "Daily backup protection needs attention; inspect the JSON status and backup recovery procedure.",
            err=True,
        )
        raise click.exceptions.Exit(1)


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
