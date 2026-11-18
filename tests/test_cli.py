"""Tests for autoduo.cli helpers (disclaimer, retry, validators) and ui."""
import builtins
import io
import pathlib
import subprocess
import sys
import types

import pytest

import autoduo
from autoduo import cli, instances, ui


def test_validate_url_accepts_m_activate(monkeypatch):
    err = cli._validate_url("https://m-X.duosecurity.com/activate/CODE")
    assert err is None


def test_validate_url_accepts_m_android(monkeypatch):
    err = cli._validate_url("https://m-X.duosecurity.com/android/CODE")
    assert err is None


def test_validate_url_accepts_api_push(monkeypatch):
    err = cli._validate_url("https://api-X.duosecurity.com/push/v2/activation/CODE")
    assert err is None


def test_validate_url_rejects_evil(monkeypatch):
    err = cli._validate_url("https://evil.example.com/activate/X")
    assert err is not None


def test_validate_url_rejects_wrong_path(monkeypatch):
    err = cli._validate_url("https://m-X.duosecurity.com/wrong/CODE")
    assert err is not None


# --- _prompt_with_retry ---------------------------------------------------

def test_prompt_with_retry_accepts_valid(monkeypatch):
    inputs = iter(["", "", "good"])
    monkeypatch.setattr(builtins, "input", lambda _: next(inputs))
    out = cli._prompt_with_retry("? ")
    assert out == "good"


def test_prompt_with_retry_returns_none_on_eof(monkeypatch):
    def raise_eof(_):
        raise EOFError
    monkeypatch.setattr(builtins, "input", raise_eof)
    assert cli._prompt_with_retry("? ") is None


def test_prompt_with_retry_gives_up_after_max(monkeypatch):
    inputs = iter([""] * 10)  # always empty
    monkeypatch.setattr(builtins, "input", lambda _: next(inputs))
    assert cli._prompt_with_retry("? ") is None


def test_prompt_with_retry_uses_validator(monkeypatch):
    inputs = iter(["bad", "still bad", "good"])
    monkeypatch.setattr(builtins, "input", lambda _: next(inputs))
    def validator(s):
        return None if s == "good" else "nope"
    out = cli._prompt_with_retry("? ", validator=validator)
    assert out == "good"


# --- _confirm -------------------------------------------------------------

def test_confirm_yes(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda _: "y")
    assert cli._confirm("? ") is True


def test_confirm_no(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda _: "n")
    assert cli._confirm("? ") is False


def test_confirm_empty_defaults_no(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda _: "")
    assert cli._confirm("? ") is False


def test_confirm_eof_defaults_no(monkeypatch):
    def raise_eof(_):
        raise EOFError
    monkeypatch.setattr(builtins, "input", raise_eof)
    assert cli._confirm("? ") is False

# --- ui: capability detection --------------------------------------------

class _Tty(io.StringIO):
    """A StringIO that claims to be a terminal."""

    def isatty(self):
        return True


