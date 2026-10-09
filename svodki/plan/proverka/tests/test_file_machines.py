"""Число машин читается из готовых .xlsx, сводки не пересчитываются."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import openpyxl

from proverka import file_values
from proverka.file_machines import machines_from_path
from proverka.machine_runner import run_machine_proverka_batch


def _summary(path: Path, header: list[str], rows: list[list]) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Сводка"
    ws.append(header)
    for row in rows:
        ws.append(row)
    wb.save(path)


def test_summary_sheet_machines(tmp_path: Path) -> None:
    path = tmp_path / "4.xlsx"
    _summary(
        path,
        ["Агрофирма", "Количество задействованной техники", "Обработано за сутки, га"],
        [["АФ Чистопольская", 5, 10], ["Итого", 5, 10]],
    )
    assert machines_from_path(path, "4") == {"2.1 Боронование": {"АФ Чистопольская": 5}}


def _report2_book(path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "2.1 Боронование"
    ws.append(["КОЛИЧЕСТВО ЗАДЕЙСТВОВАННЫХ АГРЕГАТОВ"])
    ws.append(["Агрофирма", "Собственных\nагрегатов", "Наёмных\nза тек. день",
               "4 октября", "5 октября", "8 октября"])
    ws.append(["АФ Чистопольская", 10, 0, 1, 2, 2])
    ws.append(["АгроРаздолье", 11, 0, 1, 2, 0])
    ws.append(["Итого", 21, 0, 2, 4, 2])
    ws.append([None])
    ws.append(["СУТОЧНАЯ ВЫРАБОТКА НА 1 АГРЕГАТ"])
    wb.save(path)


def test_report2_machines_take_report_date_column(tmp_path: Path) -> None:
    path = tmp_path / "2.xlsx"
    _report2_book(path)
    got = machines_from_path(path, "2", date(2026, 10, 5))
    assert got == {"2.1 Боронование": {"АФ Чистопольская": 2, "АгроРаздолье": 2}}


def test_report2_machines_default_to_last_column(tmp_path: Path) -> None:
    path = tmp_path / "2.xlsx"
    _report2_book(path)
    got = machines_from_path(path, "2", date(2026, 10, 8))
    assert got == {"2.1 Боронование": {"АФ Чистопольская": 2, "АгроРаздолье": 0}}
    assert machines_from_path(path, "2") == got


def test_batch_reads_files_and_reports_diff(tmp_path: Path, monkeypatch) -> None:
    day = tmp_path / "up" / "2026-09-25"
    day.mkdir(parents=True)
    _summary(
        day / "4. a.xlsx",
        ["Агрофирма", "Количество задействованной техники", "Обработано за сутки, га"],
        [["АФ Чистопольская", 5, 10], ["Итого", 5, 10]],
    )
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Выраб. — 2.1 Боронование"
    ws.append(["Агрофирмы", "Количество задействованной техники в т.ч. Прицепных", "Обработано за сутки, га"])
    ws.append(["АФ Чистопольская", 7, 10])
    ws.append(["Итого", 7, 10])
    wb.save(day / "1. b.xlsx")
    monkeypatch.setattr(file_values, "upload_dir", lambda: tmp_path / "up")

    res = run_machine_proverka_batch(date(2026, 9, 25), ["4"], tmp_path / "out")
    row = next(r for r in res["machine_cross_check_rows"] if r["group_name"] == "2.1 Боронование")
    assert row["status"] == "DIFF"
    assert row["totals"] == {"1": 7, "4": 5}


def test_missing_file_is_reported(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(file_values, "upload_dir", lambda: tmp_path / "up")
    res = run_machine_proverka_batch(date(2026, 9, 25), ["4"], tmp_path / "out")
    assert "4" in res["missing_file_report_ids"]
    assert res["machine_cross_check_rows"] == []
