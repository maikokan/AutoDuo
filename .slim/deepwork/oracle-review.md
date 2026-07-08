# Oracle Review: `/opt/duo-bot/.slim/deepwork/plan.md`

**Verdict summary**

| Section | Verdict |
| --- | --- |
| 1. Architecture correctness | Approve with N changes |
| 2. Security | Approve with N changes |
| 3. Simplification & readability | Approve with N changes |
| 4. Phase plan | Reject (resequence) |

Overall: this is a competent, well-scoped plan with a clear threat-model
acknowledgement and a sane module split. The pieces that need attention are
operational: race conditions in the polling loop, idempotency under retries,
secrets handling at the environment-file boundary, and a phase plan that
front-loads risky integration before the daemon is observable in isolation.

---

## 1. Architecture correctness — Approve with N changes

The protocol choice is correct: `/push/v2/device/*` is the right surface for
a headless auto-approver. The module split (`protocol` / `vault` / `client` /
`daemon` / `enroll`) is sound. The systemd unit is mostly right. Issues below
are about correctness gaps the plan as-written will let through.

### 1.1 Time skew between daemon clock and Duo's `X-Duo-Date` — HIGH
**Problem.** `protocol.now_rfc1123()` will use local clock; if the VPS drifts
by even a few minutes (NTP off, suspended VM), Duo rejects signatures as
"invalid date" and the daemon spins forever on what looks like an auth error.
The plan never mentions clock synchronization.

**Fix.** Add `Wants=time-sync.target` and `After=time-sync.target` to the unit
(install.sh should `apt-get install -y systemd-timesyncd` if missing). In
`protocol.now_rfc1123()`, accept a clock-source injection point so tests can
pin time. On `AuthError` with `X-Duo-Date` in the response, log a one-shot
warning ("clock skew suspected").

### 1.2 Concurrent approvals racing on the same `urgid` — HIGH
**Problem.** `daemon.py` says "loop: list → for each tx approve → log →
sleep." If `list_transactions()` returns N pending tx and the network is
slow, the loop takes long enough that the *next* poll sees the same tx
again (Duo holds them pending until expiry). The plan does not deduplicate
in-process. Combined with no idempotency token on approve, a restart mid-
loop will double-approve — Duo's API tolerates this but it pollutes logs
and risks being rate-limited.

