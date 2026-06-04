from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, asdict
from typing import Any

SCHEMA_VERSION = 1


def is_fancy(ctx_obj: dict | None = None) -> bool:
    """True when rich TTY output is appropriate."""
    if os.environ.get("FANG_NO_FANCY") or os.environ.get("CI"):
        return False
    if ctx_obj and ctx_obj.get("no_fancy"):
        return False
    if ctx_obj and ctx_obj.get("output_json"):
        return False
    return sys.stderr.isatty()


# ── data model ─────────────────────────────────────────────────────────────────

@dataclass
class Diagnostic:
    id: str
    severity: str          # "info" | "warn" | "error"
    message: str
    detail: str | None = None

    def to_dict(self) -> dict:
        d: dict = {"id": self.id, "severity": self.severity, "message": self.message}
        if self.detail:
            d["detail"] = self.detail
        return d


@dataclass
class Timing:
    phase: str
    duration_ms: int

    def to_dict(self) -> dict:
        return {"phase": self.phase, "duration_ms": self.duration_ms}


@dataclass
class Artifact:
    kind: str
    path: str

    def to_dict(self) -> dict:
        return {"kind": self.kind, "path": self.path}


@dataclass
class CommandResult:
    command: str
    status: str            # "ok" | "warn" | "fail" | "error"
    payload: Any = None
    diagnostics: list[Diagnostic] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    timings: list[Timing] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        d: dict = {
            "schema_version": self.schema_version,
            "command": self.command,
            "status": self.status,
        }
        if self.diagnostics:
            d["diagnostics"] = [x.to_dict() for x in self.diagnostics]
        if self.artifacts:
            d["artifacts"] = [x.to_dict() for x in self.artifacts]
        if self.timings:
            d["timings"] = [x.to_dict() for x in self.timings]
        if self.payload is not None:
            d["payload"] = self.payload if isinstance(self.payload, dict) else asdict(self.payload)  # type: ignore[call-overload]
        return d


# ── progress events ────────────────────────────────────────────────────────────

@dataclass
class ProgressEvent:
    type: str
    phase: str | None = None
    current: int | None = None
    total: int | None = None
    message: str | None = None
    duration_ms: int | None = None
    kind: str | None = None
    path: str | None = None
    status: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


# ── serialization ───────────────────────────────────────────────────────────────

def render_json(result: CommandResult) -> str:
    return json.dumps(result.to_dict(), indent=2)


def render_ndjson(events: list[ProgressEvent], result: CommandResult) -> str:
    lines = [json.dumps(e.to_dict()) for e in events]
    lines.append(json.dumps(result.to_dict()))
    return "\n".join(lines)


# ── plain text (non-TTY / --no-fancy) ─────────────────────────────────────────

def render_plain(result: CommandResult) -> str:
    lines = [f"status: {result.status}"]
    for d in result.diagnostics:
        lines.append(f"{d.severity}: {d.message} ({d.id})")
    for a in result.artifacts:
        lines.append(f"artifact: {a.kind} {a.path}")
    for t in result.timings:
        lines.append(f"timing: {t.phase} {t.duration_ms}ms")
    return "\n".join(lines)


def fmt_ms(ms: int) -> str:
    if ms >= 1000:
        return f"{ms / 1000:.1f}s"
    return f"{ms}ms"
