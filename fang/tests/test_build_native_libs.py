from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from fang.archive import ArchiveWriter
from fang.build import (
    BuildError,
    PhaseState,
    StagedFile,
    _build_extension_index,
    _native_lib_load_order,
    _stage_site_packages,
    _validate_staged_extensions,
)


def _write(path: Path, data: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_stage_site_packages_routes_libs_bundle_to_native_libs(tmp_path):
    venv = tmp_path / "venv"
    site_packages = venv / "lib/python3.12/site-packages"
    _write(site_packages / "pkg/__init__.py", b"")
    _write(site_packages / "pkg/_native.cpython-312-darwin.so", b"ext")
    _write(site_packages / "pkg.libs/libexample-hash.so.0.28", b"lib")

    writer = ArchiveWriter()
    staged = _stage_site_packages(venv, "3.12.3", writer, PhaseState("test"))

    paths = {path for path, _ in writer._blobs}
    assert "site-packages/pkg/__init__.py" in paths
    assert "extensions/pkg/_native.cpython-312-darwin.so" in paths
    assert "native-libs/pkg.libs/libexample-hash.so.0.28" in paths
    assert staged.extensions[0].archive_path == "extensions/pkg/_native.cpython-312-darwin.so"
    assert staged.native_libs[0].archive_path == "native-libs/pkg.libs/libexample-hash.so.0.28"


def test_extension_index_uses_module_name_without_platform_tag(tmp_path):
    ext = StagedFile(
        source_path=tmp_path / "pkg/_native.cpython-312-darwin.so",
        archive_path="extensions/pkg/_native.cpython-312-darwin.so",
        filename="_native.cpython-312-darwin.so",
    )

    assert _build_extension_index([ext]) == {
        "pkg._native": "extensions/pkg/_native.cpython-312-darwin.so"
    }


def test_validate_staged_extensions_fails_for_missing_native_dep(tmp_path):
    ext_path = tmp_path / "pkg/_native.so"
    _write(ext_path)
    ext = StagedFile(ext_path, "extensions/pkg/_native.so", "_native.so")

    with patch("fang.build._inspect_shared_lib_dependencies", return_value=["libmissing.so"]):
        with pytest.raises(BuildError, match="libmissing.so"):
            _validate_staged_extensions("linux-x86_64", [ext], [])


def test_validate_staged_extensions_accepts_bundled_native_dep(tmp_path):
    ext_path = tmp_path / "pkg/_native.so"
    lib_path = tmp_path / "pkg.libs/libhelper.so"
    _write(ext_path)
    _write(lib_path)
    ext = StagedFile(ext_path, "extensions/pkg/_native.so", "_native.so")
    lib = StagedFile(lib_path, "native-libs/pkg.libs/libhelper.so", "libhelper.so")

    with patch("fang.build._inspect_shared_lib_dependencies", return_value=["libhelper.so"]):
        _validate_staged_extensions("linux-x86_64", [ext], [lib])


def test_validate_staged_extensions_ignores_self_install_name(tmp_path):
    ext_path = tmp_path / "blake3/blake3.cpython-312-darwin.so"
    _write(ext_path)
    ext = StagedFile(
        ext_path,
        "extensions/blake3/blake3.cpython-312-darwin.so",
        "blake3.cpython-312-darwin.so",
    )

    with patch(
        "fang.build._inspect_shared_lib_dependencies",
        return_value=["@rpath/blake3.cpython-312-darwin.so"],
    ):
        _validate_staged_extensions("macos-arm64", [ext], [])


def test_native_lib_load_order_puts_dependencies_first(tmp_path):
    lib_a = StagedFile(tmp_path / "liba.so", "native-libs/liba.so", "liba.so")
    lib_b = StagedFile(tmp_path / "libb.so", "native-libs/libb.so", "libb.so")

    def inspect(_target: str, path: Path):
        if path == lib_a.source_path:
            return ["libb.so"]
        return []

    with patch("fang.build._inspect_shared_lib_dependencies", side_effect=inspect):
        assert _native_lib_load_order("linux-x86_64", [lib_a, lib_b], []) == [
            "native-libs/libb.so",
            "native-libs/liba.so",
        ]
