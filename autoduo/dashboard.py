"""The live dashboard: one card per autoduo instance.

A calm screen for one question -- "is my phone-free login working right
now?" -- not a metrics board.  Each card leads with the instance's state,
then the facts that answer whether it is doing its job: the last approval,
what is pending, and the last thing that happened.

The frame is built as one string and written in one call.  On a terminal
that means the alternate screen and a redraw in place, never a flicker;
in a pipe, in a test, or under ``NO_COLOR`` it means exactly one plain
frame and exit, with no cursor codes at all.

Colour follows :mod:`autoduo.ui` (``NO_COLOR`` first, then ``FORCE_COLOR``,
then a TTY).  Duo's green is the accent and it means "alive": the wordmark
and the word ``running``.  A stopped instance is dim, an unknown one is
amber, and a failed one is muted red -- never bright red, never reverse
video, never a background.
"""
from __future__ import annotations

import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Iterable, TextIO

from . import ui

if TYPE_CHECKING:  # the seam lives in instances.py, which lands separately
    from .instances import InstanceStatus

__all__ = ["render", "run"]

Segment = tuple[str, str | None]
Line = list[Segment]

WIDE_MIN = 60            # below this the card stacks instead of using columns
LABEL_PAD = 15           # "last approval" plus two spaces
FEED_LIMIT = 8
QUIET_DAYS = 7
DAY = 86_400

_ALT_ON = "\033[?1049h"
_ALT_OFF = "\033[?1049l"
_HIDE_CURSOR = "\033[?25l"
_SHOW_CURSOR = "\033[?25h"
_HOME = "\033[H"
_CLEAR_BELOW = "\033[J"

# daemon.log carries snake_case event names; a person should not have to read
# them.  Anything not listed is un-cased, never guessed at.
_EVENT_WORDS = {
    "heartbeat": "heartbeat",
    "approve_ok": "approved",
    "approve_rejected": "approval refused",
    "approve_auth_error": "approval auth failed",
    "daemon_starting": "started",
    "daemon_stopped": "stopped",
    "tx_seen": "push seen",
    "tx_deduped": "push already handled",
    "rate_limited": "rate limited",
    "auth_error": "authentication failed",
    "circuit_open": "paused after auth failures",
    "circuit_closed": "resumed",
    "state_loaded": "state loaded",
    "state_saved": "state saved",
}


# --------------------------------------------------------------------------
# small text helpers

def _paint(text: str, tone: str | None, stream: TextIO | None) -> str:
    """One segment in a named tone; ``None`` leaves the terminal's own ink."""
    if not text or tone is None:
        return text
    if tone == "dim":
        return ui.dim(text, stream)
    if tone == "bold":
        return ui.bold(text, stream)
    if tone == "brand":
        return ui.duo_green(text, stream)
    if tone == "brand-strong":
        return ui.duo_green(text, stream, strong=True)
    return ui.paint(text, tone, stream=stream)


def _seg_width(segs: Iterable[Segment]) -> int:
    """Visible width: painted text never counts its escape sequences."""
    return sum(len(text) for text, _ in segs)


def _render(line: Line, stream: TextIO | None) -> str:
    """Paint a line, merging neighbours in the same tone into one escape run."""
    merged: Line = []
    for text, tone in line:
        if not text:
            continue
        if merged and merged[-1][1] == tone:
            merged[-1] = (merged[-1][0] + text, tone)
        else:
            merged.append((text, tone))
    return "".join(_paint(text, tone, stream) for text, tone in merged)


def _trim(segs: Line, limit: int) -> Line:
    """Cut a line to ``limit`` visible cells, marking the cut with an ellipsis."""
    if limit <= 0:
        return []
    if _seg_width(segs) <= limit:
        return list(segs)
    out: Line = []
    room = limit - 1
    used = 0
    for text, tone in segs:
        if used >= room:
            break
        take = min(len(text), room - used)
        if take > 0:
            out.append((text[:take], tone))
            used += take
    out.append(("…", out[-1][1] if out else None))
    return out


def _join(lines: Iterable[Line], stream: TextIO | None, width: int) -> str:
    """The whole frame, one line per row, no line past ``width - 2``."""
    edge = max(0, width - 2)
    kept = list(lines)
    while kept and not _seg_width(kept[-1]):
        kept.pop()
    body = "\n".join(_render(_trim(line, edge), stream) for line in kept)
    return body + "\n"