**Fix.** In `daemon.py`, keep an in-memory LRU set of recently-seen `urgid`
values (TTL = Duo's push TTL, typically 60 s, configurable). Approve only
urgids not in the set. Persist the set to `/var/lib/duo-bot/seen.json` on
graceful shutdown and load it on start, with a max size to bound growth.
The plan should call out: "approve is naturally idempotent on Duo's side,
but client-side dedup keeps logs clean and avoids 429s."

### 1.3 Retry/backoff does not distinguish 429 from 5xx — MEDIUM
**Problem.** Plan says "retries with exponential backoff on 5xx / network
errors." 429 (`X-RateLimit-*`) needs a different treatment: respect
`X-RateLimit-Retry-After` if present, and never exponential-backoff below
the server-supplied wait. Otherwise a rate-limit response will be retried
*too soon* after backoff resets, escalating the throttle.

**Fix.** In `client.py`, branch on status: 429 → sleep exactly
`Retry-After` seconds (no jitter, no backoff); 5xx / network → existing
exponential backoff capped at 60 s; 4xx (non-429) → raise immediately, no
retry (config/auth bug, not transient).

### 1.4 `health_check()` is underspecified and probably wrong — MEDIUM
**Problem.** Plan claims `GET /push/v2/ping` "works unsigned for /auth/v2/"
and suggests a "signed noop" for `/push/v2/`. This is hand-waved. If the
"noop" is `/push/v2/device/transactions` itself, that conflates health
with polling and means the daemon will approve 0-pending-state as a
"healthy" signal, which is fine, but it should not pollute the approve
path. The plan needs to pick: is health a separate signed endpoint, or is
"successful list_transactions with 0 pending" the health signal?

**Fix.** Choose the latter: drop `health_check()` from the API and treat
"200 from `list_transactions`" as the liveness probe. Remove the
under-specified ping wrapper. Watchdog stays in systemd (`Restart=always`
is the actual health system).

### 1.5 No mention of HTTPS verification / cert pinning posture — MEDIUM
**Problem.** `requests.Session` defaults to cert verification on; that's
fine, but the plan never states it. Worse: `host` comes from the
activation response (`{akey, pkey, host}`). If Duo's activation returns
`api-XXXXXX.duosecurity.com`, that's correct; but the daemon should
refuse to start if `host` does not end in `.duosecurity.com` (or a
configured allowlist). Activation MITM via a tampered response is
narrow but real on a compromised VPS.

**Fix.** In `client.py` constructor, validate `host` against an allowlist
suffix (default: `.duosecurity.com`, configurable). Raise `AuthError` on
mismatch. State explicitly in README: "TLS verification on, host
allowlist enforced."

### 1.6 Vault payload schema is undefined — LOW
**Problem.** `vault.py` seals `dict` but the plan never specifies keys
beyond naming `{akey, pkey, host, private_key_pem}`. Future you (or a
contributor) adds `created_at`, `last_used_at`, etc., and now there's a
migration question on existing vaults.

**Fix.** Add a `SCHEMA_VERSION = 1` integer in the sealed payload, with
a `vault.py:migrate(payload) -> dict` hook (no-op at v1). Document in
`vault.py` docstring.

---

## 2. Security — Approve with N changes

Threat model is explicitly acknowledged ("defeats 2FA by design") and
that's the right move — hiding the risk would be worse. The issues below
are about defense-in-depth, not about whether the project should exist.

### 2.1 Environment file is a passphrase vector — HIGH
**Problem.** `enroll.py` takes `--passphrase-env VAR`, systemd unit reads
`EnvironmentFile=/etc/duo-bot/duo-bot.env`, and the plan implies the
vault passphrase will live there. An env file readable by the
`duo-bot` user (mode 0600, owner `duo-bot`) means: anyone who can read
files as that user — or anyone who can exploit the daemon process — has
both the sealed vault *and* the key to unseal it. The vault is then
indistinguishable from plaintext.

**Fix options (pick one):**
- **(a)** Use systemd `LoadCredential=` + `LoadCredentialEncrypted=`
  (`/etc/duo-bot/duo-bot-vault.key` with `SetCredentialEncrypted:`).
  systemd decrypts per-unit at runtime; passphrase never sits on disk
  in cleartext.
- **(b)** Don't use a passphrase at all — seal the vault with a key
  derived from a TPM2 seal, or just rely on file permissions + the
  systemd unit's `ProtectSystem=strict` (which already isolates the
  process). If the VPS is rooted, you have bigger problems than the
  vault.

The plan should at minimum say which it is. Right now it quietly
implies option (a) without acknowledging option (b) is viable when
`ProtectSystem=strict` + `NoNewPrivileges` + a dedicated user is
already enforcing isolation.

### 2.2 Private key never needs to leave the daemon — MEDIUM
**Problem.** `vault.py` round-trips the full RSA private key in and out
on every daemon start. The daemon is the only thing that uses it.
Loading plaintext into memory is unavoidable, but: (a) the daemon
should `mlock()` the memory region holding the unsealed key+passphrase,
(b) the key should not be re-extracted to logs on errors.

**Fix.**
- Add `ResourceLimit=MEMLOCK=64M` to the unit so the daemon can call
  `ctypes`/`resource.setrlimit` to lock the buffer.
- In `vault.py`, on exception, log the *event* (`"vault open failed: %s"`)
  not the payload bytes. Add a linter rule / review checklist line:
  "no `repr(payload)` of unsealed vault contents."
- `daemon.py`: zero and free the key buffer on SIGTERM (best-effort in
  CPython; document that).

### 2.3 Activation URL handling — MEDIUM
**Problem.** `enroll.py` accepts `--activation-url URL` and parses out
the code. Activation URLs are one-shot bearer secrets. The plan doesn't
say:
- Whether the URL is logged (it must not be).
- Whether the URL is kept in shell history (it will be).
- Whether the URL is passed via stdin, file, or argv (argv leaks via
  `/proc/<pid>/cmdline` and `ps`).

**Fix.** Prefer `--activation-file PATH` (mode 0600, deleted after read)
or `--activation-stdin` (read from stdin). Refuse `--activation-url`
on the command line in production builds; if kept for ergonomics, log a
prominent warning. Add `set -o noclobber` / guidance in the wrapper
script.

