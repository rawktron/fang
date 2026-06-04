"""FANG archive read/write.

Binary format (little-endian):
  Header (64 bytes):
    0..4   magic b"FANG"
    4      version u8 (1)
    5..8   reserved [u8; 3]
    8..16  index_offset u64
    16..24 index_length u64
    24..32 blob_region_offset u64
    32..40 blob_region_length u64
    40..44 entry_count u32
    44..48 flags u32
    48..64 reserved [u8; 16]

  Blob region  (at blob_region_offset): concatenated zstd-compressed blobs
  Index region (at index_offset): zstd-compressed binary index

  Per index entry:
    u16      path_len
    [u8]     path (UTF-8)
    u64      offset       (from blob_region_offset)
    u64      compressed_size
    u64      uncompressed_size
    [u8; 32] content_hash (BLAKE3)
"""
from __future__ import annotations

import io
import json
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import blake3 as _blake3
import zstandard as zstd

MAGIC = b"FANG"
VERSION = 1
HEADER_SIZE = 64

VALID_PREFIXES = ("stdlib/", "site-packages/", "extensions/", "native-libs/", "app/", "meta/")


class ArchiveError(Exception):
    pass


def _zstd_decompress(data: bytes) -> bytes:
    """Decompress zstd data without requiring a content-size frame header."""
    with zstd.ZstdDecompressor().stream_reader(data) as r:
        return r.read()


# ── data types ─────────────────────────────────────────────────────────────────

@dataclass
class ArchiveEntry:
    path: str
    category: str
    compressed_size: int
    uncompressed_size: int
    content_hash: str          # hex-encoded BLAKE3


@dataclass
class Meta:
    python_version: str
    entry_point: str
    platform: str
    build_timestamp: str
    project_name: str = ""
    entry_callable: str | None = None
    extensions: dict[str, str] | None = None
    native_libs: list[str] | None = None
    rtld_global: bool = True

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "python_version": self.python_version,
            "entry_point": self.entry_point,
            "platform": self.platform,
            "build_timestamp": self.build_timestamp,
            "project_name": self.project_name,
            "rtld_global": self.rtld_global,
            "extensions": self.extensions or {},
            "native_libs": self.native_libs or [],
        }
        if self.entry_callable:
            d["entry_callable"] = self.entry_callable
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Meta":
        return cls(
            python_version=d.get("python_version", ""),
            entry_point=d.get("entry_point", ""),
            platform=d.get("platform", ""),
            build_timestamp=d.get("build_timestamp", ""),
            project_name=d.get("project_name", ""),
            entry_callable=d.get("entry_callable"),
            extensions=d.get("extensions") or {},
            native_libs=d.get("native_libs") or [],
            rtld_global=d.get("rtld_global", True),
        )


# ── internal index entry ────────────────────────────────────────────────────────

@dataclass
class _IndexEntry:
    path: str
    offset: int
    compressed_size: int
    uncompressed_size: int
    content_hash: bytes    # 32 raw bytes

    def serialize(self) -> bytes:
        path_bytes = self.path.encode()
        return (
            struct.pack("<H", len(path_bytes)) +
            path_bytes +
            struct.pack("<QQQ", self.offset, self.compressed_size, self.uncompressed_size) +
            self.content_hash
        )

    @classmethod
    def deserialize_all(cls, data: bytes, count: int) -> list["_IndexEntry"]:
        min_entry = 2 + 8 + 8 + 8 + 32  # 58 bytes minimum
        if count > len(data) // min_entry:
            raise ArchiveError(
                f"entry_count {count} exceeds what {len(data)}-byte index can hold"
            )
        entries = []
        pos = 0
        for i in range(count):
            if pos + 2 > len(data):
                raise ArchiveError(f"truncated at entry {i}")
            path_len = struct.unpack_from("<H", data, pos)[0]
            pos += 2
            if pos + path_len > len(data):
                raise ArchiveError(f"path truncated at entry {i}")
            path = data[pos:pos + path_len].decode("utf-8")
            pos += path_len
            if pos + 8 + 8 + 8 + 32 > len(data):
                raise ArchiveError(f"fields truncated at entry {i}")
            offset, compressed_size, uncompressed_size = struct.unpack_from("<QQQ", data, pos)
            pos += 24
            content_hash = data[pos:pos + 32]
            pos += 32
            entries.append(cls(path, offset, compressed_size, uncompressed_size, content_hash))
        return entries


# ── reading ─────────────────────────────────────────────────────────────────────

