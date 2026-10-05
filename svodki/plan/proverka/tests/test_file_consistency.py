"""Итоги из готовых файлов: сводки 1/2 и влияние на статус сверки."""
from __future__ import annotations

from openpyxl import Workbook

from proverka.file_consistency import (
    build_sheet_checks,
    internal_issues,
    read_report1_workbook,
    read_report2_workbook,
    report1_sheet_check_rows,
    report2_sheet_check_rows,
    report2_xlsx_areas,
    report2_xlsx_crop_areas,
    report2_xlsx_firm_areas,
    report2_xlsx_internal_issues,
)
from proverka.operation_compare import apply_file_checks, build_cross_check

HEADER = ["Агрофирма", "Культура", "Площадь", "План", "Выполнено", "в %", "23 сентября", "24 сентября"]


def _make_sheet(ws, crop_rows, firm_rows, total):
    ws.append(["5.0 Уборка"])
    ws.append(["Сезон: 2026"])
    ws.append(HEADER)
    for firm, crop, ha in crop_rows:
        ws.append([firm, crop, None, None, None, None, 0, ha])
    ws.append([None])
    ws.append(["СОВОКУПНАЯ ПЛОЩАДЬ ПО АГРОФИРМАМ"])
    ws.append(["Агрофирма", "План, га", "Выполнено, га", "%", "23 сентября", "24 сентября"])
    for firm, ha in firm_rows:
        ws.append([firm, 1000, 500, 50.0, 0, ha])
    ws.append(["Итого", 2000, 1000, 50.0, 0, total])


def _book(tmp_path, crop_rows, firm_rows, total):
    wb = Workbook()
    ws = wb.active
    ws.title = "5.0 Уборка"
    _make_sheet(ws, crop_rows, firm_rows, total)
    path = tmp_path / "2. report_crop_works_compact_2026-09-25.xlsx"
    wb.save(path)
    return path


CROPS = [
    ("АФ А", "Рапс яровой", 300.0),
    ("АФ А", "Кукуруза, из них:", 20.0),
    ("АФ А", "Кукуруза на силос", 0),
    ("АФ А", "Кукуруза на зерно", 20.0),
    ("АФ Б", "Соя", 100.0),
    ("АФ Б", "Кукуруза, из них:", 30.0),
    ("АФ Б", "Кукуруза на зерно", 30.0),
]

PERF_HDR = [
    "Агрофирмы",
    "Плановая выработка, га/сутки",
    "Количество задействованной техники в т.ч. Прицепных",
    "Обработано за сутки, га",
]
GENERAL_HDR = [
    "№ п/п",
    "Культура",
    "План, га",
    "Факт общий, га",
    "Валовый сбор\nобщий, т",
    "Факт за сутки, га",
    "Валовый сбор\nза сутки, т",
]
R1_CROPS = [
    ("Рапс яровой", 1500.0),
    ("Соя", 200.03),
    ("Кукуруза, из них:", 103.72),
    ("Кукуруза на зерно", 103.72),
    ("Кукуруза на силос", 0),
]


def _report1_book(tmp_path, perf_firms, perf_total, crops, general_total, *, perf_as_text=False):
    wb = Workbook()
    ws = wb.active
    ws.title = "Выраб. — 5.0 Уборка"
    ws.append(PERF_HDR)
    fmt = (lambda v: f"{v:,.2f}".replace(",", " ").replace(".", ",")) if perf_as_text else (lambda v: v)
    for firm, ha in perf_firms:
        ws.append([firm, 25, 10, fmt(ha)])
    ws.append(["Итого", 25, 10, fmt(perf_total)])
    g = wb.create_sheet("Общий намолот")
    g.append(GENERAL_HDR)
    for i, (crop, ha) in enumerate(crops, 1):
        g.append([i, crop, 100, 50, 10, ha, 5])
    g.append(["", "Итого", 100, 50, 10, general_total, 5])
    path = tmp_path / "1. report_main_2026-09-25.xlsx"
    wb.save(path)
    return path


