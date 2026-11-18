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
import functools
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
        return ApproveResult(http_status=200, body={"stat": "OK", "response": "OK"})


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

def test_signal_handler_sets_stop_flag_without_logging(fake_clock, fake_sleep, log_capture):
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
    assert d._stop_requested is True
    # Handlers must not do I/O (no logging mid-write / no locks).
    assert log_capture.getvalue() == ""


# --- Main entry ------------------------------------------------------------

def test_main_with_vault(tmp_path, monkeypatch, fake_clock, fake_sleep, log_capture):
    """Smoke test: build a vault, then construct daemon from it.

    Exercises one real cycle so each opened stream is actually written.
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

    audit_path = tmp_path / "audit.log"
    state_path = tmp_path / "seen.json"

    # Patch DeviceClient so we don't hit the network. The partial gives the
    # constructed client one transaction to approve.
    monkeypatch.setattr(
        "autoduo.daemon.DeviceClient",
        functools.partial(
            FakeDeviceClient, transactions_per_poll=[{"urgid": "u1"}]
        ),
    )

    def one_cycle(self):
        self._run_once()
        return 0

    monkeypatch.setattr("autoduo.daemon.DuoBotDaemon.run", one_cycle)

    rc = daemon.main(argv=[
        "--vault", str(vpath),
        "--no-passphrase",
        "--audit-log", str(audit_path),
        "--state-file", str(state_path),
    ])
    assert rc == 0
    # The audit stream is opened *and used* (2.1 regression).
    audit_text = audit_path.read_text()
    assert audit_text.strip()
    assert "approved" in audit_text
    # The state stream is opened *and used* (4.1 regression).
    state_text = state_path.read_text()
    assert state_text.strip()
    assert "u1" in json.loads(state_text)


def test_second_instance_exits_5(tmp_path, monkeypatch):
    """A second daemon must refuse to start while the lock is held."""
    from autoduo import vault

    vpath = tmp_path / "vault.enc"
    vault.seal(vpath, None, {"v": 1, "akey": "A", "pkey": "P", "host": "h"})

    fd = daemon._acquire_instance_lock(vpath)
    try:
        monkeypatch.setattr(
            "autoduo.daemon.DuoBotDaemon.run", lambda self: 0
        )
        monkeypatch.setattr(
            daemon, "INSTANCE_LOCK_TIMEOUT_SECONDS", 0.2
        )
        rc = daemon.main(argv=["--vault", str(vpath), "--no-passphrase"])
    finally:
        import os
        os.close(fd)
    assert rc == daemon.EXIT_ALREADY_RUNNING == 5


def test_network_backoff_not_pinned_by_approval_auth_error(
    fake_clock, fake_sleep, log_capture
):
    """An approval auth error must not leave network backoff at the cap."""
    from autoduo.client import AuthError, NetworkError
    clock, _ = fake_clock
    sleep_fn, _ = fake_sleep
    polls = {"n": 0}

    class Mixed(FakeDeviceClient):
        def list_transactions(self):
            polls["n"] += 1
            if polls["n"] == 1:
                raise NetworkError("blip")
            return [{"urgid": "u1"}]

        def approve(self, urgid):
            raise AuthError("401")

    d = _make_daemon(Mixed(), clock, sleep_fn, log_capture, max_auth_failures=10)
    d._run_once()  # network error -> backoff 2
    assert d._backoff_seconds == 2.0
    d._run_once()  # poll OK resets network; approval auth-fails
    assert d._backoff_seconds == 1.0
    assert d._consecutive_network_errors == 0
    assert len(d._auth_failures) == 1


def test_independent_vaults_have_independent_locks(tmp_path, monkeypatch):
    import os
    from autoduo import vault

    va = tmp_path / "a" / "vault.enc"
    vb = tmp_path / "b" / "vault.enc"
    payload = {
        "v": 1, "akey": "A", "pkey": "P",
        "host": "api-x.duosecurity.com", "private_key_pem": "ignored",
    }
    vault.seal(va, None, payload)
    vault.seal(vb, None, payload)

    fd_b = daemon._acquire_instance_lock(vb)
    try:
        monkeypatch.setattr("autoduo.daemon.DeviceClient", FakeDeviceClient)
        monkeypatch.setattr("autoduo.daemon.DuoBotDaemon.run", lambda self: 0)
        rc = daemon.main(argv=["--vault", str(va), "--no-passphrase"])
    finally:
        os.close(fd_b)
    assert rc == 0


def test_world_readable_passphrase_file_rejected(tmp_path, monkeypatch):
    from autoduo import vault

    vpath = tmp_path / "vault.enc"
    vault.seal(vpath, None, {"v": 1, "akey": "A", "pkey": "P", "host": "h"})
    pass_file = tmp_path / "passphrase.txt"
    pass_file.write_text("secret")
    pass_file.chmod(0o644)

    monkeypatch.setattr("autoduo.daemon.DuoBotDaemon.run", lambda self: 0)
    rc = daemon.main(argv=[
        "--vault", str(vpath),
        "--passphrase-file", str(pass_file),
    ])
    assert rc == 4


# --- Wave A regressions: visibility & classification -----------------------

def _events(log_capture):
    return [
        json.loads(line)["event"]
        for line in log_capture.getvalue().splitlines()
        if line.strip()
    ]


def _make_daemon(client, clock, sleep_fn, log_capture, audit=None, **kw):
    return daemon.DuoBotDaemon(
        client=client,
        poll_interval=5,
        push_ttl=kw.pop("push_ttl", 60),
        log_stream=log_capture,
        clock=clock,
        sleep=sleep_fn,
        audit_log_stream=audit,
        max_auth_failures=kw.pop("max_auth_failures", 5),
        auth_failure_window=300,
        **kw,
    )


def test_protocol_error_on_poll_is_emitted_and_backs_off(
    fake_clock, fake_sleep, log_capture
):
    from autoduo.client import ProtocolError
    clock, _ = fake_clock
    sleep_fn, sleeps = fake_sleep

    class FailPoll(FakeDeviceClient):
        def list_transactions(self):
            raise ProtocolError("stat FAIL", code=40301)

    d = _make_daemon(FailPoll(), clock, sleep_fn, log_capture)
    d._run_once()
    lines = [
        json.loads(line)
        for line in log_capture.getvalue().splitlines()
        if line.strip()
    ]
    assert "protocol_error" in [line["event"] for line in lines]
    err = [line for line in lines if line["event"] == "protocol_error"][0]
    assert err["code"] == 40301
    assert err["error"] == "stat FAIL"
    assert sleeps  # backed off rather than falling through to an empty poll


def test_unexpected_poll_error_is_caught_and_backs_off(
    fake_clock, fake_sleep, log_capture
):
    clock, _ = fake_clock
    sleep_fn, sleeps = fake_sleep

    class BoomPoll(FakeDeviceClient):
        def list_transactions(self):
            raise RuntimeError("boom")

    d = _make_daemon(BoomPoll(), clock, sleep_fn, log_capture)
    d._run_once()  # must not raise
    assert "poll_unexpected_error" in _events(log_capture)
    assert sleeps


def test_rejected_approval_is_not_ok_and_not_audited(
    fake_clock, fake_sleep, log_capture
):
    from autoduo.client import ApprovalRejected
    clock, _ = fake_clock
    sleep_fn, _ = fake_sleep
    audit = io.StringIO()
    client = FakeDeviceClient(
        transactions_per_poll=[{"urgid": "u1"}],
        approve_raises=ApprovalRejected("wrong step-up code", code="40032"),
    )
    d = _make_daemon(client, clock, sleep_fn, log_capture, audit=audit)
    d._run_once()
    events = _events(log_capture)
    assert "approve_rejected" in events
    assert "approve_ok" not in events
    assert audit.getvalue() == ""


def test_terminal_rejection_not_retried_after_push_ttl(
    fake_clock, fake_sleep, log_capture
):
    from autoduo.client import ApprovalRejected
    clock, advance = fake_clock
    sleep_fn, _ = fake_sleep
    client = FakeDeviceClient(
        transactions_per_poll=[{"urgid": "u1"}],
        approve_raises=ApprovalRejected("step-up", code="40032"),
    )
    d = _make_daemon(client, clock, sleep_fn, log_capture, push_ttl=60)
    d._run_once()
    assert client.approved == ["u1"]
    advance(120)  # well past push_ttl
    d._run_once()
    # Terminal set is not evicted by _evict_expired: no second POST.
    assert client.approved == ["u1"]


def test_transient_rejection_retried_up_to_attempt_cap(
    fake_clock, fake_sleep, log_capture
):
    from autoduo.client import ApprovalRejected
    clock, advance = fake_clock
    sleep_fn, _ = fake_sleep
    client = FakeDeviceClient(
        transactions_per_poll=[{"urgid": "u1"}],
        approve_raises=ApprovalRejected("unknown", code="99999"),
    )
    d = _make_daemon(client, clock, sleep_fn, log_capture)
    for _ in range(6):
        d._run_once()
        advance(5)
    assert client.approved == ["u1", "u1", "u1"]  # 3 attempts, then parked
    assert "u1" in d._terminal


def test_five_unrelated_rejections_do_not_open_circuit(
    fake_clock, fake_sleep, log_capture
):
    from autoduo.client import ApprovalRejected
    clock, _ = fake_clock
    sleep_fn, _ = fake_sleep
    client = FakeDeviceClient(
        transactions_per_poll=[{"urgid": f"u{i}"} for i in range(5)],
        approve_raises=ApprovalRejected("unknown", code="99999"),
    )
    d = _make_daemon(client, clock, sleep_fn, log_capture, max_auth_failures=5)
    d._run_once()
    assert len(client.approved) == 5
    assert d._circuit_open_until is None


def test_approval_auth_errors_feed_circuit_across_cycles(
    fake_clock, fake_sleep, log_capture
):
    """A successful poll must not erase an approval's AuthError (3.1)."""
    from autoduo.client import AuthError
    clock, _ = fake_clock
    sleep_fn, _ = fake_sleep
    client = FakeDeviceClient(
        transactions_per_poll=[{"urgid": "u1"}],
        approve_raises=AuthError("401"),
    )
    d = _make_daemon(client, clock, sleep_fn, log_capture, max_auth_failures=3)
    for _ in range(3):
        d._run_once()
    assert d._circuit_open_until is not None
    # Circuit is credential-scoped; it opened from the approvals.
    assert len(client.approved) == 3


