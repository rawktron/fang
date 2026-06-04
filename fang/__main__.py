from __future__ import annotations

import sys
from fang.output import is_fancy

_COMMANDS = [
    ("init",    "write a fang.toml from project discovery"),
    ("build",   "create a self-contained executable"),
    ("check",   "preflight project and environment"),
    ("inspect", "read metadata from a built executable"),
    ("version", "print the fang version"),
]


def _show_wordmark_fancy() -> None:
    from fang.wordmark import play
    play(commands=_COMMANDS)


def _show_wordmark_plain() -> None:
    from rich.console import Console
    from fang.theme import FANG_THEME, WORDMARK_TEXT
    console = Console(theme=FANG_THEME, file=sys.stderr, highlight=False)
    console.print(f"[fang.brand]{WORDMARK_TEXT}[/fang.brand]")


def show_bare_entry(ctx) -> None:
    output_json = ctx.obj.get("output_json", False) if ctx.obj else False

    if output_json:
        import json, click
        click.echo(json.dumps({"commands": [name for name, _ in _COMMANDS]}))
        return

    if not is_fancy(ctx.obj):
        sys.stdout.write(
            "fang - Single-binary Python app bundler\n\n"
            "Commands:\n"
        )
        for name, desc in _COMMANDS:
            sys.stdout.write(f"  {name:<9}{desc}\n")
        return

    _show_wordmark_fancy()
    # Commands are already rendered by the animation; nothing more to print.


def main() -> None:
    from fang.cli import cli
    cli()


if __name__ == "__main__":
    main()
