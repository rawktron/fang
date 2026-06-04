"""fang build pipeline."""
from __future__ import annotations

import os
import subprocess
import struct
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from fang.archive import ArchiveWriter, Meta
from fang.config import EffectiveConfig, RawConfig, ConfigError, resolve as resolve_config
from fang.discovery import DiscoveredProject
from fang.output import is_fancy
from fang.runtime import get_runtime, _detect_host_platform
from fang.theme import FANG_THEME


class BuildError(Exception):
    pass


@dataclass
class StagedFile:
    source_path: Path
    archive_path: str
    filename: str


@dataclass
class DependencyStageResult:
    count: int = 0
    extensions: list[StagedFile] = field(default_factory=list)
    native_libs: list[StagedFile] = field(default_factory=list)


# ── CLI entry point ───────────────────────────────────────────────────────────────

def run(
    ctx: "click.Context",
    *,
    output: str | None = None,
    target: str | None = None,
    python: str | None = None,
    entry: str | None = None,
    name: str | None = None,
) -> None:
    """CLI adapter called by cli.py."""
    import click
    from fang.cli import make_console
    from fang.output import is_fancy as _is_fancy

    console = make_console(ctx)
    fancy = _is_fancy(ctx.obj)
    cache_dir = Path(os.environ.get("FANG_CACHE_DIR", Path.home() / ".fang"))
    project_dir = Path.cwd()

    try:
        result = _run_pipeline(
            project_dir,
            cache_dir=cache_dir,
            cli_name=name,
            cli_python=python,
            cli_entry=entry,
            cli_output=output,
            cli_target=target,
            console=console,
            fancy=fancy,
        )
    except BuildError as e:
        if ctx.obj.get("output_json"):
            import json
            click.echo(json.dumps({"status": "error", "error": str(e)}))
        else:
            console.print(f"  [fang.error]✗[/]  {e}")
        ctx.exit(1)
        return

    if ctx.obj.get("output_json"):
        import json
        click.echo(json.dumps({"status": "ok", "output": str(result)}))
    else:
        console.print(f"  [fang.success]✓[/]  [fang.path]{result}[/]")


# ── phase tracking ────────────────────────────────────────────────────────────────

@dataclass
class PhaseState:
    label: str
    status: str = "pending"   # pending | running | done | failed
    detail: str = ""
    current: int = 0
    total: int = 0
    elapsed_ms: float = 0.0
    _started: float = field(default=0.0, repr=False)

    def start(self) -> None:
        self.status = "running"
        self._started = time.monotonic()

    def finish(self, detail: str = "") -> None:
        self.elapsed_ms = (time.monotonic() - self._started) * 1000
        self.status = "done"
        self.detail = detail

    def fail(self, detail: str) -> None:
        self.elapsed_ms = (time.monotonic() - self._started) * 1000
        self.status = "failed"
        self.detail = detail


# ── live panel ────────────────────────────────────────────────────────────────────

class BuildPanel:
    PHASES = [
        ("discover",  "Discover project"),
        ("config",    "Resolve config"),
        ("venv",      "Stage site-packages"),
        ("app",       "Stage app"),
        ("compile",   "Compile bytecode"),
        ("archive",   "Assemble archive"),
        ("runtime",   "Embed runtime"),
        ("link",      "Write executable"),
    ]

    def __init__(self, console: Console) -> None:
        self._console = console
        self._phases: dict[str, PhaseState] = {
            k: PhaseState(label=v) for k, v in self.PHASES
        }
        self._order = [k for k, _ in self.PHASES]
        self._spinner = Spinner("dots", style="fang.active")
        self._live: Live | None = None

    def __enter__(self) -> "BuildPanel":
        self._live = Live(
            self._render(),
            console=self._console,
            refresh_per_second=12,   # ~80ms
            transient=False,
        )
        self._live.__enter__()
        return self

    def __exit__(self, *args: object) -> None:
        if self._live:
            self._live.update(self._render())
            self._live.__exit__(*args)

    def phase(self, name: str) -> PhaseState:
        return self._phases[name]

    def update(self) -> None:
        if self._live:
            self._live.update(self._render())

    def _render(self) -> Table:
        t = Table.grid(padding=(0, 1))
        t.add_column(width=2)   # icon
        t.add_column(width=22)  # label
        t.add_column()          # detail / progress

        for key in self._order:
            p = self._phases[key]
            if p.status == "pending":
                icon = Text("·", style="fang.muted")
                label = Text(p.label, style="fang.muted")
                detail = Text("")
            elif p.status == "running":
                icon = self._spinner
                label = Text(p.label, style="fang.content")
                if p.total > 0:
                    pct = p.current / p.total
                    bar_w = 20
                    filled = int(bar_w * pct)
                    bar = "█" * filled + "░" * (bar_w - filled)
                    detail = Text(f"{bar} {p.current}/{p.total}", style="fang.muted")
                else:
                    detail = Text(p.detail or "", style="fang.muted")
            elif p.status == "done":
                icon = Text("✓", style="fang.success")
                label = Text(p.label, style="fang.content")
                ms = p.elapsed_ms
                timing = f"{ms:.0f}ms" if ms < 1000 else f"{ms/1000:.1f}s"
                detail_str = f"{p.detail}  [{timing}]" if p.detail else f"[{timing}]"
                detail = Text(detail_str, style="fang.muted")
            else:  # failed
                icon = Text("✗", style="fang.error")
                label = Text(p.label, style="fang.error")
                detail = Text(p.detail, style="fang.error")

            t.add_row(icon, label, detail)
        return t


