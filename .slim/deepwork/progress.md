# Deepwork: Duo Auto-Accept Bot (Linus VPS, 24/7)

## Goal
Build a Linux service that runs 24/7 on this VPS and automatically accepts
Duo push/2FA login prompts, so the human doesn't have to tap "Approve" on
their phone.

## User-supplied references
- adishy/AutomateDuo
- FreshSupaSulley/Auto-2FA
- JesseNaser/DuoBreak
- duosecurity/duo_api_swift
- duosecurity/duo_client_python

## Environment (this machine)
- Host: Linux vmi3152678, Ubuntu 24.04.4 LTS, kernel 6.8.0-124-generic
- Python 3.12.3 at /usr/bin/python3
- systemd 255 available, `systemctl` usable
- Working dir: /opt/duo-bot (empty, just .slim/deepwork/)
- User: root

## Operating Constraints
- Must run 24/7 → managed by systemd with Restart=always
- Must be testable on Duo Admin API sandbox (demo) where possible
- Must not leak secrets: IKEY/SKEY/AKEY in logs, must live in a 0600 file
- Must degrade safely if Duo API changes / network drops
- Idempotent: re-running must not spam /auth or /push endpoints

## Phases
1. Research (4 repos + Duo protocol docs) → write findings here
2. Plan draft + @oracle review (architecture, risk, simplification)
3. Phased implementation plan
4. Phase 1: scaffold + auth client
5. Phase 2: polling loop + tests + systemd unit
6. Phase 3: validation + final @oracle review

## Progress Log
- 2026-07-08: Initialized deepwork file. Environment confirmed Ubuntu 24.04,
  python3.12, systemd 255. Working dir /opt/duo-bot ready.
- 2026-07-08: lib-1 (AutomateDuo + DuoBreak) reconciled. Key finding: neither
  uses the Admin API. DuoBreak is the canonical reference for headless Linux
  auto-accept: hand-rolled RSA-SHA512 signing, polls
  `GET /push/v2/device/transactions`, replies via
  `POST /push/v2/device/transactions/{urgid}` with `answer=approve`. AGPL-3.0
  — must reference, not copy. AutomateDuo is Android-only and irrelevant to a
  VPS target.

## Confirmed Research Context (lib-1)
### DuoBreak protocol (the canonical headless pattern)
- Enrollment: generate RSA keypair locally, scan Duo "Tablet/Android" QR,
  `POST /push/v2/activation/{code}` → returns `akey`, `pkey`, `hotp_secret`.
- Poll: `GET /push/v2/device/transactions?akey=...&fips_status=1&hsm_status=true&pkpush=rsa-sha512`
- Approve: `POST /push/v2/device/transactions/{urgid}` body `answer=approve`
- Signing: canonical string `date\nmethod\nhost\npath\nparams`,
  PKCS1 v1.5 over SHA-512, header `Authorization: Basic base64(pkey:base64sig)`,
  `x-duo-date` in RFC 2822 format.
- Loop: 10 s sleep, 5 s backoff on `ConnectionError`, 10-failure cap.
- Storage: private key + akey + HOTP secret in PBKDF2(pass, 100k) AES vault.
- License: AGPL-3.0-or-later (viral; reference only).
- This is the **device-protocol** (`/push/v2/`), distinct from Admin (`/admin/v1/`)
  and end-user REST (`/auth/v2/`). `duo_client_python` is the Admin SDK and is
  **not** relevant.

### Implications for our design
- We will mirror DuoBreak's protocol shape but rewrite cleanly, MIT-licensed
  internally, no copy-paste of AGPL code.
- We will NOT use the Admin API or the end-user REST API — they cannot approve
  pushes on behalf of an enrolled device.
- Threat-model note: a fixed-cadence daemon from one IP / no real device
  fingerprint is in principle detectable. Document this and recommend the user
  enable Duo anomaly alerts.

## Confirmed Research Context (lib-3 — Duo Auth API)
### End-user REST API (`/auth/v2/`)
- Base URL: `https://api-<APIHOST>.duosecurity.com/auth/v2/`. Host is per-account.
- Endpoints: GET `/ping` (unsigned), POST `/preauth`, POST `/auth`,
  GET `/auth_status`. All except `/ping` require signed requests.
- Signing scheme: **HMAC-SHA1** (not SHA-256/512), header
  `Authorization: Basic base64(ikey:hex_hmac)`, plus `Date` (RFC 2822),
  `Host`, and POST `Content-Type: application/x-www-form-urlencoded`.
