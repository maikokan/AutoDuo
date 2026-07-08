"""Tests for autoduo.cli helpers (disclaimer, retry, validators)."""
import builtins
import io
import pathlib

import pytest

from autoduo import cli


def test_validate_url_accepts_m_activate(monkeypatch):
    err = cli._validate_url("https://m-X.duosecurity.com/activate/CODE")
    assert err is None


def test_validate_url_accepts_m_android(monkeypatch):
    err = cli._validate_url("https://m-X.duosecurity.com/android/CODE")
    assert err is None


def test_validate_url_accepts_api_push(monkeypatch):
    err = cli._validate_url("https://api-X.duosecurity.com/push/v2/activation/CODE")
    assert err is None


def test_validate_url_rejects_evil(monkeypatch):
    err = cli._validate_url("https://evil.example.com/activate/X")
    assert err is not None


def test_validate_url_rejects_wrong_path(monkeypatch):
    err = cli._validate_url("https://m-X.duosecurity.com/wrong/CODE")
    assert err is not None


# --- _prompt_with_retry ---------------------------------------------------

def test_prompt_with_retry_accepts_valid(monkeypatch):
    inputs = iter(["", "", "good"])
    monkeypatch.setattr(builtins, "input", lambda _: next(inputs))
    out = cli._prompt_with_retry("? ")
    assert out == "good"


def test_prompt_with_retry_returns_none_on_eof(monkeypatch):
    def raise_eof(_):
        raise EOFError
    monkeypatch.setattr(builtins, "input", raise_eof)
    assert cli._prompt_with_retry("? ") is None


def test_prompt_with_retry_gives_up_after_max(monkeypatch):
    inputs = iter([""] * 10)  # always empty
    monkeypatch.setattr(builtins, "input", lambda _: next(inputs))
    assert cli._prompt_with_retry("? ") is None


def test_prompt_with_retry_uses_validator(monkeypatch):
    inputs = iter(["bad", "still bad", "good"])
    monkeypatch.setattr(builtins, "input", lambda _: next(inputs))
    def validator(s):
        return None if s == "good" else "nope"
    out = cli._prompt_with_retry("? ", validator=validator)
    assert out == "good"


# --- _confirm -------------------------------------------------------------

def test_confirm_yes(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda _: "y")
    assert cli._confirm("? ") is True


def test_confirm_no(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda _: "n")
    assert cli._confirm("? ") is False


def test_confirm_empty_defaults_no(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda _: "")
    assert cli._confirm("? ") is False


def test_confirm_eof_defaults_no(monkeypatch):
    def raise_eof(_):
        raise EOFError
    monkeypatch.setattr(builtins, "input", raise_eof)
    assert cli._confirm("? ") is False