# ── build pipeline ────────────────────────────────────────────────────────────────

def _run_pipeline(
    project_dir: Path,
    *,
    cache_dir: Path,
    cli_name: str | None = None,
    cli_python: str | None = None,
    cli_entry: str | None = None,
    cli_output: str | None = None,
    cli_target: str | None = None,
    console: Console,
    fancy: bool = True,
) -> Path:
    """Run the full build pipeline and return the output executable path."""
    host = _detect_host_platform()
    panel = BuildPanel(console)

    ctx_mgr = panel if fancy else _NullContext(panel)
    with ctx_mgr:
        try:
            return _run_phases(
                project_dir, cache_dir=cache_dir,
                cli_name=cli_name, cli_python=cli_python,
                cli_entry=cli_entry, cli_output=cli_output,
                cli_target=cli_target, host=host,
                panel=panel, console=console, fancy=fancy,
            )
        except BuildError:
            raise
        except Exception as e:
            raise BuildError(str(e)) from e


def _run_phases(
    project_dir: Path,
    *,
    cache_dir: Path,
    cli_name: str | None,
    cli_python: str | None,
    cli_entry: str | None,
    cli_output: str | None,
    cli_target: str | None,
    host: str,
    panel: BuildPanel,
    console: Console,
    fancy: bool,
) -> Path:

    # ── discover ──────────────────────────────────────────────────────────────────
    p = panel.phase("discover")
    p.start()
    try:
        discovered = DiscoveredProject.from_directory(project_dir)
        fang_toml = project_dir / "fang.toml"
        raw = RawConfig.from_file(fang_toml)
        p.finish(discovered.name or "")
    except Exception as e:
        p.fail(str(e))
        raise BuildError(f"discovery failed: {e}") from e
    panel.update()

    # ── config ────────────────────────────────────────────────────────────────────
    p = panel.phase("config")
    p.start()
    try:
        cfg = resolve_config(
            raw,
            cli_name=cli_name,
            cli_python=cli_python,
            cli_entry=cli_entry,
            cli_output=cli_output,
            cli_target=cli_target,
            host_platform=host,
            discovered=discovered,
        )
        p.finish(f"{cfg.project.name} → {cfg.target_platform}")
    except ConfigError as e:
        p.fail(str(e))
        raise BuildError(str(e)) from e
    panel.update()

    # ── runtime preflight ─────────────────────────────────────────────────────────
    # Validate the runtime is obtainable before doing any work.
    _preflight_runtime(cfg.target_platform, cfg.project.python, cache_dir)

    with tempfile.TemporaryDirectory(prefix="fang-build-") as _tmpdir:
        tmp = Path(_tmpdir)
        writer = ArchiveWriter()

        # ── site-packages ─────────────────────────────────────────────────────────
        p = panel.phase("venv")
        p.start()
        panel.update()
        try:
            venv_path = cfg.build.venv
            if venv_path is None:
                venv_path = _uv_install_deps(cfg, project_dir, tmp, p)
            deps = _stage_site_packages(venv_path, cfg.project.python, writer, p)
            _stage_requested_native_libs(cfg, writer, deps)
            _validate_staged_extensions(cfg.target_platform, deps.extensions, deps.native_libs)
            p.finish(f"{deps.count} files")
        except Exception as e:
            p.fail(str(e))
            raise BuildError(f"site-packages staging failed: {e}") from e
        panel.update()

        # ── app ───────────────────────────────────────────────────────────────────
        p = panel.phase("app")
        p.start()
        panel.update()
        try:
            n = _stage_app(project_dir, cfg, writer, panel.phase("app"))
            p.finish(f"{n} files")
        except Exception as e:
            p.fail(str(e))
            raise BuildError(f"app staging failed: {e}") from e
        panel.update()

        # ── compile ───────────────────────────────────────────────────────────────
        p = panel.phase("compile")
        p.start()
        compile_dir = tmp / "compile"
        compile_dir.mkdir()
        panel.update()
        try:
            n = _compile_bytecode(writer, compile_dir, panel.phase("compile"))
            p.finish(f"{n} .pyc files")
        except Exception as e:
            p.fail(str(e))
            raise BuildError(f"bytecode compilation failed: {e}") from e
        panel.update()

        # ── archive ───────────────────────────────────────────────────────────────
        p = panel.phase("archive")
        p.start()
        panel.update()
        try:
            meta = Meta(
                python_version=cfg.project.python,
                entry_point=cfg.project.entry,
                platform=cfg.target_platform,
                build_timestamp=_iso_now(),
                project_name=cfg.project.name,
                entry_callable=cfg.project.entry_callable,
                extensions=_build_extension_index(deps.extensions),
                native_libs=_native_lib_load_order(
                    cfg.target_platform,
                    deps.native_libs,
                    deps.extensions,
                ),
                rtld_global=cfg.build.rtld_global,
            )
            writer.set_meta(meta)
            archive_bytes = writer.build()
            p.finish(_fmt_bytes(len(archive_bytes)))
        except Exception as e:
            p.fail(str(e))
            raise BuildError(f"archive assembly failed: {e}") from e
        panel.update()

        # ── runtime ───────────────────────────────────────────────────────────────
        p = panel.phase("runtime")
        p.start()
        panel.update()
        try:
            runtime_path = get_runtime(cfg.target_platform, cfg.project.python, cache_dir)
            runtime_bytes = runtime_path.read_bytes()
            p.finish(_fmt_bytes(len(runtime_bytes)))
        except Exception as e:
            p.fail(str(e))
            raise BuildError(f"runtime fetch failed: {e}") from e
        panel.update()

        # ── link ──────────────────────────────────────────────────────────────────
        p = panel.phase("link")
        p.start()
        panel.update()
        try:
            output = cfg.output
            _write_executable(output, runtime_bytes, archive_bytes)
            p.finish(str(output))
        except Exception as e:
            p.fail(str(e))
            raise BuildError(f"link failed: {e}") from e
        panel.update()

    return cfg.output


