"""«Cropwise за сезон»: чтение сезонных колонок, кукуруза, порог 10 га."""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("SVODKI_ROOT", str(ROOT / "svodki"))
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "svodki" / "plan"))
sys.path.insert(0, str(ROOT / "svodki" / "plan" / "utils" / "season"))

from openpyxl import Workbook  # noqa: E402

from app.application import cropwise_season_check as sc  # noqa: E402
from app.schemas import GenerateReportsIn  # noqa: E402

HARVEST = "5.3 Уборка кукурузы на зерно"
MOWING = "6.3 Скашивание с измельчением кукурузы"
REPORT_DATE = date(2026, 10, 14)


def test_limit_is_strictly_more_than_ten_hectares():
    assert not sc.season_over_limit(1000, 1010)
    assert not sc.season_over_limit(1010, 1000)
    assert sc.season_over_limit(1000, 1010.01)
    assert sc.season_over_limit(0, 25)


def test_season_option_goes_to_job_options():
    body = GenerateReportsIn(summary_date=REPORT_DATE, compare_cropwise_season=True)
    assert body.generation_job_options() == {"compare_cropwise_season": True}
    both = GenerateReportsIn(summary_date=REPORT_DATE, compare_cropwise=True, compare_cropwise_season=True)
    assert both.generation_job_options() == {"compare_cropwise": True, "compare_cropwise_season": True}
    assert GenerateReportsIn(summary_date=REPORT_DATE).generation_job_options() is None


def test_align_corn_when_file_splits_silage_and_grain():
    left, right = sc.align_corn(
        {"Кукуруза на силос": 50.0, "Кукуруза на зерно": 200.0, "Подсолнечник": 10.0},
        {"Кукуруза на силос": 50.0, "Кукуруза": 200.0},
    )
    assert left["Кукуруза на зерно"] == 200.0
    assert right == {"Кукуруза на силос": 50.0, "Кукуруза на зерно": 200.0}


def test_align_corn_when_file_has_one_corn_row():
    left, right = sc.align_corn(
        {"Кукуруза": 250.0},
        {"Кукуруза на силос": 50.0, "Кукуруза на зерно": 200.0},
    )
    assert left == {"Кукуруза": 250.0}
    assert right == {"Кукуруза": 250.0}


