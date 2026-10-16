"""Кукуруза в разрезе культур Cropwise: скашивание (6.3) — силос, уборка — зерно."""
from __future__ import annotations

import sys
from pathlib import Path

PLAN = Path(__file__).resolve().parents[2]
for extra in (PLAN, PLAN / "utils" / "season"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from proverka import crop_compare as cc  # noqa: E402

FIRMS = ["АФ А", "АФ Б"]
HARVEST = "5.3 Уборка кукурузы на зерно"
MOWING = "6.3 Скашивание с измельчением кукурузы"


def _body() -> dict:
    cols = [
        {"key": "name", "name": "Культура"},
        {"key": "harvest", "name": "Уборка, га", "work_type_names": [HARVEST]},
        {"key": "fodder", "name": "Кормозаготовка, га", "work_type_names": [MOWING]},
    ]
    rows = [
        {"name": "Всего", "harvest": ["300"], "fodder": ["80"]},
        {
            "name": "Кукуруза на зерно",
            "harvest": ["300"],
            "fodder": ["80"],
            "children": [
                {"name": "АФ А", "harvest": ["200"], "fodder": ["50"]},
                {"name": "АФ Б", "harvest": ["100"], "fodder": ["30"]},
            ],
        },
    ]
    return {"cropColumns": cols, "cropRows": rows, "regionColumns": [], "regionRows": []}


def test_mowing_goes_to_silage_and_harvest_stays_grain() -> None:
    got = cc.cropwise_area_by_firm_crop(_body(), FIRMS, [HARVEST, MOWING])
    assert got["Кукуруза на зерно"] == {"АФ А": 200.0, "АФ Б": 100.0}
    assert got["Кукуруза на силос"] == {"АФ А": 50.0, "АФ Б": 30.0}


def test_without_mowing_task_nothing_is_renamed() -> None:
    got = cc.cropwise_area_by_firm_crop(_body(), FIRMS, [HARVEST])
    assert got == {"Кукуруза на зерно": {"АФ А": 200.0, "АФ Б": 100.0}}


def test_only_mowing_task_is_silage() -> None:
    got = cc.cropwise_area_by_firm_crop(_body(), FIRMS, [MOWING])
    assert got == {"Кукуруза на силос": {"АФ А": 50.0, "АФ Б": 30.0}}


def test_is_corn_mowing_task() -> None:
    assert cc.is_corn_mowing_task(MOWING)
    assert cc.is_corn_mowing_task("6.3 скашивание с измельчением кукурузы")
    assert not cc.is_corn_mowing_task("6.1 Скашивание с измельчением трав")
    assert not cc.is_corn_mowing_task(HARVEST)
