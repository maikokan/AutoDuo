# AutoDuo

**AutoDuo defeats Duo 2FA, on purpose.** If that sentence makes you
uncomfortable, this is the wrong tool and no amount of configuration will
fix it.

It is a small daemon for Linux. You enrol it once, the same way you would
enrol a phone, and from then on it answers your Duo push prompts for you.
It runs under systemd, looks for new pushes every five seconds, and approves
them. After setup you don't touch your phone again.

[![tests](https://github.com/maikokan/AutoDuo/actions/workflows/test.yml/badge.svg)](https://github.com/maikokan/AutoDuo/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

---

## Before you install

Approving a push is the one thing Duo asks a human to do. AutoDuo hands that
job to a process on a server, so from then on your second factor is only as
strong as your access to that server. Anyone who can log in as root can read
the vault and approve a login in your name. That is the trade. There is no
version of this that avoids it.

So put it on a machine you own, that nobody else uses:

- SSH keys only, and no root password login.
- Keep the box patched. It is now part of your authentication path.
- If your tenant enforces Verified Duo Push, the approval will go through and
  the login will still fail — Duo wants a human to type a code from the
  browser into the phone, and a daemon can't do that.
- Duo may notice the pattern and disable the device. That is their call, and
  they are not wrong to make it.

If any of that is not acceptable, don't install it.

---

## Install

One command, on a fresh Debian or Ubuntu box:

```bash
curl -fsSL https://raw.githubusercontent.com/maikokan/AutoDuo/main/install.sh | sudo bash
```

Or do it yourself:

```bash
git clone https://github.com/maikokan/AutoDuo.git /opt/autoduo
cd /opt/autoduo
sudo scripts/autoduo setup
```

The installer puts the code in `/opt/autoduo`, the vault in
`/var/lib/autoduo` and the logs in `/var/log/autoduo`. `setup` is the part
that matters: it shows the warning, takes your activation URL, enrols the
device, asks you to approve the new device on your phone once, then enables
and starts the service. Enabling matters — an unenabled unit disappears at
the first reboot and takes your approvals with it.

The current release is 0.2.0. [CHANGELOG.md](CHANGELOG.md) says what changed,
and [ROBUSTNESS.md](ROBUSTNESS.md) has the audit behind the fixes if you want
to see the reasoning rather than the result.

---

## More than one account

Duo enrols one device per user, so a second account means a second full
install. AutoDuo calls each one an instance: its own vault, its own state
file, its own logs, its own systemd unit. Two accounts are two processes, and
neither can read or disturb the other's data.

```bash
sudo autoduo setup --instance alice     # enrol it, enable it, start it
sudo autoduo instances                  # what is set up, and how it's doing
sudo autoduo watch                      # live view of every instance
```

Each named instance gets its own directories:

| | |
|---|---|
| vault | `/var/lib/autoduo/instances/<name>/vault.enc` |
| state | `/var/lib/autoduo/instances/<name>/seen.json` |
| logs | `/var/log/autoduo/<name>/{daemon,audit}.log` |
| unit | `autoduo@<name>.service` |

The instance called `default` keeps the original paths
(`/var/lib/autoduo/vault.enc`), which is why an install from 0.1.0 keeps
working without being migrated.

If you start a second copy of the same instance it notices the lock, exits 5,
and systemd leaves it alone instead of restarting it forever. Two copies
approving the same push twice is not a failure mode anyone wants.

---

## Commands

```
autoduo setup       set up an account: warning, enrol, enable, start
autoduo install     install the service without the interactive parts
autoduo uninstall   remove one instance (--all removes everything)
autoduo enroll      enrol with an activation URL, without starting anything
autoduo start       start the daemon
autoduo stop        stop the daemon
autoduo restart     restart the daemon
autoduo status      what systemd thinks, plus the recent log
autoduo logs        tail the journal (Ctrl+C to stop)
autoduo instances   every instance and its state
autoduo watch       the live dashboard
autoduo version     print the version
```

Every command takes `--instance NAME`, before or after the verb, and defaults
to `default`. All of them need root.

---

## What it does

When something asks you to log in through Duo, Duo sends a push to the phone
you enrolled. AutoDuo is that phone. It sees the push, approves it, and the
login continues.

There is no browser automation and no scraping. During setup it registers
itself as a Duo Mobile device using the same endpoints the real Android app
uses, which is why it works at all. After that it is a signed HTTP poll, five
seconds apart, and a signed "approve" for each push it hasn't seen before.

One thing worth knowing if you watch the log: a push is marked seen before the
approve call goes out, so the next poll five seconds later doesn't approve the
same one again. If the call fails in a way that's worth retrying, the mark is
withdrawn on purpose. If the process is killed mid-approval, the push may get
approved twice; Duo answers the second attempt with "already answered" and the
daemon parks it. When Duo rejects an approval, it sorts the push into one of
three buckets — terminal, auth problem, or worth retrying — and only retries
the last kind, a bounded number of times.

---

## Logs

| File | What's in it |
|---|---|
| `/var/log/autoduo/daemon.log` | What the daemon did: start, stop, polls, approvals, errors |
| `/var/log/autoduo/audit.log` | One line per approval, with the personal data stripped |
| `/var/log/autoduo/traffic.log` | HTTP metadata, only if you turn it on |

A named instance writes to `/var/log/autoduo/<name>/`. The `akey`, the
`pkey` and the `Authorization` header never appear in any log in full. The
logs are 0640 in a 0750 directory, and `/etc/logrotate.d/autoduo` rotates
both the old flat layout and the per-instance one. Rotation uses
`copytruncate`, so a rotation does not interrupt the daemon.

The audit record is built from a redacted copy of the transaction: screen
text, user agent and raw attributes are dropped, and an IP is truncated. You
get the app and the fact of the approval, not who logged in from where.

---

## Requirements

- Debian 12 or Ubuntu 22.04 and later, with systemd
- Python 3.10, 3.11 or 3.12
- About 30 MB of RAM, and 50 MB of disk for the venv, vault and logs

Clock accuracy is not cosmetic. The request signature includes the date
header, so a server whose clock drifts a few minutes starts failing
authentication. The installer enables `systemd-timesyncd` for exactly this
reason.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Tests run offline, on three Python
versions, and a pull request is welcome.

## Security

See [SECURITY.md](SECURITY.md). Please report problems privately, not in a
public issue.

## License

MIT. See [LICENSE](LICENSE).

---

## Disclaimer

This software comes with no warranty, express or implied. If it approves
something you didn't want approved, locks you out, gets your account
suspended, or makes a bad day worse, that is yours to carry, not the
authors'. You are responsible for the machine it runs on. Use it on systems
you own and understand.
