"""Per-instance layout, name validation and status discovery.

Every Duo account is an independent instance: its own vault, state file,
lock, log directory and systemd unit. There is deliberately no
multi-account vault — two accounts are two processes that cannot touch
each other's data.

This module is the single source of truth for where an instance lives.
The CLI and the dashboard both derive their paths from :func:`resolve`;
nothing else should spell ``/var/lib/autoduo/...`` by hand.

The instance named ``default`` is the pre-instance (legacy) layout:
``/var/lib/autoduo/vault.enc`` and ``/var/log/autoduo/daemon.log``. It is
read as an instance so an existing install keeps working with no
migration and no moved files.

Nothing here prints, and nothing here exits: :func:`status` reports an
unreadable file as ``error`` rather than raising, because it runs inside
a live dashboard.
"""
from __future__ import annotations

import datetime
import json
import pathlib
import re
import subprocess
import time
from dataclasses import dataclass

# Overridable in tests; the deployed values are fixed by systemd.
DATA_ROOT = pathlib.Path("/var/lib/autoduo")
LOG_ROOT = pathlib.Path("/var/log/autoduo")

TAIL_BYTES = 256 * 1024          # daemon.log is megabytes; read only the end
SYSTEMCTL_TIMEOUT = 2.0
STATE_CACHE_SECONDS = 5.0

DEFAULT_NAME = "default"
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")

# systemd is-active output → the state vocabulary the UI understands.
_STATE_MAP = {
    "active": "running",
    "reloading": "running",
    "refreshing": "running",
    "activating": "running",
    "inactive": "stopped",
    "deactivating": "stopped",
    "failed": "failed",
}

# unit name → (monotonic timestamp, state)
_STATE_CACHE: dict[str, tuple[float, str]] = {}


@dataclass(frozen=True)
class InstancePaths:
    """Every path one instance owns. ``legacy`` marks the old layout."""

    name: str
    vault: pathlib.Path
    state: pathlib.Path
    lock: pathlib.Path
    daemon_log: pathlib.Path
    audit_log: pathlib.Path
    legacy: bool

    @property
    def actual_lock(self) -> pathlib.Path:
        """The lock file the daemon really creates.

        ``vault.acquire_instance_lock`` appends ``.lock`` to the vault
        name (``vault.enc.lock``), while ``lock`` above is the documented
        name in the instance layout. Kept separate so nothing has to
        guess.
        """
        return self.vault.with_name(self.vault.name + ".lock")


@dataclass
class InstanceStatus:
    """A snapshot that is safe to draw, even when the host is broken."""

    paths: InstancePaths
    state: str
    last_event: str | None
    last_event_at: str | None
    last_approval_at: str | None
    approvals_24h: int
    pending: int
    error: str | None