def test_expired_transaction_is_skipped_and_parked(
    fake_clock, fake_sleep, log_capture
):
    clock, advance = fake_clock
    sleep_fn, _ = fake_sleep
    expired_at = clock().timestamp() - 1
    client = FakeDeviceClient(
        transactions_per_poll=[
            {"urgid": "u1", "expiration": expired_at}
        ]
    )
    d = _make_daemon(client, clock, sleep_fn, log_capture)
    d._run_once()
    assert client.approved == []
    assert "tx_expired" in _events(log_capture)
    advance(120)
    d._run_once()
    assert client.approved == []


def test_rate_limited_poll_does_not_feed_auth_circuit(
    fake_clock, fake_sleep, log_capture
):
    """429 is backpressure, not auth: 10 bursts must not open the circuit."""
    from autoduo.client import RateLimitedError
    clock, _ = fake_clock
    sleep_fn, _ = fake_sleep

    class AlwaysLimited(FakeDeviceClient):
        def list_transactions(self):
            raise RateLimitedError("429")

    d = _make_daemon(
        AlwaysLimited(), clock, sleep_fn, log_capture, max_auth_failures=1
    )
    for _ in range(10):
        d._run_once()
    assert d._circuit_open_until is None
    assert len(d._auth_failures) == 0


def test_state_save_preserves_shared_dir_mode(
    tmp_path, fake_clock, fake_sleep, log_capture
):
    """seen.json goes into an existing (possibly shared) directory: the
    daemon must not chmod that directory, only create a 0600 file."""
    clock, _ = fake_clock
    sleep_fn, _ = fake_sleep
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o755)
    state_path = shared / "seen.json"

    d = _make_daemon(
        FakeDeviceClient(transactions_per_poll=[{"urgid": "u1"}]),
        clock, sleep_fn, log_capture, state_path=state_path,
    )
    d._run_once()
    d._save_seen_state()

    import stat as stat_mod
    assert stat_mod.S_IMODE(shared.stat().st_mode) == 0o755
    assert stat_mod.S_IMODE(state_path.stat().st_mode) == 0o600
    assert json.loads(state_path.read_text())["u1"]