def _report2_workbook(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "5.0 Уборка"
    ws.append(["Агрофирма", "Культура", "Площадь культуры\nучётная, га", "План", "Выполнено работ\nв Га", "в %", "14.10"])
    ws.append(["АФ А", "Подсолнечник", 0, 500, 120.5, 24, 5])
    ws.append(["АФ А", "Кукуруза, из них:", 0, 400, 250, 62, 10])
    ws.append(["АФ А", "Кукуруза на зерно", 0, 300, 200, 66, 6])
    ws.append(["АФ А", "Кукуруза на силос", 0, 100, 50, 50, 4])
    ws.append([])
    ws.append(["СОВОКУПНАЯ ПЛОЩАДЬ ПО АГРОФИРМАМ"])
    ws.append(["Агрофирма", "План, га", "Выполнено, га", "%", "14.10"])
    ws.append(["АФ А", 900, 370.5, 41, 15])
    ws.append(["Итого", 900, 370.5, 41, 15])
    wb.save(path)


def test_report2_season_columns_are_read(tmp_path):
    from proverka.season_file_values import season_areas_from_path

    path = tmp_path / "2. Сводка.xlsx"
    _report2_workbook(path)
    got = season_areas_from_path(path, "2")
    assert got["5.0 Уборка"]["total"] == 370.5
    assert got["5.0 Уборка"]["firms"] == {"АФ А": 370.5}
    # «Кукуруза, из них:» — сумма, в культуры не входит: силос и зерно отдельно.
    assert got["5.0 Уборка"]["crops"] == {
        "Подсолнечник": 120.5,
        "Кукуруза на зерно": 200.0,
        "Кукуруза на силос": 50.0,
    }


def _report1_workbook(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Общий намолот"
    ws.append(["№ п/п", "Культура", "План, га", "Факт общий, га", "Валовый сбор общий, т", "Факт за сутки, га"])
    ws.append([1, "Подсолнечник", 500, 120.5, 300, 5])
    ws.append([7, "Кукуруза, из них:", 400, 250, 900, 10])
    ws.append(["7.1", "Кукуруза на зерно", 300, 200, 700, 6])
    ws.append(["7.2", "Кукуруза на силос", 100, 50, 200, 4])
    ws.append(["", "Итого", 900, 370.5, 1400, 15])
    other = wb.create_sheet("Выраб. — 5.0 Уборка")
    other.append(["Агрофирмы", "Обработано за сутки, га"])
    wb.save(path)


def test_report1_general_sheet_season_is_read(tmp_path):
    from proverka.season_file_values import season_areas_from_path

    path = tmp_path / "1. Крупно.xlsx"
    _report1_workbook(path)
    got = season_areas_from_path(path, "1")
    assert got["5.0 Уборка"]["total"] == 370.5
    assert got["5.0 Уборка"]["crops"] == {
        "Подсолнечник": 120.5,
        "Кукуруза на зерно": 200.0,
        "Кукуруза на силос": 50.0,
    }
    assert season_areas_from_path(path, "3") == {}
    assert season_areas_from_path(tmp_path / "x.pdf", "1") == {}


def _harvest_body(grain_a: str, mow_a: str, sun_a: str = "120.5") -> dict:
    cols = [
        {"key": "name", "name": "Культура"},
        {"key": "sun", "name": "Уборка подсолнечника, га", "work_type_names": ["5.1 Уборка подсолнечника"]},
        {"key": "harvest", "name": "Уборка, га", "work_type_names": [HARVEST]},
        {"key": "fodder", "name": "Кормозаготовка, га", "work_type_names": [MOWING]},
    ]
    rows = [
        {"name": "Всего", "sun": [sun_a], "harvest": [grain_a], "fodder": [mow_a]},
        {
            "name": "Подсолнечник",
            "sun": [sun_a], "harvest": ["0"], "fodder": ["0"],
            "children": [{"name": "АФ А", "sun": [sun_a], "harvest": ["0"], "fodder": ["0"]}],
        },
        {
            "name": "Кукуруза на зерно",
            "sun": ["0"], "harvest": [grain_a], "fodder": [mow_a],
            "children": [{"name": "АФ А", "sun": ["0"], "harvest": [grain_a], "fodder": [mow_a]}],
        },
    ]
    return {"cropColumns": cols, "cropRows": rows, "regionColumns": [], "regionRows": []}


def _fake_plans(monkeypatch):
    import plans
    import proverka.season_hectares as sh
    import report_platform
    import season_unified

    # Перезагрузка plans затёрла бы подмены ниже.
    monkeypatch.setattr(report_platform, "reload_editor_modules", lambda: None)
    monkeypatch.setattr(plans, "FIRM_ORDER", ["АФ А"])
    # Сводка 2 переименовывает культуры по плану группы; в тесте план не нужен.
    monkeypatch.setattr(plans, "crop_code_for_report", lambda group, crop: crop)
    monkeypatch.setattr(plans, "is_crop_user_excluded", lambda *args: False)
    monkeypatch.setattr(
        season_unified,
        "resolve_group_season_config",
        lambda name, rd: {
            "task_season_years": [2026],
            "history_years": [2026],
            "period_start": "2026-06-10",
            "period_end": "2026-10-14",
        },
    )
    return sh


def _created(path: Path, rid: str, title: str):
    return SimpleNamespace(report_id=rid, title=title, xlsx_path=path, pdf_path=None)


def _run(tmp_path, monkeypatch, body: dict, rid: str = "1"):
    _fake_plans(monkeypatch)
    path = tmp_path / ("1.xlsx" if rid == "1" else "2.xlsx")
    (_report1_workbook if rid == "1" else _report2_workbook)(path)
    seen = {}

    def loader(cookie, prepared):
        seen["wt_ids"] = {item["group_name"]: item["wt_ids"] for item in prepared}
        return {item["group_name"]: {2026: {"ok": True, "load_status": "loaded", "body": body}} for item in prepared}

    lines, rows = sc.compare_generated_files_to_season(
        REPORT_DATE, [_created(path, rid, "Сводка")], cookie="cookie", loader=loader,
    )
    return lines, rows, seen


def test_matching_season_gives_no_red_rows(tmp_path, monkeypatch):
    lines, rows, seen = _run(tmp_path, monkeypatch, _harvest_body("200", "50"))
    assert rows == [], lines
    assert 84 in seen["wt_ids"]["5.0 Уборка"]


def test_silage_mowing_is_not_lost_and_grain_not_renamed(tmp_path, monkeypatch):
    # Cropwise: уборка зерна 200, скашивание 50 — файл так же: расхождений нет.
    _lines, rows, _seen = _run(tmp_path, monkeypatch, _harvest_body("200", "50"))
    assert rows == []
    # Скашивание в Cropwise больше на 60 га: силос расходится, зерно — нет.
    _lines, rows, _seen = _run(tmp_path, monkeypatch, _harvest_body("200", "110"))
    crop_rows = {row["group"]: row for row in rows}
    assert "5.0 Уборка / Кукуруза на силос" in crop_rows
    assert crop_rows["5.0 Уборка / Кукуруза на силос"]["delta"] == 60.0
    assert "5.0 Уборка / Кукуруза на зерно" not in crop_rows
    assert all(row["source"] == "cropwise_season" for row in rows)


def test_difference_of_ten_hectares_is_not_error_but_eleven_is(tmp_path, monkeypatch):
    _lines, rows, _seen = _run(tmp_path, monkeypatch, _harvest_body("210", "50"))
    assert rows == []
    _lines, rows, _seen = _run(tmp_path, monkeypatch, _harvest_body("211", "50"))
    assert {row["group"] for row in rows} >= {"5.0 Уборка", "5.0 Уборка / Кукуруза на зерно"}


def test_report2_checks_firms_and_crops(tmp_path, monkeypatch):
    _lines, rows, _seen = _run(tmp_path, monkeypatch, _harvest_body("200", "50"), rid="2")
    assert rows == []
    _lines, rows, _seen = _run(tmp_path, monkeypatch, _harvest_body("300", "50"), rid="2")
    labels = {row["group"] for row in rows}
    assert "5.0 Уборка" in labels
    assert "5.0 Уборка / АФ А" in labels
    assert "5.0 Уборка / Кукуруза на зерно" in labels


def test_unloaded_season_is_reported_as_unread(tmp_path, monkeypatch):
    _fake_plans(monkeypatch)
    path = tmp_path / "1.xlsx"
    _report1_workbook(path)

    def loader(cookie, prepared):
        return {item["group_name"]: {2026: {"load_status": "not_loaded", "error": "таймаут"}} for item in prepared}

    _lines, rows = sc.compare_generated_files_to_season(
        REPORT_DATE, [_created(path, "1", "Сводка")], cookie="cookie", loader=loader,
    )
    assert [row["status"] for row in rows] == ["unread"]
    assert "таймаут" in rows[0]["note"]


def test_no_cookie_skips_check(monkeypatch):
    import app.adapters.outbound.proverka.service as service

    monkeypatch.setattr(service, "session_cookie_from_env", lambda: "")
    lines, rows = sc.compare_generated_files_to_season(REPORT_DATE, [])
    assert rows == []
    assert "нет CROPWISE_SESSION_COOKIE" in lines[0]
