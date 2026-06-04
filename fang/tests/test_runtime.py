"""Tests for fang-runtime provider (all network-free)."""
from __future__ import annotations

import hashlib
import os
import struct
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from fang.archive import ArchiveWriter, Meta
from fang.runtime import (
    RuntimeError_,
    get_runtime,
    _fetch_from_release,
    _detect_host_platform,
    _runtime_asset_name,
    _cache_dir_for,
    SUPPORTED_RUNTIME_SERIES,
)


# ── helpers ──────────────────────────────────────────────────────────────────────

def _make_meta() -> Meta:
    return Meta(
        python_version="3.12.3",
        entry_point="fang.__main__:main",
        platform="macos-arm64",
        build_timestamp="2024-01-01T00:00:00Z",
        project_name="fang",
    )


def _make_exe_with_trailer(archive_bytes: bytes) -> bytes:
    """Wrap archive bytes in a fake executable with FANGPACK trailer."""
    trailer = struct.pack("<Q", len(archive_bytes)) + b"FANGPACK"
    return b"ELF binary stub" + archive_bytes + trailer


def _fake_releases_page(asset_name: str, tag: str = "fang-runtime-v0.1.0") -> list:
    """Return a fake GitHub releases list page containing one release with the given asset."""
    base = f"https://example.com/releases/download/{tag}"
    return [{
        "tag_name": tag,
        "assets": [
            {"name": asset_name, "browser_download_url": f"{base}/{asset_name}"},
            {"name": f"{asset_name}.sha256",
             "browser_download_url": f"{base}/{asset_name}.sha256"},
        ],
    }]


def _fake_release_detail(asset_name: str, tag: str = "fang-runtime-v0.1.0") -> dict:
    """Return a fake GitHub release detail response."""
    base = f"https://example.com/releases/download/{tag}"
    return {
        "tag_name": tag,
        "assets": [
            {"name": asset_name, "browser_download_url": f"{base}/{asset_name}"},
            {"name": f"{asset_name}.sha256",
             "browser_download_url": f"{base}/{asset_name}.sha256"},
        ],
    }


def _make_mock_get(asset_name: str, binary: bytes, digest: str,
                   tag: str = "fang-runtime-v0.1.0"):
    """Return a fake httpx.get that handles the releases list + detail + download calls."""
    def fake_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        if "releases?" in url:
            resp.json.return_value = _fake_releases_page(asset_name, tag)
        elif f"releases/tags/{tag}" in url:
            resp.json.return_value = _fake_release_detail(asset_name, tag)
        elif url.endswith(".sha256"):
            resp.content = f"{digest}  {asset_name}\n".encode()
        else:
            resp.content = binary
        return resp
    return fake_get


# ── FANG_RUNTIME_PATH override ───────────────────────────────────────────────────

class TestEnvOverride:
    def test_env_path_returned_directly(self, tmp_path):
        runtime = tmp_path / "fang-runtime"
        runtime.write_bytes(b"fake binary")
        with patch.dict(os.environ, {"FANG_RUNTIME_PATH": str(runtime)}):
            result = get_runtime("macos-arm64", "3.12.3", tmp_path)
        assert result == runtime

    def test_env_path_missing_raises(self, tmp_path):
        with patch.dict(os.environ, {"FANG_RUNTIME_PATH": str(tmp_path / "missing")}):
            with pytest.raises(RuntimeError_, match="FANG_RUNTIME_PATH"):
                get_runtime("macos-arm64", "3.12.3", tmp_path)


# ── cache hit ────────────────────────────────────────────────────────────────────

class TestCacheHit:
    def test_cache_hit_skips_download(self, tmp_path):
        binary = b"cached linux runtime"
        digest = hashlib.sha256(binary).hexdigest()
        series = SUPPORTED_RUNTIME_SERIES[0]
        dest_dir = _cache_dir_for(tmp_path, series, "3.12", "linux-x86_64")
        dest_dir.mkdir(parents=True)
        dest = dest_dir / "fang-runtime"
        dest.write_bytes(binary)
        (dest_dir / "fang-runtime.sha256").write_text(digest)

        with patch("fang.runtime.httpx.get") as mock_get, \
             patch.dict(os.environ, {}, clear=True):
            os.environ.pop("FANG_RUNTIME_PATH", None)
            result = get_runtime("linux-x86_64", "3.12.3", tmp_path)

        mock_get.assert_not_called()
        assert result == dest

    def test_corrupted_cache_re_downloads(self, tmp_path):
        binary = b"real runtime binary"
        digest = hashlib.sha256(binary).hexdigest()
        series = SUPPORTED_RUNTIME_SERIES[0]
        asset_name = _runtime_asset_name("linux-x86_64", "3.12")
        dest_dir = _cache_dir_for(tmp_path, series, "3.12", "linux-x86_64")
        dest_dir.mkdir(parents=True)
        dest = dest_dir / "fang-runtime"
        dest.write_bytes(b"corrupted")
        (dest_dir / "fang-runtime.sha256").write_text("wrong_hash" * 6)

        with patch("fang.runtime.httpx.get",
                   side_effect=_make_mock_get(asset_name, binary, digest)), \
             patch.dict(os.environ, {}, clear=True):
            os.environ.pop("FANG_RUNTIME_PATH", None)
            result = get_runtime("linux-x86_64", "3.12.3", tmp_path)

        assert result.read_bytes() == binary


