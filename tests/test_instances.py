"""Tests for autoduo.instances: paths, names and status discovery."""
import datetime
import json
import stat
import subprocess

import pytest

from autoduo import instances


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """Point the module at a temp root and keep the state cache cold."""
    data = tmp_path / "lib"
    logs = tmp_path / "log"
    monkeypatch.setattr(instances, "DATA_ROOT", data)
    monkeypatch.setattr(instances, "LOG_ROOT", logs)
    instances.clear_state_cache()
    yield data, logs
    instances.clear_state_cache()


def _fake_systemctl(monkeypatch, stdout="inactive\n", calls=None):
    def run(cmd, **kwargs):
        assert cmd[:2] == ["systemctl", "is-active"]
        if calls is not None:
            calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    monkeypatch.setattr(instances.subprocess, "run", run)


def _record(event, when, **fields):
    return json.dumps({"ts": when.isoformat(), "event": event, **fields})


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _sealed_instance(name="alice"):
    paths = instances.resolve(name)
    instances.ensure_dirs(paths)
    paths.vault.write_text("sealed")
    return paths


# --- paths ---------------------------------------------------------------

def test_default_keeps_the_legacy_paths(roots):
    data, logs = roots
    paths = instances.resolve("default")
    assert paths.legacy is True
    assert paths.vault == data / "vault.enc"
    assert paths.state == data / "seen.json"
    assert paths.lock == data / "vault.lock"
    assert paths.daemon_log == logs / "daemon.log"
    assert paths.audit_log == logs / "audit.log"


def test_named_instance_has_its_own_tree(roots):
    data, logs = roots
    paths = instances.resolve("alice")
    assert paths.legacy is False
    assert paths.vault == data / "instances" / "alice" / "vault.enc"
    assert paths.state == data / "instances" / "alice" / "seen.json"
    assert paths.lock == data / "instances" / "alice" / "vault.lock"
    assert paths.daemon_log == logs / "alice" / "daemon.log"
    assert paths.audit_log == logs / "alice" / "audit.log"


def test_instances_never_share_a_vault_or_a_unit(roots):
    alice = instances.resolve("alice")
    bob = instances.resolve("bob")
    assert alice.vault != bob.vault
    assert alice.daemon_log != bob.daemon_log
    assert instances.unit_name(alice) != instances.unit_name(bob)


def test_unit_names(roots):
    assert instances.unit_name(instances.resolve("default")) == "autoduo"
    assert instances.unit_name(instances.resolve("alice")) == "autoduo@alice"


def test_actual_lock_matches_the_vault_module(roots):
    paths = instances.resolve("alice")
    assert paths.actual_lock == paths.vault.parent / "vault.enc.lock"


# --- name validation -----------------------------------------------------

@pytest.mark.parametrize(
    "name",
    ["a", "default", "alice", "a_b-c1", "0instance", "x" * 32],
)
def test_valid_names_pass_through(name):
    assert instances.validate_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        " ",
        "../etc",
        "../../etc/passwd",
        "..",
        ".",
        "a/b",
        "/etc/passwd",
        "Alice",
        "ALICE",
        "a b",
        "-a",
        "_a",
        "a.b",
        "a\nb",
        "x" * 33,
        "alice;rm",
    ],
)
def test_unsafe_names_are_rejected_before_any_path(name):
    with pytest.raises(ValueError):
        instances.validate_name(name)


def test_unsafe_name_never_reaches_resolve(roots):
    with pytest.raises(ValueError):
        instances.resolve("../../etc")


# --- directories ---------------------------------------------------------

def test_ensure_dirs_uses_the_intended_modes(roots):
    paths = instances.resolve("alice")
    instances.ensure_dirs(paths)

    vault_mode = stat.S_IMODE(paths.vault.parent.stat().st_mode)
    log_mode = stat.S_IMODE(paths.daemon_log.parent.stat().st_mode)
    assert vault_mode == 0o700
    assert log_mode == 0o750


def test_ensure_dirs_for_the_legacy_instance(roots):
    data, logs = roots
    instances.ensure_dirs(instances.resolve("default"))
    assert stat.S_IMODE(data.stat().st_mode) == 0o700
    assert stat.S_IMODE(logs.stat().st_mode) == 0o750


def test_ensure_dirs_is_idempotent(roots):
    paths = instances.resolve("alice")
    instances.ensure_dirs(paths)
    instances.ensure_dirs(paths)
    assert paths.vault.parent.is_dir()


# --- discovery -----------------------------------------------------------

def test_list_instances_puts_default_first_and_skips_odd_dirs(roots):
    data, _logs = roots
    (data / "instances" / "bob").mkdir(parents=True)
    (data / "instances" / "alice").mkdir()
    (data / "instances" / "Bad Name").mkdir()
    (data / "instances" / "not-a-dir").write_text("x")

    names = [paths.name for paths in instances.list_instances()]
    assert names == ["default", "alice", "bob"]


def test_list_instances_without_a_root_is_just_default(roots):
    names = [paths.name for paths in instances.list_instances()]
    assert names == ["default"]


# --- status --------------------------------------------------------------

def test_status_missing_files_are_empty_not_fatal(roots, monkeypatch):
    _fake_systemctl(monkeypatch, stdout="inactive\n")
    row = instances.status(instances.resolve("alice"))

    assert row.state == "no-vault"
    assert row.last_event is None
    assert row.last_event_at is None
    assert row.last_approval_at is None
    assert row.approvals_24h == 0
    assert row.pending == 0
    assert row.error is None


def test_status_running_instance(roots, monkeypatch):
    _fake_systemctl(monkeypatch, stdout="active\n")
    assert instances.status(_sealed_instance()).state == "running"


