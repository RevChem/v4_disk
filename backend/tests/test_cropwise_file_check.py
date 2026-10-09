"""Порог сверки файла сводки с суммой work.json — 0.5 га, как ha_thresholds."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("SVODKI_ROOT", str(ROOT / "svodki"))
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "svodki" / "plan"))

from app.application.cropwise_file_check import ha_matches, work_json_total  # noqa: E402
from app.schemas import GenerateReportsIn  # noqa: E402
from datetime import date


def test_match_uses_half_hectare():
    assert ha_matches(100, 100.4)
    assert not ha_matches(100, 100.6)
    assert ha_matches(1000, 1000.4)
    assert not ha_matches(1000, 1006)


def test_work_json_total_sums_loaded_seasons(monkeypatch):
    import proverka.cropwise_ui as ui

    monkeypatch.setattr(ui, "cropwise_total_ha", lambda _slot, _hints: 12.5)
    total, note = work_json_total(
        {
            "by_year": {
                2026: {"load_status": "loaded"},
                2027: {"load_status": "empty"},
            }
        },
        (),
    )
    assert total == 12.5
    assert note == ""


def test_work_json_total_skips_seasons_outside_the_group(monkeypatch):
    import proverka.cropwise_ui as ui

    monkeypatch.setattr(ui, "cropwise_total_ha", lambda slot, _hints: float(slot["ha"]))
    total, note = work_json_total(
        {
            "by_year": {
                2026: {"load_status": "loaded", "ha": 100},
                2027: {"load_status": "loaded", "ha": 40},
            }
        },
        (),
        seasons={"2027"},
    )
    assert total == 40
    assert note == ""


def test_work_json_total_none_when_nothing_loaded():
    total, note = work_json_total(
        {"by_year": {2026: {"load_status": "not_loaded", "error": "таймаут"}}},
        (),
    )
    assert total is None
    assert "таймаут" in note


def test_editor_season_outside_ui_is_mentioned(monkeypatch):
    import proverka.cropwise_ui as ui

    monkeypatch.setattr(ui, "cropwise_total_ha", lambda slot, _hints: float(slot["ha"]))
    total, note = work_json_total(
        {"by_year": {2027: {"load_status": "loaded", "ha": 10}}},
        (),
        seasons={"2025", "2027"},
    )
    assert total == 10
    assert "2025" in note
    assert "не вошли в сумму" in note


def test_report_slots_do_not_reuse_one_work_json():
    from app.application.cropwise_file_check import _slots_for_report, groups_for_file_check

    packed = {"by_year": {2026: {"load_status": "loaded"}}}
    assert _slots_for_report("9", packed, ("2.4 Вспашка", "2.8 Глубокорыхление")) == []
    assert "4.1 Посев (овощи)" in groups_for_file_check("5", ("4.0 Посев",))
    assert "5.0 Уборка" in groups_for_file_check("10", ("5.4 Уборка картофеля, лука",))


def test_missing_xlsx_falls_back_to_pdf(tmp_path):
    from app.application.cropwise_file_check import _report_path

    pdf = tmp_path / "ok.pdf"
    pdf.write_bytes(b"%PDF")

    class Item:
        xlsx_path = tmp_path / "gone.xlsx"
        pdf_path = pdf

    assert _report_path(Item()) == pdf


def test_report_slots_stay_inside_its_groups():
    from app.application.cropwise_file_check import _slots_for_report

    packed = {
        "2.4 Вспашка": {"by_year": {2026: {}}},
        "3.1 Опрыскивание": {"by_year": {2026: {}}},
        "4.0 Посев": {"by_year": {2026: {}}},
    }
    spray = [name for name, _slot in _slots_for_report(
        "6", packed, ("3.1 Опрыскивание", "3.2 Опрыскивание (овощи)"),
    )]
    assert spray == ["3.1 Опрыскивание"]
    summary = {name for name, _slot in _slots_for_report("1", packed, ())}
    assert summary == {"2.4 Вспашка", "3.1 Опрыскивание", "4.0 Посев"}


def test_works_window_stays_in_calendar_year_after_july():
    from datetime import date

    from app.legacy.works_summary import _works_bounds

    start, end, _prev_start, _prev_end = _works_bounds(date.today().year + 1)
    assert start == date(date.today().year, 1, 1)
    assert end == date.today()


def test_editor_rules_follow_the_report():
    from proverka.crop_compare import editor_rules_for

    assert editor_rules_for("2", "2.4 Вспашка") == ("2.4 Вспашка", "remap")
    assert editor_rules_for("3", "1.1 Внесение минеральных удобрений")[1] == "remap"
    assert editor_rules_for("7", "2.9 Дискование") == ("2.9 Дискование", "totals")
    assert editor_rules_for("6", "3.1 Опрыскивание") == ("3.1 Опрыскивание", "user")
    assert editor_rules_for("1", "2.9 Дискование") == ("2.9 Дискование", "user")
    assert editor_rules_for("9", "2.4 Вспашка")[0] is None
    assert editor_rules_for("11", "5.0 Уборка") == ("5.0 Уборка", "totals")


def test_xlsx_summary_reads_firms_and_crops(tmp_path):
    from openpyxl import Workbook

    from proverka.file_values import crop_areas_from_path, firm_areas_from_path

    path = tmp_path / "6.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Сводка"
    ws.append(["3.1 Опрыскивание"])
    ws.append(["Агрофирма", "Обработано за сутки, га"])
    ws.append(["Аксу-Агро", 12.5])
    ws.append(["Итого", 12.5])
    ws.append(["Опрыскивание овощных культур"])
    ws.append(["№ п/п", "Культура", "Факт, сутки га"])
    ws.append([1, "Картофель", 4.0])
    ws.append([2, "Картофель", 1.5])
    wb.save(path)
    firms = firm_areas_from_path(path, "6")
    crops = crop_areas_from_path(path, "6")
    assert firms["3.1 Опрыскивание"]["Аксу-Агро"] == 12.5
    assert crops["3.2 Опрыскивание (овощи)"]["Картофель"] == 5.5


def test_generate_options_keep_cropwise_flag():
    body = GenerateReportsIn(summary_date=date(2026, 10, 7), compare_cropwise=True)
    assert body.generation_job_options() == {"compare_cropwise": True}
    off = GenerateReportsIn(summary_date=date(2026, 10, 7))
    assert off.generation_job_options() is None
