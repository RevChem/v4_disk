"""Учётная площадь: одно поле один раз среди сезонов операции."""
from __future__ import annotations

import sys
import types
from pathlib import Path

PLAN = Path(__file__).resolve().parents[2]
if str(PLAN) not in sys.path:
    sys.path.insert(0, str(PLAN))

def _real_libs_available() -> bool:
    """Заглушки нужны только там, где библиотек нет. Иначе они навсегда
    подменяли бы настоящие openpyxl/httpx/requests для соседних тестов."""
    import importlib

    for lib in ("httpx", "openpyxl", "requests", "urllib3"):
        try:
            importlib.import_module(lib)
        except ImportError:
            return False
    return True


if not _real_libs_available():
    for name in ("httpx", "openpyxl", "requests", "urllib3"):
        sys.modules.setdefault(name, types.ModuleType(name))
    openpyxl = sys.modules["openpyxl"]
    openpyxl.styles = types.ModuleType("openpyxl.styles")
    openpyxl.utils = types.ModuleType("openpyxl.utils")
    sys.modules["openpyxl.styles"] = openpyxl.styles
    sys.modules["openpyxl.utils"] = openpyxl.utils
    for attr in ("Font", "PatternFill", "Alignment", "Border", "Side"):
        setattr(openpyxl.styles, attr, object)
    openpyxl.utils.get_column_letter = lambda n: "A"
    requests = sys.modules["requests"]


    class _Session:
        def __init__(self) -> None:
            self.headers: dict = {}

        def mount(self, *_a, **_k) -> None:
            return None


    requests.Session = _Session
    requests.adapters = types.ModuleType("requests.adapters")
    sys.modules["requests.adapters"] = requests.adapters
    requests.adapters.HTTPAdapter = object
    urllib3 = sys.modules["urllib3"]
    urllib3.util = types.ModuleType("urllib3.util")
    urllib3.util.retry = types.ModuleType("urllib3.util.retry")
    sys.modules["urllib3.util"] = urllib3.util
    sys.modules["urllib3.util.retry"] = urllib3.util.retry
    urllib3.util.retry.Retry = object


def _history() -> dict:
    def item(fid: int, year: int, crop_id: int) -> dict:
        return {"field_id": fid, "year": year, "crop_id": crop_id, "active": True}

    return {
        2026: [item(1, 2026, 10), item(2, 2026, 10)],
        2027: [item(1, 2027, 20)],
    }


def test_two_years_do_not_double_the_field() -> None:
    from importlib import import_module

    m = import_module("2_main_ideal_compact")
    crops = {10: "Соя", 20: "Пшеница яровая"}
    chosen = m.field_crops_for_operation_years(_history(), [2026, 2027], crops)
    assert chosen[1] == "Соя"
    assert chosen[2] == "Соя"
    area = sum(100.0 for _fid in chosen)
    assert area == 200.0

    newer = m.field_crops_for_operation_years(
        _history(), [2026, 2027], crops, newest_year_wins=True,
    )
    assert newer[1] == "Пшеница яровая"
    assert sum(100.0 for _fid in newer) == 200.0


def test_each_season_has_its_own_accounted_crop() -> None:
    """Соя 2026 и озимый рапс 2027 на одном поле: учётная площадь стоит под обеими культурами."""
    from importlib import import_module

    m = import_module("2_main_ideal_compact")
    history = {
        2026: [{"field_id": 1, "year": 2026, "crop_id": 10, "active": True}],
        2027: [{"field_id": 1, "year": 2027, "crop_id": 30, "active": True}],
    }
    crops = {10: "Соя", 30: "Рапс озимый"}
    per_season = m.field_crops_per_season(history, [2026, 2027], crops)
    assert per_season == {1: ["Соя", "Рапс озимый"]}


def test_same_crop_in_two_seasons_is_not_doubled() -> None:
    from importlib import import_module

    m = import_module("2_main_ideal_compact")
    history = {
        2026: [{"field_id": 1, "year": 2026, "crop_id": 10, "active": True}],
        2027: [{"field_id": 1, "year": 2027, "crop_id": 10, "active": True}],
    }
    per_season = m.field_crops_per_season(history, [2026, 2027], {10: "Соя"})
    assert per_season == {1: ["Соя"]}


def test_other_year_is_not_this_operation() -> None:
    from importlib import import_module

    m = import_module("2_main_ideal_compact")
    chosen = m.field_crops_for_operation_years(
        _history(), [2027], {10: "Соя", 20: "Пшеница яровая"},
    )
    assert chosen == {1: "Пшеница яровая"}


def test_winter_crop_comes_from_the_later_season() -> None:
    from importlib import import_module

    m = import_module("2_main_ideal_compact")
    history = {
        2026: [{"field_id": 5, "year": 2026, "crop_id": 1, "active": True}],
        2027: [{"field_id": 5, "year": 2027, "crop_id": 1, "active": True}],
    }
    chosen = m.field_crops_for_operation_years(
        history, [2026, 2027], {1: "Пшеница озимая"},
    )
    assert chosen == {5: "Пшеница озимая"}
    assert len(chosen) == 1


if __name__ == "__main__":
    test_two_years_do_not_double_the_field()
    test_each_season_has_its_own_accounted_crop()
    test_same_crop_in_two_seasons_is_not_doubled()
    test_other_year_is_not_this_operation()
    test_winter_crop_comes_from_the_later_season()
    print("ok")
