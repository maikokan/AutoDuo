"""The live dashboard: one card per autoduo instance.

Every account runs as its own process with its own vault and logs, so the
honest screen is a stack of cards rather than one blended status. A card
carries the account name, its state in a word, today's approvals, how long
ago the last push was approved, how many entries are still pending, and the
last event with its age.

The look borrows Duo Mobile's green on near-black. A terminal colours the
ink, not the background, so the palette becomes a single green accent
(:func:`autoduo.ui.duo_green`): the title, a running instance, and a fresh
approval. Nothing else is coloured, every state is still spelled out, so a
pipe, ``NO_COLOR`` or a colour-blind operator loses nothing. No boxes, no
emoji, no reversed video, and at most one hairline rule on the screen.

A running account with no approval for hours is *quiet*, not broken: low
traffic is healthy, and alarming an operator about a healthy account is the
mistake this project keeps making. So the card says "quiet" and keeps the
age instead of hinting at failure.

Two output paths, chosen per call:

  * not a TTY, ``NO_COLOR`` set, or a pipe -> exactly one plain frame,
    complete, however many accounts there are;
  * a TTY -> redraw in place on the alternate screen with the cursor
    hidden, and restore both in a ``finally`` so Ctrl+C cannot wreck the
    terminal. Quit with ``q`` or Ctrl+C. The frame is cut to the height of
    the window with a quiet "… N more" line, since the alt screen cannot
    scroll.

``statuses`` is a callable returning the list of instance statuses from
:mod:`autoduo.instances`, or a plain list. A callable is used so systemd is
asked once per frame, not once per card. It may raise: the dashboard keeps
the last good frame and adds one dim line naming the error.

Everything drawn from logs, timestamps and names is untrusted input. ANSI
escapes and control characters are stripped, wide characters are measured
as two columns, and no line ever passes the terminal's own width or 80
columns, whichever is smaller.
"""
from __future__ import annotations

import os
import re
import select
import shutil
import sys
import time
import unicodedata
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, TextIO

from . import ui

try:  # POSIX only; without it, Ctrl+C still quits.
    import termios
    import tty
except ImportError:  # pragma: no cover - the project is Linux-only
    termios = None
    tty = None

MAX_WIDTH = 80              # never wider than this, even on a huge terminal
MAX_LINES = 40              # a taller frame than this is not a dashboard
FALLBACK_COLUMNS = 80
FALLBACK_LINES = 24
INDENT = "  "
QUIET_AFTER = 6 * 3600      # an older approval is quiet, not broken
# The daemon emits a heartbeat every 300 s. Four missed beats is not a
# quiet account, it is a loop that stopped turning while systemd still
# reports the unit active -- the exact failure the audit could not see.
STALLED_AFTER = 20 * 60
MAX_INTERVAL = 60.0         # a sanity cap on the refresh argument

_ENTER = "\033[?1049h\033[?25l"     # alternate screen, cursor hidden
_LEAVE = "\033[?25h\033[?1049l"     # cursor back, original screen back
_HOME = "\033[H"
_CLEAR_LINE = "\033[2K"
_CLEAR_BELOW = "\033[J"

_STATES = {
    "running": "running",
    "stopped": "stopped",
    "failed": "failed",
    "no-vault": "no vault",
    "unknown": "unknown",
}

# CSI, OSC and two-byte escapes: enough to remove anything a log can smuggle
# in, including the partial sequence a rotating file leaves behind.
_ANSI = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]"
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|[@-Z\\-_])"
)
# Everything unprintable except tab and newline, which become one space.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_ZERO_WIDTH = frozenset("\u200b\u200c\u200d\ufeff")


# --- styling --------------------------------------------------------------
# Roles are named here and painted by ui, so the palette has one source.

def _dim(stream: TextIO):
    return lambda text: ui.dim(text, stream)


def _green(stream: TextIO):
    return lambda text: ui.duo_green(text, stream)


def _green_bold(stream: TextIO):
    return lambda text: ui.duo_green(text, stream, strong=True)


def _warn(stream: TextIO):
    return lambda text: ui.paint(text, "warning", stream=stream)


def _bold(stream: TextIO):
    return lambda text: ui.bold(text, stream)


