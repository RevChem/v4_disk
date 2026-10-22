"""Сводка 14.2 формируется, но видна только роли «Разработчик»."""
from __future__ import annotations

from pathlib import Path

from app.application.schedule_config import (
    restore_developer_only_report_ids,
    strip_developer_only_report_ids,
)
from app.legacy.report_generation import (
    DEVELOPER_ONLY_REPORT_IDS,
    EXPLICIT_ONLY_REPORT_IDS,
    REPORT_CONFIGS_BASE,
    _prepare_ordered_report_ids,
    list_report_catalog_for_ui,
)
from app.legacy.report_runner import _report_id_from_filename


def test_catalog_registers_yield_days_for_developers_only():
    cfg = next(item for item in REPORT_CONFIGS_BASE if item.id == "14.2")
    assert cfg.script == "14.2_combine_yield_days.py"
    plan = Path(__file__).resolve().parents[2] / "svodki" / "plan" / cfg.script
    assert plan.is_file()
    assert "14.2" in DEVELOPER_ONLY_REPORT_IDS
    assert "14.2" in EXPLICIT_ONLY_REPORT_IDS

    public_ids = {row["id"] for row in list_report_catalog_for_ui()}
    assert "14" in public_ids
    assert "14.2" not in public_ids

    developer_rows = {
        row["id"]: row for row in list_report_catalog_for_ui(include_developer_only=True)
    }
    row = developer_rows["14.2"]
    assert row["picker_visible"] is True
    assert row["default_selected"] is False
    assert str(row["title"]).startswith("14.2.")

    assert "14.2" not in _prepare_ordered_report_ids(None)
    assert "14.2" in _prepare_ordered_report_ids(["2", "14.2"])


def test_filename_stays_distinct_from_report_14():
    name = "14.2. Аналитика урожайности комбайнов за 06.10.2026.xlsx"
    assert _report_id_from_filename(name) == "14.2"
    assert _report_id_from_filename("14. Выработка комбайнов за 06.10.2026.xlsx") == "14"
    assert _report_id_from_filename("harvester_report_by_firm_2026-10-22.pdf") == "14"
    assert (
        _report_id_from_filename(
            "Аналитика по 7 дневной работе комбайнов 01.10.2026 - 07.10.2026.pdf"
        )
        == "14.2"
    )


def test_download_pdf_names_match_archive_and_ui_title_stays_short():
    from datetime import date

    from app.legacy.report_generation import pdf_download_title
    from app.legacy.report_runner import _display_title_from_filename

    summary_date = date(2026, 10, 20)
    expected = {
        "1": "1.СВОДКА ХК Крупно за 19.10.2026",
        "2": "2.Сводка ХК Детальная за 19.10.2026",
        "3": "3.РАБОТА ТЕХНИКИ Внесение МУ за 19.10.2026",
        "4": "4.РАБОТА ТЕХНИКИ Боронование за 19.10.2026",
        "5": "5.Посев 2026 за 19.10.2026",
        "6": "6.РАБОТА ТЕХНИКИ Опрыскивание за 19.10.2026",
        "7": "7.РАБОТА ТЕХНИКИ Дискование за 19.10.2026",
        "8": "8.РАБОТА ТЕХНИКИ Культивация за 19.10.2026",
        "9": "9.РАБОТА ТЕХНИКИ Вспашка и глубокорыхление за 19.10.2026",
        "10": "10. Уборка культур по холдингу за 19.10.2026",
        "11": "11. Уборка за 19.10.2026",
        "12": "12. Статистика простоя при уборке урожая за 19.10.2026",
        "13": "13. Сводка обзор ХК за 19.10.2026",
        "14": "14. Выработка комбайнов по агрофирмам за 19.10.2026",
    }
    for rid, name in expected.items():
        assert pdf_download_title(rid, summary_date, "fallback") == name
        assert _report_id_from_filename(name + ".pdf") == rid
    assert (
        _display_title_from_filename("1.СВОДКА ХК Крупно за 19.10.2026.pdf", summary_date)
        == "1. ХК Крупно · 2026-10-20"
    )


def test_schedule_hides_developer_report_and_keeps_saved_one():
    hidden = frozenset({"14.2"})
    before = {"generation": [{"id": "morning", "report_ids": ["1", "14.2"]}], "mail": []}
    shown = strip_developer_only_report_ids(before, hidden)
    assert shown["generation"][0]["report_ids"] == ["1"]

    edited = {"generation": [{"id": "morning", "report_ids": ["1", "2"]}], "mail": []}
    saved = restore_developer_only_report_ids(before, edited, hidden)
    assert saved["generation"][0]["report_ids"] == ["1", "2", "14.2"]

    added = {"generation": [{"id": "morning", "report_ids": ["1", "14.2"]}], "mail": []}
    plain = {"generation": [{"id": "morning", "report_ids": ["1"]}], "mail": []}
    blocked = restore_developer_only_report_ids(plain, added, hidden)
    assert blocked["generation"][0]["report_ids"] == ["1"]
