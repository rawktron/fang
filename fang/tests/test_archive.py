"""Tests for FANG archive read/write and executable extraction."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from fang.archive import (
    Archive,
    ArchiveError,
    ArchiveWriter,
    Meta,
    archive_bytes_from_executable,
    _extract_trailer,
    _extract_elf_section,
)


# ── helpers ──────────────────────────────────────────────────────────────────────

def _make_meta() -> Meta:
    return Meta(
        python_version="3.12.3",
        entry_point="app.__main__",
        platform="macos-arm64",
        build_timestamp="2024-01-01T00:00:00Z",
        project_name="testapp",
    )


def _make_archive(files: dict[str, bytes], meta: Meta | None = None) -> bytes:
    w = ArchiveWriter()
    for path, data in files.items():
        w.add_bytes(path, data)
    w.set_meta(meta or _make_meta())
    return w.build()


# ── round-trip write / read ──────────────────────────────────────────────────────

class TestRoundTrip:
    def test_single_file(self):
        data = b"hello world"
        raw = _make_archive({"app/main.py": data})
        archive = Archive(raw)
        assert archive.get("app/main.py") == data

    def test_multiple_files(self):
        files = {
            "app/a.py": b"aaa",
            "stdlib/os.py": b"bbb",
            "site-packages/click/__init__.py": b"ccc",
        }
        raw = _make_archive(files)
        archive = Archive(raw)
        for path, data in files.items():
            assert archive.get(path) == data

    def test_empty_file(self):
        raw = _make_archive({"app/empty.py": b""})
        archive = Archive(raw)
        assert archive.get("app/empty.py") == b""

    def test_binary_data(self):
        data = bytes(range(256)) * 100
        raw = _make_archive({"native-libs/foo.so": data})
        archive = Archive(raw)
        assert archive.get("native-libs/foo.so") == data

    def test_large_file(self):
        data = b"x" * 1_000_000
        raw = _make_archive({"app/big.bin": data})
        archive = Archive(raw)
        assert archive.get("app/big.bin") == data

    def test_from_file(self, tmp_path):
        data = b"from file test"
        raw = _make_archive({"app/test.py": data})
        p = tmp_path / "test.fang"
        p.write_bytes(raw)
        archive = Archive.from_file(p)
        assert archive.get("app/test.py") == data

    def test_meta_round_trips(self):
        meta = _make_meta()
        meta.native_libs = ["native-libs/pkg.libs/libhelper.so"]
        raw = _make_archive({}, meta)
        archive = Archive(raw)
        loaded = archive.meta()
        assert loaded.python_version == meta.python_version
        assert loaded.entry_point == meta.entry_point
        assert loaded.platform == meta.platform
        assert loaded.project_name == meta.project_name
        assert loaded.native_libs == ["native-libs/pkg.libs/libhelper.so"]

    def test_meta_defaults_native_libs(self):
        raw = _make_archive({})
        archive = Archive(raw)
        assert archive.meta().native_libs == []

    def test_missing_path_returns_none(self):
        raw = _make_archive({"app/main.py": b"x"})
        archive = Archive(raw)
        assert archive.get("app/missing.py") is None

    def test_path_traversal_rejected(self):
        raw = _make_archive({"app/main.py": b"x"})
        archive = Archive(raw)
        assert archive.get("../etc/passwd") is None
        assert archive.get("/etc/passwd") is None


# ── entry listing ────────────────────────────────────────────────────────────────

class TestEntryListing:
    def test_entries_sorted(self):
        files = {
            "stdlib/z.py": b"z",
            "app/a.py": b"a",
            "site-packages/b.py": b"b",
        }
        raw = _make_archive(files)
        archive = Archive(raw)
        entries = archive.entries()
        # meta/meta.json is also present
        paths = [e.path for e in entries]
        assert paths == sorted(paths)

    def test_entries_include_meta(self):
        raw = _make_archive({"app/main.py": b"x"})
        archive = Archive(raw)
        paths = {e.path for e in archive.entries()}
        assert "meta/meta.json" in paths
        assert "app/main.py" in paths

    def test_category_from_prefix(self):
        files = {
            "app/main.py": b"a",
            "stdlib/os.py": b"b",
            "site-packages/click/__init__.py": b"c",
        }
        raw = _make_archive(files)
        archive = Archive(raw)
        by_path = {e.path: e for e in archive.entries()}
        assert by_path["app/main.py"].category == "app"
        assert by_path["stdlib/os.py"].category == "stdlib"
        assert by_path["site-packages/click/__init__.py"].category == "site-packages"

    def test_entry_has_sizes(self):
        data = b"hello world"
        raw = _make_archive({"app/main.py": data})
        archive = Archive(raw)
        by_path = {e.path: e for e in archive.entries()}
        e = by_path["app/main.py"]
        assert e.uncompressed_size == len(data)
        assert e.compressed_size > 0
        assert len(e.content_hash) == 64  # hex blake3


# ── hash verification ─────────────────────────────────────────────────────────────

class TestVerification:
    def test_verify_all_passes_on_intact_archive(self):
        raw = _make_archive({"app/main.py": b"good data"})
        archive = Archive(raw)
        assert archive.verify_all() == []

    def test_get_verified_returns_data_when_hash_ok(self):
        data = b"verified data"
        raw = _make_archive({"app/main.py": data})
        archive = Archive(raw)
        assert archive.get_verified("app/main.py") == data

    def test_hash_mismatch_raises(self):
        raw = _make_archive({"app/main.py": b"original"})
        archive = Archive(raw)
        # Tamper with the stored hash so verification fails
        entry = archive._index["app/main.py"]
        entry.content_hash = bytes(32)  # all-zero hash
        with pytest.raises(ArchiveError, match="hash mismatch"):
            archive.get_verified("app/main.py")

    def test_verify_all_returns_failed_paths(self):
        raw = _make_archive({"app/main.py": b"original"})
        archive = Archive(raw)
        archive._index["app/main.py"].content_hash = bytes(32)
        failed = archive.verify_all()
        assert "app/main.py" in failed


# ── invalid archives ──────────────────────────────────────────────────────────────

class TestInvalidArchive:
    def test_truncated_raises(self):
        with pytest.raises(ArchiveError, match="truncated"):
            Archive(b"\x00" * 10)

    def test_wrong_magic_raises(self):
        with pytest.raises(ArchiveError, match="invalid magic"):
            Archive(b"XANG" + b"\x00" * 60)

    def test_wrong_version_raises(self):
        header = bytearray(64)
        header[:4] = b"FANG"
        header[4] = 99  # unsupported version
        with pytest.raises(ArchiveError, match="unsupported archive version"):
            Archive(bytes(header))

    def test_build_without_meta_raises(self):
        w = ArchiveWriter()
        w.add_bytes("app/main.py", b"x")
        with pytest.raises(ArchiveError, match="meta must be set"):
            w.build()

    def test_duplicate_path_raises(self):
        w = ArchiveWriter()
        w.set_meta(_make_meta())
        w.add_bytes("app/main.py", b"first")
        with pytest.raises(ArchiveError, match="duplicate path"):
            w.add_bytes("app/main.py", b"second")

    def test_invalid_path_prefix_raises(self):
        w = ArchiveWriter()
        w.set_meta(_make_meta())
        with pytest.raises(ArchiveError, match="invalid path"):
            w.add_bytes("bad/main.py", b"x")

    def test_path_traversal_rejected_on_write(self):
        w = ArchiveWriter()
        w.set_meta(_make_meta())
        with pytest.raises(ArchiveError, match="path traversal"):
            w.add_bytes("../evil.py", b"x")


# ── extract_all ───────────────────────────────────────────────────────────────────

class TestExtractAll:
    def test_extracts_files(self, tmp_path):
        files = {
            "app/main.py": b"main content",
            "stdlib/os.py": b"os content",
        }
        raw = _make_archive(files)
        archive = Archive(raw)
        archive.extract_all(tmp_path)
        assert (tmp_path / "app/main.py").read_bytes() == b"main content"
        assert (tmp_path / "stdlib/os.py").read_bytes() == b"os content"

    def test_creates_dest_dir(self, tmp_path):
        dest = tmp_path / "output" / "nested"
        raw = _make_archive({"app/x.py": b"x"})
        archive = Archive(raw)
        archive.extract_all(dest)
        assert (dest / "app/x.py").exists()


# ── executable extraction: FANGPACK trailer ──────────────────────────────────────

class TestFangpackTrailer:
    def _wrap(self, archive_bytes: bytes, prefix: bytes = b"ELF binary data here") -> bytes:
        trailer = struct.pack("<Q", len(archive_bytes)) + b"FANGPACK"
        return prefix + archive_bytes + trailer

    def test_extracts_archive_from_trailer(self):
        inner = _make_archive({"app/main.py": b"inner"})
        wrapped = self._wrap(inner)
        extracted = _extract_trailer(wrapped)
        archive = Archive(extracted)
        assert archive.get("app/main.py") == b"inner"

    def test_wrong_magic_raises(self):
        inner = _make_archive({"app/main.py": b"x"})
        bad = b"prefix" + inner + struct.pack("<Q", len(inner)) + b"WRONGMAG"
        with pytest.raises(ArchiveError):
            _extract_trailer(bad)

    def test_too_small_raises(self):
        with pytest.raises(ArchiveError):
            _extract_trailer(b"short")

    def test_archive_bytes_from_executable_uses_trailer(self, tmp_path):
        inner = _make_archive({"app/main.py": b"exe test"})
        wrapped = self._wrap(inner)
        exe = tmp_path / "fang"
        exe.write_bytes(wrapped)
        extracted = archive_bytes_from_executable(exe)
        archive = Archive(extracted)
        assert archive.get("app/main.py") == b"exe test"


# ── executable extraction: ELF section ───────────────────────────────────────────

class TestElfSection:
    def _build_elf(self, section_name: str, section_data: bytes) -> bytes:
        """Build a minimal ELF64 LE binary with one named section."""
        # ELF header fields
        e_shoff = 64 + len(section_data)  # section headers after section data
        e_shentsize = 64
        e_shnum = 3  # null + data + shstrtab
        e_shstrndx = 2

        # Build shstrtab: \0 + section_name\0 + ".shstrtab\0"
        shstrtab_null = b"\x00"
        shstrtab_name_off = len(shstrtab_null)
        shstrtab_content = shstrtab_null + section_name.encode() + b"\x00"
        shstrtab_self_off = len(shstrtab_content)
        shstrtab_content += b".shstrtab\x00"

        # Place shstrtab after section data
        shstrtab_file_off = 64 + len(section_data)
        e_shoff = shstrtab_file_off + len(shstrtab_content)

        # ELF header (64 bytes)
        elf_header = struct.pack(
            "<4sBBBBBxxxxxxx"  # e_ident (16 bytes)
            "HHIQQQIHHHHHH",   # remaining fields
            b"\x7fELF", 2, 1, 1, 0, 0,  # magic, class=64, data=LE, version, osabi, pad
            2,   # e_type = ET_EXEC
            0x3E, # e_machine = x86_64
            1,   # e_version
            0,   # e_entry
            0,   # e_phoff
            e_shoff,
            0,   # e_flags
            64,  # e_ehsize
            0,   # e_phentsize
            0,   # e_phnum
            e_shentsize,
            e_shnum,
            e_shstrndx,
        )
        assert len(elf_header) == 64

        # Section headers (3 × 64 = 192 bytes)
        def sh(name_off, sh_type, flags, addr, off, size, link, info, align, entsize):
            return struct.pack("<IIQQQQIIQQ",
                name_off, sh_type, flags, addr, off, size, link, info, align, entsize)

        # [0] SHT_NULL
        sh0 = sh(0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        # [1] our data section
        sh1 = sh(shstrtab_name_off, 1, 0, 0, 64, len(section_data), 0, 0, 1, 0)
        # [2] .shstrtab
        sh2 = sh(shstrtab_self_off, 3, 0, 0, shstrtab_file_off, len(shstrtab_content), 0, 0, 1, 0)

        return elf_header + section_data + shstrtab_content + sh0 + sh1 + sh2

    def test_extracts_fang_assets_section(self):
        inner = _make_archive({"app/main.py": b"elf test"})
        elf = self._build_elf("fang_assets", inner)
        extracted = _extract_elf_section(elf)
        archive = Archive(extracted)
        assert archive.get("app/main.py") == b"elf test"

    def test_missing_section_raises(self):
        inner = b"some data"
        elf = self._build_elf("not_fang_assets", inner)
        with pytest.raises(ArchiveError, match="fang_assets"):
            _extract_elf_section(elf)

    def test_not_elf_raises(self):
        with pytest.raises(ArchiveError, match="not a valid ELF"):
            _extract_elf_section(b"\x7fELF" + b"\x01" + b"\x00" * 59)  # 32-bit

    def test_archive_bytes_from_executable_falls_back_to_elf(self, tmp_path):
        inner = _make_archive({"app/main.py": b"elf fallback"})
        elf = self._build_elf("fang_assets", inner)
        exe = tmp_path / "fang"
        exe.write_bytes(elf)
        extracted = archive_bytes_from_executable(exe)
        archive = Archive(extracted)
        assert archive.get("app/main.py") == b"elf fallback"
