# Security

## What you are giving up

AutoDuo defeats the security purpose of 2FA for the account it is enrolled
on. That is the design, not a bug in it. Anyone with a shell on the host — or
anyone who can read `/var/lib/autoduo/vault.enc` and the passphrase file, if
you use one — can approve any Duo push for that account. So can anything
running as root on the box.

If your Duo tenant enforces Verified Duo Push, where a human has to type a
code from the browser into the phone, the daemon will still approve the push
and the login will still fail. There is no way around that one.

This assumes a machine you own, set up like this:

- One user, no shared accounts, no other services facing the internet.
- SSH keys only, `PermitRootLogin no`, something like fail2ban in front.
- Disk encryption, so a stolen disk doesn't hand over the vault and the
  passphrase file together.
- Patched regularly, because this box is now part of your login.

## Reporting a vulnerability

Use a private advisory:

<https://github.com/maikokan/AutoDuo/security/advisories/new>

That reaches me without putting the details in a public issue. I aim to reply
within a week and to ship a fix for anything serious within a month. There is
no bug bounty — this is a small project — but you'll get credit if you want
it.

## What the logs contain

`/var/log/autoduo/audit.log` has one line per approval:

- `urgid` and `txid`, Duo's identifiers for the transaction
- `factor`, `app` and `tx_type`, which is what got approved

It does not contain usernames, raw IPs, screen text, user agents or
locations. The record is built from `autoduo.redact.redact_transaction()`,
which drops those fields and truncates an IP, before anything is written. The
file is 0640 in a 0750 directory.

If you turn on `--traffic-log`, that file gets:

- request URLs, with `akey` and `pkey` replaced by their length
- headers, with `Authorization` and `x-duo-date` replaced entirely
- a SHA-256 and a length for each body, never the body

## How it talks to Duo

- Only hosts ending in `.duosecurity.com` are accepted. The check runs when
  the client is built, and the host never comes from a log line.
- Redirects are refused. The client sends `allow_redirects=False`, so a 3xx
  from anything answering on a Duo subdomain can't move a signed request to a
  host nobody checked.
- `Retry-After` is clamped to 0–300 seconds, and a value that isn't a number
  falls back to 30. A server can't stop the daemon from approving for a week
  by sending one header.
- An HTTP 200 whose body isn't JSON is an error, on polls and approvals
  alike. A 200 is not a success unless the body says so.

## Hardening checklist

- [ ] `sudo autoduo setup`, with no passphrase. The vault is then protected
      by file permissions alone. If that isn't enough for you, skip `setup`
      and use `python -m autoduo.enroll --passphrase-file <file>` directly —
      `setup` only knows how to seal with an empty passphrase. Keep the
      passphrase file on a separate encrypted volume, mode 0400; the daemon
      refuses to start if it is readable by anyone else.
- [ ] `chmod 0600 /var/lib/autoduo/vault.enc`. Already enforced, and a loose
      mode is reported at startup as `vault_mode_insecure`.
- [ ] `chmod 0640 /var/log/autoduo/*.log`. Enforced for `daemon.log`,
      `audit.log` and `traffic.log`.
- [ ] Leave `systemd-timesyncd` enabled. The signature covers the date
      header, so a drifting clock looks exactly like bad credentials.
- [ ] Turn on Duo's anomaly alerts, so you hear it from them before you hear
      it from a locked-out morning.
- [ ] Disable Verified Duo Push for the user, or accept that this can't get
      past it.
- [ ] If Duo starts noticing the cadence, add jitter to the poll interval.

## If you are reporting a bug

Don't include activation URLs, `Authorization` headers, vault contents, or
audit entries with real transaction IDs. None of that helps reproduce a
problem, and all of it is a credential or an identifier you'd rather not
hand out.