### 2.4 Audit log includes enough to be useful — LOW (positive gap)
**Problem.** Plan says "log urgid, factor, app, time, IP if returned."
This is good. It does not say whether logs are tamper-evident or
whether failed approve attempts are logged. A user trying to detect
"someone else is approving my pushes" needs:
- Every poll cycle's transaction count (or absence).
- Every approve success and failure with the response status.
- Wall-clock + monotonic timestamp.

**Fix.** Spec the JSON log shape in the plan:
```json
{"ts":"...","mono":"...","event":"tx_seen","urgid":"...","factor":"...","app":"..."}
{"ts":"...","event":"approve_ok","urgid":"...","http":200}
{"ts":"...","event":"approve_fail","urgid":"...","http":429,"retry_after":12}
```
A dedicated event for "poll returned 0 tx" once per N minutes (heartbeat)
makes "is it still running?" answerable from logs without journalctl.

### 2.5 systemd hardening: missing `RestrictAddressFamilies` — LOW
**Problem.** `ProtectSystem=strict`, `NoNewPrivileges`, `PrivateTmp`,
`ProtectHome` are all there. Good. Missing: `RestrictAddressFamilies=AF_INET
AF_INET6 AF_UNIX` (block netlink/raw); `LockPersonality=true`;
`RestrictRealtime=true`; `RestrictSUIDSGID=true`; `SystemCallArchitectures=native`;
`MemoryDenyWriteExecute=true` (caveat: breaks `cryptography` if it JIT —
test before shipping); `CapabilityBoundingSet=`.

**Fix.** Add the above to the unit. Test `MemoryDenyWriteExecute=true`
specifically: `cryptography` uses cffi + libssl which is usually fine,
but verify `pip install -r requirements.txt && python -c "from cryptography.hazmat.primitives.asymmetric import padding"` works under it.

### 2.6 Rate-limit / abuse response missing — MEDIUM
**Problem.** If Duo's anomaly detection fires on the polling pattern,
the worst case is: account lockout. The plan does not mention a
"circuit-breaker" or a "back off hard and alert" mode for repeated 401/
403/empty-bodies responses. Right now the daemon would happily keep
polling at 5 s, making the situation worse.

**Fix.** Define a `RepeatedAuthError` heuristic: N auth failures in M
seconds → enter "alert and sleep 5 min" mode, log a high-severity event,
Telegram- or systemd-notify the operator (operator can decide whether to
`systemctl stop duo-bot`).

---

## 3. Simplification & readability — Approve with N changes

The plan is appropriately minimal; I don't want to gold-plate it. But
there are places where the design is *just barely* over-engineered or
where the plan's prose obscures intent.

### 3.1 `pyproject.toml` is YAGNI if there are no installs — LOW
**Problem.** Plan adds `pyproject.toml` "minimal, just metadata." If
nothing depends on it (no `pip install -e .`, no entry points, no
build), it's noise. `duo_bot/__init__.py` with `__version__` and a
`--version` flag is enough.

**Fix.** Drop `pyproject.toml` unless `setuptools_scm` or entry-point
console_scripts are used. If entry-points are added (`duo-bot`,
`duo-bot-enroll` as console scripts), keep it and document why.

### 3.2 `LICENSE` file vs. `License: MIT` header — LOW
**Problem.** Adding `LICENSE` file as a separate deliverable is fine
but the plan doesn't say it goes alongside SPDX headers in source. Pick
one source of truth.

**Fix.** Either:
- Plain `LICENSE` file + SPDX `License: MIT` header at top of every
  .py file (this is the modern norm), OR
- `LICENSE` file only, no per-file header.

### 3.3 `protocol.py` API surface could collapse — LOW
**Problem.** `canonical_request`, `sign`, `build_headers`, `now_rfc1123`
are 4 public functions for one signing operation. That's fine, but
they're called sequentially in exactly one place (`client.py`). The
4-function split is good for testing (canonical-string golden vector,
sign determinism, header shape), so **keep it** — but the plan should
say so explicitly. Right now it just lists them.

**Fix.** Add a one-line note: "split is for unit-testability; the only
production caller is `client._signed_request()`." This preempts a
future "why four functions?" refactor.

### 3.4 `logging_setup.py` is one module too many — LOW
**Problem.** A `logging_setup.py` module for a JSON formatter + 2
handlers is over-modularized. It's used in exactly two places (daemon,
enroll).

**Fix.** Inline the logging config in `daemon.py` (and a smaller copy
in `enroll.py`), or put it as a private function inside `daemon.py`
and have `enroll.py` import it. Drop the module.

