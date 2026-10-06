"""Выработка исполнителя из store.sqlite: период, фирма, поле, гектары, ссылка."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PLAN = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PLAN))
sys.path.insert(0, str(_PLAN / "utils" / "shared"))

import driver_work as dw  # noqa: E402

_TZ = timezone(timedelta(hours=3))


def _ts(year: int, month: int, day: int, hour: int) -> float:
    return datetime(year, month, day, hour, tzinfo=_TZ).timestamp()


def _store(tmp: Path) -> None:
    con = sqlite3.connect(tmp / "store.sqlite")
    con.executescript(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY, wt INTEGER, end_ts REAL, season TEXT,
            data TEXT, raw BLOB, machine_id INTEGER);
        CREATE TABLE mtfmi_fact (
            rid INTEGER PRIMARY KEY, task_id INTEGER, field_id INTEGER,
            covered_area REAL, hourly_n INTEGER);
        CREATE TABLE mtfmi_daily (
            rid INTEGER, shift_date TEXT, ha REAL, PRIMARY KEY (rid, shift_date));
        CREATE TABLE mtaomi (
            id INTEGER PRIMARY KEY, task_id INTEGER, agro_operation_id INTEGER);
        """
    )
    ivan = json.dumps({"id": 7, "driver_id": 9, "work_type_id": 55})
    petr = json.dumps({"id": 8, "driver_id": 4, "work_type_id": 60})
    con.execute(
        "INSERT INTO tasks VALUES (7, 55, ?, '2026', ?, NULL, 15)",
        (_ts(2026, 9, 15, 18), ivan),
    )
    con.execute(
        "INSERT INTO tasks VALUES (8, 60, ?, '2026', ?, NULL, 16)",
        (_ts(2026, 9, 20, 12), petr),
    )
    # Без почасовых: конец 1 октября в 05:00 — это смена 30 сентября.
    con.execute(
        "INSERT INTO tasks VALUES (9, 55, ?, '2026', ?, NULL, 15)",
        (_ts(2026, 10, 1, 5), ivan),
    )
    con.execute("INSERT INTO mtfmi_fact VALUES (1, 7, 100, 10, 2)")
    con.execute("INSERT INTO mtfmi_fact VALUES (2, 8, 100, 3, 1)")
    con.execute("INSERT INTO mtfmi_fact VALUES (3, 9, 200, 4.5, 0)")
    con.execute("INSERT INTO mtfmi_daily VALUES (1, '2026-09-15', 6)")
    con.execute("INSERT INTO mtfmi_daily VALUES (1, '2026-09-16', 4)")
    con.execute("INSERT INTO mtfmi_daily VALUES (2, '2026-09-20', 3)")
    con.execute("INSERT INTO mtaomi VALUES (1, 7, 88)")
    con.commit()
    con.close()

    refs = sqlite3.connect(tmp / "refs.sqlite")
    refs.execute(
        "CREATE TABLE ref (key TEXT PRIMARY KEY, endpoint TEXT, params TEXT, "
        "fetched_at REAL, n INTEGER, rows BLOB)"
    )

    def put(endpoint: str, rows: list[dict]) -> None:
        blob = zlib.compress(json.dumps(rows).encode())
        refs.execute(
            "INSERT INTO ref VALUES (?, ?, '', 1, ?, ?)",
            (endpoint, endpoint, len(rows), blob),
        )

    put("fields", [
        {"id": 100, "name": "Север", "field_group_id": 20},
        {"id": 200, "name": "лп0115", "field_group_id": 21},
    ])
    put("field_groups", [
        {"id": 20, "group_folder_id": 3},
        {"id": 21, "group_folder_id": 99},
    ])
    put("group_folders", [{"id": 3, "name": "АФ Чистопольская"}, {"id": 99, "name": "Прочее"}])
    put("agro_operations", [{"id": 88, "name": "Подкормка селитры"}])
    refs.commit()
    refs.close()


def test_people_and_rows() -> None:
    tmp_path = Path(tempfile.mkdtemp())
    _store(tmp_path)
    people = dw.query_driver_work(tmp_path, datetime(2026, 9, 1).date(), datetime(2026, 9, 30).date())
    assert people["available"] is True
    by_id = {p["id"]: p for p in people["people"]}
    assert by_id[9]["ha"] == 14.5
    assert by_id[9]["tasks"] == 2
    assert by_id[4]["ha"] == 3.0

    named = dw.query_driver_work(
        tmp_path,
        datetime(2026, 9, 1).date(),
        datetime(2026, 9, 30).date(),
        driver_id=9,
        names={9: "Иванов Иван Иванович"},
        work_type_names={55: "Внесение селитры"},
    )
    assert named["driver"] == "Иванов Иван Иванович"
    assert named["ha_total"] == 14.5
    assert named["tasks"] == 2
    assert [(r["date"], r["field"], r["firm"], r["ha"]) for r in named["rows"]] == [
        ("2026-09-15", "Север", "АФ Чистопольская", 6.0),
        ("2026-09-16", "Север", "АФ Чистопольская", 4.0),
        ("2026-09-30", "лп0115", "Лениногорская Птицефабрика", 4.5),
    ]
    first = named["rows"][0]
    assert first["operation"] == "Внесение селитры"
    assert first["agro_operation"] == "Подкормка селитры (88)"
    assert first["task_url"] == "https://operations.cropwise.com/machines/15/tasks/7"
    assert named["rows"][2]["task_url"].endswith("/machines/15/tasks/9")


def test_missing_store() -> None:
    tmp_path = Path(tempfile.mkdtemp())
    out = dw.query_driver_work(tmp_path, datetime(2026, 9, 1).date(), datetime(2026, 9, 30).date())
    assert out["available"] is False
    assert out["people"] == []


def test_period_order() -> None:
    try:
        dw.query_driver_work(Path("/tmp"), datetime(2026, 9, 30).date(), datetime(2026, 9, 1).date())
    except ValueError as exc:
        assert "раньше" in str(exc)
    else:
        raise AssertionError("ожидалась ошибка периода")


if __name__ == "__main__":
    test_people_and_rows()
    test_missing_store()
    test_period_order()
    print("ok")
