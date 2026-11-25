"""autoduo CLI: interactive setup and management commands.

Every line a person reads comes from :mod:`autoduo.ui`, so the same
wording works on a TTY (colour, dim metadata) and in a pipe (plain
text, no escape sequences).

Usage:
    autoduo setup      — interactive: disclaimer, paste link, enroll, start
    autoduo install    — install systemd service (no disclaimer)
    autoduo uninstall  — remove one instance (or --all)
    autoduo start      — start the instance's daemon
    autoduo stop       — stop the instance's daemon
    autoduo restart    — restart the instance's daemon
    autoduo status     — show the instance's daemon status
    autoduo logs       — tail the instance's daemon log
    autoduo instances  — list every instance and its state
    autoduo watch      — live dashboard (needs autoduo/dashboard.py)
    autoduo version    — print version
    autoduo enroll     — one-shot enroll (no daemon start)

Every command accepts ``--instance NAME`` before or after the command
name. The default instance is ``default``, which keeps the pre-instance
paths so an existing install needs no migration.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

from autoduo import instances, ui

REPO_DIR = pathlib.Path(__file__).resolve().parent.parent
UNIT_PATH = pathlib.Path("/etc/systemd/system/autoduo.service")
UNIT_TEMPLATE_PATH = pathlib.Path("/etc/systemd/system/autoduo@.service")
MAX_RETRIES = 3

# Set by main() from --instance; commands keep their zero-argument shape
# so setup() can call start() and the dispatch table stays simple.
_ACTIVE_INSTANCE = instances.resolve(instances.DEFAULT_NAME)
# True only when --instance was typed: `watch` then narrows to that one
# account instead of showing every instance.
_INSTANCE_EXPLICIT = False
_OPTS: dict[str, bool] = {}

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


def _paths() -> instances.InstancePaths:
    """Paths of the instance selected by ``--instance``."""
    return _ACTIVE_INSTANCE


def _unit() -> str:
    return instances.unit_name(_paths())


def _instance_flag() -> str:
    """The ``--instance`` suffix a printed command needs, if any."""
    paths = _paths()
    return "" if paths.legacy else f" --instance {paths.name}"


def _service_active() -> bool:
    return instances.unit_state(_paths()) == "running"


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
    paths = _paths()

    if not _show_disclaimer_and_get_acceptance():
        return 1

    if not paths.legacy:
        ui.kv("instance", paths.name)

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

    if paths.vault.exists():
        ui.line()
        ui.kv("vault", str(paths.vault))
        if not _confirm("  Re-enroll and orphan the existing device? (y/N) "):
            act_file.unlink(missing_ok=True)
            ui.note("Skipping enrollment. The vault is unchanged.")
            cmd_start()
            return _push_test_gate()

    ui.line()
    ui.step(2, "Enroll this device", "Generating an RSA keypair and calling Duo.")
    # The instance *is* the vault path here: enroll writes exactly one
    # file, so passing the resolved path keeps it in the right instance.
    instances.ensure_dirs(paths)
    rc = subprocess.run(
        [_venv_python(), "-m", "autoduo.enroll",
         "--vault", str(paths.vault),
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
        ui.note(
            "Approve on your phone, then run: "
            f"autoduo start{_instance_flag()}"
        )
        return 1

    ui.line()
    ui.step(4, "Start the daemon")
    _enable_and_start()
    ui.ok(f"Instance {paths.name} is running and enabled at boot.")

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
        ("autoduo instances", "every account and its state"),
        ("autoduo uninstall", "remove this instance"),
    ):
        ui.line(f"    {ui.bold(f'{name:<18}')}  {desc}")
    if not _paths().legacy:
        ui.line()
        ui.note(
            f"These apply to instance {_paths().name}; add "
            f"--instance {_paths().name} to each."
        )
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
    instances.ensure_dirs(_paths())

    ui.heading("Creating the Python environment")
    if not (REPO_DIR / ".venv").exists():
        _run([sys.executable, "-m", "venv", str(REPO_DIR / ".venv")])
    _run([str(REPO_DIR / ".venv" / "bin" / "pip"), "install", "--quiet", "--upgrade", "pip"])
    _run([str(REPO_DIR / ".venv" / "bin" / "pip"), "install", "--quiet",
          "-r", str(REPO_DIR / "requirements.txt")])

    ui.heading("Installing the systemd unit")
    shutil.copy2(REPO_DIR / "systemd" / "autoduo.service", UNIT_PATH)
    # The template makes every named instance a unit of its own.
    template = REPO_DIR / "systemd" / "autoduo@.service"
    if template.exists():
        shutil.copy2(template, UNIT_TEMPLATE_PATH)
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


def _rmdir_if_empty(path: pathlib.Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass  # absent, or another instance still lives there


def _remove_instance_files(paths: instances.InstancePaths) -> None:
    """Delete one instance's vault, state, lock and logs — and nothing else.

    The legacy instance shares ``/var/lib/autoduo`` with the
    ``instances/`` container, so it is removed file by file and the
    directories are kept while any other instance still needs them.
    """
    if paths.legacy:
        for target in (
            paths.vault,
            paths.actual_lock,
            paths.state,
            paths.lock,
        ):
            target.unlink(missing_ok=True)
        _rmdir_if_empty(paths.vault.parent)
        for target in (paths.daemon_log, paths.audit_log):
            target.unlink(missing_ok=True)
        _rmdir_if_empty(paths.daemon_log.parent)
        return
    shutil.rmtree(paths.vault.parent, ignore_errors=True)
    shutil.rmtree(paths.daemon_log.parent, ignore_errors=True)


def _uninstall_all() -> int:
    """Remove every instance, both units and the global install files."""
    typed = _read(
        "  This removes every instance. Type ALL to continue: "
    ).strip()
    if typed != "ALL":
        ui.error("Confirmation did not match. Nothing was changed.")
        return 1

    ui.heading("Stopping every instance")
    for paths in instances.list_instances():
        subprocess.run(
            ["systemctl", "disable", "--now", instances.unit_name(paths)],
            capture_output=True, check=False,
        )

    ui.heading("Removing the systemd units")
    UNIT_PATH.unlink(missing_ok=True)
    UNIT_TEMPLATE_PATH.unlink(missing_ok=True)
    _run(["systemctl", "daemon-reload"], check=False)

    ui.heading("Removing log rotation")
    pathlib.Path("/etc/logrotate.d/autoduo").unlink(missing_ok=True)

    ui.heading("Removing the autoduo command")
    pathlib.Path("/usr/local/bin/autoduo").unlink(missing_ok=True)

    ui.heading("Removing data and logs")
    shutil.rmtree(instances.DATA_ROOT, ignore_errors=True)
    shutil.rmtree(instances.LOG_ROOT, ignore_errors=True)

    ui.line()
    ui.ok("Uninstall complete.")
    ui.note("The source tree is left in place.")
    return 0


def cmd_uninstall():
    """Remove one instance, or all of them with --all."""
    _ensure_root()
    ui.banner("autoduo uninstall")

    if _OPTS.get("all"):
        return _uninstall_all()

    paths = _paths()

    # A named instance needs its own name typed: a stray Enter must not
    # erase a Duo account. The default instance keeps the historical
    # no-prompt behaviour for scripted removal.
    if not paths.legacy:
        typed = _read(f"  Type {paths.name} to remove this instance: ").strip()
        if typed != paths.name:
            ui.error("Instance name did not match. Nothing was changed.")
            return 1

    ui.heading("Stopping the service")
    subprocess.run(
        ["systemctl", "disable", "--now", _unit()],
        capture_output=True, check=False,
    )

    if paths.legacy:
        ui.heading("Removing the systemd unit")
        UNIT_PATH.unlink(missing_ok=True)
        _run(["systemctl", "daemon-reload"], check=False)

    ui.heading("Removing data and logs")
    _remove_instance_files(paths)

    ui.line()
    ui.ok(f"Instance {paths.name} removed.")
    ui.note(
        "Global files (the command, logrotate and the unit template) are "
        "kept; autoduo uninstall --all removes them."
    )
    return 0


def _enable_and_start() -> None:
    """Start the unit now *and* after a reboot.

    ``setup`` used to start the daemon without enabling it, so the first
    reboot left the account silently unapproved with nothing running to
    say why. Enabling is part of "it works", not a separate preference.
    """
    _run(["systemctl", "enable", _unit()])
    _run(["systemctl", "start", _unit()])


def cmd_start():
    _ensure_root()
    _run(["systemctl", "start", _unit()])
    ui.ok(f"Instance {_paths().name} started.")


def cmd_stop():
    _ensure_root()
    _run(["systemctl", "stop", _unit()])
    ui.ok(f"Instance {_paths().name} stopped.")


def cmd_restart():
    _ensure_root()
    _run(["systemctl", "restart", _unit()])
    ui.ok(f"Instance {_paths().name} restarted.")


def cmd_status():
    paths = _paths()
    ui.line()
    ui.kv("instance", paths.name)
    ui.kv("vault", str(paths.vault))
    # A missing vault is not "a stopped service": nothing was ever enrolled,
    # and reporting success for it is the same false-green this codebase
    # keeps having to remove.
    if not paths.vault.exists():
        ui.line()
        ui.warn(
            f"Instance {paths.name} is not enrolled. "
            f"Run: autoduo setup --instance {paths.name}"
        )
        return 1
    _run(["systemctl", "status", _unit(), "--no-pager"], check=False)
    if _service_active():
        r = subprocess.run(
            ["journalctl", "-u", _unit(), "-n", "5", "--no-pager"],
            capture_output=True, text=True,
        )
        if r.stdout.strip():
            ui.line()
            ui.heading("Recent activity")
            ui.line()
            print(r.stdout, end="")


def cmd_logs():
    _ensure_root()
    ui.note(f"Tailing {_unit()} in the journal. Ctrl+C stops it.")
    ui.line()
    try:
        subprocess.run(["journalctl", "-u", _unit(), "-f"])
    except KeyboardInterrupt:
        ui.line()
        ui.note("Stopped.")


def cmd_version():
    """Print autoduo version and the selected instance."""
    from autoduo import __version__
    ui.line(f"autoduo {__version__}")
    paths = _paths()
    ui.kv("instance", paths.name, indent=0)
    # If the unit exists, show whether its daemon is active.
    if UNIT_PATH.exists() or UNIT_TEMPLATE_PATH.exists():
        ui.kv("daemon", instances.unit_state(paths), indent=0)


def cmd_enroll():
    """One-shot enroll (no daemon start)."""
    _ensure_root()
    paths = _paths()

    if not _show_disclaimer_and_get_acceptance():
        return 1

    if not paths.legacy:
        ui.kv("instance", paths.name)

    ui.step(1, "Paste your activation URL", DUO_ADMIN_HINT)
    url = _prompt_with_retry("  Activation URL: ", validator=_validate_url)
    if url is None:
        return 1

    if not _confirm("  Was this URL just generated for you in Duo Admin? (y/N) "):
        return 1

    act_file = _secret_tempfile()
    act_file.write_text(url)

    instances.ensure_dirs(paths)
    rc = subprocess.run(
        [_venv_python(), "-m", "autoduo.enroll",
         "--vault", str(paths.vault),
         "--activation-file", str(act_file),
         "--no-passphrase"],
        cwd=str(REPO_DIR),
    )
    act_file.unlink(missing_ok=True)

    if rc.returncode == 0:
        ui.line()
        ui.ok("Enrollment complete.")
        ui.note(
            "Approve the new-device prompt on your phone, then run: "
            f"autoduo start{_instance_flag()}"
        )
        return 0
    return rc.returncode


# ──────────────────────────────────────────────────────────────────────
# Instance listing and dashboard dispatch
# ──────────────────────────────────────────────────────────────────────

def _short_time(value: str | None) -> str:
    """``2026-11-18T02:08:09+00:00`` → ``2026-11-18 02:08`` (or never)."""
    if not value:
        return "never"
    return value.replace("T", " ")[:16]


def _print_instance_table(rows: list[instances.InstanceStatus]) -> None:
    """One compact, plainly aligned line per instance, inside 80 columns."""
    name_width = max(4, min(32, max(len(row.paths.name) for row in rows)))
    ui.line(ui.bold(
        f"  {'NAME'.ljust(name_width)}  {'STATE':<9}  "
        f"{'LAST APPROVAL':<16}  {'24H':>3}  {'PEND':>4}"
    ))
    for row in rows:
        ui.line(
            f"  {row.paths.name.ljust(name_width)}  {row.state:<9}  "
            f"{_short_time(row.last_approval_at):<16}  "
            f"{row.approvals_24h:>3}  {row.pending:>4}"
        )


def cmd_instances():
    """List every instance with state and recent activity."""
    rows = instances.status_all()
    ui.line()
    _print_instance_table(rows)
    ui.line()

    needs_setup = [row.paths.name for row in rows if row.state == "no-vault"]
    if needs_setup:
        ui.note(
            "No vault yet: " + ", ".join(needs_setup)
            + " — run autoduo setup for that instance."
        )
    for row in rows:
        if row.error:
            ui.note(f"{row.paths.name}: {row.error}")
    return 0


def cmd_watch():
    """Live dashboard, refreshed from the instance status module.

    The dashboard takes a *callable* so it re-polls every frame; a
    snapshot list would freeze the view. ``--instance NAME`` narrows it
    to one account, otherwise every instance is shown.
    """
    try:
        from autoduo import dashboard
    except ImportError:
        ui.error(
            "watch needs the dashboard module (autoduo/dashboard.py), "
            "which is not installed in this build."
        )
        return 1

    names = [_paths().name] if _INSTANCE_EXPLICIT else None

    def statuses() -> list[instances.InstanceStatus]:
        return instances.status_all(names)

    return dashboard.run(statuses, refresh=1.0) or 0


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
    "instances": cmd_instances,
    "watch": cmd_watch,
    "version": cmd_version,
    "enroll": cmd_enroll,
}

COMMAND_HELP = (
    ("setup", "interactive setup with disclaimer (first run)"),
    ("install", "install the service and its dependencies"),
    ("uninstall", "remove one instance (--all for everything)"),
    ("enroll", "enroll with a Duo activation URL"),
    ("start", "start the instance's daemon"),
    ("stop", "stop the instance's daemon"),
    ("restart", "restart the instance's daemon"),
    ("status", "show the instance's daemon status"),
    ("logs", "tail the instance's daemon log"),
    ("instances", "list every instance and its state"),
    ("watch", "live dashboard of all instances"),
    ("version", "print the version"),
)


def _split_instance(argv: list[str]) -> tuple[list[str], str | None]:
    """Pull ``--instance NAME`` (or ``--instance=NAME``) out of argv.

    Accepting it anywhere is what makes ``autoduo --instance alice
    status`` and ``autoduo status --instance alice`` both work.
    """
    rest: list[str] = []
    name: str | None = None
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--instance":
            if index + 1 >= len(argv):
                raise ValueError("--instance needs a name")
            name = argv[index + 1]
            index += 2
            continue
        if token.startswith("--instance="):
            name = token.split("=", 1)[1]
            index += 1
            continue
        rest.append(token)
        index += 1
    return rest, name


def _usage(prog: str) -> None:
    ui.banner("autoduo")
    ui.kv("usage", f"{prog} [--instance NAME] <command>", indent=0)
    ui.line()
    ui.heading("Commands")
    ui.line()
    for name, desc in COMMAND_HELP:
        ui.line(f"    {ui.bold(f'{name:<10}')}  {desc}")
    ui.line()
    ui.note("Start with: sudo autoduo setup")
    ui.line()


def main():
    global _ACTIVE_INSTANCE, _INSTANCE_EXPLICIT, _OPTS

    try:
        argv, instance_name = _split_instance(list(sys.argv[1:]))
        _ACTIVE_INSTANCE = instances.resolve(
            instance_name if instance_name is not None else instances.DEFAULT_NAME
        )
    except ValueError as exc:
        ui.error(str(exc))
        sys.exit(1)

    _INSTANCE_EXPLICIT = instance_name is not None
    _OPTS = {"all": "--all" in argv}
    argv = [token for token in argv if token != "--all"]

    if not argv or argv[0] not in COMMANDS:
        prog = pathlib.Path(sys.argv[0]).name
        if prog.endswith(".py"):
            prog = "autoduo"  # run as `python -m autoduo.cli`
        _usage(prog)
        sys.exit(1)

    rc = COMMANDS[argv[0]]()
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
