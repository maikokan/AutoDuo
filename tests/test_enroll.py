"""Tests for autoduo.enroll (pure-function paths) and autoduo.config.

The actual HTTP call to Duo is NOT covered here — that requires a real
activation URL and is tested in production enrollment. We test:
- Activation URL parsing (extract code, validate host).
- Source-reading paths (file/stdin, delete after read).
- Passphrase rejection (argv forbidden, file required).
- Env-var config loading.
"""
import io
import pathlib

import pytest

from autoduo import enroll


# --- _extract_activation_code ----------------------------------------------

def test_extract_activation_code_from_m_host_url():
    """The m- portal URL returns the SHORT code, not the composite."""
    url = "https://m-08dc11c9.duosecurity.com/activate/sC6kGnHdYcARtZaVGkFd"
    code = enroll._extract_activation_code(url)
    # Short code is returned directly — the API expects only this.
    assert code == "sC6kGnHdYcARtZaVGkFd"


def test_extract_activation_code_from_m_android_url():
    """Duo also serves an /android/CODE variant of the portal URL."""
    url = "https://m-08dc11c9.duosecurity.com/android/OnPDhPsDZDKcGEmlGJZb"
    assert enroll._extract_activation_code(url) == "OnPDhPsDZDKcGEmlGJZb"


def test_extract_activation_code_from_api_url():
    """The api- URL already has the full composite code."""
    url = "https://api-12345678901234567890123456789012.duosecurity.com/push/v2/activation/abc-def-123"
    assert enroll._extract_activation_code(url) == "abc-def-123"


def test_extract_activation_code_from_trailing_slash():
    url = "https://api-X.duosecurity.com/push/v2/activation/MYCODE/"
    assert enroll._extract_activation_code(url) == "MYCODE"


def test_extract_activation_code_rejects_no_activation():
    with pytest.raises(enroll.EnrollError, match="activation"):
        enroll._extract_activation_code("https://api-X.duosecurity.com/something/else")


def test_extract_activation_code_rejects_empty_code():
    with pytest.raises(enroll.EnrollError):
        enroll._extract_activation_code(
            "https://api-X.duosecurity.com/push/v2/activation/"
        )


def test_extract_activation_code_rejects_unknown_path():
    with pytest.raises(enroll.EnrollError, match="unrecognized"):
        enroll._extract_activation_code("https://api-X.duosecurity.com/foo/bar")


# --- _activation_host ------------------------------------------------------

def test_activation_host_accepts_api_url():
    url = "https://api-12345678901234567890123456789012.duosecurity.com/push/v2/activation/X"
    assert enroll._activation_host(url) == "api-12345678901234567890123456789012.duosecurity.com"


def test_activation_host_normalizes_m_to_api():
    """m- host is normalized to api- host."""
    url = "https://m-08dc11c9.duosecurity.com/activate/sC6kGnHdYcARtZaVGkFd"
    assert enroll._activation_host(url) == "api-08dc11c9.duosecurity.com"


def test_activation_host_rejects_evil():
    with pytest.raises(enroll.EnrollError):
        enroll._activation_host(
            "https://evil.example.com/push/v2/activation/X"
        )


def test_activation_host_rejects_http():
    with pytest.raises(enroll.EnrollError):
        enroll._activation_host(
            "http://api-X.duosecurity.com/push/v2/activation/X"
        )


def test_activation_host_rejects_lookalike():
    with pytest.raises(enroll.EnrollError):
        enroll._activation_host(
            "https://api-X.duosecurity.com.evil.example/push/v2/activation/X"
        )


def test_activation_host_rejects_bare_duosecurity():
    with pytest.raises(enroll.EnrollError, match="host"):
        enroll._activation_host(
            "https://duosecurity.com/push/v2/activation/X"
        )


def test_activation_host_rejects_non_default_port():
    with pytest.raises(enroll.EnrollError, match="port"):
        enroll._activation_host(
            "https://api-X.duosecurity.com:8443/push/v2/activation/X"
        )


def test_passphrase_file_loose_perms_hard_error(tmp_path):
    """Passphrase file with mode 0644 must hard-error, not warn."""
    f = tmp_path / "p.txt"
    f.write_text("hunter2\n")
    f.chmod(0o644)
    args = enroll.build_parser().parse_args([
        "--vault", str(tmp_path / "v.enc"),
        "--activation-file", str(tmp_path / "a.txt"),
        "--passphrase-file", str(f),
    ])
    with pytest.raises(enroll.EnrollError, match="chmod 0400"):
        enroll._read_passphrase(args)