def _state_style(state: str, stream: TextIO):
    """Only ``running`` earns the accent; a failure earns weight, not colour."""
    if state == "running":
        return _green(stream)
    if state == "failed":
        return _bold(stream)
    return _dim(stream)


# --- untrusted text -------------------------------------------------------

def _clean(value: Any, limit: int = 160) -> str:
    """One safe line: no escapes, no control characters, no runaway length."""
    if value is None:
        return ""
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:  # an object whose __str__ is broken is not a crisis
        return ""
    text = _ANSI.sub("", text)
    text = _CONTROL.sub(" ", text)
    text = " ".join(text.split())
    return _clip(text, limit)


def _width(text: str) -> int:
    """Columns the text occupies, counting a wide character as two."""
    total = 0
    for char in text:
        if char in _ZERO_WIDTH or unicodedata.combining(char):
            continue
        total += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return total


def _clip(text: str, limit: int) -> str:
    """Shorten to ``limit`` columns with a single ellipsis."""
    if limit <= 0:
        return ""
    if _width(text) <= limit:
        return text
    kept: list[str] = []
    used = 0
    for char in text:
        char_width = _width(char)
        if used + char_width > limit - 1:
            break
        kept.append(char)
        used += char_width
    return "".join(kept) + "…"


def _screen_clip(text: str, limit: int) -> str:
    """Last line of defence: keep visible columns within ``limit``.

    Escapes have no width, so a painted line is measured the way the
    terminal sees it. A clipped line is given a reset, since the one that
    belonged to the text may have been cut off.
    """
    if limit <= 0:
        return ""
    kept: list[str] = []
    used = 0
    index = 0
    while index < len(text):
        if text[index] == "\x1b":
            match = _ANSI.match(text, index)
            if match:
                kept.append(match.group(0))
                index = match.end()
                continue
        char = text[index]
        char_width = _width(char)
        if used + char_width > limit:
            kept.append("\033[0m")
            return "".join(kept)
        kept.append(char)
        used += char_width
        index += 1
    return "".join(kept)


def _row(left: str, right: str, width: int, left_style=None,
         right_style=None) -> str:
    """A left label and a right label on one line, never wider than ``width``."""
    width = max(1, width)
    right = _clip(right, max(0, width - 2))
    room = width - _width(right) - 1
    if right and room < 1:  # a narrow terminal: shrink the right side first
        right = _clip(right, max(0, width - 2))
        room = max(0, width - _width(right) - 1)
    left = _clip(left, max(0, room))
    head = left_style(left) if left_style else left
    if not right:
        return head
    gap = max(0, width - _width(left) - _width(right))
    tail = right_style(right) if right_style else right
    return head + " " * gap + tail


# --- times ----------------------------------------------------------------

