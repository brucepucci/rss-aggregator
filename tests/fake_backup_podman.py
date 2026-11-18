"""Stateful external Podman substitute for backup command-boundary tests."""

import io
import json
import os
import shutil
import sys
import tarfile
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


def resume_writer(stage):
    if state.get("resume_writer_at") != stage or state.get("writer_resumed"):
        return
    state["containers"]["rss-aggregator-freshrss"]["State"]["Running"] = True
    content = b"synthetic post-capture change"
    with tarfile.open(state["payload"], "a") as archive:
        info = tarfile.TarInfo("users/alice/post-capture.txt")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    state["writer_resumed"] = True
    save()


machine = state["machine"]
connection = state["connection"]
if args == ["machine", "inspect", "rss-aggregator"]:
    print(json.dumps([machine]))
elif args == ["machine", "list", "--format", "json"]:
    print(
        json.dumps(
            (
                [{"Name": "rss-aggregator", "Running": machine["State"] == "running"}]
                if machine
                else []
            )
            + state.get("other_machines", [])
        )
    )
elif args == ["system", "connection", "list", "--format", "json"]:
    if "connection_reads_after_export" in state:
        state["connection_reads_after_export"] += 1
        # The first post-export read belongs to _vm_plan; the second captures
        # retained foreign connections just before the receipt and VM stop.
        if state["connection_reads_after_export"] == 2:
            resume_writer("final_inventory")
        save()
    print(
        json.dumps(
            ([connection] if connection else []) + state.get("other_connections", [])
        )
    )
elif args[:2] == ["machine", "init"]:
    if machine is not None:
        fail("machine already exists")
    state["machine"] = {
        "Name": "rss-aggregator",
        "Created": "recreated",
        "ConfigDir": {"Path": "/new-fixture"},
        "Rootful": False,
        "SSHConfig": {"Port": 12346, "IdentityPath": "/new-fixture/key"},
        "State": "stopped",
    }
    state["connection"] = {
        "Name": "rss-aggregator",
        "URI": "ssh://new-fixture",
        "Identity": "/new-fixture/key",
    }
    save()
elif args[:2] == ["machine", "start"]:
    machine["State"] = "running"
    save()
elif args == ["machine", "stop", "rss-aggregator"]:
    # Native VM shutdown does not protect an application that resumed writing.
    state["writers_at_vm_stop"] = [
        name for name, c in state["containers"].items() if c["State"]["Running"]
    ]
    for container in state["containers"].values():
        container["State"]["Running"] = False
    machine["State"] = "stopped"
    save()
elif args == ["machine", "rm", "rss-aggregator"]:
    if machine["State"] != "stopped" or sys.stdin.read() != "y\n":
        fail("unsafe machine removal")
    if state.get("fail_rm"):
        fail("injected removal failure")
    state["machine"] = state["connection"] = None
    state["volumes"] = {}
    state["containers"] = {}
    save()
elif args[:3] == ["machine", "ssh", "rss-aggregator"]:
    if machine["State"] != "running" or state.get("unresponsive"):
        fail("source unavailable with sensitive diagnostics")
    kind = (
        "rootful_volumes"
        if "volume" in args
        else "rootful_images"
        if "images" in args
        else "rootful"
    )
    print(json.dumps(state.get(kind, [])))
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
        for mount in state["containers"][args[-1]]["Mounts"]:
            if (
                mount.get("Name") in state["volumes"]
                and "MountCount" in state["volumes"][mount["Name"]]
            ):
                state["volumes"][mount["Name"]]["MountCount"] = 0
        save()
    elif args[0] == "rm":
        del state["containers"][args[-1]]
        save()
    elif args[0] == "start":
        state["containers"][args[-1]]["State"]["Running"] = True
        save()
    elif args[:2] == ["volume", "ls"]:
        print(json.dumps([{"Name": name} for name in state["volumes"]]))
    elif args[0] == "images":
        print(json.dumps(state.get("images", [{"Id": "fixture-image"}])))
    elif args[:2] == ["volume", "exists"]:
        raise SystemExit(0 if args[2] in state["volumes"] else 1)
    elif args[:2] == ["volume", "inspect"]:
        print(json.dumps([state["volumes"][args[2]]]))
    elif args[:2] == ["volume", "rm"]:
        name = args[-1]
        if any(
            m.get("Name") == name
            for c in state["containers"].values()
            for m in c["Mounts"]
        ):
            fail("volume in use")
        del state["volumes"][name]
        save()
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
        resume_writer("export")
        if state.get("resume_writer_at") == "final_inventory":
            state["connection_reads_after_export"] = 0
            save()
        if state.get("add_volume_during_export"):
            state["volumes"]["appeared-during-export"] = {"Labels": {}}
            save()
    elif args[:2] == ["volume", "import"]:
        if state.get("fail_import"):
            fail("injected import failure with sensitive filename")
        state["volumes"][args[2]]["restored"] = True
        save()
    else:
        fail("UNEXPECTED ENGINE OPERATION: " + " ".join(args))
else:
    fail("UNEXPECTED HOST OPERATION: " + " ".join(args))
