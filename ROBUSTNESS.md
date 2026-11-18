# Robustness audit & improvement roadmap

*2026-11-18 · baseline: `main` @ ff477bf · 114 tests passing · revised after review*

> **Status.** Wave A is implemented on top of this baseline: 1.1, 1.2, 2.1,
> 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 3.1, 3.3 and 4.1 are done, 161 tests pass, and
> `bandit -r autoduo/ -ll` reports no issues. Still open: 1.3 (needs the
> live-tenant measurement), 3.2, 3.4, 3.5, 4.2–4.6. Two things this audit
> predicted were confirmed on the live host: `audit.log` did not exist, and
> `/var/lib/autoduo` was 0755.
>
> The audit's 1.3 risk is **latent, not active**: of 215 approvals logged on
> the production tenant, every urgid appears exactly twice — one `tx_seen`,
> one `approve_ok` — so the minimal `answer=approve` payload with no `txId`
> header was accepted every time. The divergence is still worth closing, but
> it is not the cause of a broken deployment.

This is an audit of how AutoDuo fails, not a feature wishlist. Every item was
verified by reading the code, and the load-bearing ones were reproduced with
a probe whose output is quoted below. Where a claim depends on Duo's server
behaviour it is marked **VERIFY**, with the experiment that settles it.

The reference for "what the protocol actually requires" is the open-source
[DuoBreak](https://github.com/JesseNaser/DuoBreak) client (v2.0.0), which
speaks the same device protocol against a live tenant.

**Revision note.** A first draft of this audit mis-diagnosed the Verified Push
failure (it cannot trip the circuit breaker — see 3.1), claimed no test
exercises `main()` (one does — see 4.1), and under-ranked the poll path.
Those are corrected here.

## Status after Wave A

Everything in this table is verified against the code, not intended. The rest
of the document is left as written — it is the record of why each change was
made, and the reasoning is still the reasoning.

| Item | State |
|---|---|
| 1.1 poll `stat` / malformed body | **shipped** — raises `ProtocolError`, three doors closed |
| 1.2 approval `stat` | **shipped** — `ApprovalRejected`, never logged `approve_ok`, never audited |
| 1.3 approval payload | **open** — see the live evidence below; latent, not active |
| 2.1 audit stream | **shipped** — `audit_log_stream` wired, `--audit-log` in both units |
| 2.2 vault mode window + fsync | **shipped** — `mkstemp` 0600, fsync file + dir, parent tightened only if created |
| 2.3 `/tmp` activation file | **shipped** — `mkstemp`, deleted in `finally` |
| 2.4 passphrase file mode | **shipped** — `mode & 0o077` checked at startup |
| 2.5 single instance | **shipped** — bounded `flock` beside the vault, exit **5**, never unlinked |
| 2.6 signal handlers | **shipped** — plain bool flags, logging on the loop; reopen deliberately cut |
| 2.7 expired transactions | **shipped** — skipped and parked |
| 3.1 circuit breaker | **shipped** — auth-class allowlist, per-urgid cap, 429 no longer counts |
| 3.2 watchdog | open |
| 3.3 CI parity | **shipped** — `bandit -r autoduo/ -ll` exits 0 with no `--exclude`; nested venv deleted |
| 3.4 double sleep on error paths | open |
| 3.5 `seen.json` order / bound | open |
| 4.1 `main()` assertions | **shipped** — `--audit-log` and `--state-file` asserted non-empty |
| 4.2 installer unification | partial — `cli.py:319` fixed, `install.sh` vault dir now 0700; three installers still exist |
| 4.3 dead code | partial |
| 4.4 dependency pins | open |
| 4.5 dry-run | open |
| 4.6 clock skew | open |

**Evidence that 1.3 is latent.** The live deployment on this host has made 215
approvals since 2026-07-10. Each ursid appears exactly twice in `daemon.log` —
one `tx_seen`, one `approve_ok` — and **never more**. A rejected approval stays
in Duo's transaction list and would be re-approved after `push_ttl`, showing up
four, six, eight times. The minimal `answer=approve` body is therefore accepted
by this tenant today. That does not make it safe: it is an undocumented
dependency on server-side leniency, and it can only be measured on a live
tenant with 1.2's logging in place.

**One correction found in production.** `audit.log` did not exist on the host —
exactly as 2.1 predicted — which is the cheapest possible confirmation that the
Tier-2 items were real and not theoretical.

## How to read this

Ranked by **blast radius, not effort**:

| Tier | Meaning |
|---|---|
| **1** | The daemon cannot tell whether it is working, and nothing says so. |
| **2** | Silently defeats a guarantee the project advertises. |
| **3** | A defect in the safety net itself; you cannot see it failing. |
| **4** | Multiplies future bugs; slows every later change. |

## Tier 1 — the daemon cannot tell whether it is working

Both items below end in the same place: a healthy-looking daemon that
approves nothing. They are one failure mode with two entry points, and a fix
for one does not cover the other.

### 1.1 The poll path is never validated: a failed poll looks like "no pushes"

`list_transactions()` (`client.py:112-138`) inspects only the JSON *shape*.
If Duo returns HTTP 200 with `{"stat": "FAIL", "code": …, "message": …}` —
which it does for logical errors — none of the unwrapping branches match,
and the method silently returns `[]`.

Probe, real `DeviceClient` against a transport returning
`{"stat":"FAIL","code":40301}`:

```
M1  list_transactions() -> []
M1b events emitted for stat=FAIL poll: ''
```

Then `daemon.py:265-267`:

```python
if not txs:
    self._maybe_heartbeat()
    return
```

So the daemon logs **nothing**, the heartbeat keeps ticking, `systemctl
status` is green, and every push goes unapproved for as long as the error
persists. This is the most dangerous state the product can be in: silent,
indistinguishable from idle, and permanent.

**Fix:** check `body["stat"] == "OK"` before unwrapping; on `FAIL`, raise a
typed `ProtocolError` carrying `code`/`message` so the daemon's existing
error paths engage and the event log records the reason. Do not swallow it
into `[]`.

**Verify:** the probe above should produce an error event, not silence.

### 1.2 An approval that failed is recorded as an approval that succeeded

`DeviceClient.approve()` returns the parsed body (`client.py:151-155`), but
the daemon logs only the status code (`daemon.py:308`):

```python
self._emit("approve_ok", urgid=urgid, http=result.http_status)
```

Duo reports logical failures as HTTP 200 with `{"stat": "FAIL", "code": …}` —
the reference proves this by handling `code == "40032"` *after*
`raise_for_status()` has passed. DuoBreak therefore gates on
`reply.get("stat") == "OK"`; AutoDuo gates on nothing.

Consequences, all silent: a rejected approval is logged `approve_ok`; an
audit record is written for it; and the urgid is marked seen, so it is
**never retried** — the login fails with no explanation anywhere.

**Fix:** treat `stat != "OK"` as failure. Emit `approve_rejected` with
`code`/`message`, write no audit record, and branch retry policy on the code
(`40032` is a wrong step-up code, not an auth failure).

### 1.3 …and the payload itself may be the reason it fails

AutoDuo's approval POST sends one field and no `txId` header
(`client.py:146-150`):

```python
params={"answer": "approve"}
```

The reference sends `akey, answer, fips_status, hsm_status, pkpush` **and**
sets `headers["txId"] = urgid`. Either AutoDuo works only because Duo
tolerates a partial payload — an undocumented dependency on server-side
leniency — or approvals are failing in the way 1.2 hides. Both are Tier 1
because the operator cannot tell which.

**VERIFY (needs a live tenant):** with 1.2's logging in place, trigger one
real push and read `stat`/`code` on the approval response. `OK` ⇒ latent
risk; `FAIL` ⇒ this is the root cause of a broken deployment.

**Fix after measuring:** mirror the reference payload and the `txId` header.

**Coupling — do not fix this without fixing it too.** `protocol.py:53` signs
`urlencode(sorted(params.items()))`, while `client.py:177-180` transmits
`params` in **dict insertion order**. Today they agree only because every
current parameter set happens to be alphabetical; the reference's five-field
body happens to be too. The moment a param is added out of order, the
signature no longer matches what the server reconstructs, and you get a 401
that is indistinguishable from clock skew (4.6). **The invariant to enforce
is signed order == transmitted order** — enforce it once, in
`canonical_request`, and add a test that asserts the two encodings match for
every real request.

## Tier 2 — silently defeats a guarantee the project advertises

### 2.1 The audit log is opened and then thrown away

```python
audit_stream = _open_audit_stream(args)          # daemon.py:457
...
daemon_obj = DuoBotDaemon(
    client=client, log_stream=log_stream,
    state_path=args.state_file,                  # daemon.py:489-495
)                                                # audit_log_stream NOT passed
```

`_emit_audit` returns immediately when its stream is `None`, and the shipped
unit passes no `--audit-log` either. So `audit.log` never exists, though
README, CHANGELOG and SECURITY.md all document it and SECURITY.md describes
its contents in detail.

**Fix:** pass `audit_log_stream=audit_stream`; add the flag to the unit;
extend the existing `main()` test to assert the file is non-empty (4.1).

### 2.2 The vault is briefly world-readable, and is written without fsync

`vault.py:100-114`:

```python
tmp = p.with_suffix(p.suffix + ".tmp")   # predictable name
tmp.write_bytes(blob)                    # 0644 under umask 022
os.rename(tmp, p)                        # still 0644
os.chmod(p, 0o600)                       # only now
os.chmod(parent, 0o700)                  # parent was 0755 until here
```

Reproduced:

```
tmp mode after write_bytes: 0o644
vault.enc mode after rename : 0o644 <- still permissive
parent dir mode             : 0o755
```

`scripts/install.sh:25` and `cli.py:304` create the parent 0755. Under the
shipped `--no-passphrase` posture the blob decrypts with an empty passphrase,
so any local reader in that window — or any crash inside it, which leaves the
file 0644 permanently — exposes `akey`, `pkey` and the private key.

Separately, there is no `fsync` before the rename and no directory fsync after
it, on either the vault or `seen.json` (`daemon.py:178-180`). The rename is
atomic for readers, but after power loss the replaced file can be zero-length
or partial on XFS, btrfs and overlayfs (ext4 `data=ordered` is usually, not
universally, safe). This file is the **only copy** of the device's RSA key;
losing it means re-enrollment and orphaning the device. DuoBreak fsyncs the
file and the parent directory for exactly this reason.

**Fix:** create the temp file with `os.open(..., O_CREAT|O_EXCL|O_NOFOLLOW,
0o600)` (or `mkstemp`), `fsync` it, `os.replace`, then fsync the directory;
create the parent 0700 *before* the first write. Same for `seen.json`.

### 2.3 Predictable `/tmp` path, written before it is chmodded

`cli.py:200` and `cli.py:436`:

```python
act_file = pathlib.Path("/tmp/autoduo-act.txt")
act_file.write_text(url)     # follows an existing symlink
act_file.chmod(0o600)        # 0600 only after the bytes are on disk
```

A pre-planted symlink makes **root** write the activation URL to an arbitrary
path, and between `write_text` and `chmod` the bearer secret is
world-readable. Same bug class as 2.2; `bandit` flags both as B108.

**Fix:** `tempfile.mkstemp(mode=0o600)` or `O_CREAT|O_EXCL|O_NOFOLLOW`;
delete in `finally`, not only on success.

### 2.4 The daemon accepts a world-readable passphrase file

Enrollment hard-fails when the passphrase file is group- or world-readable
(`enroll.py:265-270`). The daemon does a bare `read_text()`
(`daemon.py:462-467`). AGENTS.md lists the file-mode checks as a
non-negotiable invariant; the runtime side does not implement it, so the
exact exposure SECURITY.md warns about is accepted at startup.

**Fix:** share one `read_secret_file()` helper between `enroll` and `daemon`
that enforces `mode & 0o077 == 0`.

### 2.5 No single-instance guard

`systemctl start` plus a manual `python -m autoduo.daemon` is enough to run
two daemons: both poll, both see an unseen urgid before either saves state,
both POST `answer=approve` — duplicate approvals and a last-writer-wins race
on `seen.json`. DuoBreak takes an advisory `flock` on the vault and refuses a
second opener.

**Fix:** `flock` a lock file beside the vault at startup; exit with a
distinct code if held. Three lines. It is also a *precondition* for the 1.3
measurement (two daemons make the `stat` reading ambiguous).

### 2.6 Signal handlers do file I/O

Both handlers call `_emit()` (`daemon.py:353-373`). A signal handler runs on
the main thread between bytecodes, so it can fire **while the main thread is
inside `_emit`'s `write()`/`flush()`** on the same buffered stream and
re-enter it; `sighup_handler` additionally closes and replaces `self._log`
mid-write. This lands on the shutdown path and on every logrotate.

**Fix:** handlers set a flag only (`_stop_event.set()` is already the right
pattern); do the logging and the reopen in the main loop.

### 2.7 Expired transactions are approved

Transactions carry an `expiration` epoch; the reference skips expired ones.
AutoDuo never references the field, so it POSTs approvals for pushes that
have already timed out — wasted requests, and, with 2.1's fix, a stream of
`approve_rejected` events.

**Fix:** skip `time.time() >= expiration` and mark seen.

## Tier 3 — defects in the safety net

### 3.1 The circuit breaker cannot fire for approval errors

This was mis-diagnosed in the first draft as "circuit oscillates every 5
minutes". It never trips at all. `daemon.py:263` calls `_record_success()` on
every successful poll, and `_record_success()` (`daemon.py:214-217`)
**clears** `_auth_failures`. An approval-path `AuthError` is therefore erased
at the top of the next cycle.

Probe — a client whose approval always raises `AuthError`, 30 cycles:

```
W1  30 cycles -> {'tx_seen': 30, 'approve_auth_error': 30}
W1  circuit_open_until: None | auth_failures: 1
```

So a hostile transaction (Verified Push, expired push, rejected payload) is
retried **once every poll interval, for ever** — one POST per 5 s, ~17k a day,
with two log lines each and no backoff. The circuit breaker protects the poll
path only, which is not its primary use case.

**Fix:** keep authentication failures distinct from per-transaction
rejections. Count consecutive `AuthError`s from *either* path toward the
circuit; treat per-transaction `stat: FAIL` codes as transaction-scoped
(mark seen, no circuit impact) unless the code is auth-class. A
consecutive-failures-per-urgid cap is the cheap belt-and-braces.

### 3.2 No watchdog: a hung loop looks identical to a healthy one

The unit is `Type=simple` with `Restart=always`, which covers process death
only. The loop can block in the client's own backoff for up to 60 s, plus a
5-minute circuit cooldown, and nothing outside can distinguish healthy-idle
from wedged.

**Fix (stdlib only):** `Type=notify` + `NotifyAccess=main`, send `READY=1`
after the vault opens and `WATCHDOG=1` each cycle via a datagram to
`$NOTIFY_SOCKET`. **Condition it on 1.1/1.2:** a watchdog proves the loop
runs, not that approvals succeed, so include the timestamp of the last
successful approval in the `READY=1` payload and alert on "ready but no
approvals in N hours". Otherwise it certifies a daemon that approves nothing.

### 3.3 The CI test job is red

`test.yml:32` runs `bandit -r autoduo/ -ll`; that exits 1 on three medium
findings (B108 ×2 at `cli.py:200/436`, see 2.3/2.2; B103 at `cli.py:320` for
`chmod 0755` on the wrapper, a false positive needing `# nosec B103` with a
reason). So the **whole test job fails today**, not merely the documented
local command.

Locally it is also noisy for a different reason: a stray **13 MB
`autoduo/.venv`** sits inside the package directory, so the scan covers
103,404 lines of pip internals instead of the 1,685 real ones. CI is clean of
it (`.venv/` is gitignored) — local and CI results disagree, the worst
property a verification command can have.

**Fix:** delete the nested venv; add `--exclude .venv`; clear the three
findings.

### 3.4 Error paths sleep twice

`_run_once` sleeps on every error branch (`daemon.py:241, 254, 259, 297`) and
`run()` sleeps again after each cycle (`daemon.py:347-348`) — a failure waits
~2× the poll interval, after the client has *also* backed off internally.

**Fix:** `_run_once` owns the wait on error branches; `run()` owns
steady-state.

### 3.5 State persistence assumes an order it does not enforce

`_save_seen_state` is skipped entirely on passes with no transactions
(`daemon.py:265-267`), so evictions are not persisted on quiet passes.
`_load_seen_state` accepts any dict unbounded (`daemon.py:169`), and
`_evict_expired` (`daemon.py:126-140`) assumes the JSON key order is
oldest-first — a hand-edited or re-serialized file stalls eviction and the
map grows without bound.

**Fix:** sort by timestamp on load (or store `{urgid: ts}` and evict by
value), cap the number of entries, persist on every pass that evicted
anything.

## Tier 4 — slow the next change

### 4.1 Extend the `main()` test that already exists

`tests/test_daemon.py:421 test_main_with_vault` already builds a vault,
patches `DeviceClient`, and calls `daemon.main(argv=[...])`. The gap is not
coverage but assertions: it never passes `--audit-log` and never checks that
each opened stream is actually used — which is precisely why 2.1 shipped.

**Fix:** ~3 lines — pass `--audit-log`, assert the file is non-empty; add the
same for `--state-file`. Cheapest regression net in the repo.

### 4.2 Three installers, one of them broken

`cli.cmd_install` (`cli.py:289-327`), `scripts/install.sh` and the root
`install.sh` install the same thing and have diverged:

- `cli.py:319` copies `REPO_DIR / "autoduo"` — the **package directory** — to
  `/usr/local/bin/autoduo`, raising `IsADirectoryError`. The shell installer
  is correct (`scripts/install.sh:38`).
- README's `sudo ./autoduo setup` names a path that does not exist at the
  repo root; the wrapper is `scripts/autoduo`.
- The root `install.sh` — the file users actually `curl | sudo bash` — is
  absent from both the shellcheck set (`test.yml:42`) and AGENTS.md's parity
  command.

**Fix:** make `cmd_install` exec `scripts/install.sh` and delete the
duplicate; add the root installer to the shellcheck set.

### 4.3 Dead code and dangling contracts

- `redact.redact_transaction()` — tested, never called; SECURITY.md claims
  PII is stripped via it.
- `vault.migrate()` — no caller; `open_vault` hard-fails on a version bump.
- `client.py:165` — unused local `from autoduo import redact`.
- Unused imports: `getpass` (`cli.py:17`, `enroll.py:25`), `os`
  (`enroll.py:28`), `b64` (`enroll.py:64`), `stat` (`vault.py:26`).
- `requirements.txt` lists `pytest`, a dev dependency, in the runtime file.

### 4.4 Dependency drift

`>=` pins only, no lockfile, Dependabot deferred to 0.2. For a daemon whose
whole job is holding a key, an unreviewed transitive bump of `cryptography`
lands on the next install. Pin exactly or add a lockfile; restore
Dependabot.

### 4.5 Add a client-level dry-run — after 1.3

There is no way to validate a change against a live tenant without approving
real pushes. A `--dry-run` is worth building, but it must live in the
**client** (log the prepared request, skip `send`) rather than in the daemon
logging `would_approve {urgid, type}` — a daemon-level dry-run hard-codes the
body and will fossilize the wrong payload. And because it never sees a
response, it cannot substitute for the 1.3 measurement.

### 4.6 Clock skew presents as an auth failure

`x-duo-date` is inside the signed canonical string, so drift produces 4xx,
which the daemon reports as `auth_error` and (after 3.1's fix) eventually a
circuit trip. Nothing checks NTP state or mentions time skew. On a
persistent `AuthError` streak, log local time, time source, and the hint.

## Protocol conformance summary

Diffed against DuoBreak v2.0.0.

| Element | DuoBreak | AutoDuo | Impact |
|---|---|---|---|
| Poll response `stat` | checked | **ignored** (returns `[]`) | 1.1 |
| Approval body | `akey, answer, fips_status, hsm_status, pkpush` | `answer` only | 1.3 |
| `txId` header on POST | set | absent | 1.3 |
| Approval response `stat` | `== "OK"` | **ignored** | 1.2 |
| Step-up / Verified Push | detects `step_up_code_info` | **ignores** | 3.1 |
| `expiration` | skips expired | **ignores** | 2.7 |
| `urgid` validation | regex `[A-Za-z0-9_-]{1,128}` | truthiness + URL-quote | low |
| Canonical string | `urlencode(data)`, insertion order | `urlencode(sorted(...))` | 1.3 coupling |
| Signature | PKCS#1 v1.5 + SHA-512 | same | ✅ |
| Vault durability | fsync file + dir, digest check, flock | tmp+rename, no fsync, no lock | 2.2, 2.5 |
| Retry/backoff | none (next poll) | full policy | AutoDuo better |

## Recommended sequence

**Wave A — make failure visible (prerequisites for everything else).**
1.1 poll validation · 1.2 `stat` check · 3.1 circuit-breaker fix · 2.5 flock ·
2.1 audit stream · 4.1 assertions. All small, independently shippable, and
together they turn the daemon from "silently green" into "reports what
happened".

**Measurement gate.** Run one real approval with 2.5 in place; read
`stat`/`code`. This is a live-tenant step, not a code change.

**Wave B — settle the protocol.** 1.3 payload + `txId` + the
signed-order invariant, **in the same change set as the measurement** — not
deferred, because if the payload is the cause, Waves A–B were built on an
approval that never worked. Then 2.7, 2.2, 2.3, 2.4, 2.6.

**Wave C — make it observable.** 3.2 watchdog (with last-success in the
payload), 3.3, 3.4, 3.5, 4.2, 4.4, 4.5, 4.6.

## Verification recipe

```bash
.venv/bin/python -m pytest tests/ -v                       # 114 today
.venv/bin/bandit -r autoduo/ -ll --exclude autoduo/.venv   # must exit 0
.venv/bin/python -m compileall -q autoduo/
shellcheck scripts/autoduo scripts/install.sh scripts/uninstall.sh install.sh
systemd-analyze verify systemd/autoduo.service
```

Reproduce the two Tier-1 findings before changing anything:

```python
# 1.1 — a failed poll is indistinguishable from an idle one
client.list_transactions()   # -> []  (no exception, no event)

# 1.2 / 3.1 — approval errors are logged as ok and retried forever
# 30 cycles with approve() raising AuthError -> 30 approve_auth_error,
# 0 circuit_opened, circuit_open_until is None
```

## Known issues not covered above

- The unit runs as **root** — no `User=`/`DynamicUser=`.
- `daemon.py:430` opens the live log 0644 under umask, while
  `scripts/logrotate-autoduo` creates 0640 and the docs claim 0640.
- `StartLimitBurst=10` + `Restart=always` drops the unit into `failed` with
  no alarm after a crash loop (interacts with 3.2).
- `SECURITY.md:25` still carries `TODO-add-real-address@domain.example`.
- `SECURITY.md:57` recommends jittering the poll interval. This audit's
  non-goals reject that (honesty over concealment); the contradiction should
  be resolved in one place rather than left to drift.

## Non-goals

- Bypassing Verified Duo Push. It needs a human code; the honest fix is to
  detect and park it (3.1), not to defeat it.
- Evading Duo's automation detection.
- Packaging for PyPI; the source-tree install is deliberate.
