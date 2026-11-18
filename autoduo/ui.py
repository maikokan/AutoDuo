"""Terminal presentation for autoduo's command line.

Nothing happens at import time: capability checks run on every call, so
a pipe, a redirect or NO_COLOR takes effect immediately.

Colour rules, in order:

  1. NO_COLOR set (any value)            -> never colour.
  2. FORCE_COLOR set to anything but "0" -> colour, even in a pipe.
  3. TERM=dumb                           -> no colour.
  4. otherwise                           -> colour only on a TTY.

The palette is deliberately quiet: one warm clay accent for the thing
that matters, soft green for success, muted amber for a warning and
muted red for an error. Everything else is the terminal's own ink, with
dim for metadata and bold for emphasis. Prose wraps at 68 columns, so
the result fits an 80-column terminal with room to spare.
"""
from __future__ import annotations

import os
import sys
import textwrap
from typing import TextIO

MAX_WIDTH = 68          # the longest line drawn, indentation included
RULE_WIDTH = 60
LABEL_COLUMN = 9        # "  ok     " and "  error  " line up

_BOLD = "1"
_DIM = "2"

# name: (256-colour foreground, nearest basic colour)
_COLOURS = {
    "accent": ("38;5;173", "33"),   # warm clay
    "success": ("38;5;108", "32"),  # soft green
    "warning": ("38;5;179", "33"),  # muted amber
    "danger": ("38;5;174", "31"),   # muted red
}


def color_enabled(stream: TextIO | None = None, env=None) -> bool:
    """Whether ANSI colour belongs on ``stream``."""
    env = os.environ if env is None else env
    if env.get("NO_COLOR"):
        return False
    if env.get("FORCE_COLOR", "") not in ("", "0"):
        return True
    if env.get("TERM") == "dumb":
        return False
    stream = sys.stdout if stream is None else stream
    try:
        return bool(stream.isatty())
    except Exception:  # a closed or exotic stream cannot answer
        return False


def _colour_code(name: str, env=None) -> str:
    env = os.environ if env is None else env
    rich = bool(env.get("COLORTERM")) or "256color" in env.get("TERM", "")
    full, basic = _COLOURS[name]
    return full if rich else basic


def _paint(text: str, code: str, stream: TextIO | None = None) -> str:
    return f"\033[{code}m{text}\033[0m" if color_enabled(stream) else text


def dim(text: str, stream: TextIO | None = None) -> str:
    """Metadata: commands, paths, hints, counters."""
    return _paint(text, _DIM, stream)


def bold(text: str, stream: TextIO | None = None) -> str:
    """Emphasis: the label of a step, a heading, the thing to notice."""
    return _paint(text, _BOLD, stream)


def accent(text: str, stream: TextIO | None = None) -> str:
    """The one warm mark on a screen. Use it once."""
    return _paint(text, _colour_code("accent"), stream)


def _out(stream: TextIO | None) -> TextIO:
    return sys.stdout if stream is None else stream


def _err_out(stream: TextIO | None) -> TextIO:
    return sys.stderr if stream is None else stream


def _wrapped(text: str, indent: int = 0, hanging: int = 0) -> list[str]:
    """Wrap ``text`` so that indent + line never passes MAX_WIDTH."""
    width = max(24, MAX_WIDTH - indent - hanging)
    parts = textwrap.wrap(
        text, width=width, break_long_words=False, break_on_hyphens=False
    ) or [""]
    pad = " " * indent
    rest = " " * (indent + hanging)
    return [pad + parts[0]] + [rest + p for p in parts[1:]]


def line(text: str = "", indent: int = 0, stream: TextIO | None = None) -> None:
    """A verbatim line: blank spacing, or output whose layout is not ours."""
    out = _out(stream)
    print((" " * indent + text) if text else "", file=out)


def banner(title: str, subtitle: str | None = None, stream: TextIO | None = None) -> None:
    """The screen's own name, set once at the top."""
    out = _out(stream)
    print(_paint(title, f"{_colour_code('accent')};{_BOLD}", out), file=out)
    if subtitle:
        print(dim(subtitle, out), file=out)
    print(file=out)


def heading(text: str, indent: int = 2, stream: TextIO | None = None) -> None:
    """A section label: bold ink, no rules, no underline."""
    out = _out(stream)
    print(" " * indent + bold(text, out), file=out)


def step(number: int | str, label: str, detail: str | None = None,
         stream: TextIO | None = None) -> None:
    """``1.  Paste your activation URL``: dim numeral, bold label."""
    out = _out(stream)
    print(f"  {dim(f'{number}.', out)}  {bold(label, out)}", file=out)
    if detail:
        for text in _wrapped(detail, indent=6):
            print(dim(text, out), file=out)


def _status(label: str, colour: str, message: str, stream: TextIO | None) -> None:
    to_stderr = colour in ("warning", "danger")
    out = _err_out(stream) if to_stderr else _out(stream)
    if to_stderr:
        sys.stdout.flush()  # keeps order when stdout and stderr share a file
    head = _paint(label.ljust(5), _colour_code(colour), out)
    parts = _wrapped(message, hanging=LABEL_COLUMN)
    print(f"  {head}  {parts[0]}", file=out)
    for text in parts[1:]:
        print(text, file=out)


def ok(message: str, stream: TextIO | None = None) -> None:
    """Something finished. Green label, plain words."""
    _status("ok", "success", message, stream)


def warn(message: str, stream: TextIO | None = None) -> None:
    """A caveat worth reading before going on. Goes to stderr."""
    _status("warn", "warning", message, stream)


def error(message: str, stream: TextIO | None = None) -> None:
    """Something failed. Goes to stderr, and never in scare capitals."""
    _status("error", "danger", message, stream)


def note(message: str, indent: int = 2, stream: TextIO | None = None) -> None:
    """A quiet aside, dim and wrapped."""
    out = _out(stream)
    for text in _wrapped(message, indent):
        print(dim(text, out), file=out)


def para(text: str, indent: int = 2, stream: TextIO | None = None) -> None:
    """Body prose: default ink, wrapped, indented."""
    out = _out(stream)
    for chunk in _wrapped(text, indent):
        print(chunk, file=out)


def bullet(text: str, indent: int = 4, stream: TextIO | None = None) -> None:
    """One item in a short list. The dot is dim; the words are not."""
    out = _out(stream)
    parts = _wrapped(text, hanging=indent + 2)
    print(" " * indent + dim("·", out) + " " + parts[0], file=out)
    for chunk in parts[1:]:
        print(chunk, file=out)


def kv(key: str, value: str, indent: int = 2, pad: int = 8,
       stream: TextIO | None = None) -> None:
    """A label and its value, e.g. ``vault    /var/lib/autoduo/vault.enc``."""
    out = _out(stream)
    parts = _wrapped(value, hanging=indent + pad + 2)
    print(" " * indent + dim(key.ljust(pad), out) + "  " + parts[0], file=out)
    for chunk in parts[1:]:
        print(chunk, file=out)


def rule(width: int = RULE_WIDTH, stream: TextIO | None = None) -> None:
    """One hairline, when a screen genuinely needs a break. At most one."""
    out = _out(stream)
    print(dim("─" * width, out), file=out)
