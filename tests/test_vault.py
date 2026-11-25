"""Tests for autoduo.vault.

Covers:
- Round-trip seal/open with the right passphrase returns the original payload.
- Wrong passphrase raises VaultError.
- Tampered ciphertext raises VaultError (AES-GCM auth tag).
- Tampered header (salt, nonce, version) raises VaultError.
- Schema-version mismatch is detected and reported.
- File permissions are 0600 / dir 0700 after init/seal.
"""
import pathlib
import stat

import pytest

from autoduo import vault


@pytest.fixture
def vault_path(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "vault.enc"


@pytest.fixture
def sample_payload() -> dict:
    return {
        "v": 1,
        "created_at": "2024-08-21T12:00:00+00:00",
        "akey": "FAKE_AKEY",
        "pkey": "FAKE_PKEY_FINGERPRINT",
        "host": "api-XXXXXXXX.duosecurity.com",
        "private_key_pem": "-----BEGIN PRIVATE KEY-----\nMIIE...\n-----END PRIVATE KEY-----\n",
        "activation": {"akey": "FAKE_AKEY", "pkey": "FAKE_PKEY", "host": "api-X"},
    }


def test_init_creates_file_with_correct_perms(vault_path, sample_payload):
    vault.seal(vault_path, "correct horse battery staple", sample_payload)
    mode = stat.S_IMODE(vault_path.stat().st_mode)
    assert mode == 0o600


def test_init_creates_parent_dir_with_0700(tmp_path, sample_payload):
    parent = tmp_path / "vdir"  # deliberately not pre-created
    p = parent / "vault.enc"
    vault.seal(p, "pp", sample_payload)
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700


def test_seal_does_not_chmod_an_existing_parent(tmp_path, sample_payload):
    """Never tighten a caller's shared directory (item 4 amendment)."""
    parent = tmp_path / "shared"
    parent.mkdir()
    parent.chmod(0o755)
    p = parent / "vault.enc"
    vault.seal(p, "pp", sample_payload)
    assert stat.S_IMODE(parent.stat().st_mode) == 0o755


def test_seal_then_open_round_trip(vault_path, sample_payload):
    passphrase = "correct horse battery staple"
    vault.seal(vault_path, passphrase, sample_payload)
    opened = vault.open_vault(vault_path, passphrase)
    assert opened == sample_payload


def test_seal_then_open_round_trip_no_passphrase(vault_path, sample_payload):
    """Passphrase=None seals with empty string — still round-trips."""
    vault.seal(vault_path, None, sample_payload)
    opened = vault.open_vault(vault_path, None)
    assert opened == sample_payload


def test_open_with_wrong_passphrase_raises(vault_path, sample_payload):
    vault.seal(vault_path, "right", sample_payload)
    with pytest.raises(vault.VaultError):
        vault.open_vault(vault_path, "wrong")


def test_open_with_tampered_ciphertext_raises(vault_path, sample_payload):
    vault.seal(vault_path, "pp", sample_payload)
    raw = vault_path.read_bytes()
    # Flip a bit in the ciphertext (last byte before tag, or middle of file)
    tampered = bytearray(raw)
    # AES-GCM: format is [4 bytes version][32 bytes salt][12 bytes nonce][N bytes ciphertext][16 bytes tag]
    # Tamper with a ciphertext byte (offset somewhere in the middle).
    if len(tampered) > 50:
        tampered[50] ^= 0x01
    vault_path.write_bytes(bytes(tampered))
    with pytest.raises(vault.VaultError):
        vault.open_vault(vault_path, "pp")


def test_open_with_tampered_salt_raises(vault_path, sample_payload):
    vault.seal(vault_path, "pp", sample_payload)
    raw = bytearray(vault_path.read_bytes())
    # Salt is right after the 4-byte version header.
    if len(raw) > 10:
        raw[5] ^= 0xFF  # flip a byte in the salt
    vault_path.write_bytes(bytes(raw))
    with pytest.raises(vault.VaultError):
        vault.open_vault(vault_path, "pp")


def test_open_with_truncated_file_raises(vault_path, sample_payload):
    vault.seal(vault_path, "pp", sample_payload)
    vault_path.write_bytes(vault_path.read_bytes()[:30])
    with pytest.raises(vault.VaultError):
        vault.open_vault(vault_path, "pp")


def test_schema_version_v2_raises_when_v1_only_supported(vault_path, sample_payload):
    """A vault claiming schema v2 with no migration path must be rejected."""
    payload_v2 = dict(sample_payload, v=2)
    vault.seal(vault_path, "pp", payload_v2)
    with pytest.raises(vault.VaultError, match="schema"):
        vault.open_vault(vault_path, "pp")


def test_tampered_schema_version_bytes_raise(vault_path, sample_payload):
    """AAD must include schema_version so flipping the bytes in the file
    header fails GCM even if the explicit version check were skipped."""
    vault.seal(vault_path, "pp", sample_payload)
    raw = bytearray(vault_path.read_bytes())
    # Flip a bit in the schema_version field (bytes 4..7).
    if len(raw) > 5:
        raw[5] ^= 0x01
    vault_path.write_bytes(bytes(raw))
    with pytest.raises(vault.VaultError):
        vault.open_vault(vault_path, "pp")


def test_seal_overwrites_existing(vault_path, sample_payload):
    """Re-sealing an existing vault must replace (not append)."""
    vault.seal(vault_path, "pp", sample_payload)
    new_payload = dict(sample_payload, akey="ROTATED")
    vault.seal(vault_path, "pp", new_payload)
    assert vault.open_vault(vault_path, "pp")["akey"] == "ROTATED"


def test_pbkdf2_iterations_at_least_600k():
    """Sanity: the PBKDF2 iteration count must match OWASP 2024 guidance
    (>= 600_000 for SHA-256). Catches accidental downgrades."""
    assert vault.PBKDF2_ITERATIONS >= 600_000


# --- Atomic / private write path (ROBUSTNESS 2.2) -------------------------

def test_temp_file_is_0600_and_parent_0700_before_replace(
    tmp_path, sample_payload, monkeypatch
):
    """At the moment of the rename, the temp file and its directory are
    already private — no 0644/0755 window exists."""
    from autoduo import vault as v
    parent = tmp_path / "vdir"
    p = parent / "vault.enc"
    seen = {}
    real_replace = v.os.replace

    def spy(src, dst):
        src_p, dst_p = pathlib.Path(src), pathlib.Path(dst)
        seen["tmp_mode"] = stat.S_IMODE(src_p.stat().st_mode)
        seen["parent_mode"] = stat.S_IMODE(dst_p.parent.stat().st_mode)
        seen["same_dir"] = src_p.parent == dst_p.parent
        real_replace(src, dst)

    monkeypatch.setattr(v.os, "replace", spy)
    v.seal(p, "pp", sample_payload)
    assert seen == {
        "tmp_mode": 0o600,
        "parent_mode": 0o700,
        "same_dir": True,
    }
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_stale_fixed_name_tmp_does_not_block_seal(
    tmp_path, sample_payload
):
    """A crash-left vault.enc.tmp must not make every future seal fail:
    the temp name is unique (mkstemp), not a fixed .tmp name."""
    p = tmp_path / "vault.enc"
    (tmp_path / "vault.enc.tmp").write_bytes(b"leftover from a crash")
    vault.seal(p, "pp", sample_payload)
    assert vault.open_vault(p, "pp") == sample_payload


def test_interrupted_replace_keeps_old_vault_and_leaves_no_tmp(
    tmp_path, sample_payload, monkeypatch
):
    from autoduo import vault as v
    p = tmp_path / "vault.enc"
    vault.seal(p, "pp", sample_payload)
    good = p.read_bytes()

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(v.os, "replace", boom)
    with pytest.raises(OSError):
        v.seal(p, "pp", dict(sample_payload, akey="ROTATED"))

    # Old vault is intact and readable; no partial temp file was left.
    assert p.read_bytes() == good
    assert vault.open_vault(p, "pp")["akey"] == "FAKE_AKEY"
    assert sorted(x.name for x in tmp_path.iterdir()) == [
        "vault.enc",
        "vault.enc.lock",  # acquired then closed; never unlinked
    ]
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_seal_refuses_while_lock_is_held(tmp_path, sample_payload):
    """Enrollment must not swap the vault under a running daemon."""
    p = tmp_path / "vault.enc"
    vault.seal(p, "pp", sample_payload)
    fd = vault.acquire_instance_lock(p)
    try:
        with pytest.raises(vault.VaultError, match="locked"):
            vault.seal(p, "pp", dict(sample_payload, akey="ROTATED"))
    finally:
        import os
        os.close(fd)
    # The running vault was not replaced.
    assert vault.open_vault(p, "pp")["akey"] == "FAKE_AKEY"