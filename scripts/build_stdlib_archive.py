#!/usr/bin/env python3
"""Build the runtime stdlib fang-pack archive from a PBS tarball."""

from __future__ import annotations

import argparse
import gzip
import json
import struct
import tarfile
from pathlib import Path
from typing import BinaryIO

import blake3
import zstandard as zstd

MAGIC = b"FANG"
VERSION = 1
HEADER_SIZE = 64
COMPRESSION_LEVEL = 3

VALID_PREFIXES = ("stdlib/", "site-packages/", "extensions/", "native-libs/", "app/", "meta/")

BOOTSTRAP_CONSTS = [
    ("encodings/__pycache__/__init__.{suffix}.pyc", "ENCODINGS_BYTECODE", True),
    ("encodings/__pycache__/aliases.{suffix}.pyc", "ENCODINGS_ALIASES_BYTECODE", False),
    ("encodings/__pycache__/utf_8.{suffix}.pyc", "ENCODINGS_UTF8_BYTECODE", False),
]


class ArchiveBuildError(Exception):
    pass


def python_series(version: str) -> str:
    base = version.split("+", 1)[0]
    parts = base.split(".")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise ArchiveBuildError(f"invalid Python version: {version!r}")
    return f"{parts[0]}.{parts[1]}"


def pyc_suffix(version: str) -> str:
    series = python_series(version)
    major, minor = series.split(".")
    return f"cpython-{major}{minor}"


def platform_from_tarball_name(path: Path) -> str:
    name = path.name
    for suffix in ("-pgo+lto-full.tar.zst", "-install_only.tar.gz"):
        if name.endswith(suffix):
            stem = name[: -len(suffix)]
            marker = "+"
            if marker in stem:
                after_date = stem.split(marker, 1)[1]
                parts = after_date.split("-", 1)
                if len(parts) == 2:
                    return parts[1]
    return "unknown"


def open_tar_stream(path: Path) -> tuple[tarfile.TarFile, BinaryIO]:
    raw = path.open("rb")
    if path.name.endswith(".tar.zst"):
        reader = zstd.ZstdDecompressor().stream_reader(raw)
        return tarfile.open(fileobj=reader, mode="r|"), reader
    if path.name.endswith((".tar.gz", ".tgz")):
        reader = gzip.GzipFile(fileobj=raw)
        return tarfile.open(fileobj=reader, mode="r|"), reader
    raw.close()
    raise ArchiveBuildError(f"unsupported archive format: {path}")


def should_exclude(rel: str) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    if not rel or rel.endswith(".pyc"):
        return True
    if any(part in {"__pycache__", "test", "tests", "site-packages", "ensurepip", "idlelib",
                    "tkinter", "turtledemo", "lib2to3", "pydoc_data"} for part in parts):
        return True
    if any(part.startswith("config-") for part in parts):
        return True
    if name.startswith("_tkinter") or name.startswith("turtle"):
        return True
    return False


def validate_path(path: str) -> None:
    if path.startswith("/") or ".." in path.split("/"):
        raise ArchiveBuildError(f"path traversal rejected: {path!r}")
    if not any(path.startswith(prefix) for prefix in VALID_PREFIXES):
        raise ArchiveBuildError(f"invalid archive path: {path!r}")


def serialize_index_entry(path: str, offset: int, compressed_size: int,
                          uncompressed_size: int, content_hash: bytes) -> bytes:
    path_bytes = path.encode("utf-8")
    if len(path_bytes) > 0xFFFF:
        raise ArchiveBuildError(f"archive path too long: {path!r}")
    return (
        struct.pack("<H", len(path_bytes))
        + path_bytes
        + struct.pack("<QQQ", offset, compressed_size, uncompressed_size)
        + content_hash
    )


