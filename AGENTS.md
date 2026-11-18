# AGENTS.md — AutoDuo

Guidance for AI agents and new contributors working in this repository.

## What this is

AutoDuo is a headless Linux daemon that auto-approves Duo 2FA push
notifications. It enrolls itself as a virtual "Duo Mobile" device via
Duo's own device API (`/push/v2/device/...`) and then polls
`/push/v2/device/transactions` every 5 s, approving each unseen
transaction.

**It defeats the purpose of 2FA by design.** Read `SECURITY.md` before
touching anything. The non-negotiables below (never log `akey`/`pkey`,
never put secrets on argv, host allowlist) are the product, not style.

Scale: ~2,100 lines of Python in 8 modules, 2 runtime deps (`requests`,
`cryptography`), 114 unit tests, no network needed to test. No
`pyproject.toml`/`setup.py` — this is a source-tree project by design
(see `.slim/deepwork/plan.md` §3).

## Commands

```bash
# Tests — must stay 100% offline
.venv/bin/python -m pytest tests/ -v

# Run a CLI from the source tree (no install needed)
.venv/bin/python -m autoduo.cli version
.venv/bin/python -m autoduo.enroll --help
.venv/bin/python -m autoduo.daemon --help

# Full CI parity (all four run in .github/workflows/test.yml)
.venv/bin/python -m pytest tests/ -v
.venv/bin/bandit -r autoduo/ -ll
python3 -m compileall autoduo/
shellcheck scripts/autoduo scripts/install.sh scripts/uninstall.sh
systemd-analyze verify systemd/autoduo.service
```

CI runs pytest + bandit on Python 3.10, 3.11, 3.12, plus a lint job for
shellcheck, compileall and `systemd-analyze verify`.

## Architecture

| File | Responsibility |
|---|---|
| `autoduo/__init__.py` | `__version__` only. |
| `autoduo/__main__.py` | `python -m autoduo` → `cli.main()`. |
| `autoduo/cli.py` | Operator CLI (`setup`/`install`/`enroll`/`start`/`status`/…). Mostly `subprocess` wrappers around `systemctl`/`apt-get` + interactive prompts. Does **not** talk to Duo directly — it shells out to `python -m autoduo.enroll`. |
| `autoduo/enroll.py` | One-shot enrollment CLI. URL → `(host, code)`, RSA-2048 keypair, POST `/push/v2/activation/<code>`, seal the vault. |
| `autoduo/vault.py` | Encrypted container `DVB1`: magic(4)+schema(4)+salt(32)+nonce(12)+ciphertext. PBKDF2-HMAC-SHA256 ×600k → AES-256-GCM. AAD = magic+version+salt. |
| `autoduo/protocol.py` | Request signing: canonical string, PKCS#1 v1.5 / SHA-512, `Authorization: Basic base64(pkey:b64sig)` + `x-duo-date`. |
| `autoduo/client.py` | `DeviceClient`: signed HTTP, retry taxonomy, `.duosecurity.com` allowlist, opt-in traffic log. |
| `autoduo/redact.py` | The log boundary. `akey`/`pkey` are length-only (`<28 chars:hidden>`); `Authorization`/`x-duo-date` replaced wholesale. |
| `autoduo/daemon.py` | The poll→dedup→approve loop, circuit breaker, backoff, JSON event log, signals, `seen.json` persistence. |

### Runtime flow

1. systemd starts `python -m autoduo.daemon` (see `systemd/autoduo.service`).
2. Daemon opens the vault → builds `DeviceClient(host, pkey, akey, private_key_pem)`.
3. Loop: `GET /push/v2/device/transactions` (signed) → dedup by `urgid`
   (TTL 60 s, persisted to `seen.json` every 60 s) → for unseen urgids:
   mark seen **before** calling, then `POST /push/v2/device/transactions/<urgid>`
   with `answer=approve`; un-mark on failure so the next poll retries.
4. Errors branch on exception type: `AuthError` → circuit breaker
   (≥5 in 300 s → 5 min cooldown, no work done), `NetworkError` →
   daemon-side exponential backoff (base 1 s, cap 60 s), `RateLimitedError`
   → log and continue.

### Enrollment flow (manual, once)

`autoduo setup` → disclaimer gate → paste activation URL → written to a
0600 temp file → `python -m autoduo.enroll` → vault sealed at
`/var/lib/autoduo/vault.enc` → user taps **Approve** on the phone once →
daemon starts → "push-test gate" asks the user to confirm a real push was
approved.

## Conventions

- **Dependency injection everywhere; no global state.** `clock`, `sleep`,
  `log_stream`, `audit_log_stream`, `state_path`, `requests.Session` are
  constructor arguments. This is what makes the loop testable — keep it.
- **Secrets never on argv.** Both parsers set `allow_abbrev=False`
  deliberately, so `--passphrase` cannot prefix-match `--passphrase-file`.
  The activation URL comes from a file (mode 0600, hard error otherwise)
  or stdin, and the file is always deleted after reading. Passphrase comes
  from a file, never argv.
