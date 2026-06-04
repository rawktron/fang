"""Tests for Mach-O section injection in _write_executable.

The injection inserts the archive between __DATA and __LINKEDIT in the file,
shifting __LINKEDIT (and all its load-command references) by the archive size,
then adds a __FANG,__assets LC_SEGMENT_64 load command in the header gap.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from fang.build import (
    _inject_macho_section,
    _macho_find_linkedit,
    _macho_next_vmaddr,
    _macho_segment_data_start,
    _MACH_HEADER_64_SIZE,
    _LC_SEGMENT_64,
    _SEGMENT_CMD_64_SIZE,
    _SECTION_64_SIZE,
    _MACH_MAGIC_64,
)


# ── minimal synthetic Mach-O builder ─────────────────────────────────────────

def _pack_segment(
    segname: bytes,
    vmaddr: int,
    vmsize: int,
    fileoff: int,
    filesize: int,
    nsects: int = 0,
    maxprot: int = 1,
    initprot: int = 1,
) -> bytes:
    """Build a segment_command_64 with no sections (72 bytes)."""
    name = segname.ljust(16, b"\x00")[:16]
    cmdsize = _SEGMENT_CMD_64_SIZE + nsects * _SECTION_64_SIZE
    return struct.pack(
        "<II16sQQQQIIII",
        _LC_SEGMENT_64,
        cmdsize,
        name,
        vmaddr,
        vmsize,
        fileoff,
        filesize,
        maxprot,
        initprot,
        nsects,
        0,  # flags
    )


def _make_minimal_macho(linkedit_payload: bytes = b"ld_data") -> bytes:
    """Build a minimal but structurally valid 64-bit Mach-O binary.

    Layout:
      Header (32 B) + load commands + zero padding | __DATA data | __LINKEDIT data
    """
    _PAGE = 0x1000  # 4KB pages for test simplicity

    # We'll have four segments: __PAGEZERO, __TEXT, __DATA, __LINKEDIT.
    # Compute sizes first, then build.
    seg_pagezero = _pack_segment(b"__PAGEZERO", 0, 0x100000000, 0, 0, maxprot=0, initprot=0)
    # __TEXT covers the whole header area (fileoff=0)
    # We'll decide filesize after we know the header size.
    # __DATA starts after __TEXT.
    data_payload = b"\xda" * 16          # some fake data
    linkedit_size = len(linkedit_payload)
    data_size = len(data_payload)

    # Compute header size (rough): 32 + 4 * 72 = 320 bytes → round up to 4KB
    header_reserved = _PAGE  # 4KB for load commands + padding
    text_filesize = header_reserved
    data_fileoff = text_filesize
    linkedit_fileoff = data_fileoff + data_size

    seg_text = _pack_segment(
        b"__TEXT", 0x100000000, header_reserved, 0, text_filesize,
        maxprot=5, initprot=5,
    )
    seg_data = _pack_segment(
        b"__DATA", 0x100001000, data_size, data_fileoff, data_size,
        maxprot=3, initprot=3,
    )
    seg_linkedit = _pack_segment(
        b"__LINKEDIT", 0x100002000, linkedit_size or _PAGE, linkedit_fileoff, linkedit_size,
        maxprot=1, initprot=1,
    )

    load_cmds = seg_pagezero + seg_text + seg_data + seg_linkedit
    ncmds = 4
    sizeofcmds = len(load_cmds)

    # Build Mach-O header (32 bytes)
    header = struct.pack(
        "<IIIIIIII",
        _MACH_MAGIC_64,          # magic
        0x0100000C,              # cputype  (ARM64)
        0,                       # cpusubtype
        2,                       # filetype (MH_EXECUTE)
        ncmds,
        sizeofcmds,
        0,                       # flags
        0,                       # reserved
    )

    # Pad load commands region to header_reserved bytes
    lc_region = header + load_cmds
    assert len(lc_region) <= header_reserved
    lc_region = lc_region + b"\x00" * (header_reserved - len(lc_region))

    return lc_region + data_payload + linkedit_payload


# ── helpers ───────────────────────────────────────────────────────────────────

def _find_fang_section(binary: bytes) -> tuple[int, int] | None:
    """Return (fileoff, size) of __FANG,__assets section, or None."""
    data = bytearray(binary)
    ncmds = struct.unpack_from("<I", data, 16)[0]
    offset = _MACH_HEADER_64_SIZE
    for _ in range(ncmds):
        if len(data) < offset + 8:
            break
        cmd     = struct.unpack_from("<I", data, offset)[0]
        cmdsize = struct.unpack_from("<I", data, offset + 4)[0]
        if cmdsize == 0:
            break
        if cmd == _LC_SEGMENT_64 and len(data) >= offset + _SEGMENT_CMD_64_SIZE:
            segname = data[offset + 8 : offset + 24]
            if segname[:6] == b"__FANG":
                nsects = struct.unpack_from("<I", data, offset + 64)[0]
                so = offset + _SEGMENT_CMD_64_SIZE
                for _ in range(nsects):
                    if len(data) < so + _SECTION_64_SIZE:
                        break
                    sectname = data[so : so + 16]
                    if sectname[:8] == b"__assets":
                        size     = struct.unpack_from("<Q", data, so + 40)[0]
                        file_off = struct.unpack_from("<I", data, so + 48)[0]
                        return file_off, size
                    so += _SECTION_64_SIZE
        offset += cmdsize
    return None


# ── unit tests ────────────────────────────────────────────────────────────────

class TestMachoHelpers:
    def test_segment_data_start(self):
        binary = bytearray(_make_minimal_macho())
        ncmds = struct.unpack_from("<I", binary, 16)[0]
        # gap_end should equal __DATA.fileoff (0x1000)
        gap = _macho_segment_data_start(binary, ncmds)
        assert gap == 0x1000

    def test_next_vmaddr(self):
        binary = bytearray(_make_minimal_macho())
        ncmds = struct.unpack_from("<I", binary, 16)[0]
        next_vm = _macho_next_vmaddr(binary, ncmds)
        # Must be past __LINKEDIT's vmaddr+vmsize
        assert next_vm >= 0x100002000

    def test_find_linkedit(self):
        binary = bytearray(_make_minimal_macho(b"x" * 32))
        ncmds = struct.unpack_from("<I", binary, 16)[0]
        _, fileoff, vmaddr, vmsize = _macho_find_linkedit(binary, ncmds)
        assert fileoff == 0x1000 + 16   # data_fileoff + data_size
        assert vmaddr == 0x100002000


class TestInjectMachoSection:
    def test_section_is_readable(self):
        runtime = _make_minimal_macho()
        archive = b"hello fang archive"
        result = _inject_macho_section(runtime, archive)
        found = _find_fang_section(result)
        assert found is not None, "section not found after injection"
        foff, size = found
        assert size == len(archive)
        assert result[foff : foff + size] == archive

    def test_linkedit_is_shifted(self):
        archive = b"A" * 64
        runtime = _make_minimal_macho(b"LD" * 8)
        orig_li_fileoff = _macho_find_linkedit(bytearray(runtime), 4)[1]
        result = _inject_macho_section(runtime, archive)
        new_li_fileoff = _macho_find_linkedit(bytearray(result), 5)[1]
        assert new_li_fileoff == orig_li_fileoff + len(archive)

    def test_linkedit_data_preserved(self):
        ld_payload = b"linkedit sentinel bytes"
        runtime = _make_minimal_macho(ld_payload)
        archive = b"my archive"
        result = _inject_macho_section(runtime, archive)
        # Find new __LINKEDIT and verify its data is intact
        data = bytearray(result)
        ncmds = struct.unpack_from("<I", data, 16)[0]
        _, li_foff, _, li_vmsize = _macho_find_linkedit(data, ncmds)
        li_data = result[li_foff : li_foff + len(ld_payload)]
        assert li_data == ld_payload

    def test_ncmds_incremented(self):
        runtime = _make_minimal_macho()
        orig_ncmds = struct.unpack_from("<I", bytearray(runtime), 16)[0]
        result = _inject_macho_section(runtime, b"x")
        new_ncmds = struct.unpack_from("<I", bytearray(result), 16)[0]
        assert new_ncmds == orig_ncmds + 1

    def test_fang_vmaddr_not_in_pagezero(self):
        runtime = _make_minimal_macho()
        result = _inject_macho_section(runtime, b"data")
        data = bytearray(result)
        ncmds = struct.unpack_from("<I", data, 16)[0]
        offset = _MACH_HEADER_64_SIZE
        for _ in range(ncmds):
            cmd = struct.unpack_from("<I", data, offset)[0]
            cs  = struct.unpack_from("<I", data, offset + 4)[0]
            if cmd == _LC_SEGMENT_64 and data[offset + 8 : offset + 14] == b"__FANG":
                vmaddr = struct.unpack_from("<Q", data, offset + 24)[0]
                # Must not be inside __PAGEZERO ([0, 0x100000000))
                assert vmaddr >= 0x100000000
                break
            if cs == 0:
                break
            offset += cs


class TestWriteExecutableDispatch:
    """Verify _write_executable uses section injection for Mach-O, trailer for ELF."""

    def test_macho_uses_section(self, tmp_path):
        from fang.build import _write_executable
        runtime = _make_minimal_macho()
        archive = b"test archive bytes"
        out = tmp_path / "out"
        _write_executable(out, runtime, archive)
        result = out.read_bytes()
        assert _find_fang_section(result) is not None
        # No FANGPACK trailer at end
        assert result[-8:] != b"FANGPACK"

    def test_non_macho_uses_trailer(self, tmp_path):
        from fang.build import _write_executable
        runtime = b"ELF" + b"\x00" * 64   # fake ELF (not Mach-O magic)
        archive = b"test archive bytes"
        out = tmp_path / "out"
        _write_executable(out, runtime, archive)
        result = out.read_bytes()
        assert result[-8:] == b"FANGPACK"
