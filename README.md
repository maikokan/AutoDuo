# 🚨 autoduo

**⚠️ WARNING: This software defeats Duo 2FA by design.**

`autoduo` is a headless Linux daemon that runs 24/7 and automatically
approves Duo push notifications on your behalf. After a one-time
enrollment (scan QR / approve on phone once), it handles all future
push approvals unattended.

[![tests](https://github.com/maikokan/AutoDuo/actions/workflows/test.yml/badge.svg)](https://github.com/maikokan/AutoDuo/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

---

## ⚠️ Threat Model — Read First

`autoduo` **defeats the entire purpose of Duo 2FA**. Anyone with shell
on this server can approve pushes for the configured account. Use **only**
on a dedicated VPS that you fully control. You must:

- Run SSH-key-only auth (`PermitRootLogin no`)
- Keep the host patched
- Disable Verified Duo Push for the account (if enforced, the login will
  fail even after approval)
- Accept that Duo may detect and disable the account (anomaly detection)

By using this software, you accept all responsibility for the security
consequences. The authors accept no liability.

---

## Quick Start

```bash
git clone https://github.com/maikokan/AutoDuo.git /opt/autoduo && sudo ./autoduo setup
```

It will install dependencies, show a disclaimer, ask for your Duo
activation URL, enroll a virtual device, start the daemon, and run a
push-test to verify everything works.

---

## Commands

```
autoduo setup       Interactive setup with disclaimer (first run)
autoduo install     Install systemd service (no disclaimer, for scripts)
autoduo uninstall   Remove systemd service and data
autoduo enroll      One-shot enroll with a Duo activation URL
autoduo start       Start the daemon
autoduo stop        Stop the daemon
autoduo restart     Restart the daemon
autoduo status      Show daemon status
autoduo logs        Tail live logs (Ctrl+C to stop)
autoduo version     Print version
```

All commands require root (`sudo`).

---

## How It Works

```
┌─────────────────────────────────────────────────────────────────────┐
│                        autoduo architecture                          │
│                                                                      │
│  ┌─────────────────┐    ┌─────────────────────────────────────────┐  │
│  │  Duo Admin Panel │───▶│  Enroll (RSA-2048 keypair generated    │  │
│  │  (Add Device)   │    │  locally, registered via activation API)│  │
│  └─────────────────┘    └─────────────────────────────────────────┘  │
│                                     │                                 │
│                                     ▼                                 │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  Vault: /var/lib/autoduo/vault.enc                             │  │
│  │  ── RSA private key    (AES-GCM, PBKDF2 600k rounds)          │  │
│  │  ── akey, pkey         (Duo's per-device identifiers)          │  │
│  │  ── app_install_id     (UUID fingerprint)                      │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                     │                                 │
│                                     ▼                                 │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │  daemon.py  (systemd, Restart=always)                           │  │
│  │  ── Polls  GET /push/v2/device/transactions  (every 5s)        │  │
│  │  ── Approve POST .../transactions/<urgid>  (answer=approve)    │  │
│  │  ── In-process urgid dedup (no double-approve)                │  │
│  │  ── Circuit breaker on repeated auth failures                  │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  Logs:                                                               │
│  ── /var/log/autoduo/daemon.log     (events, lifecycle)              │
│  ── /var/log/autoduo/audit.log      (approved transactions)         │
│  ── /var/log/autoduo/traffic.log    (opt-in HTTP traffic)           │
└─────────────────────────────────────────────────────────────────────┘
```

### The protocol

Duo has three HTTP surfaces:

| Surface | Purpose | Can approve push? |
|---|---|---|
| `/auth/v2/` | End-user REST API | **No** (only sends + polls) |
| `/admin/v1/` | Admin API | **No** (user management only) |
| `/push/v2/...` | Mobile device protocol | **Yes** |

`autoduo` speaks the third surface — the same protocol as the real
Duo Mobile Android app. All requests are signed with RSA-SHA512 over
the device's private key. The key is stored in an AES-GCM encrypted
vault (PBKDF2-HMAC-SHA256, 600k iterations for offline brute-force
resistance).

---

## Logging

| Log file | Contents | Sensitivity |
|---|---|---|
| `daemon.log` | Lifecycle events, approval successes, errors | Low |
| `audit.log` | Approved transactions (urgid, app, factor) | Medium (PII redacted) |
| `traffic.log` | HTTP requests/responses (headers redacted, body SHA-256 only) | High — **opt-in only** |

Enable the traffic log via the daemon's `--traffic-log` flag.

`akey` and `pkey` are never logged in full; any log line that might
contain them shows only `<N chars:hidden>`.

---

## Requirements

- **OS:** Ubuntu 22.04+ / Debian 12+ (systemd required)
- **Python:** 3.10, 3.11, or 3.12
- **RAM:** ~30 MB resident
- **Disk:** ~50 MB (venv + vault + logs)

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

See [SECURITY.md](SECURITY.md).

## License

MIT — see [LICENSE](LICENSE).

---

## Disclaimer

This software is provided "AS IS", without warranty of any kind, express
or implied. **The authors accept no liability** for any damage, account
loss, security breach, or other consequence of using this software.
You are solely responsible for the security of your systems. Use at your
own risk. There is no warranty of fitness for any particular purpose.