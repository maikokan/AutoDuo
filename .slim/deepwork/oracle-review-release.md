# Oracle Review — duo-bot v0.1.0 Release Plan

**File reviewed:** `/opt/duo-bot/.slim/deepwork/release-plan.md` (284 lines)
**Reviewer:** Oracle (advisory, read-only)
**Date:** 2026-07-08

---

## Lens 1 — Correctness / Completeness

**Verdict: Approve with 7 changes.**

The plan is structurally sound and the four goals map cleanly to phases. However, several items are vague, internally inconsistent, or have implementation gaps that will surface late.

### Issues

1. **`I ACCEPT` is bypassable by `--yes` convention.** C.1 mandates exact `I ACCEPT` re-prompt, but the plan never says `--yes`/`DUO_BOT_ACCEPT=1` are forbidden. Add: "The disclaimer acceptance is non-skippable; no `--yes` flag will override it. Document this in CLI help."

2. **C.3 push-test gate has no failure path on `list_transactions() != 200`.** Subtlety paragraph acknowledges it can't verify a specific push, but step 3 says "log the result" with no branch. Add: "If `list_transactions()` returns non-200 or raises, the gate fails immediately with a diagnostic hint; do not ask the y/N question."

3. **C.2 retry counter resets per-field or globally is unspecified.** "Max 3 retries on same field" reads as per-field, but the push-test gate has a separate "max 3 times" counter. State explicitly: per-field retry, not cumulative across the whole flow.

4. **C.4 `__version_info__` PEP-440 vs. git-describe form are mixed.** The plan introduces both `__version__ = "0.1.0"` *and* `__version_info__` *and* `git describe --tags --dirty` output as `0.1.0+abc1234`. PEP 440 local version separator is `+`, but `git describe --dirty` produces `v0.1.0-3-gabc1234-dirty` style. Decide: drop the dynamic git-describe path for v0.1.0 (keep `__version__` static), or define an explicit builder. Mixing the two will produce conflicting version strings.

5. **A.2 redaction policy is incomplete.** "Keep only 'Basic ' + first 8 chars of the base64 token" — but Duo's signed requests use HMAC-SHA1 of (date|method|host|path|salted body), and the `Authorization` header on responses is not Basic auth. Confirm which header carries the signature (it's a custom `Authorization: Basic <b64(ikey:skey)>` for the *signed* endpoint, but plain HTTPS for `/auth/v2/ping`). Specify per-endpoint redaction; don't apply one rule blindly.

6. **A.4 logrotate assumes `/var/log/duo-bot/` is writable by the daemon user.** Default systemd `DynamicUser=yes` means `/var/log/duo-bot/` needs `StateDirectory=log/duo-bot` or `LogsDirectory=duo-bot`. Add to D.3: ship a `tmpfiles.d/duo-bot.conf` and a logrotate drop-in that references the actually-used paths (RuntimeDirectory vs LogsDirectory).

7. **Phase D validation gates `systemd-analyze security 3.9 OK`** but `--user` services and `DynamicUser=yes` change the score significantly. Without knowing the final unit file, this is unverifiable. Either: (a) attach the proposed unit to the plan and compute the score first, or (b) drop the specific 3.9 number and say "passes security audit with no high-severity findings."

### What's already correct
- Disclaimer-before-action ordering (C.1 before C.5 flow) — correct.
- `duc_bot.duo_bot.install` vs `setup` split — sensible.
- "What we are NOT doing in v0.1.0" — good explicit non-goals; prevents scope creep.
- `--version` already wired in both CLIs.

---

## Lens 2 — Risk

**Verdict: Approve with 5 changes.**

The threat model is stated, the disclaimer is locked, and Secrets-in-traffic-log is the obvious one the plan acknowledges. But there are second-order risks the plan under-weights.

### Issues

8. **`traffic.log` is a credential-adjacent side channel.** Even with redaction, response body SHA-256 + URL + method + timestamp can be replayed against Duo's rate limiter, or correlated with the user's login patterns. Recommend: (a) default `traffic.log` off; (b) opt-in via `--trace-http` flag or `[debug] http_trace = true` in config; (c) document that enabling it is a privacy decision. The plan currently implies it ships on by default — that's a foot-gun for a public release.

9. **`akey_len`/`pkey_len` event payloads are fine, but `daemon_loaded_vault` fires before a successful `pong()`.** A user with a corrupted vault can observe the event but the daemon can't yet prove the keys work. Add: load + verify-credentials as separate events, so operators can distinguish "loaded" from "working."

