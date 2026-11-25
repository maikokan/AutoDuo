# Changelog

All notable changes to autoduo are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-11-25

If you ran 0.1.0, the short version: it could tell you it was working when it
wasn't, and it could report an approval that never happened. Both are fixed,
and a second account no longer needs a second machine.

### Added

- **Multiple accounts.** Each Duo account is its own process with its own
  vault, state file, logs, lock and systemd unit, so two accounts can't read
  or disturb each other. The account called `default` keeps the old paths, so
  an existing install needs no migration. `sudo autoduo setup --instance alice`
  enrols, enables and starts one.
- **`autoduo instances`**, which lists every account and how it's doing.
- **`autoduo watch`**, a live dashboard of all of them: state, approvals in
  the last 24 hours, pending pushes, and how long ago the last one happened.
  Piped, it prints a single plain frame with no escape codes. An account that
  has been quiet all day reads as *quiet*, not broken. A running account that
  hasn't written anything for twenty minutes says so, because systemd will
  happily call a hung loop `active` until the sun burns out.
- **`--instance NAME`** on every command, accepted before or after the verb.

### Fixed

- An HTTP 200 with a non-JSON body counted as a successful approval, so a
  captive portal, a proxy error page or a Duo maintenance page was logged as
  `approve_ok` and written to the audit log as a real approval. It is now an
  error, retried a few times and then parked.
- A poll answering 200 with `stat: FAIL` returned an empty list and logged
  nothing, which looked exactly like "no pushes waiting". It now raises, and
  you see it.
- An approval Duo rejected was logged as `approve_ok`, written to the audit
  log and marked seen, so it was never retried. Rejections are now classified
  (terminal, auth problem, transient) and only the last kind is retried.
- A `seen.json` entry without a UTC offset raised `TypeError` on every start,
  outside the poll's error handling. With `Restart=always` that is a restart
  loop while the unit still reports `active`. Eviction now reads such a
  timestamp as UTC, housekeeping faults cost one log line instead of the
  loop, and a crash that escapes anyway writes `fatal_error` and exits 6.
- An error cycle waited its backoff *and* the poll interval, so the daemon
  slept longer than the `backoff_seconds` it printed.
- The audit log was opened and never used, so `audit.log` never existed.
- The vault was readable by everyone between `rename` and `chmod`, and wasn't
  fsynced. Both fixed; the parent directory is only tightened when this
  program created it.
- A `seen.json` written by an older build kept its group- or world-readable
  mode until the next approval, which on a quiet account can be days. It is
  tightened to 0600 when loaded.
- The activation URL went to a predictable `/tmp` path before being chmodded.
  It now uses `mkstemp` and is deleted in a `finally`.
- The one-liner installer never installed the per-instance unit or log
  rotation, and its next steps told you to put the activation URL in
  `/tmp/act.txt` and run the module without its virtualenv. It now installs
  both and points you at `autoduo setup`.
- `autoduo install` copied the package directory to `/usr/local/bin/autoduo`
  and raised `IsADirectoryError`. It copies the wrapper script.
- Two daemons could run against the same vault and approve each push twice.
  A second copy now exits 5, and systemd doesn't restart it.
- Signal handlers did file I/O and could re-enter the log stream.
- Expired pushes were approved. They're skipped.
- The circuit breaker could never fire for approval errors, because a
  successful poll cleared the failure count. Auth failures and rate limiting
  are now counted separately, and 429s don't count toward the circuit.
- `pytest` was a runtime dependency. It's development-only now.

### Changed

- The terminal output was restyled: no box drawing, one accent colour, word
  labels, and a plain fallback that stays under 80 columns.
- `autoduo status` on an account that was never enrolled exits 1 and says so,
  instead of inheriting `systemctl`'s silence.
- `autoduo watch`, piped, exits 1 when it can't read instance status, so a
  script can tell a broken monitor from a quiet one.
- `autoduo uninstall` removes one account. `--all` removes everything,
  including the global files.
- Logs are 0640 in 0750 directories, matching what the units ask for, and
  rotation covers the per-instance layout.

### Security

- **Redirects are no longer followed.** `requests` follows them by default,
  so a 3xx from anything answering on a Duo subdomain would have carried a
  signed request to a host the allowlist never checked.
- **`Retry-After` is bounded** to 0–300 seconds. A negative or NaN value made
  `time.sleep` raise; a huge one stopped the account approving for days.
- The audit record is built through `redact.redact_transaction()`, so the
  boundary SECURITY.md describes is the one the code actually applies.
- A world-readable vault is reported as `vault_mode_insecure` at startup
  instead of being opened in silence.

### Notes

- `vault.migrate()` is gone. It had no caller and could not migrate anything.
- Measured on a live tenant: seven days, 14 approvals, 14 matching audit
  records, each push seen exactly twice, no auth errors, no restarts.

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