# ── preflight ────────────────────────────────────────────────────────────────────

def _preflight_runtime(platform: str, python_version: str, cache_dir: Path) -> None:
    """Fast-path checks before starting the build. Fails early on definitive errors only.

    If no fast path is available, we proceed — the runtime phase will fetch from
    GitHub and surface any network errors there with full context.
    """
    import os, sys
    from fang.runtime import SUPPORTED_RUNTIME_SERIES, _python_series, _cache_dir_for

    # 1. Explicit override — fail immediately if the path doesn't exist.
    env_path = os.environ.get("FANG_RUNTIME_PATH")
    if env_path:
        if Path(env_path).exists():
            return
        raise BuildError(f"FANG_RUNTIME_PATH={env_path!r} does not exist")

    py_series = _python_series(python_version)

    # 2. Already cached — fast path, no network needed.
    for series in SUPPORTED_RUNTIME_SERIES:
        cached = _cache_dir_for(cache_dir, series, py_series, platform) / "fang-runtime"
        if cached.exists():
            return

    # No cache hit — the runtime phase will download from GitHub releases.


# ── staging helpers ───────────────────────────────────────────────────────────────

_STDLIB_SKIP_EXTS = frozenset([".pyc", ".pyo"])


def _uv_install_deps(
    cfg: EffectiveConfig,
    project_dir: Path,
    tmp: Path,
    phase: PhaseState,
) -> Path | None:
    """Create a temp venv via uv and install the project's dependencies into it."""
    import shutil, subprocess

    if not shutil.which("uv"):
        return None
    if not (project_dir / "pyproject.toml").exists() and not (project_dir / "requirements.txt").exists():
        return None

    venv = tmp / "deps-venv"
    phase.detail = "uv install…"
    subprocess.run(
        ["uv", "venv", "--python", cfg.project.python, str(venv)],
        check=True, capture_output=True,
    )
    install_args = ["uv", "pip", "install", f"--python={venv / 'bin' / 'python'}"]
    if (project_dir / "pyproject.toml").exists():
        install_args.append(str(project_dir))
    else:
        install_args += ["-r", str(project_dir / "requirements.txt")]
    subprocess.run(install_args, check=True, capture_output=True)
    return venv


