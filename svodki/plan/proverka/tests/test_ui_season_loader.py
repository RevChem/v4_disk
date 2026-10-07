"""Загрузчик work.json: чужой сезон не записывается, сессия возвращается."""
from __future__ import annotations

import sys
import types
from pathlib import Path

PLAN = Path(__file__).resolve().parents[2]
for extra in (PLAN, PLAN / "utils" / "season", PLAN / "utils" / "api"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

try:
    import httpx  # noqa: F401
except ImportError:  # заглушка только если библиотеки нет
    httpx = types.ModuleType("httpx")
    httpx.AsyncClient = object
    httpx.HTTPError = Exception
    sys.modules["httpx"] = httpx

from proverka.cropwise_crops import combine_loaded_seasons, svodka_counted_years  # noqa: E402
from proverka.cropwise_ui import WorkLoadJob, load_works_for_seasons  # noqa: E402
import proverka.cropwise_ui as ui  # noqa: E402


def _body(total: str) -> dict:
    return {
        "regionColumns": [
            {"key": "name", "name": "Поле"},
            {"key": "f", "name": "Факт, га", "work_type_names": ["3.1 Опрыскивание"]},
        ],
        "regionRows": [
            {"name": "Всего", "f": total},
            {"name": "Агрофирма А", "f": total},
        ],
        "cropRows": [],
        "cropColumns": [],
    }


def _install(switch, reads: list[int | None]) -> list[int]:
    calls: list[int] = []

    def set_year(web, csrf, year):
        calls.append(int(year))
        return switch(int(year))

    def read(web):
        if not reads:
            return None
        return reads.pop(0)

    ui.warm_web_session = lambda cookie: (object(), "csrf", [], 2026)
    ui.set_ui_session_year = set_year
    ui.read_ui_session_season = read
    ui.fetch_work_report = lambda *args, **kwargs: {
        "ok": True,
        "body": _body("100"),
        "ui_session_seasonValue": kwargs.get("ui_session_season"),
    }
    return calls


def _job() -> WorkLoadJob:
    return WorkLoadJob("3.1 Опрыскивание", "t0", "t1", [1], ("3.1 Опрыскивание",))


def test_failed_switch_does_not_store_foreign_body() -> None:
    calls = _install(lambda year: None, [2026, 2026, 2026])
    loaded = load_works_for_seasons("cookie", [_job()], years=[2026, 2027])
    slot = loaded["3.1 Опрыскивание"][2027]
    assert slot["load_status"] == "not_loaded"
    assert slot.get("body") is None
    assert calls[0] == 2027
    assert calls[-1] == 2026


def test_season_change_discards_batch() -> None:
    calls = _install(lambda year: year, [2026, 2026, 2027, 2026])
    loaded = load_works_for_seasons("cookie", [_job()], years=[2026, 2027])
    assert loaded["3.1 Опрыскивание"][2026]["load_status"] == "loaded"
    assert loaded["3.1 Опрыскивание"][2027]["load_status"] == "not_loaded"
    assert loaded["3.1 Опрыскивание"][2027].get("body") is None
    assert calls[-1] == 2026


def test_original_season_is_restored() -> None:
    calls = _install(lambda year: year, [2026, 2026, 2027, 2027])
    load_works_for_seasons("cookie", [_job()], years=[2026, 2027])
    assert calls == [2027, 2026]


def test_blank_is_not_replaced_and_foreign_key_is_not_summed() -> None:
    foreign = {
        "ok": True,
        "load_status": "loaded",
        "ui_session_seasonValue": 2026,
        "body": _body("100"),
    }
    combined = combine_loaded_seasons(
        {
            2026: {
                "ok": True,
                "load_status": "loaded",
                "ui_session_seasonValue": 2026,
                "body": _body("100"),
            },
            2027: foreign,
        },
        [2026, 2027],
        ["Агрофирма А"],
        ["3.1 Опрыскивание"],
        counted_years=None,
    )
    assert combined["compare_ha"] == 100.0
    assert combined["incomplete"] is True
    assert 2027 in combined["incomplete_years"]

    blank = combine_loaded_seasons(
        {2027: {"ok": True, "load_status": "empty", "ui_session_seasonValue": 2027, "body": {"reportBlank": True}}},
        [2027],
        ["Агрофирма А"],
        ["3.1 Опрыскивание"],
        counted_years=None,
    )
    assert blank["compare_ha"] == 0.0
    assert blank["incomplete"] is False
    assert blank["seasons"]["2027"]["status"] == "empty"


def test_ignored_season_is_a_warning_not_part_of_the_diff() -> None:
    assert svodka_counted_years(
        [2026, 2027], group_name="2.1 Боронование", season_rule="none", task_years=None,
    ) is None
    assert svodka_counted_years(
        [2026, 2027], group_name="4.0 Посев", season_rule="year_plus_one", task_years=[2027],
    ) == [2027]
    combined = combine_loaded_seasons(
        {
            2026: {
                "ok": True,
                "load_status": "loaded",
                "ui_session_seasonValue": 2026,
                "body": _body("40"),
            },
            2027: {
                "ok": True,
                "load_status": "loaded",
                "ui_session_seasonValue": 2027,
                "body": _body("10"),
            },
        },
        [2026, 2027],
        ["Агрофирма А"],
        ["3.1 Опрыскивание"],
        counted_years=[2027],
    )
    assert combined["compare_ha"] == 10.0
    assert combined["both_seasons_ha"] == 50.0
    assert combined["warnings"]
    assert "2026" in combined["warnings"][0]
