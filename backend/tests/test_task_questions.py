"""Задания всех операций: удобрения и уборка читаются одним срезом."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from app.adapters.outbound.proverka.service import _ensure_plan_path
from app.application.namolot_view import filter_record_rows
from app.application.task_slice import ask_rows, format_ask_table

_TZ = timezone(timedelta(hours=3))


def _ts(year, month, day, hour, minute=0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=_TZ).timestamp()


def _task(task_id, machine_id, wt, end, driver_id):
    return json.dumps(
        {
            "id": task_id,
            "driver_id": driver_id,
            "end_time": "ignored",
        },
        ensure_ascii=False,
    )


def _item(field_id, history_id, ha):
    return json.dumps(
        {"field_id": field_id, "history_item_id": history_id, "covered_area": ha},
        ensure_ascii=False,
    )


def _db():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, machine_id INTEGER, wt INTEGER, end_ts REAL, data TEXT)")
    con.execute("CREATE TABLE mtfmi (task_id INTEGER, data TEXT)")
    con.execute("CREATE TABLE history_crop (hid INTEGER PRIMARY KEY, crop TEXT)")
    con.executemany(
        "INSERT INTO history_crop (hid, crop) VALUES (?, ?)",
        [(1, "Соя"), (2, "Пшеница озимая")],
    )
    con.execute(
        "INSERT INTO tasks VALUES (10, 7, 55, ?, ?)",
        (_ts(2026, 3, 28, 17, 59), _task(10, 7, 55, None, 5)),
    )
    con.execute(
        "INSERT INTO mtfmi VALUES (10, ?)",
        (_item(100, 1, 12.5),),
    )
    con.execute(
        "INSERT INTO tasks VALUES (11, 8, 90, ?, ?)",
        (_ts(2026, 8, 1, 10), _task(11, 8, 90, None, 6)),
    )
    con.execute(
        "INSERT INTO mtfmi VALUES (11, ?)",
        (_item(100, 2, 4),),
    )
    con.execute(
        "INSERT INTO tasks VALUES (12, 7, 55, ?, ?)",
        (_ts(2025, 12, 1, 12), _task(12, 7, 55, None, 5)),
    )
    con.execute(
        "INSERT INTO mtfmi VALUES (12, ?)",
        (_item(100, 1, 9),),
    )
    return con


def test_area_rows_keep_fertilizer_and_harvest_inside_the_year():
    _ensure_plan_path()
    import task_questions

    con = _db()
    raw = task_questions.area_rows(con, _ts(2026, 1, 1, 0), _ts(2026, 10, 26, 6))
    assert {row["task_id"] for row in raw} == {10, 11}
    crops = {row["task_id"]: row["crop"] for row in raw}
    assert crops[10] == "Соя"
    assert crops[11] == "Пшеница озимая"
    assert {row["work_type_id"] for row in raw} == {55, 90}


def test_same_namolot_filters_narrow_every_operation():
    _ensure_plan_path()
    import task_questions

    raw = task_questions.area_rows(_db(), _ts(2026, 1, 1, 0), _ts(2026, 10, 26, 6))
    rows = task_questions.label_rows(
        raw,
        firms={100: "Актай"},
        plates={7: "А 2824 В", 8: "1111"},
        operations={55: "1.1 Внесение минеральных удобрений", 90: "5.0 Уборка"},
        field_names={100: "Поле 1"},
    )
    for row in rows:
        row["operator"] = "Пискеев Иван" if row["task_id"] == 10 else "Иванов"
        row["surname"] = row["operator"].split()[0]
    assert {row["operation"] for row in rows} == {
        "1.1 Внесение минеральных удобрений",
        "5.0 Уборка",
    }
    only_plate = filter_record_rows(rows, plate="2824")
    assert [row["task_id"] for row in only_plate] == [10]
    only_name = filter_record_rows(rows, surname="иванов")
    assert [row["task_id"] for row in only_name] == [11]
    spring = filter_record_rows(
        rows,
        date_from="2026-03-28",
        time_from="06:00",
        date_to="2026-03-29",
        time_to="05:59",
    )
    assert [row["operation"] for row in spring] == ["1.1 Внесение минеральных удобрений"]
    spraying = filter_record_rows(rows, operation="опрыскивание")
    assert spraying == []
    harvest = filter_record_rows(rows, operation="уборка")
    assert [row["task_id"] for row in harvest] == [11]
    fertilizer = filter_record_rows(rows, operation="минеральных")
    assert [row["task_id"] for row in fertilizer] == [10]


def test_ask_table_totals_cover_hidden_operations():
    rows = [
        {"at": "2026-03-28T12:00:00", "firm": "Актай", "crop": "Соя", "operation": "1.1 Внесение минеральных удобрений", "plate": "1", "operator": "Иванов", "field": "П", "ha": float(index)}
        for index in range(5)
    ]
    text, meta = format_ask_table(rows, max_rows=2)
    assert meta["rows_total"] == 5
    assert meta["ha_total"] == 10.0
    assert "По операциям:" in text
    assert "Внесение минеральных удобрений" in text
    empty = ask_rows([], "сколько га?", use_llm=True)
    assert empty["llm_used"] is False
    assert empty["answer"] == "По заданному фильтру заданий нет."