- Canonical string is 5 newline-joined components:
  RFC2822-date, METHOD, lowercased host, path (leading slash), URL-encoded
  sorted k=v params.
- `duo_client_python` is the Admin SDK; for the `/auth/v2/` REST API we would
  use `duo_client.Auth.client` (still wraps the same signing internally).
### Push flow on `/auth/v2/`
- `POST /auth/v2/preauth` → lists `response.devices[].factors` (`push`,
  `sms`, `phone`, `totp`).
- `POST /auth/v2/auth` with `factor=push` → returns `txid`,
  `result: "waiting"`.
- `GET /auth/v2/auth_status?txid=...` → poll for `allow`/`deny`. Push times
  out ~60 s.
### Crucial finding
**The Auth API cannot programmatically approve a push.** There is no
`auto`, `silent`, or `auto_approve` parameter. `factor=auto` only picks
between push and phone — it does NOT auto-approve.
### Admin API cannot bypass 2FA either
- `/admin/v1/users/[id]/send_verification_push` is help-desk verification,
  not login.
- `/admin/v1/users/[id]/bypass_codes` mints codes the *user* must type —
  not server-side auth.
- No admin endpoint creates a session, SSO assertion, or "logged-in" state.
### What this means for the actual product
The user's stated goal — "auto accept any duo login request" — has **only
one technically valid implementation**: enroll a virtual device using Duo's
mobile-protocol endpoint (`/push/v2/...`), exactly the DuoBreak pattern.
The Auth API alone is insufficient: it can only initiate and poll, never
approve.
**This is the architectural pivot the user must understand before we build.**
Options:
A. Build the DuoBreak-style headless daemon (recommended — actually works,
   matches the goal).
B. Build a wrapper around `/auth/v2/` that polls and *notifies the user*
   instead of auto-approving (defeats the goal).
C. Use bypass codes minted via Admin API — defeats 2FA entirely and is
   policy-dependent.
We must surface this to the user as a clarifying question before drafting
the final plan, because the answer changes whether the project is even
buildable as requested.

### Threat-model & policy notes
- Verified Duo Push requires the user to type a browser code into the
  phone — even our headless approve would not satisfy Verified Push. If
  the user's tenant uses Verified Push, no unattended tool can complete
  login. We must check this.
- `trusted_device_token` from `/auth_status` could let future requests
  skip 2FA — but it requires an initial human approval, so does not solve
  the cold-start problem.
- FCM/APNs tokens are not exposed — silent push to a service-controlled
  device is not possible.

## Confirmed Research Context (lib-2 — Auto-2FA + duo_client_python)
### FreshSupaSulley/Auto-2FA (archived 2026-06-14)
- WXT-framework browser extension (JS/TS), not Selenium. Service-worker
  fetches Duo's API directly.
- It registers as a **new Duo Mobile device** (activation) then polls
  `/push/v2/device/transactions` and approves via
  `POST /push/v2/device/transactions/<urgid}` with `answer=approve`.
- Same wire shape as DuoBreak: per-device 2048-bit RSA keypair, RSA-SHA512
  signing, canonical string `date\nMETHOD\nhost\npath\nsorted_urlencoded_params`.
- Per-tenant host learned at activation (e.g. `api-46217189.duosecurity.com`).
- Bootstrap requires one human QR-scan or dashboard approval — after that
  self-signed forever.
- No `duo_client_python` usage; `otplib` for TOTP fallback.

### duosecurity/duo_client_python (v5.6.1)
- Two clients: `Auth(ikey, skey, host)` for `/auth/v2/`, `Admin(...)` for
  `/admin/v1/`. Both wrap HMAC-SHA512 request signing (sig_version=5).
- Auth methods relevant to push: `ping`, `check`, `preauth`, `auth(factor=push, async_txn=True)`, `auth_status(txid)`. With `async_txn=True`, `auth` returns `txid`; `auth_status` is the longpoll for `allow`/`deny`.
- Admin methods include `get_authentication_log`, `get_user_*`,
  `enroll_user`, `bypass_codes` — all **management only**. No
  `push_tx`/`approve_transaction`/list-of-in-flight-pushes. Admin API
  cannot log a user in.
- Library is for /auth/v2/ and /admin/v1/ only — does NOT speak the
  `/push/v2/device/*` mobile protocol.