# ── GitHub fetch ──────────────────────────────────────────────────────────────────

class TestGitHubFetch:
    def test_fetch_downloads_and_caches(self, tmp_path):
        asset_name = _runtime_asset_name("linux-x86_64", "3.12")
        binary = b"linux runtime binary"
        digest = hashlib.sha256(binary).hexdigest()

        with patch("fang.runtime.httpx.get",
                   side_effect=_make_mock_get(asset_name, binary, digest)), \
             patch.dict(os.environ, {}, clear=True):
            os.environ.pop("FANG_RUNTIME_PATH", None)
            result = _fetch_from_release("linux-x86_64", "3.12", tmp_path)

        assert result.read_bytes() == binary
        sidecar = result.parent / "fang-runtime.sha256"
        assert sidecar.read_text().strip() == digest

    def test_runtime_made_executable(self, tmp_path):
        import stat as _stat
        asset_name = _runtime_asset_name("linux-x86_64", "3.12")
        binary = b"linux runtime binary"
        digest = hashlib.sha256(binary).hexdigest()

        with patch("fang.runtime.httpx.get",
                   side_effect=_make_mock_get(asset_name, binary, digest)):
            result = _fetch_from_release("linux-x86_64", "3.12", tmp_path)

        assert result.stat().st_mode & _stat.S_IEXEC

    def test_sha256_mismatch_raises(self, tmp_path):
        asset_name = _runtime_asset_name("linux-x86_64", "3.12")
        binary = b"real binary"
        wrong_hash = "a" * 64

        with patch("fang.runtime.httpx.get",
                   side_effect=_make_mock_get(asset_name, binary, wrong_hash)):
            with pytest.raises(RuntimeError_, match="SHA256 mismatch"):
                _fetch_from_release("linux-x86_64", "3.12", tmp_path)

    def test_unsupported_platform_raises(self, tmp_path):
        with pytest.raises(RuntimeError_, match="unsupported platform"):
            _fetch_from_release("windows-x86_64", "3.12", tmp_path)

    def test_no_matching_release_raises(self, tmp_path):
        def fake_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            resp.json.return_value = []  # empty releases list
            return resp

        with patch("fang.runtime.httpx.get", side_effect=fake_get):
            with pytest.raises(RuntimeError_):
                _fetch_from_release("linux-x86_64", "3.12", tmp_path)

    def test_tries_next_series_on_failure(self, tmp_path):
        asset_name = _runtime_asset_name("linux-x86_64", "3.12")
        binary = b"fallback series runtime"
        digest = hashlib.sha256(binary).hexdigest()

        call_count = [0]

        def fake_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            call_count[0] += 1
            if "releases?" in url and call_count[0] == 1:
                resp.json.return_value = []  # first series: no releases
            elif "releases?" in url:
                resp.json.return_value = _fake_releases_page(asset_name, "fang-runtime-v0.2.0")
            elif "releases/tags" in url:
                resp.json.return_value = _fake_release_detail(asset_name, "fang-runtime-v0.2.0")
            elif url.endswith(".sha256"):
                resp.content = f"{digest}  {asset_name}\n".encode()
            else:
                resp.content = binary
            return resp

        with patch("fang.runtime.SUPPORTED_RUNTIME_SERIES", ["0.1", "0.2"]), \
             patch("fang.runtime.httpx.get", side_effect=fake_get):
            result = _fetch_from_release("linux-x86_64", "3.12", tmp_path)

        assert result.read_bytes() == binary


# ── asset naming ──────────────────────────────────────────────────────────────────

class TestAssetNaming:
    def test_asset_name_includes_python_version(self):
        name = _runtime_asset_name("linux-x86_64", "3.12")
        assert name == "fang-runtime-3.12-x86_64-unknown-linux-gnu"

    def test_asset_name_all_platforms(self):
        cases = {
            "linux-x86_64": "fang-runtime-3.12-x86_64-unknown-linux-gnu",
            "linux-arm64":  "fang-runtime-3.12-aarch64-unknown-linux-gnu",
            "macos-x86_64": "fang-runtime-3.12-x86_64-apple-darwin",
            "macos-arm64":  "fang-runtime-3.12-aarch64-apple-darwin",
        }
        for platform, expected in cases.items():
            assert _runtime_asset_name(platform, "3.12") == expected

    def test_unknown_platform_returns_none(self):
        assert _runtime_asset_name("windows-x86_64", "3.12") is None


# ── host platform detection ───────────────────────────────────────────────────────

class TestDetectHostPlatform:
    def test_returns_known_platform_string(self):
        result = _detect_host_platform()
        known = {"linux-x86_64", "linux-arm64", "macos-x86_64", "macos-arm64"}
        assert result in known, f"unexpected platform: {result!r}"
