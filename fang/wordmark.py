"""Bloody-font wordmark + commands pour animation — pure Python, no runtime deps.

Effect: each non-space character falls independently from the top of the canvas
to its final position (TTE rain/pour style).  Columns pour left-to-right;
within each column, characters release in top-to-bottom order.

Font: "Bloody" figlet font — pre-rendered FANG.
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from collections import defaultdict

# ── ANSI primitives ───────────────────────────────────────────────────────────

def _rgb(r: int, g: int, b: int) -> str:
    return f"\033[38;2;{r};{g};{b}m"

_RST  = "\033[0m"
_HIDE = "\033[?25l"
_SHOW = "\033[?25h"

# ── Blood palette ─────────────────────────────────────────────────────────────
_FLASH  = _rgb(255, 220, 200)  # arrival — warm white-orange flash
_HOT    = _rgb(255,  70,   0)  # mid-fall — intense orange
_WARM   = _rgb(245,  30,   5)  # just settled — vivid red-orange

# Settled wordmark: hue shifts from orange-red (░ drip tips, freshest) through
# red (▒▓) to deep crimson (█ solid body, dried).  This gives the font's
# built-in ░▒▓█ gradient actual colour range, not just brightness.
_S_SOLID = _rgb(170,   0,   0)  # █ ▄ ▀ ▌ ▐ — dried blood, deep red
_S_DARK  = _rgb(205,  18,   5)  # ▓ — rich red
_S_MID   = _rgb(238,  55,  12)  # ▒ — red-orange
_S_LIGHT = _rgb(255, 100,  30)  # ░ — drip tips — warm orange (freshest)

_CNAME  = _rgb(255, 208,  75)  # command name — gold accent (contrast against blood reds)
_CDESC  = _rgb(155,  82,  70)  # command description — warm brownish-red

# ── Bloody figlet font — FANG pre-rendered (36 wide × 9 tall) ─────────────────
# Generated with: pyfiglet.Figlet(font='Bloody').renderText('FANG')
# Trailing all-space row stripped.
LINES: list[str] = [
    '  █████▒▄▄▄       ███▄    █   ▄████ ',
    '▓██   ▒▒████▄     ██ ▀█   █  ██▒ ▀█▒',
    '▒████ ░▒██  ▀█▄  ▓██  ▀█ ██▒▒██░▄▄▄░',
    '░▓█▒  ░░██▄▄▄▄██ ▓██▒  ▐▌██▒░▓█  ██▓',
    '░▒█░    ▓█   ▓██▒▒██░   ▓██░░▒▓███▀▒',
    ' ▒ ░    ▒▒   ▓▒█░░ ▒░   ▒ ▒  ░▒   ▒ ',
    ' ░       ▒   ▒▒ ░░ ░░   ░ ▒░  ░   ░ ',
    ' ░ ░     ░   ▒      ░   ░ ░ ░ ░   ░ ',
    '             ░  ░         ░       ░ ',
]

_WM_H = len(LINES)                          # 9
_WM_W = max(len(ln) for ln in LINES)        # 36

# ── Timing constants ──────────────────────────────────────────────────────────
_COL_STAGGER  = 0.12   # frame delay between adjacent columns (left→right wave)
_ROW_STAGGER  = 0.25   # frame delay between successive chars in the same column
_SPEED        = 1.00   # canvas rows each char falls per frame
_N_FRAMES     = 25     # 25 × 34 ms ≈ 850 ms total; extra frames let last chars dry
_FRAME_MS     = 34
_SETTLE_FAST  = 1.0    # frames after arrival → fully settled color

# Column index where command name ends and description begins
_CMD_BREAK = 11


# ── Per-character settled colour ──────────────────────────────────────────────

def _settled_esc(ch: str, is_cmd: bool, is_name: bool) -> str:
    """Return the ANSI escape for a fully-settled character."""
    if not is_cmd:
        # Bloody-font wordmark: preserve the ░▒▓█ gradient built into the font.
        # ░ drip-tips are the freshest (brightest red); solid █ body is dried.
        if ch == '░':
            return _S_LIGHT
        if ch == '▒':
            return _S_MID
        if ch == '▓':
            return _S_DARK
        return _S_SOLID          # █ ▄ ▀ ▌ ▐ and anything else
    return _CNAME if is_name else _CDESC


# ── Canvas builder ────────────────────────────────────────────────────────────

def _build(cmd_lines: list[str]):
    """Return (col_chars, canvas_h, canvas_w).

    col_chars: column → sorted list of (final_row, char, settled_esc_str).
    Pre-computing the settled escape avoids per-cell branching in the hot loop.
    """
    col: dict[int, list[tuple[int, str, str]]] = defaultdict(list)

    for r, line in enumerate(LINES):
        for c, ch in enumerate(line):
            if ch != ' ':
                col[c].append((r, ch, _settled_esc(ch, False, False)))

    cmd_start = _WM_H + 1          # one blank separator row
    for li, line in enumerate(cmd_lines):
        row = cmd_start + li
        for c, ch in enumerate(line):
            if ch != ' ':
                col[c].append((row, ch, _settled_esc(ch, True, c < _CMD_BREAK)))

    for c in col:
        col[c].sort()             # ascending by final_row → top chars fall first

    canvas_h = cmd_start + len(cmd_lines)
    canvas_w = max(_WM_W, *(len(ln) for ln in cmd_lines), 0)
    return dict(col), canvas_h, canvas_w


# ── Per-frame renderer ────────────────────────────────────────────────────────

def _render(f: int, col_chars: dict, canvas_h: int, canvas_w: int) -> str:
    buf: list[str] = []

    for row in range(canvas_h):
        buf.append('\r')
        for c in range(canvas_w):
            chars = col_chars.get(c)
            if not chars:
                buf.append(' ')
                continue

            falling_ch: str | None = None
            falling_esc = ''
            settled_ch: str | None = None
            settled_esc_str = ''

            for idx, (final_row, ch, s_esc) in enumerate(chars):
                elapsed = f - c * _COL_STAGGER - idx * _ROW_STAGGER
                if elapsed <= 0.0:
                    continue
                pos = elapsed * _SPEED

                if pos < final_row:
                    # Still falling — show at current row with fall colour
                    if int(pos) == row:
                        progress = pos / final_row if final_row > 0 else 1.0
                        falling_ch = ch
                        falling_esc = _FLASH if progress > 0.78 else _HOT
                else:
                    # Settled — colour transitions flash → warm → final
                    if final_row == row:
                        settle_t = pos - final_row
                        settled_ch = ch
                        if settle_t < 0.35:
                            settled_esc_str = _FLASH
                        elif settle_t < _SETTLE_FAST:
                            settled_esc_str = _WARM
                        else:
                            settled_esc_str = s_esc   # per-character final colour
                        break  # only one settled char per (row, col)

            # Falling char takes visual priority — passes through settled chars
            if falling_ch is not None:
                buf.append(falling_esc + falling_ch)
            elif settled_ch is not None:
                buf.append(settled_esc_str + settled_ch)
            else:
                buf.append(' ')

        buf.append(_RST + '\n')

    return ''.join(buf)


# ── Public API ────────────────────────────────────────────────────────────────

def play(
    commands: list[tuple[str, str]] | None = None,
    file=None,
) -> None:
    """Rain/pour the FANG wordmark then the commands list into place.

    *commands* is an optional list of (name, description) pairs animated
    by the same left-to-right rain sweep that paints the wordmark.

    Silently no-ops when output is not a TTY, NO_COLOR is set, or the
    terminal is too narrow.
    """
    out = file if file is not None else sys.stderr
    if not out.isatty():
        return
    if os.environ.get('NO_COLOR'):
        return
    if shutil.get_terminal_size((0, 0)).columns < _WM_W + 2:
        return

    cmd_lines: list[str] = []
    if commands:
        for name, desc in commands:
            cmd_lines.append(f'  {name:<9}{desc}')

    col_chars, canvas_h, canvas_w = _build(cmd_lines)

    # Reserve canvas_h+1 blank lines; reposition to first canvas row.
    out.write(_HIDE)
    out.write('\n' * (canvas_h + 1))
    out.write(f'\033[{canvas_h}A\r')
    out.flush()

    try:
        for f in range(_N_FRAMES):
            t0 = time.perf_counter()
            out.write(_render(f, col_chars, canvas_h, canvas_w))
            out.flush()
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            gap = _FRAME_MS - elapsed_ms
            if gap > 0.5:
                time.sleep(gap / 1000.0)
            if f < _N_FRAMES - 1:
                out.write(f'\033[{canvas_h}A\r')
    finally:
        out.write(_SHOW)
        out.flush()
