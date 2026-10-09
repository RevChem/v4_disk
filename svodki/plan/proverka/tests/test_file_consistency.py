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


# --- Сломанный/пустой файл сводки 2 не должен выглядеть как «нечего проверять» ---

import pytest  # noqa: E402

from proverka import file_consistency as fc  # noqa: E402
from proverka.file_consistency import ReportFileError, read_report2_workbook_checked  # noqa: E402


def _good_book_with_extra_sheet(tmp_path, extra_title, extra_rows=()):
    path = _book(tmp_path, CROPS, [("АФ А", 320.0), ("АФ Б", 130.0)], 450.0)
    from openpyxl import load_workbook

    wb = load_workbook(path)
    ws = wb.create_sheet(extra_title)
    for row in extra_rows:
        ws.append(row)
    wb.save(path)
    return path


def test_corrupt_file_raises_instead_of_returning_empty(tmp_path):
    path = tmp_path / "2. report_crop_works_compact_2026-09-25.xlsx"
    path.write_bytes(b"this is not an xlsx")
    with pytest.raises(ReportFileError) as err:
        read_report2_workbook(path)
    assert path.name in str(err.value)
    assert "не открылся" in str(err.value)
    # все потребители идут через строгое чтение и тоже падают, а не отдают {}
    for fn in (report2_xlsx_areas, report2_xlsx_firm_areas, report2_xlsx_crop_areas,
               report2_xlsx_internal_issues):
        with pytest.raises(ReportFileError):
            fn(path)


def test_missing_file_raises(tmp_path):
    with pytest.raises(ReportFileError):
        read_report2_workbook(tmp_path / "nope.xlsx")


def test_workbook_without_operation_sheets_is_an_error_not_empty(tmp_path):
    wb = Workbook()
    wb.active.title = "Лист1"
    path = tmp_path / "2. report.xlsx"
    wb.save(path)
    with pytest.raises(ReportFileError) as err:
        read_report2_workbook(path)
    assert "нет ни одного листа операции" in str(err.value)


def test_operation_sheet_with_unrecognised_structure_is_an_error(tmp_path):
    path = _good_book_with_extra_sheet(tmp_path, "2.1 Боронование", [["что-то не то"]])
    with pytest.raises(ReportFileError) as err:
        read_report2_workbook(path)
    assert "2.1 Боронование" in str(err.value)
    assert "структура не распознана" in str(err.value)


def test_sheet_parse_exception_is_reported_with_sheet_name(tmp_path, monkeypatch):
    path = _book(tmp_path, CROPS, [("АФ А", 320.0), ("АФ Б", 130.0)], 450.0)

    def boom(ws):
        raise ValueError("битая ячейка")

    monkeypatch.setattr(fc, "read_report2_sheet", boom)
    with pytest.raises(ReportFileError) as err:
        read_report2_workbook(path)
    assert "5.0 Уборка" in str(err.value)
    assert "ValueError: битая ячейка" in str(err.value)
    assert err.value.problems[0]["scope"].startswith("лист")


def test_non_operation_sheets_are_still_ignored(tmp_path):
    path = _good_book_with_extra_sheet(tmp_path, "Служебный лист", [["любой текст"]])
    assert set(read_report2_workbook(path)) == {"5.0 Уборка"}


def test_checked_reader_keeps_good_sheets_and_lists_problems(tmp_path):
    path = _good_book_with_extra_sheet(tmp_path, "2.1 Боронование", [["что-то не то"]])
    data, problems = read_report2_workbook_checked(path)
    assert set(data) == {"5.0 Уборка"}
    assert len(problems) == 1 and "2.1 Боронование" in problems[0]["scope"]


def test_sheet_check_rows_keep_good_sheet_and_add_error_row_for_bad_one(tmp_path):
    path = _good_book_with_extra_sheet(tmp_path, "2.1 Боронование", [["что-то не то"]])
    rows = report2_sheet_check_rows(path)
    statuses = {r["group_name"]: r["status"] for r in rows}
    assert statuses["5.0 Уборка"] == "OK"
    bad = [r for r in rows if r["status"] == "ERROR"]
    assert len(bad) == 1 and "2.1 Боронование" in bad[0]["group_name"]
    assert bad[0]["issues"][0]["kind"] == "read_error"


def test_sheet_check_rows_for_corrupt_file_is_a_single_error_row(tmp_path):
    path = tmp_path / "2. report.xlsx"
    path.write_bytes(b"garbage")
    rows = report2_sheet_check_rows(path)
    assert [r["status"] for r in rows] == ["ERROR"]


