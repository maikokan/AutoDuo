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

ALLOWED_HOST_SUFFIX = ".duosecurity.com"

# Retry policy
MAX_5XX_ATTEMPTS = 7
MAX_429_ATTEMPTS = 1  # one extra attempt after a Retry-After wait
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 60.0
DEFAULT_RETRY_AFTER_SECONDS = 30.0
DEFAULT_HTTP_TIMEOUT = 10.0


class AuthError(Exception):
    """Caller/config problem — wrong host, bad credentials, 4xx non-429."""


class RateLimitedError(Exception):
    """Server returned 429 even after Retry-After."""


class NetworkError(Exception):
    """5xx or transport failure after exhausting retries."""


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
        except ValueError:
            logger.warning("list_transactions: non-JSON response (status %d)", resp.status_code)
            return []
        # Duo returns {"stat": "OK", "response": {"transactions": [...], "current_time": ...}}
        # or sometimes {"stat": "OK", "response": [...]}.
        # Unwrap the transactions list regardless of shape.
        if isinstance(body, dict):
            response = body.get("response")
            if isinstance(response, dict):
                txs = response.get("transactions")
                if isinstance(txs, list):
                    return list(txs)
            if isinstance(response, list):
                return list(response)
        if isinstance(body, list):
            return body
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
        except ValueError:
            body = {}
        return ApproveResult(http_status=resp.status_code, body=body)

    # ------------------------------------------------------------------ signing

    def _signed_request(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
    ) -> requests.Response:
        from autoduo import redact
        date = protocol.now_rfc1123(clock=self._clock)
        canonical = protocol.canonical_request(date, method, self.host, path, params)
        sig = protocol.sign(self._private_key, canonical)
        headers = protocol.build_headers(
            pkey=self.pkey,
            signature_b64=base64.b64encode(sig).decode("ascii"),
            date_rfc1123=date,
        )
        url = f"https://{self.host}{path}"
        # For GET, params go in the query string; for POST they go in body.
        # requests handles this via `params=` for GET and `data=` for POST.
        if method.upper() == "GET":
            req = requests.Request(method, url, params=params, headers=headers).prepare()
        else:
            req = requests.Request(method, url, data=params, headers=headers).prepare()

        self._log_traffic_request(method, req)

        resp = self._session.send(req, timeout=self._http_timeout)
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
                retry_after = resp.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else DEFAULT_RETRY_AFTER_SECONDS
                except (TypeError, ValueError):
                    wait = DEFAULT_RETRY_AFTER_SECONDS
                logger.warning("429 from %s %s; sleeping %.1fs", method, path, wait)
                self._sleep(wait)
                continue

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