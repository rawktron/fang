"""fang inspect command."""
from __future__ import annotations

import json
from pathlib import Path

import click

from fang.archive import Archive, ArchiveError, archive_bytes_from_executable
from fang.cli import make_console
from fang.output import CommandResult, Diagnostic


def run(
    ctx: click.Context,
    *,
    artifact: str,
    list_assets: bool,
    verify: bool,
    show_deps: bool,
    raw_archive: bool,
) -> None:
    console = make_console(ctx)
    obj = ctx.obj or {}
    output_json = obj.get("output_json", False)

    artifact_path = Path(artifact)
    diagnostics: list[Diagnostic] = []
    payload: dict = {}

    # Extract archive bytes
    try:
        archive_bytes = archive_bytes_from_executable(artifact_path)
    except ArchiveError as e:
        diagnostics.append(Diagnostic("INS-001", "error", f"could not extract archive: {e}"))
        result = CommandResult(command="inspect", status="error", diagnostics=diagnostics)
        if output_json:
            click.echo(json.dumps(result.to_dict()))
        else:
            console.print(f"[fang.error]error:[/] {e}")
        ctx.exit(1)
        return

    if raw_archive:
        import sys
        sys.stdout.buffer.write(archive_bytes)
        return

    # Parse archive
    try:
        archive = Archive(archive_bytes)
    except ArchiveError as e:
        diagnostics.append(Diagnostic("INS-002", "error", f"invalid archive: {e}"))
        result = CommandResult(command="inspect", status="error", diagnostics=diagnostics)
        if output_json:
            click.echo(json.dumps(result.to_dict()))
        else:
            console.print(f"[fang.error]error:[/] {e}")
        ctx.exit(1)
        return

    # Read meta
    try:
        meta = archive.meta()
        payload["meta"] = meta.to_dict()
    except ArchiveError as e:
        diagnostics.append(Diagnostic("INS-003", "warn", f"could not read meta: {e}"))
        meta = None

    entries = archive.entries()
    payload["entry_count"] = len(entries)
    payload["archive_size"] = len(archive_bytes)

    # Category summary
    categories: dict[str, int] = {}
    for e in entries:
        categories[e.category] = categories.get(e.category, 0) + 1
    payload["categories"] = categories

    # --list-assets
    if list_assets:
        payload["assets"] = [
            {
                "path": e.path,
                "category": e.category,
                "compressed_size": e.compressed_size,
                "uncompressed_size": e.uncompressed_size,
                "content_hash": e.content_hash,
            }
            for e in entries
        ]

    # --verify
    if verify:
        failed = archive.verify_all()
        payload["verify_failed"] = failed
        if failed:
            diagnostics.append(Diagnostic(
                "INS-004", "error",
                f"{len(failed)} entries failed hash verification",
                detail="\n".join(failed[:10]),
            ))
        else:
            diagnostics.append(Diagnostic("INS-004", "info", "all hashes verified"))

    # --show-deps
    if show_deps and meta:
        payload["python_version"] = meta.python_version
        payload["entry_point"] = meta.entry_point
        payload["platform"] = meta.platform
        if meta.extensions:
            payload["extensions"] = meta.extensions

    overall = "fail" if any(d.severity == "error" for d in diagnostics) else "ok"
    result = CommandResult(
        command="inspect",
        status=overall,
        payload=payload,
        diagnostics=diagnostics,
    )

    if output_json:
        click.echo(json.dumps(result.to_dict()))
    else:
        from rich.table import Table
        from rich.text import Text

        # Metadata table
        if meta:
            cat_summary = "  ".join(f"{n} {cat}" for cat, n in sorted(categories.items()))
            t = Table.grid(padding=(0, 2))
            t.add_column(style="fang.muted", width=10)
            t.add_column(style="fang.value")
            t.add_row("project", meta.project_name or "—")
            t.add_row("entry", meta.entry_point or "—")
            t.add_row("python", meta.python_version or "—")
            t.add_row("platform", meta.platform or "—")
            t.add_row("built", meta.build_timestamp or "—")
            t.add_row("archive", f"{_fmt_bytes(len(archive_bytes))}  ({len(entries)} entries: {cat_summary})")
            console.print(t)
        else:
            console.print(f"  [fang.path]{artifact_path}[/]  "
                          f"[fang.muted]{len(entries)} entries  {_fmt_bytes(len(archive_bytes))}[/]")

        if list_assets:
            console.print()
            at = Table.grid(padding=(0, 2))
            at.add_column(style="fang.path")
            at.add_column(style="fang.muted")
            for e in entries:
                at.add_row(e.path,
                           f"{_fmt_bytes(e.uncompressed_size)} → {_fmt_bytes(e.compressed_size)}")
            console.print(at)

        if verify:
            console.print()
            failed = payload.get("verify_failed", [])
            if failed:
                console.print(f"  [fang.error]✗[/]  {len(failed)} verification failure{'s' if len(failed) != 1 else ''}")
                for p in failed:
                    console.print(f"     [fang.error]{p}[/]")
            else:
                console.print("  [fang.success]✓[/]  all hashes verified")

        for d in diagnostics:
            if d.severity in ("error", "warn"):
                style = "fang.error" if d.severity == "error" else "fang.warning"
                icon = "✗" if d.severity == "error" else "!"
                console.print(f"  [{style}]{icon}[/]  {d.message}")

    if overall == "fail":
        ctx.exit(1)


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f}KB"
    return f"{n / 1024 / 1024:.1f}MB"
