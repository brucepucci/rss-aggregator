"""Verify portable instance archives and copy complete native data trees."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import struct
import tarfile
import tempfile
import unicodedata
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

_FORMAT = "rss-aggregator-backup"
_INCLUDES = "complete FreshRSS data directory; no extensions in initial deployment"
_METADATA_LIMIT = 64 * 1024
_PATH_DEPTH_LIMIT = 64
_PATH_METADATA_LIMIT = 8 * 1024 * 1024
_PATH_STRING_OVERHEAD = 256
_CHUNK = 1024 * 1024
_PAX_FIELDS = {"path", "size", "uid", "gid", "mtime"}
_RUNTIME_FIELDS = {
    "freshrss_version",
    "freshrss_sha256",
    "runtime_version",
    "runtime_sha256",
    "php",
    "caddy",
    "platform",
}
_DIGEST = re.compile(r"[0-9a-f]{64}")


def _temporary(directory: Path, suffix: str) -> tuple[BinaryIO, Path]:
    """Create a private temporary file in the caller-managed work directory."""
    descriptor, name = tempfile.mkstemp(
        prefix=".rss-backup-", suffix=suffix, dir=directory
    )
    try:
        os.fchmod(descriptor, 0o600)
        return os.fdopen(descriptor, "w+b"), Path(name)
    except BaseException:
        os.close(descriptor)
        _cleanup(Path(name))
        raise


def _cleanup(*paths: Path | None) -> None:
    """Attempt all removals without exposing filesystem paths in cleanup errors."""
    failed = False
    for path in paths:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                failed = True
    if failed:
        raise OSError("Backup archive cleanup failed.") from None


def _copy(source: BinaryIO, target: BinaryIO | None = None) -> tuple[str, int]:
    """Stream bytes, optionally copying them, and return their digest and length."""
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(_CHUNK):
        digest.update(chunk)
        size += len(chunk)
        if target is not None:
            target.write(chunk)
    return digest.hexdigest(), size


def _json(value: Any) -> bytes:
    """Encode finite JSON deterministically, including compatibility metadata."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys instead of silently changing their meaning."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _compatibility(
    image: dict[str, Any] | None, runtime: dict[str, Any] | None
) -> tuple[int, str, dict[str, Any]]:
    """Require an explicit, mutually exclusive legacy or native identity."""
    if runtime is None and isinstance(image, dict):
        return 1, "image", image
    if (
        image is not None
        or not isinstance(runtime, dict)
        or set(runtime) != _RUNTIME_FIELDS
        or not all(isinstance(value, str) and value for value in runtime.values())
        or _DIGEST.fullmatch(runtime["freshrss_sha256"]) is None
        or _DIGEST.fullmatch(runtime["runtime_sha256"]) is None
    ):
        raise ValueError
    return 2, "runtime", runtime


def _manifest(
    data: bytes, version: int, key: str, identity: dict[str, Any]
) -> dict[str, Any]:
    """Check the selected format's complete schema and exact identity."""
    value = json.loads(data.decode("utf-8"), object_pairs_hook=_object)
    if not isinstance(value, dict) or set(value) != {
        "format",
        "format_version",
        "created_at",
        key,
        "includes",
        "payload",
    }:
        raise ValueError
    if (
        value["format"] != _FORMAT
        or type(value["format_version"]) is not int
        or value["format_version"] != version
        or value["includes"] != _INCLUDES
        or not isinstance(value[key], dict)
        or _json(value[key]) != _json(identity)
        or not isinstance(value["created_at"], str)
    ):
        raise ValueError
    created = datetime.fromisoformat(value["created_at"].replace("Z", "+00:00"))
    if created.tzinfo is None or created.utcoffset() != timezone.utc.utcoffset(created):
        raise ValueError
    payload = value["payload"]
    if (
        not isinstance(payload, dict)
        or set(payload) != {"sha256", "size"}
        or not isinstance(payload["sha256"], str)
        or _DIGEST.fullmatch(payload["sha256"]) is None
        or type(payload["size"]) is not int
        or payload["size"] < 0
    ):
        raise ValueError
    return value


