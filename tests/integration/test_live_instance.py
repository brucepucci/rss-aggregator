"""Retained real HTTP/fetch/persistence coverage; select with pytest -m live."""

import re
import socket
import time

import pytest

from .support import Browser, expect, project

pytestmark = pytest.mark.live
CRON_COMMAND = (
    ". /var/www/FreshRSS/Docker/env.txt; "
    "su www-data -s /bin/sh -c 'php /var/www/FreshRSS/app/actualize_script.php' > /tmp/FreshRSS.log"
)


def test_cron_command_collects_a_changed_controlled_feed(instance):
    fixture = instance
    fixture.podman(
        "exec",
        "--user",
        "www-data",
        fixture.name,
        "php",
        "-r",
        'require "cli/_cli.php";$p="data/config.php";$v=require $p;$d=require "config.default.php";'
        '$v["limits"]=array_replace($d["limits"],$v["limits"]??[],["cache_duration_min"=>0]);'
        'file_put_contents($p,"<?php return ".var_export($v,true).";");',
    )
    fixture.controlled_feed(1)
    fixture.subscribe_controlled()
    fixture.podman(
        "exec",
        "--user",
        "www-data",
        fixture.name,
        "php",
        "cli/reconfigure-user.php",
        "--user",
        "testreader",
        "--key",
        "ttl_default",
        "--set",
        "--value",
        "1",
    )
    fixture.podman("exec", fixture.name, "sh", "-c", CRON_COMMAND)
    fixture.wait_article("Controlled article 1")
    fixture.controlled_feed(2)
    time.sleep(2)
    fixture.podman("exec", fixture.name, "sh", "-c", CRON_COMMAND)
    page = fixture.browser.request("/i/?a=normal&get=a&state=3")
    expect(
        "Controlled article 2" in page,
        "Completed cron command missed changed controlled feed",
    )


def test_installed_instance_can_move_to_another_loopback_port(instance):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    instance.podman(
        "exec",
        "--user",
        "www-data",
        instance.name,
        "php",
        "cli/reconfigure.php",
        "--base-url",
        base,
    )
    instance.podman("stop", "--time", "60", instance.name)
    instance.podman("rm", instance.name)
    instance.port, instance.base = port, base
    instance.start()
    instance.login()
    page = instance.browser.request("/i/?c=subscription")
    expect(
        "FreshRSS releases" in page, "Subscription missing after base URL/port change"
    )


def test_authenticated_feed_articles_settings_and_replacement(instance):
    fixture = instance
    fixture.podman(
        "exec", "--user", "www-data", fixture.name, "php", "app/actualize_script.php"
    )
    page = fixture.browser.request("/i/?a=normal&get=a&state=3")
    expect("FreshRSS releases" in page, "Public subscription missing from HTTP")
    entry = re.search(r'id="flux_(\d+)"', page)
    assert entry is not None, "Fetched public-feed articles missing from HTTP"
    entry_id = entry[1]
    csrf = Browser.csrf(page)
    fixture.browser.request(
        "/i/?c=entry&a=bookmark", {"id": entry_id, "is_favorite": "1", "_csrf": csrf}
    )
    fixture.browser.request(
        "/i/?c=entry&a=read", {"id": entry_id, "is_read": "1", "_csrf": csrf}
    )
    fixture.read_and_starred(entry_id)
    settings = fixture.browser.request("/i/?c=configure&a=archiving")
    for name in ("enable_keep_period", "enable_keep_max"):
        control = re.search(r'<input[^>]*name="' + name + r'"[^>]*>', settings)
        assert control is not None, "Archiving checkbox missing: " + name
        assert "checked" not in control[0]
    fixture.podman("stop", "--time", "60", fixture.name)
    fixture.podman("rm", fixture.name)
    fixture.start()
    fixture.login()
    expect(
        "FreshRSS releases" in fixture.browser.request("/i/?a=normal&get=a&state=3"),
        "Subscription lost after replacement",
    )
    fixture.read_and_starred(entry_id)
    fixture.podman("stop", "--time", "60", fixture.name)
    for command in ("stop", "stop", "start"):
        result = project(command)
        assert result.returncode == 0, (
            "Project lifecycle command failed: " + command + "\n" + result.stderr
        )
        if command == "stop":
            with socket.socket() as connection:
                assert connection.connect_ex(("127.0.0.1", fixture.port)) != 0
    fixture.podman("start", fixture.name)
    fixture.login()
    fixture.read_and_starred(entry_id)
    fixture.controlled_feed(1)
    fixture.subscribe_controlled()
    fixture.podman(
        "exec", "--user", "www-data", fixture.name, "php", "app/actualize_script.php"
    )
    expect(
        "Controlled article 1" in fixture.browser.request("/i/?a=normal&get=a&state=3"),
        "Controlled feed fetch failed",
    )