def _stage_site_packages(
    venv_path: Path | None,
    python_version: str,
    writer: ArchiveWriter,
    phase: PhaseState,
) -> DependencyStageResult:
    """Collect site-packages from a venv."""
    staged = DependencyStageResult()
    if venv_path is None:
        return staged

    python_series = ".".join(python_version.split(".")[:2])
    sp_dir = venv_path / "lib" / f"python{python_series}" / "site-packages"
    if not sp_dir.exists():
        sp_dir = venv_path / "lib" / "site-packages"
    if not sp_dir.exists():
        return staged

    files = sorted(sp_dir.rglob("*"))
    phase.total = len(files)

    for src in files:
        if not src.is_file():
            continue
        rel = src.relative_to(sp_dir)
        parts = rel.parts
        # Skip dist-info and __pycache__
        if any(p.endswith(".dist-info") or p == "__pycache__" for p in parts):
            continue
        if src.suffix in _STDLIB_SKIP_EXTS:
            continue

        rel_posix = rel.as_posix()
        if _top_level_libs_dir(rel) is not None:
            archive_path = f"native-libs/{rel_posix}"
            target = staged.native_libs
        elif _is_extension_file(src):
            archive_path = f"extensions/{rel_posix}"
            target = staged.extensions
        else:
            archive_path = f"site-packages/{rel_posix}"
            target = None

        try:
            writer.add_bytes(archive_path, src.read_bytes())
            staged.count += 1
            if target is not None:
                target.append(StagedFile(
                    source_path=src,
                    archive_path=archive_path,
                    filename=src.name,
                ))
        except Exception:
            pass
        phase.current = staged.count

    return staged


def _top_level_libs_dir(rel: Path) -> str | None:
    if not rel.parts:
        return None
    first = rel.parts[0]
    return first if first.endswith(".libs") else None


def _is_extension_file(path: Path) -> bool:
    return path.suffix in (".so", ".dylib")


def _is_macos_helper_dylib(path: Path) -> bool:
    return path.suffix == ".dylib" and ".cpython-" not in path.name


def _stage_requested_native_libs(
    cfg: EffectiveConfig,
    writer: ArchiveWriter,
    deps: DependencyStageResult,
) -> None:
    for lib in cfg.bundle.native_libs:
        names = {lib, f"{lib}.so", f"{lib}.dylib"}
        found = next((item for item in deps.extensions if item.filename in names), None)
        if found is None:
            raise BuildError(f"missing native library dependency requested in bundle: {lib}")
        archive_path = f"native-libs/{found.filename}"
        writer.add_bytes(archive_path, found.source_path.read_bytes())
        deps.native_libs.append(StagedFile(
            source_path=found.source_path,
            archive_path=archive_path,
            filename=found.filename,
        ))


def _build_extension_index(extensions: list[StagedFile]) -> dict[str, str]:
    index: dict[str, str] = {}
    for item in extensions:
        rel = item.archive_path.removeprefix("extensions/")
        module_name = _extension_path_to_module_name(rel)
        index[module_name] = item.archive_path
    return index


def _extension_path_to_module_name(rel: str) -> str:
    parts = rel.split("/")
    filename = parts[-1]
    stem = filename.split(".", 1)[0]
    return ".".join([*parts[:-1], stem])