def test_reads_last_date_total_firms_and_crops_without_double_counting_corn(tmp_path):
    path = _book(tmp_path, CROPS, [("АФ А", 320.0), ("АФ Б", 130.0)], 450.0)
    data = read_report2_workbook(path)["5.0 Уборка"]
    assert data["date_label"] == "24 сентября"
    assert data["total"] == 450.0
    assert data["by_firm"] == {"АФ А": 320.0, "АФ Б": 130.0}
    assert data["by_crop"]["Кукуруза"] == 50.0
    assert sum(data["by_crop"].values()) == 450.0
    assert report2_xlsx_areas(path) == {"5.0 Уборка": 450.0}
    assert report2_xlsx_firm_areas(path)["5.0 Уборка"]["АФ Б"] == 130.0
    assert report2_xlsx_crop_areas(path)["5.0 Уборка"]["Соя"] == 100.0
    assert report2_xlsx_internal_issues(path) == {}


def test_internal_check_catches_culture_missing_from_total(tmp_path):
    path = _book(tmp_path, CROPS, [("АФ А", 300.0), ("АФ Б", 100.0)], 400.0)
    issues = report2_xlsx_internal_issues(path)["5.0 Уборка"]
    kinds = {i["kind"] for i in issues}
    assert "crops_vs_total" in kinds
    assert "firm_crops_vs_firm" in kinds
    crops = next(i for i in issues if i["kind"] == "crops_vs_total")
    assert crops["diff_ha"] == 50.0


def test_internal_check_ignores_rounding():
    data = {
        "total": 100.0,
        "by_firm": {"А": 50.0, "Б": 50.0},
        "by_crop": {"x": 99.9},
        "firm_from_crops": {"а": 50.0, "б": 49.9},
        "nonzero_rows": 4,
        "nonzero_rows_by_firm": {"а": 2, "б": 2},
    }
    assert internal_issues(data) == []


def _batch(file_totals_by_rid, group="5.0 Уборка"):
    op = {"total_ha": 2161.09, "area_today_by_firm": {"АФ А": 2161.09}}
    batch = {"reports": {rid: {"svodka": {"operations": {group: dict(op)}}} for rid in ("2", "11", "10")}}
    batch["cross_check"] = build_cross_check(batch)
    row = batch["cross_check"]["operations"][0]
    row["file_totals"] = dict(file_totals_by_rid)
    return batch, row


def test_status_stays_ok_before_file_check_and_becomes_diff_after():
    batch, row = _batch({"2": 2239.0, "11": 2161.09, "10": 2161.09})
    assert row["status"] == "OK"
    apply_file_checks(batch)
    assert row["status"] == "DIFF"
    assert row["file_status"] == "DIFF"
    assert row["file_max_diff_ha"] == round(2239.0 - 2161.09, 2)
    assert "file_totals" in row["status_reason"]
    assert batch["cross_check"]["summary"]["file_diff"] == 1


def test_matching_files_keep_status_ok():
    batch, row = _batch({"2": 2161.09, "11": 2161.09, "10": 2161.09})
    apply_file_checks(batch)
    assert row["status"] == "OK"
    assert row["file_status"] == "OK"
    assert "status_reason" not in row


def test_file_spread_below_threshold_is_not_diff():
    batch, row = _batch({"2": 2161.30, "11": 2161.09})
    apply_file_checks(batch)
    assert row["status"] == "OK"


def test_single_file_value_is_not_compared():
    batch, row = _batch({"2": 2239.0})
    apply_file_checks(batch)
    assert row["status"] == "OK"
    assert "file_status" not in row


def test_internal_issue_of_one_file_is_attached_but_does_not_flip_cross_check_status():
    batch, row = _batch({"2": 2161.09, "11": 2161.09})
    batch["reports"]["2"]["file_internal"] = {
        "5.0 Уборка": [{"kind": "crops_vs_total", "expected": 2161.09, "actual": 2239.0, "diff_ha": 77.91}],
    }
    apply_file_checks(batch)
    assert row["status"] == "OK"
    assert "status_reason" not in row
    assert row["file_internal"][0]["report"] == "2"
    assert batch["cross_check"]["file_internal"][0]["group_name"] == "5.0 Уборка"