def _zip_metadata(archive: Path) -> None:
    """Bound central-directory allocation before handing a ZIP to zipfile.

    ZIP64 is supported for large data directories. Comments and split archives
    are not part of this format; bounding metadata does not cap payload size.
    """
    with archive.open("rb") as source:
        source.seek(0, os.SEEK_END)
        end = source.tell() - 22
        if end < 0:
            raise ValueError
        source.seek(end)
        signature, disk, cd_disk, count, total, size, offset, comment = struct.unpack(
            "<4s4H2LH", source.read(22)
        )
        if signature != b"PK\x05\x06" or disk or cd_disk or comment or count != total:
            raise ValueError
        if end < 20:
            raise ValueError
        source.seek(end - 20)
        locator = source.read(20)
        has_zip64 = locator.startswith(b"PK\x06\x07")
        if (
            total == 0xFFFF or size == 0xFFFFFFFF or offset == 0xFFFFFFFF
        ) and not has_zip64:
            raise ValueError
        if has_zip64:
            signature, disk, position, disks = struct.unpack("<4sLQL", locator)
            if disk or disks != 1:
                raise ValueError
            source.seek(position)
            fields = struct.unpack("<4sQ2H2L4Q", source.read(56))
            if (
                fields[0] != b"PK\x06\x06"
                or fields[1] != 44
                or fields[4] != 0
                or fields[5] != 0
                or fields[6] != fields[7]
                or position + 56 != end - 20
            ):
                raise ValueError
            total, size, offset = fields[7:10]
            end = position
        if total != 2 or size > 4 * _METADATA_LIMIT or offset + size != end:
            raise ValueError


def _local_headers(archive: Path, entries: list[zipfile.ZipInfo]) -> None:
    """Check local ZIP sizes and CRCs as well as central-directory metadata."""
    previous_end = 0
    with archive.open("rb") as source:
        for entry in sorted(entries, key=lambda item: item.header_offset):
            if entry.header_offset != previous_end:
                raise ValueError
            source.seek(entry.header_offset)
            fields = struct.unpack("<4s5H3L2H", source.read(30))
            signature, _, flags, compression, _, _, crc, packed, size, names, extras = (
                fields
            )
            if (
                signature != b"PK\x03\x04"
                or flags != entry.flag_bits
                or compression != zipfile.ZIP_STORED
            ):
                raise ValueError
            name = source.read(names).decode("utf-8" if flags & 2048 else "cp437")
            if name != entry.filename:
                raise ValueError
            extra = source.read(extras)
            if len(extra) != extras:
                raise ValueError
            zip64 = size == 0xFFFFFFFF or packed == 0xFFFFFFFF
            extended: bytes | None = None
            position = 0
            while position < len(extra):
                tag, length = struct.unpack("<HH", extra[position : position + 4])
                position += 4
                value = extra[position : position + length]
                if len(value) != length:
                    raise ValueError
                if tag == 1:
                    if extended is not None:
                        raise ValueError
                    extended = value
                position += length
            if zip64:
                values = extended or b""
                if size == 0xFFFFFFFF:
                    size = struct.unpack("<Q", values[:8])[0]
                    values = values[8:]
                if packed == 0xFFFFFFFF:
                    packed = struct.unpack("<Q", values[:8])[0]
            data_end = source.tell() + entry.compress_size
            source.seek(data_end)
            if flags & 8:
                if (
                    crc not in {0, entry.CRC}
                    or size not in {0, entry.file_size}
                    or packed not in {0, entry.compress_size}
                ):
                    raise ValueError
                first = source.read(4)
                if first == b"PK\x07\x08":
                    first = source.read(4)
                crc = struct.unpack("<L", first)[0]
                width = 16 if zip64 else 8
                packed, size = struct.unpack(
                    "<QQ" if zip64 else "<LL", source.read(width)
                )
            if (
                crc != entry.CRC
                or size != entry.file_size
                or packed != entry.compress_size
            ):
                raise ValueError
            previous_end = source.tell()
        if source.read(4) != b"PK\x01\x02":
            raise ValueError


