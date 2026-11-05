"""Поля с начала сезона и работы 2025/2026: те же правила, что у дневных сводок."""
from datetime import date

from app.legacy.field_season_summary import (
    _mix_material_rows,
    _normalize_firm_name,
    _operation_count,
    _restamp_field_yield,
    _rows_for_operations,
)
from app.legacy.works_summary import (
    UNDEFINED_CROP,
    _replace_year,
    _task_contributions,
    _work_type_groups,
    crop_of_mapping_item,
    effective_work_groups,
)


def test_work_groups_follow_plans_and_keep_shared_types():
    groups = effective_work_groups()
    names = [name for name, _ids in groups]
    mapping = _work_type_groups(groups)
    assert "3.2 Опрыскивание (овощи)" in names
    assert "4.0 Посев" in mapping[159]
    assert "4.0 Посев" in mapping[163]
    assert mapping[151] == ("3.2 Опрыскивание (овощи)",)
    assert "5.0 Уборка" in mapping[76]
    assert "5.4 Уборка картофеля, лука" in mapping[76]
    assert "5.0 Уборка" in mapping[75]
    assert "5.4 Уборка картофеля, лука" in mapping[75]


def test_leap_day_shifts_to_feb_28():
    assert _replace_year(date(2024, 2, 29), 2023) == date(2023, 2, 28)


def test_task_area_scales_to_official_and_enters_every_group():
    task = {
        "id": 1,
        "status": "done",
        "work_type_id": 76,
        "end_time": "2026-11-05T10:00:00+03:00",
        "covered_area": 8,
    }
    items = [
        {
            "field_id": 5,
            "history_item_id": 9,
            "covered_area_hourly": [["2026-11-05T08:00:00+03:00", 2]],
        }
    ]
    rows = _task_contributions(
        task,
        items,
        {5: "АФ Чистопольская"},
        {9: "Пшеница озимая"},
        {9: 5},
        {76: ("5.0 Уборка", "5.4 Уборка картофеля, лука")},
        2026,
        date(2026, 1, 1),
        date(2026, 11, 5),
    )
    assert len(rows) == 2
    assert {row["group"] for row in rows} == {"5.0 Уборка", "5.4 Уборка картофеля, лука"}
    assert {row["year"] for row in rows} == {2026}
    assert all(row["area"] == 8 for row in rows)
    assert all(row["crop"] == "Пшеница озимая" for row in rows)


def test_small_area_is_kept_and_missing_crop_stays_in_the_total():
    task = {
        "status": "done",
        "work_type_id": 55,
        "end_time": "2026-03-02T12:00:00+03:00",
        "covered_area": 0.2,
    }
    items = [{"field_id": 1, "covered_area": 0.2}]
    rows = _task_contributions(
        task,
        items,
        {1: "Актай"},
        {},
        {},
        {55: ("1.1 Внесение минеральных удобрений",)},
        2026,
        date(2026, 1, 1),
        date(2026, 3, 2),
    )
    assert len(rows) == 1
    assert rows[0]["area"] == 0.2
    assert rows[0]["crop"] == UNDEFINED_CROP


def test_hourly_outside_the_shift_does_not_fall_back_to_full_area():
    task = {
        "status": "done",
        "work_type_id": 55,
        "end_time": "2026-03-02T04:00:00+03:00",
        "covered_area": 20,
    }
    # Смена закрылась в 04:00, её конец — 06:00 этих суток. Час 07:00 уже следующий день.
    items = [
        {
            "field_id": 1,
            "history_item_id": 3,
            "covered_area": 20,
            "covered_area_hourly": [["2026-03-02T07:00:00+03:00", 20]],
        }
    ]
    rows = _task_contributions(
        task,
        items,
        {1: "Актай"},
        {3: "Соя"},
        {3: 1},
        {55: ("1.1 Внесение минеральных удобрений",)},
        2026,
        date(2026, 1, 1),
        date(2026, 3, 2),
    )
    assert rows == []


def test_skip_crop_and_foreign_history_do_not_borrow_the_field_crop():
    assert crop_of_mapping_item({"field_id": 1}, {}, {}) == UNDEFINED_CROP
    assert (
        crop_of_mapping_item(
            {"field_id": 1, "history_item_id": 4},
            {4: "Пшеница озимая"},
            {4: 9},
        )
        == UNDEFINED_CROP
    )
    task = {
        "status": "done",
        "work_type_id": 60,
        "end_time": "2026-04-01T12:00:00+03:00",
        "covered_area": 5,
    }
    items = [{"field_id": 1, "history_item_id": 4, "covered_area": 5}]
    assert (
        _task_contributions(
            task,
            items,
            {1: "Актай"},
            {4: "Залежь"},
            {4: 1},
            {60: ("2.9 Дискование",)},
            2026,
            date(2026, 1, 1),
            date(2026, 4, 1),
        )
        == []
    )


def test_field_yield_follows_start_date_and_zero_custom_area_stays_zero():
    ops = [
        {
            "id": 1,
            "field_id": 7,
            "history_item_id": 11,
            "planned_start_date": "2026-01-01",
            "actual_start_datetime": "2026-08-01T08:00:00+03:00",
            "harvested_weight": 1000,
            "completed_area": 10,
            "work_type_id": 6,
        },
        {
            "id": 2,
            "field_id": 7,
            "history_item_id": 11,
            "planned_start_date": "2026-07-01",
            "actual_start_datetime": "2026-06-01T08:00:00+03:00",
            "harvested_weight": 5000,
            "completed_area": 10,
            "work_type_id": 6,
            "application_mix_items": [
                {"applicable_type": "Chemical", "applicable_id": 3, "custom_fact_area": 0, "planned_rate": 1},
                {"applicable_type": "Chemical", "applicable_id": 4, "custom_fact_area": 0, "planned_rate": 2},
            ],
        },
    ]
    rows_by_field = _rows_for_operations(
        ops,
        field_name_map={7: "П-1"},
        field_area_map={7: 40},
        field_firm={7: "НурлатАгро"},
        work_type_map={6: "Уборка"},
        material_map={"Chemical": {3: "А", 4: "Б"}, "Fertilizer": {}, "Seed": {}},
        history_crop={11: "Соя"},
        history_field={11: 7},
    )
    rows = rows_by_field[7]
    assert _operation_count(rows) == 2
    assert len(rows) == 3
    assert all(row["yield_value"] == 5.0 for row in rows)
    mix = [row for row in rows if row["operation_id"] == 2]
    assert len(mix) == 2
    assert all(row["covered_area"] == 0.0 for row in mix)
    assert _normalize_firm_name("Нурлат Агро") == "НурлатАгро"


def test_mix_zero_area_is_not_replaced_by_completed_area():
    rows = _mix_material_rows(
        {
            "completed_area": 12,
            "application_mix_items": [{"applicable_type": "Seed", "applicable_id": 1, "custom_fact_area": 0}],
        },
        {"Seed": {1: "Семена"}, "Fertilizer": {}, "Chemical": {}},
    )
    assert rows[0]["covered_area"] == 0.0


def test_restamp_uses_the_earliest_positive_yield():
    rows = [
        {"date_start": "2026-08-01", "date_end": "", "op_yield": 1, "yield_value": 0},
        {"date_start": "2026-06-01", "date_end": "", "op_yield": 5, "yield_value": 0},
    ]
    _restamp_field_yield(rows)
    assert rows[0]["yield_value"] == 5.0
    assert rows[1]["yield_value"] == 5.0
