"""Учёт культур редактора планов: «Иные культуры» и «не считать»."""
from __future__ import annotations

import plans
from utils.season._apply_plans_edits import _rebuild_crop_report_mode


GROUP = "4.0 Посев"


def _set_modes(*, other: set[str] = frozenset(), skip: set[str] = frozenset()) -> None:
    plans.CROP_OTHER_SET = frozenset((GROUP, name) for name in other)
    plans.CROP_SKIP_REPORT_SET = frozenset((GROUP, name) for name in skip)


def test_bundle_renames_and_sums_plan():
    _set_modes(other={"Нут", "Горох"})
    plans.HARDCODED_PLAN.setdefault(GROUP, {}).setdefault("АФ А", {})
    firm = plans.HARDCODED_PLAN[GROUP]["АФ А"]
    saved = dict(firm)
    firm["Нут"] = 10
    firm["Горох"] = 5
    firm["Соя"] = 20
    try:
        assert plans.crop_code_for_report(GROUP, "Нут") == "Иные культуры"
        assert plans.crop_code_for_report(GROUP, "Соя") == "Соя"
        assert plans.plan_ha_for_report(GROUP, "АФ А", "Иные культуры") == 15
        assert plans.plan_ha_for_report(GROUP, "АФ А", "Нут") == 0
        assert plans.plan_ha_for_report(GROUP, "АФ А", "Соя") == 20
        folded = plans.collapse_crop_areas(GROUP, {"Нут": 3, "Горох": 1, "Соя": 8})
        assert folded == {"Иные культуры": 4, "Соя": 8}
        assert plans.is_crop_omitted(GROUP, "АФ А", "Нут") is False
    finally:
        firm.clear()
        firm.update(saved)
        _set_modes()


def test_skip_drops_area_and_hides_the_row():
    _set_modes(skip={"Залежь"})
    try:
        assert plans.crop_code_for_report(GROUP, "Залежь") is None
        assert plans.is_crop_omitted(GROUP, "АФ А", "Залежь") is True
        folded = plans.collapse_crop_areas(GROUP, {"Залежь": 12, "Соя": 2})
        assert folded == {"Соя": 2}
    finally:
        _set_modes()


def test_rebuild_splits_modes():
    g: dict = {
        "CROP_REPORT_MODE": {
            GROUP: {"Нут": 1, "Пар": 2, "Соя": 0},
        }
    }
    _rebuild_crop_report_mode(g)
    assert g["CROP_OTHER_SET"] == {(GROUP, "Нут")}
    assert g["CROP_SKIP_REPORT_SET"] == {(GROUP, "Пар")}
