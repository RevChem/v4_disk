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
HINTS = ["Опрыск"]


def _body() -> dict:
    return {
        "regionColumns": [
            {"key": "name", "name": "Культура / Поле"},
            {"key": "f", "name": "Опрыскивание, га"},
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
            {"key": "f", "name": "Опрыскивание, га"},
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
