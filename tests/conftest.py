"""Shared test selection; baseline execution never requests live prerequisites."""

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--observe-cron",
        action="store_true",
        default=False,
        help="With -m live, observe two real */20 executions (20–40 minutes).",
    )


def pytest_collection_finish(session):
    if session.config.getoption("--observe-cron") and not any(
        item.get_closest_marker("live") for item in session.items
    ):
        raise pytest.UsageError(
            "--observe-cron requires explicit live test selection (-m live)"
        )
