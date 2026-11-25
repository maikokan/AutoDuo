# Changelog

All notable changes to autoduo are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-11-25

### Added
- **Per-account instances.** Each Duo account is an independent process with
  its own vault, state file, log directory, lock and `autoduo@<name>.service`
  unit, so two accounts cannot affect each other. The instance named
  `default` keeps the original paths, so existing installs need no migration.
- `autoduo instances` lists every configured account with its state.
- `autoduo watch`: a live terminal dashboard — per-account state, approvals in
  the last 24 h, pending pushes, and the age of the last approval. Plain
  single-frame output when piped. A running account that has written nothing
  for 20 minutes says so instead of looking healthy.
- Per-instance `--instance NAME` on every command, accepted before or after
  the verb.

### Fixed
- An HTTP 200 whose body was not JSON was treated as a successful approval, so
  a captive portal, a proxy error page or a Duo maintenance page was recorded
  as `approve_ok` and written to the audit log. It is now a protocol error,
  retried under a bounded budget and then parked.
- A poll answering HTTP 200 with `stat: FAIL` returned an empty list and
  emitted nothing, so a failed poll looked exactly like "no pushes". It now
  raises and is reported.
- An approval Duo rejected was logged as `approve_ok`, written to the audit
  log, and marked seen, so it was never retried. It is now reported as
  rejected, is not audited, and is retried or parked deliberately.
- A `seen.json` entry without a UTC offset crashed the daemon with an uncaught
  `TypeError` on every start, which `Restart=always` turned into a restart
  loop while the unit still reported `active`. Eviction now assumes UTC and
  bookkeeping faults cost one log line, not the loop.
- A crash in the loop left only a journal traceback; it now writes
  `fatal_error` to the event log and exits 6 so systemd retries.
- An error cycle waited its backoff *and* the poll interval, so the daemon
  slept longer than the `backoff_seconds` it printed.
- The audit log was opened and never used, so `audit.log` never existed.
- The vault was world-readable between `rename` and `chmod`, and was not
  fsynced; both fixed, and the parent directory is only tightened when this
  program created it.
- A `seen.json` written by an older build kept its group- or world-readable
  mode until the next approval, which on a quiet account can be days. It is
  tightened to 0600 when it is loaded.
- The activation URL was written to a predictable `/tmp` path before being
  chmodded; it now uses `mkstemp` and is removed in a `finally`.
- The one-liner installer never installed the per-instance unit or log
  rotation, and its next steps told you to put the activation URL in
  predictable `/tmp/act.txt` and run the module without its virtualenv.
- `autoduo install` copied the package directory to `/usr/local/bin/autoduo`
  and raised `IsADirectoryError`; it now installs the wrapper script.
- Two daemons could run for the same vault and approve each push twice. A
  second copy now exits 5 and systemd does not restart it.
- Signal handlers did file I/O and could re-enter the log stream.
- Expired pushes were approved; they are now skipped.
- The circuit breaker could never fire for approval errors, because a
  successful poll cleared the failure count. Authentication failures and
  rate limiting are now counted separately and correctly.
- `pytest` was a runtime dependency; it is development-only now.

### Changed
- The terminal output was restyled: no box drawing, one accent, word labels,
  plain-text fallback that stays under 80 columns.
- `autoduo status` on an account that was never enrolled exits 1 and says so
  rather than inheriting `systemctl`'s silence.
- `autoduo watch` exits 1 when it cannot read instance status, so a script
  can tell a broken monitor from a quiet one.
- `autoduo uninstall` is scoped to one account; `--all` removes everything.
- Logs are `daemon.log` 0640 and directories 0750, matching what the units
  ask for; log rotation covers the per-instance layout.

### Security
- **Redirects are no longer followed.** `requests` follows them by default,
  so a 3xx from anything answering on a Duo subdomain carried a signed
  request to a host the allowlist never vetted.
- **`Retry-After` is bounded** to 0–300 s. A negative or NaN value made
  `time.sleep` raise; a huge one stopped the account approving for days.
- The approval audit record is built through `redact.redact_transaction()`,
  so the boundary SECURITY.md describes is the one the code applies.
- A world-readable vault is reported as `vault_mode_insecure` at startup.

### Notes
- `vault.migrate()` was removed: it had no caller and could not perform a
  migration, so it promised nothing.
- Verified against a live tenant: 14 approvals over seven days, each urgid
  seen exactly twice, 14 matching audit records, zero auth errors.

## [0.1.0] - 2026-07-08

### Added
- Initial release.
- Headless Duo auto-accept daemon for Linux + systemd.
- Emulates Android Duo Mobile for the `/push/v2/activation/` enrollment endpoint.
- Polls `/push/v2/device/transactions` and POSTs `answer=approve`.
- AES-GCM encrypted vault for the device RSA keypair (PBKDF2-HMAC-SHA256, 600k iterations).
- Interactive `autoduo setup` flow with disclaimer acceptance.
- Commands: `setup`, `install`, `uninstall`, `enroll`, `start`, `stop`, `restart`, `status`, `logs`.
- JSON event log, audit log (PII redacted), and opt-in HTTP traffic log.
- Host allowlist (`.duosecurity.com` only).
- Connection retry with 429/5xx/4xx branching.
- In-process urgid dedup with persisted `seen.json`.
- Circuit breaker on repeated auth failures.
- systemd hardening: `ProtectSystem=strict`, `MemoryDenyWriteExecute=true`, `RestrictAddressFamilies`, etc.

### Security
- 101 unit tests, no network access required.
- `MemoryDenyWriteExecute=true` verified under systemd.
- Re-redacts `akey`, `pkey`, `app_install_id` in any log emission.

### Known Limitations
- Does not bypass Verified Duo Push (human must still type code into phone).
- Fixed-cadence polling is fingerprintable as automation; Duo may detect and disable.
- Defeats the security purpose of 2FA by design.