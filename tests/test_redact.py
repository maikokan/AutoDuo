"""Tests for autoduo.redact.

CRITICAL: these tests are the invariant. If a future change makes any
of them fail, the change has leaked credentials into logs.
"""
from urllib.parse import parse_qsl

from autoduo import redact


def test_redact_query_strips_akey_value():
    out = redact.redact_query("akey=DAVLCYDCZRDV5UPUK33G&fips_status=1")
    pairs = dict(parse_qsl(out))
    # akey must be replaced with redaction; fips_status unchanged
    assert "DAVLCYDCZRDV5UPUK33G" not in out
    assert "DAVL" not in out
    assert "<" in pairs["akey"]  # redacted form
    assert pairs["fips_status"] == "1"


def test_redact_query_strips_pkey_value():
    out = redact.redact_query("pkey=DPYESICANEHP2ATLPW2W&other=value")
    assert "DPYESICANEHP2ATLPW2W" not in out
    assert "DPYE" not in out


def test_redact_query_strips_app_install_id():
    out = redact.redact_query("app_install_id=999f5587-65fb-4663-bd9f-f3bb62648da2")
    assert "999f5587-65fb-4663-bd9f-f3bb62648da2" not in out
    assert "999f" not in out


def test_redact_query_accepts_dict():
    out = redact.redact_query({"akey": "SECRET_KEY_HERE", "fips_status": "1"})
    assert "SECRET_KEY_HERE" not in out


def test_redact_query_accepts_list_of_tuples():
    out = redact.redact_query([("akey", "SECRET"), ("other", "value")])
    assert "SECRET" not in out


def test_redact_query_empty_input():
    assert redact.redact_query("") == ""
    assert redact.redact_query(None) == ""


def test_redact_query_passes_through_safe_fields():
    out = redact.redact_query("customer_protocol=1&fips_status=1&hsm_status=true")
    assert out == "customer_protocol=1&fips_status=1&hsm_status=true"


# --- redact_headers -------------------------------------------------------

def test_redact_headers_strips_authorization_completely():
    headers = {
        "Authorization": "Basic " + "A" * 200,
        "Host": "api-X.duosecurity.com",
    }
    out = redact.redact_headers(headers)
    assert out["Authorization"] == "Basic <redacted>"
    # The base64 payload (or any byte of it) must not appear.
    assert "A" * 50 not in str(out)
    assert out["Host"] == "api-X.duosecurity.com"


def test_redact_headers_redacts_x_duo_date():
    headers = {"x-duo-date": "Wed, 21 Aug 2024 12:00:00 +0000"}
    out = redact.redact_headers(headers)
    assert "12:00:00" not in str(out)
    assert out["x-duo-date"] == "<redacted-timestamp>"


def test_redact_headers_passes_through_safe_fields():
    headers = {
        "Host": "api-X.duosecurity.com",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "okhttp/4.0",
    }
    out = redact.redact_headers(headers)
    assert out == headers


def test_redact_headers_does_not_mutate_input():
    headers = {"Authorization": "Basic SECRET123"}
    original = dict(headers)
    redact.redact_headers(headers)
    assert headers == original


def test_redact_headers_handles_empty():
    assert redact.redact_headers({}) == {}
    assert redact.redact_headers(None) == {}


def test_redact_headers_case_insensitive():
    headers = {"AUTHORIZATION": "Basic SECRET", "X-Duo-Date": "X"}
    out = redact.redact_headers(headers)
    assert out["AUTHORIZATION"] == "Basic <redacted>"
    assert out["X-Duo-Date"] == "<redacted-timestamp>"


# --- redact_transaction ---------------------------------------------------

def test_redact_transaction_strips_screen_text():
    tx = {"urgid": "abc", "screen_text": "Approve login from Chrome"}
    out = redact.redact_transaction(tx)
    assert "screen_text" not in out
    assert out["urgid"] == "abc"


def test_redact_transaction_strips_user_agent():
    tx = {"urgid": "abc", "user_agent": "Mozilla/5.0 (Linux) ..."}
    out = redact.redact_transaction(tx)
    assert "user_agent" not in out


def test_redact_transaction_strips_attributes():
    tx = {
        "urgid": "abc",
        "attributes": [[["Username", "1155267403"], ["IP", "153.240.60.13"]]],
    }
    out = redact.redact_transaction(tx)
    assert "attributes" not in out


def test_redact_transaction_truncates_ipv4():
    tx = {"urgid": "abc", "ip": "153.240.60.13"}
    out = redact.redact_transaction(tx)
    assert out["ip"] == "153.240.60.0"


def test_redact_transaction_truncates_ipv6():
    tx = {"urgid": "abc", "ip": "2001:db8:abcd:0000:0000:0000:0000:0001"}
    out = redact.redact_transaction(tx)
    assert out["ip"].startswith("2001:db8:abcd")


def test_redact_transaction_keeps_safe_fields():
    tx = {
        "urgid": "abc",
        "txid": "tx-123",
        "factor": "push",
        "app": "CUHK LOGIN",
        "type": "Login",
    }
    out = redact.redact_transaction(tx)
    assert out == tx