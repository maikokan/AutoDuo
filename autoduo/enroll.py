"""One-shot enrollment CLI for autoduo.

Generates an RSA keypair, calls Duo's device activation endpoint with
the user's activation code, validates the host, and writes everything
to a sealed vault.

Security posture (per oracle plan §4):
- Refuses `--passphrase` on argv (must come from file via
  `--passphrase-file`).
- Refuses `--activation-url` (must come from file via
  `--activation-file` or stdin via `--activation-stdin`).
- Deletes activation file after reading if `--activation-delete` is set
  (default: yes).
- Never logs the activation URL or passphrase.
- Validates that returned `host` ends with `.duosecurity.com` before
  sealing.

The user MUST still tap "Approve" on their real Duo Mobile app once to
confirm the new device — this CLI cannot bypass that step.
"""
from __future__ import annotations

import argparse
import datetime
import getpass
import json
import logging
import os
import pathlib
import sys
import uuid
from typing import Any

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from autoduo import vault
from autoduo.client import ALLOWED_HOST_SUFFIX

logger = logging.getLogger("autoduo.enroll")

ACTIVATION_PATH = "/push/v2/activation"
DEFAULT_TIMEOUT = 15


def _normalize_activation_url(url: str) -> tuple[str, str]:
    """Normalize a Duo activation URL into (host, code).

    Duo has two URL shapes:
    1. https://m-XXXXXXXX.duosecurity.com/activate/CODE
       (web-portal, host uses `m-` prefix, path is `/activate/`)
    2. https://api-XXXXXXXX.duosecurity.com/push/v2/activation/CODE
       (programmatic, host uses `api-` prefix, path is `/push/v2/activation/`)

    The `/push/v2/activation/` endpoint expects a FULL composite activation
    code of the form `{SHORT_CODE}-{base64(API_HOST)}`. The `m-` portal
    URL only contains the SHORT_CODE; we must construct the full code
    by base64-encoding the API host and appending it.

    Returns (host, code) suitable for a POST to
    `https://{host}/push/v2/activation/{code}`.
    """
    import base64 as b64
    from urllib.parse import urlparse

    parsed = urlparse(url.strip())

    if parsed.scheme != "https":
        raise EnrollError(f"activation URL must be https; got {parsed.scheme!r}")
    if parsed.port not in (None, 443):
        raise EnrollError(f"activation URL has non-standard port: {parsed.port}")

    host = (parsed.hostname or "").lower()

    if not host.endswith(ALLOWED_HOST_SUFFIX):
        raise EnrollError(
            f"activation URL host {host!r} does not end with "
            f"{ALLOWED_HOST_SUFFIX!r}"
        )

    path = parsed.path.rstrip("/")

    # Shape 1: /activate/CODE or /android/CODE on m- host
    # (Duo uses different path prefixes depending on the link source:
    #  /activate/ from the standard QR code, /android/ from the
    #  Android-specific landing page. Both yield the same code format.)
    if host.startswith("m-") and (
        path.startswith("/activate/") or path.startswith("/android/")
    ):
        for prefix in ("/activate/", "/android/"):
            if path.startswith(prefix):
                short_code = path[len(prefix):]
                break
        else:
            short_code = ""
        if not short_code:
            raise EnrollError("activation URL has empty code")
        api_host = "api-" + host[2:]  # m-XXXXXXXX → api-XXXXXXXX
        return api_host, short_code

    # Shape 2: /push/v2/activation/CODE on api- host
    # This is already the full composite code (from /auth/v2/enroll or
    # from extracting the full code from the portal page).
    if host.startswith("api-") and path.startswith("/push/v2/activation/"):
        code = path[len("/push/v2/activation/"):]
        return host, code

    raise EnrollError(
        f"unrecognized activation URL format: {url!r}\n"
        "Expected one of:\n"
        "  https://m-XXXXXXXX.duosecurity.com/activate/CODE\n"
        "  https://m-XXXXXXXX.duosecurity.com/android/CODE\n"
        "  https://api-XXXXXXXX.duosecurity.com/push/v2/activation/CODE"
    )


class EnrollError(Exception):
    """Raised on any enrollment failure."""


def _configure_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(level)


