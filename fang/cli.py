import sys
import click
from fang import __version__
from fang.theme import FANG_THEME
from rich.console import Console


def make_console(ctx: click.Context) -> Console:
    no_fancy = ctx.obj.get("no_fancy", False)
    return Console(theme=FANG_THEME, highlight=False, markup=True,
                   no_color=no_fancy or not sys.stdout.isatty())


@click.group(invoke_without_command=True, context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--json", "output_json", is_flag=True, default=False,
              help="Emit machine-readable JSON on stdout.")
@click.option("--no-fancy", is_flag=True, default=False, envvar="FANG_NO_FANCY",
              help="Disable rich terminal output and animations.")
@click.option("--offline", is_flag=True, default=False,
              help="Skip all network requests.")
@click.option("--progress", is_flag=True, default=False,
              help="With --json, emit NDJSON progress events before the final result.")
@click.pass_context
def cli(ctx: click.Context, output_json: bool, no_fancy: bool, offline: bool, progress: bool) -> None:
    """fang — single-binary Python app bundler."""
    ctx.ensure_object(dict)
    ctx.obj["output_json"] = output_json
    ctx.obj["no_fancy"] = no_fancy
    ctx.obj["offline"] = offline
    ctx.obj["progress"] = progress

    if ctx.invoked_subcommand is None:
        # Bare `fang` — show identity screen
        from fang import _entry
        _entry.show_bare_entry(ctx)


@cli.command()
@click.option("--name", default=None, help="Override project name.")
@click.option("--python", default=None, help="Override Python version (e.g. 3.12).")
@click.option("--entry", default=None, help="Override entry point (e.g. myapp.__main__).")
@click.option("--force", is_flag=True, default=False, help="Overwrite existing fang.toml.")
@click.pass_context
def init(ctx: click.Context, name: str | None, python: str | None,
         entry: str | None, force: bool) -> None:
    """Discover project config and write a minimal fang.toml."""
    from fang import init as _init
    _init.run(ctx, name=name, python=python, entry=entry, force=force)


@cli.command()
@click.option("--online", is_flag=True, default=False,
              help="Also check remote artifact availability.")
@click.pass_context
def check(ctx: click.Context, online: bool) -> None:
    """Preflight bundle readiness without building."""
    from fang import check as _check
    _check.run(ctx, online=online)


@cli.command()
@click.argument("output", default=None, required=False, metavar="[OUTPUT]")
@click.option("--target", default=None, metavar="PLATFORM",
              help="Target platform (e.g. linux-x86_64, macos-arm64).")
@click.option("--python", default=None, help="Override Python version.")
@click.option("--entry", default=None, help="Override entry point.")
@click.option("--name", default=None, help="Override project name.")
@click.pass_context
def build(ctx: click.Context, output: str | None, target: str | None,
          python: str | None, entry: str | None, name: str | None) -> None:
    """Create a self-contained executable from a Python project."""
    from fang import build as _build
    _build.run(ctx, output=output, target=target, python=python,
               entry=entry, name=name)


@cli.command()
@click.argument("artifact", type=click.Path(exists=True))
@click.option("--list-assets", is_flag=True, default=False, help="List embedded archive entries.")
@click.option("--verify", is_flag=True, default=False, help="Verify asset hashes.")
@click.option("--show-deps", is_flag=True, default=False,
              help="Show embedded distribution metadata.")
@click.option("--raw-archive", is_flag=True, default=False, hidden=True)
@click.pass_context
def inspect(ctx: click.Context, artifact: str, list_assets: bool, verify: bool,
            show_deps: bool, raw_archive: bool) -> None:
    """Read metadata from a built Fang executable."""
    from fang import inspector as _inspect
    _inspect.run(ctx, artifact=artifact, list_assets=list_assets,
                 verify=verify, show_deps=show_deps, raw_archive=raw_archive)


@cli.command()
@click.pass_context
def version(ctx: click.Context) -> None:
    """Print the fang version."""
    obj = ctx.obj or {}
    if obj.get("output_json"):
        import json
        click.echo(json.dumps({"version": __version__}))
    else:
        click.echo(f"fang {__version__}")
