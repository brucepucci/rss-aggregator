"""Bind API network inspection to an audited, unchanged stock guest context.

Executed as standalone source inside the guest (Python 3.11+), not as a service.
Only fingerprints leave the guest; failures never include inspected metadata.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _require(value: Any) -> None:
    """Refuse an unsupported or unverifiable provenance condition."""
    if not value:
        raise ValueError("Unverified network context")


def _digest(value: Any) -> str:
    """Return a private equality witness without exposing metadata values."""
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _start(proc: Path) -> int:
    """Read kernel process start ticks without confusing parentheses in comm."""
    return int((proc / "stat").read_text().rsplit(") ", 1)[1].split()[19])


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    """Compare configuration identity without mistaking read-atime for mutation."""
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _stock_configuration(config: dict[str, Any]) -> None:
    """Reject selectors using native case-insensitive TOML struct-key semantics."""

    def folded(table: dict[str, Any]) -> dict[str, Any]:
        """Refuse ambiguous aliases rather than guessing native map iteration order."""
        _require(isinstance(table, dict))
        result = {key.casefold(): value for key, value in table.items()}
        _require(len(result) == len(table))
        return result

    tables = folded(config)
    _require(not folded(tables.get("network", {})).get("network_config_dir"))
    _require(not folded(tables.get("engine", {})).get("env"))


def _context(rootful: bool, socket: str, graph: str, run: str) -> dict[str, Any]:
    """Attest the selected stock listener, namespace and pre-load configuration.

    Rootless execution must already be inside native ``podman unshare``. Refuse
    custom startup selectors, config-directory overrides, symlinks, fresh/changed
    configuration, clock uncertainty and endpoint/process swaps. This is not an
    atomic guarantee against independently mutating native clients or the kernel.
    """
    uid = 0 if rootful else int(os.environ["_CONTAINERS_ROOTLESS_UID"])
    account_home = Path(pwd.getpwuid(uid).pw_dir)
    home = account_home.resolve()
    expected_socket = (
        "/run/podman/podman.sock" if rootful else f"/run/user/{uid}/podman/podman.sock"
    )
    _require(socket == expected_socket)
    manager = ["systemctl", *([] if rootful else ["--user"])]
    result = subprocess.run(
        [*manager, "show", "podman.service", "--property=MainPID", "--value"],
        text=True,
        capture_output=True,
        check=False,
    )
    _require(result.returncode == 0 and not result.stderr)
    pid = int(result.stdout.strip())
    _require(pid > 1)
    proc = Path("/proc") / str(pid)
    started = _start(proc)
    _require(
        (proc / "cmdline").read_bytes().split(b"\0")[:-1]
        == [b"/usr/bin/podman", b"--log-level=info", b"system", b"service"]
    )
    _require(os.path.samefile(proc / "exe", "/usr/bin/podman"))
    _require(os.path.samefile(proc / "root", "/"))
    for namespace in ("mnt", "user", "net"):
        _require(
            os.readlink(proc / "ns" / namespace)
            == os.readlink(Path("/proc/self/ns") / namespace)
        )
    environment = dict(
        entry.split(b"=", 1)
        for entry in (proc / "environ").read_bytes().split(b"\0")
        if b"=" in entry
    )
    denied = {
        "XDG_DATA_HOME",
        "STORAGE_DRIVER",
        "STORAGE_OPTS",
        "CONTAINER_HOST",
        "CONTAINER_CONNECTION",
    }
    for key in environment:
        name = os.fsdecode(key)
        _require(not name.startswith("CONTAINERS_") and name not in denied)
    for name, value in os.environ.items():
        if name.startswith("CONTAINERS_"):
            _require(
                not rootful
                and {"CONTAINERS_GRAPHROOT": graph, "CONTAINERS_RUNROOT": run}.get(name)
                == value
            )
        _require(name not in denied)
    for selected in (
        environment.get(b"XDG_CONFIG_HOME"),
        os.fsencode(os.environ["XDG_CONFIG_HOME"])
        if "XDG_CONFIG_HOME" in os.environ
        else None,
    ):
        _require(selected is None or selected == os.fsencode(str(home / ".config")))
    service_home = environment.get(b"HOME")
    _require(
        (service_home is None and rootful)
        or service_home in {os.fsencode(str(account_home)), os.fsencode(str(home))}
    )
    _require(Path.home().resolve() == home)
    if not rootful:
        runtime = os.fsencode(f"/run/user/{uid}")
        _require(environment.get(b"XDG_RUNTIME_DIR") == runtime)
        _require(os.fsencode(os.environ.get("XDG_RUNTIME_DIR", "")) == runtime)
    rows = [
        line.split()
        for line in Path("/proc/net/unix").read_text().splitlines()[1:]
        if len(line.split()) == 8 and line.split()[-1] == socket
    ]
    _require(len(rows) == 1 and rows[0][3:6] == ["00010000", "0001", "01"])
    inode = rows[0][6]
    descriptors = set()
    for descriptor in (proc / "fd").iterdir():
        try:
            descriptors.add(os.readlink(descriptor))
        except FileNotFoundError:
            continue
    _require(f"socket:[{inode}]" in descriptors)
    boot = int(
        next(
            line.split()[1]
            for line in Path("/proc/stat").read_text().splitlines()
            if line.startswith("btime ")
        )
    )
    # btime is whole seconds. Reject clock disagreement; leave two seconds of
    # conservative clearance rather than crediting inputs created near startup.
    offset = time.time_ns() - time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    _require(0 <= offset - boot * 1_000_000_000 < 1_100_000_000)
    threshold = (
        boot * 1_000_000_000
        + started * 1_000_000_000 // os.sysconf("SC_CLK_TCK")
        - 2_000_000_000
    )
    records: list[Any] = []
    observed: set[Path] = set()

    def witness(path: Path, *, file: bool = False) -> None:
        """Fingerprint inputs and absent-path ancestors; refuse late mutations."""
        import tomllib

        if path in observed:
            return
        observed.add(path)
        if not path.exists() and not path.is_symlink():
            witness(path.parent)
            records.append([str(path), None])
            return
        # Fedora's stock root home is the one audited absolute alias.
        if path == account_home and rootful and path.is_symlink():
            _require(str(path) == "/root" and str(home) == "/var/roothome")
            info = path.lstat()
            _require(max(info.st_mtime_ns, info.st_ctime_ns) < threshold)
            records.append([str(path), str(home), info.st_ino, info.st_ctime_ns])
            witness(home)
            return
        _require(not path.is_symlink())
        info = path.stat()
        _require(max(info.st_mtime_ns, info.st_ctime_ns) < threshold)
        _require(stat.S_ISREG(info.st_mode) if file else stat.S_ISDIR(info.st_mode))
        if path != Path("/"):
            witness(path.parent)
        record: list[Any] = [
            str(path),
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_size if file else None,
            info.st_mtime_ns,
            info.st_ctime_ns,
        ]
        if file:
            _require(info.st_size <= 1_048_576)
            data = path.read_bytes()
            config = tomllib.loads(data.decode())
            _stock_configuration(config)
            record.append(hashlib.sha256(data).hexdigest())
            _require(_stamp(path.stat()) == _stamp(info))
        records.append(record)

    witness(account_home)
    for directory in (
        Path("/usr/share/containers"),
        Path("/etc/containers"),
        home / ".config/containers",
    ):
        for name in ("containers", "storage"):
            witness(directory / (name + ".conf"), file=True)
            kind = "rootful" if rootful else "rootless"
            dropins = [
                directory / (name + ".conf.d"),
                directory / f"{name}.{kind}.conf.d",
            ]
            if not rootful:
                dropins.append(directory / f"{name}.rootless.conf.d" / str(uid))
            for dropin in dropins:
                witness(dropin)
                if dropin.exists():
                    # Inspect all entries, never silently truncate a config tree.
                    for entry in sorted(dropin.rglob("*")):
                        _require(not entry.is_symlink())
                        if entry.is_dir():
                            witness(entry)
                        elif entry.suffix == ".conf":
                            witness(entry, file=True)
    _require(_start(proc) == started)
    return {
        "version": 1,
        "fingerprint": _digest([uid, socket, records]),
        "service": _digest([pid, started, inode]),
    }


def main() -> None:
    """Emit only attested fingerprints or a fixed failure diagnostic."""
    try:
        print(json.dumps(_context(sys.argv[1] == "rootful", *sys.argv[2:])))
    except Exception:
        sys.stderr.write("Unverifiable API network context; resources retained.\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