def test_error_sheet_check_becomes_a_violation(tmp_path):
    from proverka.summary import compute_violations

    path = tmp_path / "2. report.xlsx"
    path.write_bytes(b"garbage")
    result = compute_violations([], [], sheet_checks=report2_sheet_check_rows(path))
    kinds = [(v["kind"], v["status"]) for v in result["violations"]]
    assert kinds == [("sheet_consistency", "ERROR")]
    assert result["sheet_consistency_violation_count"] == 1
    assert "не открылся" in result["violations"][0]["detail"]
    assert result["error_logs"]


def test_build_sheet_checks_reports_failure_as_error_row_not_just_log(tmp_path, monkeypatch):
    from proverka import file_values as fv

    path = tmp_path / "2. report.xlsx"
    path.write_bytes(b"garbage")
    monkeypatch.setattr(fv, "find_report_xlsx", lambda d, rid: path if rid == "2" else None)
    monkeypatch.setattr(fv, "find_report_file", lambda *a, **k: None)
    rows = build_sheet_checks("2026-09-25", ["1", "2"])
    assert [r["status"] for r in rows] == ["ERROR"]
    assert rows[0]["report_id"] == "2"


# --- Сводка 1: read_report1_workbook / read_report1_seeding_crops не глотают ошибки ---

from proverka.file_consistency import (  # noqa: E402
    read_report1_seeding_crops,
    read_report1_workbook_checked,
    report1_sheet_check_rows_from_path,
)

SEED_TITLE = "Посев — Сводно по всем агрофирмам"
SEED_HDR = ["№ п/п", "Культура", "План, га", "Факт за сутки, га"]


def _seeding_book(tmp_path, rows, *, header=True, title=True):
    wb = Workbook()
    ws = wb.active
    ws.title = "Итого (все АФ)"
    if title:
        ws.append([SEED_TITLE])
    if header:
        ws.append(SEED_HDR)
    for crop, ha in rows:
        ws.append([None, crop, 10, ha])
    ws.append([None, "Итого", 10, sum(h for _c, h in rows)])
    path = tmp_path / "5. report_seeding_2026-09-25.xlsx"
    wb.save(path)
    return path


def _corrupt(tmp_path, name="1. report_main_2026-09-25.xlsx"):
    path = tmp_path / name
    path.write_bytes(b"this is not an xlsx")
    return path


def test_seeding_reader_reads_valid_table(tmp_path):
    path = _seeding_book(tmp_path, [("Пшеница озимая", 1757.8), ("Рапс озимый", 3.38)])
    assert read_report1_seeding_crops(path) == {
        "4.0 Посев": {"Пшеница озимая": 1757.8, "Рапс озимый": 3.38}
    }


def test_seeding_reader_corrupt_file_raises(tmp_path):
    with pytest.raises(ReportFileError) as err:
        read_report1_seeding_crops(_corrupt(tmp_path))
    assert "не открылся" in str(err.value)


def test_seeding_reader_without_openpyxl_raises(tmp_path, monkeypatch):
    path = _seeding_book(tmp_path, [("Рапс озимый", 1.0)])
    monkeypatch.setattr(fc, "load_workbook", None)
    with pytest.raises(ReportFileError) as err:
        read_report1_seeding_crops(path)
    assert "openpyxl" in str(err.value)


def test_seeding_reader_title_without_header_raises(tmp_path):
    path = _seeding_book(tmp_path, [("Рапс озимый", 1.0)], header=False)
    with pytest.raises(ReportFileError) as err:
        read_report1_seeding_crops(path)
    assert "шапка не распознана" in str(err.value) or "нет шапки" in str(err.value)
    assert "Итого (все АФ)" in str(err.value)


def test_seeding_reader_table_absent_is_legitimately_empty(tmp_path):
    # в сводке 1 такой таблицы может не быть — это «нечего сравнивать», не ошибка
    path = _report1_book(tmp_path, [("АФ А", 1.0)], 1.0, R1_CROPS, 1.0)
    assert read_report1_seeding_crops(path) == {}


def test_report1_corrupt_file_raises_for_all_consumers(tmp_path):
    path = _corrupt(tmp_path)
    with pytest.raises(ReportFileError):
        read_report1_workbook(path)
    data, problems = read_report1_workbook_checked(path)
    assert data == {"perf": {}, "general": None}
    assert problems and "не открылся" in problems[0]["message"]


def test_report1_missing_file_raises(tmp_path):
    with pytest.raises(ReportFileError):
        read_report1_workbook(tmp_path / "nope.xlsx")


def test_report1_workbook_without_known_sheets_is_an_error(tmp_path):
    wb = Workbook()
    wb.active.title = "Лист1"
    path = tmp_path / "1. report.xlsx"
    wb.save(path)
    with pytest.raises(ReportFileError) as err:
        read_report1_workbook(path)
    assert "файл пуст или не тот" in str(err.value)