def _ellipsis_middle(text: str, limit: int) -> str:
    """``very-long-instance-name`` inside ``limit``, both ends kept."""
    if limit <= 0:
        return ""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    head = (limit - 1) // 2
    tail = limit - 1 - head
    return text[:head] + "…" + text[-tail:]


def _end_trim(text: str, limit: int) -> str:
    """``text`` on one line, cut at the end: for prose, never for names."""
    if limit <= 0:
        return ""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    return text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------
# time

def _terminal_width() -> int:
    try:
        columns = shutil.get_terminal_size(fallback=(80, 24)).columns
    except OSError:  # an exotic stream cannot answer
        columns = 80
    return max(1, min(int(columns), ui.MAX_WIDTH))


def _parse_time(value: object) -> datetime | None:
    """ISO 8601 (with or without ``Z``), epoch seconds, or nothing."""
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, (int, float)):
        try:
            moment = datetime.fromtimestamp(float(value))
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            moment = datetime.fromtimestamp(float(text))
        except ValueError:
            if text.endswith(("Z", "z")):
                text = text[:-1] + "+00:00"
            try:
                moment = datetime.fromisoformat(text)
            except ValueError:
                return None
        except (OverflowError, OSError):
            return None
    if moment.tzinfo is not None:
        moment = moment.astimezone()
    return moment


def _age_seconds(value: object, now: datetime) -> float | None:
    moment = _parse_time(value)
    if moment is None:
        return None
    return max(0.0, now.timestamp() - moment.timestamp())


