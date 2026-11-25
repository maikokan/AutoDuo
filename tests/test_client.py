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
import pathlib
import time
import urllib.parse
from unittest.mock import MagicMock

import pytest
import requests

from autoduo import client, protocol


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
        if body is None:
            resp._content = b""
        elif isinstance(body, (bytes, bytearray)):
            # Raw wire bytes, so a test can send a body that is not JSON.
            resp._content = bytes(body)
        else:
            resp._content = json.dumps(body).encode()
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
            resp._content = b'{"stat": "OK", "response": "OK"}'
            return resp

    cap = Capture()
    monkeypatch.setattr(c, "_session", _mock_session(cap))
    c.approve("abc/def?ghi=1")
    # The path must have the special chars encoded.
    assert cap.calls[0].url.endswith("/transactions/abc%2Fdef%3Fghi%3D1")


# --- list_transactions -----------------------------------------------------

def test_list_transactions_returns_parsed_json(device_client, monkeypatch):
    transport = MockTransport([(200, {}, {"stat": "OK", "response": [{"urgid": "u1"}, {"urgid": "u2"}]})])
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
    transport = MockTransport([(200, {}, {"stat": "OK", "response": "OK"})])
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
    transport = MockTransport([(200, {}, {"stat": "OK", "response": "OK"})])
    monkeypatch.setattr(device_client, "_session", _mock_session(transport))
    result = device_client.approve("abc123")
    assert result.http_status == 200
    assert result.body == {"stat": "OK", "response": "OK"}


# --- Retry / backoff branches ---------------------------------------------

