"""Main poll-and-approve loop for autoduo.

Public surface:
- DuoBotDaemon: holds all state (vault, client, dedup set, log buffer).
- main(argv): CLI entry point used by `python -m autoduo.daemon`.

Loop:
  while not stop:
      if circuit_open: check cool-off; skip work if still open
      else:
          try list_transactions:
              on NetworkError: backoff, increment counter
              on AuthError: increment failure window, maybe open circuit
              on success: reset counters
          for each tx not in seen:
              approve(urgid), log result
      maybe emit heartbeat
      save seen.json every N seconds
      sleep(poll_interval) with backoff factored in

All injected via constructor for testability (clock, sleep, log stream,
state path). No global state.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import logging
import os
import pathlib
import signal
import sys
import tempfile
import threading
import time
from typing import Any, Callable, TextIO

from autoduo import redact, vault
from autoduo.client import (
    ApprovalRejected,
    AuthError,
    DeviceClient,
    NetworkError,
    ProtocolError,
    RateLimitedError,
)

logger = logging.getLogger("autoduo.daemon")

DEFAULT_BACKOFF_BASE_SECONDS = 1.0
DEFAULT_BACKOFF_CAP_SECONDS = 60.0
DEFAULT_HEARTBEAT_INTERVAL = 300.0  # 5 min
DEFAULT_STATE_SAVE_INTERVAL = 60.0
DEFAULT_CIRCUIT_COOLDOWN_SECONDS = 300.0  # 5 min of "alert and back off"

# Per-urgid retry budget for *transient* approval rejections.
MAX_APPROVE_ATTEMPTS = 3

# Logical rejection codes that are terminal for a transaction: retrying
# cannot change the outcome, so the urgid is remembered for ever (in a set
# that _evict_expired does not touch).
#   40032 — wrong step-up code / Verified Push requires a human.
# Expired and already-answered transactions are prevented from reaching
# approve() in the first place (expiration check + seen set); if Duo ever
# reports them as codes, add them here rather than guessing.
TERMINAL_REJECTION_CODES = frozenset({"40032"})

# Only these rejection codes are credential/auth class and may feed the
# global circuit.  Unknown codes deliberately get their own per-urgid
# counter instead of defaulting to an outage-amplifying 5-minute blackout.
CIRCUIT_AUTH_CODES = frozenset({"401", "403"})

# Distinct exit code for "another daemon already holds the vault lock".
EXIT_ALREADY_RUNNING = 5

# Bounded wait for the instance lock: short enough that a stale manual
# daemon cannot wedge a systemd start, long enough for a race to settle.
INSTANCE_LOCK_TIMEOUT_SECONDS = 1.0


class DuoBotDaemon:
    def __init__(
        self,
        client: DeviceClient,
        poll_interval: int = 5,
        push_ttl: int = 60,
        log_stream: TextIO | None = None,
        clock: Callable[[], datetime.datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        state_path: Any = None,
        max_auth_failures: int = 5,
        auth_failure_window: int = 300,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
        circuit_cooldown_seconds: float = DEFAULT_CIRCUIT_COOLDOWN_SECONDS,
        audit_log_stream: TextIO | None = None,
    ) -> None:
        self._client = client
        self._poll_interval = poll_interval
        self._push_ttl = push_ttl
        self._log = log_stream or sys.stdout
        self._audit_log = audit_log_stream  # approval-only stream; None = no separate audit file
        self._clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self._sleep = sleep or time.sleep
        self._state_path = state_path
        self._max_auth_failures = max_auth_failures
        self._auth_failure_window = auth_failure_window
        self._heartbeat_interval = heartbeat_interval
        self._circuit_cooldown = circuit_cooldown_seconds

        # State
        self._stop_event = threading.Event()  # retained for callers/tests
        self._stop_requested = False  # set by signal handlers (plain bool)
        self._reopen_requested = False  # SIGHUP flag; no reopen (copytruncate)
        self._seen: collections.OrderedDict[str, str] = collections.OrderedDict()
        # urgency_id → ISO timestamp when first seen; entries older than
        # push_ttl are evicted on access.
        # Terminal rejections live here instead: never evicted, so a
        # rejected transaction is genuinely never retried.
        self._terminal: set[str] = set()
        # Per-urgid transient-rejection attempt counter (survives un-mark).
        self._approve_attempts: dict[str, int] = {}
        self._auth_failures: collections.deque[datetime.datetime] = collections.deque(maxlen=64)
        self._consecutive_network_errors = 0
        self._backoff_seconds = DEFAULT_BACKOFF_BASE_SECONDS
        self._circuit_open_until: datetime.datetime | None = None
        self._last_heartbeat: datetime.datetime | None = None
        self._last_state_save: datetime.datetime | None = None
        self._last_approve: datetime.datetime | None = None
        # Set by _backoff(); read by run() so a cycle waits once, not twice.
        self._waited_this_cycle = False

        if state_path is not None:
            self._load_seen_state()

    # ------------------------------------------------------------------ logging

    def _emit(self, event: str, **fields: Any) -> None:
        record = {
            "ts": self._clock().isoformat(),
            "event": event,
            **fields,
        }
        self._log.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._log.flush()

    def _emit_audit(self, event: str, **fields: Any) -> None:
        """Emit an event to the audit log only (PII redacted).

        Used for events that contain user/IP/location from the transaction
        payload — these go to a separate, less-promiscuous log file
        (typically mode 0640 owned by the autoduo group).
        """
        if self._audit_log is None:
            return
        record = {
            "ts": self._clock().isoformat(),
            "event": event,
            **fields,
        }
        self._audit_log.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._audit_log.flush()

    # ------------------------------------------------------------------ dedup

    def _evict_expired(self) -> None:
        cutoff = self._clock() - datetime.timedelta(seconds=self._push_ttl)
        while self._seen:
            oldest_key = next(iter(self._seen))
            oldest_ts = self._seen[oldest_key]
            try:
                ts = datetime.datetime.fromisoformat(oldest_ts)
            except (TypeError, ValueError):
                # Bad entry (unparseable, or not even a string); evict.
                del self._seen[oldest_key]
                continue
            if ts.tzinfo is None:
                # A hand-edited file or an older build. Comparing a naive
                # datetime with an aware one raises TypeError, which used to
                # escape this method and kill the loop on every restart.
                # This daemon only ever means UTC.
                ts = ts.replace(tzinfo=datetime.timezone.utc)
            if ts < cutoff:
                del self._seen[oldest_key]
            else:
                break

    def _is_seen(self, urgid: str) -> bool:
        return urgid in self._seen or urgid in self._terminal

    def _mark_seen(self, urgid: str) -> None:
        self._seen[urgid] = self._clock().isoformat()

    def _unmark_seen(self, urgid: str) -> None:
        """Forget a provisional seen entry so the next poll retries it."""
        self._seen.pop(urgid, None)

    def _mark_terminal(self, urgid: str) -> None:
        """Remember a jamais-retried urgid for the process lifetime.

        Deliberately not timestamped: _evict_expired must never drop it,
        otherwise "seen, don't retry" becomes "retry every push_ttl".
        """
        self._terminal.add(urgid)
        self._seen.pop(urgid, None)

    # ------------------------------------------------------------------ state

    def _load_seen_state(self) -> None:
        if self._state_path is None:
            return
        p = pathlib.Path(self._state_path)
        try:
            content = p.read_text()
        except FileNotFoundError:
            return
        except OSError as e:
            self._emit("state_load_error", error=str(e))
            return
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            self._emit("state_corrupt", error=str(e))
            return
        if not isinstance(data, dict):
            self._emit("state_corrupt", error="not a dict")
            return
        self._seen.update(data)
        self._tighten_state_mode(p)
        self._emit("state_loaded", entries=len(self._seen))

    def _tighten_state_mode(self, p: pathlib.Path) -> None:
        """Make an inherited state file as private as a freshly written one.

        ``mkstemp`` protects only what this build writes. A state file left
        by an older build can be group- or world-readable, and it is not
        rewritten until the next approval -- which on a quiet account can be
        days. One chmod at load is cheaper than rewriting the file on a
        timer just to fix a mode.
        """
        try:
            if p.stat().st_mode & 0o077:
                p.chmod(0o600)
                self._emit("state_mode_tightened")
        except OSError as e:
            self._emit("state_mode_error", error=str(e))

    def _save_seen_state(self) -> None:
        if self._state_path is None:
            return
        p = pathlib.Path(self._state_path)
        tmp_name: str | None = None
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            # mkstemp: unique name, created 0600 regardless of umask. A
            # fixed .tmp name would collide with a crash leftover.
            fd, tmp_name = tempfile.mkstemp(
                dir=str(p.parent), prefix=p.name + ".", suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(self._seen))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, p)
            tmp_name = None
            dir_fd = os.open(p.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as e:
            self._emit("state_save_error", error=str(e))
            return
        finally:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
        self._last_state_save = self._clock()
        self._emit("state_saved", entries=len(self._seen))

    # ------------------------------------------------------------------ circuit

    def _circuit_is_open(self) -> bool:
        if self._circuit_open_until is None:
            return False
        if self._clock() >= self._circuit_open_until:
            self._circuit_open_until = None
            self._emit("circuit_closed")
            return False
        return True

    def _record_auth_failure(self) -> None:
        now = self._clock()
        self._auth_failures.append(now)
        # Drop entries outside the window.
        cutoff = now - datetime.timedelta(seconds=self._auth_failure_window)
        while self._auth_failures and self._auth_failures[0] < cutoff:
            self._auth_failures.popleft()
        if len(self._auth_failures) >= self._max_auth_failures:
            self._circuit_open_until = now + datetime.timedelta(seconds=self._circuit_cooldown)
            self._emit(
                "circuit_opened",
                failures=len(self._auth_failures),
                cooldown_seconds=self._circuit_cooldown,
                severity="high",
            )

    def _clear_auth_failures(self) -> None:
        """Clear the auth window after a cycle that saw no auth error.

        Deliberately separate from network-backoff reset: an approval-path
        auth error must not pin the network backoff (or vice versa).
        """
        self._auth_failures.clear()

    def _reset_network_state(self) -> None:
        """Clear transient/network backoff after a successful poll."""
        self._consecutive_network_errors = 0
        self._backoff_seconds = DEFAULT_BACKOFF_BASE_SECONDS

    def _note_poll_error(self, event: str, **fields: Any) -> None:
        """Emit a poll error and back off (network/protocol/unexpected)."""
        self._consecutive_network_errors += 1
        self._backoff_seconds = min(
            self._backoff_seconds * 2, DEFAULT_BACKOFF_CAP_SECONDS
        )
        self._emit(
            event,
            backoff_seconds=self._backoff_seconds,
            consecutive=self._consecutive_network_errors,
            **fields,
        )
        self._backoff(self._backoff_seconds)

    # ------------------------------------------------------------------ run

    def _is_expired(self, tx: dict[str, Any]) -> bool:
        expiration = tx.get("expiration")
        if expiration is None:
            return False
        try:
            expires_at = float(expiration)
        except (TypeError, ValueError):
            return False
        return self._clock().timestamp() >= expires_at

    def _handle_rejection(self, urgid: str, exc: ApprovalRejected) -> None:
        """Classify a protocol-level approval rejection.

        - terminal: never retried (set survives _evict_expired).
        - auth-class code: feeds the credential-scoped circuit.
        - anything else: retried under MAX_APPROVE_ATTEMPTS with backoff,
          then parked as terminal. No class opens the circuit by default.
        """
        self._classify_rejection(
            urgid, exc.code, exc.message, "approve_rejected"
        )

    def _handle_protocol_error(self, urgid: str, exc: ProtocolError) -> None:
        """A 2xx body we could not use: retry, then park.

        We cannot tell whether the approval landed, so this is not terminal;
        it is not a credential problem either, so it must not feed the
        circuit. The per-urgid retry budget keeps a broken far end from
        becoming an infinite loop.
        """
        self._classify_rejection(
            urgid, exc.code, exc.message, "approve_protocol_error"
        )

    def _classify_rejection(
        self, urgid: str, code: Any, message: str, event: str
    ) -> None:
        code = None if code is None else str(code)
        attempt = self._approve_attempts.get(urgid, 0) + 1
        self._approve_attempts[urgid] = attempt
        base = {"urgid": urgid, "code": code, "message": message}

        if code in TERMINAL_REJECTION_CODES:
            self._mark_terminal(urgid)
            self._approve_attempts.pop(urgid, None)
            self._emit(event, classification="terminal", **base)
            return

        if code in CIRCUIT_AUTH_CODES:
            self._unmark_seen(urgid)
            self._record_auth_failure()
            self._emit(event, classification="auth", **base)
            return

        if attempt >= MAX_APPROVE_ATTEMPTS:
            self._mark_terminal(urgid)
            self._approve_attempts.pop(urgid, None)
            self._emit(
                event, classification="exhausted", attempts=attempt, **base
            )
            return

        self._unmark_seen(urgid)
        self._backoff_seconds = min(
            DEFAULT_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)),
            DEFAULT_BACKOFF_CAP_SECONDS,
        )
        self._emit(
            event,
            classification="transient",
            attempt=attempt,
            retry_in_seconds=self._backoff_seconds,
            **base,
        )
        self._backoff(self._backoff_seconds)

    def emit_fatal(self, exc: BaseException) -> None:
        """Record a crash in the event log, where the dashboard can see it."""
        self._emit(
            "fatal_error",
            error=repr(exc),
            error_type=type(exc).__name__,
        )

    def emit_event(self, event: str, **fields: Any) -> None:
        """Emit one operator-facing event (startup checks, notices)."""
        self._emit(event, **fields)

    def _backoff(self, seconds: float) -> None:
        """Sleep once, and remember that this cycle already waited.

        ``run()`` adds ``poll_interval`` after every cycle. Without this
        flag an error path waited its backoff *and* the poll interval, so
        the log said ``backoff_seconds: 2`` and the daemon actually waited
        seven -- slower recovery than the number it printed.
        """
        self._waited_this_cycle = True
        self._sleep(seconds)

    def _bookkeeping(self, where: str, work: Callable[[], None]) -> None:
        """Run one piece of housekeeping without risking the loop.

        Eviction, state saving and heartbeats are not the product. A fault
        in any of them must cost one log line, not every future approval:
        ``Restart=always`` would otherwise turn a corrupt file into a crash
        loop whose only symptom is pushes nobody answers.
        """
        try:
            work()
        except Exception as e:  # noqa: BLE001 — housekeeping must not be fatal
            self._emit("loop_error", where=where, error=repr(e))

    def _run_once(self) -> None:
        """Run a single poll-and-approve cycle.

        Does NOT check the stop flag — the outer `run()` loop does that
        between cycles. Tests can call this directly without setting up
        a signal handler.
        """
        self._waited_this_cycle = False
        if self._circuit_is_open():
            # Sleep a fraction of the cooldown so we come back to check.
            self._backoff(min(self._poll_interval, 5.0))
            return

        had_auth_error = False
        self._bookkeeping("evict", self._evict_expired)

        # Poll.
        try:
            txs = self._client.list_transactions()
        except RateLimitedError as e:
            # 429 is rate limiting, not an auth failure: it must not feed
            # the credential-scoped circuit.
            self._emit("rate_limited", error=str(e))
            self._backoff(self._poll_interval)
            return
        except NetworkError as e:
            self._note_poll_error("network_error", error=str(e))
            return
        except AuthError as e:
            self._record_auth_failure()
            self._emit("auth_error", error=str(e))
            self._backoff(self._poll_interval)
            return
        except ProtocolError as e:
            self._note_poll_error(
                "protocol_error", code=e.code, error=e.message
            )
            return
        except Exception as e:  # noqa: BLE001 — daemon must never traceback
            self._note_poll_error("poll_unexpected_error", error=repr(e))
            return

        # A successful poll always resets transient/network backoff, but
        # auth failures are only cleared at the end of a cycle with none.
        self._reset_network_state()

        if txs:
            for tx in txs:
                if self._approve_transaction(tx):
                    had_auth_error = True
            self._bookkeeping("save_state", self._maybe_save_state)
        else:
            self._bookkeeping("heartbeat", self._maybe_heartbeat)

        if not had_auth_error:
            self._bookkeeping("clear_auth", self._clear_auth_failures)

    def _approve_transaction(self, tx: dict[str, Any]) -> bool:
        """Handle one transaction. Returns True if it caused an auth error."""
        urgid = tx.get("urgid")
        if not urgid:
            self._emit("tx_skipped", reason="missing urgid", keys=list(tx.keys()))
            return False
        if self._is_seen(urgid):
            self._emit("tx_deduped", urgid=urgid)
            return False
        if self._is_expired(tx):
            # Do not manufacture our own rejections for dead pushes.
            self._mark_terminal(urgid)
            self._emit(
                "tx_expired", urgid=urgid, expiration=tx.get("expiration")
            )
            return False

        self._emit("tx_seen", urgid=urgid, factor=tx.get("factor"))
        # Mark seen BEFORE the approve call to prevent duplicate approve if
        # the daemon is SIGKILL'd between the HTTP response and the next
        # seen.json flush. On retryable failure we un-mark.
        self._mark_seen(urgid)
        try:
            result = self._client.approve(urgid)
        except ApprovalRejected as e:
            # Must precede the bare Exception handler: rejections have
            # their own retry/terminal policy.
            self._handle_rejection(urgid, e)
            return False
        except ProtocolError as e:
            # A 2xx we could not read. Same budget as a transient
            # rejection: unknown outcome, so retry and then park.
            self._handle_protocol_error(urgid, e)
            return False
        except NetworkError as e:
            self._unmark_seen(urgid)
            self._note_poll_error("approve_network_error", urgid=urgid, error=str(e))
            return False
        except AuthError as e:
            self._unmark_seen(urgid)
            self._record_auth_failure()
            self._emit("approve_auth_error", urgid=urgid, error=str(e))
            return True
        except Exception as e:  # noqa: BLE001 — last-resort guard
            self._unmark_seen(urgid)
            self._emit("approve_unexpected_error", urgid=urgid, error=repr(e))
            return False

        self._approve_attempts.pop(urgid, None)
        self._emit("approve_ok", urgid=urgid, http=result.http_status)
        # The audit record is built from a redacted copy of the transaction:
        # screen_text, user_agent and raw attributes are dropped and an IP
        # is truncated. Today's field list is already safe, but the payload
        # grows and SECURITY.md promises this boundary, so it is applied
        # rather than assumed.
        safe_tx = redact.redact_transaction(tx)
        self._emit_audit(
            "approved",
            urgid=urgid,
            txid=safe_tx.get("txid"),
            factor=safe_tx.get("factor"),
            app=safe_tx.get("summary"),
            tx_type=safe_tx.get("type"),
        )
        self._last_approve = self._clock()
        return False

    def _maybe_heartbeat(self) -> None:
        if self._last_heartbeat is None:
            self._last_heartbeat = self._clock()
            return
        elapsed = (self._clock() - self._last_heartbeat).total_seconds()
        if elapsed >= self._heartbeat_interval:
            self._emit("heartbeat", seen=len(self._seen))
            self._last_heartbeat = self._clock()

    def _maybe_save_state(self) -> None:
        if self._state_path is None:
            return
        if self._last_state_save is None:
            self._save_seen_state()
            return
        elapsed = (self._clock() - self._last_state_save).total_seconds()
        if elapsed >= DEFAULT_STATE_SAVE_INTERVAL:
            self._save_seen_state()

    def run(self) -> int:
        """Run until SIGTERM/SIGINT. Returns process exit code."""
        self._install_signal_handlers()
        self._emit("daemon_starting", poll_interval=self._poll_interval, push_ttl=self._push_ttl)
        while not self._stop_requested and not self._stop_event.is_set():
            if self._reopen_requested:
                # Logging happens here, never in the signal handler.
                # logrotate uses copytruncate, so no reopen is performed.
                self._reopen_requested = False
                self._emit("sighup_received", reopen=False)
            self._run_once()
            if not self._stop_requested and not self._stop_event.is_set():
                if not self._waited_this_cycle:
                    self._sleep(self._poll_interval)
        self._save_seen_state()
        self._emit("daemon_stopped")
        return 0

    def _install_signal_handlers(self) -> Callable:
        def handler(signum, frame):
            # Plain bool assignment only: a signal handler runs on the
            # main thread between bytecodes, so taking a lock (Event.set)
            # or touching a buffered stream can deadlock/re-enter a write.
            self._stop_requested = True

        def sighup_handler(signum, frame):
            # Set a flag only; the main loop logs it. No file I/O here.
            self._reopen_requested = True

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGHUP, sighup_handler)
        return handler


# --- main() ----------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    from autoduo import __version__
    parser = argparse.ArgumentParser(
        prog="python -m autoduo.daemon",
        description="Run the Duo auto-accept daemon.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--version", action="version",
        version=f"autoduo {__version__}",
    )
    parser.add_argument("--vault", required=True, help="path to the sealed vault")
    parser.add_argument(
        "--passphrase-file", default=None,
        help="path to the vault passphrase (optional with --no-passphrase)",
    )
    parser.add_argument(
        "--no-passphrase", action="store_true", default=False,
        help="open vault with empty passphrase (file-permission protection only)",
    )
    parser.add_argument(
        "--log-file", default=None,
        help="write JSON event log to this file (default: stdout only)",
    )
    parser.add_argument(
        "--audit-log", default=None,
        help="write approval audit log (PII) to this file; mode 0640",
    )
    parser.add_argument(
        "--traffic-log", default=None,
        help="write HTTP traffic log to this file (verbose; default: disabled)",
    )
    parser.add_argument(
        "--state-file", default=None,
        help="path to the seen-urgid state file (default: in-memory only)",
    )
    parser.add_argument(
        "--poll-interval", type=int, default=5,
        help="seconds between polls (default: 5)",
    )
    parser.add_argument(
        "--push-ttl", type=int, default=60,
        help="seconds before a seen urgid is forgotten (default: 60)",
    )
    return parser


def _open_log_stream(args: argparse.Namespace):
    if args.log_file is None:
        return sys.stdout
    p = pathlib.Path(args.log_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    stream = p.open("a", buffering=1)  # line-buffered
    # The event log carries urgids and app names, so it is 0640 like the
    # audit log rather than whatever the caller's umask happens to give.
    try:
        os.chmod(p, 0o640)
    except OSError:
        pass
    return stream


def _open_audit_stream(args: argparse.Namespace):
    """Open audit log; mode 0640 for PII protection."""
    if args.audit_log is None:
        return None
    p = pathlib.Path(args.audit_log)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch(mode=0o640, exist_ok=True)
    os.chmod(p, 0o640)
    return p.open("a", buffering=1)


def _open_traffic_stream(args: argparse.Namespace):
    """Open HTTP traffic log; opt-in only (disk-fill risk)."""
    if args.traffic_log is None:
        return None
    p = pathlib.Path(args.traffic_log)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p.open("a", buffering=1)


def _read_passphrase_file(path: str) -> str:
    """Read a passphrase file, refusing group/world-readable modes.

    Duplicated on purpose from enroll.py: enroll.py is outside this
    lane's write scope, so the same invariant is enforced here inline.
    (The runtime side must not accept the exposure enrollment rejects.)
    """
    p = pathlib.Path(path)
    try:
        mode = p.stat().st_mode
    except OSError as e:
        raise ValueError(f"cannot stat passphrase file {p}: {e}") from e
    if mode & 0o077:
        raise ValueError(
            f"passphrase file {p} is world- or group-readable "
            f"(mode={oct(mode & 0o777)}); chmod 0400 before retrying"
        )
    try:
        return p.read_text().strip()
    except OSError as e:
        raise ValueError(f"cannot read passphrase file {p}: {e}") from e


def _acquire_instance_lock(vault_path: str | pathlib.Path) -> int:
    """Take the same bounded flock the vault writer uses (item 2.5).

    Returns a raw os.open fd; the caller must keep it open for the process
    lifetime. Closing it (or GC of a file object) would release the lock,
    which is why this never wraps the fd in a Python file object.
    """
    return vault.acquire_instance_lock(
        vault_path, timeout=INSTANCE_LOCK_TIMEOUT_SECONDS
    )


def _check_vault_mode(daemon_obj: "DuoBotDaemon", path: str) -> None:
    """Say so when the vault is readable by someone other than its owner.

    The vault is the only copy of the device key; with --no-passphrase the
    file mode *is* the whole protection. open_vault cannot refuse a loose
    file (an existing install would stop upgrading), but it must not be
    silent about it either.
    """
    try:
        mode = pathlib.Path(path).stat().st_mode & 0o777
    except OSError:
        return
    if mode & 0o077:
        daemon_obj.emit_event("vault_mode_insecure", mode=oct(mode))


def _run_guarded(daemon_obj: "DuoBotDaemon") -> int:
    """Run the loop and never let a traceback be the only record of a crash.

    ``Restart=always`` means a crash is not silent to systemd, only to the
    operator: the journal says ``Traceback`` and the dashboard says
    ``running`` until the next restart. Writing ``fatal_error`` to the event
    log puts the reason where it is already being read.
    """
    try:
        return daemon_obj.run()
    except Exception as e:  # noqa: BLE001 — last resort
        logger.exception("daemon loop crashed")
        daemon_obj.emit_fatal(e)
        # 6, not 5: 5 is "another copy holds the lock" and the units are
        # told not to restart it. A crash is worth another attempt.
        return 6


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    log_stream = _open_log_stream(args)
    audit_stream = _open_audit_stream(args)
    traffic_stream = _open_traffic_stream(args)

    # Single-instance guard: acquire before the (expensive) vault open so
    # the loser does no PBKDF2 and touches no state file.
    try:
        _instance_lock_fd = _acquire_instance_lock(args.vault)
    except OSError:
        logger.error(
            "another autoduo daemon is already running for %s "
            "(lock file is held); refusing to start",
            args.vault,
        )
        return EXIT_ALREADY_RUNNING
    # _instance_lock_fd is intentionally held until process exit.

    if args.no_passphrase:
        passphrase = None
    elif args.passphrase_file:
        try:
            passphrase = _read_passphrase_file(args.passphrase_file)
        except ValueError as e:
            logger.error("%s", e)
            return 4
    else:
        logger.error("must specify either --passphrase-file or --no-passphrase")
        return 4

    try:
        payload = vault.open_vault(args.vault, passphrase)
    except vault.VaultError as e:
        logger.error("cannot open vault: %s", e)
        return 4

    poll_interval = args.poll_interval
    push_ttl = args.push_ttl

    client = DeviceClient(
        host=payload["host"],
        pkey=payload["pkey"],
        akey=payload["akey"],
        private_key_pem=payload["private_key_pem"],
        traffic_log=traffic_stream,
    )

    daemon_obj = DuoBotDaemon(
        client=client,
        poll_interval=poll_interval,
        push_ttl=push_ttl,
        log_stream=log_stream,
        state_path=args.state_file,
        audit_log_stream=audit_stream,
    )
    _check_vault_mode(daemon_obj, args.vault)
    return _run_guarded(daemon_obj)


if __name__ == "__main__":
    sys.exit(main())