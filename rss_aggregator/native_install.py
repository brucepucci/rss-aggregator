"""Install and verify privately owned, checksum-pinned native runtime files."""

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unicodedata
import urllib.request
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO
from urllib.error import URLError

from rss_aggregator.archives import sync_directory
from rss_aggregator.native_service import (
    WRITER_LEASE_RECORD,
    WRITER_LEASES,
    WriterLeases,
    save,
    stage_writer_leases,
)
from rss_aggregator.recovery import _rename_exclusive

if TYPE_CHECKING:
    from rss_aggregator.runtime import Runtime


PLATFORM = "darwin/arm64"
SOURCE_URL = "https://codeload.github.com/FreshRSS/FreshRSS/tar.gz/"
RUNTIME_URL = "https://github.com/php/frankenphp/releases/download/v"
CHUNK = 1024 * 1024
SOURCE_MAX = 64 * CHUNK
BINARY_MAX = 300 * CHUNK
TREE_MAX = 128 * CHUNK
TREE_ENTRIES_MAX = 30_000
DIGEST = re.compile(r"[0-9a-f]{64}")
REVISION = re.compile(r"[0-9a-f]{40}")
TOKEN = re.compile(r"[0-9a-f]{32}")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
FIRST_STAGE = ".native-first-setup"
FIRST_PUBLICATION = "publication.json"
FIRST_FILES = (*WRITER_LEASES, WRITER_LEASE_RECORD, "native-owner.json")


class NativeInstallError(ValueError):
    """Report invalid, unowned, changed, or unavailable native runtime assets."""


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    """Match a complete string without accepting implicit type coercions."""
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Decode JSON objects without accepting ambiguous duplicate keys."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise NativeInstallError("Native runtime metadata has duplicate JSON keys.")
        result[key] = value
    return result


def _validate_manifest(pin: Any) -> dict[str, Any]:
    """Require exact artifact URLs, revision, release metadata, and SHA-256 pins."""
    if (
        not isinstance(pin, dict)
        or pin.keys() != {"freshrss", "runtime", "platform"}
        or pin["platform"] != PLATFORM
    ):
        raise NativeInstallError("Native manifest must target darwin/arm64.")
    source, runtime = pin["freshrss"], pin["runtime"]
    if (
        not isinstance(source, dict)
        or source.keys() != {"version", "revision", "url", "sha256"}
        or not _matches(VERSION, source["version"])
        or not _matches(REVISION, source["revision"])
        or not _matches(DIGEST, source["sha256"])
        or source["url"] != SOURCE_URL + source["revision"]
    ):
        raise NativeInstallError("Invalid pinned FreshRSS source in native manifest.")
    if (
        not isinstance(runtime, dict)
        or runtime.keys() != {"version", "php", "caddy", "url", "sha256"}
        or not all(
            _matches(VERSION, runtime[key]) for key in ("version", "php", "caddy")
        )
        or not _matches(DIGEST, runtime["sha256"])
        or runtime["url"] != f"{RUNTIME_URL}{runtime['version']}/frankenphp-mac-arm64"
    ):
        raise NativeInstallError("Invalid pinned FrankenPHP asset in native manifest.")
    return pin


def read_manifest(root: Path) -> dict[str, Any]:
    """Read the reviewed native artifact manifest from a checkout."""
    return _validate_manifest(
        json.loads(
            (root / "config" / "native.json").read_text(encoding="utf-8"),
            object_pairs_hook=_unique_keys,
        )
    )


def manifest_identity(manifest: dict[str, Any]) -> dict[str, str]:
    """Flatten a validated manifest for jobs, recovery metadata and CLI results."""
    source, runtime = manifest["freshrss"], manifest["runtime"]
    return {
        "freshrss_version": source["version"],
        "freshrss_sha256": source["sha256"],
        "runtime_version": runtime["version"],
        "runtime_sha256": runtime["sha256"],
        "php": runtime["php"],
        "caddy": runtime["caddy"],
        "platform": manifest["platform"],
    }


def _require_platform() -> None:
    """Require native Apple Silicon execution and the asset's macOS 12 minimum."""
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise NativeInstallError(
            "Native setup requires Apple Silicon macOS (darwin/arm64)."
        )
    major = platform.mac_ver()[0].split(".")[0]
    if not major.isdecimal() or int(major) < 12:
        raise NativeInstallError("Native setup requires macOS 12 or later.")


