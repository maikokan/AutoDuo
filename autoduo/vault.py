"""Encrypted vault for Duo device credentials.

File format (little-endian, fixed-size header):

    offset  size  field
    ------  ----  -----
    0       4     magic bytes "DVB1" (Duo Vault Binary v1)
    4       4     schema_version (uint32 big-endian)
    8       32    PBKDF2 salt
    40      12    AES-GCM nonce
    52      N     ciphertext (N = len(payload) + 16 GCM tag)

The payload is JSON-encoded (UTF-8). The schema version is carried in the
header so a future on-disk format is refused with a clear message instead
of looking like a wrong passphrase.

Threat model: this file may be copied by an attacker. Without the
passphrase, AES-GCM + 600k PBKDF2 rounds should resist offline brute
force for years. Do NOT echo the passphrase or write it to disk in
plaintext. systemd LoadCredential is the recommended delivery channel.
"""
from __future__ import annotations

import fcntl
import json
import os
import pathlib
import tempfile
import time
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

MAGIC = b"DVB1"
SUPPORTED_SCHEMA_VERSION = 1
PBKDF2_ITERATIONS = 600_000
SALT_LEN = 32
NONCE_LEN = 12
KEY_LEN = 32  # AES-256

# Single-instance lock: same file used by the daemon and by seal(). A
# bounded wait keeps a stale daemon from failing a systemd start forever,
# while still making two writers a hard error.
LOCK_TIMEOUT_SECONDS = 1.0
LOCK_POLL_SECONDS = 0.1


def acquire_instance_lock(
    path: str | pathlib.Path,
    timeout: float = LOCK_TIMEOUT_SECONDS,
) -> int:
    """Take a bounded, exclusive flock beside `path`.

    Returns a raw fd that the caller must hold for as long as it needs
    mutual exclusion (closing it releases the lock). The lock file is
    never unlinked: unlink+recreate defeats mutual exclusion because the
    old inode stays locked while a new one is created. Raises OSError
    (BlockingIOError after `timeout`) when the lock is held elsewhere.
    """
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lock_path = p.with_name(p.name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(LOCK_POLL_SECONDS)
    except BaseException:
        os.close(fd)
        raise


class VaultError(Exception):
    """Raised on any vault open/seal failure (wrong passphrase, tamper,
    unknown schema version, I/O)."""


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_LEN,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return kdf.derive(passphrase.encode("utf-8"))


def seal(
    path: str | pathlib.Path,
    passphrase: str | None,
    payload: dict[str, Any],
) -> None:
    """Encrypt `payload` (a JSON-serializable dict) and write atomically.

    If `passphrase` is None, the vault is sealed with an empty passphrase
    (PBKDF2 derivation from "" + random salt). This means the vault is
    protected only by file permissions — anyone who can read the file can
    open it. Only use --no-passphrase on systems where you trust file
    permissions (e.g. a dedicated VPS with no multi-user access).

    The file-level schema version is taken from `payload["v"]` so future
    versions can self-describe.

    Creates the parent directory 0700 only when it did not already exist;
    an existing parent's mode is left alone. The file is 0600 regardless of
    caller umask. Atomic via write-to-tmp + rename so a crash mid-write
    can't corrupt an existing vault.
    """
    p = pathlib.Path(path)
    parent = p.parent
    created_parent = not parent.exists()
    parent.mkdir(parents=True, exist_ok=True)
    # Tighten only a directory this call created. Never chmod a caller's
    # shared directory (e.g. a group-readable log/data dir).
    if created_parent:
        os.chmod(parent, 0o700)

    if "v" not in payload:
        raise VaultError("payload must include 'v' (schema version)")
    schema_version = int(payload["v"])

    salt = os.urandom(SALT_LEN)
    nonce = os.urandom(NONCE_LEN)
    key = _derive_key(passphrase or "", salt)
    aead = AESGCM(key)

    plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    # AAD binds the ciphertext to the entire authenticated file header:
    # MAGIC (4) + schema_version (4) + salt (32) = 40 bytes of attacker-
    # uncontrolled context. Without this, an attacker who rewrites the
    # file could swap the salt or schema_version and still pass GCM.
    aad = MAGIC + schema_version.to_bytes(4, "big") + salt
    ciphertext = aead.encrypt(nonce, plaintext, associated_data=aad)

    header = MAGIC + schema_version.to_bytes(4, "big")
    blob = header + salt + nonce + ciphertext

    # Atomic, durable write: unique 0600 temp file (mkstemp, not a fixed
    # .tmp name — a crash leftover must not make future seals fail with
    # EEXIST), fsync the file, replace, then fsync the directory. Without
    # the directory fsync a power loss can leave a zero-length vault, and
    # this file is the only copy of the device key.
    #
    # Same flock as the daemon: enrollment must not swap the vault under a
    # running daemon.
    try:
        lock_fd = acquire_instance_lock(p)
    except OSError as e:
        raise VaultError(
            "vault is locked by a running daemon; stop it before enrolling"
        ) from e

    tmp_name: str | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(parent), prefix=p.name + ".", suffix=".tmp"
        )
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, p)
        tmp_name = None
        dir_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        os.chmod(p, 0o600)
    except Exception:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        raise
    finally:
        os.close(lock_fd)


def open_vault(
    path: str | pathlib.Path,
    passphrase: str | None,
) -> dict[str, Any]:
    """Decrypt and return the vault payload. Raises VaultError on any failure.

    If `passphrase` is None, opens with an empty passphrase (must match
    how the vault was sealed).
    """
    p = pathlib.Path(path)
    try:
        blob = p.read_bytes()
    except OSError as e:
        raise VaultError(f"cannot read vault: {e}") from e

    if len(blob) < len(MAGIC) + 4 + SALT_LEN + NONCE_LEN + 16:
        raise VaultError("vault file too short to be valid")

    if blob[:4] != MAGIC:
        raise VaultError("vault magic header mismatch (not a autoduo vault?)")

    schema = int.from_bytes(blob[4:8], "big")
    if schema != SUPPORTED_SCHEMA_VERSION:
        raise VaultError(
            f"vault schema version {schema} not supported "
            f"(this build supports {SUPPORTED_SCHEMA_VERSION})"
        )

    salt = blob[8 : 8 + SALT_LEN]
    nonce = blob[8 + SALT_LEN : 8 + SALT_LEN + NONCE_LEN]
    ciphertext = blob[8 + SALT_LEN + NONCE_LEN :]

    key = _derive_key(passphrase or "", salt)
    aead = AESGCM(key)
    # AAD must match seal() exactly: MAGIC + schema_version + salt.
    aad = MAGIC + schema.to_bytes(4, "big") + salt
    try:
        plaintext = aead.decrypt(nonce, ciphertext, associated_data=aad)
    except Exception as e:
        # AESGCM raises InvalidTag on auth failure; treat all decrypt
        # failures uniformly so we don't leak which check failed.
        raise VaultError("vault decryption failed (wrong passphrase or tampered file)") from e

    try:
        return json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise VaultError("vault payload is not valid JSON") from e
