"""CROPWISE_SESSION_COOKIE в .env: замена строки и повторное чтение."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "svodki" / "plan"))

from proverka.reconcile import (  # noqa: E402
    load_env,
    normalize_cookie_header,
    rewrite_session_cookie_text,
    save_session_cookie,
    session_cookie,
)


def test_rewrite_replaces_cookie_and_keeps_other_keys():
    source = (
        "CROPWISE_API_TOKEN=secret\n"
        "# CROPWISE_SESSION_COOKIE=old-comment\n"
        'CROPWISE_SESSION_COOKIE= "old"\n'
        "CROPWISE_SESSION_COOKIE=duplicate\n"
        "DEEPSEEK_API_KEY=key\n"
    )
    updated = rewrite_session_cookie_text(source, "_cropio_ses=abc; other=1")
    assignments = [
        line for line in updated.splitlines() if line.startswith("CROPWISE_SESSION_COOKIE=")
    ]
    assert assignments == ['CROPWISE_SESSION_COOKIE="_cropio_ses=abc; other=1"']
    assert "# CROPWISE_SESSION_COOKIE=old-comment" in updated
    assert "CROPWISE_API_TOKEN=secret" in updated
    assert "DEEPSEEK_API_KEY=key" in updated


def test_normalize_joins_lines_and_strips_header_name():
    raw = "Cookie: _cropio_ses=abc\nremember_token=def;\n"
    assert normalize_cookie_header(raw) == "_cropio_ses=abc; remember_token=def"


def test_save_then_load_env_replaces_process_cookie(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text('CROPWISE_API_TOKEN=keep\nCROPWISE_SESSION_COOKIE="old"\n', encoding="utf-8")
    monkeypatch.setenv("CROPWISE_SESSION_COOKIE", "stale-from-process")
    monkeypatch.setattr("proverka.reconcile.env_file_path", lambda: env_path)

    saved = save_session_cookie('_cropio_ses=fresh; x="y"')
    assert saved == '_cropio_ses=fresh; x="y"'
    assert os.environ["CROPWISE_SESSION_COOKIE"] == saved

    monkeypatch.setenv("CROPWISE_SESSION_COOKIE", "stale-again")
    load_env()
    assert session_cookie() == saved
    text = env_path.read_text(encoding="utf-8")
    assert "CROPWISE_API_TOKEN=keep" in text
    assert "stale" not in text


def test_empty_cookie_clears_env(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text('CROPWISE_SESSION_COOKIE="old"\n', encoding="utf-8")
    monkeypatch.setattr("proverka.reconcile.env_file_path", lambda: env_path)
    monkeypatch.setenv("CROPWISE_SESSION_COOKIE", "old")

    assert save_session_cookie("   ") == ""
    assert "CROPWISE_SESSION_COOKIE" not in os.environ
    load_env()
    assert session_cookie() == ""
