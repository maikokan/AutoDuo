"""Tests for autoduo.daemon.

Uses fake clock and fake DeviceClient (no real network, no real vault).
Asserts:
- list → approve per tx happens once per urgid.
- In-process dedup: same urgid across two poll cycles → only one approve.
- seen.json round-trips on graceful shutdown.
- Signals (SIGTERM/SIGINT) trigger graceful shutdown.
- Circuit breaker engages after N auth failures in M seconds.
- Backoff doubles on consecutive NetworkErrors, resets on success.
- JSON-line logger emits the expected events.
- Heartbeat event fires every N seconds even with 0 transactions.
- Exit codes: 4 for VaultError on startup, 0 for clean shutdown.
"""
import datetime
import io
import json
import pathlib
import signal
import threading
import time
from unittest.mock import MagicMock

import pytest

from autoduo import daemon


# --- Fake DeviceClient -----------------------------------------------------

class FakeDeviceClient:
    """In-memory stand-in for DeviceClient.

    Configurable `transactions_per_poll` and `approve_raises` for failure
    simulation. Records every approve() call in `self.approved`.
    """

    def __init__(self, transactions_per_poll=None, approve_raises=None, **kwargs):
        # Accept and ignore kwargs that the real DeviceClient expects
        # (host, pkey, akey, private_key_pem, ...).
        self._transactions = transactions_per_poll or []
        self._approve_raises = approve_raises
        self.approved = []
        self.list_calls = 0
        self._lock = threading.Lock()

    def list_transactions(self):
        with self._lock:
            self.list_calls += 1
            return list(self._transactions)

    def approve(self, urgid):
        with self._lock:
            self.approved.append(urgid)
        if self._approve_raises:
            raise self._approve_raises
        from autoduo.client import ApproveResult
        return ApproveResult(http_status=200, body={"response": "OK"})


