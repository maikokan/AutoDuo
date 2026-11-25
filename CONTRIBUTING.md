# Contributing

Thanks for looking. This is a small project and it's easy to get a change in.

## Getting set up

```bash
git clone https://github.com/maikokan/AutoDuo.git
cd AutoDuo
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

`requirements-dev.txt` pulls in `requirements.txt` plus pytest and bandit.

## Tests

```bash
.venv/bin/python -m pytest tests/ -v
```

They run offline and take about fifteen seconds. Everything that would touch
the network is faked: `MockTransport` in `tests/test_client.py` replaces
`requests.Session.send` with a queue of canned responses, and the daemon
tests pass in a fake clock, a fake sleep and a `FakeDeviceClient`.

Please don't add a test that makes a real request. If you need a new response
shape, extend the mock.

`tests/fixtures/golden_test_key.pem` is a throwaway key that exists only to
pin the request-signing format. Don't replace it — the golden vectors stop
meaning anything if the key changes.

## Before you open a pull request

These are the same checks CI runs, and they're quick:

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/bandit -r autoduo/ -ll
python3 -m compileall autoduo/
shellcheck install.sh scripts/autoduo scripts/install.sh scripts/uninstall.sh
systemd-analyze verify systemd/autoduo.service systemd/autoduo@.service
logrotate -d scripts/logrotate-autoduo
```

## Style

- Python 3.10+, because that's the oldest version in the test matrix.
- Type hints on public functions.
- A test for any new public behaviour. If it fixes a bug, the test should
  fail on the old code — that's how we know it's the right test.
- Keep functions under about fifty lines. Past that, pull a helper out.
- Dependencies are `requests` and `cryptography`. Anything else needs a
  conversation first, not a pull request.

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/), with the body
saying why rather than restating the diff:

```
feat: add per-instance units
fix: treat a non-JSON 200 as an error, not an approval
docs: explain why the clock matters
test: cover the redirect refusal
```

## Reporting bugs

An issue is fine for anything that isn't a security problem. Include:

- the version (`autoduo version`)
- `systemctl status autoduo` if it's about the service
- the relevant lines from `/var/log/autoduo/daemon.log`

Never paste authorization headers, full activation URLs, or the vault.

For security problems, see [SECURITY.md](SECURITY.md) and use a private
advisory instead of a public issue.

## License

Contributions are MIT, same as the project. See [LICENSE](LICENSE).
