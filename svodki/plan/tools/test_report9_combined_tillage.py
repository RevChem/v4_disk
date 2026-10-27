"""Сводка 9: страница суммы вспашки и глубокорыхления.

Факт строки — вспашка за сезоны «2.4» плюс глубокорыхление за сезоны «2.8»
из редактора планов. Площадь — tillable_area полей культуры.
Лист стоит перед разбивкой по машинам.
"""
from __future__ import annotations

import importlib
from datetime import datetime

import openpyxl

import plans


def _report9():
    return importlib.reload(importlib.import_module("9_main_fixed"))


def _dt(text: str, tz) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=tz)


def test_combined_page_sums_plow_and_deep_for_season_year_only():
    r9 = _report9()
    tz = r9.TZ_MSK
    firm_a, firm_b = r9.FIRM_ORDER[0], r9.FIRM_ORDER[1]
    cutoff = _dt("2026-10-26T06:00:00", tz)
    plow_wt = next(iter(r9.PLOW_WT_IDS))
    deep_wt = next(iter(r9.DEEP_WT_IDS))

    def task(tid: int, season: int, wt: int, end: str) -> dict:
        return {
            "id": tid,
            "season": season,
            "work_type_id": wt,
            "end_time": _dt(end, tz).isoformat(),
        }

    def item(field_id: int, end: str, ha: float) -> dict:
        return {
            "field_id": field_id,
            "covered_area_hourly": [[_dt(end, tz).isoformat(), ha]],
        }

    tasks = [
        task(1, 2027, plow_wt, "2026-09-01T12:00:00"),
        task(2, 2027, deep_wt, "2026-09-02T12:00:00"),
        task(3, 2026, plow_wt, "2026-09-03T12:00:00"),
        task(4, 2027, plow_wt, "2026-10-25T18:00:00"),
        task(5, 2027, plow_wt, "2026-08-01T12:00:00"),
    ]
    mtfmi = {
        1: [item(1, "2026-09-01T12:00:00", 10)],
        2: [item(1, "2026-09-02T12:00:00", 4)],
        3: [item(1, "2026-09-03T12:00:00", 50)],
        4: [item(1, "2026-10-25T18:00:00", 3)],
        5: [item(3, "2026-08-01T12:00:00", 8)],
    }
    work = r9.accumulate_combined_tillage(
        tasks,
        mtfmi,
        today_task_ids={4},
        cutoff=cutoff,
        field_to_firm={1: firm_a, 2: firm_a, 3: firm_b},
        field_to_crop={1: "Соя", 2: "Морковь", 3: "Пшеница озимая"},
        plow_seasons={2027},
        deep_seasons={2027},
    )
    area = r9.combined_area_by_crop(
        {1: "Соя", 2: "Морковь", 3: "Пшеница озимая"},
        {1: 100.0, 2: 20.0, 3: 50.0},
    )
    view = r9.build_combined_tillage_view(work, area)

    by_name = {row["crop"]: row for row in view["wide"]}
    assert by_name["Соя"]["fact"] == 17
    assert by_name["Соя"]["today"] == 3
    assert by_name["Соя"]["fact_by_firm"][firm_a] == 17
    assert firm_b not in by_name["Соя"]["fact_by_firm"]
    assert by_name["Пшеница озимая"]["fact"] == 8
    assert by_name["Морковь"]["area"] == 20
    assert by_name["Морковь"]["fact"] == 0
    assert [row["crop"] for row in view["wide"]] == ["Соя", "Пшеница озимая", "Морковь"]
    assert [row["crop"] for row in view["field"]] == ["Соя", "Пшеница озимая"]
    assert [row["crop"] for row in view["veg"]] == ["Морковь"]
    assert view["firms"] == [firm_a, firm_b]

    wb = openpyxl.Workbook()
    wb.active.title = "Сводка"
    assert r9.combined_season_label({2027}, {2027}) == "2027"
    assert r9.combined_season_label({2026, 2027}, {2027}) == "2026+2027"
    assert r9.write_combined_tillage_sheet(wb, view, season_label="2027") is True
    wb.create_sheet(firm_a[:28])
    assert wb.sheetnames == ["Сводка", r9.COMBINED_TILLAGE_SHEET, firm_a[:28]]

    ws = wb[r9.COMBINED_TILLAGE_SHEET]
    assert ws["A1"].value == r9.COMBINED_TILLAGE_TITLE
    assert ws["A2"].value == "Культура 2027 г."
    assert ws["B2"].value == "Площадь полей, га"
    assert ws["C2"].value == firm_a
    assert ws["D2"].value == firm_b
    assert ws["E2"].value == "Факт общий, га"
    assert ws["A3"].value == "Соя"
    assert ws["B3"].value == 100
    assert ws["C3"].value == 17
    assert ws["D3"].value is None
    assert ws["E3"].value == 17
    assert ws["A6"].value == "Итого"
    assert ws["B6"].value == 170
    assert ws["E6"].value == 25

    # Полевой блок: доля и сутки. Овощи ниже, без второй шапки-заголовка.
    assert ws["A8"].value == r9.COMBINED_TILLAGE_TITLE
    assert ws["A10"].value == "Соя"
    assert ws["C10"].value == 17
    assert ws["D10"].value == 83
    assert abs(ws["E10"].value - 0.17) < 1e-9
    assert ws["F10"].value == 3
    assert ws["F10"].number_format == "# ##0"
    carrot_row = next(
        r for r in range(1, ws.max_row + 1)
        if ws.cell(r, 1).value == "Морковь" and r > 9
    )
    assert ws.cell(carrot_row, 6).number_format == "# ##0.00"
    assert ws.cell(carrot_row, 3).value is None