def _validate_staged_extensions(
    target_platform: str,
    extensions: list[StagedFile],
    native_libs: list[StagedFile],
) -> None:
    provider_names = _dependency_provider_filenames(target_platform, native_libs, extensions)
    for ext in extensions:
        if not _is_extension_file(ext.source_path):
            continue
        deps = _inspect_shared_lib_dependencies(target_platform, ext.source_path)
        if deps is None:
            continue
        for dep in deps:
            if _is_known_system_library(target_platform, dep):
                continue
            dep_name = _dependency_basename(dep)
            if dep_name == ext.filename:
                continue
            if dep_name not in provider_names:
                raise BuildError(
                    f"missing native library dependency {dep_name!r} "
                    f"required by {ext.source_path}"
                )


def _dependency_provider_filenames(
    target_platform: str,
    native_libs: list[StagedFile],
    extensions: list[StagedFile],
) -> set[str]:
    names = {item.filename for item in native_libs}
    if _is_macos(target_platform):
        names.update(
            item.filename
            for item in extensions
            if _is_macos_helper_dylib(item.source_path)
        )
    return names


def _native_lib_load_order(
    target_platform: str,
    native_libs: list[StagedFile],
    extensions: list[StagedFile],
) -> list[str]:
    libs = list(native_libs)
    if _is_macos(target_platform):
        libs.extend(
            item for item in extensions
            if _is_macos_helper_dylib(item.source_path)
        )
    if not libs:
        return []

    by_filename = {item.filename: item.archive_path for item in libs}
    deps_by_archive_path: dict[str, list[str]] = {}
    for item in libs:
        deps_by_archive_path[item.archive_path] = []
        deps = _inspect_shared_lib_dependencies(target_platform, item.source_path)
        if deps is None:
            continue
        for dep in deps:
            if _is_known_system_library(target_platform, dep):
                continue
            dep_archive_path = by_filename.get(_dependency_basename(dep))
            if dep_archive_path and dep_archive_path != item.archive_path:
                deps_by_archive_path[item.archive_path].append(dep_archive_path)

    return _topo_sort_native_libs([item.archive_path for item in libs], deps_by_archive_path)


def _topo_sort_native_libs(
    archive_paths: list[str],
    deps_by_archive_path: dict[str, list[str]],
) -> list[str]:
    temporary: set[str] = set()
    permanent: set[str] = set()
    ordered: list[str] = []

    def visit(node: str) -> None:
        if node in permanent:
            return
        if node in temporary:
            return
        temporary.add(node)
        for dep in sorted(deps_by_archive_path.get(node, [])):
            visit(dep)
        temporary.remove(node)
        permanent.add(node)
        ordered.append(node)

    for node in sorted(set(archive_paths)):
        visit(node)
    return ordered


def _inspect_shared_lib_dependencies(target_platform: str, path: Path) -> list[str] | None:
    if _is_macos(target_platform):
        cmd = ["otool", "-L", str(path)]
        parser = _parse_otool_dependencies
    else:
        cmd = ["ldd", str(path)]
        parser = _parse_ldd_dependencies
    try:
        output = subprocess.run(cmd, check=False, capture_output=True, text=True)
    except FileNotFoundError:
        _warn(f"{cmd[0]} not found; skipping native dependency inspection for {path}")
        return None
    except OSError as e:
        _warn(f"failed to run {cmd[0]} for {path}: {e}; skipping native dependency inspection")
        return None
    if output.returncode != 0:
        detail = output.stderr.strip()
        _warn(f"{cmd[0]} failed for {path}: {detail}; skipping native dependency inspection")
        return None
    return parser(output.stdout)


def _parse_ldd_dependencies(output: str) -> list[str]:
    deps = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or "statically linked" in line:
            continue
        name = line.split("=>", 1)[0].strip() if "=>" in line else line.split()[0]
        if name.endswith(":"):
            continue
        deps.append(name)
    return deps


def _parse_otool_dependencies(output: str) -> list[str]:
    deps = []
    for raw_line in output.splitlines()[1:]:
        line = raw_line.strip()
        if not line:
            continue
        deps.append(line.split(" (", 1)[0].strip())
    return deps


def _dependency_basename(dep: str) -> str:
    return dep.rsplit("/", 1)[-1]


