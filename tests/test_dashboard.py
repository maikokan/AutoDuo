"""The dashboard: one card per instance, a push feed, and a safe terminal.

Every test builds its own rows instead of importing :mod:`autoduo.instances`:
the dashboard duck-types the seam, so these tests hold it to that contract
even while the instance module is still moving.
"""
import io
import pathlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from autoduo import dashboard, ui

NOW = datetime(2026, 11, 18, 12, 0, 0)
ANSI = re.compile(r"\033\[[0-9;?]*[a-zA-Z]")


@dataclass(frozen=True)
class FakePaths:
    name: str
    vault: pathlib.Path = pathlib.Path("/tmp/autoduo/vault.enc")
    state: pathlib.Path = pathlib.Path("/tmp/autoduo/seen.json")
    lock: pathlib.Path = pathlib.Path("/tmp/autoduo/vault.lock")
    daemon_log: pathlib.Path = pathlib.Path("/tmp/autoduo/daemon.log")
    audit_log: pathlib.Path = pathlib.Path("/tmp/autoduo/audit.log")
    legacy: bool = False


@dataclass
class FakeStatus:
    paths: FakePaths
    state: str
    last_event: str | None = None
    last_event_at: str | None = None
    last_approval_at: str | None = None
    approvals_24h: int = 0
    pending: int = 0
    error: str | None = None


def status(name="alice", state="running", *, approvals=0, pending=0,
           last_approval_at=None, last_event=None, last_event_at=None,
           error=None):
    return FakeStatus(
        paths=FakePaths(name),
        state=state,
        last_event=last_event,
        last_event_at=last_event_at,
        last_approval_at=last_approval_at,
        approvals_24h=approvals,
        pending=pending,
        error=error,
    )


def iso(moment):
    return moment.isoformat()