class Archive:
    def __init__(self, data: bytes) -> None:
        if len(data) < HEADER_SIZE:
            raise ArchiveError("truncated archive")
        if data[:4] != MAGIC:
            raise ArchiveError(f"invalid magic: {data[:4]!r}")
        version = data[4]
        if version != VERSION:
            raise ArchiveError(f"unsupported archive version {version}")

        (index_offset, index_length, blob_region_offset,
         blob_region_length, entry_count, _flags) = struct.unpack_from("<QQQQII", data, 8)

        idx_end = index_offset + index_length
        if idx_end > len(data):
            raise ArchiveError("truncated archive (index region)")

        raw_index = _zstd_decompress(data[index_offset:idx_end])
        index_entries = _IndexEntry.deserialize_all(raw_index, entry_count)

        self._data = data
        self._blob_offset = blob_region_offset
        self._index: dict[str, _IndexEntry] = {e.path: e for e in index_entries}

    @classmethod
    def from_file(cls, path: Path | str) -> "Archive":
        return cls(Path(path).read_bytes())

    def entries(self) -> list[ArchiveEntry]:
        result = []
        for e in self._index.values():
            cat = e.path.split("/")[0] if "/" in e.path else ""
            result.append(ArchiveEntry(
                path=e.path,
                category=cat,
                compressed_size=e.compressed_size,
                uncompressed_size=e.uncompressed_size,
                content_hash=e.content_hash.hex(),
            ))
        result.sort(key=lambda x: x.path)
        return result

    def get(self, path: str) -> bytes | None:
        if _has_traversal(path):
            return None
        e = self._index.get(path)
        if e is None:
            return None
        start = self._blob_offset + e.offset
        compressed = self._data[start:start + e.compressed_size]
        return _zstd_decompress(compressed)

    def get_verified(self, path: str) -> bytes | None:
        data = self.get(path)
        if data is None:
            return None
        e = self._index[path]
        actual = _blake3.blake3(data).digest()
        if actual != bytes(e.content_hash):
            raise ArchiveError(f"hash mismatch for {path}")
        return data

    def verify_all(self) -> list[str]:
        """Return paths of entries whose BLAKE3 hash fails verification."""
        failed = []
        for path in sorted(self._index):
            try:
                self.get_verified(path)
            except ArchiveError:
                failed.append(path)
        return failed

    def meta(self) -> Meta:
        data = self.get("meta/meta.json")
        if data is None:
            raise ArchiveError("archive is missing meta/meta.json")
        return Meta.from_dict(json.loads(data))

    def extract_all(self, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        for path in self._index:
            if _has_traversal(path):
                continue
            data = self.get(path)
            if data is None:
                continue
            out = dest / path
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)


# ── writing ─────────────────────────────────────────────────────────────────────

class ArchiveWriter:
    def __init__(self, compression_level: int = 3) -> None:
        self._blobs: list[tuple[str, bytes]] = []
        self._seen: set[str] = set()
        self._meta: Meta | None = None
        self._level = compression_level

    def add_bytes(self, archive_path: str, data: bytes) -> None:
        _validate_path(archive_path)
        if archive_path in self._seen:
            raise ArchiveError(f"duplicate path: {archive_path!r}")
        self._seen.add(archive_path)
        self._blobs.append((archive_path, data))

    def add_file(self, src: Path, archive_path: str) -> None:
        self.add_bytes(archive_path, src.read_bytes())

    def set_meta(self, meta: Meta) -> None:
        self._meta = meta

    def build(self) -> bytes:
        if self._meta is None:
            raise ArchiveError("meta must be set before building")

        blobs = list(self._blobs)
        blobs.append(("meta/meta.json", json.dumps(self._meta.to_dict()).encode()))

        cctx = zstd.ZstdCompressor(level=self._level)

        blob_region = bytearray()
        index_entries: list[_IndexEntry] = []
        for path, data in blobs:
            content_hash = bytes(_blake3.blake3(data).digest())
            offset = len(blob_region)
            compressed = cctx.compress(data)
            index_entries.append(_IndexEntry(
                path=path,
                offset=offset,
                compressed_size=len(compressed),
                uncompressed_size=len(data),
                content_hash=content_hash,
            ))
            blob_region.extend(compressed)

        raw_index = b"".join(e.serialize() for e in index_entries)
        compressed_index = cctx.compress(raw_index)

        blob_region_offset = HEADER_SIZE
        index_offset = blob_region_offset + len(blob_region)

        header = struct.pack(
            "<4sBBBBQQQQII16s",
            MAGIC, VERSION, 0, 0, 0,
            index_offset, len(compressed_index),
            blob_region_offset, len(blob_region),
            len(index_entries), 0,
            b"\x00" * 16,
        )
        assert len(header) == HEADER_SIZE

        return bytes(header) + bytes(blob_region) + compressed_index

    def write(self, dest: Path) -> None:
        dest.write_bytes(self.build())


