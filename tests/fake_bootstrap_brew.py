"""Stateful Homebrew boundary fixture; never contact or change the real host."""

import json
import os
import sys
from pathlib import Path

path = Path(os.environ["FAKE_BREW_INSTALLED"])
state = json.loads(path.read_text())
args = sys.argv[1:]
for key in (
    "HOMEBREW_NO_AUTO_UPDATE",
    "HOMEBREW_NO_INSTALL_UPGRADE",
    "HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK",
    "HOMEBREW_NO_INSTALL_CLEANUP",
):
    if os.environ.get(key) != "1":
        raise SystemExit("Missing Homebrew safeguard: " + key)
if args == ["list", "--formula", "--versions"]:
    print(
        "\n".join(name + " " + version for name, version in state["packages"].items())
    )
elif args == ["list", "--cask", "--versions"]:
    print("healthy 1.5")  # Partial output from a failed query must not be trusted.
    raise SystemExit("Error: Cask 'discord' is not installed.")
elif args in (["info", "--json=v2", "--installed", "--cask"], ["list", "--cask"]):
    query = "JSON" if args[0] == "info" else "NAMES"
    if os.environ.get("FAKE_CASK_FAIL") == query:
        raise SystemExit("Cask " + query + " query failed")
    default = (
        '{"casks": [{"token": "healthy", "installed": "2.0", "version": "3.0"}]}'
        if query == "JSON"
        else "discord\nhealthy\n"
    )
    sys.stdout.write(os.environ.get("FAKE_CASK_" + query, default))
elif args == ["deps", "--installed"]:
    print("shared:")
elif args == ["info", "--json=v2", "podman"]:
    print('{"formulae": [{"name": "podman"}]}')
elif args == ["install", "--dry-run", "podman"]:
    print(
        os.environ.get(
            "FAKE_INSTALL_PREVIEW", "Would install:\npodman 6.2.0\nvm-runtime 1.0"
        )
    )
elif args == ["install", "podman"]:
    state["packages"].update({"podman": "6.2.0", "vm-runtime": "1.0"})
    path.write_text(json.dumps(state))
else:
    raise SystemExit("UNEXPECTED HOMEBREW MUTATION: " + " ".join(args))
