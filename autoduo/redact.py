"""Secret-redaction utilities for logs.

These functions are the boundary between any sensitive data and the log
stream. Every log emission that might include secrets MUST go through
`redact_headers` and `redact_query` before serialization.

CRITICAL INVARIANT: the pkey and akey values are the only stable
identifiers Duo uses to tie a device to an account. Never expose any
bytes of either in any log line.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode

# Fields whose values are credentials and must never appear in logs.
SENSITIVE_QUERY_FIELDS = {"akey", "pkey", "app_install_id"}
SENSITIVE_HEADER_FIELDS = {"authorization", "x-duo-date"}


def _redact_credential(value: str) -> str:
    """Redact a credential to a safe debug form.

    For pkey/akey (the Duo device identifiers), expose ONLY the length.
    Never the first chars — the prefix is part of the credential.
    """
    if not value:
        return ""
    return f"<{len(value)} chars:hidden>"


def redact_query(query: str | dict | list[tuple[str, str]] | None) -> str:
    """Redact sensitive fields in a URL query string.

    Accepts raw query string, dict, or list of tuples (urllib.parse
    parse_qsl output). Returns a query string safe for logging.
    """
    if not query:
        return ""
    if isinstance(query, str):
        pairs = parse_qsl(query, keep_blank_values=True)
    elif isinstance(query, dict):
        pairs = list(query.items())
    else:
        pairs = list(query)

    redacted = []
    for k, v in pairs:
        if k.lower() in SENSITIVE_QUERY_FIELDS:
            redacted.append((k, _redact_credential(v)))
        else:
            redacted.append((k, v))
    return urlencode(redacted)


def redact_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Redact sensitive fields in an HTTP header dict.

    - Authorization: replaced with `Basic <redacted>` (no bytes leaked)
    - x-duo-date: replaced with `<present>` / `<absent>`
    - All other headers pass through unchanged.

    Returns a new dict; does not mutate the input.
    """
    if not headers:
        return {}
    out: dict[str, str] = {}
    for k, v in headers.items():
        kl = k.lower()
        if kl == "authorization":
            # Header value is "Basic <base64>". Strip everything after "Basic ".
            out[k] = "Basic <redacted>"
        elif kl == "x-duo-date":
            out[k] = "<redacted-timestamp>"
        else:
            out[k] = v
    return out


def redact_transaction(tx: dict[str, Any]) -> dict[str, Any]:
    """Redact PII fields from a Duo transaction before logging.

    Strips `screen_text`, `user_agent`, raw `attributes` (which contain
    user/IP/location in raw form). Keeps `urgid`, `txid`, `factor`,
    `app`, `ip` (truncated to /24).
    """
    safe: dict[str, Any] = {}
    for k, v in tx.items():
        if k in ("screen_text", "user_agent", "attributes"):
            # Skip — these contain raw PII (user, IP, location, etc.)
            continue
        elif k == "ip" and isinstance(v, str):
            # Truncate IP to /24 for v4, /48 for v6
            if ":" in v:
                parts = v.split(":")
                safe[k] = ":".join(parts[:3]) + ":…"
            else:
                parts = v.split(".")
                safe[k] = ".".join(parts[:3]) + ".0"
        else:
            safe[k] = v
    return safe