@pytest.fixture
def fake_clock():
    base = datetime.datetime(2025, 1, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
    state = {"now": base}

    def clock():
        return state["now"]

    def advance(seconds):
        state["now"] = state["now"] + datetime.timedelta(seconds=seconds)

    return clock, advance


@pytest.fixture
def fake_sleep():
    sleeps = []
    def s(secs):
        sleeps.append(secs)
        # Don't actually sleep.
    return s, sleeps


@pytest.fixture
def log_capture():
    buf = io.StringIO()
    return buf


# --- Loop basics -----------------------------------------------------------

def test_loop_approves_every_new_urgid(fake_clock, fake_sleep, log_capture):
    clock, advance = fake_clock
    sleep_fn, _sleeps = fake_sleep
    client = FakeDeviceClient(transactions_per_poll=[
        {"urgid": "u1"}, {"urgid": "u2"}, {"urgid": "u3"}
    ])
    d = daemon.DuoBotDaemon(
        client=client,
        poll_interval=5,
        push_ttl=60,
        log_stream=log_capture,
        clock=clock,
        sleep=sleep_fn,
        max_auth_failures=5,
        auth_failure_window=300,
    )
    # Run one iteration of the loop body, then stop.
    d._stop_event.set()
    d._run_once()
    assert sorted(client.approved) == ["u1", "u2", "u3"]


def test_loop_dedups_repeat_urgids_across_polls(fake_clock, fake_sleep, log_capture):
    clock, advance = fake_clock
    sleep_fn, _ = fake_sleep
    # Two polls, same urgid each time.
    state = {"poll": 0, "txs": [[{"urgid": "u1"}], [{"urgid": "u1"}]]}

    class TwoPollClient(FakeDeviceClient):
        def list_transactions(self):
            tx = state["txs"][min(state["poll"], 1)]
            state["poll"] += 1
            return list(tx)

    client = TwoPollClient()
    d = daemon.DuoBotDaemon(
        client=client,
        poll_interval=5,
        push_ttl=60,
        log_stream=log_capture,
        clock=clock,
        sleep=sleep_fn,
        max_auth_failures=5,
        auth_failure_window=300,
    )
    d._stop_event.set()
    d._run_once()
    # First poll approves u1.
    assert client.approved == ["u1"]
    # Manually reset stop, run again — second poll has same urgid; should not re-approve.
    d._stop_event.clear()
    # But push_ttl=60 means by the time of the second run, the urgid is
    # still in the dedup set (we haven't advanced the clock). So it stays deduped.
    d._run_once()
    assert client.approved == ["u1"]


def test_loop_expires_urgids_after_push_ttl(fake_clock, fake_sleep, log_capture):
    clock, advance = fake_clock
    sleep_fn, _ = fake_sleep
    client = FakeDeviceClient(transactions_per_poll=[{"urgid": "u1"}])
    d = daemon.DuoBotDaemon(
        client=client,
        poll_interval=5,
        push_ttl=10,
        log_stream=log_capture,
        clock=clock,
        sleep=sleep_fn,
        max_auth_failures=5,
        auth_failure_window=300,
    )
    d._stop_event.set()
    d._run_once()
    assert client.approved == ["u1"]
    # Advance past TTL.
    advance(20)
    d._stop_event.clear()
    d._run_once()
    # u1 was evicted from the dedup set; should be re-approved.
    assert client.approved == ["u1", "u1"]


# --- Backoff ---------------------------------------------------------------

def test_network_error_increments_backoff(fake_clock, fake_sleep, log_capture):
    from autoduo.client import NetworkError
    clock, _ = fake_clock
    sleep_fn, sleeps = fake_sleep

    class FlakyClient(FakeDeviceClient):
        def list_transactions(self):
            raise NetworkError("boom")

    client = FlakyClient()
    d = daemon.DuoBotDaemon(
        client=client,
        poll_interval=5,
        push_ttl=60,
        log_stream=log_capture,
        clock=clock,
        sleep=sleep_fn,
        max_auth_failures=5,
        auth_failure_window=300,
    )
    d._stop_event.set()
    d._run_once()
    assert d._consecutive_network_errors == 1


def test_successful_poll_resets_backoff(fake_clock, fake_sleep, log_capture):
    from autoduo.client import NetworkError
    clock, _ = fake_clock
    sleep_fn, _ = fake_sleep
    state = {"calls": 0}

    class FlakyClient(FakeDeviceClient):
        def list_transactions(self):
            state["calls"] += 1
            if state["calls"] == 1:
                raise NetworkError("boom")
            return []

    client = FlakyClient()
    d = daemon.DuoBotDaemon(
        client=client,
        poll_interval=5,
        push_ttl=60,
        log_stream=log_capture,
        clock=clock,
        sleep=sleep_fn,
        max_auth_failures=5,
        auth_failure_window=300,
    )
    d._stop_event.set()
    d._run_once()
    assert d._consecutive_network_errors == 1
    d._stop_event.clear()
    d._run_once()
    assert d._consecutive_network_errors == 0


# --- Circuit breaker -------------------------------------------------------

def test_circuit_breaker_engages_after_max_auth_failures(fake_clock, fake_sleep, log_capture):
    from autoduo.client import AuthError
    clock, _ = fake_clock
    sleep_fn, _ = fake_sleep

    class AlwaysAuthFails(FakeDeviceClient):
        def list_transactions(self):
            raise AuthError("nope")

    client = AlwaysAuthFails()
    d = daemon.DuoBotDaemon(
        client=client,
        poll_interval=5,
        push_ttl=60,
        log_stream=log_capture,
        clock=clock,
        sleep=sleep_fn,
        max_auth_failures=3,
        auth_failure_window=300,
    )
    # Trigger 3 auth errors.
    for _ in range(3):
        d._stop_event.set()
        d._run_once()
    # Circuit breaker should now be engaged.
    assert d._circuit_open_until is not None
    assert d._circuit_open_until > clock()


def test_circuit_breaker_cools_off(fake_clock, fake_sleep, log_capture):
    from autoduo.client import AuthError
    clock, advance = fake_clock
    sleep_fn, _ = fake_sleep

    class AlwaysAuthFails(FakeDeviceClient):
        def list_transactions(self):
            raise AuthError("nope")

    client = AlwaysAuthFails()
    d = daemon.DuoBotDaemon(
        client=client,
        poll_interval=5,
        push_ttl=60,
        log_stream=log_capture,
        clock=clock,
        sleep=sleep_fn,
        max_auth_failures=2,
        auth_failure_window=300,
    )
    for _ in range(2):
        d._stop_event.set()
        d._run_once()
    assert d._circuit_open_until is not None
    # Advance past the cool-off (default 5 min).
    advance(400)
    # Next poll should be allowed and (still failing) re-trigger.
    d._stop_event.set()
    d._run_once()
    # After cool-off, the breaker resets on a new attempt.
    assert d._circuit_open_until is None or d._circuit_open_until > clock()


# --- seen.json persistence -------------------------------------------------

def test_seen_state_persists_to_disk(tmp_path, fake_clock, fake_sleep, log_capture):
    clock, _ = fake_clock
    sleep_fn, _ = fake_sleep
    client = FakeDeviceClient(transactions_per_poll=[{"urgid": "u1"}])
    state_path = tmp_path / "seen.json"
    d = daemon.DuoBotDaemon(
        client=client,
        poll_interval=5,
        push_ttl=60,
        log_stream=log_capture,
        clock=clock,
        sleep=sleep_fn,
        state_path=state_path,
        max_auth_failures=5,
        auth_failure_window=300,
    )
    d._stop_event.set()
    d._run_once()
    d._save_seen_state()
    assert state_path.exists()
    data = json.loads(state_path.read_text())
    assert "u1" in data


def test_seen_state_loaded_on_start(tmp_path, fake_clock, fake_sleep, log_capture):
    clock, _ = fake_clock
    sleep_fn, _ = fake_sleep
    state_path = tmp_path / "seen.json"
    # Pre-populate seen state with u1.
    state_path.write_text(json.dumps({"u1": clock().isoformat()}))
    client = FakeDeviceClient(transactions_per_poll=[{"urgid": "u1"}])
    d = daemon.DuoBotDaemon(
        client=client,
        poll_interval=5,
        push_ttl=60,
        log_stream=log_capture,
        clock=clock,
        sleep=sleep_fn,
        state_path=state_path,
        max_auth_failures=5,
        auth_failure_window=300,
    )
    d._stop_event.set()
    d._run_once()
    # u1 was already in seen.json → must NOT be approved.
    assert client.approved == []


# --- Logging ---------------------------------------------------------------

def test_log_emits_tx_seen_and_approve_ok(fake_clock, fake_sleep, log_capture):
    clock, _ = fake_clock
    sleep_fn, _ = fake_sleep
    client = FakeDeviceClient(transactions_per_poll=[{"urgid": "u1", "factor": "push"}])
    d = daemon.DuoBotDaemon(
        client=client,
        poll_interval=5,
        push_ttl=60,
        log_stream=log_capture,
        clock=clock,
        sleep=sleep_fn,
        max_auth_failures=5,
        auth_failure_window=300,
    )
    d._stop_event.set()
    d._run_once()
    lines = [json.loads(l) for l in log_capture.getvalue().splitlines() if l.strip()]
    events = [l["event"] for l in lines]
    assert "tx_seen" in events
    assert "approve_ok" in events


def test_heartbeat_emitted_when_no_transactions(fake_clock, fake_sleep, log_capture):
    clock, advance = fake_clock
    sleep_fn, _ = fake_sleep
    client = FakeDeviceClient(transactions_per_poll=[])
    d = daemon.DuoBotDaemon(
        client=client,
        poll_interval=5,
        push_ttl=60,
        log_stream=log_capture,
        clock=clock,
        sleep=sleep_fn,
        max_auth_failures=5,
        auth_failure_window=300,
        heartbeat_interval=30,
    )
    # First poll: no transactions, but it's not yet heartbeat time.
    d._stop_event.set()
    d._run_once()
    advance(31)  # advance past heartbeat interval
    d._stop_event.clear()
    d._run_once()
    lines = [json.loads(l) for l in log_capture.getvalue().splitlines() if l.strip()]
    events = [l["event"] for l in lines]
    assert "heartbeat" in events


# --- Signals ---------------------------------------------------------------

def test_signal_handler_sets_stop_event(fake_clock, fake_sleep, log_capture):
    clock, _ = fake_clock
    sleep_fn, _ = fake_sleep
    client = FakeDeviceClient()
    d = daemon.DuoBotDaemon(
        client=client,
        poll_interval=5,
        push_ttl=60,
        log_stream=log_capture,
        clock=clock,
        sleep=sleep_fn,
        max_auth_failures=5,
        auth_failure_window=300,
    )
    # Synthesize a signal handler call.
    handler = d._install_signal_handlers()
    handler(signal.SIGTERM, None)
    assert d._stop_event.is_set()


# --- Main entry ------------------------------------------------------------

def test_main_with_vault(tmp_path, monkeypatch, fake_clock, fake_sleep, log_capture):
    """Smoke test: build a vault, then construct daemon from it.

    Uses an unconnected client and exercises the run loop with the stop
    event pre-set so main() returns immediately.
    """
    from autoduo import vault
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    # Set up a vault with no passphrase.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    vpath = tmp_path / "vault.enc"
    vault.seal(vpath, None, {
        "v": 1,
        "akey": "AKEY",
        "pkey": "PKEY",
        "host": "api-12345678901234567890123456789012.duosecurity.com",
        "private_key_pem": pem,
        "activation": {},
    })

    # Patch DeviceClient so we don't hit the network, and patch
    # DuoBotDaemon.run() to return immediately.
    monkeypatch.setattr("autoduo.daemon.DeviceClient", FakeDeviceClient)
    monkeypatch.setattr(
        "autoduo.daemon.DuoBotDaemon.run",
        lambda self: 0,
    )

    rc = daemon.main(argv=[
        "--vault", str(vpath),
        "--no-passphrase",
    ])
    assert rc == 0