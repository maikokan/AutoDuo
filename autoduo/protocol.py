"""Duo device-protocol request signing.

Split into 4 functions for unit testability; the only production caller is
`client._signed_request()`. The canonical-string format and signing scheme
are verified against the open-source DuoBreak reference
(github.com/JesseNaser/DuoBreak, duobreak.py:277-285).
"""
from __future__ import annotations

import base64
import datetime
import email.utils
import urllib.parse
from typing import Any, Mapping

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def now_rfc1123(clock=None) -> str:
    """Return current UTC time in RFC 1123 format (matches RFC 2822 here).

    `clock` is an injection point for tests; defaults to a callable
    returning `datetime.datetime.now(timezone.utc)`.
    """
    if clock is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    else:
        now = clock()
    return email.utils.format_datetime(now)


def canonical_request(
    date: str,
    method: str,
    host: str,
    path: str,
    params: Mapping[str, Any],
) -> str:
    """Build the canonical request string per Duo device protocol.

    Format: date + "\\n" + method + "\\n" + host.lower() + "\\n" + path + "\\n" + urlencode(params)

    urlencode() sorts keys lexicographically and URL-encodes values, matching
    DuoBreak's reference behavior. Params may be empty (yields trailing
    newline + empty last line).
    """
    return (
        date + "\n"
        + method + "\n"
        + host.lower() + "\n"
        + path + "\n"
        + urllib.parse.urlencode(sorted(params.items()))
    )


def sign(private_key: rsa.RSAPrivateKey, canonical: str) -> bytes:
    """Sign the canonical string with PKCS1 v1.5 / SHA-512.

    Returns the raw signature bytes. Caller is responsible for base64-encoding.
    """
    return private_key.sign(
        canonical.encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA512(),
    )


def build_headers(
    pkey: str,
    signature_b64: str,
    date_rfc1123: str,
    host: str | None = None,
) -> dict[str, str]:
    """Build the HTTP headers Duo expects on a signed device-protocol request.

    - Authorization: Basic base64(pkey + ":" + base64(signature))
    - x-duo-date: the same date used in the canonical string
    - host: optional, set when caller wants it pinned (some DuoBreak
      variants include it; Duo accepts either)
    """
    token = base64.b64encode(f"{pkey}:{signature_b64}".encode("ascii")).decode("ascii")
    headers = {
        "Authorization": f"Basic {token}",
        "x-duo-date": date_rfc1123,
    }
    if host is not None:
        headers["host"] = host
    return headers