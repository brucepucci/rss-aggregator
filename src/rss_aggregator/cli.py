"""Expose the FreshRSS runtime as seven JSON-producing Click commands."""

import json

import click

from rss_aggregator.runtime import Operation, Runtime, RuntimeFailure


def _execute(operation: Operation) -> None:
    """Emit one successful result or translate runtime/configuration errors."""
    try:
        result = Runtime().run(operation)
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
    """Manage the local-only FreshRSS instance with guarded lifecycle commands."""


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
    """Stop project writers and VM without creating an external backup."""
    _execute("stop")