def build_archive(blobs: list[tuple[str, bytes]], meta: dict[str, object]) -> bytes:
    seen: set[str] = set()
    for path, _data in blobs:
        validate_path(path)
        if path in seen:
            raise ArchiveBuildError(f"duplicate archive path: {path!r}")
        seen.add(path)

    meta_json = json.dumps(meta, separators=(",", ":")).encode("utf-8")
    all_blobs = [*blobs, ("meta/meta.json", meta_json)]
    compressor = zstd.ZstdCompressor(level=COMPRESSION_LEVEL)

    blob_region = bytearray()
    index_entries = bytearray()
    for path, data in all_blobs:
        offset = len(blob_region)
        compressed = compressor.compress(data)
        blob_region.extend(compressed)
        index_entries.extend(
            serialize_index_entry(
                path=path,
                offset=offset,
                compressed_size=len(compressed),
                uncompressed_size=len(data),
                content_hash=blake3.blake3(data).digest(),
            )
        )

    compressed_index = compressor.compress(bytes(index_entries))
    blob_region_offset = HEADER_SIZE
    index_offset = blob_region_offset + len(blob_region)
    header = struct.pack(
        "<4sBBBBQQQQII16s",
        MAGIC,
        VERSION,
        0,
        0,
        0,
        index_offset,
        len(compressed_index),
        blob_region_offset,
        len(blob_region),
        len(all_blobs),
        0,
        b"\x00" * 16,
    )
    if len(header) != HEADER_SIZE:
        raise AssertionError("fang-pack header size mismatch")
    return header + bytes(blob_region) + compressed_index


def rust_byte_array(data: bytes) -> str:
    return ",".join(str(byte) for byte in data)


def write_frozen_bootstrap(path: Path, found: dict[str, tuple[bytes, bool]]) -> None:
    lines = []
    for _pattern, const_name, is_package in BOOTSTRAP_CONSTS:
        data, actual_is_package = found[const_name]
        lines.append(
            f"pub static {const_name}: (&[u8], bool) = "
            f"(&[{rust_byte_array(data)}], {str(actual_is_package).lower()});"
        )
        if actual_is_package != is_package:
            raise AssertionError("bootstrap package flag mismatch")
    path.write_text("\n".join(lines) + "\n")


def collect_stdlib(tarball: Path, version: str) -> tuple[list[tuple[str, bytes]], dict[str, tuple[bytes, bool]]]:
    series = python_series(version)
    prefixes = (
        f"python/install/lib/python{series}/",
        f"python/lib/python{series}/",
    )
    suffix = pyc_suffix(version)
    bootstrap_targets = {
        pattern.format(suffix=suffix): (const_name, is_package)
        for pattern, const_name, is_package in BOOTSTRAP_CONSTS
    }

    blobs: list[tuple[str, bytes]] = []
    frozen: dict[str, tuple[bytes, bool]] = {}

    archive, reader = open_tar_stream(tarball)
    try:
        for member in archive:
            if not member.isfile():
                continue
            path = member.name
            extracted = archive.extractfile(member)
            if extracted is None:
                continue

            for target_suffix, (const_name, is_package) in bootstrap_targets.items():
                if path.endswith(target_suffix) and const_name not in frozen:
                    data = extracted.read()
                    if len(data) <= 16:
                        raise ArchiveBuildError(f"bootstrap .pyc too short: {path}")
                    frozen[const_name] = (data[16:], is_package)
                    break
            else:
                rel = next((path.removeprefix(prefix) for prefix in prefixes if path.startswith(prefix)), None)
                if rel is None or should_exclude(rel):
                    continue
                blobs.append((f"stdlib/{rel}", extracted.read()))
    finally:
        archive.close()
        reader.close()

    if not blobs:
        raise ArchiveBuildError(f"did not find stdlib files for Python {series} in {tarball}")
    missing = [const_name for _pattern, const_name, _pkg in BOOTSTRAP_CONSTS if const_name not in frozen]
    if missing:
        raise ArchiveBuildError(f"missing bootstrap .pyc files: {', '.join(missing)}")
    return blobs, frozen


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tarball", required=True, type=Path)
    parser.add_argument("--python-version", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--frozen-out", type=Path)
    args = parser.parse_args()

    frozen_out = args.frozen_out or args.out.with_name("frozen_bootstrap.rs")
    blobs, frozen = collect_stdlib(args.tarball, args.python_version)
    meta = {
        "python_version": args.python_version,
        "entry_point": "__fang_runtime_stdlib__",
        "entry_callable": None,
        "platform": platform_from_tarball_name(args.tarball),
        "build_timestamp": "runtime-stdlib",
        "project_name": "fang-runtime-stdlib",
        "extensions": {},
        "native_libs": [],
        "rtld_global": True,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    frozen_out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(build_archive(blobs, meta))
    write_frozen_bootstrap(frozen_out, frozen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
