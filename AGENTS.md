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
.venv/bin/python -m autoduo.cli instances
.venv/bin/python -m autoduo.cli watch          # one frame when piped
.venv/bin/python -m autoduo.enroll --help
.venv/bin/python -m autoduo.daemon --help

# Full CI parity (all of `.github/workflows/test.yml`)
.venv/bin/python -m pytest tests/ -v
.venv/bin/bandit -r autoduo/ -ll
.venv/bin/python -c "import autoduo, autoduo.client, autoduo.daemon, autoduo.enroll, autoduo.protocol, autoduo.redact, autoduo.vault, autoduo.instances, autoduo.dashboard, autoduo.ui"
python3 -m compileall autoduo/
shellcheck install.sh scripts/autoduo scripts/install.sh scripts/uninstall.sh
systemd-analyze verify systemd/autoduo.service systemd/autoduo@.service
logrotate -d scripts/logrotate-autoduo
```

CI runs pytest + bandit + an import smoke test on Python 3.10, 3.11, 3.12,
plus a lint job for shellcheck (every script, including `install.sh`),
compileall, `systemd-analyze verify` (both units) and a `logrotate -d`
parse of the rotation config.

## Architecture

| File | Responsibility |
|---|---|
| `autoduo/__init__.py` | `__version__` only. |
| `autoduo/__main__.py` | `python -m autoduo` → `cli.main()`. |
| `autoduo/cli.py` | Operator CLI (`setup`/`install`/`enroll`/`start`/`status`/`instances`/`watch`/…). Mostly `subprocess` wrappers around `systemctl`/`apt-get` + interactive prompts. Does **not** talk to Duo directly — it shells out to `python -m autoduo.enroll`. |
| `autoduo/instances.py` | The single source of truth for where an instance lives: name validation, path resolution, legacy mapping, and `status()`/`status_all()`. Never prints, never exits, never raises — it runs inside the live dashboard. |
| `autoduo/ui.py` | Presentation primitives (accent, headings, `kv`, `rule`, state words). Colour only on a TTY, never with `NO_COLOR`; every primitive has a plain fallback. |
| `autoduo/dashboard.py` | `autoduo watch`: the live multi-instance view. Alternate screen on a TTY, one plain frame when piped. |
| `autoduo/enroll.py` | One-shot enrollment CLI. URL → `(host, code)`, RSA-2048 keypair, POST `/push/v2/activation/<code>`, seal the vault. |
| `autoduo/vault.py` | Encrypted container `DVB1`: magic(4)+schema(4)+salt(32)+nonce(12)+ciphertext. PBKDF2-HMAC-SHA256 ×600k → AES-256-GCM. AAD = magic+version+salt. Also the instance lock (`acquire_instance_lock`). |
| `autoduo/protocol.py` | Request signing: canonical string, PKCS#1 v1.5 / SHA-512, `Authorization: Basic base64(pkey:b64sig)` + `x-duo-date`. |
| `autoduo/client.py` | `DeviceClient`: signed HTTP, retry taxonomy, `.duosecurity.com` allowlist, opt-in traffic log, `ProtocolError`/`ApprovalRejected` for `stat != "OK"`. |
| `autoduo/redact.py` | The log boundary. `akey`/`pkey` are length-only (`<28 chars:hidden>`); `Authorization`/`x-duo-date` replaced wholesale. |
| `autoduo/daemon.py` | The poll→dedup→approve loop, circuit breaker, backoff, JSON event log, signals, `seen.json` persistence, single-instance lock. |

### Runtime flow

1. systemd starts `python -m autoduo.daemon` — either `autoduo.service`
   (instance `default`, legacy paths) or `autoduo@<name>.service`.
2. Daemon takes a `flock` on `<vault>.lock` **before** opening the vault.
   A second copy exits 5 and the unit's `RestartPreventExitStatus=5` stops
   it from restart-looping the copy that is running.
3. Daemon opens the vault → builds `DeviceClient(host, pkey, akey, private_key_pem)`.
4. Loop: `GET /push/v2/device/transactions` (signed) → dedup by `urgid`
   (TTL 60 s, persisted to `seen.json` every 60 s) → for unseen urgids:
   mark seen **before** calling, then `POST /push/v2/device/transactions/<urgid>`
   with `answer=approve`.
5. A response is only a success if `stat == "OK"`. A poll that answers
   `stat != "OK"` raises `ProtocolError`; an approval that does raises
   `ApprovalRejected`, which is classified as terminal (parked, never
   retried), auth-class (allowlist, feeds the circuit), or transient
   (retried up to `MAX_APPROVE_ATTEMPTS`, then parked).
6. An approval whose 2xx body cannot be read (not JSON, not an object) or
   whose response is a 3xx raises `ProtocolError` and gets the same bounded
   retry-then-park treatment: we cannot know whether it landed, so it is
   neither a success nor a credential failure.
7. Errors branch on type: `AuthError` and auth-class rejections → circuit
   breaker (≥5 in 300 s → 5 min cooldown), `NetworkError` → exponential
   backoff (base 1 s, cap 60 s), `RateLimitedError` → its own backoff and
   **not** the circuit. `Retry-After` is clamped to 0–300 s.
8. `run()` sleeps once per cycle: a cycle that already backed off sets
   `_waited_this_cycle`, so the loop does not add `poll_interval` on top.
9. Housekeeping (eviction, state save, heartbeat, auth clearing) runs
   through `_bookkeeping`, which logs `loop_error` and continues. A crash
   that escapes anyway is written as `fatal_error` and exits 6 — 5 is
   reserved for "another copy holds the lock", which systemd must not retry.

### Instance layout

| | `default` (legacy) | `<name>` |
|---|---|---|
| vault | `/var/lib/autoduo/vault.enc` | `/var/lib/autoduo/instances/<name>/vault.enc` |
| state | `/var/lib/autoduo/seen.json` | `/var/lib/autoduo/instances/<name>/seen.json` |
| logs | `/var/log/autoduo/` | `/var/log/autoduo/<name>/` |
| unit | `autoduo.service` | `autoduo@<name>.service` |

Instance names are validated to `[a-z0-9][a-z0-9_-]{0,31}` before reaching a
path or a unit name. Two accounts are two processes; there is deliberately no
multi-account vault, because Duo enrolls one device per user.

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
landed before acting on them. The first audit's findings are all closed and
recorded in `ROBUSTNESS.md`; what remains is smaller.

1. **`.slim/deepwork/` is stale and gitignored.** It describes a `duo_bot/`
   package and `/var/lib/duo-bot/` paths, both since renamed to
   `autoduo/` and `/var/lib/autoduo/`. Useful for intent; not current
   fact. Never publish it.
2. **Clock accuracy is functional.** `x-duo-date` is inside the signed
   canonical string, so skewed time ⇒ auth failures. Both installers
   enable `systemd-timesyncd` for this reason.
3. **The shipped unit uses `--no-passphrase`** — the vault is sealed with
   an empty passphrase and relies on filesystem permissions. Deliberate
   (documented in SECURITY.md) but it is the default posture. A loose vault
   is at least reported now (`vault_mode_insecure`).
4. **`_evict_expired` assumes `seen.json` is in chronological order** and
   stops at the first unexpired entry. Every writer preserves that order, so
   it holds today; a hand-edited file only keeps stale dedup keys longer
   than the TTL, which cannot skip a real push. Do not rely on the order
   for anything else.
5. **A `Retry-After` HTTP-date is ignored** — the parser accepts seconds
   only and falls back to 30 s for a date, which is a safe default but not
   the value the server asked for.
6. **The dashboard's live mode needs a TTY and `NO_COLOR` unset.** With
   `NO_COLOR` on a terminal it prints one frame and exits 0, by design; a
   script cannot ask for N frames.

## Paths

- This checkout is `/opt/duo-bot`, but every deployed path assumes
  `/opt/autoduo`. `cli.py` derives `REPO_DIR` from `__file__`;
  `scripts/autoduo`, `scripts/install.sh` and `systemd/autoduo.service`
  hardcode `/opt/autoduo`. On this host `/opt/autoduo` is a symlink to the
  checkout.
- Runtime data: vault `/var/lib/autoduo/vault.enc`, state
  `/var/lib/autoduo/seen.json`, logs `/var/log/autoduo/` (instance
  `default`); a named instance uses
  `/var/lib/autoduo/instances/<name>/` and `/var/log/autoduo/<name>/`.
  `autoduo.instances.resolve(name)` is the only place that decides this.
- The lock file beside a vault is `<vault>.lock` — in practice
  `vault.enc.lock`, not the `vault.lock` that older notes mention.
  `InstancePaths.actual_lock` returns the real one.
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
