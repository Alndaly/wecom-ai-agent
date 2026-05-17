"""Colorized terminal logging for the FastAPI backend.

Replaces the default single-color stream so operators can scan a busy log:
- Per-level color on the level label (INFO cyan / WARN yellow / ERROR red / …)
- Per-namespace color on the logger name (loggers under the same top module
  share a hue, so `app.ai.react_agent` and `app.ai.react_playbooks` are easy
  to group at a glance)
- Inline highlight for the noisy tokens that show up in ReAct/device logs:
  `ok=True/False`, `source=cache/llm/rule`, `elapsed=...ms`. These are the
  ones we routinely scan for when debugging.

No third-party deps — raw ANSI escapes only. Honors `NO_COLOR` (off) and
`FORCE_COLOR` (on) per the de facto env-var conventions.
"""
from __future__ import annotations

import logging
import os
import re
import sys


class _C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GRAY = "\033[90m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN = "\033[96m"


_LEVEL_STYLES = {
    logging.DEBUG: _C.GRAY,
    logging.INFO: _C.BRIGHT_CYAN,
    logging.WARNING: _C.BRIGHT_YELLOW,
    logging.ERROR: _C.BRIGHT_RED,
    logging.CRITICAL: _C.BOLD + _C.BRIGHT_RED,
}

_LEVEL_LABEL = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO ",
    logging.WARNING: "WARN ",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "CRIT ",
}

# Loggers that share a top namespace share a hue — picks a colour by hashing
# the namespace head, then caches the result per full logger name.
_NAME_PALETTE = (
    _C.MAGENTA,
    _C.BLUE,
    _C.GREEN,
    _C.BRIGHT_MAGENTA,
    _C.BRIGHT_BLUE,
    _C.BRIGHT_GREEN,
    _C.YELLOW,
    _C.CYAN,
)
_NAME_COLOR_CACHE: dict[str, str] = {}


def _logger_color(name: str) -> str:
    if name in _NAME_COLOR_CACHE:
        return _NAME_COLOR_CACHE[name]
    parts = name.split(".")
    bucket = ".".join(parts[:2]) if len(parts) >= 2 else parts[0]
    idx = abs(hash(bucket)) % len(_NAME_PALETTE)
    color = _NAME_PALETTE[idx]
    _NAME_COLOR_CACHE[name] = color
    return color


# Inline-highlight patterns. Order matters when patterns might overlap — the
# more specific one comes first.
_INLINE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bok=True\b"), _C.BRIGHT_GREEN),
    (re.compile(r"\bok=False\b"), _C.BRIGHT_RED),
    (re.compile(r"\bsource=cache\b"), _C.BRIGHT_GREEN),
    (re.compile(r"\bsource=rule\b"), _C.BRIGHT_CYAN),
    (re.compile(r"\bsource=llm\b"), _C.BRIGHT_MAGENTA),
    (re.compile(r"\bstatus=(completed|sent)\b"), _C.BRIGHT_GREEN),
    (re.compile(r"\bstatus=(failed|cancelled|error)\b"), _C.BRIGHT_RED),
    (re.compile(r"\bstatus=(pending|dispatched|running)\b"), _C.BRIGHT_YELLOW),
    # Slow ops (>1s elapsed) stand out so it's easy to spot stalls.
    (re.compile(r"\belapsed=(\d{4,})ms\b"), _C.BRIGHT_YELLOW),
    (re.compile(r"\[react\]"), _C.BRIGHT_BLUE),
)


def _colorize_message(msg: str, base_color: str) -> str:
    """Apply inline token highlights. After each match, restore `base_color`
    so the surrounding text keeps its level/baseline hue instead of falling
    back to the terminal default."""
    out = msg
    for pat, col in _INLINE_RULES:
        out = pat.sub(lambda m, c=col, b=base_color: f"{c}{m.group(0)}{_C.RESET}{b}", out)
    return out


class ColorFormatter(logging.Formatter):
    def __init__(self, *, use_color: bool) -> None:
        super().__init__()
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%H:%M:%S")
        level_label = _LEVEL_LABEL.get(record.levelno, record.levelname.ljust(5))
        name = record.name
        msg = record.getMessage()
        if not self.use_color:
            base = f"{ts} {level_label} {name} | {msg}"
            if record.exc_info:
                base += "\n" + self.formatException(record.exc_info)
            return base
        level_color = _LEVEL_STYLES.get(record.levelno, "")
        name_color = _logger_color(name)
        body = _colorize_message(msg, base_color="")
        line = (
            f"{_C.GRAY}{ts}{_C.RESET} "
            f"{level_color}{level_label}{_C.RESET} "
            f"{name_color}{name}{_C.RESET} "
            f"{_C.GRAY}|{_C.RESET} {body}"
        )
        if record.exc_info:
            # Stack traces stay readable in dim grey so they don't overpower
            # the structured header lines around them.
            line += "\n" + _C.GRAY + self.formatException(record.exc_info) + _C.RESET
        return line


def _should_use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


def setup_logging(level: str | int = "INFO") -> None:
    """Install the colored formatter on the root logger and re-route uvicorn /
    httpx loggers through it. Safe to call multiple times — clears existing
    handlers each time to avoid duplicate output if uvicorn or basicConfig
    already attached one."""
    use_color = _should_use_color()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ColorFormatter(use_color=use_color))
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    root.setLevel(level)
    # uvicorn/httpx attach their own handlers — strip them so messages flow
    # up to our root and get the colour treatment uniformly.
    for noisy in ("uvicorn", "uvicorn.access", "uvicorn.error", "httpx", "httpcore"):
        lg = logging.getLogger(noisy)
        for h in list(lg.handlers):
            lg.removeHandler(h)
        lg.propagate = True