def _parse_time(value: Any) -> datetime | None:
    """An ISO timestamp in UTC, or ``None`` when the text is not one."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:  # the daemon writes UTC; assume it when unsaid
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _seconds_since(value: Any, now: datetime) -> float | None:
    moment = _parse_time(value)
    if moment is None:
        return None
    return max(0.0, (now - moment).total_seconds())


def _humanise(seconds: float) -> str:
    """``just now``, ``4m ago``, ``2h ago``, ``3d ago`` -- never a bare number."""
    whole = int(max(0, seconds))
    if whole < 45:
        return "just now"
    minutes = max(1, int(round(whole / 60)))
    if whole < 60 * 60:
        return f"{minutes}m ago"
    hours = max(1, int(round(whole / 3600)))
    if hours < 24:
        return f"{hours}h ago"
    days = max(1, int(round(whole / 86400)))
    if days < 14:
        return f"{days}d ago"
    if days < 90:
        return f"{max(2, int(round(days / 7)))}w ago"
    months = max(3, int(round(days / 30)))
    if months < 18:
        return f"{months}mo ago"
    return f"{max(1, int(round(days / 365)))}y ago"


# --- reading one status ---------------------------------------------------
# A field may be missing, None or of the wrong type. Every getter is total:
# the dashboard runs on a host that may be broken, and a traceback would
# hide every other instance.

def _attr(item: Any, name: str, default: Any = None) -> Any:
    try:
        if isinstance(item, Mapping):
            return item.get(name, default)
        return getattr(item, name, default)
    except Exception:
        return default


def _number(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _name_of(item: Any) -> str:
    name = _attr(_attr(item, "paths"), "name") or _attr(item, "name")
    return _clean(name, 120) or "unnamed"


def _state_of(item: Any) -> str:
    raw = _clean(_attr(item, "state"), 24).lower()
    return _STATES.get(raw, raw or "unknown")


# --- one card -------------------------------------------------------------

def _approval_line(item: Any, state: str, now: datetime) -> tuple[str, str]:
    """The card's headline: a fresh push, a quiet account, or nothing.

    Returns the words and a style key. A *running* account older than
    ``QUIET_AFTER`` is healthy and says so; a stopped one simply has an old
    approval, so it gets the plain age. Only an unreadable timestamp is
    reported as unknown, and even that stays calm.
    """
    stamp = _attr(item, "last_approval_at")
    elapsed = _seconds_since(stamp, now)
    if elapsed is None:
        if stamp in (None, ""):
            return "no approvals yet", "dim"
        return "last approval time unreadable", "dim"
    age = _humanise(elapsed)
    if elapsed > QUIET_AFTER:
        if state == "running":
            return f"quiet · last approval {age}", "plain"
        return f"last approval {age}", "dim"
    if state == "running":
        return f"approved {age}", "green"
    return f"last approval {age}", "dim"


def _event_line(item: Any, now: datetime) -> str:
    event = _clean(_attr(item, "last_event"), 40)
    if not event:
        return "last event  none yet"
    elapsed = _seconds_since(_attr(item, "last_event_at"), now)
    if elapsed is None:
        return f"last event  {event}"
    return f"last event  {event} · {_humanise(elapsed)}"


def _card(item: Any, width: int, stream: TextIO, now: datetime) -> list[str]:
    state = _state_of(item)
    lines = [_row(_name_of(item), state, width, _bold(stream),
                  _state_style(state, stream))]

    error = _clean(_attr(item, "error"), 200)
    if error:
        # A dim note instead of zeros that would be a lie.
        lines.append(_row(error, "", width, _dim(stream)))
        return lines

    headline, kind = _approval_line(item, state, now)
    styles = {"green": _green(stream), "dim": _dim(stream), "plain": None}
    # The field is a rolling 24 hours, not a calendar day, so say so.
    counts = (f"{_number(_attr(item, 'approvals_24h'))} in 24h · "
              f"{_number(_attr(item, 'pending'))} pending")
    lines.append(_row(headline, counts, width, styles[kind], _dim(stream)))
    lines.append(_row(_event_line(item, now), "", width, _dim(stream)))

    if state == "running":
        silent = _seconds_since(_attr(item, "last_event_at"), now)
        if silent is not None and silent > STALLED_AFTER:
            # systemd says active; the log says nothing has happened. Say
            # both, because "running" alone would be the lie.
            lines.append(
                _row(f"no event for {_humanise(silent).removesuffix(' ago')} · "
                     "the loop may be stuck", "", width, _warn(stream))
            )

    if state == "failed":
        lines.append(_row("stopped after an error, see the service log", "",
                          width, _dim(stream)))
    return lines


def _safe_card(item: Any, width: int, stream: TextIO,
               now: datetime) -> list[str]:
    """A card that cannot take the screen down with it."""
    try:
        return _card(item, width, stream, now)
    except Exception:
        return [_row("an instance could not be read", "", width,
                     _dim(stream))]


# --- the frame ------------------------------------------------------------

def _summary(items: list[Any]) -> str:
    if not items:
        return ""
    running = sum(1 for item in items if _state_of(item) == "running")
    failed = sum(1 for item in items if _state_of(item) == "failed")
    parts = [f"{len(items)} account" + ("" if len(items) == 1 else "s"),
             f"{running} running" if running else "none running"]
    if failed:
        parts.append(f"{failed} failed")
    return " · ".join(parts)


def _fit(blocks: list[list[str]], room: int) -> tuple[list[list[str]], int]:
    """As many cards as fit in ``room`` lines, and how many were dropped."""
    kept: list[list[str]] = []
    used = 0
    for index, block in enumerate(blocks):
        hidden_after = len(blocks) - index - 1
        cost = len(block) + 1          # the card and its blank separator
        tail = 1 if hidden_after else 0  # the "… N more" line
        if kept and used + cost + tail > room:
            break
        used += cost
        kept.append(block)
    return kept, len(blocks) - len(kept)


def _render(items: list[Any] | None, width: int, stream: TextIO,
            error: str | None = None, live: bool = False,
            now: datetime | None = None,
            height: int | None = None) -> list[str]:
    """The whole screen as lines, each already within ``width`` columns.

    ``height`` caps the frame for the alternate screen, which cannot
    scroll; the plain path passes ``None`` and stays complete.
    """
    now = datetime.now(timezone.utc) if now is None else now
    width = max(1, width)
    budget = max(1, width - len(INDENT))
    lines: list[str] = [""]

    if items is None:
        lines.append(INDENT + _row("autoduo", "", budget, _green_bold(stream)))
        lines.append("")
        note = error or "account states are unavailable"
        lines.append(INDENT + _row(note, "", budget, _dim(stream)))
        lines.append("")
        return [_screen_clip(line, width) for line in lines]

    lines.append(INDENT + _row("autoduo", _summary(items), budget,
                               _green_bold(stream), _dim(stream)))
    lines.append(INDENT + ui.dim("─" * budget, stream))
    lines.append("")

    if not items:
        lines.append(INDENT + "No accounts are set up yet.")
        lines.append(INDENT + ui.dim("Run  autoduo setup  to create one.",
                                     stream))
        lines.append("")
    else:
        blocks = [_safe_card(item, budget, stream, now) for item in items]
        hidden = 0
        if height is not None:
            room = max(1, height - len(lines) - (1 if live else 0)
                       - (1 if error else 0))
            blocks, hidden = _fit(blocks, room)
        for block in blocks:
            lines.extend(INDENT + text for text in block)
            lines.append("")
        if hidden:
            lines.append(INDENT + ui.dim(f"… {hidden} more", stream))

    if error:
        lines.append(INDENT + _row(f"states unavailable · {error}", "",
                                   budget, _dim(stream)))
        lines.append("")
    if live:
        lines.append(INDENT + ui.dim("q to quit", stream))
    return [_screen_clip(line, width) for line in lines]


# --- output ---------------------------------------------------------------

def _write(out: TextIO, lines: list[str]) -> None:
    """One static frame. A closed pipe is not worth a traceback."""
    try:
        for text in lines:
            out.write(text + "\n")
        out.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass


def _draw(out: TextIO, lines: list[str]) -> bool:
    """Redraw in place: home, clear each line, clear what is left below."""
    try:
        out.write(_HOME)
        for text in lines:
            out.write(_CLEAR_LINE + text + "\n")
        out.write(_CLEAR_LINE + _CLEAR_BELOW)
        out.flush()
    except (BrokenPipeError, OSError, ValueError):
        return False
    return True


def _screen_size(out: TextIO) -> tuple[int, int]:
    """Columns and lines, from the stream's terminal or the environment.

    A pty can report zero (``script`` and some CI terminals do), and a
    zero-column frame is a blank frame, so anything unusable falls back to
    ``COLUMNS``/``LINES`` or 80x24 instead of being taken literally.
    """
    columns = 0
    lines = 0
    try:
        size = os.get_terminal_size(out.fileno())
        columns, lines = size.columns, size.lines
    except Exception:
        columns = lines = 0
    if columns <= 0 or lines <= 0:
        try:
            fallback = shutil.get_terminal_size(
                (FALLBACK_COLUMNS, FALLBACK_LINES))
            columns = columns if columns > 0 else fallback.columns
            lines = lines if lines > 0 else fallback.lines
        except Exception:
            columns = columns or FALLBACK_COLUMNS
            lines = lines or FALLBACK_LINES
    try:
        columns = int(columns)
    except (TypeError, ValueError):
        columns = FALLBACK_COLUMNS
    try:
        lines = int(lines)
    except (TypeError, ValueError):
        lines = FALLBACK_LINES
    columns = columns if columns > 0 else FALLBACK_COLUMNS
    lines = lines if lines > 0 else FALLBACK_LINES
    return max(1, min(columns, MAX_WIDTH)), max(1, min(lines, MAX_LINES))


def _columns(out: TextIO) -> int:
    """The line budget: the terminal's width, capped at 80 columns."""
    return _screen_size(out)[0]