def _is_known_system_library(target_platform: str, dep: str) -> bool:
    base = _dependency_basename(dep)
    if _is_macos(target_platform):
        return (
            dep.startswith("/System/Library/")
            or dep.startswith("/usr/lib/")
            or base == "libSystem.B.dylib"
        )
    return (
        base in {
            "libc.so.6",
            "libpthread.so.0",
            "libm.so.6",
            "libdl.so.2",
            "libgcc_s.so.1",
            "libstdc++.so.6",
            "ld-linux-x86-64.so.2",
            "ld-linux-aarch64.so.1",
        }
        or base.startswith("linux-vdso.so")
    )


def _is_macos(target_platform: str) -> bool:
    return target_platform.startswith("macos-")


def _warn(message: str) -> None:
    import sys
    print(f"fang: warning: {message}", file=sys.stderr)


def _stage_app(
    project_dir: Path,
    cfg: EffectiveConfig,
    writer: ArchiveWriter,
    phase: PhaseState,
) -> int:
    """Stage the application package."""
    # The entry module is like "fang.__main__" or "myapp"; the root package is the first component.
    root_pkg = cfg.project.entry.split(".")[0]
    pkg_root = Path(cfg.project.package_root)
    if not pkg_root.is_absolute():
        pkg_root = project_dir / pkg_root

    pkg_dir = pkg_root / root_pkg
    if not pkg_dir.is_dir():
        raise BuildError(f"package directory not found: {pkg_dir}")

    count = 0
    files = sorted(pkg_dir.rglob("*"))
    phase.total = len(files)

    for src in files:
        if not src.is_file():
            continue
        if src.suffix in _STDLIB_SKIP_EXTS:
            continue
        if "__pycache__" in src.parts:
            continue
        rel = src.relative_to(pkg_root)
        archive_path = f"app/{rel.as_posix()}"
        try:
            writer.add_bytes(archive_path, src.read_bytes())
            count += 1
        except Exception:
            pass
        phase.current = count

    return count


def _compile_bytecode(
    writer: ArchiveWriter,
    compile_dir: Path,
    phase: PhaseState,
) -> int:
    """Bytecode-compile staged .py files and add .pyc entries to the archive."""
    # Collect all .py entries already queued in writer
    py_entries = [(path, data) for path, data in writer._blobs if path.endswith(".py")]
    phase.total = len(py_entries)
    count = 0

    import py_compile
    import importlib.util

    for archive_path, src_bytes in py_entries:
        src_file = compile_dir / archive_path.replace("/", "_")
        src_file.write_bytes(src_bytes)
        pyc_file = src_file.with_suffix(".pyc")
        try:
            py_compile.compile(str(src_file), cfile=str(pyc_file), doraise=True)
            pyc_archive = archive_path[:-3] + ".pyc"
            writer.add_bytes(pyc_archive, pyc_file.read_bytes())
            count += 1
        except py_compile.PyCompileError:
            pass  # syntax errors in stdlib stubs etc — skip
        phase.current = count

    return count


# ── executable writing ────────────────────────────────────────────────────────────

_MACH_MAGIC_64 = 0xFEEDFACF
_LC_SEGMENT_64 = 0x19
_LC_SYMTAB = 0x2
_LC_DYSYMTAB = 0xB
_LC_DYLD_INFO = 0x22
_LC_DYLD_INFO_ONLY = 0x80000022
_LC_FUNCTION_STARTS = 0x26
_LC_DATA_IN_CODE = 0x29
_LC_CODE_SIGNATURE = 0x1D
_LC_DYLD_EXPORTS_TRIE = 0x33
_LC_DYLD_CHAINED_FIXUPS = 0x34
_MACH_HEADER_64_SIZE = 32
_SEGMENT_CMD_64_SIZE = 72   # sizeof(segment_command_64)
_SECTION_64_SIZE = 80       # sizeof(section_64)
_FANG_LC_SIZE = _SEGMENT_CMD_64_SIZE + _SECTION_64_SIZE  # = 152


