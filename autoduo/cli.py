"""autoduo CLI: interactive setup and management commands.

Usage:
    autoduo setup      — interactive: paste link, enroll, start
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

import getpass
import os
import pathlib
import shutil
import subprocess
import sys
import textwrap

REPO_DIR = pathlib.Path(__file__).resolve().parent.parent
VAULT_DIR = pathlib.Path("/var/lib/autoduo")
LOG_DIR = pathlib.Path("/var/log/autoduo")
VAULT_PATH = VAULT_DIR / "vault.enc"
UNIT_PATH = pathlib.Path("/etc/systemd/system/autoduo.service")
MAX_RETRIES = 3


def _run(cmd: list[str], check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, capture_output=capture, text=capture)


def _ensure_root():
    if os.geteuid() != 0:
        print("ERROR: must run as root. Try: sudo autoduo ...", file=sys.stderr)
        sys.exit(1)


def _venv_python() -> str:
    return str(REPO_DIR / ".venv" / "bin" / "python")


def _service_active() -> bool:
    r = subprocess.run(
        ["systemctl", "is-active", "autoduo"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() == "active"


# ──────────────────────────────────────────────────────────────────────
# Disclaimer
# ──────────────────────────────────────────────────────────────────────

DISCLAIMER_TEXT = textwrap.dedent("""
╔══════════════════════════════════════════════════════════════╗
║                  autoduo setup                                ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  WARNING: This software defeats Duo 2FA.                     ║
║                                                              ║
║  By continuing, you acknowledge and accept that:             ║
║                                                              ║
║   • Anyone with shell access to this server can approve      ║
║     any Duo push for the configured account.                 ║
║   • This software DEFEATS the security purpose of 2FA.       ║
║   • You are SOLELY responsible for securing this server       ║
║     (SSH keys only, no shared users, encrypted disk, etc.)  ║
║   • If Verified Duo Push is enforced for your account,       ║
║     the login will still fail even after auto-approval.      ║
║   • Duo's anomaly detection may flag and disable this         ║
║     account. You accept this risk.                           ║
║   • The authors and contributors accept NO LIABILITY for      ║
║     any damage, account loss, security breach, or any other  ║
║     consequence of using this software.                       ║
║                                                              ║
║  This software is provided "AS IS", without warranty of any  ║
║  kind, express or implied. Use at your own risk.              ║
║                                                              ║
║  Set DUO_BOT_SKIP_DISCLAIMER=1 to bypass this prompt in      ║
║  scripted installs. This is the only way to skip it.         ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
""")


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
        print("Disclaimer skipped via DUO_BOT_SKIP_DISCLAIMER=1.")
        return True

    print(DISCLAIMER_TEXT)
    for attempt in range(MAX_RETRIES):
        resp = input("Type 'I ACCEPT' to continue, or Ctrl+C to abort: ").strip()
        if resp == "I ACCEPT":
            return True
        if resp == "":
            print("  (empty input — try again)")
        else:
            print(f"  (got {resp!r}, expected exactly 'I ACCEPT' — try again)")
    print("Too many invalid attempts. Aborting.")
    return False


# ──────────────────────────────────────────────────────────────────────
# Input helpers with retry
# ──────────────────────────────────────────────────────────────────────

def _prompt_with_retry(prompt: str, validator=None) -> str | None:
    """Prompt with retry on empty/invalid input.

    Returns the input string, or None if MAX_RETRIES exceeded.
    validator: optional callable(str) -> str | None. If returns None,
    input is rejected and the user re-prompts.
    """
    for attempt in range(MAX_RETRIES):
        try:
            resp = input(prompt).strip()
        except EOFError:
            return None
        if not resp:
            print("  (empty — please provide input)")
            continue
        if validator is not None:
            err = validator(resp)
            if err is not None:
                print(f"  ({err})")
                continue
        return resp
    print("  Too many invalid attempts. Aborting.")
    return None


def _confirm(prompt: str) -> bool:
    """Y/N confirmation. Defaults to No on empty/invalid."""
    try:
        resp = input(prompt).strip().lower()
    except EOFError:
        return False
    return resp in ("y", "yes")


# ──────────────────────────────────────────────────────────────────────
# Commands
# ──────────────────────────────────────────────────────────────────────

def cmd_setup():
    """Interactive setup: paste activation link, enroll, start."""
    _ensure_root()

    print()
    print("╔══════════════════════════════════════════════════════╗")
    print("║           autoduo interactive setup                 ║")
    print("╚══════════════════════════════════════════════════════╝")
    print()

    # Step 1: disclaimer
    if not _show_disclaimer_and_get_acceptance():
        return 1

    # Step 2: install if not already
    if not (REPO_DIR / ".venv").exists():
        print("First run — installing dependencies...\n")
        cmd_install()
    else:
        print("✓ Already installed.\n")

    # Step 3: get activation URL with retry
    print("Paste your Duo activation URL below.")
    print("  (Get it from Duo Admin → Users → Add Device → Tablet → Android)")
    print()
    url = _prompt_with_retry("Activation URL: ", validator=_validate_url)
    if url is None:
        return 1

    if not _confirm("  Confirm: this URL was just generated for you by Duo Admin? [y/N] "):
        print("Aborted.")
        return 1

    # Save activation URL to temp file (mode 0600)
    act_file = pathlib.Path("/tmp/autoduo-act.txt")
    act_file.write_text(url)
    act_file.chmod(0o600)

    # Step 4: check if vault already exists
    if VAULT_PATH.exists():
        print(f"\n  Vault already exists at {VAULT_PATH}")
        if not _confirm("  Overwrite? This will re-enroll and orphan the old device. [y/N] "):
            print("  Skipping enrollment. Vault unchanged.")
            act_file.unlink(missing_ok=True)
            cmd_start()
            return _push_test_gate()

    # Step 5: enroll
    print(f"\n  Enrolling (generating RSA keypair, calling Duo API)...")
    rc = subprocess.run(
        [_venv_python(), "-m", "autoduo.enroll",
         "--vault", str(VAULT_PATH),
         "--activation-file", str(act_file),
         "--no-passphrase"],
        cwd=str(REPO_DIR),
    )
    act_file.unlink(missing_ok=True)

    if rc.returncode != 0:
        print("\n  Enrollment failed. Check the output above.", file=sys.stderr)
        if _confirm("  Try again with a new activation URL? [y/N] "):
            return cmd_setup()
        return 1

    # Step 6: tell user to approve on phone
    print()
    print("  ┌─────────────────────────────────────────────────┐")
    print("  │  IMPORTANT: Open Duo Mobile on your phone now   │")
    print("  │  and TAP APPROVE on the new-device prompt.      │")
    print("  │  This is the only time you'll need to touch it. │")
    print("  └─────────────────────────────────────────────────┘")
    print()
    if not _confirm("  Have you approved on your phone? [y/N] "):
        print("  Skipping. Approve on your phone, then run: autoduo start")
        return 1

    # Step 7: start daemon
    cmd_start()

    # Step 8: push-test gate
    return _push_test_gate()


def _push_test_gate() -> int:
    """After daemon start, confirm end-to-end push handling works.

    Asks the user to trigger a push on any service protected by their
    Duo account, then verifies the daemon received and approved it.
    """
    print()
    print("  ┌─────────────────────────────────────────────────┐")
    print("  │  Push-test gate                                  │")
    print("  │                                                  │")
    print("  │  Trigger a login on any service protected by    │")
    print("  │  your Duo account (e.g. try to log in to VPN,    │")
    print("  │  SSO, or wherever you have Duo enabled).         │")
    print("  │                                                  │")
    print("  │  The daemon should auto-approve it without you   │")
    print("  │  touching your phone.                            │")
    print("  └─────────────────────────────────────────────────┘")
    print()
    if not _confirm("  Did the login succeed (without you tapping the phone)? [y/N] "):
        print()
        print("  Troubleshooting:")
        print("    1. Did you approve the new-device prompt on your phone earlier?")
        print("    2. Is the service protected by Duo Verified Push? If so,")
        print("       auto-approval is impossible — Verified Push requires")
        print("       you to type a code from the browser into the phone.")
        print("    3. Check 'autoduo logs' for any error events.")
        print()
        return 1

    print()
    print("  ✓ Setup verified! The daemon is auto-accepting pushes.")
    print()
    print("  Useful commands:")
    print("    autoduo status    # check daemon is running")
    print("    autoduo logs      # live log tail")
    print("    autoduo uninstall # remove everything")
    print()
    return 0


def cmd_install():
    """Install systemd service (no disclaimer, for scripted use)."""
    _ensure_root()

    print("Installing autoduo...\n")

    print("  Installing OS packages...")
    _run(["apt-get", "update", "-qq"], check=False, capture=True)
    _run(["apt-get", "install", "-y", "--no-install-recommends",
           "python3", "python3-venv", "python3-pip", "systemd-timesyncd"])

    print("  Enabling time sync...")
    _run(["systemctl", "enable", "--now", "systemd-timesyncd"], check=False)

    print("  Creating directories...")
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    print("  Creating Python venv...")
    if not (REPO_DIR / ".venv").exists():
        _run([sys.executable, "-m", "venv", str(REPO_DIR / ".venv")])
    _run([str(REPO_DIR / ".venv" / "bin" / "pip"), "install", "--quiet", "--upgrade", "pip"])
    _run([str(REPO_DIR / ".venv" / "bin" / "pip"), "install", "--quiet",
          "-r", str(REPO_DIR / "requirements.txt")])

    print("  Installing systemd unit...")
    shutil.copy2(REPO_DIR / "systemd" / "autoduo.service", UNIT_PATH)
    _run(["systemctl", "daemon-reload"])

    print("  Installing autoduo wrapper to /usr/local/bin...")
    shutil.copy2(REPO_DIR / "autoduo", "/usr/local/bin/autoduo")
    os.chmod("/usr/local/bin/autoduo", 0o755)

    print("  Installing logrotate config...")
    shutil.copy2(REPO_DIR / "scripts" / "logrotate-autoduo",
                 "/etc/logrotate.d/autoduo")

    print("\n  ✓ Install complete.")
    print("    Next step: sudo autoduo setup\n")


def cmd_uninstall():
    """Remove everything."""
    _ensure_root()

    print("Uninstalling autoduo...\n")

    if subprocess.run(["systemctl", "list-unit-files", "autoduo"],
                      capture_output=True, returncode=0).returncode == 0:
        print("  Stopping service...")
        subprocess.run(["systemctl", "disable", "--now", "autoduo"],
                       capture_output=True, check=False)

    print("  Removing systemd unit...")
    UNIT_PATH.unlink(missing_ok=True)
    _run(["systemctl", "daemon-reload"])

    print("  Removing logrotate config...")
    pathlib.Path("/etc/logrotate.d/autoduo").unlink(missing_ok=True)

    print("  Removing /usr/local/bin/autoduo...")
    pathlib.Path("/usr/local/bin/autoduo").unlink(missing_ok=True)

    print("  Removing data and logs...")
    shutil.rmtree("/var/lib/autoduo", ignore_errors=True)
    shutil.rmtree("/var/log/autoduo", ignore_errors=True)

    print("\n  ✓ Uninstall complete. Source tree at /opt/autoduo left in place.\n")


def cmd_start():
    _ensure_root()
    print("  Starting autoduo...")
    _run(["systemctl", "start", "autoduo"])
    print("  ✓ Started.\n")


def cmd_stop():
    _ensure_root()
    print("  Stopping autoduo...")
    _run(["systemctl", "stop", "autoduo"])
    print("  ✓ Stopped.\n")


def cmd_restart():
    _ensure_root()
    print("  Restarting autoduo...")
    _run(["systemctl", "restart", "autoduo"])
    print("  ✓ Restarted.\n")


def cmd_status():
    print()
    _run(["systemctl", "status", "autoduo", "--no-pager"], check=False)
    print()
    if _service_active():
        r = subprocess.run(
            ["journalctl", "-u", "autoduo", "-n", "5", "--no-pager"],
            capture_output=True, text=True,
        )
        if r.stdout.strip():
            print("Recent logs:")
            print(r.stdout)


def cmd_logs():
    _ensure_root()
    print("Tailing autoduo logs (Ctrl+C to stop)...\n")
    try:
        subprocess.run(["journalctl", "-u", "autoduo", "-f"])
    except KeyboardInterrupt:
        print("\n  Stopped tailing.\n")


def cmd_version():
    """Print autoduo version."""
    from autoduo import __version__
    print(f"autoduo {__version__}")
    # If systemd unit exists, show whether daemon is active
    if UNIT_PATH.exists():
        r = subprocess.run(
            ["systemctl", "is-active", "autoduo"],
            capture_output=True, text=True,
        )
        status = r.stdout.strip()
        if status == "active":
            print("daemon: active")
        elif status:
            print(f"daemon: {status}")
        else:
            print("daemon: not running")


def cmd_enroll():
    """One-shot enroll (no daemon start)."""
    _ensure_root()

    if not _show_disclaimer_and_get_acceptance():
        return 1

    url = _prompt_with_retry("Activation URL: ", validator=_validate_url)
    if url is None:
        return 1

    if not _confirm("  Was this just generated for you by Duo Admin? [y/N] "):
        return 1

    act_file = pathlib.Path("/tmp/autoduo-act.txt")
    act_file.write_text(url)
    act_file.chmod(0o600)

    rc = subprocess.run(
        [_venv_python(), "-m", "autoduo.enroll",
         "--vault", str(VAULT_PATH),
         "--activation-file", str(act_file),
         "--no-passphrase"],
        cwd=str(REPO_DIR),
    )
    act_file.unlink(missing_ok=True)

    if rc.returncode == 0:
        print("\n  ✓ Enrollment complete.")
        print("    Approve the new-device prompt on your phone, then:")
        print("      autoduo start\n")
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


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        prog = sys.argv[0]
        print(f"""Usage: {prog} <command>

Commands:
  setup       Interactive setup with disclaimer (recommended for first run)
  install     Install systemd service and dependencies (no disclaimer)
  uninstall   Remove systemd service and data
  enroll      Enroll with a Duo activation URL
  start       Start the daemon
  stop        Stop the daemon
  restart     Restart the daemon
  status      Show daemon status
  logs        Tail daemon logs (Ctrl+C to stop)
  version     Print version
""")
        sys.exit(1)

    rc = COMMANDS[sys.argv[1]]()
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()