def _live(out: TextIO) -> bool:
    """Live redrawing needs a terminal and permission to colour it.

    ``NO_COLOR`` is taken as a request for plain, copyable output even on a
    TTY, so that path prints one frame and returns.
    """
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return bool(out.isatty())
    except Exception:
        return False


def _interval(refresh: Any) -> float:
    try:
        value = float(refresh)
    except (TypeError, ValueError):
        return 1.0
    if value != value:  # NaN
        return 1.0
    return min(MAX_INTERVAL, max(0.0, value))


def _snapshot(source: Any) -> tuple[list[Any] | None, str | None]:
    """One frame of data, or ``None`` plus a reason it could not be had."""
    try:
        raw = source() if callable(source) else source
        if raw is None or isinstance(raw, (str, bytes)):
            return [], None
        return list(raw), None
    except Exception as exc:  # a broken status read is a line, not a crash
        detail = _clean(str(exc), 120) or "no detail"
        return None, _clean(f"{type(exc).__name__}: {detail}", 160)


@contextmanager
def _keys():
    """Best-effort cbreak input, so ``q`` acts without Enter.

    Nothing changes when stdin is not a terminal, and the original settings
    are restored even when Ctrl+C interrupts the read. Setup is kept apart
    from the ``yield`` so an error in the loop body cannot be mistaken for
    an error in the terminal (which would make contextlib yield twice).
    """
    handle = None
    saved = None
    if tty is not None and termios is not None:
        try:
            if sys.stdin.isatty():
                handle = sys.stdin.fileno()
                saved = termios.tcgetattr(handle)
        except Exception:
            handle = saved = None
    if handle is None or saved is None:
        yield False
        return
    try:
        tty.setcbreak(handle)
    except Exception:
        yield False
        return
    try:
        yield True
    finally:
        try:
            termios.tcsetattr(handle, termios.TCSADRAIN, saved)
        except Exception:
            pass


