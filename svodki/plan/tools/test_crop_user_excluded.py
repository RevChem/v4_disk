"""Правки «Редактора планов» (скрытие / «не считать») должны убирать гектары из итогов сводки 2."""
from __future__ import annotations

import json

import plans
from utils.season._apply_plans_edits import apply_plans_edits_data

PLOW = "2.4 Вспашка"
FIRM = "АФ Чистопольская"
WINTER = ("Рапс озимый", "Пшеница озимая")


def _fresh_plans_with(edits: dict):
    """Свежая копия модуля plans с наложенными правками (как в генерации)."""
    import importlib

    mod = importlib.reload(plans)
    apply_plans_edits_data(json.loads(json.dumps(edits)), mod.__dict__)
    return mod


def teardown_module(_m):
    import importlib

    importlib.reload(plans)


def test_baseline_winter_crops_counted():
    p = _fresh_plans_with({})
    for crop in WINTER:
        assert not p.is_crop_user_excluded(PLOW, FIRM, crop)
        assert p.plan_ha_for_report(PLOW, FIRM, crop) > 0


def test_report_mode_skip_excludes_base_plan_crop_instead_of_other():
    p = _fresh_plans_with({"CROP_REPORT_MODE": {PLOW: {c: 2 for c in WINTER}}})
    for crop in WINTER:
        assert p.is_crop_user_excluded(PLOW, FIRM, crop)
        assert p.plan_ha_for_report(PLOW, FIRM, crop) == 0.0


def test_rank_zero_excludes_for_all_firms():
    p = _fresh_plans_with({"CROP_DISPLAY_RANK": {PLOW: {c: 0 for c in WINTER}}})
    for crop in WINTER:
        assert p.is_crop_user_excluded(PLOW, FIRM, crop)
        assert p.is_crop_user_excluded(PLOW, "АгроРаздолье", crop)


def test_firm_hidden_is_per_firm():
    p = _fresh_plans_with({"CROP_FIRM_HIDDEN": {PLOW: {FIRM: {"Рапс озимый": 1}}}})
    assert p.is_crop_user_excluded(PLOW, FIRM, "Рапс озимый")
    assert not p.is_crop_user_excluded(PLOW, "АгроРаздолье", "Рапс озимый")
    assert not p.is_crop_user_excluded(PLOW, FIRM, "Пшеница озимая")


def test_out_of_plan_crop_still_goes_to_other_on_plow():
    """Культура вне плана — не решение пользователя: гектары остаются в «Иных»."""
    p = _fresh_plans_with({})
    assert p.crop_code_for_report(PLOW, "Нут") is None  # по умолчанию «не считать»
    assert not p.is_crop_user_excluded(PLOW, FIRM, "Нут")


def test_other_group_not_affected():
    p = _fresh_plans_with({"CROP_DISPLAY_RANK": {PLOW: {c: 0 for c in WINTER}}})
    assert not p.is_crop_user_excluded("2.9 Дискование", FIRM, "Рапс озимый")


def test_fingerprint_changes_with_editor_settings():
    a = _fresh_plans_with({}).crop_filter_fingerprint()
    b = _fresh_plans_with({"CROP_DISPLAY_RANK": {PLOW: {"Рапс озимый": 0}}}).crop_filter_fingerprint()
    c = _fresh_plans_with({"CROP_REPORT_MODE": {PLOW: {"Рапс озимый": 2}}}).crop_filter_fingerprint()
    d = _fresh_plans_with({"CROP_FIRM_HIDDEN": {PLOW: {FIRM: {"Рапс озимый": 1}}}}).crop_filter_fingerprint()
    assert len({a, b, c, d}) == 4
    assert a == _fresh_plans_with({}).crop_filter_fingerprint()


def test_disk_report_skips_only_excluded_crop():
    """Сводка 7: скрытый рапс выпадает, соя и «Зябь» остаются."""
    import importlib

    m7 = importlib.import_module("7_main__4_")
    _fresh_plans_with({"CROP_DISPLAY_RANK": {"2.9 Дискование": {"Рапс озимый": 0}}})
    crops = m7.DiskCrops(
        history_crop={11: "Рапс озимый", 12: "Соя"},
        by_year={1: {2027: "Зябь"}},
        field_to_crop={},
    )
    task = {"season": 2027}
    assert m7.disk_item_excluded(task, {"history_item_id": 11, "field_id": 1}, FIRM, crops)
    assert not m7.disk_item_excluded(task, {"history_item_id": 12, "field_id": 1}, FIRM, crops)
    assert m7.disk_item_crop(task, {"field_id": 1}, crops) == ""
    assert not m7.disk_item_excluded(task, {"field_id": 1}, FIRM, crops)


def test_proverka_applies_editor_rules_to_cropwise_side():
    from proverka.crop_compare import apply_editor_crop_rules, compare_svodka_cropwise_crops

    p = _fresh_plans_with({"CROP_DISPLAY_RANK": {PLOW: {"Рапс озимый": 0}}})
    cw = {
        "Рапс озимый": {FIRM: 100.0},
        "Соя": {FIRM: 50.0},
        "Нут": {FIRM: 7.0},  # вне плана → на вспашке «Иные культуры»
    }
    got = apply_editor_crop_rules(PLOW, cw)
    assert "Рапс озимый" not in got
    assert got["Соя"] == {FIRM: 50.0}
    assert got[p.OTHER_CROP_CODE] == {FIRM: 7.0}
    # вне вспашки/дискования культура вне плана просто не считается
    assert "Нут" not in apply_editor_crop_rules("4.0 Посев", cw)
    # сводки 4/5/6/8 имена не сворачивают: скрытие действует только в своей группе
    kept = apply_editor_crop_rules(PLOW, cw, remap=False)
    assert "Рапс озимый" not in kept
    assert kept["Нут"] == {FIRM: 7.0}
    assert kept["Соя"] == {FIRM: 50.0}
    # без группы — данные как есть
    assert apply_editor_crop_rules(None, cw) == cw

    sv = {"Соя": {FIRM: 50.0}, p.OTHER_CROP_CODE: {FIRM: 7.0}}
    rows = compare_svodka_cropwise_crops(sv, cw, group_name=PLOW)
    assert all(r["status"] == "OK" for r in rows), rows
    rows_raw = compare_svodka_cropwise_crops(sv, cw)
    assert any(r["status"] != "OK" for r in rows_raw)
