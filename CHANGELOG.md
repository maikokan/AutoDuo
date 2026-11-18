# Changelog

All notable changes to autoduo are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Per-account instances.** Each Duo account is an independent process with
  its own vault, state file, log directory, lock and `autoduo@<name>.service`
  unit, so two accounts cannot affect each other. The instance named
  `default` keeps the original paths, so existing installs need no migration.
- `autoduo instances` lists every configured account with its state.
- `autoduo watch`: a live terminal dashboard — per-account state, approvals in
  the last 24 h, pending pushes, and the age of the last approval. Plain
  single-frame output when piped.
- Per-instance `--instance NAME` on every command, accepted before or after
  the verb.

### Fixed
- A poll answering HTTP 200 with `stat: FAIL` returned an empty list and
  emitted nothing, so a failed poll looked exactly like "no pushes". It now
  raises and is reported.
- An approval Duo rejected was logged as `approve_ok`, written to the audit
  log, and marked seen, so it was never retried. It is now reported as
  rejected, is not audited, and is retried or parked deliberately.
- The audit log was opened and never used, so `audit.log` never existed.
- The vault was world-readable between `rename` and `chmod`, and was not
  fsynced; both fixed, and the parent directory is only tightened when this
  program created it.
- The activation URL was written to a predictable `/tmp` path before being
  chmodded; it now uses `mkstemp` and is removed in a `finally`.
- `autoduo install` copied the package directory to `/usr/local/bin/autoduo`
  and raised `IsADirectoryError`; it now installs the wrapper script.
- Two daemons could run for the same vault and approve each push twice. A
  second copy now exits 5 and systemd does not restart it.
- Signal handlers did file I/O and could re-enter the log stream.
- Expired pushes were approved; they are now skipped.
- The circuit breaker could never fire for approval errors, because a
  successful poll cleared the failure count. Authentication failures and
  rate limiting are now counted separately and correctly.

### Changed
- The terminal output was restyled: no box drawing, one accent, word labels,
  plain-text fallback that stays under 80 columns.

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