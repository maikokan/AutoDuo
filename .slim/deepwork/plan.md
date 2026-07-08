# Plan: duo-bot (headless Duo auto-accept daemon) — FINAL POST-ORACLE

> Oracle review (`/opt/duo-bot/.slim/deepwork/oracle-review.md`) verdict:
> "Approve with ~20 changes, none structural." All flagged items addressed
> inline below. Phases resequenced to 1a/1b/1c/2a/2b/3 with explicit gates.

## 1. Goal (confirmed with user)
A systemd-managed Python daemon on this VPS (Ubuntu 24.04, Python 3.12) that
runs 24/7 and auto-approves any Duo push request it receives for the user's
account. No human tap required after one-time enrollment.

## 2. Architecture
Register a virtual Duo Mobile device via the **device protocol**
(`/push/v2/device/...`) — same surface the official Duo Mobile app uses.

Runtime daemon:
1. Load AES-GCM-sealed vault (PBKDF2-HMAC-SHA256, 600k iterations) at
   `/var/lib/duo-bot/vault.enc` containing RSA private key + `akey` +
   `pkey` + host + activation response (full dict for forward-compat).
2. Poll `GET /push/v2/device/transactions` every `POLL_INTERVAL` (default
   5 s), RSA-SHA512-signed.
3. Dedup by `urgid` in an LRU set persisted to
   `/var/lib/duo-bot/seen.json` (TTL = Duo's push TTL, default 60 s).
4. For each *unseen* transaction, POST
   `POST /push/v2/device/transactions/<urgid>` with `answer=approve`.
5. Distinguish 429 (use `Retry-After`) from 5xx/network (exponential
   backoff capped 60 s) from 4xx-other (no retry, surface as
   `AuthError`).
6. Circuit-breaker: N auth failures in M seconds → enter "alert and
   sleep 5 min" mode, log high-severity event, exit code 75 for systemd
   to record.
7. Logs every event as one-line JSON to stdout AND
   `/var/log/duo-bot/daemon.log`, including heartbeat once per 5 min
   even when 0 transactions. systemd-journal compatible.

Enrollment CLI (`enroll.py`) runs once. User supplies the Duo activation
URL via **file** (`--activation-file PATH`, mode 0600, deleted after read)
or **stdin** (`--activation-stdin`). Refuses `--activation-url` on argv
in production builds (it leaks via `/proc/<pid>/cmdline` and shell
history).

## 3. Project Layout (post-review, simplified)
```
/opt/duo-bot/
├── README.md                     # operator notes, threat model, install
├── requirements.txt              # requests, cryptography, pyotp (optional)
├── LICENSE                       # MIT
├── duo_bot/
│   ├── __init__.py               # __version__ = "0.1.0"
│   ├── config.py                 # env-var loader (DUO_BOT_* prefix)
│   ├── vault.py                  # PBKDF2 + AES-GCM, schema_version=1
│   ├── protocol.py               # canonical string, sign, headers, now_rfc1123
│   ├── client.py                 # HTTP + RSA-SHA512, retry, dedup, allowlist
│   ├── daemon.py                 # poll loop, signals, logging inline
│   └── enroll.py                 # one-shot CLI
├── scripts/
│   ├── install.sh                # creates user, dirs, venv, unit
│   └── uninstall.sh              # inverse of install.sh
├── systemd/
│   ├── duo-bot.service           # hardened unit
│   └── duo-bot.env.example       # template (no secrets)
├── tests/
│   ├── test_protocol.py          # golden vectors for canonical+sign
│   ├── test_vault.py             # round-trip, tamper fails, schema migration
│   ├── test_client.py            # mock transport, 429/5xx/4xx branches, allowlist
│   └── test_daemon.py            # fake clock + fake transport; asserts dedup
└── .slim/deepwork/               # this work
```

**Dropped** (oracle 3.1, 3.4, 3.5):
- `pyproject.toml` — YAGNI; `duo_bot/__init__.py` with `__version__` is enough.
- `duo_bot/logging_setup.py` — inline JSON logger in `daemon.py` and `enroll.py`.
- `duo-bot-daemon.sh`, `duo-bot-vault-init.sh` — systemd unit calls
  `python -m duo_bot.daemon` directly. `enroll` is invoked manually as
  `python -m duo_bot.enroll`.

## 4. Module Responsibilities

### `duo_bot/protocol.py` (TDD: write golden vectors FIRST)
- `now_rfc1123(clock=time.gmtime) -> str` — clock injection for tests.
- `canonical_request(date_rfc1123, method, host, path, params) -> str`.
- `sign(private_key_pem, canonical_str) -> bytes` — PKCS1 v1.5, SHA-512.
- `build_headers(pkey, signature_b64, date_rfc1123) -> dict`.
- One-line module docstring notes: "split into 4 functions for unit
  testability; the only production caller is `client._signed_request`."

### `duo_bot/vault.py`
- `SCHEMA_VERSION = 1`. Sealed payload shape:
  `{"v": 1, "created_at": ISO8601, "akey": ..., "pkey": ...,
  "host": ..., "private_key_pem": ..., "activation": {...full dict...}}`.
- `init(path, passphrase) -> None` creates file with random PBKDF2 salt
  (32 B) + AES-GCM nonce (12 B) + ciphertext + 16 B tag.
- `seal(path, passphrase, payload: dict) -> None` — re-seal (key rotation).
- `open_vault(path, passphrase) -> dict` — raises `VaultError` on tamper
  (GCM auth tag mismatch) or schema-version mismatch (with `migrate()`
  hook for future versions, no-op at v1).
- File perms: parent dir 0700, file 0600.

### `duo_bot/client.py`
- `ALLOWED_HOST_SUFFIX = ".duosecurity.com"` — enforced in
  `__init__`, raises `AuthError` on mismatch.
- `DeviceClient(host, pkey, akey, private_key_pem, session=None,
  clock=None, http_timeout=10)` — wraps `requests.Session` (TLS verify
  on by default, document this).
- `list_transactions() -> list[dict]` — GET
  `/push/v2/device/transactions`. Empty list on `[]` body.
- `approve(urgid) -> ApproveResult` — POST
  `/push/v2/device/transactions/<urgid>` with `answer=approve`.
  `ApproveResult` is a typed dataclass with `(http_status, body, retry_after_s)`
  so the daemon can branch without re-parsing.
- `_signed_request(method, path, params=None) -> requests.Response` —
  the single signing/header/canonical path.
- **No `health_check` method.** "200 from `list_transactions`" is the
  liveness signal. systemd's `Restart=always` is the actual watchdog.
- **Retry policy** (single `_request_with_retry` helper):
  - 200 → return.
  - 429 → sleep exactly `Retry-After` seconds (or 30 s default if
    header absent), one retry, then raise.
  - 5xx / network → exponential backoff 1, 2, 4, 8, 16, 32, 60 s (cap),
    7 attempts total.
  - Other 4xx → raise `AuthError` immediately, no retry.
- On any `AuthError` containing `X-Duo-Date` in response: log one-shot
  warning ("clock skew suspected — install timesyncd").

### `duo_bot/daemon.py`
- `DuoBotDaemon(vault_path, passphrase_provider, poll_interval=5,
  push_ttl=60, log_path, state_path)`:
  - Loads vault on start; on `VaultError`, exit 4 (`exitcode 4`
    for systemd to log).
  - Loads `seen.json` (in-memory `OrderedDict` LRU, max 10 000 entries,
    entries older than `push_ttl` are evicted).
  - Loop: list_transactions() → for each new urgid, approve() → log
    → sleep `poll_interval`.
  - On `NetworkError`: increment backoff, sleep up to 60 s.
  - On `AuthError`: increment `auth_fail_window` counter; if > 5 in
    5 min → enter "alert mode": log `severity=high` event,
    sleep 5 min, do not approve anything.
  - Persists `seen.json` on graceful shutdown and every 60 s.
  - SIGTERM/SIGINT → save state, exit 0.
- Inline `configure_logging(log_path)` private function — JSON formatter,
  file + stdout handlers, fields: `ts`, `event`, `urgid`, `factor`,
  `app`, `http`, `retry_after`, `severity`.

### `duo_bot/enroll.py`
- CLI args: `--vault PATH` (default `/var/lib/duo-bot/vault.enc`),
  `--activation-file PATH` OR `--activation-stdin`,
  `--passphrase-file PATH` (mode 0600; **never** argv). Refuses argv
  passphrase. Refuses `--activation-url` (use `--activation-file`).
- Steps:
  1. Read activation URL from file/stdin. Delete file (if `--activation-file`)
     after read. Never log it.
  2. Generate 2048-bit RSA keypair.
  3. Extract activation code.
  4. POST `/push/v2/device/activation/<code>` with our pubkey → returns
     `{akey, pkey, host, ...}` — **store the entire response**, not just
     named fields, so HOTP secret etc. are preserved if Duo returns them.
  5. Validate `host` endswith `.duosecurity.com`.
  6. Seal payload to vault.
  7. Print clear next-step instructions: "Open Duo Mobile, approve the
     'new device' prompt, then start the daemon."

## 5. systemd Unit (`systemd/duo-bot.service`)
```
[Unit]
Description=Duo headless auto-accept daemon
Documentation=file:///opt/duo-bot/README.md
After=network-online.target time-sync.target
Wants=network-online.target time-sync.target

[Service]
Type=simple
User=duo-bot
Group=duo-bot
WorkingDirectory=/opt/duo-bot
EnvironmentFile=/etc/duo-bot/duo-bot.env
ExecStart=/opt/duo-bot/.venv/bin/python -m duo_bot.daemon
Restart=always
RestartSec=5
StartLimitIntervalSec=300
StartLimitBurst=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=duo-bot
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/log/duo-bot /var/lib/duo-bot
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
LockPersonality=true
RestrictRealtime=true
RestrictSUIDSGID=true
SystemCallArchitectures=native
MemoryDenyWriteExecute=true
CapabilityBoundingSet=
ResourceLimit=MEMLOCK=64M

[Install]
WantedBy=multi-user.target
```

**Vault passphrase** lives in `/etc/duo-bot/duo-bot-vault.key` (mode
0400, owner root:duo-bot group-readable). Read by daemon via systemd
`LoadCredential=`. daemon passes it to `vault.open_vault()`. systemd
hardens the credential boundary.

**Defaults** (`/etc/duo-bot/duo-bot.env`, mode 0640 root:duo-bot):
```
DUO_BOT_VAULT_PATH=/var/lib/duo-bot/vault.enc
DUO_BOT_LOG_PATH=/var/log/duo-bot/daemon.log
DUO_BOT_POLL_INTERVAL=5
DUO_BOT_PUSH_TTL=60
DUO_BOT_STATE_PATH=/var/lib/duo-bot/seen.json
DUO_BOT_VAULT_CREDENTIAL=duo-bot-vault-passphrase
```

## 6. Install / Uninstall

`scripts/install.sh` (root, once):
1. `apt-get install -y python3-venv python3-pip systemd-timesyncd`
2. `systemctl enable --now systemd-timesyncd`
3. `useradd --system --shell /usr/sbin/nologin --home /var/lib/duo-bot duo-bot`
4. `install -d -m 0750 -o duo-bot -g duo-bot /var/lib/duo-bot /var/log/duo-bot`
5. `python3 -m venv /opt/duo-bot/.venv`
6. `/opt/duo-bot/.venv/bin/pip install -r /opt/duo-bot/requirements.txt`
7. `install -m 0644 systemd/duo-bot.service /etc/systemd/system/`
8. `install -m 0640 -o root -g duo-bot systemd/duo-bot.env.example /etc/duo-bot/duo-bot.env`
9. `install -m 0400 -o root -g duo-bot /dev/null /etc/duo-bot/duo-bot-vault.key`
   (operator must populate this file manually)
10. `systemctl daemon-reload`
11. Print: "Run `python -m duo_bot.enroll --vault /var/lib/duo-bot/vault.enc --activation-file <path> --passphrase-file <path>`, then `systemctl enable --now duo-bot`."

`scripts/uninstall.sh`: inverse — `systemctl disable --now duo-bot`,
remove unit, remove user (with `userdel --remove`), remove `/etc/duo-bot`,
remove `/var/lib/duo-bot`, remove `/var/log/duo-bot`. Does NOT touch
`/opt/duo-bot` itself (operator may want to keep the source).

logrotate: `/etc/logrotate.d/duo-bot` weekly, rotate 4, compress,
`postrotate` sends `systemctl reload-or-restart duo-bot` so the file
handle is reopened.

## 7. Threat Model & Limitations (will be in README §2)

- **Defeats 2FA**: by design. Anyone with shell as `duo-bot` (or root)
  can approve pushes. Recommend: dedicated VPS, SSH key only,
  `PermitRootLogin no`, fail2ban, no other services on the box.
- **Verified Duo Push**: if tenant enforces Verified Push, the *login*
  still fails because the human must type a browser code into the
  phone. We approve the push, but Duo rejects the login. Recommend
  disabling Verified Push for the affected user, or accept that
  Verified Push bypass is out of scope.
- **Anomaly detection**: fixed-cadence polling from one IP, with no
  real device fingerprint, is detectable in principle. Recommend
  Duo Admin "anomaly alerts" → email.
- **Vault passphrase loss** → unrecoverable (must re-enroll).
- **No copy of DuoBreak code**: all Python is original. License: MIT.

## 8. Phased Execution Plan (resequenced post-oracle)

### Phase 1a — `protocol.py` + `vault.py` + golden-vector tests (TDD)
**Gate:** `pytest` green; canonical-string golden vector matches
expected output derived from Duo protocol docs (independent of impl).
- Write `tests/test_protocol.py` with golden vector for canonical
  string (a known date/method/host/path/params → known canonical →
  known base64 signature) **before** implementing `protocol.py`.
- Implement `protocol.py` to make the test pass.
- Implement `vault.py` and `tests/test_vault.py` (round-trip,
  tamper-detection, schema-version migration hook).

### Phase 1b — `client.py` + mock-transport tests
**Gate:** `pytest` green; "signed request matches golden vector" test
passes; 429/5xx/4xx branch tests pass; host-allowlist test passes.
- Implement `client.py`.
- `tests/test_client.py` uses a `MockTransport` class (no real
  network). Asserts:
  - URL is correct (path + query string).
  - `Authorization: Basic pkey:base64sig` header is well-formed.
  - `Date` header is RFC 1123.
  - 429 response → client sleeps `Retry-After` seconds, retries once.
  - 503 response → exponential backoff.
  - 401/403 → raises `AuthError` immediately, no retry.
  - Host `evil.example.com` → raises before any HTTP call.

### Phase 1c — `enroll.py` + inline logging + activation-file mode
**Gate:** `python -m duo_bot.enroll --help` works; `shellcheck` clean;
no passphrase accepted via argv.
- Implement `enroll.py`. Inline logging (no separate module).
- `bandit -r duo_bot/enroll.py` clean.

### Phase 2a — `daemon.py` + `tests/test_daemon.py` (fake clock + fake transport)
**Gate:** `pytest` green; signals handled; in-process dedup proven;
`seen.json` round-trips.
- Implement `daemon.py` with the loop, backoff, signals, heartbeat.
- Test asserts: (a) two `list_transactions` calls returning the same
  urgid result in only one `approve` call; (b) `seen.json` is written
  on SIGTERM; (c) heartbeat event fires once per 5 min even with 0
  transactions; (d) circuit-breaker engages after 5 auth failures in
  5 min.

### Phase 2b — systemd unit + install/uninstall scripts + logrotate
**Gate:** `systemd-analyze verify duo-bot.service` clean;
`systemd-analyze security duo-bot.service` shows no warnings beyond
expected; `shellcheck scripts/*.sh` clean.
- Drop hardened unit (full hardening list above).
- `scripts/install.sh`, `scripts/uninstall.sh`.
- `/etc/logrotate.d/duo-bot`.
- `install.sh` is idempotent (safe to re-run).

### Phase 3 — Validation + final @oracle review
**Gate:** every box below green.
- `systemd-analyze security duo-bot.service` — no warnings beyond
  expected.
- `systemd-analyze verify duo-bot.service` — unit valid.
- `shellcheck scripts/*.sh` — clean.
- `bandit -r duo_bot/` — no high/medium.
- `python -m compileall duo_bot/` — no syntax errors.
- `python -m pytest` — all green.
- `python -m duo_bot.daemon --help` and `--version` — work.
- `python -m duo_bot.enroll --help` and `--version` — work.
- Fresh-venv install: `python3 -m venv .venv && pip install -r
  requirements.txt && python -c "from cryptography.hazmat.primitives.asymmetric import padding; print('ok')"` — works under
  `MemoryDenyWriteExecute=true` (run unit-test under that sandbox via
  `systemd-run --scope -p MemoryDenyWriteExecute=true ...`).
- Final @oracle review for simplification + readability.

## 9. What we are NOT doing
- Not building the `/auth/v2/` poll-only path (user said no).
- Not copying DuoBreak / Auto-2FA code (license + AGPL).
- Not building a TUI / web UI (YAGNI; journal + log file is enough).
- Not implementing TOTP / SMS / phone factors (push only).
- Not adding Telegram notifications (out of scope; journald is enough).
- Not building a `health_check` endpoint on the daemon (YAGNI).

## 10. Risks (carry-over from research, restated)
- **Protocol change**: Duo could rotate the device protocol signing
  scheme. Mitigation: protocol module is isolated and golden-tested;
  breakage shows up as a signature-mismatch on the very first
  transaction.
- **Vault passphrase loss**: unrecoverable (by design). Document.
- **Verified Push policy**: documented as known limitation; user
  acknowledged it.
- **AGPL contamination**: avoid by writing all code from scratch.
- **Clock skew**: mitigated by `time-sync.target` dependency and
  one-shot warning on auth failure.

## 11. Decisions locked before each phase
- **Phase 1a**: golden vector format locked (date, method, host, path,
  params → expected canonical bytes). Decided to use RFC 1123 (matches
  `email.utils.formatdate(usegmt=True)`), same as DuoBreak's
  `toUTCString()`.
- **Phase 1b**: retry counts locked (7 for 5xx, 1 for 429). Host
  suffix locked (`.duosecurity.com`, hard-coded; configurable later if
  needed).
- **Phase 2a**: poll cadence locked (default 5 s, env-overridable).
  Push TTL locked (default 60 s, env-overridable).
- **Phase 2b**: run-as-user locked (`duo-bot`, even in dev). Logs to
  both file and journal (matches prod).