"""Сводка 14: скашивание входит в площадь только если культура — кукуруза."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import plans  # noqa: F401


def _report14():
    path = Path(__file__).resolve().parents[1] / "14.main.py"
    spec = importlib.util.spec_from_file_location("report14_mowing_filter", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mowing_keeps_corn_and_drops_grass_and_unknown():
    report = _report14()
    report._SILAGE_TASK_IDS.clear()
    report._SILAGE_TASK_IDS.add(84)
    rows = [
        {"machine_task_id": 84, "field_id": 25, "covered_area": 44.68},
        {"machine_task_id": 84, "field_id": 11, "covered_area": 12.0},
        {"machine_task_id": 84, "field_id": 99, "covered_area": 1.5},
        {"machine_task_id": 126, "field_id": 25, "covered_area": 8.0},
    ]
    kept = report.drop_non_corn_mowing(
        rows,
        {
            25: "Многолетние травы 2024 года",
            11: "Кукуруза на зерно",
        },
        {},
    )
    assert [(row["field_id"], row["covered_area"]) for row in kept] == [
        (11, 12.0),
        (25, 8.0),
    ]


def test_grain_harvest_of_corn_is_not_relabeled_by_a_mowing_plan():
    report = _report14()
    report._SILAGE_TASK_IDS.clear()
    report._SILAGE_TASK_IDS.add(84)
    rows = [
        {"machine_task_id": 118, "field_id": 11, "covered_area": 30.0},
    ]
    kept = report.drop_non_corn_mowing(rows, {11: "Кукуруза на зерно"}, {})
    assert kept == rows
