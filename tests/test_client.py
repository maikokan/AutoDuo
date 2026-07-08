"""Tests for autoduo.client.

Uses a MockTransport that intercepts `requests.Session.send` so we never
hit the network. Asserts:
- URL is well-formed.
- Authorization header is well-formed Basic pkey:base64sig.
- Date header is RFC 1123.
- 429 → uses Retry-After, single retry.
- 5xx / network → exponential backoff.
- Other 4xx → raises AuthError, no retry.
- Host allowlist enforced in __init__.
- list_transactions returns parsed JSON.
- approve POSTs answer=approve with correct URL.
"""
import base64
import json
import time
import urllib.parse
from unittest.mock import MagicMock

import pytest
import requests

from autoduo import client


# --- Mock transport --------------------------------------------------------

class MockTransport:
    """Replaces requests.Session.send with a programmable response queue.

    Each call pops the next (status, headers, body) tuple from
    `self.responses`. Records each call in `self.calls` for assertions.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.sleeps = []

    def __call__(self, request, **kwargs):
        self.calls.append(request)
        return self._make_response(*self.responses.pop(0))

    def _make_response(self, status, headers=None, body=None):
        resp = requests.Response()
        resp.status_code = status
        resp.headers.update(headers or {})
        if body is not None:
            resp._content = json.dumps(body).encode()
        else:
            resp._content = b""
        return resp


@pytest.fixture
def golden_key_pem():
    from pathlib import Path
    return Path(__file__).parent / "_golden_key.pem"


@pytest.fixture
def device_client(golden_key_pem):
    from cryptography.hazmat.primitives import serialization
    key = serialization.load_pem_private_key(golden_key_pem.read_bytes(), password=None)
    return client.DeviceClient(
        host="api-12345678901234567890123456789012.duosecurity.com",
        pkey="FAKE_PKEY_FINGERPRINT",
        akey="FAKE_AKEY",
        private_key_pem=golden_key_pem.read_bytes().decode(),
        sleep=lambda s: None,  # don't actually sleep in tests
    )


# --- Host allowlist --------------------------------------------------------

def test_host_allowlist_rejects_evil_host(golden_key_pem):
    with pytest.raises(client.AuthError, match="host"):
        client.DeviceClient(
            host="evil.example.com",
            pkey="X",
            akey="X",
            private_key_pem=golden_key_pem.read_bytes().decode(),
        )


def test_host_allowlist_accepts_duosecurity_suffix(golden_key_pem):
    c = client.DeviceClient(
        host="api-12345678901234567890123456789012.duosecurity.com",
        pkey="X",
        akey="X",
        private_key_pem=golden_key_pem.read_bytes().decode(),
    )
    assert c.host == "api-12345678901234567890123456789012.duosecurity.com"


def test_host_allowlist_rejects_duosecurity_lookalike(golden_key_pem):
    # "duosecurity.com.evil.example" ends with duosecurity.com substring,
    # but the suffix check should require the FULL .duosecurity.com suffix.
    with pytest.raises(client.AuthError):
        client.DeviceClient(
            host="api-12345678901234567890123456789012.duosecurity.com.evil.example",
            pkey="X",
            akey="X",
            private_key_pem=golden_key_pem.read_bytes().decode(),
        )


def test_host_allowlist_rejects_bare_suffix(golden_key_pem):
    """`.duosecurity.com` alone (no subdomain) is too short to be valid."""
    with pytest.raises(client.AuthError, match="too short"):
        client.DeviceClient(
            host=".duosecurity.com",
            pkey="X",
            akey="X",
            private_key_pem=golden_key_pem.read_bytes().decode(),
        )


def test_urgid_url_quoted(golden_key_pem, monkeypatch):
    """Defense in depth: urgid with / or ? must be URL-encoded in the path."""
    c = client.DeviceClient(
        host="api-12345678901234567890123456789012.duosecurity.com",
        pkey="X",
        akey="X",
        private_key_pem=golden_key_pem.read_bytes().decode(),
        sleep=lambda s: None,
    )

    class Capture:
        def __init__(self):
            self.calls = []

        def __call__(self, request, **kwargs):
            self.calls.append(request)
            resp = requests.Response()
            resp.status_code = 200
            resp._content = b'{"response": "OK"}'
            return resp

    cap = Capture()
    monkeypatch.setattr(c, "_session", _mock_session(cap))
    c.approve("abc/def?ghi=1")
    # The path must have the special chars encoded.
    assert cap.calls[0].url.endswith("/transactions/abc%2Fdef%3Fghi%3D1")


# --- list_transactions -----------------------------------------------------

def test_list_transactions_returns_parsed_json(device_client, monkeypatch):
    transport = MockTransport([(200, {}, {"response": [{"urgid": "u1"}, {"urgid": "u2"}]})])
    monkeypatch.setattr(device_client, "_session", _mock_session(transport))
    txs = device_client.list_transactions()
    assert len(txs) == 2
    assert txs[0]["urgid"] == "u1"


def test_list_transactions_sends_signed_get_to_correct_path(device_client, monkeypatch):
    transport = MockTransport([(200, {}, [])])
    monkeypatch.setattr(device_client, "_session", _mock_session(transport))
    device_client.list_transactions()
    req = transport.calls[0]
    assert req.method == "GET"
    parsed = urllib.parse.urlparse(req.url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "api-12345678901234567890123456789012.duosecurity.com"
    assert parsed.path == "/push/v2/device/transactions"


def test_list_transactions_request_authorization_header(device_client, monkeypatch):
    transport = MockTransport([(200, {}, [])])
    monkeypatch.setattr(device_client, "_session", _mock_session(transport))
    device_client.list_transactions()
    req = transport.calls[0]
    auth = req.headers["Authorization"]
    assert auth.startswith("Basic ")
    decoded = base64.b64decode(auth.split(" ", 1)[1]).decode()
    pkey, sig_b64 = decoded.split(":", 1)
    assert pkey == "FAKE_PKEY_FINGERPRINT"
    # Signature is base64 of 256 bytes (2048-bit RSA).
    assert len(base64.b64decode(sig_b64)) == 256


def test_list_transactions_request_date_header(device_client, monkeypatch):
    transport = MockTransport([(200, {}, [])])
    monkeypatch.setattr(device_client, "_session", _mock_session(transport))
    device_client.list_transactions()
    req = transport.calls[0]
    import email.utils
    parsed = email.utils.parsedate_to_datetime(req.headers["x-duo-date"])
    assert parsed is not None
    assert parsed.tzinfo is not None


def test_list_transactions_signs_with_canonical_string(golden_key_pem, monkeypatch):
    """The signature on the wire must verify against our private key using
    the canonical-string format documented in protocol.py."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    key = serialization.load_pem_private_key(golden_key_pem.read_bytes(), password=None)
    c = client.DeviceClient(
        host="api-12345678901234567890123456789012.duosecurity.com",
        pkey="FAKE_PKEY_FINGERPRINT",
        akey="FAKE_AKEY",
        private_key_pem=golden_key_pem.read_bytes().decode(),
        sleep=lambda s: None,
    )
    transport = MockTransport([(200, {}, [])])
    monkeypatch.setattr(c, "_session", _mock_session(transport))
    c.list_transactions()

    req = transport.calls[0]
    import urllib.parse
    auth = req.headers["Authorization"]
    sig_b64 = base64.b64decode(auth.split(" ", 1)[1]).decode().split(":", 1)[1]
    sig = base64.b64decode(sig_b64)

    date = req.headers["x-duo-date"]
    parsed_qs = urllib.parse.parse_qs(urllib.parse.urlparse(req.url).query)
    # Reconstruct the canonical string and verify the signature.
    canonical = (
        date + "\n"
        + req.method + "\n"
        + "api-12345678901234567890123456789012.duosecurity.com" + "\n"
        + urllib.parse.urlparse(req.url).path + "\n"
        + urllib.parse.urlencode(sorted(
            (k, v[0]) for k, v in parsed_qs.items()
        ))
    )
    key.public_key().verify(
        sig, canonical.encode("ascii"), padding.PKCS1v15(), hashes.SHA512()
    )


