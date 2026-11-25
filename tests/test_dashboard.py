"""Tests for the live dashboard: total rendering and a terminal left clean.

The dashboard is fed fake instance statuses and a fake stream, so the whole
suite stays offline and deterministic. Every frame is also checked against
the width budget, because a card that wraps is a card an operator cannot
read at a glance.
"""
import io
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from autoduo import dashboard, ui

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class _Tty(io.StringIO):
    """A stream a terminal could plausibly be."""

    def isatty(self):
        return True


def _env(monkeypatch, term="xterm-256color", force=None, no_color=None,
         columns=None):
    """Pin every colour and geometry input for one test."""
    setting = {"TERM": term, "FORCE_COLOR": force, "NO_COLOR": no_color,
               "COLORTERM": None, "COLUMNS": columns}
    for name, value in setting.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def _ago(**age):
    moment = datetime.now(timezone.utc) - timedelta(**age)
    return moment.isoformat()


def _row(name="work", **overrides):
    fields = dict(
        paths=SimpleNamespace(name=name),
        state="running",
        last_event="approve_ok",
        last_event_at=None,
        last_approval_at=None,
        approvals_24h=0,
        pending=0,
        error=None,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _frame(rows, monkeypatch, **env):
    """The plain single-frame path, as a list of lines."""
    _env(monkeypatch, **env)
    buf = io.StringIO()
    code = dashboard.run(rows, stream=buf)
    assert code == 0
    return buf.getvalue().splitlines()


def _plain(lines):
    return [ANSI.sub("", line) for line in lines]


def _fits(lines, width=80):
    for line in lines:
        assert len(ANSI.sub("", line)) <= width, repr(line)


def _display_width(text):
    total = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        total += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return total


# --- the empty screen -----------------------------------------------------

def test_no_instances_is_one_calm_line_and_the_command(monkeypatch):
    lines = _frame([], monkeypatch)
    text = "\n".join(_plain(lines))
    assert "No accounts are set up yet." in text
    assert "autoduo setup" in text
    assert "error" not in text.lower()
    _fits(lines)


def test_an_empty_callable_is_the_same_screen(monkeypatch):
    lines = _frame(lambda: [], monkeypatch)
    assert "No accounts are set up yet." in "\n".join(_plain(lines))


# --- a card ---------------------------------------------------------------

def test_running_instance_shows_a_fresh_approval(monkeypatch):
    item = _row(last_approval_at=_ago(minutes=2), last_event_at=_ago(minutes=2),
                approvals_24h=3)
    text = "\n".join(_plain(_frame([item], monkeypatch)))
    assert "work" in text
    assert "running" in text
    assert "approved 2m ago" in text
    assert "3 in 24h · 0 pending" in text
    assert "last event  approve_ok · 2m ago" in text


def test_pending_pushes_are_counted(monkeypatch):
    item = _row(pending=2, approvals_24h=1)
    text = "\n".join(_plain(_frame([item], monkeypatch)))
    assert "1 in 24h · 2 pending" in text


def test_stopped_instance_still_names_its_state(monkeypatch):
    item = _row(name="personal", state="stopped", last_approval_at=_ago(days=3))
    text = "\n".join(_plain(_frame([item], monkeypatch)))
    assert "personal" in text
    assert "stopped" in text
    assert "quiet" not in text  # quiet is a running account's story


def test_no_vault_and_unknown_read_as_words(monkeypatch):
    rows = [_row(name="fresh", state="no-vault"),
            _row(name="odd", state="something-else")]
    text = "\n".join(_plain(_frame(rows, monkeypatch)))
    assert "no vault" in text
    assert "something-else" in text


def test_failed_instance_says_so_and_points_at_the_log(monkeypatch):
    item = _row(name="broken", state="failed")
    text = "\n".join(_plain(_frame([item], monkeypatch)))
    assert "failed" in text
    assert "see the service log" in text


def test_an_error_is_a_dim_note_not_fake_zeros(monkeypatch):
    item = _row(name="locked", state="unknown",
                error="cannot read log: permission denied")
    text = "\n".join(_plain(_frame([item], monkeypatch)))
    assert "cannot read log: permission denied" in text
    assert "0 in 24h" not in text
    assert "0 pending" not in text


def test_an_over_long_error_is_clipped(monkeypatch):
    item = _row(error="x" * 500)
    lines = _frame([item], monkeypatch)
    _fits(lines)
    assert "…" in "\n".join(_plain(lines))


# --- time -----------------------------------------------------------------

def test_an_iso_timestamp_far_in_the_past_is_humanised(monkeypatch):
    item = _row(last_approval_at="2020-01-01T00:00:00Z")
    text = "\n".join(_plain(_frame([item], monkeypatch)))
    assert "quiet · last approval" in text
    assert re.search(r"\d+(y|mo) ago", text), text


def test_a_naive_timestamp_is_read_as_utc(monkeypatch):
    item = _row(last_approval_at=_ago(hours=2)[:-6])  # drop the +00:00
    text = "\n".join(_plain(_frame([item], monkeypatch)))
    assert "approved 2h ago" in text


def test_an_unparseable_timestamp_stays_calm(monkeypatch):
    item = _row(last_approval_at="last tuesday", last_event_at="???")
    lines = _frame([item], monkeypatch)
    text = "\n".join(_plain(lines))
    assert "last approval time unreadable" in text
    assert "last event  approve_ok" in text
    _fits(lines)


def test_a_running_instance_older_than_six_hours_is_quiet(monkeypatch):
    item = _row(last_approval_at=_ago(hours=8), approvals_24h=0)
    text = "\n".join(_plain(_frame([item], monkeypatch)))
    assert "quiet · last approval 8h ago" in text
    assert "failed" not in text
    assert "error" not in text.lower()


def test_a_recent_approval_is_not_called_quiet(monkeypatch):
    item = _row(last_approval_at=_ago(minutes=5))
    text = "\n".join(_plain(_frame([item], monkeypatch)))
    assert "quiet" not in text


def test_no_approval_yet_is_not_a_failure(monkeypatch):
    text = "\n".join(_plain(_frame([_row()], monkeypatch)))
    assert "no approvals yet" in text
    assert "failed" not in text


# --- hostile data ---------------------------------------------------------

def test_a_200_character_name_is_truncated(monkeypatch):
    item = _row(name="account-" + "z" * 200)
    lines = _frame([item], monkeypatch)
    _fits(lines)
    joined = "\n".join(_plain(lines))
    assert "…" in joined
    assert "z" * 200 not in joined


def test_control_characters_and_ansi_are_stripped(monkeypatch):
    item = _row(name="\x1b[31mred\x1b[0m\x07bad\nname")
    lines = _frame([item], monkeypatch)
    text = "\n".join(_plain(lines))
    assert "red bad name" in text
    assert "\x1b[31m" not in text
    assert "\x07" not in text


def test_wide_characters_do_not_pass_the_width(monkeypatch):
    item = _row(name="\u5de5\u4f5c\u30a2\u30ab\u30a6\u30f3\u30c8" * 12)
    lines = _frame([item], monkeypatch)
    for line in _plain(lines):
        assert _display_width(line) <= 80, repr(line)


def test_missing_and_none_rows_do_not_raise(monkeypatch):
    lines = _frame([SimpleNamespace(), None], monkeypatch)
    text = "\n".join(_plain(lines))
    assert "unnamed" in text
    assert "unknown" in text
    _fits(lines)


def test_mapping_rows_are_accepted(monkeypatch):
    rows = [{"paths": {"name": "mapped"}, "state": "running",
             "approvals_24h": 2, "pending": 0}]
    text = "\n".join(_plain(_frame(rows, monkeypatch)))
    assert "mapped" in text
    assert "2 in 24h" in text


# --- geometry -------------------------------------------------------------

def test_a_narrow_terminal_narrows_the_frame(monkeypatch):
    rows = [_row(name="work", last_approval_at=_ago(minutes=2)),
            _row(name="personal", state="stopped")]
    lines = _frame(rows, monkeypatch, columns="40")
    _fits(lines, 40)


def test_a_wide_terminal_still_stops_at_eighty(monkeypatch):
    lines = _frame([_row()], monkeypatch, columns="200")
    _fits(lines, 80)


def test_a_broken_columns_variable_is_ignored(monkeypatch):
    lines = _frame([_row()], monkeypatch, columns="wide")
    _fits(lines, 80)


def test_a_zero_column_terminal_is_not_a_blank_screen(monkeypatch):
    # `script` and some CI ptys report 0 columns; a blank frame is no use.
    lines = _frame([_row()], monkeypatch, columns="0")
    _fits(lines, 80)
    assert "work" in "\n".join(_plain(lines))


def test_a_short_window_drops_cards_with_a_count(monkeypatch):
    _env(monkeypatch)
    rows = [_row(name=f"acct-{n}") for n in range(12)]
    frame = dashboard._render(rows, 80, io.StringIO(), live=True, height=14)
    plain = ANSI.sub("", "\n".join(frame))
    assert "more" in plain
    assert len(frame) <= 14


def test_a_pipe_shows_every_account(monkeypatch):
    rows = [_row(name=f"acct-{n}") for n in range(12)]
    text = "\n".join(_plain(_frame(rows, monkeypatch)))
    assert "acct-11" in text
    assert "more" not in text


# --- failures --------------------------------------------------------------

def test_a_raising_callable_prints_one_line_and_exits_nonzero(monkeypatch):
    def broken():
        raise RuntimeError("systemctl exploded")

    _env(monkeypatch)
    buf = io.StringIO()
    # The line is for a person; the exit code is for a script. Silence plus
    # exit 0 is the false-green this project keeps having to remove.
    assert dashboard.run(broken, stream=buf) == 1
    lines = buf.getvalue().splitlines()
    text = "\n".join(_plain(lines))
    assert "RuntimeError" in text
    assert "systemctl exploded" in text
    assert "Traceback" not in text
    _fits(lines)


def test_a_statuses_read_that_explodes_is_a_line(monkeypatch):
    def broken():
        raise OSError(13, "permission denied")

    _env(monkeypatch)
    buf = io.StringIO()
    assert dashboard.run(broken, stream=buf) == 1
    assert "permission denied" in buf.getvalue()


# --- colour ---------------------------------------------------------------

def test_a_plain_pipe_gets_no_escape_bytes(monkeypatch):
    _env(monkeypatch)
    buf = io.StringIO()
    dashboard.run([_row()], stream=buf)
    assert "\033" not in buf.getvalue()


def test_force_color_paints_a_pipe_as_asked(monkeypatch):
    _env(monkeypatch, force="1", term="xterm-256color")
    buf = io.StringIO()
    dashboard.run([_row(last_approval_at=_ago(minutes=2))], stream=buf)
    assert "\033[38;5;77m" in buf.getvalue()


def test_no_color_means_a_plain_single_frame(monkeypatch):
    out = _Tty()
    _env(monkeypatch, force="1", no_color="1")
    assert dashboard.run(lambda: [_row()], refresh=0.0, stream=out) == 0
    text = out.getvalue()
    assert "\033" not in text
    assert text.count("autoduo") == 1  # exactly one frame, not a loop


def test_the_accent_is_green_on_a_colour_terminal(monkeypatch):
    _env(monkeypatch, force="1", term="xterm-256color")
    buf = io.StringIO()
    dashboard.run([_row(last_approval_at=_ago(minutes=2))], stream=buf)
    text = buf.getvalue()
    assert "\033[38;5;77m" in text          # the title and the approval
    assert "approved 2m ago" in ANSI.sub("", text)


def test_a_stopped_instance_is_not_painted_with_the_accent(monkeypatch):
    _env(monkeypatch, force="1", term="xterm-256color")
    buf = io.StringIO()
    dashboard.run([_row(state="stopped")], stream=buf)
    stopped = [line for line in buf.getvalue().splitlines()
               if "stopped" in ANSI.sub("", line)][0]
    assert "\033[38;5;77" not in stopped


def test_duo_green_prefers_256_colour(monkeypatch):
    _env(monkeypatch, force="1", term="xterm-256color")
    assert ui.duo_green("x") == "\033[38;5;77mx\033[0m"


def test_duo_green_falls_back_to_the_deeper_green(monkeypatch):
    _env(monkeypatch, force="1", term="screen")
    assert ui.duo_green("x") == "\033[38;5;71mx\033[0m"


def test_duo_green_falls_back_to_simple_green(monkeypatch):
    _env(monkeypatch, force="1", term="xterm")
    assert ui.duo_green("x") == "\033[32mx\033[0m"


def test_duo_green_can_be_bold_and_respects_no_color(monkeypatch):
    _env(monkeypatch, force="1", term="xterm-256color")
    assert ui.duo_green("x", strong=True) == "\033[38;5;77;1mx\033[0m"
    monkeypatch.setenv("NO_COLOR", "1")
    assert ui.duo_green("x") == "x"


# --- the live path ---------------------------------------------------------

def test_interactive_restores_the_screen_after_ctrl_c(monkeypatch):
    out = _Tty()
    _env(monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    calls = []

    def statuses():
        calls.append(1)
        if len(calls) > 1:
            raise KeyboardInterrupt
        return [_row()]

    assert dashboard.run(statuses, refresh=0.0, stream=out) == 0
    text = out.getvalue()
    assert text.index("\033[?1049h") < text.index("\033[?1049l")
    assert "\033[?25l" in text and "\033[?25h" in text
    assert "work" in ANSI.sub("", text)
    assert "q to quit" in ANSI.sub("", text)


def test_interactive_asks_for_the_statuses_every_frame(monkeypatch):
    out = _Tty()
    _env(monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    seen = []

    def statuses():
        seen.append(len(seen))
        if len(seen) == 4:
            raise KeyboardInterrupt
        return [_row(name=f"account-{len(seen)}")]

    assert dashboard.run(statuses, refresh=0.0, stream=out) == 0
    assert len(seen) == 4
    text = ANSI.sub("", out.getvalue())
    assert "account-1" in text and "account-3" in text


def test_interactive_keeps_the_last_good_frame_when_statuses_break(
        monkeypatch):
    out = _Tty()
    _env(monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    calls = []

    def statuses():
        calls.append(1)
        if len(calls) == 1:
            return [_row(name="steady")]
        if len(calls) == 2:
            raise RuntimeError("no systemctl")
        raise KeyboardInterrupt

    assert dashboard.run(statuses, refresh=0.0, stream=out) == 0
    text = ANSI.sub("", out.getvalue())
    assert "steady" in text
    assert "RuntimeError" in text


def test_an_odd_refresh_is_clamped_not_fatal(monkeypatch):
    _env(monkeypatch)
    buf = io.StringIO()
    for value in (None, "often", float("nan"), -3, 10_000):
        assert dashboard.run([_row()], refresh=value, stream=buf) == 0


# --- the loop that stopped without dying ----------------------------------

def test_a_running_instance_with_a_stale_log_is_called_stuck(monkeypatch):
    """systemd says active; the heartbeat says otherwise.

    A hung poll loop is indistinguishable from a healthy one unless the
    silence itself is reported, which is the whole point of the surface.
    """
    item = _row(last_event_at=_ago(minutes=40), last_approval_at=_ago(minutes=40))
    lines = _frame([item], monkeypatch)
    text = "\n".join(_plain(lines))
    assert "running" in text
    assert "no event for 40m" in text
    assert "the loop may be stuck" in text
    _fits(lines)


def test_a_stopped_instance_is_allowed_to_be_silent(monkeypatch):
    item = _row(state="stopped", last_event_at=_ago(days=3))
    text = "\n".join(_plain(_frame([item], monkeypatch)))
    assert "may be stuck" not in text


def test_a_fresh_heartbeat_is_not_a_stall(monkeypatch):
    item = _row(last_event_at=_ago(minutes=4))
    text = "\n".join(_plain(_frame([item], monkeypatch)))
    assert "may be stuck" not in text
