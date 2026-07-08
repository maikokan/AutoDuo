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
import threading
import time
from typing import Any, Callable, TextIO

from autoduo import vault
from autoduo.client import AuthError, DeviceClient, NetworkError

logger = logging.getLogger("autoduo.daemon")

DEFAULT_BACKOFF_BASE_SECONDS = 1.0
DEFAULT_BACKOFF_CAP_SECONDS = 60.0
DEFAULT_HEARTBEAT_INTERVAL = 300.0  # 5 min
DEFAULT_STATE_SAVE_INTERVAL = 60.0
DEFAULT_CIRCUIT_COOLDOWN_SECONDS = 300.0  # 5 min of "alert and back off"


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
        self._stop_event = threading.Event()
        self._seen: collections.OrderedDict[str, str] = collections.OrderedDict()
        # urgency_id → ISO timestamp when first seen; entries older than
        # push_ttl are evicted on access.
        self._auth_failures: collections.deque[datetime.datetime] = collections.deque(maxlen=64)
        self._consecutive_network_errors = 0
        self._backoff_seconds = DEFAULT_BACKOFF_BASE_SECONDS
        self._circuit_open_until: datetime.datetime | None = None
        self._last_heartbeat: datetime.datetime | None = None
        self._last_state_save: datetime.datetime | None = None
        self._last_approve: datetime.datetime | None = None

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
            except ValueError:
                # Bad entry; evict.
                del self._seen[oldest_key]
                continue
            if ts < cutoff:
                del self._seen[oldest_key]
            else:
                break

    def _is_seen(self, urgid: str) -> bool:
        return urgid in self._seen

    def _mark_seen(self, urgid: str) -> None:
        self._seen[urgid] = self._clock().isoformat()

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
        self._emit("state_loaded", entries=len(self._seen))

    def _save_seen_state(self) -> None:
        if self._state_path is None:
            return
        p = pathlib.Path(self._state_path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(json.dumps(self._seen))
            tmp.replace(p)
        except OSError as e:
            self._emit("state_save_error", error=str(e))
            return
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

    def _record_success(self) -> None:
        self._auth_failures.clear()
        self._consecutive_network_errors = 0
        self._backoff_seconds = DEFAULT_BACKOFF_BASE_SECONDS

    # ------------------------------------------------------------------ run

    def _run_once(self) -> None:
        """Run a single poll-and-approve cycle.

        Does NOT check _stop_event — the outer `run()` loop does that
        between cycles. Tests can call this directly without setting up
        a signal handler.
        """
        if self._circuit_is_open():
            # Sleep a fraction of the cooldown so we come back to check.
            self._sleep(min(self._poll_interval, 5.0))
            return

        self._evict_expired()

        # Poll.
        try:
            txs = self._client.list_transactions()
        except NetworkError as e:
            self._consecutive_network_errors += 1
            self._backoff_seconds = min(
                self._backoff_seconds * 2, DEFAULT_BACKOFF_CAP_SECONDS
            )
            self._emit(
                "network_error",
                error=str(e),
                backoff_seconds=self._backoff_seconds,
                consecutive=self._consecutive_network_errors,
            )
            self._sleep(self._backoff_seconds)
            return
        except AuthError as e:
            self._record_auth_failure()
            self._emit("auth_error", error=str(e))
            self._sleep(self._poll_interval)
            return

        # Success path.
        self._record_success()

        if not txs:
            self._maybe_heartbeat()
            return

        for tx in txs:
            urgid = tx.get("urgid")
            if not urgid:
                self._emit("tx_skipped", reason="missing urgid", keys=list(tx.keys()))
                continue
            if self._is_seen(urgid):
                self._emit("tx_deduped", urgid=urgid)
                continue
            self._emit("tx_seen", urgid=urgid, factor=tx.get("factor"))
            # Mark seen BEFORE the approve call to prevent duplicate
            # approve if the daemon is SIGKILL'd between the HTTP
            # response and the next seen.json flush. On failure we
            # un-mark so the next poll retries.
            self._mark_seen(urgid)
            try:
                result = self._client.approve(urgid)
            except NetworkError as e:
                del self._seen[urgid]  # retry on next poll
                self._consecutive_network_errors += 1
                self._backoff_seconds = min(
                    self._backoff_seconds * 2, DEFAULT_BACKOFF_CAP_SECONDS
                )
                self._emit(
                    "approve_network_error",
                    urgid=urgid,
                    error=str(e),
                    backoff_seconds=self._backoff_seconds,
                )
                self._sleep(self._backoff_seconds)
                return
            except AuthError as e:
                del self._seen[urgid]  # retry on next poll
                self._record_auth_failure()
                self._emit("approve_auth_error", urgid=urgid, error=str(e))
                continue
            except Exception as e:  # noqa: BLE001 — last-resort guard
                del self._seen[urgid]  # retry on next poll
                self._emit("approve_unexpected_error", urgid=urgid, error=repr(e))
                continue
            self._emit("approve_ok", urgid=urgid, http=result.http_status)
            # Audit log entry with redacted PII (separate, less-accessible log).
            self._emit_audit(
                "approved",
                urgid=urgid,
                txid=tx.get("txid"),
                factor=tx.get("factor"),
                app=tx.get("summary"),
                tx_type=tx.get("type"),
            )
            self._last_approve = self._clock()

        self._maybe_save_state()

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
        while not self._stop_event.is_set():
            self._run_once()
            if not self._stop_event.is_set():
                self._sleep(self._poll_interval)
        self._save_seen_state()
        self._emit("daemon_stopped")
        return 0

    def _install_signal_handlers(self) -> Callable:
        def handler(signum, frame):
            self._emit("signal_received", signum=int(signum))
            self._stop_event.set()
        def sighup_handler(signum, frame):
            """Reopen log file on SIGHUP — logrotate sends this after
            copying the old file. Replacing _log avoids writing to the
            renamed (orphaned) file."""
            self._emit("log_reopen_requested", signum=1)
            if self._log is not sys.stdout and hasattr(self._log, "name"):
                try:
                    new_stream = open(self._log.name, "a", buffering=1)
                    self._log.close()
                    self._log = new_stream
                    self._emit("log_reopen_ok")
                except OSError as e:
                    self._emit("log_reopen_failed", error=str(e))
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
    return p.open("a", buffering=1)  # line-buffered


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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    log_stream = _open_log_stream(args)
    audit_stream = _open_audit_stream(args)
    traffic_stream = _open_traffic_stream(args)

    if args.no_passphrase:
        passphrase = None
    elif args.passphrase_file:
        try:
            passphrase = pathlib.Path(args.passphrase_file).read_text().strip()
        except OSError as e:
            logger.error("cannot read passphrase file %s: %s", args.passphrase_file, e)
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
    )
    return daemon_obj.run()


if __name__ == "__main__":
    sys.exit(main())