"""Golden-vector tests for autoduo.protocol.

These tests are written FIRST (TDD) and lock the canonical-string format
and signing output against a fixed RSA keypair. They must pass against
ANY implementation of the device-protocol signing scheme.

Protocol shape (verified against DuoBreak reference at
github.com/JesseNaser/DuoBreak, duobreak.py:277-285):
  canonical = date + "\n" + method + "\n" + host.lower() + "\n" + path + "\n" + urlencode(params)
  signature = PKCS1_v1.5_sign(private_key, SHA-512(canonical))
  Authorization = "Basic " + base64(pkey + ":" + base64(signature))
  Date header = RFC 1123 format (also valid: RFC 2822 with -0000)
"""
import base64
import datetime
import email.utils
import pathlib

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from autoduo import protocol

GOLDEN_KEY_PATH = pathlib.Path(__file__).parent / "_golden_key.pem"


@pytest.fixture(scope="module")
def golden_private_key():
    return serialization.load_pem_private_key(
        GOLDEN_KEY_PATH.read_bytes(), password=None
    )


# --- canonical_request -----------------------------------------------------

def test_canonical_string_matches_golden_vector():
    """The canonical string must be exactly this byte sequence."""
    canonical = protocol.canonical_request(
        date="Wed, 21 Aug 2024 12:00:00 +0000",
        method="GET",
        host="api-12345678901234567890123456789012.duosecurity.com",
        path="/push/v2/device/transactions",
        params={
            "akey": "AAAA",
            "fips_status": "1",
            "hsm_status": "true",
            "pkpush": "rsa-sha512",
        },
    )
    expected = (
        "Wed, 21 Aug 2024 12:00:00 +0000\n"
        "GET\n"
        "api-12345678901234567890123456789012.duosecurity.com\n"
        "/push/v2/device/transactions\n"
        "akey=AAAA&fips_status=1&hsm_status=true&pkpush=rsa-sha512"
    )
    assert canonical == expected


def test_canonical_string_lowercases_host():
    canonical = protocol.canonical_request(
        date="X",
        method="GET",
        host="API-FOO.duosecurity.COM",
        path="/push/v2/device/transactions",
        params={},
    )
    # Host must be lowercased per DuoBreak reference and Duo convention.
    assert "api-foo.duosecurity.com" in canonical
    assert "API-FOO" not in canonical


def test_canonical_string_sorts_params_lexicographically():
    canonical = protocol.canonical_request(
        date="X",
        method="GET",
        host="h",
        path="/p",
        params={"zeta": "1", "alpha": "2", "mu": "3"},
    )
    # urllib.parse.urlencode sorts by default; alpha < mu < zeta
    assert canonical.endswith("\nalpha=2&mu=3&zeta=1")


def test_canonical_string_handles_empty_params():
    """GET with no params: last line is empty string (DuoBreak convention)."""
    canonical = protocol.canonical_request(
        date="X", method="GET", host="h", path="/p", params={}
    )
    # trailing newline then empty string
    assert canonical.endswith("\n/p\n")


def test_canonical_string_url_encodes_special_chars():
    canonical = protocol.canonical_request(
        date="X",
        method="POST",
        host="h",
        path="/p",
        params={"txid": "abc/def+ghi=?"},
    )
    # urlencode escapes /, +, =, ? — verify exactly
    assert "txid=abc%2Fdef%2Bghi%3D%3F" in canonical


# --- sign ------------------------------------------------------------------

def test_sign_matches_golden_signature(golden_private_key):
    canonical = (
        "Wed, 21 Aug 2024 12:00:00 +0000\n"
        "GET\n"
        "api-12345678901234567890123456789012.duosecurity.com\n"
        "/push/v2/device/transactions\n"
        "akey=AAAA&fips_status=1&hsm_status=true&pkpush=rsa-sha512"
    )
    sig = protocol.sign(golden_private_key, canonical)
    # Verify against the public half of the same key — this proves the
    # signature is a valid PKCS1 v1.5 / SHA-512 signature over `canonical`.
    pub = golden_private_key.public_key()
    pub.verify(sig, canonical.encode("ascii"), padding.PKCS1v15(), hashes.SHA512())
    # And it should be base64-decodable to the expected 256 bytes (2048-bit key).
    assert len(base64.b64encode(sig)) > 0
    assert len(sig) == 256


def test_sign_deterministic(golden_private_key):
    canonical = "a\nb\nc\nd\ne=1"
    s1 = protocol.sign(golden_private_key, canonical)
    s2 = protocol.sign(golden_private_key, canonical)
    assert s1 == s2


def test_sign_different_for_different_messages(golden_private_key):
    s1 = protocol.sign(golden_private_key, "a\nb\nc\nd\ne=1")
    s2 = protocol.sign(golden_private_key, "a\nb\nc\nd\ne=2")
    assert s1 != s2


# --- build_headers ---------------------------------------------------------

def test_build_headers_basic():
    headers = protocol.build_headers(
        pkey="PUBKEY_FINGERPRINT",
        signature_b64="AAAA",
        date_rfc1123="Wed, 21 Aug 2024 12:00:00 +0000",
        host="api-12345678901234567890123456789012.duosecurity.com",
    )
    assert headers["Authorization"] == "Basic " + base64.b64encode(
        b"PUBKEY_FINGERPRINT:AAAA"
    ).decode("ascii")
    assert headers["x-duo-date"] == "Wed, 21 Aug 2024 12:00:00 +0000"
    assert headers["host"] == "api-12345678901234567890123456789012.duosecurity.com"
    # DuoBreak uses lowercase header names; verify we match.
    # (note: requests is case-insensitive but the underlying wire is what matters)


def test_build_headers_omits_host_when_not_given():
    """`host` is HTTP/1.1 mandatory; requests sets it from the URL. We only
    include it in build_headers when explicitly pinned (some integrations
    proxy through a custom host header)."""
    headers = protocol.build_headers(
        pkey="PUBKEY", signature_b64="A", date_rfc1123="X"
    )
    assert "host" not in headers
    assert headers["Authorization"].startswith("Basic ")
    assert headers["x-duo-date"] == "X"


# --- now_rfc1123 -----------------------------------------------------------

def test_now_rfc1123_with_injected_clock():
    fixed = datetime.datetime(2024, 8, 21, 12, 0, 0, tzinfo=datetime.timezone.utc)
    formatted = protocol.now_rfc1123(clock=lambda: fixed)
    assert formatted == "Wed, 21 Aug 2024 12:00:00 +0000"


def test_now_rfc1123_default_clock_returns_valid_rfc1123():
    """Sanity check the default clock path returns something parseable."""
    s = protocol.now_rfc1123()
    # email.utils.parsedate_to_datetime accepts RFC 1123 / 2822.
    parsed = email.utils.parsedate_to_datetime(s)
    assert parsed is not None
    assert parsed.tzinfo is not None  # must include timezone


def test_now_rfc1123_matches_email_utils_formatdatetime():
    """Our formatter must match email.utils.format_datetime so the canonical
    string is identical regardless of which path produced it."""
    fixed = datetime.datetime(2025, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
    assert protocol.now_rfc1123(clock=lambda: fixed) == email.utils.format_datetime(fixed)