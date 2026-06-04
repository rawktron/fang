"""fang check command — project + environment preflight."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import click

from fang.cli import make_console
from fang.config import RawConfig, ConfigError, resolve as resolve_config
from fang.discovery import DiscoveredProject, DiscoveryError
from fang.output import CommandResult, Diagnostic
from fang.runtime import _detect_host_platform


def run(ctx: click.Context, *, online: bool) -> None:
    console = make_console(ctx)
    obj = ctx.obj or {}
    output_json = obj.get("output_json", False)
    offline = obj.get("offline", False)
    project_dir = Path.cwd()
    host = _detect_host_platform()

    diagnostics: list[Diagnostic] = []
    project_rows: list[tuple[str, str, str]] = []   # (status, label, detail)
    env_rows:     list[tuple[str, str, str]] = []
    overall = "ok"

    # ── project checks ────────────────────────────────────────────────────────────

    has_pyproject = (project_dir / "pyproject.toml").exists()
    has_fang_toml = (project_dir / "fang.toml").exists()

    if not has_pyproject and not has_fang_toml:
        diagnostics.append(Diagnostic("CHK-001", "error",
            "no pyproject.toml or fang.toml found", "Run `fang init`."))
        project_rows.append(("fail", "config", "no config found — run fang init"))
        overall = "fail"

    discovered: DiscoveredProject | None = None
    if overall != "fail":
        try:
            discovered = DiscoveredProject.from_directory(project_dir)
        except DiscoveryError as e:
            diagnostics.append(Diagnostic("CHK-002", "error", f"discovery failed: {e}"))
            project_rows.append(("fail", "project", f"discovery failed: {e}"))
            overall = "fail"

    if discovered is not None:
        entry = discovered.resolve_entry()
        if entry is None and not discovered.entrypoints:
            diagnostics.append(Diagnostic("CHK-003", "error", "no entrypoint found",
                "Add [project.scripts] to pyproject.toml or a __main__.py package."))
            project_rows.append(("fail", "entrypoint", "none found — add [project.scripts] or __main__.py"))
            overall = "fail"
        elif entry is None:
            names = ", ".join(e.module for e in discovered.entrypoints)
            diagnostics.append(Diagnostic("CHK-003", "warn",
                "multiple entrypoints; set project.entry in fang.toml", names))
            project_rows.append(("warn", "entrypoint",
                f"ambiguous ({names}) — set project.entry in fang.toml"))
            if overall == "ok":
                overall = "warn"
        else:
            project_rows.append(("ok", "entrypoint", entry))

    cfg = None
    if discovered is not None and overall != "fail":
        raw = RawConfig.from_file(project_dir / "fang.toml")
        try:
            cfg = resolve_config(raw, host_platform=host, discovered=discovered)
        except ConfigError as e:
            diagnostics.append(Diagnostic("CHK-004", "error", f"config error: {e}"))
            project_rows.append(("fail", "config", str(e)))
            overall = "fail"

    if cfg is not None:
        from fang.discovery import SUPPORTED_SERIES
        series = ".".join(cfg.project.python.split(".")[:2])
        if series not in SUPPORTED_SERIES:
            diagnostics.append(Diagnostic("CHK-005", "warn",
                f"Python {series} is not in the tested series {SUPPORTED_SERIES}"))
            project_rows.append(("warn", "python", f"{cfg.project.python}  (not in tested series)"))
            if overall == "ok":
                overall = "warn"
        else:
            project_rows.append(("ok", "python", cfg.project.python))

    # ── environment checks ────────────────────────────────────────────────────────

    # uv
    uv_path = shutil.which("uv")
    if uv_path:
        diagnostics.append(Diagnostic("ENV-001", "info", f"uv found at {uv_path}"))
        env_rows.append(("ok", "uv", uv_path))
    else:
        diagnostics.append(Diagnostic("ENV-001", "warn", "uv not found on PATH",
            "uv is used for dependency install; get it from astral.sh/uv"))
        env_rows.append(("warn", "uv", "not found — install from astral.sh/uv"))
        if overall == "ok":
            overall = "warn"

    # runtime
    cache_dir = Path(os.environ.get("FANG_CACHE_DIR", Path.home() / ".fang"))
    _check_runtime(host, cache_dir, env_rows, diagnostics)
    if any(r[0] == "fail" for r in env_rows):
        overall = "fail"
    elif any(r[0] == "warn" for r in env_rows if r[1] == "runtime") and overall == "ok":
        overall = "warn"

    # cache dir
    cache_exists   = cache_dir.exists()
    cache_writable = not cache_exists or os.access(cache_dir, os.W_OK)
    if not cache_writable:
        diagnostics.append(Diagnostic("ENV-003", "error", f"cache not writable: {cache_dir}"))
        env_rows.append(("fail", "cache", f"{cache_dir}  (not writable)"))
        overall = "fail"
    else:
        diagnostics.append(Diagnostic("ENV-003", "info", f"cache: {cache_dir}"))
        label = str(cache_dir) + ("" if cache_exists else "  (will be created)")
        env_rows.append(("ok" if cache_exists else "info", "cache", label))

    # update check
    if not offline and not os.environ.get("FANG_NO_UPDATE_CHECK"):
        try:
            _check_update(env_rows, diagnostics)
        except Exception:
            pass

    # ── final payload ─────────────────────────────────────────────────────────────

    all_rows = project_rows + env_rows
    errors  = [d for d in diagnostics if d.severity == "error"]
    warns   = [d for d in diagnostics if d.severity == "warn"]
    payload = {"errors": len(errors), "warnings": len(warns)}
    result  = CommandResult(command="check", status=overall,
                            payload=payload, diagnostics=diagnostics)

    if output_json:
        click.echo(json.dumps(result.to_dict()))
    else:
        from rich.table import Table
        from rich.text import Text

        _ICONS = {
            "ok":   ("✓", "fang.success"),
            "warn": ("!", "fang.warning"),
            "fail": ("✗", "fang.error"),
            "info": ("·", "fang.muted"),
        }

        def _table(rows: list[tuple[str, str, str]]) -> Table:
            t = Table.grid(padding=(0, 2))
            t.add_column(width=2)
            t.add_column(width=12, style="fang.muted")
            t.add_column()
            for status, label, detail in rows:
                icon_ch, icon_style = _ICONS.get(status, ("·", "fang.muted"))
                val_style = ("fang.warning" if status == "warn" else
                             "fang.error"   if status == "fail" else
                             "fang.muted"   if status == "info" else "fang.content")
                t.add_row(Text(icon_ch, style=icon_style), label, Text(detail, style=val_style))
            return t

        console.print(_table(project_rows))
        console.print()
        console.print(_table(env_rows))
        console.print()

        n_e = sum(1 for r in all_rows if r[0] == "fail")
        n_w = sum(1 for r in all_rows if r[0] == "warn")
        if n_e:
            console.print(
                f"  [fang.error]{n_e} error{'s' if n_e != 1 else ''}[/]" +
                (f"  [fang.warning]{n_w} warning{'s' if n_w != 1 else ''}[/]" if n_w else ""))
        elif n_w:
            console.print(f"  [fang.warning]{n_w} warning{'s' if n_w != 1 else ''}[/]")
        else:
            console.print("  [fang.success]all checks passed[/]")

    if overall == "fail":
        ctx.exit(1)


def _check_runtime(
    host: str,
    cache_dir: Path,
    env_rows: list[tuple[str, str, str]],
    diagnostics: list[Diagnostic],
) -> None:
    from fang.runtime import SUPPORTED_RUNTIME_SERIES, _python_series, _cache_dir_for

    env_path = os.environ.get("FANG_RUNTIME_PATH")
    if env_path:
        if Path(env_path).exists():
            diagnostics.append(Diagnostic("ENV-002", "info", f"runtime: FANG_RUNTIME_PATH={env_path}"))
            env_rows.append(("ok", "runtime", f"FANG_RUNTIME_PATH={env_path}"))
        else:
            diagnostics.append(Diagnostic("ENV-002", "warn",
                f"FANG_RUNTIME_PATH={env_path!r} does not exist"))
            env_rows.append(("warn", "runtime", f"FANG_RUNTIME_PATH={env_path!r}  (not found)"))
        return

    py_series = _python_series("3.12")  # default series for doctor check
    for series in SUPPORTED_RUNTIME_SERIES:
        cached = _cache_dir_for(cache_dir, series, py_series, host) / "fang-runtime"
        if cached.exists():
            diagnostics.append(Diagnostic("ENV-002", "info", "runtime: cached"))
            env_rows.append(("ok", "runtime", f"cached (series {series})"))
            return

    diagnostics.append(Diagnostic("ENV-002", "info", "runtime: not yet downloaded"))
    env_rows.append(("ok", "runtime", "will be fetched from GitHub on first build"))


def _check_update(
    env_rows: list[tuple[str, str, str]],
    diagnostics: list[Diagnostic],
) -> None:
    import httpx
    from fang import __version__

    from fang.runtime import GITHUB_REPO
    resp = httpx.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
        headers={"User-Agent": "fang", "Accept": "application/vnd.github+json"},
        timeout=5, follow_redirects=True,
    )
    if resp.status_code != 200:
        return
    latest = resp.json().get("tag_name", "").lstrip("v")
    if not latest:
        return
    if latest != __version__:
        diagnostics.append(Diagnostic("ENV-004", "warn",
            f"fang {latest} available (you have {__version__})"))
        env_rows.append(("warn", "fang", f"{__version__}  ({latest} available)"))
    else:
        diagnostics.append(Diagnostic("ENV-004", "info", f"fang {__version__} is up to date"))
        env_rows.append(("ok", "fang", f"{__version__}  (up to date)"))
