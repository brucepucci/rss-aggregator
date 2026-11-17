"""Disposable stateful Podman executable for public supervision operations."""

import json
import os
import shutil
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
elif args == ["machine", "stop", "rss-aggregator"]:
    if state["app"]["State"]["Running"]:
        fail()
    state["machine"]["State"] = "stopped"
    save()
elif args[:3] == ["machine", "ssh", "rss-aggregator"]:
    print(json.dumps(state.get("rootful", [])))
elif args[:2] == ["--connection", "rss-aggregator"]:
    args = args[2:]
    app = state["app"]
    if args[0] == "ps":
        print(
            json.dumps(
                [{"Names": ["rss-aggregator-freshrss"]}]
                if app and app["State"]["Running"]
                else []
            )
        )
    elif args[:2] == ["image", "inspect"]:
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
        if args[2] == "rss-aggregator-freshrss" and state.get("changed_on_recheck"):
            state["app_inspections"] = state.get("app_inspections", 0) + 1
            if state["app_inspections"] == 2:
                changed = state["changed_on_recheck"]
                if changed == "owner":
                    app["Config"]["Labels"]["io.rss-aggregator.owner"] = "foreign-owner"
                elif changed == "running":
                    app["State"] = {"Running": True, "Status": "running"}
                else:
                    app["Config"]["Cmd"] = ["changed-command"]
            save()
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
    elif args[:2] == ["volume", "export"]:
        if os.environ.get("BACKUP_TEST_GATE"):
            gate = Path(os.environ["BACKUP_TEST_GATE"])
            gate.with_suffix(".ready").write_text(str(os.getpid()))
            with gate.open("rb") as barrier:
                barrier.read(1)
        if app["State"]["Running"] or state.get("fail_export"):
            fail()
        shutil.copyfile(state["payload"], args[args.index("--output") + 1])
    elif args[0] == "create":
        mount = dict(
            part.split("=", 1) for part in args[args.index("--mount") + 1].split(",")
        )
        host, port, _container_port = args[args.index("--publish") + 1].split(":")
        state["app_generation"] = state.get("app_generation", 0) + 1
        state["app"] = {
            "Id": "fixture-created-" + str(state["app_generation"]),
            "Image": state["image"]["Id"],
            "Config": {
                **state["image"]["Config"],
                "Labels": dict(
                    args[i + 1].split("=", 1)
                    for i, arg in enumerate(args)
                    if arg == "--label"
                ),
                "Env": [args[i + 1] for i, arg in enumerate(args) if arg == "--env"],
            },
            "HostConfig": {
                "PortBindings": {"80/tcp": [{"HostIp": host, "HostPort": port}]}
            },
            "Mounts": [{"Name": mount["source"], "Destination": mount["destination"]}],
            "State": {"Status": "created", "Running": False},
        }
        state.pop("stale_pid", None)
        save()
    elif args[0] == "run":
        state["checks"][args[args.index("--name") + 1]] = {
            "Config": {"Labels": {"io.rss-aggregator.owner": "fixture-owner"}}
        }
        save()
    elif args[0] == "rm":
        if args[-1] == "rss-aggregator-freshrss":
            state["app"] = None
        else:
            state["checks"].pop(args[-1], None)
        save()
    elif args[0] == "stop":
        app["State"] = {"Status": "exited", "Running": False}
        save()
    elif args[0] == "start":
        app["State"] = (
            {"Status": "exited", "Running": False, "ExitCode": 1}
            if state.get("stale_pid")
            else {"Status": "running", "Running": True}
        )
        save()
    elif args[0] == "cp":
        if Path(args[1]).name == state.get("fail_copy_once"):
            state.pop("fail_copy_once")
            fail()
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
            if args[1] == "rss-aggregator-freshrss" and state.get("fail_app_probe"):
                fail()
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
