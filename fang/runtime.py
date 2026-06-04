"""fang-runtime binary provider.

Priority:
  1. FANG_RUNTIME_PATH env var (dev override)
  2. Local cache (~/.fang/runtime-cache/...)
  3. Download from GitHub releases and cache
  4. Error
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

import httpx

GITHUB_REPO = "rawktron/fang"
_GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}"
_HEADERS = {"User-Agent": "fang", "Accept": "application/vnd.github+json"}

# Most-preferred runtime series first. Any patch release within a listed series
# is accepted. Add a new entry to the front when the runtime ABI changes in a
# breaking way; keep old entries for backward compatibility with previously built
# fang executables.
SUPPORTED_RUNTIME_SERIES = ["0.1"]

_PLATFORM_TRIPLES = {
    "linux-x86_64":  "x86_64-unknown-linux-gnu",
    "linux-arm64":   "aarch64-unknown-linux-gnu",
    "macos-x86_64":  "x86_64-apple-darwin",
    "macos-arm64":   "aarch64-apple-darwin",
}


class RuntimeError_(Exception):
    pass


def _python_series(version: str) -> str:
    """Extract 'major.minor' from a version string like '3.12' or '3.12.3'."""
    parts = version.split(".")
    return f"{parts[0]}.{parts[1]}"


def _runtime_asset_name(platform: str, py_series: str) -> str | None:
    """Return the release asset name, e.g. 'fang-runtime-3.12-x86_64-unknown-linux-gnu'."""
    triple = _PLATFORM_TRIPLES.get(platform)
    if triple is None:
        return None
    return f"fang-runtime-{py_series}-{triple}"


def _cache_dir_for(cache_dir: Path, runtime_series: str, py_series: str, platform: str) -> Path:
    return cache_dir / "runtime-cache" / runtime_series / py_series / platform


def get_runtime(platform: str, python_version: str, cache_dir: Path) -> Path:
    """Return path to the fang-runtime binary for `platform` and `python_version`."""
    env_path = os.environ.get("FANG_RUNTIME_PATH")
    if env_path:
        p = Path(env_path)
        if not p.exists():
            raise RuntimeError_(f"FANG_RUNTIME_PATH={env_path!r} does not exist")
        return p

    py_series = _python_series(python_version)
    return _fetch_from_release(platform, py_series, cache_dir)


# ── GitHub fetch / local cache ────────────────────────────────────────────────────

def _fetch_from_release(platform: str, py_series: str, cache_dir: Path) -> Path:
    """Fetch fang-runtime, trying each supported series in preference order."""
    asset_name = _runtime_asset_name(platform, py_series)
    if asset_name is None:
        raise RuntimeError_(f"unsupported platform: {platform!r}")

    errors: list[str] = []
    for series in SUPPORTED_RUNTIME_SERIES:
        try:
            return _fetch_series(series, asset_name, py_series, platform, cache_dir)
        except RuntimeError_ as e:
            errors.append(f"  {series}: {e}")

    raise RuntimeError_(
        f"no fang-runtime found for {platform} (python {py_series}) "
        f"in any supported series ({', '.join(SUPPORTED_RUNTIME_SERIES)}):\n"
        + "\n".join(errors)
    )


def _fetch_series(
    series: str, asset_name: str, py_series: str, platform: str, cache_dir: Path
) -> Path:
    """Fetch the latest patch release within `series` and return the cached path."""
    dest_dir = _cache_dir_for(cache_dir, series, py_series, platform)
    dest = dest_dir / "fang-runtime"
    sidecar = dest_dir / "fang-runtime.sha256"

    if dest.exists() and sidecar.exists():
        stored = sidecar.read_text().strip()
        actual = hashlib.sha256(dest.read_bytes()).hexdigest()
        if actual == stored:
            return dest
        dest.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)

    tag = _resolve_latest_patch(series, asset_name)
    asset_url, sha256_url = _find_release_asset(tag, asset_name)

    binary = _download(asset_url)
    sha256_text = _download(sha256_url).decode().split()[0]
    actual_hash = hashlib.sha256(binary).hexdigest()
    if actual_hash != sha256_text:
        raise RuntimeError_(
            f"SHA256 mismatch for {asset_name}: "
            f"expected {sha256_text}, got {actual_hash}"
        )

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(binary)
    dest.chmod(dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    sidecar.write_text(actual_hash)
    return dest


def _resolve_latest_patch(series: str, asset_name: str) -> str:
    """Return the tag for the highest patch in `series` that has `asset_name`."""
    patch_re = re.compile(rf"^fang-runtime-v{re.escape(series)}\.(\d+)$")

    best_patch: int | None = None
    best_tag: str | None = None

    page = 1
    while True:
        url = f"{_GITHUB_API}/releases?per_page=50&page={page}"
        resp = httpx.get(url, headers=_HEADERS, follow_redirects=True, timeout=30)
        resp.raise_for_status()
        releases = resp.json()
        if not releases:
            break

        for release in releases:
            tag = release.get("tag_name", "")
            m = patch_re.match(tag)
            if m is None:
                continue
            patch = int(m.group(1))
            asset_names = {a["name"] for a in release.get("assets", [])}
            if asset_name not in asset_names:
                continue
            if best_patch is None or patch > best_patch:
                best_patch = patch
                best_tag = tag

        if len(releases) < 50:
            break
        page += 1

    if best_tag is None:
        raise RuntimeError_(
            f"no release found for series {series!r} with asset {asset_name!r}"
        )
    return best_tag


def _find_release_asset(tag: str, asset_name: str) -> tuple[str, str]:
    """Return (asset_url, sha256_url) for the given tag and asset name."""
    url = f"{_GITHUB_API}/releases/tags/{tag}"
    resp = httpx.get(url, headers=_HEADERS, follow_redirects=True, timeout=30)
    if resp.status_code == 404:
        raise RuntimeError_(f"no release found for tag {tag!r}")
    resp.raise_for_status()
    release = resp.json()

    assets = {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}
    asset_url = assets.get(asset_name)
    sha256_url = assets.get(f"{asset_name}.sha256")

    if not asset_url:
        raise RuntimeError_(f"asset {asset_name!r} not found in release {tag}")
    if not sha256_url:
        raise RuntimeError_(f"sha256 sidecar for {asset_name!r} not found in release {tag}")

    return asset_url, sha256_url


def _download(url: str) -> bytes:
    resp = httpx.get(url, headers=_HEADERS, follow_redirects=True, timeout=600)
    if resp.status_code in (404, 410):
        raise RuntimeError_(f"not found: {url}")
    resp.raise_for_status()
    return resp.content


# ── host platform detection ───────────────────────────────────────────────────────

def _detect_host_platform() -> str:
    import platform as _platform
    system = _platform.system().lower()
    machine = _platform.machine().lower()

    if system == "linux":
        prefix = "linux"
    elif system == "darwin":
        prefix = "macos"
    else:
        return f"unknown-{system}"

    if machine in ("x86_64", "amd64"):
        arch = "x86_64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        return f"{prefix}-{machine}"

    return f"{prefix}-{arch}"
