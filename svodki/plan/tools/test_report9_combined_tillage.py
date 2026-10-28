"""Сводка 9: страница суммы вспашки и глубокорыхления.

Факт строки — вспашка и глубокорыхление за год урожая (сезон вспашки «2.4»).
Глубокорыхление прошлого сезона на эту страницу не входит.
Площадь — tillable_area полей культуры.
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


def test_unmapped_field_is_not_a_row_on_the_combined_page():
    """Гектары без культуры сезона не попадают на эталонную страницу."""
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
        deep_seasons={2027},
    )
    assert work[r9.PLOW_FIELD_OTHER_CROP]["fact"][firm] == 12.4
    view = r9.build_combined_tillage_view(work, {})
    assert view["wide"] == []
    assert view["field"] == []
    assert view["veg"] == []


def test_combined_page_uses_harvest_year_for_both_operations():
    """Страница суммы — год урожая. Глубокорыхление 2026 на неё не входит."""
    r9 = _report9()
    assert set(r9.PLOW_FACT_SEASONS) == {2027}
    assert set(r9.DEEP_FACT_SEASONS) == {2026, 2027}
    assert r9.SEASON_YEAR == 2027

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
    tasks = [
        task(1, 2026, plow_wt),
        task(2, 2027, plow_wt),
        task(3, 2026, deep_wt),
        task(4, 2027, deep_wt),
        task(5, 2028, plow_wt),
    ]
    mtfmi = {1: [item], 2: [item], 3: [item], 4: [item], 5: [item]}
    work = r9.accumulate_combined_tillage(
        tasks,
        mtfmi,
        today_task_ids=set(),
        cutoff=cutoff,
        field_to_firm={1: firm},
        field_to_crop={1: "Соя"},
        plow_seasons={r9.SEASON_YEAR},
        deep_seasons={r9.SEASON_YEAR},
    )
    # Вспашка 2027 и глубокорыхление 2027. Сезоны 2026 и 2028 — нет.
    assert work["Соя"]["fact"][firm] == 10


def test_combined_season_fact_matches_plow_all_firms_total():
    """Факт сезона листа суммы равен «Итого» вспашки, без овощей."""
    r9 = _report9()
    firm_a, firm_b = r9.FIRM_ORDER[0], r9.FIRM_ORDER[1]

    def slot(fact: float, today: float = 0.0) -> dict:
        return {"season_plow": fact, "plow_today": today}

    by_firm = {
        firm_a: {
            "Рапс озимый": slot(10000),
            "Рапс яровой": slot(2000, 10),
            "Соя": slot(500),
            "Многолетние травы": slot(30),
            "Картофель": slot(80, 4),
        },
        firm_b: {
            "Рапс озимый": slot(5000),
            "Пшеница озимая": slot(100),
        },
    }
    view = r9.combined_view_from_plow_summary(by_firm, {"Рапс озимый": 40000, "Соя": 1000})
    field_rows, veg_rows, _note = r9._plow_all_firms_rows_field_and_veg(by_firm)
    names = {row["crop"] for row in view["wide"]}
    assert "Рапс озимый" not in names
    assert "Пшеница озимая" not in names
    assert "Тритикале озимая" not in names
    assert "Картофель" not in names
    assert "Морковь" not in names
    assert "Свекла столовая" not in names
    assert [row["crop"] for row in view["wide"]] == [crop for crop, *_rest in field_rows]
    assert [row["crop"] for row in view["veg"]] == [crop for crop, *_rest in veg_rows]
    by_name = {row["crop"]: row for row in view["field"]}
    assert by_name["Рапс яровой"]["fact"] == 2000
    assert by_name["Иные культуры"]["fact"] == 30
    veg = {row["crop"]: row for row in view["veg"]}
    assert veg["Картофель"]["fact"] == 80
    plow_total = round(sum(row["fact"] for row in view["field"]), 2)
    details = {
        firm_a: {
            "deep_season_by_crop": {"Рапс яровой": 7.0, "Пар": 2.5, "Картофель": 9.0, "Пшеница озимая": 40.0},
            "deep_today_by_crop": {"Рапс яровой": 1.0},
            "deep_today": 1.0,
        },
    }
    with_deep = r9.combined_view_from_plow_summary(by_firm, {"Рапс озимый": 40000}, details)
    deep_rows, _note = r9._deep_all_firms_rows(details)
    deep_field = sum(
        fact for crop, _plan, fact, _today in deep_rows
        if crop not in r9.PLOW_VEGETABLE_CROPS
    )
    assert round(sum(row["fact"] for row in with_deep["field"]), 2) == round(plow_total + deep_field, 2)
    assert "Картофель" not in {row["crop"] for row in with_deep["field"]}
    assert "Пшеница озимая" not in {row["crop"] for row in with_deep["wide"]}
    rape = {row["crop"]: row for row in with_deep["field"]}["Рапс яровой"]
    assert rape["fact"] == 2007
    assert rape["fact_by_firm"][firm_a] == 2007
    veg_deep = {row["crop"]: row for row in with_deep["veg"]}
    assert veg_deep["Картофель"]["fact"] == 89


def test_first_sheet_firm_table_uses_plow_plan_and_both_facts():
    r9 = _report9()
    details = {}
    for i, firm in enumerate(r9.FIRM_ORDER):
        st = r9._empty_stats()
        st["season_plow"] = 100.0 + i
        st["season_deep"] = 10.0
        st["plow_today"] = 2.0
        st["deep_today"] = 0.5
        details[firm] = st
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    r9.write_tillage_combined_sheet(wb, details)
    ws = wb["Сводка"]
    header_row = next(
        r for r in range(1, ws.max_row + 1) if ws.cell(r, 1).value == "Агрофирма"
    )
    assert [ws.cell(header_row, c).value for c in range(1, 7)] == r9._FIRM_PLOW_DEEP_COLS
    total_plan = total_fact = 0.0
    for i, firm in enumerate(r9.FIRM_ORDER):
        row = header_row + 1 + i
        plan = round(float(r9._plow_season_plan(firm) or 0.0), 2)
        fact = round(110.0 + i, 2)
        assert ws.cell(row, 1).value == firm
        assert ws.cell(row, 2).value == plan
        assert ws.cell(row, 3).value == fact
        assert ws.cell(row, 4).value == round(plan - fact, 2)
        expect_share = (fact / plan) if plan else 0.0
        assert abs(ws.cell(row, 5).value - expect_share) < 1e-9
        assert ws.cell(row, 6).value == 2.5
        total_plan += plan
        total_fact += fact
    total_row = header_row + 1 + len(r9.FIRM_ORDER)
    assert ws.cell(total_row, 1).value == "Итого"
    assert ws.cell(total_row, 2).value == round(total_plan, 2)
    assert ws.cell(total_row, 3).value == round(total_fact, 2)
    assert ws.cell(total_row, 6).value == round(2.5 * len(r9.FIRM_ORDER), 2)
    assert ws.page_setup.fitToWidth == 1
    assert ws.page_setup.fitToHeight == 1


def teardown_module(_module):
    importlib.reload(plans)
