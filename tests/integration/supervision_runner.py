"""Exercise real application recovery with disposable names and fake launchd only."""

import json
import os
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from rss_aggregator import supervision

from .replacement_support import (
    checked_application,
    checked_volume,
    owned_runtime,
    redirect_fixture_names,
    replacement_names,
)
from .support import expect


def run():
    request = json.load(sys.stdin)
    token, port = request["token"], request["port"]
    app, _volume, check = replacement_names(token)
    controller, owner = owned_runtime()
    checked_volume(controller, owner, token, request["volume"])
    checked_application(controller, owner, token, port, require_test_label=True)
    expect(
        not controller._exists("container", check), "Fixture preflight name occupied"
    )
    real_home = str(Path.home())
    real_path = os.environ["PATH"]
    podman = shutil.which("podman")
    expect(podman is not None, "Podman unavailable")
    prefix = "test-" + token + "-"
    redirect_fixture_names(token)
    supervision.JOB_LABEL = "io.rss-aggregator.test-" + token
    # Keep the REAL command.lock and maintenance hold. Nothing uses real launchd,
    # the user's LaunchAgents directory, intent or forwarding records.
    try:
        with TemporaryDirectory(prefix="rss-supervision-") as temporary:
            home = Path(temporary)
            tools = home / "bin"
            tools.mkdir()
            wrapper = tools / "podman"
            wrapper.write_text(
                "#!"
                + sys.executable
                + "\nimport os, sys\n"
                + f"os.execve({podman!r}, [{podman!r}, *sys.argv[1:]], dict(os.environ, HOME={real_home!r}, PATH={real_path!r}))\n"
            )
            wrapper.chmod(0o700)
            launchctl = tools / "launchctl"
            # Only the fake scheduler receives bootstrap/bootout commands.
            launchctl.write_text(
                "#!"
                + sys.executable
                + "\nimport os, plistlib, sys\nfrom pathlib import Path\n"
                "home=Path(os.environ['HOME']); args=sys.argv[1:]; loaded=home/'loaded'\n"
                "if args[0]=='print' and args[1].count('/')==2:\n"
                " if not loaded.exists(): raise SystemExit(113)\n"
                " job=loaded.read_text(); definition=plistlib.loads(Path(job).read_bytes())\n"
                " print('path = ' + job)\n"
                " print('program = ' + definition['ProgramArguments'][0])\n"
                " print('arguments = {\\n' + '\\n'.join(definition['ProgramArguments']) + '\\n}')\n"
                " print('environment = {\\n' + '\\n'.join(key+' => '+value for key,value in definition['EnvironmentVariables'].items()) + '\\n}')\n"
                "elif args[0]=='bootstrap': loaded.write_text(args[2])\n"
                "elif args[0]=='bootout': loaded.unlink()\n"
            )
            launchctl.chmod(0o700)
            os.environ.update(
                HOME=str(home),
                PATH=str(tools) + os.pathsep + os.environ["PATH"],
                RSS_STATE_DIR=str(controller.state_dir),
                RSS_PORT=str(port),
                RSS_TAILSCALE="0",
            )
            controller.run("start")
            controller.run("supervision-install")
            before = controller._check_label("container", app, owner)
            healthy = controller.run("supervision-tick")
            after = controller._check_label("container", app, owner)
            expect(
                before["State"]["StartedAt"] == after["State"]["StartedAt"],
                "Healthy check restarted the fixture",
            )
            controller._podman("stop", "--time", "60", app)
            recovered = controller.run("supervision-tick")
            expect(recovered["state"] == "recovered", "Fixture did not recover")
            controller.run("supervision-remove")
            print(
                json.dumps(
                    {"healthy": healthy["state"], "recovery": recovered["state"]},
                    indent=2,
                )
            )
    finally:
        for suffix in (
            "operator-intent.json",
            "supervision.json",
            "supervision-recovery.json",
        ):
            (controller.state_dir / (prefix + suffix)).unlink(missing_ok=True)


if __name__ == "__main__":
    run()