def clean_env(monkeypatch, term="xterm-256color", force=None, no_color=None,
              colorterm=None):
    for name, value in (("NO_COLOR", no_color), ("FORCE_COLOR", force),
                        ("COLORTERM", colorterm), ("TERM", term)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def frame(rows, **kwargs):
    """Render once, plainly, at a fixed "now"."""
    buf = io.StringIO()
    text = dashboard.render(rows, stream=buf, now=NOW, **kwargs)
    assert buf.getvalue() == "", "render returns the frame; it does not print it"
    return text


def plain(text):
    return ANSI.sub("", text)


def card_line(text, name):
    """The first line of ``name``'s card (the name is bold, the line plain)."""
    for line in plain(text).splitlines():
        if line.startswith("  ") and name in line:
            return line
    raise AssertionError(f"no card line for {name!r}")


class FakeTTY(io.StringIO):
    """A buffer that claims to be a terminal, so the live path can run."""

    def isatty(self):
        return True


# --- one-shot, plain ------------------------------------------------------

def test_one_shot_is_plain_and_returns_zero(monkeypatch):
    clean_env(monkeypatch)
    buf = io.StringIO()
    assert dashboard.run(lambda: [status(approvals=3)], stream=buf) == 0
    out = buf.getvalue()
    assert "\033" not in out
    assert "alice" in out
    assert "running" in out
    assert "3 approvals today" in out
    assert out.endswith("\n")


def test_no_color_on_a_terminal_still_gets_one_plain_frame(monkeypatch):
    clean_env(monkeypatch, no_color="1")
    buf = FakeTTY()
    assert dashboard.run(lambda: [status()], stream=buf) == 0
    out = buf.getvalue()
    assert "\033" not in out
    assert "alice" in out


def test_once_on_a_terminal_has_no_cursor_codes(monkeypatch):
    clean_env(monkeypatch)
    buf = FakeTTY()
    assert dashboard.run(lambda: [status()], once=True, stream=buf) == 0
    out = buf.getvalue()
    assert "\033[?1049" not in out
    assert "\033[?25" not in out
    assert "alice" in out


# --- header ---------------------------------------------------------------

def test_totals_count_instances_running_and_approvals():
    out = frame([
        status(approvals=2),
        status("bob", "stopped"),
        status("carol", "failed", approvals=1),
    ])
    header = plain(out).splitlines()[0]
    assert "3 instances" in header
    assert "1 running" in header
    assert "3 approvals today" in header


def test_approvals_read_singular_for_one():
    assert "1 approval today" in frame([status(approvals=1)])


# --- every state ----------------------------------------------------------

@pytest.mark.parametrize("state,words", [
    ("running", "running"),
    ("stopped", "stopped"),
    ("failed", "failed"),
    ("unknown", "unknown"),
    ("no-vault", "not enrolled"),
])
def test_every_state_is_named_in_words(state, words):
    out = frame([status("alice", state=state)])
    assert words in card_line(out, "alice")


def test_no_vault_says_what_to_run():
    out = plain(frame([status("dave", "no-vault")]))
    assert "not enrolled" in out
    assert "autoduo setup" in out
    assert "pending" not in out


def test_unknown_without_an_error_explains_itself():
    out = plain(frame([status("erin", "unknown")]))
    assert "unknown" in card_line(out, "erin")
    assert "could not read its status" in out


# --- errors ---------------------------------------------------------------

def test_an_error_is_one_truncated_warning_line(monkeypatch):
    clean_env(monkeypatch, force="1", colorterm="truecolor")
    error = (
        'Traceback (most recent call last):\n'
        '  File "daemon.py", line 1, in <module>\n'
        "ValueError: vault is locked and the daemon cannot sign "
        + "x" * 500
    )
    out = frame([status("carol", "failed", error=error)], width=68)
    assert "Traceback" not in out
    assert "vault is locked" in out
    assert "\033[38;5;179m" in out   # muted amber, the warning tone
    assert "\033[31m" not in out     # never bright red
    error_line = next(line for line in plain(out).splitlines() if "error" in line)
    assert error_line.endswith("…")
    assert max(len(line) for line in plain(out).splitlines()) <= 68


def test_an_error_is_shown_even_when_the_instance_is_running():
    out = plain(frame([status(error="daemon.log: permission denied")]))
    assert "permission denied" in out
    assert "running" in card_line(out, "alice")


def test_a_row_with_no_paths_or_bad_timestamps_still_draws():
    row = status(last_approval_at="not a time", last_event_at="???")
    row.paths = None
    out = plain(frame([row]))
    assert "unnamed" in out
    assert "running" in out


# --- recency --------------------------------------------------------------

def test_a_running_instance_quiet_for_over_a_week_says_so():
    out = plain(frame([status(last_approval_at=iso(NOW - timedelta(days=9)))]))
    assert "quiet for 9 days" in out
    assert "running" in card_line(out, "alice")


def test_a_running_instance_within_a_week_is_not_quiet():
    out = plain(frame([status(last_approval_at=iso(NOW - timedelta(days=6)))]))
    assert "quiet for" not in out
    assert "6 days ago" in out


def test_a_stopped_instance_reads_as_no_approval_in_days():
    out = plain(frame([
        status("bob", "stopped", last_approval_at=iso(NOW - timedelta(days=4))),
    ]))
    assert "no approval in 4 days" in out


def test_relative_ages_stay_short():
    out = plain(frame([
        status(last_approval_at=iso(NOW - timedelta(hours=2))),
    ]))
    assert "2h ago" in out


# --- width and names ------------------------------------------------------

def test_a_long_name_is_truncated_in_the_middle():
    name = "production-laptop-for-the-night-shift-2026"
    out = frame([status(name)], width=68)
    line = card_line(out, "production")
    assert "…" in line
    assert "production" in line
    assert "2026" in line
    assert name not in plain(out)
    assert max(len(line) for line in plain(out).splitlines()) <= 68


@pytest.mark.parametrize("columns", [30, 40, 59, 60, 67, 68])
def test_no_line_passes_the_width(columns):
    rows = [
        status("a-very-long-instance-name-for-the-night-shift",
               approvals=1234, pending=2,
               last_approval_at=iso(NOW - timedelta(minutes=5)),
               last_event="heartbeat", last_event_at=iso(NOW)),
        status("bob", "stopped",
               last_approval_at=iso(NOW - timedelta(days=12))),
        status("carol", "failed", pending=1,
               error="vault is locked; run autoduo setup"),
    ]
    out = frame(rows, width=columns)
    assert max(len(line) for line in plain(out).splitlines()) <= columns


def test_colour_never_changes_the_measured_width(monkeypatch):
    clean_env(monkeypatch, force="1", colorterm="truecolor")
    rows = [status("a-very-long-instance-name", approvals=12,
                   last_approval_at=iso(NOW - timedelta(minutes=2)),
                   pending=1, last_event="heartbeat",
                   last_event_at=iso(NOW))]
    out = frame(rows, width=68)
    assert "\033[" in out
    assert max(len(line) for line in plain(out).splitlines()) <= 68


def test_under_sixty_columns_the_card_stacks():
    out = plain(frame([status(approvals=3,
                              last_approval_at=iso(NOW - timedelta(hours=2)),
                              last_event="heartbeat",
                              last_event_at=iso(NOW))], width=48))
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if "alice" in line)
    assert lines[start].strip() == "alice"
    assert lines[start + 1].strip().startswith("running")
    assert "3 approvals today" in lines[start + 1]
    assert "last approval" in out
    assert max(len(line) for line in lines) <= 48