def _human_age(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return "just now"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = total // 3600
    if hours < 24:
        return f"{hours}h ago"
    days = total // DAY
    return "1 day ago" if days == 1 else f"{days} days ago"


def _days_text(seconds: float) -> str:
    days = max(1, int(seconds // DAY))
    return "1 day" if days == 1 else f"{days} days"


# --------------------------------------------------------------------------
# one row, reduced to what the card shows

@dataclass
class _Summary:
    name: str
    state_label: str
    state_tone: str
    headline: tuple[str, str] | None
    headline_short: str | None
    sentence: tuple[str, str | None] | None
    pending: int | None
    facts: list[tuple[str, str, str | None]]


def _state_of(row: object) -> str:
    state = getattr(row, "state", None)
    text = str(state).strip().lower() if state is not None else ""
    return text or "unknown"


def _name_of(row: object) -> str:
    paths = getattr(row, "paths", None)
    name = getattr(paths, "name", None)
    text = " ".join(str(name).split()) if name is not None else ""
    return text or "unnamed"


def _int_of(row: object, field: str) -> int:
    try:
        return max(0, int(getattr(row, field, 0)))
    except (TypeError, ValueError):
        return 0


def _approvals_text(count: int) -> str:
    noun = "approval" if count == 1 else "approvals"
    return f"{count} {noun} today"


def _event_words(event: object) -> str:
    if event is None:
        return ""
    text = " ".join(str(event).split())
    if not text:
        return ""
    return _end_trim(_EVENT_WORDS.get(text, text.replace("_", " ")), 80)


def _event_text(row: object, now: datetime) -> str:
    text = _event_words(getattr(row, "last_event", None))
    moment = _parse_time(getattr(row, "last_event_at", None))
    clock = ""
    if moment is not None:
        clock = moment.strftime("%H:%M")
        if moment.date() != now.date():
            clock = moment.strftime("%d %b %H:%M")
    if text and clock:
        return f"{text} at {clock}"
    return text or clock


def _tidy_error(error: object, limit: int | None = None) -> str:
    """One line, no traceback frame, and never the whole stack."""
    lines = [ln.strip() for ln in str(error or "").splitlines() if ln.strip()]
    if lines and lines[0].startswith("Traceback ("):
        text = lines[-1]  # the exception line names the actual fault
    else:
        text = " ".join(lines)
    if not text:
        text = "no detail given"
    return _end_trim(text, limit) if limit else text


def _summarise(row: object, now: datetime) -> _Summary:
    state = _state_of(row)
    label, tone = ui.state_style(state)
    approvals = _int_of(row, "approvals_24h")
    age = _age_seconds(getattr(row, "last_approval_at", None), now)
    error = str(getattr(row, "error", "") or "").strip()
    quiet = age is not None and age >= QUIET_DAYS * DAY

    headline: tuple[str, str] | None = None
    short: str | None = None
    sentence: tuple[str, str | None] | None = None
    pending: int | None = None
    facts: list[tuple[str, str, str | None]] = []

    if state == "running":
        pending = _int_of(row, "pending")
        if quiet:
            headline = (f"quiet for {_days_text(age)}", "warning")
        elif age is None:
            headline = ("no approvals yet", "dim")
        else:
            headline = (_approvals_text(approvals), None)
            short = f"{approvals} today"
    elif state == "failed":
        pending = _int_of(row, "pending")
        if approvals:
            headline = (_approvals_text(approvals), None)
            short = f"{approvals} today"
    elif state == "unknown":
        pending = _int_of(row, "pending")
        if approvals:
            headline = (_approvals_text(approvals), None)
            short = f"{approvals} today"
        if not error:
            sentence = ("could not read its status", "dim")
    elif state == "stopped":
        if age is not None and age >= DAY:
            sentence = (f"no approval in {_days_text(age)}", None)
        elif age is not None:
            facts.append(("last approval", _human_age(age), None))
    elif state == "no-vault":
        sentence = ("run autoduo setup to enroll it", "dim")

    if age is not None and state in ("running", "failed", "unknown") and not quiet:
        facts.insert(0, ("last approval", _human_age(age), None))
    if error:
        facts.append(("error", _tidy_error(error, 200), "warning"))
    event = _event_text(row, now)
    if event and state in ("running", "failed", "unknown"):
        facts.append(("last event", event, None))

    return _Summary(
        name=_name_of(row),
        state_label=label,
        state_tone=tone,
        headline=headline,
        headline_short=short,
        sentence=sentence,
        pending=pending,
        facts=facts,
    )


# --------------------------------------------------------------------------
# cards

def _kv(fact: tuple[str, str, str | None]) -> Line:
    label, value, tone = fact
    return [("    ", None), (label.ljust(LABEL_PAD), "dim"), (value, tone)]


def _pending_cell(count: int) -> Line:
    return [
        ("pending", "dim"),
        ("   ", None),
        (str(count), "warning" if count else None),
    ]


def _row_with_right(left: Line, right: Line, edge: int) -> list[Line]:
    if _seg_width(left) + 2 + _seg_width(right) <= edge:
        gap = edge - _seg_width(left) - _seg_width(right)
        return [left + [(" " * gap, None)] + right]
    pad = max(0, edge - _seg_width(right))
    return [left, [(" " * pad, None)] + right]


def _wide_card(summary: _Summary, width: int) -> list[Line]:
    edge = max(0, width - 2)
    state_col = max(12, width - 34)
    name = _ellipsis_middle(summary.name, max(1, state_col - 4))
    line: Line = [
        ("  ", None),
        (name, "bold"),
        (" " * max(0, state_col - 2 - len(name)), None),
        (summary.state_label, summary.state_tone),
    ]
    if summary.headline:
        text, tone = summary.headline
        for candidate in (text, summary.headline_short):
            if not candidate:
                continue
            gap = edge - _seg_width(line) - len(candidate)
            if gap >= 1:
                line = line + [(" " * gap, None), (candidate, tone)]
                break

    lines = [line]
    if summary.sentence:
        text, sentence_tone = summary.sentence
        lines.append([("    ", None), (text, sentence_tone)])
    facts = list(summary.facts)
    if summary.pending is not None and facts and facts[0][0] == "last approval":
        paired = _row_with_right(_kv(facts.pop(0)), _pending_cell(summary.pending), edge)
        lines.extend(paired)
    elif summary.pending is not None:
        lines.append(_kv(("pending", str(summary.pending),
                          "warning" if summary.pending else None)))
    lines.extend(_kv(fact) for fact in facts)
    return lines


def _narrow_card(summary: _Summary, width: int) -> list[Line]:
    edge = max(0, width - 2)
    name = _ellipsis_middle(summary.name, max(1, edge - 2))
    lines: list[Line] = [[("  ", None), (name, "bold")]]
    state_line: Line = [("    ", None),
                        (summary.state_label, summary.state_tone)]
    extra: Line | None = None
    if summary.headline:
        text, tone = summary.headline
        joined = state_line + [("  ·  ", "dim"), (text, tone)]
        if _seg_width(joined) <= edge:
            state_line = joined
        else:
            extra = [("    ", None), (text, tone)]
    lines.append(state_line)
    if extra:
        lines.append(extra)
    if summary.sentence:
        text, sentence_tone = summary.sentence
        lines.append([("    ", None), (text, sentence_tone)])
    facts = list(summary.facts)
    pending_fact = ("pending", str(summary.pending),
                    "warning" if summary.pending else None)
    if summary.pending is not None and facts and facts[0][0] == "last approval":
        lines.append(_kv(facts.pop(0)))
        lines.append(_kv(pending_fact))
    elif summary.pending is not None:
        lines.append(_kv(pending_fact))
    lines.extend(_kv(fact) for fact in facts)
    return lines


# --------------------------------------------------------------------------
# the push feed

def _feed(rows: list, width: int, now: datetime) -> list[Line]:
    recent: list[tuple[float, str, str]] = []
    for row in rows:
        moment = _parse_time(getattr(row, "last_approval_at", None))
        if moment is None:
            continue
        age = now.timestamp() - moment.timestamp()
        if age >= DAY:
            continue
        recent.append((moment.timestamp(), moment.strftime("%H:%M"), _name_of(row)))
    recent.sort(key=lambda item: item[0], reverse=True)

    lines: list[Line] = [[("  ", None), ("recent approvals", "bold")]]
    if not recent:
        lines.append([("    ", None), ("no approvals in the last 24 hours", "dim")])
        return lines
    room = max(4, width - 2 - 4 - 5 - 2 - 2 - len("approved"))
    for _, clock, name in recent[:FEED_LIMIT]:
        lines.append([
            ("    ", None),
            (clock, "dim"),
            ("  ", None),
            (_ellipsis_middle(name, room), None),
            ("  ", None),
            ("approved", "dim"),
        ])
    return lines


# --------------------------------------------------------------------------
# header and frame

def _totals_variants(rows: list) -> list[str]:
    total = len(rows)
    running = sum(1 for row in rows if _state_of(row) == "running")
    failed = sum(1 for row in rows if _state_of(row) == "failed")
    unenrolled = sum(1 for row in rows if _state_of(row) == "no-vault")
    approvals = sum(_int_of(row, "approvals_24h") for row in rows)
    noun = "instance" if total == 1 else "instances"
    head = f"{total} {noun} · {running} running"
    today = f"{approvals} {'approval' if approvals == 1 else 'approvals'} today"
    extras = ""
    if failed:
        extras += f" · {failed} failed"
    if unenrolled:
        extras += f" · {unenrolled} not enrolled"
    return [
        f"{head}{extras} · {today}",
        f"{head} · {today}",
        f"{total} {noun} · {today}",
    ]


def _pick_totals(rows: list, room: int) -> str | None:
    for text in _totals_variants(rows):
        if len(text) <= room:
            return text
    return None


def _header(rows: list, width: int, note: str | None) -> list[Line]:
    edge = max(0, width - 2)
    word: Line = [("  ", None), ("autoduo", "brand-strong")]
    if rows:
        room = edge - _seg_width(word) - 2
        totals = _pick_totals(rows, room)
        if totals is not None:
            gap = edge - _seg_width(word) - len(totals)
            lines = [word + [(" " * gap, None), (totals, "dim")]]
        else:
            totals = _pick_totals(rows, edge - 2) or _totals_variants(rows)[-1]
            lines = [word, [("  ", None), (totals, "dim")]]
        lines.append([("  ", None), ("─" * max(0, width - 4), "dim")])
    else:
        lines = [word]
        text = "no instances yet"
        gap = edge - _seg_width(word) - len(text)
        if gap >= 1:
            lines[0] = word + [(" " * gap, None), (text, "dim")]
        else:
            lines.append([("  ", None), (text, "dim")])
    if note:
        lines.append([("  ", None), (str(note), "warning")])
    return lines


def render(rows: Iterable[InstanceStatus], width: int | None = None,
           stream: TextIO | None = None, now: datetime | None = None,
           note: str | None = None) -> str:
    """One complete frame, plain text unless ``stream`` earns colour.

    ``width`` defaults to the terminal, capped at :data:`autoduo.ui.MAX_WIDTH`
    so the dashboard never stretches past the prose it sits beside.  Every
    line is laid out inside ``width - 2`` columns: two for the left indent,
    two for the right margin.
    """
    if width is None:
        width = _terminal_width()
    else:
        width = max(1, min(int(width), ui.MAX_WIDTH))
    rows = list(rows or [])
    moment = now if now is not None else datetime.now().astimezone()

    lines = _header(rows, width, note)
    if not rows:
        message = "Run autoduo setup to enroll the first instance."
        if len(message) + 2 > width - 2:
            message = "Run autoduo setup to enroll one."
        lines += [
            [],
            [("  ", None), ("No instances yet.", "bold")],
            [("  ", None), (message, "dim")],
        ]
        return _join(lines, stream, width)

    lines.append([])
    for index, row in enumerate(rows):
        if index:
            lines.append([])
        summary = _summarise(row, moment)
        if width >= WIDE_MIN:
            lines.extend(_wide_card(summary, width))
        else:
            lines.extend(_narrow_card(summary, width))
    lines.append([])
    lines.extend(_feed(rows, width, moment))
    return _join(lines, stream, width)


# --------------------------------------------------------------------------
# terminal

def _provider(rows_provider: object) -> Callable[[], Iterable[InstanceStatus]]:
    """The agreed seam is a callable; a plain list is accepted as a courtesy.

    ``cli.py`` may hand over a captured ``status_all()`` result while its own
    lane catches up; a callable is still asked afresh on every refresh.
    """
    if callable(rows_provider):
        return rows_provider
    rows = list(rows_provider or [])
    return lambda: rows


def _interactive(stream: TextIO, once: bool) -> bool:
    """Live redraw needs a terminal, colour, and ``NO_COLOR`` left unset."""
    if once:
        return False
    try:
        if not stream.isatty():
            return False
    except Exception:  # a closed or exotic stream cannot answer
        return False
    return ui.color_enabled(stream)


def _write(stream: TextIO, text: str) -> None:
    stream.write(text)
    stream.flush()


def _restore(stream: TextIO) -> None:
    try:
        stream.write(_SHOW_CURSOR + _ALT_OFF)
        stream.flush()
    except OSError:  # the terminal is already gone
        return


def _failure_frame(error: object, stream: TextIO | None) -> str:
    width = _terminal_width()
    lines: list[Line] = [
        [("  ", None), ("autoduo", "brand-strong")],
        [],
        [("  ", None), ("Could not read instance status", "warning")],
        [("    ", None), (_tidy_error(error, max(8, width - 6)), "dim")],
    ]
    return _join(lines, stream, width)


def run(rows_provider: Callable[[], Iterable[InstanceStatus]],
        refresh: float = 1.0, stream: TextIO | None = None,
        once: bool = False) -> int:
    """Draw the dashboard until Ctrl+C, and restore the terminal either way.

    ``rows_provider`` is called for every frame: it may shell out to
    ``systemctl`` and it may raise.  The very first failure is fatal (return
    1, one quiet line, no traceback); a later one keeps the last good rows
    and says so above them.

    ``once``, a non-terminal stream, or ``NO_COLOR`` renders a single frame
    and returns 0.  Returns 0 on a normal Ctrl+C as well.
    """
    out = sys.stdout if stream is None else stream
    provider = _provider(rows_provider)

    try:
        rows = list(provider() or [])
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        _write(out, _failure_frame(exc, out))
        return 1

    if not _interactive(out, once):
        _write(out, render(rows, stream=out))
        return 0

    delay = max(0.0, float(refresh))
    entered = False
    try:
        _write(out, _ALT_ON + _HIDE_CURSOR)
        entered = True
        _write(out, _HOME + render(rows, stream=out) + _CLEAR_BELOW)
        while True:
            if delay:
                time.sleep(delay)
            note = None
            try:
                rows = list(provider() or [])
            except Exception as exc:
                note = "status check failed · " + _tidy_error(exc, 120)
            _write(out, _HOME + render(rows, stream=out, note=note) + _CLEAR_BELOW)
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        return 0
    finally:
        if entered:
            _restore(out)