@pytest.mark.parametrize(
    "stdout, expected",
    [
        ("active\n", "running"),
        ("activating\n", "running"),
        ("inactive\n", "stopped"),
        ("failed\n", "failed"),
        ("weird\n", "unknown"),
        ("", "unknown"),
    ],
)
def test_state_vocabulary(roots, monkeypatch, stdout, expected):
    _fake_systemctl(monkeypatch, stdout=stdout)
    assert instances.status(_sealed_instance()).state == expected


def test_state_is_unknown_when_systemctl_is_missing(roots, monkeypatch):
    def missing(cmd, **kwargs):
        raise FileNotFoundError("systemctl not installed")

    monkeypatch.setattr(instances.subprocess, "run", missing)
    assert instances.status(_sealed_instance()).state == "unknown"


def test_state_is_cached_for_five_seconds(roots, monkeypatch):
    calls = []
    _fake_systemctl(monkeypatch, stdout="active\n", calls=calls)
    paths = _sealed_instance()

    assert instances.status(paths).state == "running"
    assert instances.status(paths).state == "running"
    assert len(calls) == 1


def test_status_survives_a_truncated_last_line(roots, monkeypatch):
    _fake_systemctl(monkeypatch, stdout="active\n")
    paths = _sealed_instance()
    now = _now()
    paths.daemon_log.write_text(
        _record("heartbeat", now) + "\n" + '{"ts":"2026-11-18T00:0'
    )

    row = instances.status(paths)
    assert row.last_event == "heartbeat"
    assert row.error is None


def test_status_reads_only_the_tail_of_a_large_log(roots, monkeypatch):
    _fake_systemctl(monkeypatch, stdout="active\n")
    paths = _sealed_instance()
    now = _now()
    # The bulk line has no trailing newline inside the tail window, so
    # the partial first line is dropped along with its approval.
    padding = "x" * (instances.TAIL_BYTES + 100) + "\n"
    paths.daemon_log.write_text(
        padding
        + _record("approve_ok", now, urgid="old") + "\n"
        + _record("heartbeat", now) + "\n"
    )

    row = instances.status(paths)
    assert row.last_event == "heartbeat"
    assert row.last_approval_at is not None


def test_status_counts_approvals_and_the_window(roots, monkeypatch):
    _fake_systemctl(monkeypatch, stdout="active\n")
    paths = _sealed_instance()
    now = _now()
    recent = now - datetime.timedelta(hours=1)
    old = now - datetime.timedelta(hours=30)
    paths.daemon_log.write_text(
        "\n".join([
            _record("approve_ok", old, urgid="a"),
            _record("approve_ok", recent, urgid="b"),
            _record("heartbeat", recent, seen=0),
        ]) + "\n"
    )
    # The audit log repeats the same approval; it must not double count.
    paths.audit_log.write_text(
        _record("approved", recent, urgid="b", txid="t1") + "\n"
    )

    row = instances.status(paths)
    assert row.approvals_24h == 1
    assert row.last_event == "heartbeat"
    assert row.last_approval_at is not None
    assert row.last_approval_at.startswith(recent.isoformat()[:16])


def test_status_keeps_the_last_approval_even_when_old(roots, monkeypatch):
    _fake_systemctl(monkeypatch, stdout="inactive\n")
    paths = _sealed_instance()
    old = _now() - datetime.timedelta(hours=30)
    paths.daemon_log.write_text(_record("approve_ok", old, urgid="a") + "\n")

    row = instances.status(paths)
    assert row.approvals_24h == 0
    assert row.last_approval_at is not None


def test_status_reads_pending_from_seen_json(roots, monkeypatch):
    _fake_systemctl(monkeypatch, stdout="active\n")
    paths = _sealed_instance()
    paths.state.write_text(json.dumps({"u1": "t", "u2": "t"}))

    assert instances.status(paths).pending == 2


def test_status_reports_corrupt_seen_json(roots, monkeypatch):
    _fake_systemctl(monkeypatch, stdout="active\n")
    paths = _sealed_instance()
    paths.state.write_text('{"unterminated": ')

    row = instances.status(paths)
    assert row.pending == 0
    assert row.error is not None
    assert "seen.json" in row.error


def test_status_reports_an_unreadable_log_not_a_traceback(roots, monkeypatch):
    _fake_systemctl(monkeypatch, stdout="active\n")
    paths = _sealed_instance()
    paths.daemon_log.mkdir()  # a directory where a file belongs

    row = instances.status(paths)
    assert row.last_event is None
    assert row.error is not None
    assert "daemon.log" in row.error


def test_status_all_covers_every_instance(roots, monkeypatch):
    _fake_systemctl(monkeypatch, stdout="inactive\n")
    data, _logs = roots
    (data / "instances" / "alice").mkdir(parents=True)
    (data / "instances" / "bob").mkdir()

    rows = instances.status_all()
    assert [row.paths.name for row in rows] == ["default", "alice", "bob"]
    assert all(isinstance(row.paths, instances.InstancePaths) for row in rows)


def test_status_all_accepts_a_names_filter(roots, monkeypatch):
    _fake_systemctl(monkeypatch, stdout="inactive\n")

    rows = instances.status_all(names=["alice"])
    assert [row.paths.name for row in rows] == ["alice"]


def test_status_all_caches_systemctl_across_refreshes(roots, monkeypatch):
    calls = []
    _fake_systemctl(monkeypatch, stdout="active\n", calls=calls)
    for name in ("alice", "bob"):
        _sealed_instance(name)

    instances.status_all()
    instances.status_all()
    # One query per unit, not one per instance per frame.
    assert len(calls) == 2

