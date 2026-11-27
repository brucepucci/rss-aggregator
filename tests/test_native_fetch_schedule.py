"""Exercise native feed scheduling at the controller module entrypoint."""

import fcntl
import json
import runpy
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from rss_aggregator.native_install import manifest_identity
from rss_aggregator.native_service import lease
from tests.test_native_process_lifetime import _exited, _wait_for, process_checkout

scheduled_checkout = process_checkout
NONCE = "a" * 32

TIMEOUT_CONTROLLER = """
import json
import runpy
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path.cwd()))
control = Path(sys.argv[1]) / 'service'
clock = (0, 0)
ack = None
pause = time.sleep

class Process(subprocess.Popen):
    def __init__(self, args, **kwargs):
        super().__init__(args, **kwargs)
        if args[1] == 'php-cli':
            with (control / 'scheduled-pids').open('a') as history:
                print(self.pid, file=history)

    def terminate(self):
        if self.args[1] == 'run' and (control / 'hold-web').exists():
            # Model a web child draining requests and not yet ready to exit.
            (control / 'web-draining').touch()
        else:
            super().terminate()


def sleep(_):
    global clock, ack
    if ack != clock:
        temporary = control / 'ack.tmp'
        temporary.write_text(json.dumps(clock))
        temporary.replace(control / 'ack')
        ack = clock
    pause(0.01)
    clock = tuple(json.loads((control / 'clock').read_text()))


with (
    patch('time.time', lambda: clock[0]),
    patch('time.monotonic', lambda: clock[1]),
    patch('time.sleep', sleep),
    patch.object(subprocess, 'Popen', Process),
):
    runpy.run_module('rss_aggregator.native_service', run_name='__main__', alter_sys=True)
"""


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
    (state / "service" / f"approved-{NONCE}.json").touch()

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
                "-c",
                "import sys; from pathlib import Path; sys.path.insert(0, sys.argv.pop(1)); "
                "from tests.test_native_fetch_schedule import drive_controller; "
                "drive_controller(Path(sys.argv[1]))",
                str(Path(__file__).resolve().parents[1]),
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


@pytest.mark.parametrize("stopping", [False, True], ids=["running", "stopping"])
def test_scheduled_timeout_reaps_owned_fetch(scheduled_checkout, stopping):
    checkout = scheduled_checkout
    state, control = checkout.state, checkout.control
    control.mkdir(mode=0o700)
    (state / "service.json").write_text(
        json.dumps(
            {
                "nonce": NONCE,
                "port": 18080,
                "instance": "reader",
                "url": "http://127.0.0.1:18080",
                "runtime": manifest_identity(checkout.manifest),
            }
        )
    )
    (state / "security.json").write_text(json.dumps({"nonce": NONCE}))

    def advance(wall, elapsed):
        temporary = control / "clock.tmp"
        temporary.write_text(json.dumps([wall, elapsed]))
        temporary.replace(control / "clock")
        _wait_for(
            lambda: (control / "ack").exists()
            and json.loads((control / "ack").read_text()) == [wall, elapsed],
            f"controller did not acknowledge wall={wall}, elapsed={elapsed}",
        )

    def request_stop():
        stop = control / "stop.tmp"
        stop.write_text(json.dumps({"nonce": NONCE}))
        stop.replace(state / "stop-service.json")

    def status():
        result = checkout.run("status")
        assert result.returncode == 0, result.stderr
        assert not result.stderr
        snapshot = json.loads(result.stdout)
        assert snapshot["http_ready"] is True
        assert snapshot["scheduler"]["running"] is True
        return snapshot["scheduler"]["last_fetch"]

    (control / "clock").write_text("[0, 0]")
    (control / "next-slot").touch()  # Arm the disposable binary's hanging fetch.
    with (
        lease(state / "writers.lock") as writer,
        lease(state / "controller.lock") as controller,
    ):
        assert writer is not None and controller is not None
        process = subprocess.Popen(
            (
                sys.executable,
                "-I",
                "-B",
                "-c",
                TIMEOUT_CONTROLLER,
                str(state),
                str(writer),
                str(controller),
            ),
            cwd=checkout.root,
            env=checkout.environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            pass_fds=(writer, controller),
        )
    history = control / "scheduled-pids"
    try:
        _wait_for((control / "ack").exists, "controller did not begin scheduling")
        _wait_for((control / ("web-pid-" + NONCE)).exists, "web child did not start")
        # A matching security receipt alone does not open the scheduler gate.
        advance(1200, 1)
        assert not history.exists()
        (control / f"approved-{NONCE}.json").touch()
        advance(1200, 2)
        assert not history.exists(), "approval must not replay the missed slot"
        advance(2400, 10)
        _wait_for((control / "fetch-started").exists, "fetch did not start")
        pid = int(history.read_text())

        # A wall-clock jump past intervening slots isn't a deadline. Manual
        # fetching remains excluded until the active child has actually exited.
        advance(6000, 609)
        assert status() is None
        assert not _exited(pid)
        busy = checkout.run("fetch")
        assert busy.returncode == 1 and not busy.stdout
        assert "A feed fetch is already in progress" in busy.stderr

        if stopping:
            (control / "hold-web").touch()
            request_stop()
            _wait_for((control / "web-draining").exists, "web drain did not begin")
            advance(6000, 610)
            assert _exited(pid), "shutdown failed to enforce the fetch deadline"
            assert process.poll() is None, "web should still be draining"
            return

        advance(6000, 610)
        assert status() == "failed"
        assert _exited(pid), "the timed-out fetch child was not reaped"
        assert history.read_text().splitlines() == [str(pid)]

        (control / "next-slot").unlink()  # Subsequent disposable fetches succeed.
        manual = checkout.run("fetch")
        assert manual.returncode == 0, manual.stderr
        assert json.loads(manual.stdout) == {"status": "completed"}
        advance(7199, 611)
        assert status() == "failed"
        assert history.read_text().splitlines() == [str(pid)]

        advance(7200, 612)
        _wait_for(lambda: status() == "completed", "next eligible fetch did not finish")
        assert len(history.read_text().splitlines()) == 2
    finally:
        request_stop()
        (control / "fetch-release").touch()
        (control / ("web-release-" + NONCE)).touch()
        try:
            assert process.wait(timeout=5) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