# ── executable extraction ───────────────────────────────────────────────────────

def archive_bytes_from_executable(path: Path | str) -> bytes:
    """Extract the embedded FANG archive from a built binary.

    Supports macOS (FANGPACK trailer) and Linux (ELF fang_assets section).
    """
    data = Path(path).read_bytes()
    try:
        return _extract_trailer(data)
    except ArchiveError:
        pass
    return _extract_elf_section(data)


def _extract_trailer(data: bytes) -> bytes:
    TRAILER_MAGIC = b"FANGPACK"
    TRAILER_SIZE = 16  # 8 bytes length + 8 bytes magic

    if len(data) < TRAILER_SIZE:
        raise ArchiveError("too small for FANGPACK trailer")

    logical_eof = _macho_codesig_dataoff(data) or len(data)
    if logical_eof < TRAILER_SIZE or logical_eof > len(data):
        raise ArchiveError("invalid logical EOF")

    trailer_start = logical_eof - TRAILER_SIZE
    if data[trailer_start + 8:trailer_start + 16] != TRAILER_MAGIC:
        raise ArchiveError("FANGPACK magic not found")

    archive_len = struct.unpack_from("<Q", data, trailer_start)[0]
    if trailer_start < archive_len:
        raise ArchiveError("archive_len too large")

    archive_start = trailer_start - archive_len
    return data[archive_start:archive_start + archive_len]


def _extract_elf_section(data: bytes) -> bytes:
    if len(data) < 64 or data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        raise ArchiveError("not a valid ELF64 LE binary")

    shoff = struct.unpack_from("<Q", data, 40)[0]
    shentsize = struct.unpack_from("<H", data, 58)[0]
    shnum = struct.unpack_from("<H", data, 60)[0]
    shstrndx = struct.unpack_from("<H", data, 62)[0]

    if shentsize == 0 or shnum == 0 or shoff + shnum * shentsize > len(data):
        raise ArchiveError("invalid ELF section headers")

    shstr_off = shoff + shstrndx * shentsize
    shstr_offset = struct.unpack_from("<Q", data, shstr_off + 24)[0]
    shstr_size = struct.unpack_from("<Q", data, shstr_off + 32)[0]

    for i in range(shnum):
        off = shoff + i * shentsize
        name_off = struct.unpack_from("<I", data, off)[0]
        sec_offset = struct.unpack_from("<Q", data, off + 24)[0]
        sec_size = struct.unpack_from("<Q", data, off + 32)[0]

        name_abs = shstr_offset + name_off
        if name_abs >= shstr_offset + shstr_size or name_abs >= len(data):
            continue
        null = data.index(b"\x00", name_abs) if b"\x00" in data[name_abs:] else len(data)
        name = data[name_abs:null].decode("utf-8", errors="ignore")

        if name == "fang_assets" and sec_offset + sec_size <= len(data):
            return data[sec_offset:sec_offset + sec_size]

    raise ArchiveError("fang_assets ELF section not found")


def _macho_codesig_dataoff(data: bytes) -> int | None:
    MH_MAGIC_64 = 0xFEEDFACF
    LC_CODE_SIGNATURE = 0x0000001D
    HEADER_SIZE = 32

    if len(data) < HEADER_SIZE:
        return None
    magic = struct.unpack_from("<I", data, 0)[0]
    if magic != MH_MAGIC_64:
        return None
    ncmds = struct.unpack_from("<I", data, 16)[0]
    offset = HEADER_SIZE
    for _ in range(ncmds):
        if len(data) < offset + 8:
            return None
        cmd, cmdsize = struct.unpack_from("<II", data, offset)
        if cmd == LC_CODE_SIGNATURE:
            if len(data) < offset + 12:
                return None
            return struct.unpack_from("<I", data, offset + 8)[0]
        if cmdsize == 0:
            return None
        offset += cmdsize
    return None


# ── helpers ─────────────────────────────────────────────────────────────────────

def _has_traversal(path: str) -> bool:
    if path.startswith("/") or ".." in path.split("/"):
        return True
    return False


def _validate_path(path: str) -> None:
    if _has_traversal(path):
        raise ArchiveError(f"path traversal rejected: {path!r}")
    if not any(path.startswith(p) for p in VALID_PREFIXES):
        raise ArchiveError(
            f"invalid path {path!r}: must start with one of {VALID_PREFIXES}"
        )
