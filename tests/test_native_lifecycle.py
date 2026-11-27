"""Native lifecycle contracts through the public CLI without live resources."""

import json

from click.testing import CliRunner

from rss_aggregator.cli import main


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