def _optional_stat(path: Path) -> os.stat_result | None:
    """Inspect a pathname without treating a dangling link as absent."""
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _private(
    path: Path,
    metadata: os.stat_result,
    *,
    mode: int,
    device: int,
    directory: bool = False,
    links: int = 1,
) -> None:
    """Refuse changed types, ownership, permissions, mounts and hard links."""
    valid_type = (
        stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    )
    if (
        not valid_type
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != mode
        or metadata.st_dev != device
        or (not directory and metadata.st_nlink != links)
    ):
        raise NativeInstallError(
            f"Refusing non-private or unexpected native path: {path}"
        )


def _private_file(path: Path, device: int, mode: int) -> BinaryIO:
    """Open an owned regular file without following links or blocking on a FIFO."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        _private(path, os.fstat(descriptor), mode=mode, device=device)
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _write_file(path: Path, source: BinaryIO, *, limit: int) -> str:
    """Exclusively create a private file, enforcing a byte limit and hashing bytes."""
    digest = hashlib.sha256()
    count = 0
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        os.fchmod(output.fileno(), 0o600)
        while block := source.read(CHUNK):
            count += len(block)
            if count > limit:
                raise NativeInstallError("Native asset exceeds the permitted size.")
            output.write(block)
            digest.update(block)
    return digest.hexdigest()


def _download(asset: dict[str, str], path: Path, limit: int, label: str) -> None:
    """Fetch a reviewed URL and reject mismatched bytes before interpreting them."""
    try:
        with urllib.request.urlopen(asset["url"], timeout=60) as response:
            actual = _write_file(path, response, limit=limit)
    except (URLError, TimeoutError) as error:
        raise NativeInstallError(
            f"Unable to download the pinned {label} asset."
        ) from error
    if actual != asset["sha256"]:
        raise NativeInstallError(f"Pinned {label} download SHA-256 mismatch.")


def _directory(path: Path) -> None:
    """Create a new directory with normalized private permissions."""
    path.mkdir(mode=0o700)
    path.chmod(0o700)


def _path_parts(name: str) -> tuple[str, ...]:
    """Reject absolute paths, traversal, ambiguous separators and unsafe names."""
    parts = tuple(name.split("/"))
    if any(
        part in {"", ".", ".."}
        or any(
            char in "\\:" or unicodedata.category(char) in {"Cc", "Cs"} for char in part
        )
        for part in parts
    ):
        raise NativeInstallError("Unsafe path in the pinned FreshRSS archive.")
    return parts


def _folded(parts: tuple[str, ...]) -> tuple[str, ...]:
    """Conservatively detect case and Unicode normalization aliases on macOS."""
    return tuple(unicodedata.normalize("NFD", part.casefold()) for part in parts)


def _extract_source(archive: Path, source: Path, revision: str) -> None:
    """Extract only declared directories and regular files under the pinned root."""
    top = f"FreshRSS-{revision}"
    seen: set[tuple[str, ...]] = set()
    directories: set[tuple[str, ...]] = set()
    total = 0
    try:
        with tarfile.open(archive, "r|gz") as members:
            for member in members:
                parts = _path_parts(member.name)
                alias = _folded(parts)
                if alias in seen:
                    raise NativeInstallError(
                        "Duplicate or colliding path in FreshRSS archive."
                    )
                if (
                    parts[0] != top
                    or (len(parts) > 1 and parts[:-1] not in directories)
                    or member.pax_headers not in ({}, {"comment": revision})
                    or member.type not in (tarfile.DIRTYPE, tarfile.REGTYPE)
                    or member.linkname
                    or member.size < 0
                    or (member.isdir() and member.size != 0)
                    or (len(parts) == 1 and not member.isdir())
                ):
                    raise NativeInstallError(
                        "Unsafe type, path or metadata in FreshRSS archive."
                    )
                seen.add(alias)
                total += member.size
                if len(seen) > TREE_ENTRIES_MAX or total > TREE_MAX:
                    raise NativeInstallError(
                        "Pinned FreshRSS source exceeds extraction limits."
                    )
                target = source.joinpath(*parts[1:])
                if member.isdir():
                    _directory(target)
                    directories.add(parts)
                else:
                    contents = members.extractfile(member)
                    if contents is None:
                        raise NativeInstallError(
                            "Missing contents in pinned FreshRSS archive."
                        )
                    with contents:
                        _write_file(target, contents, limit=member.size)
    except (tarfile.TarError, EOFError) as error:
        raise NativeInstallError(
            "Invalid or incomplete pinned FreshRSS archive."
        ) from error
    if (top,) not in directories:
        raise NativeInstallError("Missing pinned FreshRSS source root.")


def _file_sha(path: Path, device: int, mode: int, limit: int) -> str:
    """Hash a bounded owned file without following links."""
    digest = hashlib.sha256()
    count = 0
    with _private_file(path, device, mode) as stream:
        while block := stream.read(CHUNK):
            count += len(block)
            if count > limit:
                raise NativeInstallError(
                    "Installed native file exceeds its size limit."
                )
            digest.update(block)
    return digest.hexdigest()


def _tree_sha(source: Path, device: int) -> str:
    """Hash the sorted relative paths, entry types and hashes of the complete tree."""
    digest = hashlib.sha256(b"rss-aggregator native source tree v1\n")
    pending = [source]
    count, size = 0, 0
    while pending:
        path = pending.pop()
        metadata = path.lstat()
        directory = stat.S_ISDIR(metadata.st_mode)
        _private(
            path,
            metadata,
            mode=0o700 if directory else 0o600,
            device=device,
            directory=directory,
        )
        count += 1
        size += 0 if directory else metadata.st_size
        if count > TREE_ENTRIES_MAX or size > TREE_MAX:
            raise NativeInstallError(
                "Installed FreshRSS source exceeds validation limits."
            )
        if directory:
            pending.extend(sorted(path.iterdir(), reverse=True))
        relative = path.relative_to(source).as_posix()
        content = None if directory else _file_sha(path, device, 0o600, TREE_MAX)
        digest.update(
            json.dumps([relative, content], ensure_ascii=True).encode("ascii")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _check_source_release(source: Path, version: str) -> None:
    """Check the pinned source constant and entrypoints used by native operation."""
    if not all(
        (source / relative).is_file()
        for relative in (
            "constants.php",
            "p/index.php",
            "app/actualize_script.php",
            "cli/do-install.php",
            "cli/create-user.php",
        )
    ):
        raise NativeInstallError(
            "Pinned FreshRSS source is missing required entrypoints."
        )
    constants = (source / "constants.php").read_text(encoding="utf-8")
    versions = re.findall(
        r"^\s*const FRESHRSS_VERSION = '([^']+)';\s*$", constants, re.M
    )
    if versions != [version]:
        raise NativeInstallError(
            "FreshRSS source version does not match its native pin."
        )


def _probe(binary: Path, cwd: Path, *args: str) -> str:
    """Run only the verified executable, without ambient PHP or loader config."""
    try:
        result = subprocess.run(
            (str(binary), *args),
            cwd=cwd,
            env={"PATH": os.defpath, "HOME": str(cwd), "LC_ALL": "C"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise NativeInstallError(
            "Pinned FrankenPHP failed its native executable probe."
        ) from error
    return result.stdout.strip()


def _check_runtime_release(binary: Path, scratch: Path, pin: dict[str, str]) -> None:
    """Check exact FrankenPHP, PHP and Caddy versions and built-in SQLite support."""
    expected = f"FrankenPHP v{pin['version']} PHP {pin['php']} Caddy v{pin['caddy']}"
    if not re.fullmatch(
        re.escape(expected) + r"(?: h1:[A-Za-z0-9+/=]+)?",
        _probe(binary, scratch, "version"),
    ):
        raise NativeInstallError(
            "FrankenPHP, PHP or Caddy version does not match its native pin."
        )
    if (
        _probe(
            binary,
            scratch,
            "php-cli",
            "-n",
            "-r",
            'echo PHP_VERSION, "\\n"; exit(extension_loaded("pdo_sqlite") && extension_loaded("sqlite3") ? 0 : 1);',
        )
        != pin["php"]
    ):
        raise NativeInstallError(
            "Pinned FrankenPHP PHP CLI version does not match its native pin."
        )


class NativeInstall:
    """Manage native code only; the caller owns the command lock and writer guards."""

    def __init__(self, runtime: "Runtime") -> None:
        """Bind the owned paths to a runtime's checkout and private state directory."""
        self.runtime = runtime
        self.state = runtime.state_dir
        self.root = self.state / "runtime"
        self.owner_path = self.state / "native-owner.json"

    def _state_device(self) -> int:
        """Require a private state root and return its filesystem device."""
        metadata = self.state.lstat()
        _private(
            self.state, metadata, mode=0o700, device=metadata.st_dev, directory=True
        )
        return metadata.st_dev

    def _require_initial_state(
        self,
        device: int,
        *,
        staging: Path | None = None,
        published: frozenset[Path] = frozenset(),
    ) -> None:
        """Refuse entries not proved to belong to this first setup."""
        for entry in self.state.iterdir():
            if entry == staging or entry in published:
                continue
            if entry.name == "command.lock":
                metadata = entry.lstat()
                _private(entry, metadata, mode=0o600, device=device)
                if metadata.st_size == 0:
                    continue
            if entry == self.root:
                raise NativeInstallError(
                    "Refusing to adopt a pre-existing unowned native runtime path."
                )
            raise NativeInstallError(
                "Unowned RSS_STATE_DIR is not empty; use a separate private directory."
            )

    def _read_owner(self, device: int) -> dict[str, Any] | None:
        """Validate a persistent receipt without adopting foreign files."""
        if _optional_stat(self.owner_path) is None:
            return None
        with _private_file(self.owner_path, device, 0o600) as stream:
            payload = stream.read(65537)
        if len(payload) > 65536:
            raise NativeInstallError("Native ownership receipt exceeds its size limit.")
        owner = json.loads(payload, object_pairs_hook=_unique_keys)
        if (
            not isinstance(owner, dict)
            or owner.keys() != {"schema", "token", "manifest", "source_tree_sha256"}
            or type(owner["schema"]) is not int
            or owner["schema"] != 1
            or not _matches(TOKEN, owner["token"])
            or not _matches(DIGEST, owner["source_tree_sha256"])
        ):
            raise NativeInstallError(
                "Invalid native ownership receipt; no runtime was adopted."
            )
        _validate_manifest(owner["manifest"])
        return owner

    def _verify_release(
        self, release: Path, owner: dict[str, Any], device: int, *, verify: bool = True
    ) -> None:
        """Verify the exact owned layout and optionally check all runtime bytes."""
        metadata = _optional_stat(release)
        if metadata is None:
            raise NativeInstallError("Native runtime is not installed; run rss setup.")
        _private(release, metadata, mode=0o700, device=device, directory=True)
        if {entry.name for entry in release.iterdir()} != {"frankenphp", "FreshRSS"}:
            raise NativeInstallError(
                "Native runtime layout changed; refusing to operate on it."
            )
        binary, source = release / "frankenphp", release / "FreshRSS"
        _private(binary, binary.lstat(), mode=0o700, device=device)
        _private(source, source.lstat(), mode=0o700, device=device, directory=True)
        if not verify:
            return
        if (
            _file_sha(binary, device, 0o700, BINARY_MAX)
            != owner["manifest"]["runtime"]["sha256"]
        ):
            raise NativeInstallError(
                "Installed FrankenPHP SHA-256 changed; refusing to operate on it."
            )
        if _tree_sha(source, device) != owner["source_tree_sha256"]:
            raise NativeInstallError(
                "Installed FreshRSS source tree changed; refusing to operate on it."
            )

    def _require_owner(self, device: int) -> dict[str, Any]:
        """Require a validated private receipt without inspecting the release tree."""
        owner = self._read_owner(device)
        if owner is None:
            raise NativeInstallError(
                "Native runtime has no ownership receipt; run rss setup only if the runtime path is absent."
            )
        return owner

    def receipt(self) -> tuple[dict[str, Any], dict[str, str]]:
        """Read ownership for shutdown even when the released files have drifted."""
        owner = self._require_owner(self._state_device())
        return owner, manifest_identity(owner["manifest"])

    def owned(
        self, *, require_current: bool = True, verify: bool = True
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Return the owner and installed identity, verifying the runtime by default.

        ``require_current=False`` checks against the installed receipt instead of
        the checkout pin. ``verify=False`` checks the layout, not the installed bytes.
        """
        device = self._state_device()
        owner = self._require_owner(device)
        if require_current and self.runtime._manifest() != owner["manifest"]:
            raise NativeInstallError(
                "Native pin changed; setup and normal operation will not upgrade it implicitly."
            )
        self._verify_release(self.root, owner, device, verify=verify)
        return owner, manifest_identity(owner["manifest"])

    def _stage(
        self, staging: Path, manifest: dict[str, Any], token: str, device: int
    ) -> dict[str, Any]:
        """Prepare a verified release directory and receipt without touching live paths."""
        release = staging / "runtime"
        _directory(release)
        archive = staging / "FreshRSS.tar.gz"
        binary = release / "frankenphp"
        source = release / "FreshRSS"
        source_pin, runtime_pin = manifest["freshrss"], manifest["runtime"]
        _download(source_pin, archive, SOURCE_MAX, "FreshRSS")
        _extract_source(archive, source, source_pin["revision"])
        _check_source_release(source, source_pin["version"])
        tree = _tree_sha(source, device)
        _download(runtime_pin, binary, BINARY_MAX, "FrankenPHP")
        binary.chmod(0o700)
        _check_runtime_release(binary, staging, runtime_pin)
        owner = {
            "schema": 1,
            "token": token,
            "manifest": manifest,
            "source_tree_sha256": tree,
        }
        self._verify_release(release, owner, device)
        receipt = staging / self.owner_path.name
        with receipt.open("x", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(owner, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        return owner

    def _checkpoint_first_setup(self, staging: Path) -> Path:
        """Publish the verified stage before adding any individual lease path."""
        stage_writer_leases(staging)
        save(
            staging / FIRST_PUBLICATION,
            {
                "state": self.state.lstat().st_ino,
                "paths": {
                    name: (staging / name).lstat().st_ino
                    for name in (*FIRST_FILES, self.root.name)
                },
            },
        )
        checkpoint = self.state / FIRST_STAGE
        _rename_exclusive(staging, checkpoint)
        sync_directory(self.state)
        return checkpoint

    def _first_setup_paths(self, staging: Path, device: int) -> frozenset[Path]:
        """Identify only the names published by the same first-setup transaction."""
        _private(staging, staging.lstat(), mode=0o700, device=device, directory=True)
        with _private_file(staging / FIRST_PUBLICATION, device, 0o600) as marker:
            record = json.loads(marker.read(4096), object_pairs_hook=_unique_keys)
        paths = record.get("paths") if isinstance(record, dict) else None
        if (
            not isinstance(paths, dict)
            or paths.keys() != {*FIRST_FILES, self.root.name}
            or record.get("state") != self.state.lstat().st_ino
        ):
            raise NativeInstallError("Invalid first setup checkpoint; inspect locally.")
        published = set()
        for name, inode in paths.items():
            source, target = staging / name, self.state / name
            existing = {
                p: info for p in (source, target) if (info := _optional_stat(p))
            }
            directory = target == self.root
            if (
                not existing
                or (directory and len(existing) != 1)
                or type(inode) is not int
                or inode <= 0
            ):
                raise NativeInstallError(
                    "First setup evidence is incomplete; inspect locally."
                )
            for path, info in existing.items():
                _private(
                    path,
                    info,
                    mode=0o700 if directory else 0o600,
                    device=device,
                    directory=directory,
                    links=len(existing),
                )
                if info.st_ino != inode:
                    raise NativeInstallError(
                        "First setup pathname changed; inspect locally."
                    )
            if target in existing:
                published.add(target)
        return frozenset(published)

    def _finish_first_setup(
        self, staging: Path, device: int, manifest: dict[str, Any]
    ) -> dict[str, Any]:
        """Complete interrupted publication without adopting unrelated state."""
        published = self._first_setup_paths(staging, device)
        if self.root not in published:
            self._require_initial_state(device, staging=staging, published=published)
        elif any(self.state / name not in published for name in FIRST_FILES):
            raise NativeInstallError(
                "First setup evidence is incomplete; inspect locally."
            )
        linked = []
        try:
            for name in FIRST_FILES:
                source, target = staging / name, self.state / name
                if target not in published:
                    os.link(source, target, follow_symlinks=False)
                    linked.append((source, target))
            sync_directory(self.state)
        except OSError:
            # Keep the staged inode allocated while removing only links this call
            # added; a substituted or unrelated path is never rolled back.
            for source, target in reversed(linked):
                info = _optional_stat(target)
                if info is not None and os.path.samestat(source.lstat(), info):
                    target.unlink()
            if not any(os.path.lexists(self.state / name) for name in FIRST_FILES):
                shutil.rmtree(staging)
                sync_directory(self.state)
            raise

        for name in FIRST_FILES:
            (staging / name).unlink(missing_ok=True)
        sync_directory(staging)
        owner = self._require_owner(device)
        if owner["manifest"] != manifest:
            raise NativeInstallError(
                "Native pin changed; setup will not upgrade it implicitly."
            )
        WriterLeases(self.state)
        release = self.root if self.root in published else staging / self.root.name
        self._verify_release(release, owner, device)
        if release != self.root:
            _rename_exclusive(release, self.root)
            sync_directory(self.state)
        (staging / FIRST_PUBLICATION).unlink()
        shutil.rmtree(staging)
        return owner

    def install(self) -> dict[str, Any]:
        """Install the reviewed assets or verify an existing installation unchanged."""
        _require_platform()
        device = self._state_device()
        manifest = _validate_manifest(self.runtime._manifest())
        checkpoint = self.state / FIRST_STAGE
        if _optional_stat(checkpoint / FIRST_PUBLICATION) is not None:
            return self._finish_first_setup(checkpoint, device, manifest)
        owner = self._read_owner(device)
        if owner is not None and owner["manifest"] != manifest:
            raise NativeInstallError(
                "Native pin changed; setup will not upgrade it implicitly."
            )
        if owner is None:
            self._require_initial_state(device)
        else:
            try:
                WriterLeases(self.state)
            except (OSError, ValueError):
                raise NativeInstallError(
                    "Native writer state is unconfirmed; writer lease evidence is missing or damaged. Inspect the state directory locally."
                ) from None
            if _optional_stat(self.root) is not None:
                self._verify_release(self.root, owner, device)
                return owner

        with tempfile.TemporaryDirectory(
            prefix=".native-install-", dir=self.state
        ) as directory:
            staging = Path(directory)
            new_owner = self._stage(
                staging, manifest, owner["token"] if owner else uuid.uuid4().hex, device
            )
            if owner is not None and owner != new_owner:
                raise NativeInstallError(
                    "Pinned FreshRSS archive does not match the prior installation's source tree."
                )
            if _optional_stat(self.root) is not None:
                raise NativeInstallError(
                    "The native runtime path appeared during setup; refusing replacement."
                )
            if owner is None:
                if _optional_stat(self.owner_path) is not None:
                    raise NativeInstallError(
                        "A native ownership receipt appeared during setup; refusing replacement."
                    )
                self._require_initial_state(device, staging=staging)
                checkpoint = self._checkpoint_first_setup(staging)
                return self._finish_first_setup(checkpoint, device, manifest)
            (staging / "runtime").rename(self.root)
        return new_owner

    def upgrade(
        self, *, expected_manifest: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Stage and explicitly replace a stopped runtime without changing its token.

        The caller must establish stopped writers, a verified backup and approval.
        A failed staging attempt leaves the old install intact. If interrupted
        during publication, an absent runtime or mismatched receipt fails closed;
        the private staging directory retains the prior runtime for reconciliation.
        """
        _require_platform()
        device = self._state_device()
        owner, _ = self.owned(require_current=False)
        manifest = _validate_manifest(self.runtime._manifest())
        if expected_manifest is not None and manifest != expected_manifest:
            raise NativeInstallError(
                "The reviewed native upgrade pin changed; preview again."
            )
        if manifest == owner["manifest"]:
            return owner

        staging = Path(tempfile.mkdtemp(prefix=".native-upgrade-", dir=self.state))
        previous, release = staging / "previous-runtime", staging / "runtime"
        try:
            replacement = self._stage(staging, manifest, owner["token"], device)
            if (
                self._read_owner(device) != owner
                or self.runtime._manifest() != manifest
            ):
                raise NativeInstallError(
                    "The native upgrade plan changed; preview again."
                )
            self._verify_release(self.root, owner, device)
            self.root.rename(previous)
            try:
                release.rename(self.root)
            except OSError:
                previous.rename(self.root)
                raise
            try:
                (staging / self.owner_path.name).replace(self.owner_path)
            except OSError:
                self.root.rename(release)
                previous.rename(self.root)
                raise
        except BaseException:
            if _optional_stat(previous) is None:
                shutil.rmtree(staging)
            raise
        shutil.rmtree(staging)
        return replacement

    def uninstall(self) -> dict[str, Any]:
        """Remove only verified runtime files after the caller proves writers stopped.

        Retain the owner receipt, so future installs retain the instance token and
        cannot silently adopt a changed pin. Never traverse or remove instance data.
        """
        owner, _ = self.owned(require_current=False)
        with tempfile.TemporaryDirectory(
            prefix=".native-remove-", dir=self.state
        ) as directory:
            retired = Path(directory) / "runtime"
            self.root.rename(retired)
            shutil.rmtree(retired)
        return owner
