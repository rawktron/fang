from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fang.discovery import DiscoveredProject


class ConfigError(Exception):
    pass


# ── raw (fang.toml) ────────────────────────────────────────────────────────────

@dataclass
class RawProject:
    name: str | None = None
    version: str | None = None
    entry: str | None = None
    package_root: str | None = None
    python: str | None = None


@dataclass
class RawBuild:
    strip: bool = False
    compress: str = "zstd"
    include_stdlib: str = "runtime"
    rtld_global: bool | None = None
    venv: str | None = None


@dataclass
class RawBundle:
    native_libs: list[str] = field(default_factory=list)


@dataclass
class RawConfig:
    project: RawProject = field(default_factory=RawProject)
    build: RawBuild = field(default_factory=RawBuild)
    bundle: RawBundle = field(default_factory=RawBundle)

    @classmethod
    def from_file(cls, path: Path) -> "RawConfig | None":
        """Return None if fang.toml does not exist."""
        try:
            data = tomllib.loads(path.read_text())
        except FileNotFoundError:
            return None
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"fang.toml parse error: {e}") from e

        proj_data = data.get("project", {})
        build_data = data.get("build", {})
        bundle_data = data.get("bundle", {})

        return cls(
            project=RawProject(
                name=proj_data.get("name"),
                version=proj_data.get("version"),
                entry=proj_data.get("entry"),
                package_root=proj_data.get("package-root"),
                python=proj_data.get("python"),
            ),
            build=RawBuild(
                strip=build_data.get("strip", False),
                compress=build_data.get("compress", "zstd"),
                include_stdlib=build_data.get("include-stdlib", "runtime"),
                rtld_global=build_data.get("rtld-global"),
                venv=build_data.get("venv"),
            ),
            bundle=RawBundle(
                native_libs=bundle_data.get("native-libs", []),
            ),
        )


# ── effective ──────────────────────────────────────────────────────────────────

@dataclass
class ProjectConfig:
    name: str
    version: str
    entry: str
    entry_callable: str | None
    package_root: str
    python: str


@dataclass
class BuildConfig:
    strip: bool
    compress: str       # "zstd"
    include_stdlib: str # "runtime" | "full"
    rtld_global: bool
    venv: Path | None


@dataclass
class BundleConfig:
    native_libs: list[str]


@dataclass
class EffectiveConfig:
    project: ProjectConfig
    build: BuildConfig
    bundle: BundleConfig
    host_platform: str
    target_platform: str
    output: Path


# ── resolution ─────────────────────────────────────────────────────────────────

def resolve(
    raw: RawConfig | None,
    *,
    cli_name: str | None = None,
    cli_python: str | None = None,
    cli_entry: str | None = None,
    cli_output: str | None = None,
    cli_target: str | None = None,
    host_platform: str,
    discovered: "DiscoveredProject | None" = None,
) -> EffectiveConfig:
    """Merge CLI flags → fang.toml → discovery → error."""
    raw = raw or RawConfig()

    name = cli_name or raw.project.name or _discovered_str(discovered, "name")
    name = _require("project.name", name, discovered)

    version = raw.project.version or _discovered_str(discovered, "version") or "0.0.0"

    entry_raw = cli_entry or raw.project.entry
    if entry_raw is None and discovered is not None:
        entry_raw = discovered.resolve_entry()
    entry_raw = _require("project.entry", entry_raw, discovered)
    entry, entry_callable = _parse_entry(entry_raw)

    package_root = raw.project.package_root or "."

    python = cli_python or raw.project.python
    if python is None and discovered is not None:
        python = discovered.resolve_python()
    python = _require("project.python", python, discovered)

    if raw.build.compress not in ("zstd",):
        raise ConfigError(f"build.compress must be 'zstd', got '{raw.build.compress}'")
    if raw.build.include_stdlib not in ("runtime", "full"):
        raise ConfigError(
            f"build.include-stdlib must be 'runtime' or 'full', "
            f"got '{raw.build.include_stdlib}'"
        )

    rtld_global = raw.build.rtld_global if raw.build.rtld_global is not None else True
    venv_str = raw.build.venv or (discovered.venv_path if discovered else None)
    venv = Path(venv_str) if venv_str else None

    target_platform = cli_target or host_platform
    output = Path(cli_output) if cli_output else Path("dist") / name

    return EffectiveConfig(
        project=ProjectConfig(
            name=name,
            version=version,
            entry=entry,
            entry_callable=entry_callable,
            package_root=package_root,
            python=python,
        ),
        build=BuildConfig(
            strip=raw.build.strip,
            compress=raw.build.compress,
            include_stdlib=raw.build.include_stdlib,
            rtld_global=rtld_global,
            venv=venv,
        ),
        bundle=BundleConfig(native_libs=raw.bundle.native_libs),
        host_platform=host_platform,
        target_platform=target_platform,
        output=output,
    )


# ── helpers ────────────────────────────────────────────────────────────────────

def _discovered_str(discovered: "DiscoveredProject | None", field: str) -> str | None:
    if discovered is None:
        return None
    return getattr(discovered, field, None)


def _require(field_name: str, value: str | None, discovered: "DiscoveredProject | None") -> str:
    if value:
        return value
    hint = " (add it to pyproject.toml or fang.toml)" if discovered is not None else ""
    raise ConfigError(f"missing required field {field_name}{hint}")


def _parse_entry(raw: str) -> tuple[str, str | None]:
    """Split 'pkg.module:callable' into (module, callable). Callable is optional."""
    if ":" in raw:
        mod, callable_ = raw.split(":", 1)
        return mod.strip(), callable_.strip() or None
    return raw.strip(), None


def generate_fang_toml(config: EffectiveConfig, discovered: "DiscoveredProject | None") -> str:
    """Generate minimal fang.toml content — only fields that differ from discovery."""
    lines = ["[project]"]
    lines.append(f'name = "{config.project.name}"')
    lines.append(f'entry = "{config.project.entry}"')
    lines.append(f'python = "{config.project.python}"')
    return "\n".join(lines) + "\n"