def _pax(data: bytes) -> dict[str, str]:
    """Parse bounded PAX records, retaining only safe semantic overrides."""
    result: dict[str, str] = {}
    position = 0
    while position < len(data):
        space = data.find(b" ", position)
        if space < 0 or not data[position:space].isdigit():
            raise ValueError
        length = int(data[position:space])
        stop = position + length
        if length <= space - position + 2 or stop > len(data):
            raise ValueError
        record = data[space + 1 : stop]
        if not record.endswith(b"\n") or b"=" not in record or b"\0" in record:
            raise ValueError
        key, value = record[:-1].decode("utf-8").split("=", 1)
        if (
            not key
            or key.startswith("GNU.sparse")
            or key
            in {
                "SCHILY.realsize",
                "SCHILY.filetype",
                "SCHILY.holesdata",
                "SUN.holesdata",
            }
        ):
            raise ValueError
        if key in _PAX_FIELDS:
            result[key] = value
        position = stop
    return result


def _path(name: str) -> str:
    """Normalize safe export paths, allowing at most 64 non-root components."""
    if (
        not name
        or len(name.encode("utf-8")) > 4096
        or name.startswith("/")
        or "\\" in name
        or "\0" in name
    ):
        raise ValueError
    components = name.split("/")
    if ".." in components:
        raise ValueError
    normalized = "/".join(part for part in components if part not in {"", "."}) or "."
    if normalized != "." and normalized.count("/") + 1 > _PATH_DEPTH_LIMIT:
        raise ValueError
    return normalized


def _number(value: str, fractional: bool = False) -> int:
    """Parse bounded numeric PAX ownership, size, or modification time."""
    if len(value) > 128:
        raise ValueError
    if fractional:
        if re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value) is None:
            raise ValueError
        number = float(value)
        if not math.isfinite(number):
            raise ValueError
        return int(number)
    if re.fullmatch(r"[0-9]+", value) is None:
        raise ValueError
    return int(value)


def _padding(source: BinaryIO, size: int) -> None:
    """Consume and validate tar record padding after a member's content."""
    padding = (-size) % 512
    if source.read(padding) != bytes(padding):
        raise ValueError


def _native_name(path: str, paths: dict[str, str]) -> None:
    """Reject different spellings that alias on a default macOS filesystem."""
    folded = unicodedata.normalize("NFD", unicodedata.normalize("NFD", path).casefold())
    if paths.setdefault(folded, path) != path:
        raise ValueError


