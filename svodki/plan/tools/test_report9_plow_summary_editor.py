"""Свод вспашки 9-й сводки (лист «Итого», PDF стр. 14) слушает редактор планов.

«Учитывать» даёт культуре свою строку, в том числе «Пшенице озимой», которой нет
в коротком списке яровых. «Не учитывать» убирает строку и не кладёт гектары в «Иные».
"""
from __future__ import annotations

import importlib
import json

import plans
from utils.season._apply_plans_edits import apply_plans_edits_data

PLOW = "2.4 Вспашка"
WHEAT = "Пшеница озимая"
FIRM = "АФ Чистопольская"


def _fresh_plans_with(edits: dict):
    mod = importlib.reload(plans)
    apply_plans_edits_data(json.loads(json.dumps(edits)), mod.__dict__)
    return mod


def _report9(edits: dict):
    _fresh_plans_with(edits)
    r9 = importlib.import_module("9_main_fixed")
    return importlib.reload(r9)


def _by_crop(r9, wheat_fact: float = 100.0, wheat_today: float = 5.0):
    by = {firm: {} for firm in r9.FIRM_ORDER}
    by[FIRM][WHEAT] = {
        "season_plow": wheat_fact,
        "plow_today": wheat_today,
        "engine_plow": 0.0,
        "plow_machines_today": 0,
    }
    by[FIRM]["Многолетние травы 2026 года"] = {
        "season_plow": 22.24,
        "plow_today": 0.0,
        "engine_plow": 0.0,
        "plow_machines_today": 0,
    }
    return by


def _names(rows):
    return [row[0] for row in rows]


def teardown_module(_m):
    importlib.reload(plans)


def test_counted_winter_wheat_is_its_own_row():
    r9 = _report9({})
    field, _veg, note = r9._plow_all_firms_rows_field_and_veg(_by_crop(r9))
    names = _names(field)
    assert WHEAT in names
    assert "Рапс яровой" in names
    wheat = next(row for row in field if row[0] == WHEAT)
    assert wheat[1] > 0
    assert wheat[2] == 100.0
    assert wheat[3] == 5.0
    assert names.index("Рапс яровой") < names.index(WHEAT)
    assert names.index(WHEAT) < names.index("Иные культуры")
    assert "Пшеница" not in note
    assert "Многолетние травы" in note


def test_skip_winter_wheat_drops_row_and_hectares():
    r9 = _report9({"CROP_REPORT_MODE": {PLOW: {WHEAT: 2}}})
    field, veg, note = r9._plow_all_firms_rows_field_and_veg(_by_crop(r9))
    names = _names(field) + _names(veg)
    assert WHEAT not in names
    other = next(row for row in field if row[0] == "Иные культуры")
    assert other[2] == 22.24
    assert "Пшеница" not in note


def test_potato_is_in_the_same_total_as_field_crops():
    r9 = _report9({})
    by = _by_crop(r9, wheat_fact=0.0, wheat_today=0.0)
    by[FIRM]["Картофель"] = {
        "season_plow": 479.53,
        "plow_today": 7.82,
        "engine_plow": 0.0,
        "plow_machines_today": 0,
    }
    field, veg, _note = r9._plow_all_firms_rows_field_and_veg(by)
    assert veg == []
    names = _names(field)
    assert "Картофель" in names
    assert names.index("Картофель") < names.index("Иные культуры")
    potato = next(row for row in field if row[0] == "Картофель")
    assert potato[2] == 479.53
    assert potato[3] == 7.82


def test_other_mode_folds_winter_wheat_into_other_row():
    r9 = _report9({"CROP_REPORT_MODE": {PLOW: {WHEAT: 1}}})
    field, _veg, _note = r9._plow_all_firms_rows_field_and_veg(_by_crop(r9))
    assert WHEAT not in _names(field)
    other = next(row for row in field if row[0] == "Иные культуры")
    assert other[2] == 122.24
    assert other[1] > 0
