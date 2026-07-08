# 🚨 AutoDuo

**⚠️ WARNING: This software defeats Duo 2FA by design.**

AutoDuo is a headless Linux daemon that runs 24/7 and automatically
approves Duo push notifications on your behalf. After a one-time
enrollment (scan QR / approve on phone once), it handles all future
push approvals unattended.

[![tests](https://github.com/maikokan/AutoDuo/actions/workflows/test.yml/badge.svg)](https://github.com/maikokan/AutoDuo/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

---

## ⚠️ Threat Model — Read First

AutoDuo **defeats the entire purpose of Duo 2FA**. Anyone with shell
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

**One-liner install** (copy and paste):

```bash
curl -fsSL https://raw.githubusercontent.com/maikokan/AutoDuo/main/install.sh | sudo bash
```

**Or install manually:**

```bash
git clone https://github.com/maikokan/AutoDuo.git /opt/autoduo
cd /opt/autoduo
sudo ./autoduo setup
```

AutoDuo installs to `/opt/autoduo/` with vault at `/var/lib/autoduo/`
and logs at `/var/log/autoduo/`.

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

Duo has three HTTP APIs:

| API | What it does | Can approve push? |
|---|---|---|
| `/auth/v2/` | User auth (send + poll) | **No** |
| `/admin/v1/` | User management | **No** |
| `/push/v2/...` | Mobile device protocol | **Yes** |

AutoDuo uses the third API — the same protocol as the real Duo Mobile
Android app. On enrollment it generates an RSA-2048 keypair, registers
it with Duo, and stores everything in an encrypted vault
(AES-GCM, PBKDF2 600k rounds). The daemon then polls for pending
pushes every 5 seconds and approves them automatically.

No browser automation. No screen scraping. Just the same API calls
the real app makes.

---

## Logging

| Log | What it contains |
|---|---|
| `/var/log/autoduo/daemon.log` | Events, lifecycle, approval success/failure |
| `/var/log/autoduo/audit.log` | Redacted approval records (urgid, app, factor) |
| `/var/log/autoduo/traffic.log` | HTTP traffic (opt-in, headers redacted, body SHA-256 only) |

Sensitive fields (`akey`, `pkey`, authorization headers) are never
logged in full. The audit log is mode 0640.

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