def test_unmapped_field_stays_in_other_crops():
    r9 = _report9()
    tz = r9.TZ_MSK
    firm = r9.FIRM_ORDER[0]
    cutoff = _dt("2026-10-26T06:00:00", tz)
    end = _dt("2026-09-01T12:00:00", tz)
    work = r9.accumulate_combined_tillage(
        [{
            "id": 1,
            "season": 2027,
            "work_type_id": next(iter(r9.DEEP_WT_IDS)),
            "end_time": end.isoformat(),
        }],
        {1: [{"field_id": 9, "covered_area_hourly": [[end.isoformat(), 12.4]]}]},
        today_task_ids=set(),
        cutoff=cutoff,
        field_to_firm={9: firm},
        field_to_crop={},
        plow_seasons={2027},
        deep_seasons={2026, 2027},
    )
    view = r9.build_combined_tillage_view(work, {})
    assert [row["crop"] for row in view["wide"]] == ["Иные культуры"]
    assert view["wide"][0]["fact"] == 12.4
    assert view["field"][0]["crop"] == "Иные культуры"
    assert view["veg"] == []


def test_combined_page_uses_each_operation_editor_seasons():
    """Вспашка и глубокорыхление берут свои сезоны из редактора, не один общий."""
    r9 = _report9()
    assert set(r9.PLOW_FACT_SEASONS) == {2027}
    assert set(r9.DEEP_FACT_SEASONS) == {2026, 2027}
    assert r9.combined_season_label(r9.PLOW_FACT_SEASONS, r9.DEEP_FACT_SEASONS) == "2026+2027"

    tz = r9.TZ_MSK
    firm = r9.FIRM_ORDER[0]
    cutoff = _dt("2026-10-26T06:00:00", tz)
    plow_wt = next(iter(r9.PLOW_WT_IDS))
    deep_wt = next(iter(r9.DEEP_WT_IDS))

    def task(tid: int, season: int, wt: int) -> dict:
        return {
            "id": tid,
            "season": season,
            "work_type_id": wt,
            "end_time": _dt("2026-09-01T12:00:00", tz).isoformat(),
        }

    item = {"field_id": 1, "covered_area_hourly": [[_dt("2026-09-01T12:00:00", tz).isoformat(), 5]]}
    work = r9.accumulate_combined_tillage(
        [
            task(1, 2026, plow_wt),
            task(2, 2027, plow_wt),
            task(3, 2026, deep_wt),
            task(4, 2027, deep_wt),
            task(5, 2028, plow_wt),
        ],
        {1: [item], 2: [item], 3: [item], 4: [item], 5: [item]},
        today_task_ids=set(),
        cutoff=cutoff,
        field_to_firm={1: firm},
        field_to_crop={1: "Соя"},
        plow_seasons=set(r9.PLOW_FACT_SEASONS),
        deep_seasons=set(r9.DEEP_FACT_SEASONS),
    )
    # Вспашка только 2027, глубокорыхление 2026 и 2027. Вспашка 2026 и 2028 — нет.
    assert work["Соя"]["fact"][firm] == 15


def teardown_module(_module):
    importlib.reload(plans)
