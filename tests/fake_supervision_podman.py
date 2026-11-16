"""Disposable stateful Podman executable for public supervision operations."""

import json
import os
import sys
from pathlib import Path

path = Path(os.environ["HOME"]) / "engine.json"
state = json.loads(path.read_text())
args = sys.argv[1:]
state["calls"].append(args)


def save():
    path.write_text(json.dumps(state))


def fail():
    save()
    raise SystemExit("private-feed://synthetic-secret must not reach status/logs")


save()
if args == ["machine", "inspect", "rss-aggregator"]:
    print(json.dumps([state["machine"]]))
elif args == ["machine", "list", "--format", "json"]:
    print(
        json.dumps(
            [
                {
                    "Name": "rss-aggregator",
                    "Running": state["machine"]["State"] == "running",
                }
            ]
        )
    )
elif args == ["system", "connection", "list", "--format", "json"]:
    print(json.dumps([state["connection"]]))
elif args[:2] == ["machine", "start"]:
    state["machine"]["State"] = "running"
    save()
elif args[:2] == ["--connection", "rss-aggregator"]:
    args = args[2:]
    app = state["app"]
    if args[:2] == ["image", "inspect"]:
        if state.get("fail_image"):
            fail()
        print(json.dumps([state["image"]]))
    elif args[:2] == ["container", "exists"]:
        raise SystemExit(
            0
            if (args[2] == "rss-aggregator-freshrss" and app)
            or args[2] in state["checks"]
            else 1
        )
    elif args[:2] == ["container", "inspect"]:
        print(
            json.dumps(
                [
                    app
                    if args[2] == "rss-aggregator-freshrss"
                    else state["checks"][args[2]]
                ]
            )
        )
    elif args[:2] == ["volume", "exists"]:
        raise SystemExit(0)
    elif args[:2] == ["volume", "inspect"]:
        print(json.dumps([{"Labels": {"io.rss-aggregator.owner": "fixture-owner"}}]))
    elif args[0] == "run":
        state["checks"][args[args.index("--name") + 1]] = {
            "Config": {"Labels": {"io.rss-aggregator.owner": "fixture-owner"}}
        }
        save()
    elif args[0] == "rm":
        state["checks"].pop(args[-1], None)
        save()
    elif args[0] == "stop":
        app["State"] = {"Status": "exited", "Running": False}
        save()
    elif args[0] == "start":
        app["State"] = {"Status": "running", "Running": True}
        save()
    elif args[0] == "cp":
        pass
    elif args[0] == "exec":
        if not app["State"]["Running"] and "rss-aggregator-freshrss" in args:
            fail()
        if "crontab" in args:
            if state.get("fail_scheduler"):
                fail()
            print("*/20 * * * * php actualize_script.php")
        elif args[-1] == "echo PHP_VERSION;":
            print("8.4.27")
        elif 'echo file_get_contents("http:' in args[-1]:
            print(
                json.dumps(
                    {"php": "8.4.27", "sapi": "apache2handler", "upload_progress": "0"}
                )
            )
        elif "app/actualize_script.php" in args:
            pass
    else:
        fail()
else:
    fail()