def test_report1_perf_sheet_with_unrecognised_structure_is_an_error(tmp_path):
    path = _report1_book(tmp_path, [("АФ А", 1.0)], 1.0, R1_CROPS, 1.0)
    from openpyxl import load_workbook

    wb = load_workbook(path)
    bad = wb.create_sheet("Выраб. — 2.1 Боронование")
    bad.append(["что-то не то"])
    wb.save(path)
    with pytest.raises(ReportFileError) as err:
        read_report1_workbook(path)
    assert "Выраб. — 2.1 Боронование" in str(err.value)
    assert "структура не распознана" in str(err.value)


def test_report1_general_sheet_without_total_is_an_error(tmp_path):
    path = _report1_book(tmp_path, [("АФ А", 1.0)], 1.0, R1_CROPS, 1.0)
    from openpyxl import load_workbook

    wb = load_workbook(path)
    g = wb["Общий намолот"]
    for row in g.iter_rows():
        for c in row:
            if c.value == "Итого":
                c.value = "—"
    wb.save(path)
    with pytest.raises(ReportFileError) as err:
        read_report1_workbook(path)
    assert "Общий намолот" in str(err.value)


def test_report1_parse_exception_is_reported_with_sheet_name(tmp_path, monkeypatch):
    path = _report1_book(tmp_path, [("АФ А", 1.0)], 1.0, R1_CROPS, 1.0)

    def boom(ws):
        raise ValueError("битая ячейка")

    monkeypatch.setattr(fc, "_read_perf_sheet", boom)
    with pytest.raises(ReportFileError) as err:
        read_report1_workbook(path)
    assert "Выраб. — 5.0 Уборка" in str(err.value)
    assert "ValueError: битая ячейка" in str(err.value)


def test_report1_unrelated_sheets_are_ignored(tmp_path):
    path = _report1_book(tmp_path, [("АФ А", 1.0)], 1.0, R1_CROPS, 1.0)
    from openpyxl import load_workbook

    wb = load_workbook(path)
    wb.create_sheet("Овощи").append(["любой текст"])
    wb.save(path)
    data = read_report1_workbook(path)
    assert set(data["perf"]) == {"5.0 Уборка"} and data["general"] is not None


def test_report1_rows_from_path_keep_good_sheets_and_add_error_row(tmp_path):
    path = _report1_book(tmp_path, [("АФ А", 1.0)], 1.0, R1_CROPS, 1.0)
    from openpyxl import load_workbook

    wb = load_workbook(path)
    wb.create_sheet("Выраб. — 2.1 Боронование").append(["что-то не то"])
    wb.save(path)
    rows = report1_sheet_check_rows_from_path(path)
    by_status = {r["status"] for r in rows}
    assert by_status == {"OK", "ERROR"} or by_status == {"DIFF", "ERROR"}
    (bad,) = [r for r in rows if r["status"] == "ERROR"]
    assert "Выраб. — 2.1 Боронование" in bad["group_name"]
    assert bad["report_id"] == "1" and bad["issues"][0]["kind"] == "read_error"


def test_build_sheet_checks_report1_corrupt_xlsx_is_error_and_pdf_fallback_kept(tmp_path, monkeypatch):
    from proverka import file_values as fv

    xlsx = _corrupt(tmp_path)
    pdf_row = {"report_id": "1", "group_name": "5.0 Уборка", "status": "OK", "issues": []}
    monkeypatch.setattr(fv, "find_report_xlsx", lambda d, rid: xlsx if rid == "1" else None)
    monkeypatch.setattr(fv, "find_report_file", lambda d, rid, ext: tmp_path / "x.pdf")
    monkeypatch.setattr(fc, "_pdf_report1_rows", lambda p: [pdf_row])
    rows = build_sheet_checks("2026-09-25", ["1"])
    assert [r["status"] for r in rows] == ["OK", "ERROR"]  # PDF как запасной путь, но ошибка Excel видна


def test_load_saved_areas_surfaces_report1_crop_read_error(tmp_path, monkeypatch):
    from datetime import date

    from proverka import file_values as fv

    bad = _corrupt(tmp_path)
    monkeypatch.setattr(fv, "_saved_report_files", lambda d: [bad])
    _ops, crops, _firms, errors = fv.load_saved_areas(date(2026, 9, 25))
    assert crops == {}
    assert any(e.startswith("культуры:") and "не открылся" in e for e in errors.get("1", []))


def test_report2_sheet_titled_vegetable_harvest_maps_to_cropwise_name(tmp_path):
    path = _book(tmp_path, CROPS, [("АФ А", 34.75)], 34.75)
    from openpyxl import load_workbook

    wb = load_workbook(path)
    wb.active.title = "5.4 Уборка овощей"
    wb.save(path)
    assert report2_xlsx_areas(path) == {"5.4 Уборка картофеля, лука": 34.75}


