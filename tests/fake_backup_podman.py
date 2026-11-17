"""Stateful external Podman substitute for backup command-boundary tests."""

import json
import os
import shutil
import sys
from pathlib import Path

path = Path(os.environ["FAKE_BACKUP_ENGINE"])
state = json.loads(path.read_text())
args = sys.argv[1:]
state["calls"].append(args)
path.write_text(json.dumps(state))


def save():
    path.write_text(json.dumps(state))


def fail(message):
    sys.stderr.write(message)
    raise SystemExit(125)


machine = state["machine"]
connection = state["connection"]
if args == ["machine", "inspect", "rss-aggregator"]:
    print(json.dumps([machine]))
elif args == ["machine", "list", "--format", "json"]:
    print(
        json.dumps(
            [{"Name": "rss-aggregator", "Running": machine["State"] == "running"}]
        )
    )
elif args == ["system", "connection", "list", "--format", "json"]:
    print(json.dumps([connection]))
elif args[:2] == ["machine", "start"]:
    machine["State"] = "running"
    save()
elif args == ["machine", "stop", "rss-aggregator"]:
    if any(c["State"]["Running"] for c in state["containers"].values()):
        fail("VM stopped with writers running")
    machine["State"] = "stopped"
    save()
elif args[:3] == ["machine", "ssh", "rss-aggregator"]:
    print(json.dumps(state.get("rootful", [])))
elif args[:2] == ["--connection", "rss-aggregator"]:
    if machine["State"] != "running":
        fail("engine unavailable")
    args = args[2:]
    if args[0] == "ps":
        print(
            json.dumps(
                [
                    {"Names": [name]}
                    for name, container in state["containers"].items()
                    if "--all" in args or container["State"]["Running"]
                ]
            )
        )
    elif args[:2] == ["image", "inspect"]:
        image = state["image"]
        print(
            json.dumps(
                [
                    {
                        "Id": "fixture-image",
                        "Os": "linux",
                        "Architecture": "arm64",
                        "RepoDigests": [
                            image["image"],
                            image["image"].split("@")[0]
                            + "@"
                            + image["platform_manifest"],
                        ],
                        "Labels": {
                            "org.opencontainers.image.version": image["version"],
                            "org.opencontainers.image.revision": image["revision"],
                        },
                    }
                ]
            )
        )
    elif args[:2] == ["container", "exists"]:
        raise SystemExit(0 if args[2] in state["containers"] else 1)
    elif args[:2] == ["container", "inspect"]:
        print(json.dumps([state["containers"][args[2]]]))
    elif args[0] == "stop":
        state["containers"][args[-1]]["State"]["Running"] = False
        save()
    elif args[0] == "rm":
        del state["containers"][args[-1]]
        save()
    elif args[0] == "start":
        state["containers"][args[-1]]["State"]["Running"] = True
        save()
    elif args[:2] == ["volume", "exists"]:
        raise SystemExit(0 if args[2] in state["volumes"] else 1)
    elif args[:2] == ["volume", "inspect"]:
        print(json.dumps([state["volumes"][args[2]]]))
    elif args[:2] == ["volume", "create"]:
        name = args[-1]
        if name in state["volumes"]:
            fail("existing volume overwritten")
        label = args[args.index("--label") + 1].split("=", 1)
        state["volumes"][name] = {"Labels": {label[0]: label[1]}}
        save()
    elif args[:2] == ["volume", "export"]:
        if os.environ.get("BACKUP_TEST_GATE"):
            gate = Path(os.environ["BACKUP_TEST_GATE"])
            gate.with_suffix(".ready").write_text(str(os.getpid()))
            with gate.open("rb") as barrier:
                barrier.read(1)
        if any(c["State"]["Running"] for c in state["containers"].values()):
            fail("export attempted with writers running")
        output = Path(args[args.index("--output") + 1])
        if state.get("fail_export"):
            output.write_bytes(b"partial archive")
            fail("injected interrupted export with sensitive filename")
        shutil.copyfile(state["payload"], output)
    elif args[:2] == ["volume", "import"]:
        if state.get("fail_import"):
            fail("injected import failure with sensitive filename")
        state["volumes"][args[2]]["restored"] = True
        save()
    else:
        fail("UNEXPECTED ENGINE OPERATION: " + " ".join(args))
else:
    fail("UNEXPECTED HOST OPERATION: " + " ".join(args))
