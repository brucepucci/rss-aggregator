"""Stateful external Podman substitute for backup command-boundary tests."""

import hashlib
import io
import json
import os
import shlex
import shutil
import sys
import tarfile
from datetime import datetime, timezone
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


def list_secrets(engine, local=False):
    if state.get("fail_secret_inventory") == engine:
        # The native remote tunnel drops SecretList's API error. Only the
        # guest-local engine reliably propagates an unavailable inventory.
        if local:
            fail("sensitive secret inspection diagnostic")
        return
    if state.get("malformed_secret_inventory") == engine:
        print(json.dumps({"sensitive": "invalid secret metadata"}))
    else:
        key = "secrets" if engine == "rootless" else "rootful_secrets"
        # Guest-local --quiet emits IDs only; --format json is instead a
        # literal Go template. Both write nothing for a healthy empty store.
        for secret in state.get(key, []):
            print(secret["ID"] if local else "json")


def guest_records(engine, resource, default):
    if state.get("fail_guest_inventory") == [engine, resource]:
        fail("sensitive guest inspection diagnostic")
    if state.get("malformed_guest_inventory") == [engine, resource]:
        return {"sensitive": "invalid guest metadata"}
    key = resource if engine == "rootless" else "rootful_" + resource
    return state.get(key, default)


def list_guest_quiet(engine, resource):
    records = guest_records(engine, resource, [])
    if not isinstance(records, list):
        print(json.dumps(records))
    else:
        for _record in records:
            print("synthetic-id")


def engine_info(engine, remote=False):
    if remote and state.get("fail_api_info") == engine:
        fail("sensitive API info diagnostic")
    if remote and state.get("malformed_api_info") == engine:
        return ["sensitive invalid API info"]
    rootful = engine == "rootful"
    graph = (
        "/var/lib/containers/storage"
        if rootful
        else "/var/home/core/.local/share/containers/storage"
    )
    info = {
        "host": {
            "serviceIsRemote": remote,
            "security": {"rootless": not rootful},
            "networkBackend": "netavark",
            "databaseBackend": "sqlite",
            "eventLogger": "journald",
        },
        "version": {"Version": "6.2.0"},
        "store": {
            "transientStore": False,
            "imageCopyTmpDir": "/var/tmp",
            "graphRoot": graph,
            "runRoot": "/run/containers/storage"
            if rootful
            else "/run/user/1000/containers",
            "volumePath": graph + "/volumes",
            "graphDriverName": "overlay",
            "graphOptions": {},
        },
    }
    return state.get(engine + ("_api_info" if remote else "_info"), info)


def network_context(engine):
    if state.get("fail_network_context") == engine:
        fail("sensitive service network provenance diagnostic")
    if state.get("malformed_network_context") == engine:
        print(json.dumps({"sensitive": "invalid context"}))
        return
    fingerprint = state.get(
        engine + "_network_fingerprint", hashlib.sha256(engine.encode()).hexdigest()
    )
    service = state.get(
        engine + "_network_service",
        hashlib.sha256((engine + "service").encode()).hexdigest(),
    )
    if state.get("flap_network_service") == engine:
        reads = sum(
            "python3" in call
            for call in state["calls"]
            if ("sudo" in call) == (engine == "rootful")
        )
        service = hashlib.sha256(str(reads).encode()).hexdigest()
    print(json.dumps({"version": 1, "fingerprint": fingerprint, "service": service}))


