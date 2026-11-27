"""Exercise native feed scheduling at the controller module entrypoint."""

import fcntl
import json
import runpy
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

NONCE = "a" * 32


def test_native_controller_fetches_once_in_an_eligible_utc_slot(tmp_path):
    state = tmp_path / "state"
    (state / "service").mkdir(parents=True, mode=0o700)
    data = state / "instances/default/data"
    data.mkdir(parents=True)
    for name in ("applied_migrations.txt", "config.php"):
        (data / name).touch()
    (state / "service.json").write_text(
        json.dumps({"nonce": NONCE, "port": 18080, "instance": "default"})
    )
    (state / "security.json").write_text(json.dumps({"nonce": NONCE}))

    # Exercise the entrypoint with inherited kernel leases, without starting a listener.
    with (
        (state / "writers.lock").open("wb") as writer,
        (state / "controller.lock").open("wb") as controller,
    ):
        for lock in (writer, controller):
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                str(Path(__file__).resolve()),
                str(state),
                str(writer.fileno()),
                str(controller.fileno()),
            ],
            env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            pass_fds=(writer.fileno(), controller.fileno()),
            timeout=10,
            check=False,
        )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["2026-01-15T12:20:00+00:00"]


def drive_controller(state):
    first_poll = datetime(2026, 1, 15, 12, 19, 59, tzinfo=timezone.utc).timestamp()
    # Once past the boundary, poll again immediately and just before the next slot.
    ticks = iter([1, 2, 1200])
    elapsed = 0

    def sleep(_):
        nonlocal elapsed
        tick = next(ticks, None)
        if tick is None:
            (state / "stop-service.json").write_text(json.dumps({"nonce": NONCE}))
        else:
            elapsed = tick

    class Process:
        def __class_getitem__(cls, _):
            # The entrypoint also uses Popen in its runtime type annotations.
            return cls

        def __init__(self, args, **_):
            assert Path(args[0]) == state / "runtime/frankenphp"
            if args[1] == "run":
                self.returncode = None
            else:
                assert args[1:] == ["php-cli", "app/actualize_script.php"]
                print(
                    datetime.fromtimestamp(
                        first_poll + elapsed, timezone.utc
                    ).isoformat()
                )
                self.returncode = 0

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

    with (
        patch("time.time", lambda: first_poll + elapsed),
        patch("time.monotonic", lambda: elapsed),
        patch("time.sleep", sleep),
        patch.object(subprocess, "Popen", Process),
    ):
        runpy.run_module(
            "rss_aggregator.native_service", run_name="__main__", alter_sys=True
        )


if __name__ == "__main__":
    drive_controller(Path(sys.argv[1]))