def test_network_backoff_not_pinned_by_approval_auth_error(
    fake_clock, fake_sleep, log_capture
):
    """A network blip plus an approval AuthError must not leave backoff at
    its cap: network and auth state are reset independently."""
    from autoduo.client import AuthError, NetworkError
    clock, _ = fake_clock
    sleep_fn, _ = fake_sleep

    class Mixed(FakeDeviceClient):
        mode = "net"

        def list_transactions(self):
            if self.mode == "net":
                raise NetworkError("blip")
            return list(self._transactions)

        def approve(self, urgid):
            self.approved.append(urgid)
            raise AuthError("401")

    client = Mixed(transactions_per_poll=[{"urgid": "u1"}])
    d = _make_daemon(client, clock, sleep_fn, log_capture, max_auth_failures=10)

    d._run_once()  # network error -> backoff 2
    assert d._backoff_seconds == 2.0

    client.mode = "ok"
    d._run_once()  # successful poll resets network, approval auth-fails
    assert d._backoff_seconds == 1.0
    assert len(d._auth_failures) == 1

    client.mode = "net"
    d._run_once()  # next blip backs off from base, not from the cap
    assert d._backoff_seconds == 2.0


def test_independent_vaults_have_independent_locks(tmp_path, monkeypatch):
    """Multiple accounts are independent instances: a held lock for vault B
    must not block instance A (lock path is derived from --vault)."""
    from autoduo import vault

    vault_a = tmp_path / "account-a" / "vault.enc"
    vault_b = tmp_path / "account-b" / "vault.enc"
    payload = {
        "v": 1, "akey": "A", "pkey": "P", "host": "h",
        "private_key_pem": "unused",
    }
    vault.seal(vault_a, None, payload)
    vault.seal(vault_b, None, payload)

    lock_a = vault_a.with_name(vault_a.name + ".lock")
    lock_b = vault_b.with_name(vault_b.name + ".lock")
    assert lock_a != lock_b

    fd_b = daemon._acquire_instance_lock(vault_b)
    try:
        monkeypatch.setattr(
            "autoduo.daemon.DeviceClient", FakeDeviceClient
        )
        monkeypatch.setattr("autoduo.daemon.DuoBotDaemon.run", lambda self: 0)
        rc = daemon.main(argv=["--vault", str(vault_a), "--no-passphrase"])
    finally:
        import os
        os.close(fd_b)
    assert rc == 0