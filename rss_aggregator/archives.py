"""Private, verified instance-backup archives without filesystem extraction."""

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
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

_FORMAT = "rss-aggregator-backup"
_INCLUDES = "complete FreshRSS data directory; no extensions in initial deployment"
_METADATA_LIMIT = 64 * 1024
_CHUNK = 1024 * 1024
_PAX_FIELDS = {"path", "size", "uid", "gid", "mtime"}


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
    """Encode finite JSON deterministically, including caller image metadata."""
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


def _manifest(data: bytes, image: dict[str, Any]) -> dict[str, Any]:
    """Check the complete manifest schema and exact configured image identity."""
    value = json.loads(data.decode("utf-8"), object_pairs_hook=_object)
    if not isinstance(value, dict) or set(value) != {
        "format",
        "format_version",
        "created_at",
        "image",
        "includes",
        "payload",
    }:
        raise ValueError
    if (
        value["format"] != _FORMAT
        or type(value["format_version"]) is not int
        or value["format_version"] != 1
        or value["includes"] != _INCLUDES
        or not isinstance(value["image"], dict)
        or _json(value["image"]) != _json(image)
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
        or re.fullmatch(r"[0-9a-f]{64}", payload["sha256"]) is None
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
    """Normalize export paths while rejecting traversal and ambiguous separators."""
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
    return "/".join(part for part in components if part not in {"", "."}) or "."


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


def _repack(source: BinaryIO, target: BinaryIO) -> None:
    """Validate the entire raw tar and emit only normalized files/directories.

    Header extensions are parsed with bounded allocations rather than allowing
    tarfile to load arbitrary PAX or GNU metadata. File content is streamed.
    """
    seen: set[str] = set()
    files: set[str] = set()
    ancestors: set[str] = set()
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
            parents = name.split("/")[:-1]
            parent_paths = {"/".join(parents[:i]) for i in range(1, len(parents) + 1)}
            if parent_paths & files or (not is_directory and name in ancestors):
                raise ValueError
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


def _sanitize(payload: Path, directory: Path) -> Path:
    """Return a private canonical tar, removing partial output on any failure."""
    target, path = _temporary(directory, ".tar")
    try:
        with target, payload.open("rb") as source:
            _repack(source, target)
            target.flush()
            os.fsync(target.fileno())
        return path
    except BaseException:
        _cleanup(path)
        raise


def validate_archive(
    archive: Path, directory: Path, image: dict[str, Any]
) -> tuple[dict[str, Any], Path]:
    """Verify an instance backup and return its manifest and sanitized data tar.

    The caller owns the returned private tar and must remove it after use. No
    archive paths are extracted. The work directory must already be private.

    Raises
    ------
    ValueError
        The archive, manifest, image identity, or tar content is invalid.
    OSError
        Private temporary storage or archive I/O failed.
    """
    raw: Path | None = None
    sanitized: Path | None = None
    complete = False
    try:
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
            manifest = _manifest(source.read("manifest.json"), image)
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
        sanitized = _sanitize(raw, directory)
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
    payload: Path, directory: Path, image: dict[str, Any]
) -> dict[str, Any]:
    """Validate, privately stage, verify, and atomically publish an instance backup.

    Existing archives are never overwritten. Publication uses a hard link in the
    supplied directory; the caller must ensure that directory has mode 0700.
    The result contains the absolute archive path, whole-ZIP SHA-256, verification
    status, and manifest. Errors deliberately omit paths and archived content.
    """
    temporary: Path | None = None
    sanitized: Path | None = None
    published: Path | None = None
    complete = False
    try:
        directory = directory.resolve()
        sanitized = _sanitize(payload, directory)
        sanitized.unlink()
        sanitized = None
        with payload.open("rb") as source:
            digest, size = _copy(source)
        now = datetime.now(timezone.utc)
        manifest = {
            "format": _FORMAT,
            "format_version": 1,
            "created_at": now.isoformat(timespec="seconds"),
            "image": image,
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
        verified, sanitized = validate_archive(temporary, directory, image)
        sanitized.unlink()
        sanitized = None
        with temporary.open("rb") as source:
            checksum, _ = _copy(source)
        destination = directory / f"backup-{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex}.zip"
        os.link(temporary, destination)
        published = destination
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
