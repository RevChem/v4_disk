"""Ручные правки планов переносятся из тома и не затираются новым файлом."""
from __future__ import annotations

from app.config import settings
from app.legacy.plans_editor import load_edits_store


def test_legacy_plans_are_copied_once_and_then_kept(tmp_path, monkeypatch):
    legacy_dir = tmp_path / "uploads"
    legacy_dir.mkdir()
    legacy = legacy_dir / "plans_edits.json"
    legacy.write_text(
        '{"HARDCODED_PLAN": {"2.4 Вспашка": {"АФ": {"Соя": 3}}}}',
        encoding="utf-8",
    )
    dest = tmp_path / "data" / "plans_edits.json"
    monkeypatch.setattr(settings, "upload_dir", str(legacy_dir))
    monkeypatch.setattr(settings, "svodki_plans_edits_path", str(dest))

    loaded = load_edits_store()
    assert loaded["HARDCODED_PLAN"]["2.4 Вспашка"]["АФ"]["Соя"] == 3
    assert dest.is_file()

    dest.write_text(
        '{"HARDCODED_PLAN": {"2.4 Вспашка": {"АФ": {"Соя": 11}}}}',
        encoding="utf-8",
    )
    legacy.write_text(
        '{"HARDCODED_PLAN": {"2.4 Вспашка": {"АФ": {"Соя": 3}}}}',
        encoding="utf-8",
    )
    kept = load_edits_store()
    assert kept["HARDCODED_PLAN"]["2.4 Вспашка"]["АФ"]["Соя"] == 11