10. **A.3 approvals.log contains plaintext user + IP + location.** On a public-facing server this is PII that must not be committed, and `--version` style "host (without secrets)" doesn't apply — `host` here is `api-XXXX.duosecurity.com`, not sensitive. But `user`/`ip`/`location` are. Recommend: file mode `0640` owned by a dedicated `duo-bot` group, and a `Warnings` block in README: "approvals.log contains PII; treat as sensitive."

11. **`duo-bot install` is described as "for scripted installs" with the disclaimer only in `setup`.** Anyone point-and-clicks their way through `install --help` and bypasses the threat-model acknowledgement entirely. Move the disclaimer into `install` too, with a `DUO_BOT_SKIP_DISCLAIMER=1` env var for the truly scripted path. Document both paths in SECURITY.md.

12. **Mitigation for "Authorization redaction breaks" is "redact in `_signed_request` itself."** This is correct but only half the picture: the *response* is what's logged with redacted headers, and the response header set is different from the request. Spell out: redaction must live in `client.py` at the boundary of every `(method, url, headers, body) → log` call, not inside one helper. Add a unit test that fails if a new code path emits a log without going through the redaction wrapper.

### Risks the plan handles well
- Logrotate cadence per-file (A.4) prevents the trivial disk-fill from verbose logs.
- "We can't verify our specific push" honestly flagged in C.3 — better to admit than fake it.
- `--no-passphrase` escape hatch avoids user lockout (C.2).

---

## Lens 3 — Simplification

**Verdict: Approve with 6 changes.**

For a v0.1, this plan has accreted several features that are nice-to-have, not release-blocking. Trim aggressively.

### Issues

13. **`docs/how-it-works.md` is not needed for v0.1.0.** A v0.1 release earns its "works" claim, not its "explained" claim. The same content fits in README under "How it works." Defer docs/ until there's a second document to justify the directory.

14. **A.5 `--verbose / -v` is two characters of value for marginal gain.** DEBUG logging for troubleshooting is already implicit in journald (`SYSTEMD_LOG_LEVEL=debug`). Adding a flag means adding argparse + plumbing + docs. Keep if `--version`-style discoverability matters; otherwise delete.

15. **Dependabot weekly updates in v0.1.0 is premature.** The repo has no transitive security surface yet to speak of (`requirements-dev.txt` is `pytest`, `bandit`, `shellcheck`). Add dependabot in 0.2 once the dep tree stabilizes.

16. **C.5 flow diagram is fine, but "passphrase mode (a/b) choice" can be deferred.** For v0.1 the simpler path is: ask for a passphrase file path; if the user wants no passphrase, document `--no-passphrase` and only prompt when it's absent. Asking interactively at first run splits the UX without saving anyone any work. C.2 already implies `--no-passphrase`. Remove the menu in C.5.

17. **`duo-bot version` command that "prints host without secrets and uptime if running" is scope creep.** `--version` already prints version. A `status` command that *might* exist already covers uptime. Verify whether `duo-bot status` exists; if so, version goes in that, not in a new command. Otherwise, the version printout in C.4 is fine — just stop adding fields to it.

18. **B.3 release workflow's "optional PyPI publish (defer; users install from git)"** should be **deleted**, not deferred. Defer-as-comment is dead-code in a plan. Either remove or replace with a single sentence: "PyPI: out of scope for 0.1."

### Things to *keep*, despite pressure to remove
- `SECURITY.md` + threat-model doc separation. Worth its keep.
- `CODE_OF_CONDUCT.md`. Standard, copy-paste, free.
- Three-badge README banner. Cheap, high signal.

---

## Summary

| Lens | Verdict | Required changes |
|---|---|---|
| Correctness / Completeness | Approve w/ changes | 7 items |
| Risk | Approve w/ changes | 5 items |
| Simplification | Approve w/ changes | 6 items |
| **Total actionable items** | | **18** |

**Overall recommendation:** The plan is *directionally right* and appropriately scoped for "initial public release of a working daemon that defeats 2FA by design." But it over-trusts its own framing in three places: (1) the redaction invariant (#12), (2) the disclaimer boundary (#11), and (3) the `traffic.log` default-on state (#8). Fix those three and the v0.1 cuts cleanly.

The simplifications (#13–18) are independent of correctness/risk; you can ship without them. Cut what doesn't earn its keep at v0.1, not v0.3.

**No build-blocking gaps found.** The actual coding work, post-changes, is mechanical.
