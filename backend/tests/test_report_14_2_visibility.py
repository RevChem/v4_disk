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


def test_script_pdf_names_keep_report_id(tmp_path):
    from app.legacy.report_generation import _extract_report_id_from_stem, _pdf_named_as_script

    assert _extract_report_id_from_stem("1. fertilizer_report_2026-10-22") == "1"
    assert _extract_report_id_from_stem("2. report_crop_works_compact_2026-10-22") == "2"
    assert _extract_report_id_from_stem("report_harrowing_2026-10-22") == "4"
    assert _extract_report_id_from_stem("report_plowing_2026-10-22") == "9"
    assert _extract_report_id_from_stem("crop_report_holding_2026-10-22") == "10"
    assert _extract_report_id_from_stem("11. report_harvest_2026-10-22") == "11"
    assert _extract_report_id_from_stem("12. report_harvest_idle_2026-10-22") == "12"
    assert _report_id_from_filename("8. report_cultivation_2026-10-22.pdf") == "8"

    src_xlsx = tmp_path / "1. fertilizer_report_2026-10-22.xlsx"
    src_pdf = tmp_path / "1. fertilizer_report за 21.10.2026.pdf"
    src_xlsx.write_bytes(b"")
    src_pdf.write_bytes(b"%PDF")
    named = _pdf_named_as_script(src_pdf, src_xlsx)
    assert named.name == "1. fertilizer_report_2026-10-22.pdf"
    assert named.is_file()


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
