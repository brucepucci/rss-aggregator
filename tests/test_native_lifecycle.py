"""Native lifecycle contracts through the public CLI without live resources."""

import json
import subprocess
import sys

from click.testing import CliRunner
from test_native_install import Checkout

from rss_aggregator.cli import main


def test_status_from_other_directory_uses_owning_checkout_default_state(tmp_path):
    checkout = Checkout(tmp_path)
    checkout.prepare_source()
    checkout.prepare_runtime()
    checkout.environment["RSS_STATE_DIR"] = str(checkout.root / ".local/native")
    installed = checkout.run("setup")
    assert installed.returncode == 0, installed.stderr

    caller = tmp_path / "caller"
    caller.mkdir()
    del checkout.environment["RSS_STATE_DIR"]
    result = subprocess.run(
        (
            sys.executable,
            "-I",
            "-B",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from rss_aggregator.cli import main; main(args=['status'])",
            str(checkout.root),
        ),
        cwd=caller,
        env=checkout.environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    status = json.loads(result.stdout)
    assert (status["runtime"], status["application"]) == ("installed", "stopped")
    assert not list(caller.iterdir())


def test_uninstalled_native_state_never_starts_writers(tmp_path, monkeypatch):
    monkeypatch.setenv("RSS_STATE_DIR", str(tmp_path / "fresh"))
    result = CliRunner().invoke(main, ["start"])
    assert result.exit_code != 0
    assert "rss setup" in result.stderr

    result = CliRunner().invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["runtime"] == "absent"
    assert json.loads(result.stdout)["application"] == "stopped"


def test_native_setup_does_not_adopt_legacy_ownership(tmp_path, monkeypatch):
    monkeypatch.setenv("RSS_STATE_DIR", str(tmp_path))
    legacy = tmp_path / "owner.json"
    legacy.write_text('{"machine":"rss-aggregator","token":"do-not-adopt"}')

    result = CliRunner().invoke(main, ["setup"])
    assert result.exit_code != 0
    assert "legacy" in result.stderr.lower()
    assert legacy.read_text() == '{"machine":"rss-aggregator","token":"do-not-adopt"}'
