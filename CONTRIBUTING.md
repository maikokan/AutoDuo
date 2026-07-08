# Contributing

Thanks for your interest in autoduo.

## Setup

```bash
git clone https://github.com/maikokan/AutoDuo.git
cd autoduo
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

For development with testing tools:

```bash
.venv/bin/pip install -r requirements-dev.txt
```

## Running tests

```bash
.venv/bin/pytest tests/ -v
```

Tests are pure unit tests with no network access. Mocks for the Duo
HTTP endpoints live in `tests/test_client.py`.

## Code style

- Python 3.10+ syntax (we test on 3.10, 3.11, 3.12).
- Type hints on public functions.
- Tests for any new public function or behavior.
- Keep functions small; refactor when they grow past ~50 lines.

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add audit-log PII redaction
fix: handle empty transaction list
docs: clarify Verified Push limitation
test: add circuit-breaker cooldown test
```

## Filing issues

When filing an issue, please include:
- autoduo version (`autoduo --version`)
- Output of `systemctl status autoduo` if relevant
- Relevant log entries from `/var/log/autoduo/daemon.log`
  (NEVER paste authorization headers or full activation URLs)

## Reporting security issues

See [SECURITY.md](SECURITY.md). Do not file security issues publicly.

## License

By contributing, you agree that your contributions will be licensed
under the MIT License (see [LICENSE](LICENSE)).