def test_429_uses_retry_after(device_client, monkeypatch):
    sleeps = []
    monkeypatch.setattr(device_client, "_sleep", lambda s: sleeps.append(s))
    # 429 with Retry-After, then 200.
    transport = MockTransport([
        (429, {"Retry-After": "7"}, None),
        (200, {}, {"stat": "OK", "response": []}),
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
        (200, {}, {"stat": "OK", "response": []}),
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


# --- Protocol validation (ROBUSTNESS 1.1 / 1.2) ----------------------------

def test_list_transactions_raises_on_stat_fail(device_client, monkeypatch):
    transport = MockTransport([
        (200, {}, {"stat": "FAIL", "code": 40301, "message": "no akey"})
    ])
    monkeypatch.setattr(device_client, "_session", _mock_session(transport))
    with pytest.raises(client.ProtocolError) as ei:
        device_client.list_transactions()
    assert ei.value.code == 40301
    assert ei.value.message == "no akey"


def test_list_transactions_stat_fail_beats_usable_response(device_client, monkeypatch):
    """A FAIL body with something that looks like a transaction list is
    still a failure — checking shape first would swallow it."""
    transport = MockTransport([
        (200, {}, {"stat": "FAIL", "code": 1, "response": [{"urgid": "u1"}]})
    ])
    monkeypatch.setattr(device_client, "_session", _mock_session(transport))
    with pytest.raises(client.ProtocolError):
        device_client.list_transactions()


def test_list_transactions_raises_on_ok_without_transactions(device_client, monkeypatch):
    transport = MockTransport([(200, {}, {"stat": "OK"})])
    monkeypatch.setattr(device_client, "_session", _mock_session(transport))
    with pytest.raises(client.ProtocolError) as ei:
        device_client.list_transactions()
    assert ei.value.code == "malformed"


def test_list_transactions_accepts_missing_stat_legacy_shape(
    device_client, monkeypatch
):
    """stat absent must keep polling: old bodies/fakes omit it."""
    transport = MockTransport([(200, {}, {"response": [{"urgid": "u1"}]})])
    monkeypatch.setattr(device_client, "_session", _mock_session(transport))
    assert device_client.list_transactions() == [{"urgid": "u1"}]


def test_list_transactions_raises_on_non_json_body(device_client, monkeypatch):
    transport = MockTransport([(200, {}, None)])  # empty body -> ValueError
    monkeypatch.setattr(device_client, "_session", _mock_session(transport))
    with pytest.raises(client.ProtocolError):
        device_client.list_transactions()


def test_list_transactions_accepts_bare_list(device_client, monkeypatch):
    """A bare list cannot carry stat=FAIL; accept it as the tx list."""
    transport = MockTransport([(200, {}, [{"urgid": "u1"}])])
    monkeypatch.setattr(device_client, "_session", _mock_session(transport))
    assert device_client.list_transactions() == [{"urgid": "u1"}]


def test_approve_raises_on_stat_fail(device_client, monkeypatch):
    transport = MockTransport([
        (200, {}, {"stat": "FAIL", "code": "40032", "message": "step-up"})
    ])
    monkeypatch.setattr(device_client, "_session", _mock_session(transport))
    with pytest.raises(client.ApprovalRejected) as ei:
        device_client.approve("abc123")
    assert ei.value.code == "40032"
    assert ei.value.message == "step-up"


def test_approve_accepts_body_without_stat(device_client, monkeypatch):
    """Legacy/partial bodies omit stat; only a present non-OK stat fails."""
    transport = MockTransport([(200, {}, {"response": "OK"})])
    monkeypatch.setattr(device_client, "_session", _mock_session(transport))
    result = device_client.approve("abc123")
    assert result.http_status == 200


def test_signed_bytes_equal_transmitted_bytes_for_out_of_order_params(
    device_client, monkeypatch
):
    """canonical_request signs sorted(params); the wire must carry the same
    order and encoding (ROBUSTNESS 1.3 coupling)."""
    transport = MockTransport([(200, {}, {"stat": "OK", "response": []})])
    monkeypatch.setattr(device_client, "_session", _mock_session(transport))

    device_client._signed_request(
        "POST",
        "/push/v2/device/transactions/abc123",
        {"zeta": "1", "alpha": "2"},  # deliberately not sorted
    )

    req = transport.calls[0]
    body = req.body
    if isinstance(body, bytes):
        body = body.decode()
    # Transmitted as sorted key/value pairs, form-encoded (Content-Type set).
    assert body == "alpha=2&zeta=1"
    assert req.headers["Content-Type"].startswith(
        "application/x-www-form-urlencoded"
    )

    pairs = urllib.parse.parse_qsl(body)
    assert pairs == [("alpha", "2"), ("zeta", "1")]
    canon = protocol.canonical_request(
        req.headers["x-duo-date"], req.method, device_client.host,
        "/push/v2/device/transactions/abc123", dict(pairs),
    )
    # The final canonical line is exactly the bytes on the wire.
    assert canon.rsplit("\n", 1)[1] == body

    # And the signature verifies over that canonical string.
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    key = serialization.load_pem_private_key(
        (pathlib.Path(__file__).parent / "_golden_key.pem").read_bytes(),
        password=None,
    )
    auth = req.headers["Authorization"]
    sig_b64 = base64.b64decode(auth.split(" ", 1)[1]).decode().split(":", 1)[1]
    key.public_key().verify(
        base64.b64decode(sig_b64),
        canon.encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA512(),
    )


# --- helpers ---------------------------------------------------------------

def _mock_session(transport):
    """Wrap a transport callable in a fake requests.Session."""
    session = MagicMock()
    session.send = transport
    return session

# --- an HTTP 200 is not automatically a success ---------------------------

def test_approve_rejects_a_non_json_200(device_client, monkeypatch):
    """A captive portal or maintenance page must not look like an approval.

    The poll path already refused a body it could not read; the approve
    path did not, so an HTML 200 was emitted as ``approve_ok`` and written
    to the audit log as a real approval.
    """
    transport = MockTransport([(200, {"Content-Type": "text/html"},
                                b"<html>maintenance</html>")])
    monkeypatch.setattr(device_client, "_session", _mock_session(transport))
    with pytest.raises(client.ProtocolError) as ei:
        device_client.approve("abc123")
    assert ei.value.code == "malformed"


def test_approve_rejects_a_json_body_that_is_not_an_object(
        device_client, monkeypatch):
    transport = MockTransport([(200, {}, ["not", "a", "dict"])])
    monkeypatch.setattr(device_client, "_session", _mock_session(transport))
    with pytest.raises(client.ProtocolError):
        device_client.approve("abc123")


# --- redirects are not followed -------------------------------------------

def test_a_redirect_is_refused_not_followed(device_client, monkeypatch):
    """The allowlist is checked once, at construction.

    ``requests.Session.send`` follows redirects by default, so a 3xx from
    anything answering on a Duo subdomain would carry the signed request
    somewhere nobody vetted and hand back a stranger's body.
    """
    transport = MockTransport([(302, {"Location": "https://evil.example/"},
                                b"")])
    monkeypatch.setattr(device_client, "_session", _mock_session(transport))
    with pytest.raises(client.ProtocolError) as ei:
        device_client.approve("abc123")
    assert ei.value.code == "redirect"
    assert len(transport.calls) == 1  # no second request was made


def test_the_client_asks_requests_not_to_follow(device_client, monkeypatch):
    seen = {}

    class Session:
        def send(self, req, **kwargs):
            seen.update(kwargs)
            resp = requests.Response()
            resp.status_code = 200
            resp._content = b'{"stat": "OK"}'
            return resp

    monkeypatch.setattr(device_client, "_session", Session())
    device_client.approve("abc123")
    assert seen.get("allow_redirects") is False


# --- Retry-After is clamped -----------------------------------------------

@pytest.mark.parametrize("header,expected", [
    ("999999", client.RETRY_AFTER_CAP_SECONDS),
    ("-5", client.DEFAULT_RETRY_AFTER_SECONDS),
    ("nan", client.DEFAULT_RETRY_AFTER_SECONDS),
    ("inf", client.DEFAULT_RETRY_AFTER_SECONDS),
    ("Wed, 21 Oct 2015 07:28:00 GMT", client.DEFAULT_RETRY_AFTER_SECONDS),
    ("", client.DEFAULT_RETRY_AFTER_SECONDS),
    ("12", 12.0),
])
def test_retry_after_is_clamped_to_something_survivable(header, expected):
    assert client._retry_after_seconds(header) == expected


def test_a_huge_retry_after_does_not_stop_the_daemon_for_days(
        device_client, monkeypatch):
    sleeps = []
    monkeypatch.setattr(device_client, "_sleep", lambda s: sleeps.append(s))
    transport = MockTransport([
        (429, {"Retry-After": "999999"}, b""),
        (200, {}, {"stat": "OK"}),
    ])
    monkeypatch.setattr(device_client, "_session", _mock_session(transport))
    device_client.approve("abc123")
    assert sleeps == [client.RETRY_AFTER_CAP_SECONDS]
    assert all(0 <= s <= client.RETRY_AFTER_CAP_SECONDS for s in sleeps)


def test_a_negative_retry_after_never_reaches_time_sleep(
        device_client, monkeypatch):
    sleeps = []
    monkeypatch.setattr(device_client, "_sleep", lambda s: sleeps.append(s))
    transport = MockTransport([
        (429, {"Retry-After": "-5"}, b""),
        (200, {}, {"stat": "OK"}),
    ])
    monkeypatch.setattr(device_client, "_session", _mock_session(transport))
    device_client.approve("abc123")
    assert sleeps == [client.DEFAULT_RETRY_AFTER_SECONDS]