def list_networks(engine, remote=False):
    # Native v6.2.0 synthesizes an absent default anew per local process;
    # a persisted replacement's Created field remains stable across calls.
    network = {
        "name": "podman",
        "id": "2f259bab93aaaaa2542ba43ef33eb990d0999ee1b9924b557b7be53c0b7a1bb9",
        "driver": "bridge",
        "network_interface": "podman0",
        "created": datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "789Z"),
        "subnets": [{"subnet": "10.88.0.0/16", "gateway": "10.88.0.1"}],
        "ipv6_enabled": False,
        "internal": False,
        "dns_enabled": False,
        "ipam_options": {"driver": "host-local"},
    }
    if remote:
        # The service caches its synthetic timestamp, unlike fresh local engines.
        network["created"] = "2026-01-01T00:00:00.123456789Z"
        if state.get("fail_api_network_inventory") == engine:
            fail("sensitive API network diagnostic")
        if state.get("warn_api_network_inventory") == engine:
            sys.stderr.write("sensitive API network client diagnostic")
        if state.get("malformed_api_network_inventory") == engine:
            print(json.dumps({"sensitive": "invalid API network metadata"}))
        else:
            print(
                json.dumps(
                    state.get(
                        engine + "_api_networks",
                        guest_records(engine, "networks", [network]),
                    )
                )
            )
        return
    times = state.get("network_creation_times", {}).get(engine)
    if times:
        reads = sum(
            call[-4:] == ["network", "ls", "--format", "json"]
            and "--remote=false" in call
            and ("sudo" in call) == (engine == "rootful")
            for call in state["calls"]
        )
        network["created"] = times[(reads - 1) % len(times)]
    if state.get("warn_network_inventory") == engine:
        sys.stderr.write("sensitive unreadable network configuration")
    print(json.dumps(guest_records(engine, "networks", [network])))


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
    state["writer_resumed_call_index"] = len(state["calls"]) - 1
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
        # _vm_plan reads connections twice: its inventory, then _owner's
        # inventory. Read three captures retained foreign connections just
        # before the receipt and final idle check.
        if state["connection_reads_after_export"] == 3:
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
        "SSHConfig": {
            "Port": 12346,
            "IdentityPath": "/new-fixture/key",
            "RemoteUsername": "core",
        },
        "State": "stopped",
    }
    state["connection"] = {
        "Name": "rss-aggregator",
        "URI": "ssh://core@new-fixture/run/user/1000/podman/podman.sock",
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
    for key in (
        "secrets",
        "rootful_secrets",
        "secret_payloads",
        "rootful_secret_payloads",
        "selected_api_secrets",
        "rootless_api_networks",
        "rootful_api_networks",
        "artifacts",
        "rootful_artifacts",
        "networks",
        "rootful_networks",
        "pods",
        "rootful_pods",
        "external",
        "rootful_external",
        "guest_sentinel",
        "build_cache",
        "rootful_build_cache",
        "secret_storage",
        "rootful_secret_storage",
        "intermediate_images",
        "rootful_intermediate_images",
    ):
        state.pop(key, None)
    save()
elif args[:2] == ["machine", "ssh"]:
    if machine["State"] != "running" or state.get("unresponsive"):
        fail(
            state.get(
                "unresponsive_diagnostic",
                "source unavailable with sensitive diagnostics",
            )
        )
    if args[2:4] == ["--username", "core"]:
        assert args[4] == "rss-aggregator"
        command = args[5:]
    else:
        assert args[2] == "rss-aggregator"
        command = args[3:]
    rootful = command[:1] == ["sudo"]
    if rootful:
        command = command[3:] if command[:3] == ["sudo", "-n", "--"] else command[1:]
    engine = "rootful" if rootful else "rootless"
    command = shlex.split(" ".join(command))
    if command[:2] == ["podman", "--log-level=warn"]:
        command = ["podman", *command[2:]]
    if "python3" in command and "-c" in command:
        assert "Bind API network inspection" in command[command.index("-c") + 1]
        network_context(engine)
        raise SystemExit(0)
    if command[:1] == ["find"]:
        assert command[1] == "-H"
        if command[-4:] == ["-name", "buildah-cache-*", "-print", "-quit"]:
            list_guest_quiet(engine, "build_cache")
        else:
            assert command[-5:] == ["!", "-name", "secrets.lock", "-print", "-quit"]
            list_guest_quiet(engine, "secret_storage")
        raise SystemExit(0)
    if command[:2] == ["podman", "--remote=false"]:
        if command[2:] == ["info", "--format", "json"]:
            print(json.dumps(engine_info(engine)))
            raise SystemExit(0)
        if command[2:] == ["network", "ls", "--format", "json"]:
            list_networks(engine)
            raise SystemExit(0)
        if command[2:] == ["artifact", "ls", "--quiet"]:
            list_guest_quiet(engine, "artifacts")
            raise SystemExit(0)
        if command[2:] == ["pod", "ps", "--quiet"]:
            list_guest_quiet(engine, "pods")
            raise SystemExit(0)
    if command == [
        "podman",
        "--remote",
        "--url",
        "unix:///run/podman/podman.sock",
        "network",
        "ls",
        "--format",
        "json",
    ]:
        assert rootful
        list_networks(engine, remote=True)
        raise SystemExit(0)
    if command == [
        "podman",
        "--remote",
        "--url",
        "unix:///run/podman/podman.sock",
        "info",
        "--format",
        "json",
    ]:
        assert rootful
        print(json.dumps(engine_info(engine, remote=True)))
        raise SystemExit(0)
    if command == ["podman", "--remote=false", "secret", "ls", "--quiet"]:
        list_secrets("rootful" if rootful else "rootless", local=True)
        raise SystemExit(0)
    if command == ["podman", "secret", "ls", "--format", "json"]:
        list_secrets("rootful" if rootful else "rootless")
        raise SystemExit(0)
    if command[:2] == ["podman", "--remote=false"]:
        command = ["podman", *command[2:]]
    operations = {
        ("podman", "ps", "--format", "json"): "rootful",
        ("podman", "ps", "--all", "--external", "--format", "json"): "rootful",
        ("podman", "volume", "ls", "--format", "json"): "rootful_volumes",
        ("podman", "images", "--all", "--format", "json"): "rootful_images",
    }
    kind = operations.get(tuple(command))
    if not rootful or kind is None:
        fail("UNEXPECTED GUEST OPERATION: " + " ".join(command))
    records = state.get(kind, [])
    if "--external" in args:
        external = guest_records(engine, "external", [])
        records = records + external if isinstance(external, list) else external
    if "--all" in args and "images" in args:
        records += state.get("rootful_intermediate_images", [])
    print(json.dumps(records))
elif args[:2] == ["--connection", "rss-aggregator"]:
    if machine["State"] != "running":
        fail("engine unavailable")
    args = args[2:]
    if args == ["info", "--format", "json"]:
        print(json.dumps(engine_info("rootless", remote=True)))
    elif args == ["network", "ls", "--format", "json"]:
        list_networks("rootless", remote=True)
    elif args == ["secret", "ls", "--format", "json"]:
        list_secrets("rootless")
    elif args[0] == "ps":
        records = [
            {"Names": [name]}
            for name, container in state["containers"].items()
            if "--all" in args or container["State"]["Running"]
        ]
        if "--external" in args:
            external = guest_records("rootless", "external", [])
            records = records + external if isinstance(external, list) else external
        print(json.dumps(records))
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
        images = state.get("images", [{"Id": "fixture-image"}])
        if "--all" in args:
            images += state.get("intermediate_images", [])
        print(json.dumps(images))
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
        if state.get("secret_state_during_export"):
            state.update(state["secret_state_during_export"])
            save()
        if state.get("guest_state_during_export"):
            state.update(state["guest_state_during_export"])
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
