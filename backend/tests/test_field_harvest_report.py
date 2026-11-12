"""Поле, гектары, сорт и физический валовый сбор."""
from datetime import date
from io import BytesIO

from openpyxl import load_workbook

from app.application.field_harvest_report import (
    FIELD_HEADERS,
    NO_VARIETY,
    VARIETY_HEADERS,
    FieldHarvestError,
    assemble,
    available_field_ids,
    field_firm,
    parse_firms,
    parse_year,
    sown_hectares,
    variety_summary,
    workbook,
)


CROPS = [
    {"id": 24, "name": "Рапс яровой"},
    {"id": 52, "name": "Пшеница яровая"},
    {"id": 74, "name": "Кукуруза на зерно"},
]


def test_example_field_uses_tillable_area_variety_and_physical_weight():
    fields = [
        {
            "id": 4355,
            "name": "АР-06-07",
            "tillable_area": 361.0,
            "legal_area": 360.46,
            "calculated_area": 360.4618,
        },
        {"id": 9, "name": "АР-архив", "tillable_area": 10, "archived": True},
    ]
    history = [
        {
            "field_id": 4355,
            "year": 2026,
            "crop_id": 74,
            "variety": "СИ ротанго",
            "harvested_weight": None,
            "marketable_weight": 12.5,
        },
        {"field_id": 9, "crop_id": 74, "variety": "Архивный", "harvested_weight": 5},
        {"field_id": 404, "crop_id": 74, "variety": "Нет в активе", "harvested_weight": 8},
    ]
    rows = assemble(fields, history, CROPS, available_ids={4355})
    assert rows == [
        {
            "field": "АР-06-07",
            "area_ha": 361.0,
            "crop": "Кукуруза на зерно",
            "variety": "СИ ротанго",
            "weight_t": 0.0,
            "yield_c_ha": None,
            "sown_ha": 0.0,
            "sowing_on": None,
            "harvest_on": None,
        }
    ]


def test_weight_is_summed_once_per_field_and_grouped_by_variety():
    fields = [
        {"id": 1, "name": "А-01", "tillable_area": 32},
        {"id": 2, "name": "Б-02", "tillable_area": 10.5},
        {"id": 3, "name": "В-03", "tillable_area": 100},
    ]
    history = [
        {"field_id": 1, "crop_id": 24, "variety": "Амулет", "harvested_weight": 20.5},
        {"field_id": 1, "crop_id": 24, "variety": "Амулет", "harvested_weight": 24.2},
        {"field_id": 2, "crop_id": 52, "variety": "", "harvested_weight": 3.125},
        {"field_id": 3, "crop_id": None, "variety": None, "harvested_weight": None},
    ]
    rows = assemble(fields, history, CROPS, sown_by_field={1: 30.5, 2: 10.5})
    assert [row["field"] for row in rows] == ["А-01", "Б-02"]
    assert rows[0]["area_ha"] == 32.0
    assert rows[0]["crop"] == "Рапс яровой"
    assert rows[0]["weight_t"] == 44.7
    assert rows[0]["yield_c_ha"] is None
    assert rows[0]["sown_ha"] == 30.5
    assert rows[1]["variety"] == ""
    assert rows[1]["yield_c_ha"] is None
    summary = variety_summary(rows)
    assert summary[0]["variety"] == "Амулет"
    assert summary[0]["area_ha"] == 32.0
    assert summary[0]["sown_ha"] == 30.5
    assert summary[0]["weight_t"] == 44.7
    assert summary[0]["yield_c_ha"] is None
    assert summary[-1]["variety"] == NO_VARIETY
    assert summary[-1]["area_ha"] == 10.5
    assert summary[-1]["weight_t"] == 3.125