### 3.5 Shell scripts add value or noise? — LOW
**Problem.** Plan adds `scripts/duo-bot-enroll.sh`, `scripts/duo-bot-daemon.sh`,
`scripts/duo-bot-vault-init.sh`. systemd unit uses `ExecStart=python -m
duo_bot.daemon`, so `duo-bot-daemon.sh` is dead code unless the unit is
rewritten. The wrapper scripts add little over `python -m ...`.

**Fix.** Drop `duo-bot-daemon.sh`. Keep `duo-bot-enroll.sh` *only* if
it does meaningful work (sudo handling, env-var marshaling). Otherwise
drop all three and document `python -m duo_bot.enroll` and
`python -m duo_bot.daemon --help` in the README.

### 3.6 Configuration via env file vs. CLI flags — LOW
**Problem.** `duo_bot.config.py` "loads vault path, poll interval, log
path." The plan doesn't say where these come from. systemd unit uses
`EnvironmentFile`, so env vars is the answer. But `enroll.py` is a
one-shot CLI; mixing env + argv is annoying.

**Fix.** Rule: env vars for daemon-only tunables (`POLL_INTERVAL`,
`LOG_PATH`, `VAULT_PATH`). argv only for `enroll.py` (one-shot).
Document the env var names with `DUO_BOT_` prefix.

### 3.7 README should not duplicate the plan — LOW (housekeeping)
**Problem.** Plan repeatedly says "(will be in README)" for threat
model, install steps, etc. That's fine, but the plan shouldn't ship
without a README outline. The reader of the plan has no idea what
shape the README takes.

**Fix.** Add a one-section outline to the plan:
```
README sections:
  1. What this is (3 lines)
  2. Threat model (paste from §6)
  3. Install (paste from §5)
  4. Enroll (step-by-step, including the "tap Approve once" warning)
  5. Verify (how to read logs / test a push)
  6. Uninstall
```

### 3.8 Type hints are mentioned for nothing — NIT
**Problem.** `protocol.py` signatures are typed. Nothing else is. Either
type everything or note that the project uses minimal typing.

**Fix.** Either turn on `mypy --strict` in CI (but there's no CI yet —
out of scope) or add a docstring at the top of `duo_bot/__init__.py`
saying "function signatures use PEP 604 unions where helpful; full
typing is not enforced."

---

## 4. Phase plan — Reject (resequence)

The phasing is wrong in two ways: it puts risky integration too early
(items 2.1, 2.4 below) and it lacks an explicit "smoke test against
Duo's sandbox/demo" gate (item 2.5). Rejecting is harsh; it's really
"approve with restructuring."

### 4.1 Phase 1 ships `enroll.py` before the daemon exists — HIGH
**Problem.** Phase 1 includes `enroll.py`, which writes the vault. But
Phase 1 does not include a way to *use* the vault outside the daemon.
If `enroll.py` runs successfully and the developer wants to verify
the activation round-trip, they have to either:
- Skip it (test only the client against mocks — fine but useless for
  catching activation bugs).
- Run it for real against `api-XXXXXX.duosecurity.com` — risky and
  requires a real activation URL.

**Fix.** Split Phase 1 into:
- **1a:** `protocol.py` + `vault.py` + `tests/`. Pure unit tests.
  *Gate:* `pytest` green.
- **1b:** `client.py` + `tests/` (mock transport). *Gate:* `pytest`
  green, including a "signed request matches golden vector" test.
- **1c:** `enroll.py` + `logging_setup.py`. *Gate:* `--help` works;
  integration test against Duo sandbox (`duo_api_swift` has a sandbox
  mode — verify activation round-trip in CI/local, *not* in production).

### 4.2 Phase 2 daemon has no integration smoke test — HIGH
**Problem.** Phase 2 ships `daemon.py` with `test_daemon.py` that uses
fake clock + fake transport. That's fine for unit logic, but the
daemon never gets tested against a real Duo endpoint with a real vault.
The first time the daemon runs in production, it's on real creds.

**Fix.** Add a **Phase 2b: staging run.** Run the daemon for 5 minutes
against Duo's demo/sandbox API with a test vault, capture logs, verify
the heartbeat + tx_seen events. Document this as the gate to Phase 3.
Alternatively: provide a `duo_bot.fake_daemon` mode that points at
`httpbin`-style mock for end-to-end smoke.