def test_the_state_column_lines_up():
    out = plain(frame([
        status("alice", "running", approvals=1),
        status("bob", "stopped"),
        status("carol", "failed"),
    ], width=68))
    positions = []
    for name, word in (("alice", "running"), ("bob", "stopped"), ("carol", "failed")):
        line = next(l for l in out.splitlines() if l.startswith("  ") and name in l)
        positions.append(line.index(word))
    assert len(set(positions)) == 1


# --- empty state ----------------------------------------------------------

def test_zero_instances_is_a_helpful_empty_screen():
    out = plain(frame([]))
    assert "autoduo" in out
    assert "no instances yet" in out
    assert "No instances yet." in out
    assert "autoduo setup" in out
    assert out.strip() != "autoduo"


# --- the feed -------------------------------------------------------------

def test_the_feed_is_newest_first_and_capped():
    rows = [
        status(f"i{index}", approvals=1,
               last_approval_at=iso(NOW - timedelta(minutes=10 * index)))
        for index in range(10)
    ]
    out = plain(frame(rows, width=68))
    feed = out.split("recent approvals", 1)[1]
    lines = [line for line in feed.splitlines() if "approved" in line]
    assert len(lines) == dashboard.FEED_LIMIT
    for index in range(dashboard.FEED_LIMIT):
        assert f"i{index}" in lines[index]
    assert "i8" not in feed and "i9" not in feed
    assert lines[0].strip().startswith("12:00")


def test_the_feed_invents_nothing_without_approvals():
    out = plain(frame([status()]))
    assert "recent approvals" in out
    assert "no approvals in the last 24 hours" in out
    assert "approved" not in out


def test_the_feed_ignores_approvals_older_than_a_day():
    out = plain(frame([status(last_approval_at=iso(NOW - timedelta(days=3)))]))
    assert "no approvals in the last 24 hours" in out


# --- terminal behaviour ---------------------------------------------------

def test_the_first_provider_failure_returns_one_without_a_traceback():
    def boom():
        raise RuntimeError("systemctl is missing")

    buf = io.StringIO()
    assert dashboard.run(boom, stream=buf) == 1
    out = buf.getvalue()
    assert "Traceback" not in out
    assert "systemctl is missing" in out
    assert "\033" not in out


def test_a_later_failure_keeps_the_last_rows_and_says_so(monkeypatch):
    clean_env(monkeypatch)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            return [status(approvals=2)]
        if len(calls) == 2:
            raise RuntimeError("status check blew up")
        raise KeyboardInterrupt

    buf = FakeTTY()
    assert dashboard.run(flaky, refresh=0, stream=buf) == 0
    out = plain(buf.getvalue())
    assert "status check failed" in out
    assert "status check blew up" in out
    assert out.count("alice") >= 2


def test_the_alt_screen_is_restored_on_keyboard_interrupt(monkeypatch):
    clean_env(monkeypatch)
    calls = []

    def provider():
        calls.append(1)
        if len(calls) == 1:
            return [status(approvals=1)]
        raise KeyboardInterrupt

    buf = FakeTTY()
    assert dashboard.run(provider, refresh=0, stream=buf) == 0
    out = buf.getvalue()
    assert out.startswith("\033[?1049h\033[?25l")
    assert out.endswith("\033[?25h\033[?1049l")


def test_the_alt_screen_is_restored_when_rendering_raises(monkeypatch):
    clean_env(monkeypatch)
    real = dashboard.render
    calls = {"count": 0}

    def broken(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise ValueError("frame failed")
        return real(*args, **kwargs)

    monkeypatch.setattr(dashboard, "render", broken)
    buf = FakeTTY()
    with pytest.raises(ValueError):
        dashboard.run(lambda: [status()], refresh=0, stream=buf)
    assert buf.getvalue().endswith("\033[?25h\033[?1049l")


# --- palette --------------------------------------------------------------

def test_forced_colour_uses_duos_green(monkeypatch):
    clean_env(monkeypatch, force="1", colorterm="truecolor")
    out = frame([status(approvals=1)])
    assert "\033[38;5;77" in out


def test_brand_green_falls_back_with_the_terminal(monkeypatch):
    clean_env(monkeypatch, term="screen")
    assert ui.duo_green_code() == "38;5;71"
    clean_env(monkeypatch, term="xterm")
    assert ui.duo_green_code() == "32"


def test_state_words_are_plain_without_colour(monkeypatch):
    clean_env(monkeypatch)
    buf = io.StringIO()
    assert ui.state_word("no-vault", buf) == "not enrolled"
    assert "\033" not in buf.getvalue()
    assert ui.state_label("stopped") == "stopped"
