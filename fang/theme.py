from rich.theme import Theme
from rich.style import Style

# ── palette ────────────────────────────────────────────────────────────────────
# Crimson brand (matches the existing Rust ANSI palette exactly)
CRIMSON = "#c41010"       # brand/logo — used for the wordmark
ACTIVE   = "#ff4646"      # active/running — spinner, progress bar fill
WHITE    = "#f5f5f5"      # primary content
MUTED    = "#aaaaaa"      # secondary / timestamps
SUCCESS  = "#50c878"      # check marks, done phases
WARNING  = "#f0b429"      # warnings
ERROR    = "#e03c31"      # errors / failures
PATH     = "#7ec8e3"      # file paths
VALUE    = "#c3a6ff"      # resolved values (python version, target, etc.)
BAR_BG   = "#371414"      # progress bar background

# ── Rich theme ─────────────────────────────────────────────────────────────────
FANG_THEME = Theme({
    "fang.brand":   Style(color=CRIMSON, bold=True),
    "fang.active":  Style(color=ACTIVE),
    "fang.content": Style(color=WHITE),
    "fang.muted":   Style(color=MUTED),
    "fang.success": Style(color=SUCCESS),
    "fang.warning": Style(color=WARNING, bold=True),
    "fang.error":   Style(color=ERROR, bold=True),
    "fang.path":    Style(color=PATH),
    "fang.value":   Style(color=VALUE),
})

from fang.wordmark import LINES as WORDMARK_LINES

WORDMARK_TEXT = "\n".join(WORDMARK_LINES)