# --- _read_activation_source -----------------------------------------------

def test_read_activation_from_file(tmp_path, monkeypatch):
    f = tmp_path / "act.txt"
    f.write_text("https://api-X.duosecurity.com/push/v2/activation/CODE")
    f.chmod(0o600)
    args = enroll.build_parser().parse_args([
        "--vault", str(tmp_path / "v.enc"),
        "--activation-file", str(f),
        "--passphrase-file", str(tmp_path / "p.txt"),
    ])
    assert enroll._read_activation_source(args) == (
        "https://api-X.duosecurity.com/push/v2/activation/CODE"
    )
    # File is always deleted after read (one-shot bearer secret).
    assert not f.exists()


def test_read_activation_always_deletes_file(tmp_path):
    """Activation file is always deleted after read — one-shot bearer secret."""
    f = tmp_path / "act.txt"
    f.write_text("https://api-X.duosecurity.com/push/v2/activation/CODE")
    f.chmod(0o600)
    args = enroll.build_parser().parse_args([
        "--vault", str(tmp_path / "v.enc"),
        "--activation-file", str(f),
        "--passphrase-file", str(tmp_path / "p.txt"),
    ])
    enroll._read_activation_source(args)
    assert not f.exists()


def test_read_activation_rejects_loose_perms(tmp_path):
    """Activation file with loose perms must hard-error, not warn."""
    f = tmp_path / "act.txt"
    f.write_text("https://api-X.duosecurity.com/push/v2/activation/CODE")
    f.chmod(0o644)
    args = enroll.build_parser().parse_args([
        "--vault", str(tmp_path / "v.enc"),
        "--activation-file", str(f),
        "--passphrase-file", str(tmp_path / "p.txt"),
    ])
    with pytest.raises(enroll.EnrollError, match="chmod 0600"):
        enroll._read_activation_source(args)


def test_read_activation_from_stdin(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(
        "https://api-X.duosecurity.com/push/v2/activation/CODE\n"
    ))
    args = enroll.build_parser().parse_args([
        "--vault", str(tmp_path / "v.enc"),
        "--activation-stdin",
        "--passphrase-file", str(tmp_path / "p.txt"),
    ])
    assert "CODE" in enroll._read_activation_source(args)


def test_read_activation_requires_source(tmp_path):
    """No --activation-file and no --activation-stdin must raise at parse time."""
    parser = enroll.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([
            "--vault", str(tmp_path / "v.enc"),
            "--passphrase-file", str(tmp_path / "p.txt"),
        ])


# --- _read_passphrase ------------------------------------------------------

def test_read_passphrase_from_file(tmp_path):
    f = tmp_path / "p.txt"
    f.write_text("hunter2\n")
    f.chmod(0o400)  # match production requirement
    args = enroll.build_parser().parse_args([
        "--vault", str(tmp_path / "v.enc"),
        "--activation-file", str(tmp_path / "a.txt"),
        "--passphrase-file", str(f),
    ])
    assert enroll._read_passphrase(args) == "hunter2"


def test_read_passphrase_rejects_when_missing(tmp_path):
    args = enroll.build_parser().parse_args([
        "--vault", str(tmp_path / "v.enc"),
        "--activation-file", str(tmp_path / "a.txt"),
    ])
    with pytest.raises(enroll.EnrollError, match="must supply"):
        enroll._read_passphrase(args)


def test_read_passphrase_no_passphrase_flag(tmp_path):
    """--no-passphrase returns None."""
    args = enroll.build_parser().parse_args([
        "--vault", str(tmp_path / "v.enc"),
        "--activation-file", str(tmp_path / "a.txt"),
        "--no-passphrase",
    ])
    assert enroll._read_passphrase(args) is None


def test_passphrase_argv_forbidden(tmp_path):
    """Make sure `--passphrase foo` is rejected at parse time."""
    parser = enroll.build_parser()
    with pytest.raises(SystemExit):
        # argparse exits on unknown args.
        parser.parse_args([
            "--vault", str(tmp_path / "v.enc"),
            "--activation-file", str(tmp_path / "a.txt"),
            "--passphrase", "leaked",
        ])


# --- build_parser ----------------------------------------------------------

def test_help_exits_cleanly():
    parser = enroll.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--help"])


def test_version_via_module():
    import autoduo
    assert autoduo.__version__ == "0.1.0"