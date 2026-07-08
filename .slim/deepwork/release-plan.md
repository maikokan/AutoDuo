# Plan: duo-bot v0.1.0 — Initial Public Release

> Context: working daemon at `/opt/duo-bot`. User has successfully enrolled
> and auto-approved a push. Ready to publish as open-source.

## Goals (from user)

1. **Maximum logging** — every event, every HTTP request/response, every
   signature, every state transition.
2. **GitHub-ready** — publishable to `github.com/USER/duo-bot` for others.
3. **Disclaimer in setup** — explicit warning that this defeats 2FA;
   user must accept before proceeding.
4. **Retry-on-input-error** — bad URL / passphrase / etc. should re-ask,
   not quit.
5. **Push-test gate** — during setup, after enrollment, send a test push
   to confirm end-to-end works before declaring success.
6. **Version tracking** — proper SemVer, git tags, changelog, `--version`.

## Threat model reminder

This program is shipped with explicit acknowledgement that it **defeats
2FA by design**. Disclaimer is not optional. The README and setup
dialog must surface the threat model before any action is taken.

## Phase A — Maximum logging

### A.1 Daemon lifecycle events
Already emitted as JSON. Add:
- `daemon_loaded_vault` with `host`, `akey_len`, `pkey_len` (no full values).
- `poll_started`, `poll_completed` with `count`.
- `circuit_close` (when cooldown expires).
- `vault_reopened` (after SIGHUP).
- `exit_code`, `reason` on shutdown.

### A.2 HTTP traffic log (NEW: traffic.log)
- All requests/responses get logged to a separate `/var/log/duo-bot/traffic.log`.
- Format: one JSON line per request:
  ```
  {"ts":"...","event":"http_request","method":"GET","url":"...","headers":{...redacted...}}
  {"ts":"...","event":"http_response","status":200,"headers":{...redacted...},"body_sha256":"..."}
  ```
- Headers: redact `Authorization` (keep only "Basic " + first 8 chars of
  the base64 token, for debugging without leaking the signature).
- Body: SHA-256 hash + length, NEVER the actual body (would leak
  activations, push contents, etc.).

### A.3 Approval audit log (NEW: approvals.log)
- One line per successful approve:
  ```
  {"ts":"...","event":"approved","urgid":"...","txid":"...","factor":"push","app":"...","ip":"...","location":"...","user":"..."}
  ```
- Includes the rich fields from the transaction (`attributes` in the
  transaction list response).
- This is the "human-readable audit trail" — separate from the JSON
  daemon events so operators can grep for what was approved.

### A.4 Log rotation
- daemon.log: weekly, 4 rotations, compress.
- traffic.log: daily, 7 rotations, compress.
- approvals.log: monthly, 12 rotations, compress.
- logrotate config per file (different cadences).

### A.5 --verbose / -v flag for CLI
- DEBUG-level logging to stderr (in addition to journal).
- Useful for troubleshooting failed enrollments.

## Phase B — GitHub-release scaffolding

### B.1 Repo hygiene
- `.gitignore`: `__pycache__/`, `.venv/`, `*.log`, `data/`, `*.pem`,
  `*.key`, `.env`, `*.egg-info/`, `dist/`, `build/`, `.pytest_cache/`.
- `LICENSE` (MIT) — already present.
- `CONTRIBUTING.md` — how to set up dev env, run tests, file issues.
- `CODE_OF_CONDUCT.md` — Contributor Covenant (standard).
- `SECURITY.md` — how to report vulns; threat-model reminder.

### B.2 CI: GitHub Actions
- `.github/workflows/test.yml`:
  - Matrix: Ubuntu 22.04 + 24.04, Python 3.10/3.11/3.12.
  - Steps: checkout, setup-python, `pip install -r requirements.txt -r requirements-dev.txt`, `pytest`, `bandit -r duo_bot/`, `shellcheck scripts/*.sh duo-bot`.
- `.github/workflows/lint.yml`:
  - ruff (if added later; skip for v0.1 to keep dep list small).
- `.github/dependabot.yml`:
  - Weekly pip + GitHub Actions updates.

### B.3 Release workflow
- `.github/workflows/release.yml`:
  - Triggered on tag push matching `v*`.
  - Builds a release tarball, attaches to GitHub Release.
  - Optional: PyPI publish (defer; users install from git).
- `CHANGELOG.md` — Keep-a-Changelog format, v0.1.0 initial entry.
- `docs/` directory:
  - `docs/threat-model.md` — pulled out of README for visibility.
  - `docs/how-it-works.md` — protocol overview, link to research.

### B.4 README overhaul
- Big "WARNING: This defeats 2FA" banner at top.
- ASCII diagram of the architecture.
- Badges: CI status, license, Python version.
- Quick-start section using `duo-bot setup`.
- Full reference for all commands.
- "How it works" link to docs/.

### B.5 requirements-dev.txt
- `pytest`, `bandit`, `shellcheck` (system).

## Phase C — Interactive setup overhaul

### C.1 Disclaimer acceptance (NEW)
Before ANY action:
```
╔══════════════════════════════════════════════════════════════╗
║                       duo-bot setup                          ║
╠══════════════════════════════════════════════════════════════╣
║  WARNING: This software defeats Duo 2FA.                     ║
║                                                              ║
║  By continuing, you acknowledge that:                        ║
║   • Anyone with access to this server can approve pushes     ║
║   • This defeats the security purpose of 2FA                 ║
║   • You are solely responsible for securing the server       ║
║   • The authors accept no liability                           ║
║                                                              ║
║  Type "I ACCEPT" to continue, or Ctrl+C to abort:            ║
╚══════════════════════════════════════════════════════════════╝
```
- Must type exactly `I ACCEPT` (case-sensitive).
- Anything else re-prompts (with Ctrl+C as the abort path).