def _repack(source: BinaryIO, target: BinaryIO, *, native_paths: bool) -> None:
    """Validate the entire raw tar and emit only normalized files/directories.

    Header extensions are parsed with bounded allocations rather than allowing
    tarfile to load arbitrary PAX or GNU metadata. File content is streamed.
    Normalized paths have at most 64 components. Before constructing ancestor
    strings, charge each member name and every ancestor against an 8 MiB
    cumulative budget: four times its UTF-8 byte length plus 256 bytes for
    string/set overhead. Shared ancestors are conservatively charged again for
    every member, even when already retained. Payload bytes are not capped.
    """
    path_metadata = 0
    seen: set[str] = set()
    files: set[str] = set()
    ancestors: set[str] = set()
    native_names: dict[str, str] = {}
    global_pax: dict[str, str] = {}
    local: dict[str, str] = {}
    pending = 0
    with tarfile.open(fileobj=target, mode="w", format=tarfile.GNU_FORMAT) as output:
        while True:
            block = source.read(512)
            if block == bytes(512):
                if pending or source.read(512) != bytes(512):
                    raise ValueError
                trailing = 0
                while chunk := source.read(_CHUNK):
                    trailing += len(chunk)
                    if any(chunk):
                        raise ValueError
                if trailing % 512:
                    raise ValueError
                break
            if len(block) != 512:
                raise ValueError
            fields = (block[:100],)
            if block[257:265] == tarfile.POSIX_MAGIC:
                fields += (block[345:500],)
            for field in fields:
                _, separator, tail = field.partition(b"\0")
                if separator and any(tail):
                    raise ValueError
            member = tarfile.TarInfo.frombuf(block, "utf-8", "strict")
            if member.size < 0:
                raise ValueError
            if member.type in {
                tarfile.XHDTYPE,
                tarfile.XGLTYPE,
                tarfile.GNUTYPE_LONGNAME,
            }:
                if member.size > _METADATA_LIMIT:
                    raise ValueError
                data = source.read(member.size)
                if len(data) != member.size:
                    raise ValueError
                _padding(source, member.size)
                if member.type == tarfile.XGLTYPE:
                    global_pax.update(_pax(data))
                else:
                    pending += member.size + 512
                    if pending > 2 * _METADATA_LIMIT:
                        raise ValueError
                    if member.type == tarfile.XHDTYPE:
                        local.update(_pax(data))
                    else:
                        if not data.endswith(b"\0"):
                            raise ValueError
                        local["path"] = data[:-1].decode("utf-8")
                continue
            if member.type not in {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE}:
                raise ValueError
            overrides = global_pax | local
            name = _path(overrides.get("path", member.name))
            size = _number(overrides["size"]) if "size" in overrides else member.size
            is_directory = member.type == tarfile.DIRTYPE
            if name in seen or (name == "." and not is_directory):
                raise ValueError
            components = name.split("/")
            prefix_bytes = 0
            for index, component in enumerate(components):
                prefix_bytes += len(component.encode("utf-8")) + (index > 0)
                path_metadata += 4 * prefix_bytes + _PATH_STRING_OVERHEAD
            if path_metadata > _PATH_METADATA_LIMIT:
                raise ValueError
            parents = components[:-1]
            parent_paths = {"/".join(parents[:i]) for i in range(1, len(parents) + 1)}
            if parent_paths & files or (not is_directory and name in ancestors):
                raise ValueError
            if native_paths:
                _native_name(name, native_names)
                for parent in parent_paths:
                    _native_name(parent, native_names)
            if is_directory and size:
                raise ValueError
            clean = tarfile.TarInfo(name)
            clean.type = tarfile.DIRTYPE if is_directory else tarfile.REGTYPE
            clean.size = size
            clean.mode = member.mode & 0o777
            clean.uid = _number(overrides["uid"]) if "uid" in overrides else member.uid
            clean.gid = _number(overrides["gid"]) if "gid" in overrides else member.gid
            clean.mtime = (
                _number(overrides["mtime"], fractional=True)
                if "mtime" in overrides
                else member.mtime
            )
            if clean.uid < 0 or clean.gid < 0:
                raise ValueError
            output.addfile(clean, source if not is_directory else None)
            _padding(source, size)
            seen.add(name)
            ancestors.update(parent_paths)
            if not is_directory:
                files.add(name)
            local.clear()
            pending = 0


def _sanitize(payload: Path, directory: Path, *, native_paths: bool) -> Path:
    """Return a private canonical tar, removing partial output on any failure."""
    target, path = _temporary(directory, ".tar")
    try:
        with target, payload.open("rb") as source:
            _repack(source, target, native_paths=native_paths)
            target.flush()
            os.fsync(target.fileno())
        return path
    except BaseException:
        _cleanup(path)
        raise


