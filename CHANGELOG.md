# Changelog

All notable changes to autoduo are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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