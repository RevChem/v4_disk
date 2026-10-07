"""Сезон Y = 01.07.(Y-1)…01.07.Y: переход через 1 января не должен сдвигать сезон на год вперёд."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

PLAN = Path(__file__).resolve().parents[2]
for extra in (PLAN, PLAN / "utils" / "season"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import season_calendar as cal  # noqa: E402
import season_unified as su  # noqa: E402


@pytest.mark.parametrize(
    ("d", "expected"),
    [
        (date(2026, 6, 30), 2026),
        (date(2026, 7, 1), 2027),
        (date(2026, 10, 7), 2027),
        (date(2026, 12, 31), 2027),
        (date(2027, 1, 1), 2027),
        (date(2027, 3, 15), 2027),
        (date(2027, 6, 30), 2027),
        (date(2027, 7, 1), 2028),
    ],
)
def test_season_year_for_date(d: date, expected: int) -> None:
    assert cal.season_year_for_date(d) == expected
    assert su.season_year_for_date(d) == expected


@pytest.mark.parametrize("rule", ["year_plus_one", "plow_year_plus_one", "both_years"])
@pytest.mark.parametrize("d", [date(2026, 10, 7), date(2027, 3, 15), date(2027, 6, 30)])
def test_history_years_are_stable_across_new_year(rule: str, d: date) -> None:
    assert su.history_years_from_rule(rule, d) == [2026, 2027]


def test_history_years_roll_over_in_july() -> None:
    assert su.history_years_from_rule("year_plus_one", date(2027, 7, 1)) == [2027, 2028]


def test_task_years_for_spring_2027() -> None:
    d = date(2027, 3, 15)
    assert su.task_years_from_rule("year_plus_one", d) == [2027]
    assert su.task_years_from_rule("plow_year_plus_one", d) == [2027]
    assert su.task_years_from_rule("both_years", d) == [2026, 2027]


def test_winter_spray_seasons_in_spring_2027() -> None:
    assert su.winter_spray_task_seasons(None, date(2027, 3, 15)) == {"2027"}
    assert su.winter_spray_task_seasons({"2026", "2027"}, date(2027, 3, 15)) == {"2027"}
    assert su.winter_spray_task_seasons(None, date(2026, 10, 7)) == {"2027"}


def test_group_config_spring_2027_and_autumn_2026() -> None:
    store = {"calendar_seasons": cal.default_calendar_seasons(date(2027, 3, 15)), "by_group": {}}
    spring = su.resolve_group_season_config("4.0 Посев", date(2027, 3, 15), store=store)
    assert spring["history_years"] == [2026, 2027]
    assert spring["crop_priority_year"] == 2027
    assert (spring["period_start"], spring["period_end"]) == ("2025-07-01", "2027-03-15")

    store = {"calendar_seasons": cal.default_calendar_seasons(date(2026, 10, 7)), "by_group": {}}
    autumn = su.resolve_group_season_config("3.1 Опрыскивание", date(2026, 10, 7), store=store)
    assert autumn["history_years"] == [2026, 2027]
    assert (autumn["period_start"], autumn["period_end"]) == ("2025-07-01", "2026-10-07")


def test_future_season_period_is_not_inverted() -> None:
    report = date(2026, 10, 7)
    start, end = cal.merged_period_for_years([2028], report)
    assert start <= end
    assert end == report


def test_merged_period_docstring_examples() -> None:
    assert cal.merged_period_for_years([2026, 2027], date(2026, 9, 1)) == (date(2025, 7, 1), date(2026, 9, 1))
    assert cal.merged_period_for_years([2027], date(2026, 9, 1)) == (date(2026, 7, 1), date(2026, 9, 1))


def test_corrupt_season_file_is_quarantined(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "season_ranges.json"
    path.write_text("{битый json", encoding="utf-8")
    monkeypatch.setenv("SVODKI_SEASON_RANGES_PATH", str(path))
    store = cal.load_season_ranges_store(date(2026, 10, 7))
    assert store["by_group"] == {}
    assert not path.exists()
    assert list(tmp_path.glob("season_ranges.json.corrupt-*"))


def test_ui_check_years() -> None:
    from proverka.cropwise_ui import ui_check_years

    assert ui_check_years(date(2026, 10, 7)) == [2026, 2027]
    assert ui_check_years(date(2027, 3, 15)) == [2026, 2027]
    assert ui_check_years(2026) == [2026, 2027]