### 4.3 Phase 3 verification list is incomplete — MEDIUM
**Problem.** Phase 3 says: pytest green, compileall clean, systemd
analyze verify, --help/--version work. Missing:
- `systemd-analyze security duo-bot.service` — runs the unit through
  the security sandbox checker; will surface missing hardening.
- `python -m venv .venv && pip install -r requirements.txt` from a
  fresh checkout — confirms install path.
- `shellcheck scripts/*.sh` — the shell scripts are unlinted.
- `bandit -r duo_bot/` — security linter for Python.
- A real `--version` and `--help` for `daemon` (it might not currently
  have one; the plan doesn't say).

**Fix.** Add these to Phase 3 explicitly:
```
- systemd-analyze security duo-bot.service  (no warnings)
- systemd-analyze verify duo-bot.service    (unit valid)
- shellcheck scripts/*.sh                   (clean)
- bandit -r duo_bot/                        (no high/medium)
- python -m compileall duo_bot/             (no syntax errors)
- python -m pytest                          (all green)
- python -m duo_bot.daemon --help           (works)
- python -m duo_bot.enroll --help           (works)
```

### 4.4 No rollback / uninstall story in any phase — MEDIUM
**Problem.** Plan installs systemd unit + user + dirs but never says how
to uninstall cleanly. Operators will at some point want to remove this.

**Fix.** Add a one-liner in the install section: `scripts/uninstall.sh`
that does the inverse of `install.sh`. Don't make uninstall clever.

### 4.5 Phases don't list what's blocking — LOW (process)
**Problem.** Phases list deliverables but not blockers or decision
points. e.g. Phase 1 will block on: "what's the Duo sandbox URL?" —
plan doesn't answer. Phase 2 will block on: "do we want Telegram
notifications?" — plan says no, good, but other implicit decisions
("should the daemon run as root or as `duo-bot` user during
development?") are unanswered.

**Fix.** Add a "Decisions to lock before Phase N" subsection per phase.
E.g.:
```
Phase 2 decisions:
- Run dev daemon as root or duo-bot user? (default: duo-bot, even in
  dev, to exercise the perms path)
- Log to journal or to file in dev? (default: both; matches prod)
```

### 4.6 Phase ordering: tests before protocol implementation? No. — NIT
**Problem.** Phase 1 says `protocol.py + tests/test_protocol.py`
together. That's fine, but the canonical-string golden vector test
should be written *before* the implementation, TDD-style, to lock the
expected output. Otherwise the test is just a regression on whatever
the implementation happens to produce.

**Fix.** State explicitly: "Write `tests/test_protocol.py` golden
vectors from the Duo protocol docs *before* implementing
`protocol.py`." This is the only place TDD discipline actually
matters — everything else is integration.

---

## Cross-cutting recommendations (priority-ordered)

1. **Adopt `LoadCredentialEncrypted`** for the vault passphrase
   (or drop the passphrase entirely if you're trusting perms + systemd
   sandbox). Don't put it in a plain env file. (Sec 2.1)
2. **Add in-process `urgid` deduplication** with persistence. (Arch 1.2)
3. **Distinguish 429 from 5xx** in retry logic. (Arch 1.3)
4. **Split Phase 1 into 1a/1b/1c** with explicit gates and add Phase 2b
   staging. (Phase 4.1, 4.2)
5. **Host allowlist** on the activation response. (Arch 1.5)
6. **Add `time-sync.target` dependency** to the unit. (Arch 1.1)
7. **Extend systemd hardening** with the missing sandboxes (Arch/Sec
   2.5).
8. **Spec the JSON log shape** in the plan, not just in the eventual
   code. (Sec 2.4)
9. **Drop `pyproject.toml` and `logging_setup.py`** unless justified.
   (Simp 3.1, 3.4)
10. **Add uninstall script.** (Phase 4.4)

## Bottom line

Plan is 80% there. The 20% is operational: rate-limit handling, dedup,
secrets-at-rest posture, phase gates with real integration smoke tests,
and systemd hardening completeness. None of these are architectural
rethink — they're table-stakes fixes for a daemon that holds a
defeats-2FA credential on a VPS. Address the high-priority items
(1.1, 1.2, 1.3, 1.5, 2.1, 4.1, 4.2) before Phase 1 starts and the
rest can ride along.