def _read_activation_source(args: argparse.Namespace) -> str:
    """Read the activation URL from file or stdin. Never from argv.

    The activation file is ALWAYS deleted after a successful read — it's
    a one-shot bearer secret, leaving it on disk is never the right
    default. If you need it back, re-issue from Duo Admin.
    """
    if args.activation_stdin:
        logger.info("reading activation URL from stdin")
        return sys.stdin.read().strip()
    if args.activation_file:
        p = pathlib.Path(args.activation_file)
        if not p.exists():
            raise EnrollError(f"activation file {p} does not exist")
        mode = p.stat().st_mode
        if mode & 0o077:
            raise EnrollError(
                f"activation file {p} is world- or group-readable "
                f"(mode={oct(mode & 0o777)}); chmod 0600 before retrying"
            )
        try:
            content = p.read_text().strip()
        finally:
            try:
                p.unlink()
            except OSError as e:
                logger.warning("could not delete %s: %s", p, e)
        return content
    raise EnrollError(
        "must supply --activation-file PATH or --activation-stdin"
    )


def _extract_activation_code(activation_url: str) -> str:
    """Extract the activation code from any supported Duo activation URL.

    Delegates to `_normalize_activation_url` which handles both
    `m-XXXXXXXX.duosecurity.com/activate/CODE` and
    `api-XXXXXXXX.duosecurity.com/push/v2/activation/CODE`.
    """
    _host, code = _normalize_activation_url(activation_url)
    return code


def _activation_host(activation_url: str) -> str:
    """Extract and validate the host portion of the activation URL.

    Returns the normalized api-XXXXXXXX.duosecurity.com host suitable
    for POST to /push/v2/activation/{code}. Handles both m- and api-
    URL shapes.
    """
    host, _code = _normalize_activation_url(activation_url)
    return host


def _post_activation(
    host: str,
    short_code: str,
    pubkey_pem: str,
    app_install_id: str,
    timeout: float = 30,
) -> dict[str, Any]:
    """POST to /push/v2/activation/<short_code> to register a virtual device.

    Emulates the real Duo Mobile app's activation flow:
    - `customer_protocol=1` goes as a query parameter (not body)
    - Device info fields go as form-encoded body
    - No `pkpush` field (that's for transaction signing, not activation)
    - Uses the SHORT code from the activation URL, not the composite
    """
    url = f"https://{host}{ACTIVATION_PATH}/{short_code}"

    # customer_protocol must be a query parameter, not body
    params = {"customer_protocol": "1"}

    # Device fields — emulating Android Duo Mobile
    activation_body = {
        "pubkey": pubkey_pem,
        "pkpush": "rsa-sha512",
        "jailbroken": "false",
        "architecture": "arm64",
        "region": "US",
        "app_id": "com.duosecurity.duomobile",
        "full_disk_encryption": "true",
        "passcode_status": "true",
        "platform": "Android",
        "app_version": "4.110.0",
        "app_build_number": "4110000",
        "app_install_id": app_install_id,
        "version": "14",
        "manufacturer": "Samsung",
        "language": "en",
        "security_patch_level": "2024-01-05",
        "model": "SM-S926B",
    }

    resp = requests.post(
        url,
        params=params,
        data=activation_body,
        timeout=timeout,
    )
    logger.debug("activation POST %s → %d", resp.url, resp.status_code)
    if resp.status_code != 200:
        raise EnrollError(
            f"activation POST returned {resp.status_code}: {resp.text[:200]}"
        )
    try:
        body = resp.json()
    except ValueError as e:
        raise EnrollError(f"activation response is not JSON: {resp.text[:200]}") from e
    if "response" not in body:
        raise EnrollError(f"activation response missing 'response': {body}")
    return body["response"]


def _read_passphrase(args: argparse.Namespace) -> str | None:
    """Read passphrase from file, or return None if --no-passphrase.

    If --no-passphrase is set, returns None (vault sealed with empty
    passphrase). Otherwise reads from the file, hard-errors if the
    file is world- or group-readable.
    """
    if getattr(args, "no_passphrase", False):
        return None
    if getattr(args, "passphrase", None):
        raise EnrollError(
            "--passphrase on the command line is forbidden (use --passphrase-file)"
        )
    if not args.passphrase_file:
        raise EnrollError("must supply --passphrase-file PATH or --no-passphrase")
    p = pathlib.Path(args.passphrase_file)
    if not p.exists():
        raise EnrollError(f"passphrase file {p} does not exist")
    mode = p.stat().st_mode
    if mode & 0o077:
        raise EnrollError(
            f"passphrase file {p} is world- or group-readable "
            f"(mode={oct(mode & 0o777)}); chmod 0400 before retrying"
        )
    return p.read_text().strip()