def test_report11_xlsx_total_and_firms_are_read(tmp_path):
    import proverka.file_values as fv

    wb = Workbook()
    ws = wb.active
    ws.title = "Сводка"
    ws.append(["№ п/п", "Агрофирма", "План", "Убрано за сутки, га", "Комбайнов"])
    ws.append([1, "АФ А", 25, 300.0, 10])
    ws.append([2, "АФ Б", 25, 100.0, 5])
    ws.append([None, "Итого", 25, 400.0, 15])
    path = tmp_path / "11. report_harvest_2026-09-25.xlsx"
    wb.save(path)
    assert fv.operation_areas_from_path(path, "11") == {"5.0 Уборка": 400.0}
    assert fv.firm_areas_from_path(path, "11") == {"5.0 Уборка": {"АФ А": 300.0, "АФ Б": 100.0}}


def test_report1_perf_vs_general_reproduces_1733_vs_1803(tmp_path):
    path = _report1_book(
        tmp_path, [("АФ А", 1000.0), ("АФ Б", 733.72)], 1733.72, R1_CROPS, 1803.75,
    )
    data = read_report1_workbook(path)
    assert data["perf"]["5.0 Уборка"]["total"] == 1733.72
    assert data["general"]["total"] == 1803.75
    rows = report1_sheet_check_rows(data)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "DIFF"
    assert [i["kind"] for i in row["issues"]] == ["perf_vs_general"]
    assert row["issues"][0]["diff_ha"] == 70.03


def test_report1_consistent_sheets_are_ok(tmp_path):
    path = _report1_book(
        tmp_path, [("АФ А", 1000.0), ("АФ Б", 803.75)], 1803.75, R1_CROPS, 1803.75,
    )
    (row,) = report1_sheet_check_rows(read_report1_workbook(path))
    assert row["status"] == "OK" and row["issues"] == []


def test_build_sheet_checks_finds_saved_file_and_feeds_violations(tmp_path, monkeypatch):
    from datetime import date

    import shutil

    import proverka.file_values as fv
    from proverka.summary import compute_violations

    book = _report1_book(
        tmp_path, [("АФ А", 1000.0), ("АФ Б", 733.72)], 1733.72, R1_CROPS, 1803.75,
    )
    day = tmp_path / "uploads" / "2026-09-25"
    day.mkdir(parents=True)
    shutil.copy(book, day / book.name)
    monkeypatch.setattr(fv, "upload_dir", lambda: tmp_path / "uploads")

    checks = build_sheet_checks(date(2026, 9, 25), ["1", "11"])
    assert checks[0]["status"] == "DIFF"
    v = compute_violations([], [], sheet_checks=checks)
    assert v["sheet_consistency_violation_count"] == 1
    assert "1733.72" in v["violations"][0]["detail"]


def test_report2_sheet_checks_flag_missing_culture(tmp_path):
    path = _book(tmp_path, CROPS, [("АФ А", 300.0), ("АФ Б", 100.0)], 400.0)
    (row,) = report2_sheet_check_rows(path)
    assert row["report_id"] == "2" and row["status"] == "DIFF"


def test_cross_check_detail_for_file_only_diff_explains_the_files():
    from proverka.summary import _violation_detail_cross

    text = _violation_detail_cross({
        "display_name": "5.0 Уборка",
        "status_reason": ["file_totals"],
        "file_totals": {"2": 2239.0, "11": 2161.09},
        "file_max_diff_ha": 77.91,
    })
    assert "готовых файлах" in text and "2239.0" in text


def test_excel_export_has_sheet_with_checks():
    import openpyxl

    from proverka.export_excel import _sheet_sheet_checks

    wb = openpyxl.Workbook()
    _sheet_sheet_checks(wb, {
        "sheet_checks": [{
            "report_id": "1",
            "group_name": "5.0 Уборка",
            "display_name": "5.0 Уборка",
            "total_ha": 1733.72,
            "general_ha": 1803.75,
            "crops_ha": 1803.75,
            "max_diff_ha": 70.03,
            "status": "DIFF",
            "source": "xlsx",
            "issues": [{"label": "лист выработки ↔ «Общий намолот»", "expected": 1733.72, "actual": 1803.75}],
        }],
    })
    ws = wb["Листы сводки"]
    assert ws.cell(4, 3).value == 1733.72
    assert ws.cell(4, 5).value == 1803.75
    empty = openpyxl.Workbook()
    _sheet_sheet_checks(empty, {})
    assert "Листы сводки" not in empty.sheetnames
