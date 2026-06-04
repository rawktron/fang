from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_SERIES = ["3.11", "3.12", "3.13"]

# Default patch version for each series when no concrete version is resolved.
_DEFAULT_PATCH: dict[str, str] = {
    "3.11": "3.11.9",
    "3.12": "3.12.3",
    "3.13": "3.13.0",
}


class DiscoveryError(Exception):
    pass


@dataclass
class EntrypointCandidate:
    module: str
    source: str  # "project.scripts" | "__main__.py"


@dataclass
class DiscoveredProject:
    """Metadata discovered from pyproject.toml, requirements.txt, and the venv."""

    name: str | None = None
    version: str | None = None
    python_requires: str | None = None      # raw requires-python specifier, e.g. ">=3.12"
    entrypoints: list[EntrypointCandidate] = field(default_factory=list)
    dep_files: list[str] = field(default_factory=list)
    venv_path: str | None = None
    venv_python_version: str | None = None   # concrete version from pyvenv.cfg, e.g. "3.12.3"
    project_root: Path = field(default_factory=Path)

    # ── resolution ─────────────────────────────────────────────────────────────

    def resolve_entry(self) -> str | None:
        """Return a unique entry module, or None if ambiguous / not found."""
        if len(self.entrypoints) == 1:
            return self.entrypoints[0].module
        if len(self.entrypoints) > 1:
            # Prefer project.scripts entries; fall back to __main__.py
            scripts = [e for e in self.entrypoints if e.source == "project.scripts"]
            if len(scripts) == 1:
                return scripts[0].module
        return None

    def resolve_python(self) -> str | None:
        """Resolve a concrete Python version, preferring the active venv."""
        if self.venv_python_version:
            return self.venv_python_version
        if not self.python_requires:
            return None
        series = _select_python_series(self.python_requires)
        if series is None:
            return None
        return _DEFAULT_PATCH.get(series)

    # ── class method ──────────────────────────────────────────────────────────

    @classmethod
    def from_directory(cls, project_root: Path) -> "DiscoveredProject":
        discovered = cls(project_root=project_root)

        pyproject = project_root / "pyproject.toml"
        if pyproject.exists():
            _read_pyproject(discovered, pyproject)

        req = project_root / "requirements.txt"
        if req.exists():
            discovered.dep_files.append(str(req))

        venv, venv_ver = _detect_venv(project_root)
        if venv:
            discovered.venv_path = str(venv)
            discovered.venv_python_version = venv_ver

        _scan_main_modules(discovered, project_root)

        return discovered


# ── private helpers ────────────────────────────────────────────────────────────

def _read_pyproject(disc: DiscoveredProject, path: Path) -> None:
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise DiscoveryError(f"pyproject.toml parse error: {e}") from e

    proj = data.get("project", {})
    disc.name = proj.get("name")
    disc.version = proj.get("version")
    disc.python_requires = proj.get("requires-python")

    # [project.scripts] → entrypoints
    for script_name, entry_spec in proj.get("scripts", {}).items():
        disc.entrypoints.append(
            EntrypointCandidate(module=entry_spec.strip(), source="project.scripts")
        )

    # [project.dependencies] → flag pyproject as a dep source
    if proj.get("dependencies"):
        disc.dep_files.append(str(path))


def _scan_main_modules(disc: DiscoveredProject, root: Path) -> None:
    """Find top-level packages that contain a __main__.py."""
    known = {e.module for e in disc.entrypoints}
    for p in sorted(root.iterdir()):
        if not p.is_dir():
            continue
        if p.name.startswith(".") or p.name in ("__pycache__", "dist", "build"):
            continue
        if (p / "__main__.py").exists() and (p / "__init__.py").exists():
            module = p.name
            if module not in known:
                disc.entrypoints.append(
                    EntrypointCandidate(module=module, source="__main__.py")
                )


def _detect_venv(root: Path) -> tuple[Path, str | None] | tuple[None, None]:
    """Return (venv_path, python_version) or (None, None)."""
    candidates: list[Path] = []
    env_venv = os.environ.get("VIRTUAL_ENV")
    if env_venv:
        env_path = Path(env_venv).resolve()
        try:
            env_path.relative_to(root.resolve())
            candidates.insert(0, env_path)
        except ValueError:
            pass
    for name in (".venv", "venv", ".env"):
        candidate = root / name
        if (candidate / "pyvenv.cfg").exists():
            candidates.append(candidate)

    for path in candidates:
        cfg = path / "pyvenv.cfg"
        if cfg.exists():
            return path, _read_pyvenv_version(cfg)
    return None, None


def _read_pyvenv_version(cfg: Path) -> str | None:
    try:
        found: dict[str, str] = {}
        for line in cfg.read_text().splitlines():
            key, _, val = line.partition("=")
            found[key.strip()] = val.strip()
        # uv writes "version_info", standard venv writes "version"
        for key in ("version_info", "version"):
            v = found.get(key, "")
            m = re.match(r"^(\d+\.\d+\.\d+)", v)
            if m:
                return m.group(1)
    except OSError:
        pass
    return None


# PEP 440 version specifier → highest satisfying supported series
_SPEC_RE = re.compile(r"([><=!~^]+)\s*([\d.]+)")


def _select_python_series(specifier: str) -> str | None:
    """Pick the highest supported series satisfying `specifier`."""
    # Parse all clauses, e.g. ">=3.11,<4"
    clauses: list[tuple[str, tuple[int, ...]]] = []
    for op, ver_str in _SPEC_RE.findall(specifier):
        parts = tuple(int(x) for x in ver_str.split("."))
        clauses.append((op, parts))

    def satisfies(series: str) -> bool:
        v = tuple(int(x) for x in series.split("."))
        for op, req in clauses:
            if op == ">=" and v < req[:len(v)]:
                return False
            if op == ">" and v <= req[:len(v)]:
                return False
            if op == "<=" and v > req[:len(v)]:
                return False
            if op == "<" and v >= req[:len(v)]:
                return False
            if op in ("==", "~=") and v != req[:len(v)]:
                return False
            if op == "!=" and v == req[:len(v)]:
                return False
        return True

    # Walk from highest to lowest and pick the first satisfying series
    for series in reversed(SUPPORTED_SERIES):
        if satisfies(series):
            return series
    return None
