"""fang init command."""
from __future__ import annotations

import json
from pathlib import Path

import click

from fang.cli import make_console
from fang.config import RawConfig, ConfigError, generate_fang_toml, resolve as resolve_config
from fang.discovery import DiscoveredProject
from fang.output import CommandResult, Diagnostic, is_fancy
from fang.runtime import _detect_host_platform


def run(
    ctx: click.Context,
    *,
    name: str | None,
    python: str | None,
    entry: str | None,
    force: bool,
) -> None:
    console = make_console(ctx)
    obj = ctx.obj or {}
    output_json = obj.get("output_json", False)
    project_dir = Path.cwd()
    fang_toml = project_dir / "fang.toml"

    diagnostics: list[Diagnostic] = []

    if fang_toml.exists() and not force:
        diag = Diagnostic(
            id="init.already-exists",
            severity="error",
            message="fang.toml already exists",
            detail="Use --force to overwrite.",
        )
        result = CommandResult(command="init", status="error", diagnostics=[diag])
        if output_json:
            click.echo(json.dumps(result.to_dict()))
        else:
            console.print(f"[fang.error]error:[/] fang.toml already exists  [fang.muted](use --force to overwrite)[/]")
        ctx.exit(1)
        return

    try:
        discovered = DiscoveredProject.from_directory(project_dir)
    except Exception as e:
        diagnostics.append(Diagnostic("init.discovery-failed", "error", str(e)))
        _emit_error(ctx, console, output_json, "discovery failed", diagnostics)
        return

    host = _detect_host_platform()
    try:
        cfg = resolve_config(
            None,
            cli_name=name,
            cli_python=python,
            cli_entry=entry,
            host_platform=host,
            discovered=discovered,
        )
    except ConfigError as e:
        diagnostics.append(Diagnostic("init.config-error", "error", str(e)))
        _emit_error(ctx, console, output_json, str(e), diagnostics)
        return

    content = generate_fang_toml(cfg, discovered)
    fang_toml.write_text(content)

    payload = {
        "path": str(fang_toml),
        "name": cfg.project.name,
        "entry": cfg.project.entry,
        "python": cfg.project.python,
    }
    result = CommandResult(command="init", status="ok", payload=payload, diagnostics=diagnostics)

    if output_json:
        click.echo(json.dumps(result.to_dict()))
    else:
        from rich.table import Table
        from rich.text import Text

        t = Table.grid(padding=(0, 2))
        t.add_column(style="fang.muted", width=8)
        t.add_column(style="fang.value")
        t.add_row("name", cfg.project.name)
        t.add_row("entry", cfg.project.entry)
        t.add_row("python", cfg.project.python)
        console.print(t)
        console.print()
        console.print(f"  [fang.success]✓[/]  wrote [fang.path]{fang_toml}[/]")


def _emit_error(
    ctx: click.Context,
    console: "Console",
    output_json: bool,
    message: str,
    diagnostics: list[Diagnostic],
) -> None:
    result = CommandResult(command="init", status="error", diagnostics=diagnostics)
    if output_json:
        click.echo(json.dumps(result.to_dict()))
    else:
        console.print(f"[fang.error]✗[/]  {message}")
    ctx.exit(1)