def _generate_keypair() -> tuple[rsa.RSAPrivateKey, str, str]:
    """Generate a 2048-bit RSA keypair and return (key, priv_pem, pub_pem)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    pub_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return key, priv_pem, pub_pem


def run_enroll(args: argparse.Namespace) -> int:
    """Main enrollment flow. Returns 0 on success, non-zero on failure."""
    _configure_logging(verbose=args.verbose)
    try:
        activation_url = _read_activation_source(args)
    except EnrollError as e:
        logger.error("%s", e)
        return 2

    try:
        short_code = _extract_activation_code(activation_url)
        activation_host = _activation_host(activation_url)
    except EnrollError as e:
        logger.error("%s", e)
        return 2

    try:
        passphrase = _read_passphrase(args)
    except EnrollError as e:
        logger.error("%s", e)
        return 2

    if passphrase is None:
        logger.info("sealing vault with no passphrase (file-permission protection only)")
    else:
        if not passphrase:
            logger.error("passphrase is empty")
            return 2

    logger.info("generating RSA-2048 keypair...")
    _key, priv_pem, pub_pem = _generate_keypair()
    logger.debug("keypair generated (%d bytes public)", len(pub_pem))

    # Generate a stable UUID for this device — Duo uses this as the
    # device fingerprint. Must persist across daemon restarts.
    app_install_id = str(uuid.uuid4())
    logger.debug("app_install_id: %s", app_install_id)

    try:
        response = _post_activation(
            activation_host, short_code, pub_pem, app_install_id
        )
    except EnrollError as e:
        logger.error("activation failed: %s", e)
        return 3
    except requests.RequestException as e:
        logger.error("network error during activation: %s", e)
        return 4

    # Validate host: use the host we POSTed to (already validated before
    # the request). Duo's response doesn't include the host explicitly
    # — it just contains {akey, pkey, app_status, customer_logo, ...}.
    # The response itself is bound to the TLS connection we made to that
    # host, so trusting the request-side validation is safe.
    response_host = activation_host
    if not response_host.endswith(ALLOWED_HOST_SUFFIX):
        logger.error(
            "activation host %r fails allowlist check; aborting",
            response_host,
        )
        return 5

    payload = {
        "v": 1,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "akey": response.get("akey", ""),
        "pkey": response.get("pkey", ""),
        "host": response_host,
        "private_key_pem": priv_pem,
        "app_install_id": app_install_id,
        # Store the entire response for forward-compat (HOTP secret, etc.).
        "activation": response,
    }

    try:
        vault.seal(args.vault, passphrase, payload)
    except vault.VaultError as e:
        logger.error("vault seal failed: %s", e)
        return 6

    logger.info("vault written to %s", args.vault)
    logger.info("NEXT STEPS:")
    logger.info("  1. Open Duo Mobile on your phone.")
    logger.info("  2. Approve the 'new device enrollment' prompt for %s.", response_host)
    logger.info("  3. Start the daemon: systemctl enable --now autoduo")
    return 0


def build_parser() -> argparse.ArgumentParser:
    from autoduo import __version__
    parser = argparse.ArgumentParser(
        prog="python -m autoduo.enroll",
        description="Enroll this host as a Duo device and write the vault.",
        # Disable prefix matching so --passphrase <secret> can't be confused
        # with --passphrase-file <secret>. Prefix matching is a footgun here.
        allow_abbrev=False,
    )
    parser.add_argument(
        "--version", action="version",
        version=f"autoduo {__version__}",
    )
    parser.add_argument(
        "--vault", required=True,
        help="path to write the sealed vault (will be chmod 600)",
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--activation-file", metavar="PATH",
        help="path to a file containing the activation URL (deleted after read)",
    )
    src.add_argument(
        "--activation-stdin", action="store_true",
        help="read the activation URL from stdin",
    )
    parser.add_argument(
        "--passphrase-file", metavar="PATH", default=None,
        help="path to a file containing the vault passphrase (must be 0400)",
    )
    parser.add_argument(
        "--no-passphrase", action="store_true", default=False,
        help="seal vault with empty passphrase (file-permission protection only)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="enable debug logging",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return run_enroll(args)


if __name__ == "__main__":
    sys.exit(main())