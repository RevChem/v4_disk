"""Разбор заданий work.json для вкладки Cropwise, без сети."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

PLAN = Path(__file__).resolve().parents[2]
if str(PLAN) not in sys.path:
    sys.path.insert(0, str(PLAN))

from proverka import cropwise_ask as ca  # noqa: E402

FIRMS = ["Агрофирма А", "Агрофирма Б"]
HINTS = ["3.1 Опрыскивание"]


def _body() -> dict:
    return {
        "regionColumns": [
            {"key": "name", "name": "Культура / Поле"},
            {"key": "f", "name": "Опрыскивание, га", "work_type_names": ["3.1 Опрыскивание"]},
        ],
        "regionRows": [
            {"name": "Агрофирма А", "f": "100", "children": [
                {"name": "Поле 1 #101 Трактор CLAAS", "f": "60"},
                {"name": "Поле 2 #102", "f": "40"},
            ]},
            {"name": "Агрофирма Б", "f": "20", "children": [
                {"name": "Поле 3 #103", "f": "20"},
            ]},
        ],
        "cropColumns": [
            {"key": "name", "name": "Культура / Поле"},
            {"key": "f", "name": "Опрыскивание, га", "work_type_names": ["3.1 Опрыскивание"]},
        ],
        "cropRows": [
            {"name": "Соя", "f": "100", "children": [
                {"name": "Агрофирма А", "f": "100", "children": [
                    {"name": "Поле 1 #101 Трактор CLAAS", "f": "60"},
                    {"name": "Поле 2 #102", "f": "40"},
                ]},
            ]},
            {"name": "Пшеница", "f": "20", "children": [
                {"name": "Агрофирма Б", "f": "20", "children": [
                    {"name": "Поле 3 #103", "f": "20"},
                ]},
            ]},
        ],
    }


def test_shift_window_includes_last_day() -> None:
    t0, t1 = ca.shift_window(date(2026, 9, 18), date(2026, 9, 24))
    assert t0 == "2026-09-18T06:00:00"
    assert t1 == "2026-09-25T06:00:00"


def test_tasks_carry_crop_and_label() -> None:
    rows = ca.tasks_from_body(_body(), FIRMS, HINTS)
    by_id = {r["task_id"]: r for r in rows}
    assert set(by_id) == {101, 102, 103}
    assert by_id[101]["ha"] == 60
    assert "CLAAS" in by_id[101]["label"]
    assert by_id[101]["crop"] == "Соя"
    assert by_id[103]["firm"] == "Агрофирма Б"
    assert "Пшеница" in by_id[103]["crop"]


def test_filter_by_firm_and_crop() -> None:
    rows = ca.tasks_from_body(_body(), FIRMS, HINTS)
    soy = ca.filter_tasks(rows, crop="соя")
    assert {r["task_id"] for r in soy} == {101, 102}
    assert sum(r["ha"] for r in soy) == 100
    one = ca.filter_tasks(rows, firm="Агрофирма Б", crop="пшеница")
    assert [r["task_id"] for r in one] == [103]
    assert one[0]["ha"] == 20


def test_format_tsv_keeps_total_when_truncated() -> None:
    rows = ca.filter_tasks(ca.tasks_from_body(_body(), FIRMS, HINTS))
    text, meta = ca.format_tsv(rows, header="# test", max_rows=1)
    assert meta["rows_total"] == 3
    assert meta["truncated"]
    assert "Итого: 3 заданий, 120.00 га" in text
    assert "#101" in text or "101" in text


def test_cards_fill_driver_machine_field_and_fuel() -> None:
    from proverka import cropwise_task_cards as cards

    built = cards.build_cards(
        [{
            "id": 101,
            "machine_id": 7,
            "implement_id": 8,
            "driver_id": 9,
            "fuel_consumption": 12.5,
            "fuel_consumption_on_road": 99,
        }],
        {101: [
            {"field_id": 1, "covered_area": 40},
            {"field_id": 2, "covered_area": 20},
        ]},
        {7: {"name": "CLAAS", "registration_number": "А123АА"}},
        {8: {"custom_name": "Сеялка 8м"}},
        {1: "Поле Север", 2: "Поле Юг"},
        {9: "Иванов Иван Иванович"},
    )
    assert built[101]["driver"] == "Иванов Иван Иванович"
    assert built[101]["machine"] == "CLAAS А123АА"
    assert built[101]["implement"] == "Сеялка 8м"
    assert "Поле Север (40.00 га)" in built[101]["field"]
    assert "Поле Юг (20.00 га)" in built[101]["field"]
    assert built[101]["fuel"] == "12.5 л"
    assert built[101]["fuel_l"] == 12.5


def test_fuel_level_is_not_consumption() -> None:
    from proverka.cropwise_task_cards import fuel_of, machine_label

    liters, text = fuel_of({"fuel_level": 40})
    assert liters is None
    assert text == "остаток 40%"
    liters, text = fuel_of({
        "fuel_consumption": None,
        "fuel_consumption_on_road": 4843.38,
        "start_fuel_level": 578.13,
        "end_fuel_level": 793.58,
    })
    assert liters is None
    assert text == ""
    liters, text = fuel_of({
        "fuel_consumption": 6262.2,
        "fuel_consumption_on_road": 4843.38,
        "fuel_consumption_per_ha": 9.6,
    })
    assert liters == 6262.2
    assert text == "6262.2 л"
    assert machine_label(
        {"name": "Трактор  CLAAS 7660АК", "registration_number": "7660АК"},
        1,
    ) == "Трактор CLAAS 7660АК"


def test_person_name_uses_fio_parts() -> None:
    from proverka.cropwise_task_cards import person_name

    assert person_name({
        "last_name": "Петров",
        "first_name": "Пётр",
        "middle_name": "Петрович",
        "username": "pp",
    }) == "Петров Пётр Петрович"
    assert person_name({"username": "Джабаров Т.С."}) == "Джабаров Т.С."


def test_format_tsv_rolls_up_cards() -> None:
    rows = ca.filter_tasks(ca.tasks_from_body(_body(), FIRMS, HINTS))
    rows[0].update({
        "driver": "Иванов Иван",
        "machine": "CLAAS А111",
        "implement": "Опрыскиватель",
        "field": "Поле 1 (60.00 га)",
        "field_items": [{"name": "Поле 1", "ha": 60}],
        "fuel": "10.0 л",
        "fuel_l": 10,
    })
    rows[1].update({
        "driver": "Иванов Иван",
        "machine": "CLAAS А111",
        "implement": "",
        "field": "Поле 2 (40.00 га)",
        "field_items": [{"name": "Поле 2", "ha": 40}],
        "fuel": "5.0 л",
        "fuel_l": 5,
    })
    rows[2].update({
        "driver": "",
        "machine": "",
        "implement": "",
        "field": "",
        "field_items": [],
        "fuel": "остаток 30%",
        "fuel_l": None,
    })
    text, _ = ca.format_tsv(rows, header="# test", max_rows=1)
    assert "Иванов Иван" in text
    assert "По исполнителям, га отчёта:" in text
    assert "Топливо: 15.0 л по 2 заданиям из 3" in text
    assert "По полям, га по карточке:" in text
    assert "только остаток бака" in text
