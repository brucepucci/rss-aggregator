"""Disposable stateful Brew/Podman boundary for package-cleanup tests only."""

import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["FAKE_PACKAGE_STATE"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
program = Path(sys.argv[0]).name
state.setdefault("calls", []).append([program, *args])


def save():
    state_path.write_text(json.dumps(state))


def fail():
    save()
    print("synthetic-secret https://private.invalid/credential", file=sys.stderr)
    print("synthetic-secret", file=sys.stdout)
    raise SystemExit(12)


if state.get("fail") == program or state.get("fail_args") == args:
    fail()
if program == "brew":
    if args == ["list", "--formula", "--versions"]:
        for name, version in state["packages"].items():
            print(name, version)
    elif args[:1] == ["uses"]:
        expected = [
            "uses",
            "--installed",
            "--recursive",
            "--include-build",
            "--include-test",
            "--include-optional",
            args[-1],
        ]
        if args != expected:
            fail()
        for dependent in state.get("dependents", {}).get(args[-1], []):
            print(dependent)
        state["uses_count"] = state.get("uses_count", 0) + 1
        change = state.get("change_after_uses")
        if change and state["uses_count"] == change["count"]:
            state.update(change["values"])
    elif args[:2] == ["uninstall", "--formula"] and len(args) == 3:
        if "HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK" in os.environ:
            fail()
        if state.get("dependents", {}).get(args[-1]):
            fail()
        state["packages"].pop(args[-1], None)
        no_autoremove = os.environ.get("HOMEBREW_NO_AUTOREMOVE", "")
        state.setdefault("uninstall_environments", []).append(
            {"HOMEBREW_NO_AUTOREMOVE": no_autoremove}
        )
        # Model uninstall's global orphan sweep, not just explicit removal.
        if not no_autoremove.strip() or no_autoremove.lower() in (
            "false",
            "no",
            "off",
            "nil",
            "0",
        ):
            for package in state.get("autoremove_candidates", []):
                state["packages"].pop(package, None)
    else:
        fail()
elif program == "podman":
    if args == ["machine", "list", "--format", "json"]:
        print(json.dumps(state.get("machines", [])))
    elif args == ["system", "connection", "list", "--format", "json"]:
        print(json.dumps(state.get("connections", [])))
    else:
        fail()
else:
    fail()
save()