def validate_archive(
    archive: Path,
    directory: Path,
    image: dict[str, Any] | None = None,
    *,
    runtime: dict[str, Any] | None = None,
    native_paths: bool = False,
) -> tuple[dict[str, Any], Path]:
    """Verify an instance backup and return its manifest and sanitized data tar.

    The caller owns the returned private tar and must remove it after use. No
    archive paths are extracted. The work directory must already be private.
    Supplying ``runtime`` accepts only v2; supplying ``image`` accepts only v1.
    Set ``native_paths`` for v1 migration to additionally reject macOS aliases.

    Raises
    ------
    ValueError
        The archive, manifest, compatibility identity, or tar content is invalid.
    OSError
        Private temporary storage or archive I/O failed.
    """
    raw: Path | None = None
    sanitized: Path | None = None
    complete = False
    try:
        version, key, identity = _compatibility(image, runtime)
        _zip_metadata(archive)
        with zipfile.ZipFile(archive) as source:
            entries = source.infolist()
            if len(entries) != 2 or {entry.filename for entry in entries} != {
                "manifest.json",
                "data.tar",
            }:
                raise ValueError
            for entry in entries:
                if (
                    entry.orig_filename != entry.filename
                    or entry.is_dir()
                    or stat.S_ISDIR(entry.external_attr >> 16)
                    or entry.flag_bits & (1 | 64 | 8192)
                    or entry.compress_type != zipfile.ZIP_STORED
                    or entry.file_size != entry.compress_size
                ):
                    raise ValueError
            _local_headers(archive, entries)
            if source.getinfo("manifest.json").file_size > _METADATA_LIMIT:
                raise ValueError
            manifest = _manifest(source.read("manifest.json"), version, key, identity)
            if source.getinfo("data.tar").file_size != manifest["payload"]["size"]:
                raise ValueError
            target, raw = _temporary(directory, ".raw.tar")
            with target, source.open("data.tar") as data:
                digest, size = _copy(data, target)
            if (
                digest != manifest["payload"]["sha256"]
                or size != manifest["payload"]["size"]
            ):
                raise ValueError
        sanitized = _sanitize(raw, directory, native_paths=native_paths or version == 2)
        _cleanup(raw)
        raw = None
        complete = True
        return manifest, sanitized
    except Exception as error:
        if isinstance(error, OSError):
            raise OSError("Backup archive I/O failed.") from None
        raise ValueError("Invalid backup archive.") from None
    finally:
        _cleanup(raw, sanitized if not complete else None)


