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


def test_explicit_other_plan_is_added_to_folded_crops():
    _set_modes(other={"Нут"})
    plans.HARDCODED_PLAN.setdefault(GROUP, {}).setdefault("АФ А", {})
    firm = plans.HARDCODED_PLAN[GROUP]["АФ А"]
    saved = dict(firm)
    firm["Нут"] = 10
    firm["Иные культуры"] = 7
    try:
        assert plans.plan_ha_for_report(GROUP, "АФ А", "Иные культуры") == 17
        folded = plans.collapse_crop_areas(GROUP, {"Нут": 4, "Иные культуры": 7, "Соя": 1})
        assert folded["Иные культуры"] == 11
        assert folded["Соя"] == 1
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


def test_svodka2_labels_other_crops_as_inye_kultury():
    import importlib

    report = importlib.import_module("2_main_ideal_compact")
    _set_modes(other={"Нут"})
    try:
        assert report._accounted_crop_label(GROUP, "Нут", "АФ А") == "Иные культуры"
        assert report._accounted_crop_label(GROUP, "Соя", "АФ А") == "Соя"
        assert report._harvest_crop_excluded("АФ Чистопольская", "Иные культуры") is False
        assert report._harvest_crop_excluded("АФ Чистопольская", "Иные") is True
    finally:
        _set_modes()


def test_missing_catalog_crop_is_skipped_until_marked_counted():
    plans.sync_crop_report_sets()
    group = "2.4 Вспашка"
    crop = "Введение в оборот 2027 год"
    saved_mode = plans.CROP_REPORT_MODE[group][crop]
    firms = plans.HARDCODED_PLAN[group]
    created_firm = "АФ А" not in firms
    firm = firms.setdefault("АФ А", {})
    try:
        assert crop in plans.ALL_REPORT_CROPS
        assert crop not in plans._crops_present_in_group(group)
        assert plans.crop_code_for_report(group, crop) is None
        assert plans.crop_code_for_report(group, "Соя") == "Соя"
        assert plans.crop_code_for_report("5.0 Уборка", "Соя") == "Соя"
        assert plans.crop_code_for_report("5.0 Уборка", "Подсолнечник") == "Подсолнечник"
        assert plans.crop_code_for_report("5.0 Уборка", "Кукуруза на зерно") == "Кукуруза на зерно"
        assert plans.crop_code_for_report("5.0 Уборка", "Рапс яровой") == "Рапс яровой"
        assert plans.crop_code_for_report("5.4 Уборка картофеля, лука", "Картофель") == "Картофель"

        plans.CROP_REPORT_MODE[group][crop] = 0
        firm[crop] = 12
        plans.sync_crop_report_sets()
        assert plans.crop_code_for_report(group, crop) == crop
        assert plans.plan_ha_for_report(group, "АФ А", crop) == 12
        assert plans.is_crop_omitted(group, "АФ А", crop) is False
    finally:
        plans.CROP_REPORT_MODE[group][crop] = saved_mode
        firm.pop(crop, None)
        if created_firm:
            plans.HARDCODED_PLAN[group].pop("АФ А", None)
        plans.sync_crop_report_sets()


def test_split_layout_keeps_other_crops_as_their_own_rows():
    _set_modes(other={"Нут", "Горох"})
    plans.HARDCODED_PLAN.setdefault(GROUP, {}).setdefault("АФ А", {})
    firm = plans.HARDCODED_PLAN[GROUP]["АФ А"]
    saved = dict(firm)
    firm["Нут"] = 10
    firm["Горох"] = 5
    firm["Иные культуры"] = 7
    plans.OTHER_CROPS_LAYOUT[GROUP] = 1
    try:
        assert plans.other_crops_as_rows(GROUP) is True
        assert plans.crop_code_for_report(GROUP, "Нут") == "Нут"
        assert plans.crop_code_for_report(GROUP, "Горох") == "Горох"
        assert plans.plan_ha_for_report(GROUP, "АФ А", "Нут") == 10
        assert plans.plan_ha_for_report(GROUP, "АФ А", "Горох") == 5
        assert plans.plan_ha_for_report(GROUP, "АФ А", "Иные культуры") == 7
        folded = plans.collapse_crop_areas(GROUP, {"Нут": 3, "Горох": 1, "Иные культуры": 7})
        assert folded == {"Нут": 3, "Горох": 1, "Иные культуры": 7}
    finally:
        firm.clear()
        firm.update(saved)
        plans.OTHER_CROPS_LAYOUT.pop(GROUP, None)
        _set_modes()