def _clean_env(monkeypatch, term="xterm-256color", force=None, no_color=None):
    """Set the colour-related environment deterministically for one test."""
    for name, value in (("NO_COLOR", no_color), ("FORCE_COLOR", force),
                        ("COLORTERM", None), ("TERM", term)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def _feed(monkeypatch, answers):
    it = iter(answers)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": next(it))


def test_colour_off_when_piped(monkeypatch):
    _clean_env(monkeypatch)
    assert ui.color_enabled(stream=io.StringIO()) is False


def test_colour_on_when_tty(monkeypatch):
    _clean_env(monkeypatch)
    assert ui.color_enabled(stream=_Tty()) is True


def test_no_color_wins_on_a_tty(monkeypatch):
    _clean_env(monkeypatch, no_color="1")
    assert ui.color_enabled(stream=_Tty()) is False


def test_force_color_wins_over_a_pipe(monkeypatch):
    _clean_env(monkeypatch, force="1")
    assert ui.color_enabled(stream=io.StringIO()) is True


def test_no_color_beats_force_color(monkeypatch):
    _clean_env(monkeypatch, force="1", no_color="1")
    assert ui.color_enabled(stream=_Tty()) is False


def test_dumb_terminal_is_plain(monkeypatch):
    _clean_env(monkeypatch, term="dumb")
    assert ui.color_enabled(stream=_Tty()) is False


def test_missing_isatty_is_plain(monkeypatch):
    _clean_env(monkeypatch)

    class Opaque:
        pass

    assert ui.color_enabled(stream=Opaque()) is False


# --- ui: primitives -------------------------------------------------------

def test_primitives_have_a_plain_fallback(monkeypatch):
    _clean_env(monkeypatch)
    buf = io.StringIO()
    ui.banner("autoduo setup", "subtitle", stream=buf)
    ui.heading("Heading", stream=buf)
    ui.step(1, "Paste your activation URL", "a detail line", stream=buf)
    ui.ok("done", stream=buf)
    ui.warn("careful", stream=buf)
    ui.error("broken", stream=buf)
    ui.note("an aside", stream=buf)
    ui.kv("key", "value", stream=buf)
    ui.rule(stream=buf)
    ui.bullet("an item", stream=buf)
    out = buf.getvalue()
    assert "\033" not in out
    for word in ("autoduo setup", "subtitle", "Heading", "1.", "Paste your activation URL",
                 "a detail line", "ok", "done", "warn", "careful", "error", "broken",
                 "an aside", "key", "value", "─", "an item"):
        assert word in out, word


def test_primitives_use_the_muted_palette_on_a_tty(monkeypatch):
    _clean_env(monkeypatch)
    buf = _Tty()
    ui.banner("autoduo setup", stream=buf)
    ui.ok("done", stream=buf)
    ui.warn("careful", stream=buf)
    ui.error("broken", stream=buf)
    out = buf.getvalue()
    assert "\033[38;5;77;1m" in out   # Duo green, bold, in one sequence
    assert "\033[38;5;108m" in out    # soft green
    assert "\033[38;5;179m" in out    # muted amber
    assert "\033[38;5;174m" in out    # muted red
    assert "\033[0m" in out
    assert "\033[38;5;77m" not in out   # no nested escapes


def test_basic_colour_when_the_terminal_lacks_256(monkeypatch):
    _clean_env(monkeypatch, term="xterm")
    buf = _Tty()
    ui.ok("done", stream=buf)
    out = buf.getvalue()
    assert "\033[32m" in out
    assert "38;5" not in out


# --- The disclaimer -------------------------------------------------------

POINTS = (
    "defeats the purpose of two-factor authentication",
    "shell access",
    "2FA stops protecting this account",
    "Verified Push",
    "Duo may flag the account and disable it",
    "solely responsible for securing this host",
    "SSH keys only, no shared users, encrypted disk",
    "accept no liability",
    "provided as is, with no warranty of any kind",
    "DUO_BOT_SKIP_DISCLAIMER=1",
    "I ACCEPT",
)


def _disclaimer_output(monkeypatch, capsys, answers=("I ACCEPT",)):
    monkeypatch.delenv("DUO_BOT_SKIP_DISCLAIMER", raising=False)
    _clean_env(monkeypatch)
    _feed(monkeypatch, list(answers))
    accepted = cli._show_disclaimer_and_get_acceptance()
    return accepted, capsys.readouterr()


def test_disclaimer_accepts_exact_words(monkeypatch, capsys):
    accepted, _ = _disclaimer_output(monkeypatch, capsys)
    assert accepted is True


def test_disclaimer_keeps_every_point(monkeypatch, capsys):
    _, captured = _disclaimer_output(monkeypatch, capsys)
    flat = " ".join(captured.out.split())
    for point in POINTS:
        assert point in flat, point


def test_disclaimer_is_plain_and_fits_eighty_columns(monkeypatch, capsys):
    _, captured = _disclaimer_output(monkeypatch, capsys)
    out = captured.out.rstrip("\n")
    assert "\033" not in out
    assert max(len(line) for line in out.splitlines()) <= 80
    assert "\n\n\n" not in out  # one blank line between blocks, never two


def test_disclaimer_retries_three_times_then_declines(monkeypatch, capsys):
    accepted, captured = _disclaimer_output(
        monkeypatch, capsys, answers=("i accept", "I ACCEPTED", " yes "))
    assert accepted is False
    assert "exactly I ACCEPT" in captured.out
    assert "Too many invalid answers" in captured.err


def test_disclaimer_accepts_on_the_third_try(monkeypatch, capsys):
    accepted, _ = _disclaimer_output(monkeypatch, capsys, answers=("", "nope", "I ACCEPT"))
    assert accepted is True


def test_disclaimer_survives_eof(monkeypatch, capsys):
    monkeypatch.delenv("DUO_BOT_SKIP_DISCLAIMER", raising=False)
    _clean_env(monkeypatch)

    def boom(_prompt=""):
        raise EOFError

    monkeypatch.setattr(builtins, "input", boom)
    assert cli._show_disclaimer_and_get_acceptance() is False
    assert "No answer given" in capsys.readouterr().err


def test_disclaimer_skip_variable_does_not_prompt(monkeypatch):
    monkeypatch.setenv("DUO_BOT_SKIP_DISCLAIMER", "1")

    def refuse(_prompt=""):
        raise AssertionError("must not prompt when the skip variable is set")

    monkeypatch.setattr(builtins, "input", refuse)
    assert cli._show_disclaimer_and_get_acceptance() is True


# --- usage screen ---------------------------------------------------------

def test_main_without_a_command_prints_help_and_exits_one(monkeypatch, capsys):
    _clean_env(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["autoduo"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "\033" not in out
    assert "Commands" in out
    for name in cli.COMMANDS:
        assert name in out
    assert max(len(line) for line in out.splitlines()) <= 80


# --- secret handling ------------------------------------------------------

def test_secret_tempfile_is_private():
    path = cli._secret_tempfile()
    try:
        assert path.exists()
        assert path.name.startswith("autoduo-")
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        path.unlink(missing_ok=True)


# --- prompt plumbing ------------------------------------------------------

def test_read_ends_the_line_when_stdin_is_piped(monkeypatch, capsys):
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "y")
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    assert cli._read("  Question? ") == "y"
    assert capsys.readouterr().out == "\n"


def test_read_leaves_the_line_alone_on_a_tty(monkeypatch, capsys):
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "y")
    monkeypatch.setattr(sys, "stdin", _Tty())
    assert cli._read("  Question? ") == "y"
    assert capsys.readouterr().out == ""


# ──────────────────────────────────────────────────────────────────────
# Instances
# ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_instances(tmp_path, monkeypatch):
    """A throwaway root for both instance data and logs."""
    data = tmp_path / "lib"
    logs = tmp_path / "log"
    monkeypatch.setattr(instances, "DATA_ROOT", data)
    monkeypatch.setattr(instances, "LOG_ROOT", logs)
    instances.clear_state_cache()
    yield data, logs
    instances.clear_state_cache()


def _fake_subprocess(monkeypatch, stdout="inactive\n"):
    """Answer systemctl and swallow every other command."""
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["systemctl", "is-active"]:
            return subprocess.CompletedProcess(cmd, 0, stdout, "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def _dispatch(monkeypatch, argv):
    """Run main() against a fake status command and report its instance."""
    seen = {}

    def fake():
        seen["name"] = cli._paths().name
        return 0

    monkeypatch.setitem(cli.COMMANDS, "status", fake)
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    return exc.value.code, seen


def test_instance_option_before_the_command(monkeypatch):
    code, seen = _dispatch(monkeypatch, ["autoduo", "--instance", "alice", "status"])
    assert code == 0
    assert seen["name"] == "alice"


def test_instance_option_after_the_command(monkeypatch):
    code, seen = _dispatch(monkeypatch, ["autoduo", "status", "--instance", "alice"])
    assert code == 0
    assert seen["name"] == "alice"


def test_instance_option_equals_form(monkeypatch):
    code, seen = _dispatch(monkeypatch, ["autoduo", "status", "--instance=alice"])
    assert code == 0
    assert seen["name"] == "alice"


def test_instance_defaults_to_default(monkeypatch):
    code, seen = _dispatch(monkeypatch, ["autoduo", "status"])
    assert code == 0
    assert seen["name"] == "default"
    assert cli._INSTANCE_EXPLICIT is False


def test_explicit_instance_is_remembered_for_watch(monkeypatch):
    _dispatch(monkeypatch, ["autoduo", "--instance", "alice", "status"])
    assert cli._INSTANCE_EXPLICIT is True


def test_instance_path_traversal_is_refused(monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv", ["autoduo", "--instance", "../../etc", "status"]
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert "invalid instance name" in capsys.readouterr().err


def test_instance_without_a_name_is_refused(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["autoduo", "status", "--instance"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert "needs a name" in capsys.readouterr().err


# --- unit dispatch -------------------------------------------------------

def test_start_uses_the_instance_template(monkeypatch):
    calls = _fake_subprocess(monkeypatch)
    monkeypatch.setattr(cli, "_ensure_root", lambda: None)
    monkeypatch.setattr(cli, "_ACTIVE_INSTANCE", instances.resolve("alice"))

    cli.cmd_start()
    assert ["systemctl", "start", "autoduo@alice"] in calls


def test_start_for_default_uses_the_plain_unit(monkeypatch):
    calls = _fake_subprocess(monkeypatch)
    monkeypatch.setattr(cli, "_ensure_root", lambda: None)
    monkeypatch.setattr(cli, "_ACTIVE_INSTANCE", instances.resolve("default"))

    cli.cmd_start()
    assert ["systemctl", "start", "autoduo"] in calls


def test_printed_commands_carry_the_instance_flag(monkeypatch):
    monkeypatch.setattr(cli, "_ACTIVE_INSTANCE", instances.resolve("alice"))
    assert cli._instance_flag() == " --instance alice"
    monkeypatch.setattr(cli, "_ACTIVE_INSTANCE", instances.resolve("default"))
    assert cli._instance_flag() == ""


# --- enroll --------------------------------------------------------------

def test_enroll_writes_the_selected_instance_vault(temp_instances, monkeypatch):
    monkeypatch.setattr(cli, "_ensure_root", lambda: None)
    monkeypatch.setenv("DUO_BOT_SKIP_DISCLAIMER", "1")
    alice = instances.resolve("alice")
    monkeypatch.setattr(cli, "_ACTIVE_INSTANCE", alice)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    _feed(monkeypatch, ["https://m-X.duosecurity.com/activate/CODE", "y"])

    assert cli.cmd_enroll() == 0
    enroll_cmd = calls[-1]
    assert enroll_cmd[enroll_cmd.index("--vault") + 1] == str(alice.vault)
    assert str(alice.vault) != str(instances.resolve("default").vault)
    assert alice.vault.parent.is_dir()


# --- instances listing ---------------------------------------------------

def test_instances_lists_every_instance(temp_instances, monkeypatch, capsys):
    _fake_subprocess(monkeypatch, stdout="active\n")
    for name in ("alice", "bob"):
        paths = instances.resolve(name)
        instances.ensure_dirs(paths)
        paths.vault.write_text("sealed")

    assert cli.cmd_instances() == 0
    out = capsys.readouterr().out
    for word in ("NAME", "STATE", "LAST APPROVAL", "24H", "PEND",
                 "default", "alice", "bob"):
        assert word in out, word
    assert max(len(line) for line in out.splitlines()) <= 80


# --- watch ---------------------------------------------------------------

def test_watch_without_the_dashboard_module(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "autoduo.dashboard", None)
    monkeypatch.delattr(autoduo, "dashboard", raising=False)

    assert cli.cmd_watch() == 1
    assert "dashboard" in capsys.readouterr().err


def test_watch_hands_a_callable_to_the_dashboard(temp_instances, monkeypatch):
    _fake_subprocess(monkeypatch)
    captured = {}

    def run(statuses, refresh=1.0):
        captured["statuses"] = statuses
        captured["refresh"] = refresh
        return 0

    module = types.ModuleType("autoduo.dashboard")
    module.run = run
    monkeypatch.setitem(sys.modules, "autoduo.dashboard", module)
    monkeypatch.setattr(autoduo, "dashboard", module, raising=False)

    assert cli.cmd_watch() == 0
    assert captured["refresh"] == 1.0
    assert callable(captured["statuses"])  # not a frozen snapshot
    rows = captured["statuses"]()
    assert rows
    assert all(isinstance(row, instances.InstanceStatus) for row in rows)


def _watch_rows(temp_instances, monkeypatch, explicit):
    _fake_subprocess(monkeypatch)
    captured = {}

    def run(statuses, refresh=1.0):
        captured["rows"] = statuses()
        return 0

    module = types.ModuleType("autoduo.dashboard")
    module.run = run
    monkeypatch.setitem(sys.modules, "autoduo.dashboard", module)
    monkeypatch.setattr(autoduo, "dashboard", module, raising=False)
    monkeypatch.setattr(cli, "_INSTANCE_EXPLICIT", explicit)

    assert cli.cmd_watch() == 0
    return [row.paths.name for row in captured["rows"]]


def test_watch_shows_every_instance_by_default(temp_instances, monkeypatch):
    monkeypatch.setattr(cli, "_ACTIVE_INSTANCE", instances.resolve("default"))
    instances.ensure_dirs(instances.resolve("alice"))

    names = _watch_rows(temp_instances, monkeypatch, explicit=False)
    assert names == ["default", "alice"]


def test_watch_narrows_to_an_explicit_instance(temp_instances, monkeypatch):
    monkeypatch.setattr(cli, "_ACTIVE_INSTANCE", instances.resolve("alice"))
    instances.ensure_dirs(instances.resolve("alice"))
    instances.ensure_dirs(instances.resolve("default"))

    names = _watch_rows(temp_instances, monkeypatch, explicit=True)
    assert names == ["alice"]


# --- uninstall scoping ---------------------------------------------------

def _seed(paths):
    instances.ensure_dirs(paths)
    paths.vault.write_text("vault of " + paths.name)
    paths.state.write_text("{}")
    paths.daemon_log.write_text("{}\n")


def test_uninstall_removes_only_the_selected_instance(temp_instances, monkeypatch):
    data, _logs = temp_instances
    default = instances.resolve("default")
    alice = instances.resolve("alice")
    _seed(default)
    _seed(alice)

    _fake_subprocess(monkeypatch)
    monkeypatch.setattr(cli, "_ensure_root", lambda: None)
    monkeypatch.setattr(cli, "_ACTIVE_INSTANCE", alice)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "alice")

    assert cli.cmd_uninstall() == 0

    assert not alice.vault.parent.exists()
    assert not alice.daemon_log.parent.exists()
    assert (data / "instances").is_dir()  # the container survives
    assert default.vault.read_text() == "vault of default"
    assert default.state.exists()
    assert default.daemon_log.exists()


def test_uninstall_requires_the_instance_name(temp_instances, monkeypatch):
    alice = instances.resolve("alice")
    _seed(alice)

    _fake_subprocess(monkeypatch)
    monkeypatch.setattr(cli, "_ensure_root", lambda: None)
    monkeypatch.setattr(cli, "_ACTIVE_INSTANCE", alice)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "bob")

    assert cli.cmd_uninstall() == 1
    assert alice.vault.exists()
    assert alice.daemon_log.exists()


def test_uninstall_all_requires_typing_ALL(temp_instances, monkeypatch):
    _fake_subprocess(monkeypatch)
    monkeypatch.setattr(cli, "_ensure_root", lambda: None)
    monkeypatch.setattr(cli, "_OPTS", {"all": True})
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "all")
    alice = instances.resolve("alice")
    _seed(alice)

    assert cli.cmd_uninstall() == 1
    assert alice.vault.exists()


def test_uninstall_default_keeps_named_instances(tmp_path, temp_instances, monkeypatch):
    data, _logs = temp_instances
    default = instances.resolve("default")
    alice = instances.resolve("alice")
    _seed(default)
    _seed(alice)
    # Never touch the host's real unit file from a test.
    monkeypatch.setattr(cli, "UNIT_PATH", tmp_path / "autoduo.service")
    _fake_subprocess(monkeypatch)
    monkeypatch.setattr(cli, "_ensure_root", lambda: None)
    monkeypatch.setattr(cli, "_ACTIVE_INSTANCE", default)

    assert cli.cmd_uninstall() == 0

    assert not default.vault.exists()
    assert not default.daemon_log.exists()
    assert (data / "instances").is_dir()
    assert alice.vault.read_text() == "vault of alice"
    assert alice.daemon_log.exists()


def test_status_of_an_unenrolled_instance_fails_loudly(temp_instances, monkeypatch):
    """A missing vault is not a stopped service.

    Reporting success for an instance that was never enrolled is the same
    false-green the daemon itself had to be cured of.
    """
    _fake_subprocess(monkeypatch)
    monkeypatch.setattr(cli, "_ACTIVE_INSTANCE", instances.resolve("alice"))

    assert cli.cmd_status() == 1