def _pause(timeout: float) -> bool:
    try:
        time.sleep(max(0.0, timeout))
    except (OSError, ValueError):
        pass
    return False


def _quit(timeout: float) -> bool:
    """Wait up to ``timeout`` for a key; ``True`` means stop the loop."""
    try:
        handle = sys.stdin.fileno()
    except Exception:
        return _pause(timeout)
    try:
        ready, _, _ = select.select([handle], [], [], max(0.0, timeout))
    except Exception:
        return _pause(timeout)
    if not ready:
        return False
    try:
        key = sys.stdin.read(1)
    except Exception:
        return False
    return key in ("q", "Q") or key == ""  # EOF must not spin the loop


def _live_loop(statuses: Any, interval: float, columns: int,
               out: TextIO) -> int:
    items: list[Any] | None = None
    error: str | None = None
    try:
        out.write(_ENTER)
        out.flush()
        with _keys():
            while True:
                fresh, failure = _snapshot(statuses)
                if failure is None:
                    items, error = fresh, None
                else:
                    error = failure  # keep the last good frame's data
                columns, height = _screen_size(out)
                frame = _render(items, columns, out, error=error, live=True,
                                height=height)
                if not _draw(out, frame):
                    break
                if _quit(interval):
                    break
    except KeyboardInterrupt:
        return 0
    finally:
        try:
            out.write(_LEAVE)
            out.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
    return 0


def run(statuses: Any, refresh: float = 1.0,
        stream: TextIO | None = None) -> int:
    """Show the dashboard until the operator quits. Returns 0.

    ``statuses`` is a list of instance statuses or a callable returning one;
    a callable is asked once per frame, so systemd is queried once per
    refresh rather than once per card. ``refresh`` is the redraw interval in
    seconds. ``stream`` defaults to stdout, and is what a test passes in.
    """
    out = sys.stdout if stream is None else stream
    columns = _columns(out)
    if not _live(out):
        items, error = _snapshot(statuses)
        _write(out, _render(items, columns, out, error=error))
        # A frame that could not read the host is not a healthy frame. An
        # operator sees the line either way; a script gets a non-zero exit
        # instead of the green it would otherwise be handed.
        return 1 if error else 0
    return _live_loop(statuses, _interval(refresh), columns, out)
