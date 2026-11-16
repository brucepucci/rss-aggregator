"""Cold VM recovery through public Runtime operations and a controlled engine."""

import json
import socket

import pytest

from rss_aggregator import Runtime, RuntimeFailure
from tests.test_supervision import supervised as supervised


@pytest.fixture
def cold_start(request, monkeypatch):
    command, engine = request.getfixturevalue("supervised")
    state = json.loads(engine.read_text())
    state["machine"]["State"] = "stopped"
    state["app"]["Id"] = "fixture-original-application"
    state["app"]["State"] = {"Running": False, "Status": "exited"}
    state["stale_pid"] = True
    state["volume_data"] = {
        "subscription": "synthetic-feed",
        "read": True,
        "starred": True,
    }
    state["calls"] = []
    engine.write_text(json.dumps(state))
    for name, value in command.env.items():
        monkeypatch.setenv(name, value)

    class FixtureSocket(socket.socket):
        def bind(self, address):
            if address != ("127.0.0.1", int(command.env["RSS_PORT"])):
                return super().bind(address)

    monkeypatch.setattr("rss_aggregator.runtime.socket.socket", FixtureSocket)
    monkeypatch.setattr("rss_aggregator.runtime.time.sleep", lambda seconds: None)
    return Runtime(state_dir=command.state), command, engine


def test_cold_vm_start_recreates_only_matching_stopped_application(cold_start):
    runtime, command, engine = cold_start
    owner = (command.state / "owner.json").read_bytes()
    assert runtime.run("start")["machine"] == "running"
    state = json.loads(engine.read_text())
    assert state["app"]["Id"] != "fixture-original-application"
    assert state["app"]["State"]["Running"] is True
    assert state["volume_data"] == {
        "subscription": "synthetic-feed",
        "read": True,
        "starred": True,
    }
    assert state["app"]["Mounts"] == [
        {"Name": "rss-aggregator-data", "Destination": "/var/www/FreshRSS/data"}
    ]
    assert (command.state / "owner.json").read_bytes() == owner
    assert runtime.run("supervision-status")["intent"] == "running"
    calls = state["calls"]
    assert ["--connection", "rss-aggregator", "rm", "rss-aggregator-freshrss"] in calls
    app_removals = [
        call
        for call in calls
        if call[2:3] == ["rm"] and call[-1] == "rss-aggregator-freshrss"
    ]
    assert not any("--volumes" in call or "--force" in call for call in app_removals)
    assert not any(call[2:4] == ["volume", "rm"] for call in calls)
    copies = [
        call[-1] for call in calls if call[2:3] == ["cp"] and "/data/" in call[-1]
    ]
    assert copies == [
        "rss-aggregator-freshrss:/var/www/FreshRSS/data/config-user.custom.php",
        "rss-aggregator-freshrss:/var/www/FreshRSS/data/opml.xml",
    ]


@pytest.mark.parametrize(
    "mismatch",
    ["owner", "image", "port", "entrypoint", "command", "listener", "volume"],
)
def test_cold_start_preserves_foreign_or_changed_application(cold_start, mismatch):
    runtime, _command, engine = cold_start
    state = json.loads(engine.read_text())
    app = state["app"]
    if mismatch == "owner":
        app["Config"]["Labels"]["io.rss-aggregator.owner"] = "foreign-owner"
    elif mismatch == "image":
        app["Image"] = "sha256:unreviewed-image"
    elif mismatch == "port":
        app["HostConfig"]["PortBindings"]["80/tcp"][0]["HostPort"] = "1"
    elif mismatch == "entrypoint":
        app["Config"]["Entrypoint"] = ["unreviewed-entrypoint"]
    elif mismatch == "command":
        app["Config"]["Cmd"] = ["unreviewed-command"]
    elif mismatch == "listener":
        app["Config"]["Env"] = []
    else:
        app["Mounts"][0]["Name"] = "another-volume"
    engine.write_text(json.dumps(state))
    with pytest.raises(RuntimeFailure):
        runtime.run("start")
    after = json.loads(engine.read_text())
    assert after["app"] == app
    assert after["volume_data"] == state["volume_data"]
    assert not any(
        call[2:3] == ["rm"] and call[-1] == "rss-aggregator-freshrss"
        for call in after["calls"]
    )