### C.2 Retry on input error (NEW)
- Bad activation URL → print error, re-prompt (no quit).
- Bad passphrase file → print error, re-prompt for path.
- Empty input → re-prompt.
- `--no-passphrase` selected → skip passphrase prompt.
- Max 3 retries on same field; after that, print "too many invalid
  attempts, aborting" and exit.
- Each prompt has a timeout? No — keep it interactive.

### C.3 Push-test gate (NEW)
After enrollment + "approve on your phone":
1. Start the daemon (already does this).
2. Wait 5 seconds.
3. Call `list_transactions()` once and log the result.
4. The user must trigger a login on whatever service they want to
   protect.
5. After the user confirms they triggered a push:
   ```
   ✓ Did you receive a push notification on your phone? [y/N]
   ```
6. If yes → "Setup verified." Exit.
7. If no → "Check that you approved the device on your real Duo
   Mobile app." Wait, retry the question (max 3 times).
8. The daemon handles the actual approve; this gate just confirms
   that the user can trigger a push.

**Subtlety:** we can't directly verify our daemon approved a specific
push — we'd need to know about a test login in advance. Best we can
do is: confirm the daemon is active, confirm `list_transactions()`
returned 200, and confirm the user saw a push.

### C.4 Version tracking
- `duo_bot/__init__.py` has `__version__ = "0.1.0"`.
- `duo_bot/__init__.py` gains `__version_info__ = (0, 1, 0)`.
- Both CLIs already have `--version`.
- Add `duo-bot version` CLI command that prints version, host
  (without secrets), and uptime if running.
- `git describe --tags --dirty` integration: if running from a git
  checkout, append `-<commits>-g<sha>` (PEP 440 compliant: `0.1.0+abc1234`).
- Update `CHANGELOG.md` with v0.1.0 entry.

### C.5 Improved setup flow
```
[disclaimer]
  → must type "I ACCEPT"
  ↓
[install if missing]
  ↓
[ask passphrase mode]
  → (a) no passphrase (file-permission protection only) — RECOMMENDED
  → (b) passphrase file (extra security, requires more setup)
  ↓
[ask activation URL]
  → validate URL format; on bad input, re-ask
  → confirm: "Is this a Duo activation URL you just got from Duo Admin?"
  ↓
[enroll]
  → show progress
  → on failure, offer: (r)etry with new URL, (a)bort, (v)iew full error
  ↓
[ask: "Open Duo Mobile and approve the new-device prompt."]
  → confirm: "Have you approved? [y/N]"
  ↓
[start daemon]
  → verify it started
  ↓
[push-test gate]
  → "Trigger a login on any service protected by your Duo account."
  → wait for user confirmation
  → verify daemon saw it
  ↓
[success screen]
  → "Setup complete!"
  → "The daemon is now auto-accepting pushes."
  → "Useful commands:"
  → "  duo-bot status    # check daemon"
  → "  duo-bot logs      # live log tail"
  → "  duo-bot uninstall # remove everything"
```

## Phase D — Tag and release

### D.1 Version bump
- `__version__ = "0.1.0"`.
- CHANGELOG entry: "Initial release. Working headless Duo auto-accept daemon."

### D.2 Pre-release validation
- pytest: must pass on Python 3.10, 3.11, 3.12.
- bandit: no high/medium.
- shellcheck: clean.
- systemd-analyze verify: clean.
- systemd-analyze security: 3.9 OK or better.
- MDXE under systemd: all tests pass.
- Manual test: full setup → enroll → approve → daemon runs.

### D.3 Tag and push
- `git tag -a v0.1.0 -m "Initial release"`
- `git push origin v0.1.0`
- GitHub Actions builds the release tarball.

## Files to add/change

```
NEW:
  .gitignore
  .github/workflows/test.yml
  .github/workflows/release.yml
  .github/dependabot.yml
  CHANGELOG.md
  CONTRIBUTING.md
  CODE_OF_CONDUCT.md
  SECURITY.md
  docs/threat-model.md
  docs/how-it-works.md
  requirements-dev.txt

CHANGE:
  duo_bot/__init__.py       — add __version_info__
  duo_bot/cli.py            — disclaimer, retry, push-test, version cmd
  duo_bot/daemon.py         — additional lifecycle events
  duo_bot/client.py         — HTTP request/response event hooks
  scripts/install.sh        — install requirements-dev.txt? no, keep prod-only
  systemd/duo-bot.service   — add traffic.log path? no, log via daemon
  README.md                 — overhaul with badges, ASCII diagram, etc.
  plan.md                   — this becomes historical
```

## What we are NOT doing in v0.1.0

- Telegram notifications (out of scope).
- Web dashboard (out of scope).
- Multi-user / multi-device (single vault per install).
- PyPI publishing (deferred; install from git).
- TLS cert pinning (Duo rotates certs).
- Cross-platform support (Linux-only, systemd-only).
- Windows / macOS daemon.

## Risks

- Setup is more complex now (disclaimer + retry + push-test). Could
  annoy power users. Mitigation: `duo-bot install` (no setup) remains
  for scripted installs; disclaimer only fires in `setup`.
- HTTP traffic log could leak secrets if Authorization redaction
  breaks. Mitigation: redact in `_signed_request` itself, not in
  log emitter.
- Push-test gate is heuristic — we can't verify our daemon
  approved the user's specific test push without coordination.
  Document this limitation.

## Decisions locked

- v0.1.0 (not 0.0.x) — it works end-to-end.
- MIT license (already).
- Single vault per install.
- `duo-bot setup` is the primary user-facing flow.
- `duo-bot install` for headless/scripted installs.
- Disclaimer is shown only in `setup`, not in `install`.