### duosecurity/duo_api_swift
- Archived 2025-05-20, deprecated. Auth-only mirror of the HMAC scheme.
- Useful as a reference for canonical-string format, but irrelevant to the
  push protocol.

## Cross-Lane Synthesis (all three research lanes reconciled)
### The product the user described is NOT buildable via the Auth/Admin APIs
- `/auth/v2/auth` with `factor=push` only sends a push; the API cannot
  approve it.
- `/admin/v1/` cannot log a user in, period.
- `duo_client_python` is the Admin SDK, irrelevant to push approval.
### The only technically valid implementation
- Register a virtual device via Duo's **mobile protocol** (`/push/v2/device/*`)
  exactly like Auto-2FA / DuoBreak do.
- Bootstrap: one human scans a QR / approves on the Duo dashboard.
- Runtime: headless daemon polls `GET /push/v2/device/transactions` and
  POSTs `answer=approve` to `/push/v2/device/transactions/<urgid>`.
- This is the only path that actually satisfies "auto accept any duo login
  request" unattended.
### Edge cases the user must decide on BEFORE coding
1. **Verified Duo Push policy** — if the tenant uses Verified Push, even
   the device-protocol approval is not enough: the human must type the
   browser code into the phone. Need to confirm policy before claiming the
   bot will work.
2. **First-enrollment human step** — registering the virtual device
   requires the user to scan a QR / click approve on the real Duo Mobile
   app exactly once. After that, fully unattended. User must accept this.
3. **Threat-model acceptance** — fixed-cadence daemon from one IP, with no
   real phone fingerprint, is in principle detectable. AGPL/licensing
   note: we will reference the protocol shape but write clean
   original code, not copy DuoBreak.

These three questions gate whether to proceed with the device-protocol
design or pivot to a different product framing.

## Oracle Review (1st pass)
- Verdict: Approve with ~20 changes, none structural.
- Full review at `.slim/deepwork/oracle-review.md` (436 lines).
- High-priority items addressed in plan.md §2, §3, §4, §5, §8:
  1. Clock skew: `time-sync.target` dependency + clock injection in
     protocol.py + one-shot warning on auth failure.
  2. In-process urgid dedup with persisted `seen.json`.
  3. 429 vs 5xx vs 4xx retry branching.
  4. Drop `health_check()` method; "200 from list_transactions" is the
     liveness signal.
  5. Host allowlist `.duosecurity.com` enforced in `__init__`.
  6. Vault passphrase via systemd `LoadCredentialEncrypted`-style
     credential file (`/etc/duo-bot/duo-bot-vault.key`, 0400).
  7. Phase split: 1a/1b/1c/2a/2b/3 with explicit gates.
  8. TDD discipline: golden vectors written BEFORE `protocol.py`.
  9. `--activation-file` / `--activation-stdin` only; refuse argv
     activation URL and passphrase.
 10. Full systemd hardening (`RestrictAddressFamilies`, `LockPersonality`,
     `MemoryDenyWriteExecute`, etc.).
 11. JSON log shape specified in plan.
 12. Circuit-breaker for repeated auth failures.
 13. Activation response stored in full (HOTP forward-compat).
 14. Drop `pyproject.toml`, `logging_setup.py`, `duo-bot-daemon.sh`,
     `duo-bot-vault-init.sh`.
 15. Install + uninstall scripts.
 16. `MemoryDenyWriteExecute=true` test gate in Phase 3.
 17. `bandit`, `shellcheck`, `systemd-analyze security` in Phase 3.
 18. README outline in plan.
 19. Per-phase decisions locked.
 20. Final @oracle review in Phase 3.

## lib-5 reconciliation — Correct activation body format
Error 40002 "Unsupported platform" was caused by missing required fields
in the activation POST body. Duo validates the request looks like a
real Duo Mobile client.

The canonical working format comes from Auto-2FA (most actively
maintained, last commit 2026-04-19, app version 4.110.0).

**Required activation body (emulating Android Duo Mobile):**
```json
{
    "customer_protocol": "1",
    "pubkey": "-----BEGIN PUBLIC KEY-----\nMIIB...\n-----END PUBLIC KEY-----",
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
    "app_install_id": "<UUID-v4, unique per enrolled device>",
    "version": "14",
    "manufacturer": "Samsung",
    "language": "en",
    "security_patch_level": "2024-01-05",
    "model": "SM-S926B"
}
```