def validate_name(name: str) -> str:
    """Return ``name`` if it is safe for a path and a unit name.

    The grammar is ``[a-z0-9][a-z0-9_-]{0,31}``. That rejects ``..``,
    ``/``, whitespace, uppercase, a leading dash and over-long names, so
    a value such as ``../../etc`` can never reach a filesystem path or
    ``systemctl``. ``default`` is a valid instance name: it is the legacy
    layout and the CLI's default.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("instance name cannot be empty")
    if _NAME_RE.fullmatch(name) is None:
        raise ValueError(
            f"invalid instance name {name!r}: use 1-32 characters of "
            "a-z, 0-9, '-' or '_', starting with a letter or digit"
        )
    return name


def unit_name(paths: InstancePaths) -> str:
    """The systemd unit that runs this instance.

    ``default`` keeps the plain ``autoduo`` unit; every other instance
    uses the ``autoduo@`` template so units are never shared.
    """
    if paths.legacy:
        return "autoduo"
    return f"autoduo@{paths.name}"


def resolve(name: str) -> InstancePaths:
    """Map an instance name to its paths.

    Pure path arithmetic: it never touches the filesystem, so it works
    before ``setup`` has created anything and inside tests with a
    monkeypatched root.
    """
    name = validate_name(name)
    if name == DEFAULT_NAME:
        # The legacy layout is *not* moved; it is read as this instance.
        base = DATA_ROOT
        return InstancePaths(
            name=name,
            vault=base / "vault.enc",
            state=base / "seen.json",
            lock=base / "vault.lock",
            daemon_log=LOG_ROOT / "daemon.log",
            audit_log=LOG_ROOT / "audit.log",
            legacy=True,
        )
    base = DATA_ROOT / "instances" / name
    logs = LOG_ROOT / name
    return InstancePaths(
        name=name,
        vault=base / "vault.enc",
        state=base / "seen.json",
        lock=base / "vault.lock",
        daemon_log=logs / "daemon.log",
        audit_log=logs / "audit.log",
        legacy=False,
    )


def list_instances() -> list[InstancePaths]:
    """``default`` first, then every named instance directory, sorted.

    A directory whose name fails :func:`validate_name` is ignored: it
    cannot be addressed safely, so pretending it is an instance would
    only produce a broken unit name.
    """
    found = [resolve(DEFAULT_NAME)]
    root = DATA_ROOT / "instances"
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.is_dir():
            continue
        try:
            found.append(resolve(entry.name))
        except ValueError:
            continue
    return found


def _make_dir(path: pathlib.Path, mode: int) -> None:
    try:
        path.mkdir(parents=True)
    except FileExistsError:
        if not path.is_dir():
            raise
        return  # existing caller-owned directory: leave its mode alone
    path.chmod(mode)


def ensure_dirs(paths: InstancePaths) -> None:
    """Create the instance's directories with their intended modes.

    The vault directory is 0700 and the log directory 0750 — the same modes
    both systemd units ask for (``StateDirectoryMode``/``LogsDirectoryMode``),
    so the CLI and the unit cannot disagree about a directory one of them
    created first. An existing directory is never re-chmodded (it may be a
    caller's shared directory).
    """
    _make_dir(paths.vault.parent, 0o700)
    _make_dir(paths.daemon_log.parent, 0o750)


def clear_state_cache() -> None:
    """Forget cached systemd states (for tests and forced refreshes)."""
    _STATE_CACHE.clear()


def _query_unit(unit: str) -> str:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=SYSTEMCTL_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        # systemctl missing, permission denied, timed out: no claim.
        return "unknown"
    return _STATE_MAP.get((result.stdout or "").strip(), "unknown")


def unit_state(paths: InstancePaths) -> str:
    """``running``/``stopped``/``failed``/``unknown`` for the unit.

    Cached for :data:`STATE_CACHE_SECONDS` so a 1 Hz dashboard does not
    fork ``systemctl`` every second.
    """
    unit = unit_name(paths)
    now = time.monotonic()
    cached = _STATE_CACHE.get(unit)
    if cached is not None and now - cached[0] < STATE_CACHE_SECONDS:
        return cached[1]
    state = _query_unit(unit)
    _STATE_CACHE[unit] = (now, state)
    return state


def _tail_lines(path: pathlib.Path) -> list[str]:
    """The last chunk of a file as lines, without reading it whole.

    A partial first line (because the seek landed mid-line) is dropped;
    a trailing line without a newline is left in place so the caller can
    decide it is truncated.
    """
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        start = max(0, size - TAIL_BYTES)
        handle.seek(start)
        data = handle.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    return lines


def _read_records(path: pathlib.Path) -> tuple[list[dict], str | None]:
    """Parse JSON-lines records from the tail of a log.

    A missing file is not an error (the daemon may never have run); an
    unreadable file or directory is reported as text, never raised. A
    half-written line is simply skipped.
    """
    try:
        lines = _tail_lines(path)
    except FileNotFoundError:
        return [], None
    except OSError as exc:
        return [], f"{path.name}: {exc.strerror or exc}"
    records: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records, None


def _parse_ts(value: object) -> datetime.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _note_approval(
    store: dict[str, datetime.datetime], record: dict
) -> None:
    """Remember one approval, newest wins under the same urgid."""
    when = _parse_ts(record.get("ts"))
    if when is None:
        return
    key = record.get("urgid") or record.get("txid") or record["ts"]
    previous = store.get(key)
    if previous is None or when > previous:
        store[key] = when


def _count_state(path: pathlib.Path) -> tuple[int, str | None]:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return 0, None
    except OSError as exc:
        return 0, f"{path.name}: {exc.strerror or exc}"
    try:
        data = json.loads(text)
    except ValueError:
        return 0, f"{path.name}: not valid JSON"
    if not isinstance(data, dict):
        return 0, f"{path.name}: not a JSON object"
    return len(data), None


def status(paths: InstancePaths) -> InstanceStatus:
    """Best-effort snapshot of one instance. Never raises.

    Only the tail of ``daemon.log`` is read; approvals are also taken
    from ``audit.log`` when it exists, de-duplicated by ``urgid``.
    """
    errors: list[str] = []

    daemon_records, daemon_error = _read_records(paths.daemon_log)
    if daemon_error:
        errors.append(daemon_error)
    audit_records, audit_error = _read_records(paths.audit_log)
    if audit_error:
        errors.append(audit_error)

    last = daemon_records[-1] if daemon_records else None
    if last is None and audit_records:
        last = audit_records[-1]

    approvals: dict[str, datetime.datetime] = {}
    for record in daemon_records:
        if record.get("event") == "approve_ok":
            _note_approval(approvals, record)
    for record in audit_records:
        if record.get("event") == "approved":
            _note_approval(approvals, record)

    cutoff = _now() - datetime.timedelta(hours=24)
    approvals_24h = sum(1 for when in approvals.values() if when >= cutoff)
    latest = max(approvals.values(), default=None)

    pending, state_error = _count_state(paths.state)
    if state_error:
        errors.append(state_error)

    if paths.vault.exists():
        instance_state = unit_state(paths)
    else:
        instance_state = "no-vault"

    return InstanceStatus(
        paths=paths,
        state=instance_state,
        last_event=last.get("event") if last else None,
        last_event_at=last.get("ts") if last else None,
        last_approval_at=latest.isoformat() if latest else None,
        approvals_24h=approvals_24h,
        pending=pending,
        error="; ".join(errors) or None,
    )


def status_all(
    names: list[str] | str | None = None,
) -> list[InstanceStatus]:
    """Statuses for every instance, or only the named ones.

    ``names`` is the filter a caller like ``autoduo watch --instance
    alice`` passes; a resolved path is accepted too so a caller that
    already holds one does not have to re-derive the name. ``None``
    means every instance on the host.
    """
    if names is None:
        items = list_instances()
    else:
        if isinstance(names, (str, InstancePaths)):
            names = [names]
        items = [
            item if isinstance(item, InstancePaths) else resolve(item)
            for item in names
        ]
    return [status(paths) for paths in items]