def _write_executable(output: Path, runtime_bytes: bytes, archive_bytes: bytes) -> None:
    """Write runtime + embedded archive to output path and make executable."""
    import stat

    if (
        len(runtime_bytes) >= 4
        and struct.unpack_from("<I", runtime_bytes, 0)[0] == _MACH_MAGIC_64
    ):
        data = _inject_macho_section(runtime_bytes, archive_bytes)
    else:
        trailer = struct.pack("<Q", len(archive_bytes)) + b"FANGPACK"
        data = runtime_bytes + archive_bytes + trailer

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    output.chmod(output.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _inject_macho_section(runtime_bytes: bytes, archive_bytes: bytes) -> bytes:
    """Inject archive bytes as __FANG,__assets section into a Mach-O 64-bit binary.

    Inserts the archive between __DATA and __LINKEDIT in the file, shifting
    __LINKEDIT (and all load command file-offset references to it) by
    len(archive_bytes).  A new LC_SEGMENT_64 + section_64 load command (152 bytes)
    for __FANG,__assets is written into the header gap.  __LINKEDIT remains the
    last segment in the file, satisfying codesign strict validation.
    """
    src = bytearray(runtime_bytes)

    ncmds     = struct.unpack_from("<I", src, 16)[0]
    sizeofcmds = struct.unpack_from("<I", src, 20)[0]

    # Verify header gap can hold our new load command.
    gap_end = _macho_segment_data_start(src, ncmds)
    load_cmds_end = _MACH_HEADER_64_SIZE + sizeofcmds
    if gap_end - load_cmds_end < _FANG_LC_SIZE:
        raise BuildError(
            f"Mach-O header padding too small for fang section "
            f"(need {_FANG_LC_SIZE}B, have {gap_end - load_cmds_end}B)"
        )

    # Find __LINKEDIT.
    li_cmd_off, li_fileoff, li_vmaddr, li_vmsize = _macho_find_linkedit(src, ncmds)

    archive_size = len(archive_bytes)
    # Keep the following mapped segment (__LINKEDIT) page-aligned.  Mach-O
    # segment file offsets are loader-visible; shifting __LINKEDIT by an
    # arbitrary archive length can produce a binary that codesign can parse but
    # the kernel kills at exec time.
    _PAGE = 0x4000
    archive_span = (archive_size + _PAGE - 1) & ~(_PAGE - 1)
    archive_padding = archive_span - archive_size

    # Build new binary: everything before __LINKEDIT, then archive, then __LINKEDIT.
    data = bytearray(src[:li_fileoff])
    data += bytearray(archive_bytes)
    data += bytearray(archive_padding)
    data += bytearray(src[li_fileoff:])

    # Shift all file-offset fields in load commands that were >= li_fileoff.
    _macho_shift_offsets(data, ncmds, li_fileoff, archive_span)
    # Move __LINKEDIT's virtual address by the same aligned span, preserving its
    # original vmaddr/fileoff relationship.
    struct.pack_into("<Q", data, li_cmd_off + 24, li_vmaddr + archive_span)

    # Write the new __FANG load command into the header gap.
    segname  = b"__FANG\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    sectname = b"__assets\x00\x00\x00\x00\x00\x00\x00\x00"

    seg_cmd = struct.pack("<II", _LC_SEGMENT_64, _FANG_LC_SIZE)
    seg_cmd += segname
    seg_cmd += struct.pack(
        "<QQQQIIII",
        li_vmaddr,     # vmaddr — where __LINKEDIT used to begin
        archive_span,  # vmsize — includes page padding before __LINKEDIT
        li_fileoff,    # fileoff — archive sits right before (old) __LINKEDIT
        archive_size,  # filesize
        1,             # maxprot  (VM_PROT_READ)
        1,             # initprot (VM_PROT_READ)
        1,             # nsects
        0,             # flags
    )
    sect = sectname + segname
    sect += struct.pack("<QQ", li_vmaddr, archive_size)
    sect += struct.pack("<IIIIIIII", li_fileoff, 0, 0, 0, 0, 0, 0, 0)

    new_lc = seg_cmd + sect  # 152 bytes
    data[load_cmds_end:load_cmds_end + _FANG_LC_SIZE] = new_lc
    struct.pack_into("<I", data, 16, ncmds + 1)
    struct.pack_into("<I", data, 20, sizeofcmds + _FANG_LC_SIZE)

    return bytes(data)


def _macho_find_linkedit(
    data: bytearray, ncmds: int
) -> tuple[int, int, int, int]:
    """Return (cmd_offset, fileoff, vmaddr, vmsize) for the __LINKEDIT segment."""
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
            if segname[:10] == b"__LINKEDIT":
                fileoff = struct.unpack_from("<Q", data, offset + 40)[0]
                vmaddr  = struct.unpack_from("<Q", data, offset + 24)[0]
                vmsize  = struct.unpack_from("<Q", data, offset + 32)[0]
                return offset, fileoff, vmaddr, vmsize
        offset += cmdsize
    raise BuildError("__LINKEDIT segment not found in Mach-O binary")


def _macho_shift_offsets(
    data: bytearray, ncmds: int, threshold: int, delta: int
) -> None:
    """Increment every load-command file-offset field that is >= threshold by delta."""

    def bump32(off: int) -> None:
        v = struct.unpack_from("<I", data, off)[0]
        if v >= threshold:
            struct.pack_into("<I", data, off, v + delta)

    def bump64(off: int) -> None:
        v = struct.unpack_from("<Q", data, off)[0]
        if v >= threshold:
            struct.pack_into("<Q", data, off, v + delta)

    offset = _MACH_HEADER_64_SIZE
    for _ in range(ncmds):
        if len(data) < offset + 8:
            break
        cmd     = struct.unpack_from("<I", data, offset)[0]
        cmdsize = struct.unpack_from("<I", data, offset + 4)[0]
        if cmdsize == 0:
            break

        if cmd == _LC_SEGMENT_64:
            bump64(offset + 40)                     # fileoff
        elif cmd == _LC_SYMTAB:
            bump32(offset + 8)                      # symoff
            bump32(offset + 16)                     # stroff
        elif cmd == _LC_DYSYMTAB:
            for off in [16, 24, 32, 40, 48, 52]:
                bump32(offset + off)
        elif cmd in (_LC_DYLD_INFO, _LC_DYLD_INFO_ONLY):
            for off in [8, 16, 24, 32, 40]:
                bump32(offset + off)
        elif cmd in (
            _LC_FUNCTION_STARTS,
            _LC_DATA_IN_CODE,
            _LC_CODE_SIGNATURE,
            _LC_DYLD_EXPORTS_TRIE,
            _LC_DYLD_CHAINED_FIXUPS,
        ):
            bump32(offset + 8)                      # dataoff

        offset += cmdsize


def _macho_segment_data_start(data: bytearray, ncmds: int) -> int:
    """Return the minimum non-zero fileoff across LC_SEGMENT_64 commands.

    This is the end of the header gap — the zero-padding between the load
    commands and the first segment's actual file data.
    """
    offset = _MACH_HEADER_64_SIZE
    min_fileoff = len(data)

    for _ in range(ncmds):
        if len(data) < offset + 8:
            break
        cmd     = struct.unpack_from("<I", data, offset)[0]
        cmdsize = struct.unpack_from("<I", data, offset + 4)[0]
        if cmdsize == 0:
            break
        if cmd == _LC_SEGMENT_64 and len(data) >= offset + 48:
            fileoff = struct.unpack_from("<Q", data, offset + 40)[0]
            if 0 < fileoff < min_fileoff:
                min_fileoff = fileoff
        offset += cmdsize

    return min_fileoff


def _macho_next_vmaddr(data: bytearray, ncmds: int) -> int:
    """Return the first vmaddr past all existing segments."""
    offset = _MACH_HEADER_64_SIZE
    max_end = 0

    for _ in range(ncmds):
        if len(data) < offset + 8:
            break
        cmd     = struct.unpack_from("<I", data, offset)[0]
        cmdsize = struct.unpack_from("<I", data, offset + 4)[0]
        if cmdsize == 0:
            break
        if cmd == _LC_SEGMENT_64 and len(data) >= offset + 40:
            vmaddr = struct.unpack_from("<Q", data, offset + 24)[0]
            vmsize = struct.unpack_from("<Q", data, offset + 32)[0]
            if vmaddr + vmsize > max_end:
                max_end = vmaddr + vmsize
        offset += cmdsize

    return max_end


# ── utilities ─────────────────────────────────────────────────────────────────────

def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f}KB"
    return f"{n / 1024 / 1024:.1f}MB"


def _iso_now() -> str:
    import datetime
    return datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"


class _NullContext:
    """Context manager that delegates to a BuildPanel without calling Live."""
    def __init__(self, panel: BuildPanel) -> None:
        self._panel = panel

    def __enter__(self) -> BuildPanel:
        return self._panel

    def __exit__(self, *args: object) -> None:
        pass