Key fields missing from our previous body:
- `customer_protocol: "1"` — critical, Duo branches on this
- `pkpush: "rsa-sha512"` — signals push signing intent
- `app_id`, `app_version`, `app_build_number`, `app_install_id` — app identity
- `platform`, `manufacturer`, `model`, `architecture` — device identity
- `full_disk_encryption`, `passcode_status`, `jailbroken` — device posture

The `app_install_id` must be a persistent UUID (generated once per device,
saved to the vault). It's Duo's device fingerprint — changing it after
enrollment would orphan the device.

The `m-` portal activation code (`PoE7k3BOSuxDKzbaBMkJ`) IS the correct
token for the `/push/v2/activation/CODE` endpoint — the issue was purely
the request body, not the code format.

## lib-6 reconciliation — Correct Duo Mobile activation flow
The `m-` portal URL composite code is NOT used directly in the API.
The Duo Mobile app:
1. Receives `duo://SHORT_CODE-BASE64(API_HOST)`
2. Splits on `-`, base64-decodes the second part to get API host
3. POSTs to `https://{API_HOST}/push/v2/activation/{SHORT_CODE}?customer_protocol=1`

KEY INSIGHT: The SHORT code goes in the URL path, NOT the composite.
The `customer_protocol=1` goes as a QUERY PARAMETER, not in the body.

The body fields (pubkey, platform, device info, etc.) are form-encoded.
The `pkpush` field is also sent in the body (not as a query param).

Captured real request from Nils Streedain's mitmproxy intercept:
```
POST /push/v2/activation/{SHORT_CODE}?customer_protocol=1
Content-Type: application/x-www-form-urlencoded

jailbroken=false&architecture=armv7&region=US&app_id=com.duosecurity.duomobile
&full_disk_encryption=true&passcode_status=true&platform=Android
&app_version=3.23.0&app_build_number=323001&version=8.1&manufacturer=unknown
&language=en&model=Pixel+C&security_patch_level=2018-12-01
```

No `pkpush` in the body of the activation POST — it's used later in
the transaction polling, not during activation.

The response contains `{akey, pkey, hotp_secret}` as documented.

## lib-7 reconciliation — Correct pubkey format
Duo expects the `pubkey` field to be the full PEM-encoded public key:
```
-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA...base64...
-----END PUBLIC KEY-----
```
With LF (`\n`) newlines, 64-char line wrapping, URL-encoded as
`application/x-www-form-urlencoded`. The SubjectPublicKeyInfo format
(RFC 7468 `PUBLIC KEY` label), not raw PKCS1 or DER.

The `pkpush` field must be the literal string `rsa-sha512` — this is
what the server binds to the pubkey for future transaction signing.

40112 "Signature type is not supported" means the pubkey parsing
failed, so the server can't determine the signature scheme.

## lib-4 reconciliation — Activation URL format
Key finding: Duo has **two distinct activation URL families**:

1. **`m-XXXXXXXX.duosecurity.com/activate/CODE`** — web-portal URL.
   HTML page, not a JSON API endpoint. DuoMobile picks it up via
   Intent/Universal Link. **NOT the programmatic endpoint.**

2. **`api-XXXXXXXX.duosecurity.com/push/v2/activation/CODE`** — the
   actual programmatic device-activation API that posts a JSON body
   with the RSA public key and returns `{akey, pkey, host}`.

The `host` prefix (`m-` vs `api-`) is account-identical; only the path
differs. Duo's documented `/auth/v2/enroll` endpoint returns both URLs
in its response. DuoBreak and Auto-2FA both use the `/push/v2/` form.

**Implication for enroll.py:** we need to accept BOTH URL shapes:
- `https://m-XXXXXXXX.duosecurity.com/activate/CODE` → rewrite to
  `https://api-XXXXXXXX.duosecurity.com/push/v2/activation/CODE`
  (same account ID, `m-` → `api-` prefix swap).
- `https://api-XXXXXXXX.duosecurity.com/push/v2/activation/CODE` → use as-is.

The user may paste either form. Also: the CODE part of the `/activate/`
form is a short alphanumeric string, NOT the long base64 activation_code
from the `/auth/v2/enroll` response — it's a different (shorter, URL-
safe) activation token that Duo uses in the portal flow. Verify this by
testing: try the actual user-provided URL (`m-08dc11c9.duosecurity.com/
activate/sC6kGnHdYcARtZaVGkFd`) and confirm whether the API endpoint
accepts it or whether it needs to be converted first.

