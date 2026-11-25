"""HTTP client for the Duo device protocol (/push/v2/device/...).

Wraps `requests.Session` with:
- Per-request RSA-SHA512 signing (delegates to autoduo.protocol).
- Retry policy that distinguishes 429 (Retry-After), 5xx/network
  (exponential backoff), and other 4xx (no retry, surface immediately).
- Host allowlist enforcement in __init__ (defends against activation
  response tampering).
- A single _request_with_retry() entry point so both list_transactions
  and approve share the same behavior.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, TextIO

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from autoduo import protocol

logger = logging.getLogger(__name__)


def _sha256(data) -> str:
    """SHA-256 hex digest for body-hash logging."""
    if data is None:
        return ""
    return hashlib.sha256(data).hexdigest()


def _retry_after_seconds(value: str | None) -> float:
    """A server-supplied wait, clamped to something a daemon can survive.

    ``Retry-After`` is whatever the far end says, and it is not always a
    sane number: a negative or NaN value makes ``time.sleep`` raise, and a
    huge one stops the account approving for days with nothing in the log
    but one line. Non-numeric values (an HTTP-date, per RFC 7231) fall back
    to the default, and the result is bounded like our own backoff.
    """
    if not value:
        return DEFAULT_RETRY_AFTER_SECONDS
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER_SECONDS
    if not math.isfinite(seconds) or seconds < 0:
        return DEFAULT_RETRY_AFTER_SECONDS
    return min(seconds, RETRY_AFTER_CAP_SECONDS)

ALLOWED_HOST_SUFFIX = ".duosecurity.com"

# Retry policy
MAX_5XX_ATTEMPTS = 7
MAX_429_ATTEMPTS = 1  # one extra attempt after a Retry-After wait
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 60.0
DEFAULT_RETRY_AFTER_SECONDS = 30.0
# `Retry-After` is whatever the far end says. Without a ceiling a single
# header stops this account approving for days, with one log line to show
# for it, so it is bounded like our own backoff.
RETRY_AFTER_CAP_SECONDS = 300.0
DEFAULT_HTTP_TIMEOUT = 10.0


class AuthError(Exception):
    """Caller/config problem — wrong host, bad credentials, 4xx non-429."""


class RateLimitedError(Exception):
    """Server returned 429 even after Retry-After."""


class NetworkError(Exception):
    """5xx or transport failure after exhausting retries."""


class ProtocolError(Exception):
    """Duo answered HTTP 2xx but the body is not a usable protocol reply.

    Duo reports logical failures as HTTP 200 with
    ``{"stat": "FAIL", "code": ..., "message": ...}``.  Returning an empty
    transaction list for one of those makes a broken poll indistinguishable
    from "no pushes", so every unusable poll body raises this instead.
    """

    def __init__(self, message: str = "", code: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ApprovalRejected(Exception):
    """Duo rejected an approval at the protocol level (``stat != "OK"``).

    HTTP 200 with a logical failure is not success.  ``code`` carries the
    Duo error code (for retry classification) and ``message`` the server's
    explanation.
    """

    def __init__(self, message: str = "", code: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ApproveResult:
    http_status: int
    body: dict[str, Any]


class DeviceClient:
    def __init__(
        self,
        host: str,
        pkey: str,
        akey: str,
        private_key_pem: str,
        session: requests.Session | None = None,
        clock: Callable[[], Any] | None = None,
        sleep: Callable[[float], None] | None = None,
        http_timeout: float = DEFAULT_HTTP_TIMEOUT,
        traffic_log: TextIO | None = None,
    ) -> None:
        if not host.endswith(ALLOWED_HOST_SUFFIX):
            raise AuthError(
                f"host {host!r} does not end with {ALLOWED_HOST_SUFFIX!r} "
                "(activation response tampering suspected)"
            )
        if len(host) <= len(ALLOWED_HOST_SUFFIX):
            raise AuthError(
                f"host {host!r} is too short to be a valid Duo host"
            )
        if "://" in host or "/" in host:
            raise AuthError(f"host {host!r} contains scheme or path")

        self.host = host
        self.pkey = pkey
        self.akey = akey
        self._private_key = serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"), password=None
        )
        if not isinstance(self._private_key, rsa.RSAPrivateKey):
            raise AuthError("private_key_pem is not an RSA key")
        if self._private_key.key_size < 2048:
            raise AuthError(
                f"RSA key must be >= 2048 bits, got {self._private_key.key_size}"
            )
        self._session = session or requests.Session()
        self._clock = clock
        self._sleep = sleep if sleep is not None else time.sleep
        self._http_timeout = http_timeout
        self._traffic_log = traffic_log

    # ------------------------------------------------------------------ public

    def list_transactions(self) -> list[dict[str, Any]]:
        params = {
            "akey": self.akey,
            "fips_status": "1",
            "hsm_status": "true",
            "pkpush": "rsa-sha512",
        }
        resp = self._request_with_retry("GET", "/push/v2/device/transactions", params=params)
        try:
            body = resp.json()
        except ValueError as e:
            raise ProtocolError(
                f"list_transactions: non-JSON response (status {resp.status_code})"
            ) from e

        # A bare list cannot express a logical failure; accept it as the
        # transaction list (legacy / odd-but-harmless shape).
        if isinstance(body, list):
            return list(body)

        if not isinstance(body, dict):
            raise ProtocolError(
                "list_transactions: unrecognised response body "
                f"({type(body).__name__})",
                code="malformed",
            )

        # Duo reports logical errors as HTTP 200 {"stat": "FAIL", ...}.
        # Only a *present* non-OK stat is fatal: legacy bodies and old
        # fakes omit stat entirely, so absence must keep polling.
        stat = body.get("stat")
        if "stat" in body and stat != "OK":
            raise ProtocolError(
                str(body.get("message") or "list_transactions rejected"),
                code=body.get("code"),
            )

        # Duo returns {"stat": "OK", "response": {"transactions": [...], "current_time": ...}}
        # or sometimes {"stat": "OK", "response": [...]}.
        response = body.get("response")
        if isinstance(response, dict):
            txs = response.get("transactions")
            if isinstance(txs, list):
                return list(txs)
        if isinstance(response, list):
            return list(response)

        # stat == "OK" but no recognisable transaction list is malformed.
        # (stat absent keeps the historical unwrap-or-empty behaviour.)
        if "stat" in body:
            raise ProtocolError(
                "list_transactions: stat=OK but response is malformed",
                code="malformed",
            )
        return []

    def approve(self, urgid: str) -> ApproveResult:
        if not urgid or not isinstance(urgid, str):
            raise AuthError(f"urgid must be a non-empty string, got {urgid!r}")
        # urgid is path-unsafe in theory; the Duo API expects it as a path
        # segment. urlencode it to be safe, though urgids are alphanumeric+dash.
        safe = urllib.parse.quote(urgid, safe="")
        resp = self._request_with_retry(
            "POST",
            f"/push/v2/device/transactions/{safe}",
            params={"answer": "approve"},
        )
        try:
            body = resp.json()
        except ValueError as e:
            # An HTTP 200 whose body is not JSON is not an approval. The
            # same rule already applies to polls; a captive portal, a proxy
            # error page or a Duo maintenance page used to be recorded here
            # as a successful approval and written to the audit log.
            raise ProtocolError(
                f"approve: non-JSON response (status {resp.status_code})",
                code="malformed",
            ) from e
        if not isinstance(body, dict):
            raise ProtocolError(
                "approve: unrecognised response body "
                f"({type(body).__name__})",
                code="malformed",
            )
        if "stat" in body and body["stat"] != "OK":
            raise ApprovalRejected(
                str(body.get("message") or "approval rejected"),
                code=body.get("code"),
            )
        return ApproveResult(http_status=resp.status_code, body=body)

    # ------------------------------------------------------------------ signing

    def _signed_request(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
    ) -> requests.Response:
        date = protocol.now_rfc1123(clock=self._clock)
        canonical = protocol.canonical_request(date, method, self.host, path, params)
        sig = protocol.sign(self._private_key, canonical)
        headers = protocol.build_headers(
            pkey=self.pkey,
            signature_b64=base64.b64encode(sig).decode("ascii"),
            date_rfc1123=date,
        )
        url = f"https://{self.host}{path}"
        # Signed order == transmitted order.  protocol.canonical_request()
        # signs urlencode(sorted(params.items())); transmit the same sorted
        # sequence.  Pass a LIST OF TUPLES (not a pre-encoded string): a
        # string body makes requests set no Content-Type at all.
        ordered = sorted(params.items())
        if method.upper() == "GET":
            req = requests.Request(method, url, params=ordered, headers=headers).prepare()
        else:
            req = requests.Request(method, url, data=ordered, headers=headers).prepare()

        self._log_traffic_request(method, req)

        # Never follow a redirect. The host allowlist is checked once, in
        # __init__, and requests would happily chase a 3xx to anywhere --
        # carrying a signed request at a host nobody vetted and treating the
        # stranger's body as Duo's answer.
        resp = self._session.send(
            req, timeout=self._http_timeout, allow_redirects=False
        )
        self._log_traffic_response(method, req, resp)
        return resp

    def _log_traffic_request(self, method: str, req: requests.PreparedRequest) -> None:
        """Emit a request line to the traffic log (if enabled).

        All sensitive fields go through `redact` before serialization.
        """
        if self._traffic_log is None:
            return
        from autoduo import redact
        # req.url may contain akey, pkey. Redact.
        url_parts = urllib.parse.urlparse(req.url)
        safe_query = redact.redact_query(url_parts.query or "")
        safe_url = urllib.parse.urlunparse(url_parts._replace(query=safe_query))
        body = req.body
        if isinstance(body, str):
            body = body.encode("utf-8")
        record = {
            "event": "http_request",
            "method": method,
            "url": safe_url,
            "headers": redact.redact_headers(dict(req.headers)),
            "body_sha256": _sha256(body) if body else "",
            "body_len": len(body) if body else 0,
        }
        self._traffic_log.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._traffic_log.flush()

    def _log_traffic_response(self, method: str, req: requests.PreparedRequest, resp: requests.Response) -> None:
        """Emit a response line to the traffic log (if enabled).

        Redacts akey/pkey from the URL (the request logger creates its own
        redacted URL but does NOT mutate req.url).
        """
        if self._traffic_log is None:
            return
        from autoduo import redact
        url_parts = urllib.parse.urlparse(req.url)
        safe_query = redact.redact_query(url_parts.query or "")
        safe_url = urllib.parse.urlunparse(url_parts._replace(query=safe_query))
        try:
            body_bytes = resp.content or b""
        except Exception:
            body_bytes = b""
        record = {
            "event": "http_response",
            "method": method,
            "url": safe_url,
            "status": resp.status_code,
            "headers": redact.redact_headers(dict(resp.headers)),
            "body_sha256": _sha256(body_bytes),
            "body_len": len(body_bytes),
        }
        self._traffic_log.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._traffic_log.flush()

    # ------------------------------------------------------------------ retry

    def _request_with_retry(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
    ) -> requests.Response:
        """Single retry-policy entry point for all requests.

        Branches:
        - 200/2xx → return response.
        - 429 → sleep Retry-After (or 30 s), one extra attempt, else raise.
        - 5xx → exponential backoff, up to MAX_5XX_ATTEMPTS.
        - requests.ConnectionError / Timeout → same as 5xx (transient).
        - Other 4xx → raise AuthError immediately (config/caller bug).
        """
        attempts_5xx = 0
        attempts_429 = 0

        while True:
            try:
                resp = self._signed_request(method, path, params)
            except (requests.ConnectionError, requests.Timeout) as e:
                attempts_5xx += 1
                if attempts_5xx > MAX_5XX_ATTEMPTS:
                    raise NetworkError(f"network error after {attempts_5xx} attempts: {e}") from e
                wait = min(BACKOFF_BASE_SECONDS * (2 ** (attempts_5xx - 1)), BACKOFF_CAP_SECONDS)
                self._sleep(wait)
                continue

            status = resp.status_code
            if 200 <= status < 300:
                return resp

            if status == 429:
                attempts_429 += 1
                if attempts_429 > MAX_429_ATTEMPTS:
                    raise RateLimitedError(f"429 after {attempts_429} attempts")
                wait = _retry_after_seconds(resp.headers.get("Retry-After"))
                logger.warning("429 from %s %s; sleeping %.1fs", method, path, wait)
                self._sleep(wait)
                continue

            if 300 <= status < 400:
                # We asked not to be redirected; a 3xx still arriving means
                # the far end is not the API we signed for.
                raise ProtocolError(
                    f"{method} {path}: redirected ({status}) to "
                    f"{resp.headers.get('Location', 'no Location')!r}; "
                    "refusing to follow",
                    code="redirect",
                )

            if 500 <= status < 600:
                attempts_5xx += 1
                if attempts_5xx > MAX_5XX_ATTEMPTS:
                    raise NetworkError(f"5xx after {attempts_5xx} attempts (last status {status})")
                wait = min(BACKOFF_BASE_SECONDS * (2 ** (attempts_5xx - 1)), BACKOFF_CAP_SECONDS)
                logger.warning("5xx (%d) from %s %s; backoff %.1fs", status, method, path, wait)
                self._sleep(wait)
                continue

            # Other 4xx — caller/config bug. Surface immediately, no retry.
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            raise AuthError(f"{method} {path} → {status}: {body}")