def test_warm_vm_start_reuses_matching_stopped_application(cold_start):
    runtime, _command, engine = cold_start
    state = json.loads(engine.read_text())
    state["machine"]["State"] = "running"
    state.pop("stale_pid")
    engine.write_text(json.dumps(state))
    assert runtime.run("start")["machine"] == "running"
    after = json.loads(engine.read_text())
    assert after["app"]["Id"] == "fixture-original-application"
    assert not any(call[2:3] == ["create"] for call in after["calls"])
    assert after["volume_data"] == state["volume_data"]


@pytest.mark.parametrize("changed", ["owner", "running", "command"])
def test_cold_start_refuses_changes_before_removing_application(cold_start, changed):
    runtime, _command, engine = cold_start
    state = json.loads(engine.read_text())
    state["changed_on_recheck"] = changed
    engine.write_text(json.dumps(state))
    with pytest.raises(RuntimeFailure):
        runtime.run("start")
    after = json.loads(engine.read_text())
    assert after["app"]["Id"] == "fixture-original-application"
    assert after["volume_data"] == state["volume_data"]
    assert not any(
        call[2:3] == ["rm"] and call[-1] == "rss-aggregator-freshrss"
        for call in after["calls"]
    )


def test_cold_start_retries_partial_copy_without_recreating_again(cold_start):
    runtime, _command, engine = cold_start
    state = json.loads(engine.read_text())
    state["fail_copy_once"] = "public-feed.opml"
    engine.write_text(json.dumps(state))
    with pytest.raises(RuntimeFailure):
        runtime.run("start")
    failed = json.loads(engine.read_text())
    assert failed["app"]["Id"] != "fixture-original-application"
    assert failed["app"]["State"]["Running"] is False
    assert runtime.run("start")["machine"] == "running"
    after = json.loads(engine.read_text())
    assert after["app"]["Id"] == failed["app"]["Id"]
    assert after["volume_data"] == state["volume_data"]
    assert (
        sum(
            call[2:3] == ["rm"] and call[-1] == "rss-aggregator-freshrss"
            for call in after["calls"]
        )
        == 1
    )


def test_cold_start_does_not_recreate_again_after_application_security_failure(
    cold_start,
):
    runtime, _command, engine = cold_start
    state = json.loads(engine.read_text())
    state["fail_app_probe"] = True
    engine.write_text(json.dumps(state))
    with pytest.raises(RuntimeFailure):
        runtime.run("start")
    after = json.loads(engine.read_text())
    assert after["app"]["Id"] != "fixture-original-application"
    assert after["volume_data"] == state["volume_data"]
    assert (
        sum(
            call[2:3] == ["rm"] and call[-1] == "rss-aggregator-freshrss"
            for call in after["calls"]
        )
        == 1
    )
    assert not any(
        call[2:3] == ["exec"]
        and call[3] == "rss-aggregator-freshrss"
        and "Listen 80" in call[-1]
        for call in after["calls"]
    )


def test_cold_start_preflight_failure_preserves_original_application(cold_start):
    runtime, _command, engine = cold_start
    state = json.loads(engine.read_text())
    state["fail_image"] = True
    engine.write_text(json.dumps(state))
    with pytest.raises(RuntimeFailure):
        runtime.run("start")
    after = json.loads(engine.read_text())
    assert after["app"] == state["app"]
    assert after["volume_data"] == state["volume_data"]
    assert not any(
        call[2:3] == ["rm"] and call[-1] == "rss-aggregator-freshrss"
        for call in after["calls"]
    )