def create_archive(
    payload: Path,
    directory: Path,
    image: dict[str, Any] | None = None,
    *,
    runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate, privately stage, verify, and atomically publish an instance backup.

    Existing archives are never overwritten. Publication uses a hard link in the
    supplied directory; the caller must ensure that directory has mode 0700.
    The result contains the absolute archive path, whole-ZIP SHA-256, verification
    status, and manifest. Supplying ``runtime`` produces v2; positional ``image``
    is retained for legacy fixtures. Errors omit paths and archived content.
    """
    temporary: Path | None = None
    sanitized: Path | None = None
    published: Path | None = None
    complete = False
    try:
        version, key, identity = _compatibility(image, runtime)
        directory = directory.resolve()
        sanitized = _sanitize(payload, directory, native_paths=version == 2)
        sanitized.unlink()
        sanitized = None
        with payload.open("rb") as source:
            digest, size = _copy(source)
        now = datetime.now(timezone.utc)
        manifest = {
            "format": _FORMAT,
            "format_version": version,
            "created_at": now.isoformat(timespec="seconds"),
            key: identity,
            "includes": _INCLUDES,
            "payload": {"sha256": digest, "size": size},
        }
        encoded = _json(manifest)
        if len(encoded) > _METADATA_LIMIT:
            raise ValueError
        target, temporary = _temporary(directory, ".zip")
        with target:
            with zipfile.ZipFile(
                target, "w", compression=zipfile.ZIP_STORED
            ) as archive:
                for name in ("manifest.json", "data.tar"):
                    entry = zipfile.ZipInfo(name)
                    entry.external_attr = (stat.S_IFREG | 0o600) << 16
                    with archive.open(entry, "w", force_zip64=True) as output:
                        if name == "manifest.json":
                            output.write(encoded)
                        else:
                            with payload.open("rb") as source:
                                _copy(source, output)
            target.flush()
            os.fsync(target.fileno())
        verified, sanitized = validate_archive(
            temporary, directory, image, runtime=runtime
        )
        sanitized.unlink()
        sanitized = None
        with temporary.open("rb") as source:
            checksum, _ = _copy(source)
        destination = directory / f"backup-{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex}.zip"
        os.link(temporary, destination)
        published = destination
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        temporary.unlink()
        temporary = None
        complete = True
        return {
            "archive": str(destination),
            "sha256": checksum,
            "verified": True,
            "manifest": verified,
        }
    except Exception as error:
        if isinstance(error, OSError):
            raise OSError("Backup archive I/O failed.") from None
        raise ValueError("Invalid backup archive.") from None
    finally:
        _cleanup(temporary, sanitized, published if not complete else None)


def _write_tree(directory: int, output: tarfile.TarFile, prefix: str = "") -> None:
    """Copy regular files and directories without following data-tree links."""
    with os.scandir(directory) as entries:
        for entry in entries:
            name = _path(prefix + entry.name)
            before = entry.stat(follow_symlinks=False)
            is_directory = stat.S_ISDIR(before.st_mode)
            if not is_directory and (
                not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            ):
                raise ValueError
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if is_directory:
                flags |= os.O_DIRECTORY
            descriptor = os.open(entry.name, flags, dir_fd=directory)
            try:
                info = os.fstat(descriptor)
                if (
                    (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino)
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o022
                    or (not is_directory and info.st_nlink != 1)
                ):
                    raise ValueError
                member = tarfile.TarInfo(name)
                member.mtime = int(info.st_mtime)
                if is_directory:
                    member.type = tarfile.DIRTYPE
                    member.mode = 0o700
                    output.addfile(member)
                    _write_tree(descriptor, output, name + "/")
                else:
                    member.size = info.st_size
                    member.mode = 0o600
                    with os.fdopen(descriptor, "rb", closefd=False) as source:
                        output.addfile(member, source)
            finally:
                os.close(descriptor)


def write_data_tar(data: Path, directory: Path) -> Path:
    """Write a private tar of the quiesced owned data tree, including empty dirs."""
    target, path = _temporary(directory, ".tar")
    complete = False
    try:
        with (
            target,
            tarfile.open(fileobj=target, mode="w", format=tarfile.GNU_FORMAT) as output,
        ):
            descriptor = os.open(data, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                _write_tree(descriptor, output)
            finally:
                os.close(descriptor)
        complete = True
        return path
    except OSError:
        raise OSError("Could not capture the FreshRSS data directory.") from None
    except ValueError:
        raise ValueError(
            "FreshRSS data contains an unsafe file or directory."
        ) from None
    finally:
        if not complete:
            _cleanup(path)


def sync_directory(directory: Path) -> None:
    """Persist changes to a real directory without following a final symlink."""
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _private_parents(directory: Path, created: set[Path]) -> None:
    """Create only exact, previously unseen directories with private modes."""
    if directory not in created:
        _private_parents(directory.parent, created)
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
        created.add(directory)


def unpack_data(payload: Path, directory: Path) -> None:
    """Copy a validated canonical tar into a new private data directory.

    Never apply archived owners, permissions, link types or other metadata.
    The caller owns and discards the unpublished staging tree on failure.
    """
    try:
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
        created = {directory}
        with tarfile.open(payload, mode="r:") as archive:
            for member in archive:
                if _path(member.name) != member.name:
                    raise ValueError
                if member.name == "." and member.isdir():
                    continue
                target = directory / member.name
                if member.isdir():
                    _private_parents(target, created)
                    continue
                if not member.isreg():
                    raise ValueError
                _private_parents(target.parent, created)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError
                with source:
                    descriptor = os.open(
                        target,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                    )
                    with os.fdopen(descriptor, "wb") as output:
                        os.fchmod(output.fileno(), 0o600)
                        _digest, size = _copy(source, output)
                        if size != member.size:
                            raise ValueError
                        output.flush()
                        os.fsync(output.fileno())
        for path in created:
            sync_directory(path)
    except OSError:
        raise OSError("Could not stage the FreshRSS data directory.") from None
    except (ValueError, tarfile.TarError):
        raise ValueError("Invalid backup data directory.") from None