- **Redaction at the boundary.** Any log emission that could carry
  `akey`/`pkey`/headers must go through `autoduo/redact.py`.
- **Exception taxonomy is load-bearing.** `AuthError` / `NetworkError` /
  `RateLimitedError` drive different daemon behaviour. Don't collapse them.
- **Three log audiences.** `daemon.log` (events), `audit.log` (approvals,
  0640), `traffic.log` (HTTP metadata, opt-in only — disk-fill risk).
- **Atomic writes.** Vault and `seen.json` are written tmp-then-rename.
- **Tests are pure unit tests.** Fakes live in the test files:
  `FakeDeviceClient` (daemon), `MockTransport` (client — replaces
  `requests.Session.send` with a programmable response queue), fake
  clock/sleep injection. `tests/test_protocol.py` locks the signing
  format to golden vectors via `tests/_golden_key.pem`.
- **House style** (CONTRIBUTING.md): Python 3.10+ syntax, type hints on
  public functions, tests for any new public behaviour, functions under
  ~50 lines, Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`).

## Known gaps — verified, not speculation

These are real mismatches between code and docs. Check whether a fix has
landed before acting on them.

1. **`sudo autoduo install` raises `IsADirectoryError`.**
   `cli.py:319` copies `REPO_DIR / "autoduo"` — the *package directory* —
   instead of `REPO_DIR / "scripts" / "autoduo"`. The shell path
   (`scripts/install.sh:38`) is correct. Use `bash scripts/install.sh`.
   Also: README says `sudo ./autoduo setup`, but no `./autoduo` exists at
   the repo root; the wrapper is `scripts/autoduo`.
2. **The audit log is never written.** `daemon.main()` opens the
   `--audit-log` stream (`daemon.py:433-441`) but never passes
   `audit_log_stream=` to `DuoBotDaemon(...)` (`daemon.py:489-495`), so
   `_emit_audit` no-ops. The systemd unit doesn't pass `--audit-log`
   either. README/CHANGELOG/SECURITY.md all promise `audit.log`.
3. **`redact.redact_transaction()` is dead code** — never called by the
   daemon. SECURITY.md claims PII is stripped "via redact_transaction()
   before logging"; it isn't. The audit fields written today are safe by
   construction, so this is a doc bug unless the audit payload grows.
4. **`vault.migrate()` has no caller.** `open_vault` hard-fails on a
   version mismatch. The schema-migration hook is a placeholder.
5. **Contradictory docstrings in `enroll.py`.** `_normalize_activation_url`
   says the endpoint wants a composite `{SHORT_CODE}-{base64(API_HOST)}`
   code; `_post_activation` posts the *short* code and says so itself.
   The implemented path is `/activate/CODE` → short code → POST.
6. **Error paths sleep twice** — `_run_once` sleeps on the error branch
   and the outer `run()` loop sleeps again.
7. **`.slim/deepwork/` is stale and gitignored.** It describes a `duo_bot/`
   package and `/var/lib/duo-bot/` paths, both since renamed to
   `autoduo/` and `/var/lib/autoduo/`. Useful for intent; not current
   fact. Never publish it.
8. **Clock accuracy is functional.** `x-duo-date` is inside the signed
   canonical string, so skewed time ⇒ auth failures. Both installers
   enable `systemd-timesyncd` for this reason.
9. **The shipped unit uses `--no-passphrase`** — the vault is sealed with
   an empty passphrase and relies on filesystem permissions. Deliberate
   (documented in SECURITY.md) but it is the default posture.

## Paths

- This checkout is `/opt/duo-bot`, but every deployed path assumes
  `/opt/autoduo`. `cli.py` derives `REPO_DIR` from `__file__`;
  `scripts/autoduo`, `scripts/install.sh` and `systemd/autoduo.service`
  hardcode `/opt/autoduo`.
- Runtime data: vault `/var/lib/autoduo/vault.enc`, state
  `/var/lib/autoduo/seen.json`, logs `/var/log/autoduo/`.
- `scripts/install.sh` defaults `REPO_DIR=/opt/autoduo`; export
  `REPO_DIR` to point it at a checkout elsewhere.

## Rules for agents

- Never add a dependency without discussing it. The current set is
  `requests` + `cryptography` and that is deliberate.
- Never commit secrets, activation URLs, vault contents, or real
  `urgid`/`txid` values. `.gitignore` already excludes `*.pem`, `*.enc`,
  `*.key`, `passphrase.txt`, `activation-url.txt`.
- Don't weaken the host allowlist, the redaction, the file-mode checks,
  or the `allow_abbrev=False` argparse settings.
- Keep tests offline. If you need to exercise HTTP, extend
  `MockTransport` rather than making a real request.
- Add a test with any new public function or behaviour.
- This project intentionally defeats 2FA. Do not extend it to other
  accounts, other 2FA providers, or add any covert/undisclosed mode.