def test_workbook_has_field_and_variety_sheets():
    rows = assemble(
        [{"id": 4355, "name": "АР-06-07", "tillable_area": 361}],
        [{"field_id": 4355, "crop_id": 74, "variety": "СИ ротанго", "harvested_weight": 0}],
        CROPS,
    )
    book = load_workbook(BytesIO(workbook(rows, variety_summary(rows))))
    fields = book["Поля"]
    varieties = book["По сортам"]
    assert [cell.value for cell in fields[1]] == list(FIELD_HEADERS)
    assert [cell.value for cell in fields[2]] == [
        "АР-06-07",
        361.0,
        "Кукуруза на зерно",
        "СИ ротанго",
        0.0,
        None,
        None,
        0.0,
        0.0,
        None,
    ]
    assert fields[3][0].value == "Итого"
    assert fields[3][1].value == 361.0
    assert fields[3][9].value is None
    assert [cell.value for cell in varieties[1]] == list(VARIETY_HEADERS)
    assert varieties[2][0].value == "СИ ротанго"
    assert varieties[3][4].value is None


def test_aa_06_04_uses_api_productivity_harvest_and_dates():
    rows = assemble(
        [{"id": 1868, "name": "АА-06-04", "tillable_area": 27}],
        [
            {
                "field_id": 1868,
                "crop_id": 47,
                "variety": "Сузука (suzuka)",
                "sowing_date": "2026-05-11",
                "harvesting_date": "2026-10-02",
                "harvested_weight": 42.66,
                "productivity": 15.8,
                "marketable_weight": 0,
            }
        ],
        [{"id": 47, "name": "Подсолнечник"}],
        available_ids={1868},
    )
    assert rows == [
        {
            "field": "АА-06-04",
            "area_ha": 27.0,
            "crop": "Подсолнечник",
            "variety": "Сузука (suzuka)",
            "sown_ha": 0.0,
            "sowing_on": date(2026, 5, 11),
            "harvest_on": date(2026, 10, 2),
            "weight_t": 42.66,
            "yield_c_ha": 15.8,
        }
    ]


def test_archive_follows_inventory_availability_and_sowing_is_completed_area():
    from datetime import date

    ids = available_field_ids(
        [
            {"historyable_id": 1, "historyable_type": "Field", "available": True,
             "event_start_at": "2020-01-01", "event_end_at": None},
            {"historyable_id": 2, "historyable_type": "Field", "available": False,
             "event_start_at": "2020-01-01", "event_end_at": None},
            {"historyable_id": 3, "historyable_type": "Field", "available": True,
             "event_start_at": "2020-01-01", "event_end_at": "2025-12-31"},
        ],
        2026,
        on=date(2026, 11, 12),
    )
    assert ids == {1}
    sown = sown_hectares([
        {"field_id": 4355, "work_type_id": 70, "status": "done", "completed_area": 351},
        {"field_id": 4355, "work_type_id": 70, "status": "done", "completed_area": 10},
        {"field_id": 4355, "work_type_id": 70, "status": "planned", "completed_area": 361},
        {"field_id": 8, "work_type_id": 55, "status": "done", "completed_area": 20},
    ])
    assert sown == {4355: 361.0}


def test_year_must_be_a_season():
    assert parse_year(2026) == 2026
    try:
        parse_year(1999)
    except FieldHarvestError as exc:
        assert "2000" in str(exc)
    else:
        raise AssertionError("ожидалась ошибка года")


def test_laishevskaya_belongs_to_chistopol_and_ravines_do_not():
    assert field_firm({"field_group_id": 11}) == "АФ Чистопольская"
    assert field_firm({"field_group_id": 42}) is None
    rows = assemble(
        [
            {"id": 1, "name": "ЛА-01-20 (2)", "tillable_area": 33.86, "field_group_id": 11},
            {"id": 2, "name": "АФ2-03-21/1", "tillable_area": 21.69, "field_group_id": 42},
            {"id": 3, "name": "АР-06-07", "tillable_area": 361, "field_group_id": 3},
        ],
        [
            {"field_id": 1, "crop_id": 74, "variety": "сорт"},
            {"field_id": 2, "crop_id": 74, "variety": "овраг"},
            {"field_id": 3, "crop_id": 74, "variety": "СИ ротанго"},
        ],
        CROPS,
        firms=parse_firms(["АФ Чистопольская"]),
    )
    assert [row["field"] for row in rows] == ["ЛА-01-20 (2)"]