**CRITICAL: the user pasted a live activation URL into this chat.
That link is a one-shot bearer secret. Advise them to invalidate it
in Duo Admin immediately and issue a fresh one.**

---

## Oracle Review (2nd pass — Phase 1a)
- Verdict: Correctness ✅, Security ⚠️ (1 fix), Simplification ✅.
- 23/23 tests green, verified independently.
- Fix applied: widened AAD in `vault.py` to
  `MAGIC + schema_version(4) + salt(32)`.

## Activation URL shape (user correction + lib-4)
- User's URL is `m-08dc11c9.duosecurity.com/activate/sC6kGnHdYcARtZaVGkFd`.
- lib-4 confirmed: this is the web-portal URL. The programmatic form
  is `api-XXXXXXXX.duosecurity.com/push/v2/activation/CODE`.
- Both share the same account ID; only the host prefix and path differ.
- `enroll.py` now normalizes both shapes via
  `_normalize_activation_url()` which rewrites `m-` → `api-` and
  `/activate/` → `/push/v2/activation/`.
- `ACTIVATION_PATH` corrected from `/push/v2/device/activation` to
  `/push/v2/activation` (no `device/` segment per DuoBreak's actual
  URL).

## Final oracle review (ora-7)
- Verdict: NEEDS FIXES (18 items). All applied:
  - #1: `--version` added to both CLIs (duo-bot 0.1.0).
  - #2: README test count corrected (76 → 79).
  - #3: `config.py` deleted; `EnvironmentFile=` removed from unit.
  - #4: logrotate switched to `copytruncate`; SIGHUP handler added
        to daemon (reopens log file handle in place).
  - #5: `_mark_seen` moved before approve; un-marked on failure
        to avoid crash-window duplicate approves.
  - #6: Activation-file mode now hard-errors (parity with passphrase).
  - #7: `tx_skipped` no longer dumps full tx body (PII).
  - #8–12: README threat model expanded with log/state/passphrase/
        anomaly/clock-sync sections.
  - #14: `--activation-delete` / `--no-activation-delete` flags
        removed; always-deletes now.
  - #15, #16: `import pathlib` hoisted; `pathlib_read_text`/`write_text`
        inlined into daemon.py methods.
  - #17: `--once` flag removed.
  - #18: README verify section leads with `journalctl`.

## Final validation (all green)
- pytest: 79/79
- compileall: clean
- systemd-analyze verify: clean
- systemd-analyze security: 3.9 OK
- shellcheck: clean
- MDXE under systemd: 79/79 passed
- `--version` and `--help` both CLIs: working
## Oracle Review — Release Plan (18 actionable items)
Saved to `.slim/deepwork/oracle-review-release.md`. Summary:

**High priority (correctness/risk):**
1. `Authorization` redaction must NOT expose any pkey bytes (first 8 chars
   of base64 token = first ~6 bytes of pkey, which is the device's
   stable identifier).
2. Redact `akey` and `pkey` from URL query strings, not just headers.
3. approvals.log must redact `user_agent`, `screen_text`, etc. (PII).
4. traffic.log default-OFF (opt-in via `--verbose-traffic`) — disk-fill
   risk for default-on.
5. `akey`/`pkey` length + first 4 chars for debugging, never full value.
6. Redaction wrapper at the boundary of every log emission; unit test
   that fails if a new code path emits without redaction.
7. Disclaimer in BOTH `setup` and `install`; `DUO_BOT_SKIP_DISCLAIMER=1`
   for scripted installs.
8. approvals.log mode 0640 owned by duo-bot group; PII warning in README.

**Push-test gate fixes:**
9. If `list_transactions()` returns non-200, fail gate with diagnostic;
   don't ask y/N.
10. Retry counter is per-field, not cumulative.
11. `__version__` static; drop git-describe mixing for v0.1.

**Simplifications (cut for v0.1):**
12. Drop `docs/how-it-works.md` (fits in README).
13. Drop `--verbose / -v` CLI flag (use `SYSTEMD_LOG_LEVEL=debug`).
14. Drop dependabot (add in 0.2 once deps stabilize).
15. Drop passphrase-mode menu (--no-passphrase docs suffice).
16. Drop `duo-bot version` command (--version already exists).
17. Drop PyPI-publish "defer" comment (delete, not defer).
18. Keep: SECURITY.md, CODE_OF_CONDUCT.md, three-badge banner.

Will apply high-priority items + cut low-priority items in Phases A/B/C.