# --- approve ---------------------------------------------------------------

def test_approve_posts_answer_approve_to_urgid_url(device_client, monkeypatch):
    transport = MockTransport([(200, {}, {"response": "OK"})])
    monkeypatch.setattr(device_client, "_session", _mock_session(transport))
    result = device_client.approve("abc123")
    req = transport.calls[0]
    assert req.method == "POST"
    assert req.url.endswith("/push/v2/device/transactions/abc123")
    # Body should include answer=approve (form-encoded; may be str or bytes).
    body = req.body
    if isinstance(body, bytes):
        body = body.decode()
    assert "answer=approve" in body


def test_approve_returns_approve_result(device_client, monkeypatch):
    transport = MockTransport([(200, {}, {"response": "OK"})])
    monkeypatch.setattr(device_client, "_session", _mock_session(transport))
    result = device_client.approve("abc123")
    assert result.http_status == 200
    assert result.body == {"response": "OK"}


# --- Retry / backoff branches ---------------------------------------------

def test_429_uses_retry_after(device_client, monkeypatch):
    sleeps = []
    monkeypatch.setattr(device_client, "_sleep", lambda s: sleeps.append(s))
    # 429 with Retry-After, then 200.
    transport = MockTransport([
        (429, {"Retry-After": "7"}, None),
        (200, {}, {"response": []}),
    ])
    monkeypatch.setattr(device_client, "_session", _mock_session(transport))
    device_client.list_transactions()
    assert sleeps == [7.0]


