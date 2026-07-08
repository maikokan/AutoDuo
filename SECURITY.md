# Security

## Threat model (read first)

autoduo **defeats the security purpose of 2FA for the configured
account**. Anyone with shell on the host (or who can read
`/var/lib/autoduo/vault.enc` and the passphrase file) can approve any
Duo push request for that account.

If your Duo tenant enforces **Verified Duo Push** (where the user
must type a code from the browser into the phone), the daemon will
still approve the push, but the underlying login will fail because
Duo requires the human-typed code.

autoduo assumes:
- The host is dedicated (no other user-facing services).
- SSH-key-only auth (`PermitRootLogin no`), fail2ban, no shared users.
- The host's disk is encrypted at rest (so a stolen disk doesn't
  leak the passphrase file or vault).
- The host is patched regularly.

## Reporting a vulnerability

Email: TODO-add-real-address@domain.example (replace with maintainer's
contact before publishing).

We do NOT have a public bug bounty. We aim to acknowledge reports
within 7 days and ship fixes within 30 days for critical issues.

## PII handling

The approval audit log at `/var/log/autoduo/audit.log` contains:
- `urgid`, `txid` — Duo transaction identifiers
- `factor`, `app`, `tx_type` — what was approved

It does NOT contain usernames, raw IPs, or locations — those are
stripped via `autoduo.redact.redact_transaction()` before logging.
Permissions are 0640.

If you enable `--traffic-log`, the HTTP traffic log contains:
- Request URLs with `akey` and `pkey` redacted to length-only
- Request/response headers with `Authorization` and `x-duo-date` fully
  redacted
- Body SHA-256 + length, NEVER the body itself

## Hardening checklist for production

- [ ] Run `sudo autoduo install` (no passphrase) — vault is
      protected by file permissions alone. **OR** generate a passphrase
      and store it on a separate encrypted volume.
- [ ] `chmod 0600 /var/lib/autoduo/vault.enc` (already enforced).
- [ ] `chmod 0640 /var/log/autoduo/*.log` (audit/traffic enforced).
- [ ] Enable `systemd-timesyncd` (the install script does this).
- [ ] Run Duo Admin "anomaly alerts" → email.
- [ ] Disable Verified Duo Push for the affected user, OR accept that
      this daemon cannot bypass it.
- [ ] Consider jittering the poll interval if Duo detects your cadence.

## Reporting checklist

When reporting an issue, do NOT include:
- Activation URLs
- Authorization headers (even partial)
- Vault contents
- Audit log entries with real transaction IDs

The maintainers do not need any of the above to reproduce a bug.