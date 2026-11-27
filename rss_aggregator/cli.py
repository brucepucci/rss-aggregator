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
    """Verify and install the pinned native runtime without starting services."""
    _execute("setup")


@main.command()
def start() -> None:
    """Start FreshRSS after ownership and effective PHP security checks."""
    _execute("start")


@main.command()
def status() -> None:
    """Report the native installation, active instance, readiness and scheduling."""
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
    """Recheck effective web PHP security before reopening service."""
    _execute("verify")


@main.command()
def stop() -> None:
    """Stop writers and verify a full instance backup, even if already stopped."""
    _execute("stop")


@main.command()
@click.option("--confirm", help="Apply only this SHA-256 preview confirmation.")
def uninstall(confirm: str | None) -> None:
    """Preview owned-runtime removal; preserve instance data and backups."""
    _execute("uninstall", confirm=confirm)


@main.command()
@click.option("--confirm", help="Apply only this SHA-256 preview confirmation.")
def upgrade(confirm: str | None) -> None:
    """Preview an explicit upgrade to this checkout's reviewed runtime pin."""
    _execute("upgrade", confirm=confirm)


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
    "--instance",
    required=True,
    help="Fresh target instance name; never overwrites or activates.",
)
def restore(archive: Path, instance: str) -> None:
    """Validate a native ARCHIVE and restore it into a fresh owned instance."""
    _execute("restore", archive=archive, instance=instance)


@main.command("select-instance")
@click.argument("instance")
def select_instance(instance: str) -> None:
    """Explicitly select an owned instance while all writers are stopped."""
    _execute("select-instance", instance=instance)


@main.command("import-legacy", hidden=True)
@click.argument("archive", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--instance", required=True, help="Fresh native target; never activates.")
def import_legacy(archive: Path, instance: str) -> None:
    """One-time migration: import a verified Podman ARCHIVE into a fresh instance."""
    _execute("import-legacy", archive=archive, instance=instance)
