"""Encrypted vault for Duo device credentials.

File format (little-endian, fixed-size header):

    offset  size  field
    ------  ----  -----
    0       4     magic bytes "DVB1" (Duo Vault Binary v1)
    4       4     schema_version (uint32 big-endian)
    8       32    PBKDF2 salt
    40      12    AES-GCM nonce
    52      N     ciphertext (N = len(payload) + 16 GCM tag)

The payload is JSON-encoded (UTF-8). Schema versioning lets us migrate
later without breaking existing vaults.

Threat model: this file may be copied by an attacker. Without the
passphrase, AES-GCM + 600k PBKDF2 rounds should resist offline brute
force for years. Do NOT echo the passphrase or write it to disk in
plaintext. systemd LoadCredential is the recommended delivery channel.
"""
from __future__ import annotations

import json
import os
import pathlib
import stat
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

    Sets parent dir to 0700 and file to 0600 regardless of caller umask.
    Atomic via write-to-tmp + rename so a crash mid-write can't corrupt
    an existing vault.
    """
    p = pathlib.Path(path)
    parent = p.parent
    parent.mkdir(parents=True, exist_ok=True)

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

    # Write atomically: tmp file in same dir, then rename.
    tmp = p.with_suffix(p.suffix + ".tmp")
    old_umask = os.umask(0o077)  # so file is created 0600 even if caller umask is wider
    try:
        tmp.write_bytes(blob)
        os.rename(tmp, p)
    finally:
        os.umask(old_umask)

    # Belt-and-suspenders: enforce perms in case the rename preserved
    # an existing mode (it shouldn't, but chmod is cheap).
    os.chmod(p, 0o600)
    os.chmod(parent, 0o700)


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


def migrate(payload: dict[str, Any]) -> dict[str, Any]:
    """Hook for future schema migrations. No-op at v1."""
    return payload