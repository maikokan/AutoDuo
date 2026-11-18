"""autoduo CLI: interactive setup and management commands.

Every line a person reads comes from :mod:`autoduo.ui`, so the same
wording works on a TTY (colour, dim metadata) and in a pipe (plain
text, no escape sequences).

Usage:
    autoduo setup      — interactive: disclaimer, paste link, enroll, start
    autoduo install    — install systemd service (no disclaimer)
    autoduo uninstall  — remove everything
    autoduo start      — start the daemon
    autoduo stop       — stop the daemon
    autoduo restart    — restart the daemon
    autoduo status     — show daemon status
    autoduo logs       — tail daemon logs
    autoduo version    — print version
    autoduo enroll     — one-shot enroll (no daemon start)
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

from autoduo import ui

REPO_DIR = pathlib.Path(__file__).resolve().parent.parent
VAULT_DIR = pathlib.Path("/var/lib/autoduo")
LOG_DIR = pathlib.Path("/var/log/autoduo")
VAULT_PATH = VAULT_DIR / "vault.enc"
UNIT_PATH = pathlib.Path("/etc/systemd/system/autoduo.service")
MAX_RETRIES = 3

DUO_ADMIN_HINT = "Duo Admin → Users → Add Device → Tablet → Android"


def _run(cmd: list[str], check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    ui.kv("run", " ".join(cmd))
    return subprocess.run(cmd, check=check, capture_output=capture, text=capture)


def _ensure_root():
    if os.geteuid() != 0:
        ui.error("This needs root. Try: sudo autoduo ...")
        sys.exit(1)


def _venv_python() -> str:
    return str(REPO_DIR / ".venv" / "bin" / "python")


def _secret_tempfile(suffix: str = ".txt") -> pathlib.Path:
    """Create a private temp file for a one-shot secret.

    mkstemp creates the file O_EXCL with mode 0600 from the first byte, so
    there is no window where the secret is readable by another user and no
    pre-planted symlink can be followed (bandit B108).
    """
    fd, name = tempfile.mkstemp(prefix="autoduo-", suffix=suffix)
    os.close(fd)
    return pathlib.Path(name)


def _service_active() -> bool:
    r = subprocess.run(
        ["systemctl", "is-active", "autoduo"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() == "active"


def _read(prompt: str) -> str:
    """input(), plus the newline that a piped stdin never echoes back."""
    answer = input(prompt)
    try:
        visible = sys.stdin.isatty()
    except (AttributeError, ValueError):
        visible = True
    if not visible:
        print()
    return answer


# ──────────────────────────────────────────────────────────────────────
# Disclaimer
# ──────────────────────────────────────────────────────────────────────

RISKS = (
    "anyone with shell access here can approve any push",
    "2FA stops protecting this account",
    "if Verified Push is enforced, logins will still fail",
    "Duo may flag the account and disable it; that risk is yours",
    "you are solely responsible for securing this host: SSH keys only, "
    "no shared users, encrypted disk",
    "the authors accept no liability for any damage, account loss or breach",
    "this software is provided as is, with no warranty of any kind",
)


def _render_disclaimer() -> None:
    """The risk, in full, before anything on this host is changed."""
    ui.banner("autoduo setup")
    ui.para("Before we start, the risk.")
    ui.line()
    ui.para(
        "This program approves Duo pushes for you, without your phone. "
        "It defeats the purpose of two-factor authentication: anything "
        "that can run on this host can approve a login as you."
    )
    ui.line()
    ui.heading("What that means")
    ui.line()
    for risk in RISKS:
        ui.bullet(risk)
    ui.line()
    ui.para("Run this only on a host you control.")
    ui.line()
    ui.para(
        "Set DUO_BOT_SKIP_DISCLAIMER=1 to skip this in scripted installs. "
        "This is the only way to skip it."
    )
    ui.line()
    ui.para("Type  I ACCEPT  to continue, or Ctrl+C to stop.")
    ui.line()


def _validate_url(url: str) -> str | None:
    """Validate a Duo activation URL. Returns None if valid, error string if not."""
    if "duosecurity.com" not in url:
        return f"doesn't look like a Duo URL: {url}"
    if not (url.startswith("https://m-") and ("/activate/" in url or "/android/" in url)) \
       and not (url.startswith("https://api-") and "/push/v2/" in url):
        return "expected https://m-...duosecurity.com/activate/CODE, /android/CODE, or api-.../push/v2/activation/CODE"
    return None


def _show_disclaimer_and_get_acceptance() -> bool:
    """Show the disclaimer and require explicit acceptance.

    Returns True if accepted, False if declined (or after 3 bad tries).
    """
    if os.environ.get("DUO_BOT_SKIP_DISCLAIMER") == "1":
        ui.note("Disclaimer skipped (DUO_BOT_SKIP_DISCLAIMER=1).")
        return True

    _render_disclaimer()
    for _attempt in range(MAX_RETRIES):
        try:
            resp = _read("  > ").strip()
        except EOFError:
            ui.warn("No answer given. Nothing was changed.")
            return False
        if resp == "I ACCEPT":
            return True
        if resp == "":
            ui.note("Nothing entered — try again.")
        else:
            ui.note(f"Got {resp!r}. The answer must be exactly I ACCEPT.")
    ui.error("Too many invalid answers. Nothing was changed.")
    return False


# ──────────────────────────────────────────────────────────────────────
# Input helpers with retry
# ──────────────────────────────────────────────────────────────────────

def _prompt_with_retry(prompt: str, validator=None) -> str | None:
    """Prompt with retry on empty/invalid input.

    Returns the input string, or None if MAX_RETRIES exceeded.
    validator: optional callable(str) -> str | None. If it returns an
    error string the user re-prompts.
    """
    for _attempt in range(MAX_RETRIES):
        try:
            resp = _read(prompt).strip()
        except EOFError:
            return None
        if not resp:
            ui.note("Nothing entered — try again.")
            continue
        if validator is not None:
            err = validator(resp)
            if err is not None:
                ui.note(err)
                continue
        return resp
    ui.note("Too many invalid attempts.")
    return None


def _confirm(prompt: str) -> bool:
    """Y/N confirmation. Defaults to No on empty/invalid."""
    try:
        resp = _read(prompt).strip().lower()
    except EOFError:
        return False
    return resp in ("y", "yes")


# ──────────────────────────────────────────────────────────────────────
# Commands
# ──────────────────────────────────────────────────────────────────────

def cmd_setup():
    """Interactive setup: paste activation link, enroll, start."""
    _ensure_root()

    if not _show_disclaimer_and_get_acceptance():
        return 1

    if not (REPO_DIR / ".venv").exists():
        ui.line()
        ui.heading("First run, so the dependencies come first")
        _install()
        ui.line()

    ui.step(1, "Paste your activation URL", DUO_ADMIN_HINT)
    url = _prompt_with_retry("  Activation URL: ", validator=_validate_url)
    if url is None:
        return 1

    if not _confirm("  Was this URL just generated for you in Duo Admin? (y/N) "):
        ui.note("Nothing was changed.")
        return 1

    # The secret goes to a private 0600 file, never to argv.
    act_file = _secret_tempfile()
    act_file.write_text(url)

    if VAULT_PATH.exists():
        ui.line()
        ui.kv("vault", str(VAULT_PATH))
        if not _confirm("  Re-enroll and orphan the existing device? (y/N) "):
            act_file.unlink(missing_ok=True)
            ui.note("Skipping enrollment. The vault is unchanged.")
            cmd_start()
            return _push_test_gate()

    ui.line()
    ui.step(2, "Enroll this device", "Generating an RSA keypair and calling Duo.")
    rc = subprocess.run(
        [_venv_python(), "-m", "autoduo.enroll",
         "--vault", str(VAULT_PATH),
         "--activation-file", str(act_file),
         "--no-passphrase"],
        cwd=str(REPO_DIR),
    )
    act_file.unlink(missing_ok=True)

    if rc.returncode != 0:
        ui.line()
        ui.error("Enrollment failed. The output above says why.")
        if _confirm("  Try again with a new activation URL? (y/N) "):
            return cmd_setup()
        return 1

    ui.line()
    ui.step(3, "Approve the new device on your phone",
            "Duo Mobile asks once, now. Tap Approve. This is the only "
            "time you need to touch it.")
    if not _confirm("  Did you approve it on your phone? (y/N) "):
        ui.note("Approve on your phone, then run: autoduo start")
        return 1

    ui.line()
    ui.step(4, "Start the daemon")
    cmd_start()

    return _push_test_gate()


def _push_test_gate() -> int:
    """After daemon start, confirm end-to-end push handling works.

    Asks the user to trigger a push on any service protected by their
    Duo account, then verifies the daemon received and approved it.
    """
    ui.line()
    ui.heading("Verify a real push")
    ui.line()
    ui.para(
        "Trigger a login on any service protected by your Duo account: "
        "VPN, SSO, wherever Duo is enabled. The daemon should approve it "
        "without you touching your phone."
    )
    ui.line()
    if not _confirm("  Did the login succeed, without you tapping the phone? (y/N) "):
        ui.line()
        ui.heading("If it did not work")
        ui.line()
        ui.step(1, "Approve the new-device prompt",
                "If you have not approved it on your phone yet, do that first.")
        ui.step(2, "Check for Verified Push",
                "If the service enforces it, auto-approval is impossible: "
                "the login wants a code typed from the browser into the "
                "phone.")
        ui.step(3, "Read the log",
                "autoduo logs shows what the daemon saw and decided.")
        ui.line()
        return 1

    ui.line()
    ui.ok("Setup verified. The daemon is auto-approving pushes.")
    ui.line()
    ui.heading("Useful commands")
    ui.line()
    for name, desc in (
        ("autoduo status", "is the daemon running"),
        ("autoduo logs", "live log tail"),
        ("autoduo uninstall", "remove everything"),
    ):
        ui.line(f"    {ui.bold(f'{name:<18}')}  {desc}")
    ui.line()
    return 0


def _install():
    """The install work itself, with no banner, so setup can call it."""
    ui.heading("Installing OS packages")
    _run(["apt-get", "update", "-qq"], check=False, capture=True)
    _run(["apt-get", "install", "-y", "--no-install-recommends",
          "python3", "python3-venv", "python3-pip", "systemd-timesyncd"])

    ui.heading("Enabling time sync")
    _run(["systemctl", "enable", "--now", "systemd-timesyncd"], check=False)

    ui.heading("Creating directories")
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    ui.heading("Creating the Python environment")
    if not (REPO_DIR / ".venv").exists():
        _run([sys.executable, "-m", "venv", str(REPO_DIR / ".venv")])
    _run([str(REPO_DIR / ".venv" / "bin" / "pip"), "install", "--quiet", "--upgrade", "pip"])
    _run([str(REPO_DIR / ".venv" / "bin" / "pip"), "install", "--quiet",
          "-r", str(REPO_DIR / "requirements.txt")])

    ui.heading("Installing the systemd unit")
    shutil.copy2(REPO_DIR / "systemd" / "autoduo.service", UNIT_PATH)
    _run(["systemctl", "daemon-reload"])

    ui.heading("Installing the autoduo command")
    shutil.copy2(REPO_DIR / "scripts" / "autoduo", "/usr/local/bin/autoduo")
    # The wrapper has to be executable; bandit flags any chmod literal.
    os.chmod("/usr/local/bin/autoduo", 0o755)  # nosec B103

    ui.heading("Installing log rotation")
    shutil.copy2(REPO_DIR / "scripts" / "logrotate-autoduo",
                 "/etc/logrotate.d/autoduo")


def cmd_install():
    """Install systemd service (no disclaimer, for scripted use)."""
    _ensure_root()
    ui.banner("autoduo install")
    _install()
    ui.line()
    ui.ok("Install complete.")
    ui.note("Next: sudo autoduo setup")


def cmd_uninstall():
    """Remove everything."""
    _ensure_root()
    ui.banner("autoduo uninstall")

    if subprocess.run(["systemctl", "list-unit-files", "autoduo"],
                      capture_output=True, returncode=0).returncode == 0:
        ui.heading("Stopping the service")
        subprocess.run(["systemctl", "disable", "--now", "autoduo"],
                       capture_output=True, check=False)

    ui.heading("Removing the systemd unit")
    UNIT_PATH.unlink(missing_ok=True)
    _run(["systemctl", "daemon-reload"])

    ui.heading("Removing log rotation")
    pathlib.Path("/etc/logrotate.d/autoduo").unlink(missing_ok=True)

    ui.heading("Removing the autoduo command")
    pathlib.Path("/usr/local/bin/autoduo").unlink(missing_ok=True)

    ui.heading("Removing data and logs")
    shutil.rmtree("/var/lib/autoduo", ignore_errors=True)
    shutil.rmtree("/var/log/autoduo", ignore_errors=True)

    ui.line()
    ui.ok("Uninstall complete.")
    ui.note("The source tree at /opt/autoduo is left in place.")


def cmd_start():
    _ensure_root()
    _run(["systemctl", "start", "autoduo"])
    ui.ok("Daemon started.")


def cmd_stop():
    _ensure_root()
    _run(["systemctl", "stop", "autoduo"])
    ui.ok("Daemon stopped.")


def cmd_restart():
    _ensure_root()
    _run(["systemctl", "restart", "autoduo"])
    ui.ok("Daemon restarted.")


def cmd_status():
    ui.line()
    _run(["systemctl", "status", "autoduo", "--no-pager"], check=False)
    if _service_active():
        r = subprocess.run(
            ["journalctl", "-u", "autoduo", "-n", "5", "--no-pager"],
            capture_output=True, text=True,
        )
        if r.stdout.strip():
            ui.line()
            ui.heading("Recent activity")
            ui.line()
            print(r.stdout, end="")


def cmd_logs():
    _ensure_root()
    ui.note("Tailing the journal. Ctrl+C stops it.")
    ui.line()
    try:
        subprocess.run(["journalctl", "-u", "autoduo", "-f"])
    except KeyboardInterrupt:
        ui.line()
        ui.note("Stopped.")


def cmd_version():
    """Print autoduo version."""
    from autoduo import __version__
    ui.line(f"autoduo {__version__}")
    # If systemd unit exists, show whether daemon is active
    if UNIT_PATH.exists():
        r = subprocess.run(
            ["systemctl", "is-active", "autoduo"],
            capture_output=True, text=True,
        )
        status = r.stdout.strip()
        ui.kv("daemon", status or "not running", indent=0)


def cmd_enroll():
    """One-shot enroll (no daemon start)."""
    _ensure_root()

    if not _show_disclaimer_and_get_acceptance():
        return 1

    ui.step(1, "Paste your activation URL", DUO_ADMIN_HINT)
    url = _prompt_with_retry("  Activation URL: ", validator=_validate_url)
    if url is None:
        return 1

    if not _confirm("  Was this URL just generated for you in Duo Admin? (y/N) "):
        return 1

    act_file = _secret_tempfile()
    act_file.write_text(url)

    rc = subprocess.run(
        [_venv_python(), "-m", "autoduo.enroll",
         "--vault", str(VAULT_PATH),
         "--activation-file", str(act_file),
         "--no-passphrase"],
        cwd=str(REPO_DIR),
    )
    act_file.unlink(missing_ok=True)

    if rc.returncode == 0:
        ui.line()
        ui.ok("Enrollment complete.")
        ui.note("Approve the new-device prompt on your phone, then run: autoduo start")
        return 0
    return rc.returncode


# ──────────────────────────────────────────────────────────────────────
# Dispatch
# ──────────────────────────────────────────────────────────────────────

COMMANDS = {
    "setup": cmd_setup,
    "install": cmd_install,
    "uninstall": cmd_uninstall,
    "start": cmd_start,
    "stop": cmd_stop,
    "restart": cmd_restart,
    "status": cmd_status,
    "logs": cmd_logs,
    "version": cmd_version,
    "enroll": cmd_enroll,
}

COMMAND_HELP = (
    ("setup", "interactive setup with disclaimer (first run)"),
    ("install", "install the service and its dependencies"),
    ("uninstall", "remove the service and its data"),
    ("enroll", "enroll with a Duo activation URL"),
    ("start", "start the daemon"),
    ("stop", "stop the daemon"),
    ("restart", "restart the daemon"),
    ("status", "show the daemon status"),
    ("logs", "tail the daemon log"),
    ("version", "print the version"),
)


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        prog = pathlib.Path(sys.argv[0]).name
        if prog.endswith(".py"):
            prog = "autoduo"  # run as `python -m autoduo.cli`
        ui.banner("autoduo")
        ui.kv("usage", f"{prog} <command>", indent=0)
        ui.line()
        ui.heading("Commands")
        ui.line()
        for name, desc in COMMAND_HELP:
            ui.line(f"    {ui.bold(f'{name:<10}')}  {desc}")
        ui.line()
        ui.note("Start with: sudo autoduo setup")
        ui.line()
        sys.exit(1)

    rc = COMMANDS[sys.argv[1]]()
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