def test_report1_reads_perf_sheets_not_only_harvest(tmp_path):
    from proverka.file_values import operation_areas_from_path

    wb = Workbook()
    perf = wb.active
    perf.title = "Выраб. — 2.9 Дискование"
    perf.append(PERF_HDR)
    perf.append(["АФ А", 10, 2, 250.45])
    perf.append(["Итого", 10, 2, 250.45])
    spray = wb.create_sheet("Выраб. — 3.1 Опрыскивание")
    spray.append(PERF_HDR)
    spray.append(["АФ А", 10, 2, 100.0])
    spray.append(["Итого", 10, 2, 100.0])
    till = wb.create_sheet("Вспашка + Глубокорыхление")
    for title, total in (("2.4 Вспашка", 686.21), ("2.8 Глубокорыхление", 8.25)):
        till.append([title])
        till.append(PERF_HDR)
        till.append(["АФ А", 10, 2, total])
        till.append(["Итого", 10, 2, total])
        till.append([])
    long_name = "Выраб. — 1.1 Внесение минеральных удобрений"[:31]
    fert = wb.create_sheet(long_name)
    fert.append(PERF_HDR)
    fert.append(["АФ А", 10, 2, 12.0])
    fert.append(["Итого", 10, 2, 12.0])
    path = tmp_path / "1.xlsx"
    wb.save(path)
    areas = operation_areas_from_path(path, "1")
    assert areas["2.9 Дискование"] == 250.45
    assert areas["3.1 Опрыскивание"] == 100.0
    assert areas["2.4 Вспашка"] == 686.21
    assert areas["2.8 Глубокорыхление"] == 8.25
    assert areas["1.1 Внесение минеральных удобрений"] == 12.0


def test_crop_areas_for_cropwise_include_corn_silage_and_grain_children(tmp_path):
    """Для сравнения с Cropwise «Кукуруза на силос» берётся из файла, а не теряется как 0/нет."""
    crops = [
        ("АФ А", "Рапс яровой", 300.0),
        ("АФ А", "Кукуруза, из них:", 70.0),
        ("АФ А", "Кукуруза на силос", 50.0),
        ("АФ А", "Кукуруза на зерно", 20.0),
        ("АФ Б", "Соя", 100.0),
        ("АФ Б", "Кукуруза, из них:", 30.0),
        ("АФ Б", "Кукуруза на силос", 10.0),
        ("АФ Б", "Кукуруза на зерно", 20.0),
    ]
    path = _book(tmp_path, crops, [("АФ А", 370.0), ("АФ Б", 130.0)], 500.0)
    data = read_report2_workbook(path)["5.0 Уборка"]
    # внутренняя проверка суммы по-прежнему без двойного счёта
    assert data["by_crop"]["Кукуруза"] == 100.0
    assert "Кукуруза на силос" not in data["by_crop"]
    assert sum(data["by_crop"].values()) == 500.0
    assert report2_xlsx_internal_issues(path) == {}
    # а для сравнения с Cropwise силос и зерно видны по отдельности
    got = report2_xlsx_crop_areas(path)["5.0 Уборка"]
    assert got["Кукуруза на силос"] == 60.0
    assert got["Кукуруза на зерно"] == 40.0
    assert got["Кукуруза"] == 100.0
    assert got["Соя"] == 100.0


def test_corn_silage_kept_in_crop_areas_of_other_reports(tmp_path):
    """Сводки 1, 10, 11: «Кукуруза на силос» из «Общего намолота» не теряется при чтении файла."""
    from proverka.file_values import crop_areas_from_path

    crops = [
        ("Рапс яровой", 1500.0),
        ("Кукуруза, из них:", 103.72),
        ("Кукуруза на зерно", 53.72),
        ("Кукуруза на силос", 50.0),
    ]
    for rid, name in (
        ("1", "1. report_main_2026-09-25.xlsx"),
        ("10", "10. report_veg_2026-09-25.xlsx"),
        ("11", "11. report_harvest_2026-09-25.xlsx"),
    ):
        wb = Workbook()
        ws = wb.active
        ws.title = "Выраб. — 5.0 Уборка"
        ws.append(PERF_HDR)
        ws.append(["АФ А", 25, 10, 100.0])
        ws.append(["Итого", 25, 10, 100.0])
        g = wb.create_sheet("Общий намолот")
        g.append(GENERAL_HDR)
        for i, (crop, ha) in enumerate(crops, 1):
            g.append([i, crop, 100, 50, 10, ha, 5])
        g.append(["", "Итого", 100, 50, 10, 1653.72, 5])
        path = tmp_path / name
        wb.save(path)
        got = crop_areas_from_path(path, rid)["5.0 Уборка"]
        assert got["Кукуруза на силос"] == 50.0, rid
        assert got["Кукуруза на зерно"] == 53.72, rid
        assert "Кукуруза, из них:" not in got, rid
        assert not any("из них" in name.casefold() for name in got), rid