def test_429_retries_only_once(device_client, monkeypatch):
    sleeps = []
    monkeypatch.setattr(device_client, "_sleep", lambda s: sleeps.append(s))
    transport = MockTransport([
        (429, {"Retry-After": "5"}, None),
        (429, {"Retry-After": "5"}, None),
    ])
    monkeypatch.setattr(device_client, "_session", _mock_session(transport))
    with pytest.raises(client.RateLimitedError):
        device_client.list_transactions()
    assert sleeps == [5.0]


def test_5xx_triggers_exponential_backoff(device_client, monkeypatch):
    sleeps = []
    monkeypatch.setattr(device_client, "_sleep", lambda s: sleeps.append(s))
    transport = MockTransport([
        (503, {}, None),
        (503, {}, None),
        (200, {}, {"response": []}),
    ])
    monkeypatch.setattr(device_client, "_session", _mock_session(transport))
    device_client.list_transactions()
    # Exponential: 1, 2 (then success).
    assert sleeps == [1.0, 2.0]


def test_5xx_eventually_raises_after_max_attempts(device_client, monkeypatch):
    sleeps = []
    monkeypatch.setattr(device_client, "_sleep", lambda s: sleeps.append(s))
    transport = MockTransport([(503, {}, None)] * 10)
    monkeypatch.setattr(device_client, "_session", _mock_session(transport))
    with pytest.raises(client.NetworkError):
        device_client.list_transactions()


def test_4xx_other_raises_immediately_no_retry(device_client, monkeypatch):
    sleeps = []
    monkeypatch.setattr(device_client, "_sleep", lambda s: sleeps.append(s))
    transport = MockTransport([(401, {}, {"error": "unauthorized"})])
    monkeypatch.setattr(device_client, "_session", _mock_session(transport))
    with pytest.raises(client.AuthError):
        device_client.list_transactions()
    assert sleeps == []


def test_network_error_triggers_retry(device_client, monkeypatch):
    sleeps = []
    monkeypatch.setattr(device_client, "_sleep", lambda s: sleeps.append(s))

    def boom(request, **kwargs):
        raise requests.ConnectionError("nope")

    session = MagicMock()
    session.send = boom
    # Replace with a transport that raises on first call, succeeds on second.
    call_count = [0]

    def flaky(request, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise requests.ConnectionError("nope")
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b'{"response": []}'
        return resp

    session.send = flaky
    monkeypatch.setattr(device_client, "_session", session)
    device_client.list_transactions()
    # One retry sleep then success.
    assert len(sleeps) >= 1


# --- helpers ---------------------------------------------------------------

def _mock_session(transport):
    """Wrap a transport callable in a fake requests.Session."""
    session = MagicMock()
    session.send = transport
    return session