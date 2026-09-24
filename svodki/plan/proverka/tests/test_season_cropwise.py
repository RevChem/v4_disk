"""
Регрессия на сезоны Cropwise.

work.json отдаёт только сезон, выбранный в сессии Cropwise (переключить его
запросом нельзя — проверено на ?seasonValue= и на параметрах тела). Поэтому
при выборе «2026 и 2027» в редакторе планов второй сезон обязан приходить из
API v3, иначе опрыскивание считалось 2713.89 га вместо 6215.87 га.

Запуск:  python3 -m pytest proverka/tests -q
   или:  python3 proverka/tests/test_season_cropwise.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import time
from datetime import date
from pathlib import Path

PLAN = Path(__file__).resolve().parents[2]
for extra in (PLAN, PLAN / "utils" / "season"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


su = _load("season_unified_under_test", PLAN / "utils" / "season" / "season_unified.py")

# Сезоны опрыскивания из редактора планов (uploads/season_ranges.json).
SPRAY_YEARS = [2026, 2027]
REPORT_DATE = date(2026, 9, 18)

# Факт за смену 18.09.2026 из Cropwise: сезон 2027 виден в UI, 2026 — в API v3.
UI_2027_HA = 2713.89
API_2026_HA = 3501.98
BOTH_SEASONS_HA = 6215.87

API_BY_SEASON = {
    "2026": {"by_firm": {"Агрофирма А": API_2026_HA}, "total_ha": API_2026_HA, "tasks_count": 7},
    "2027": {"by_firm": {"Агрофирма А": UI_2027_HA}, "total_ha": UI_2027_HA, "tasks_count": 5},
}


def test_editor_selection_keeps_both_seasons() -> None:
    # Раньше year_plus_one оставляло только последний год: [2026, 2027] → [2027].
    assert su.merge_task_years_from_file(
        "year_plus_one", REPORT_DATE, SPRAY_YEARS, has_file_override=True,
    ) == [2026, 2027]


def test_rule_default_applies_without_editor_override() -> None:
    assert su.merge_task_years_from_file(
        "year_plus_one", REPORT_DATE, [], has_file_override=False,
    ) == [2027]


def _sc():
    """season_cropwise без тяжёлых зависимостей сводок (plans, httpx)."""
    try:
        from proverka import season_cropwise
    except ImportError as exc:  # pragma: no cover - нет API-окружения
        import pytest

        pytest.skip(f"season_cropwise недоступен: {exc}")
    return season_cropwise


def test_ui_session_season_is_accepted_when_selected() -> None:
    sc = _sc()
    # Сессия 2026 при выборе 2026+2027 — рабочий случай, а не блокировка.
    assert sc.ui_session_ok_for_years(2026, SPRAY_YEARS)
    assert sc.ui_session_ok_for_years(2027, SPRAY_YEARS)
    assert not sc.ui_session_ok_for_years(2025, SPRAY_YEARS)
    assert not sc.ui_session_ok_for_years(None, SPRAY_YEARS)

    blocked, _need = sc.ui_session_blocks_both_years(2026, "year_plus_one", SPRAY_YEARS)
    assert blocked is False


def test_seasons_sum_ui_session_plus_api_for_other_years() -> None:
    sc = _sc()
    seasons = sc.build_seasons_for_years(
        SPRAY_YEARS, 2027, UI_2027_HA, {"Агрофирма А": UI_2027_HA}, API_BY_SEASON,
    )
    assert seasons["2027"]["source"] == "work_json_session_season"
    assert seasons["2027"]["total_ha"] == UI_2027_HA
    assert seasons["2026"]["source"] == "api_v3_machine_task_season"
    assert seasons["2026"]["total_ha"] == API_2026_HA

    total, by_firm = sc.merge_seasons_for_task_years(seasons, SPRAY_YEARS)
    assert total == BOTH_SEASONS_HA
    assert by_firm["Агрофирма А"] == BOTH_SEASONS_HA


def test_editor_file_beats_report_level_single_task_year() -> None:
    """Proverka job кладёт task_season_years сводки 6 (часто [2027]); 3.1 — [2026,2027] из редактора."""
    store = {
        "calendar_seasons": {},
        "by_group": {"3.1 Опрыскивание": {"season_years": [2026, 2027]}},
    }
    original_loader = su.load_season_store
    su.load_season_store = lambda rd, reload=False: store  # noqa: ARG005
    su.clear_season_cache()
    try:
        cfg = su.resolve_group_season_config(
            "3.1 Опрыскивание",
            REPORT_DATE,
            run_override={"task_season_years": [2027], "by_group": store["by_group"]},
        )
        assert cfg["task_season_years"] == [2026, 2027]
    finally:
        su.load_season_store = original_loader
        su.clear_season_cache()


def test_report_check_uses_same_group_seasons_as_editor() -> None:
    """Сводка 6: список сезонов проверки = разбор групп, а не один год year_plus_one."""
    store = {
        "calendar_seasons": {},
        "by_group": {
            "3.1 Опрыскивание": {"season_years": [2026, 2027]},
            "3.2 Опрыскивание (овощи)": {"season_years": [2026]},
        },
    }
    original_loader = su.load_season_store
    su.load_season_store = lambda rd, reload=False: store  # noqa: ARG005
    su.clear_season_cache()
    try:
        cfg = su.resolve_report_season_config(
            ("3.1 Опрыскивание", "3.2 Опрыскивание (овощи)"),
            "year_plus_one",
            REPORT_DATE,
            {"task_season_years": [2027]},
        )
        assert cfg["by_group"]["3.1 Опрыскивание"]["task_season_years"] == [2026, 2027]
        assert cfg["by_group"]["3.2 Опрыскивание (овощи)"]["task_season_years"] == [2026]
        assert cfg["task_season_years"] == [2026, 2027]
    finally:
        su.load_season_store = original_loader
        su.clear_season_cache()


def test_season_store_reloads_when_editor_file_changes() -> None:
    """Сохранение в редакторе в том же процессе видно следующей проверке."""
    import json
    import tempfile

    saved = os.environ.get("SVODKI_SEASON_RANGES_PATH")
    su.clear_season_cache()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "season_ranges.json"
            os.environ["SVODKI_SEASON_RANGES_PATH"] = str(path)
            path.write_text(
                json.dumps({"by_group": {"3.1 Опрыскивание": {"season_years": [2027]}}}),
                encoding="utf-8",
            )
            first = su.load_season_store(REPORT_DATE)
            assert first["by_group"]["3.1 Опрыскивание"]["season_years"] == [2027]

            path.write_text(
                json.dumps(
                    {"by_group": {"3.1 Опрыскивание": {"season_years": [2026, 2027]}}}
                ),
                encoding="utf-8",
            )
            os.utime(path, ns=(time.time_ns(), time.time_ns()))
            second = su.load_season_store(REPORT_DATE)
            assert second["by_group"]["3.1 Опрыскивание"]["season_years"] == [2026, 2027]
            cfg = su.resolve_group_season_config("3.1 Опрыскивание", REPORT_DATE)
            assert cfg["task_season_years"] == [2026, 2027]
    finally:
        if saved is None:
            os.environ.pop("SVODKI_SEASON_RANGES_PATH", None)
        else:
            os.environ["SVODKI_SEASON_RANGES_PATH"] = saved
        su.clear_season_cache()


def test_task_season_years_set_for_spray_both_years_from_editor() -> None:
    store = {
        "calendar_seasons": {},
        "by_group": {"3.1 Опрыскивание": {"season_years": [2026, 2027]}},
    }
    original_loader = su.load_season_store
    su.load_season_store = lambda rd, reload=False: store  # noqa: ARG005
    su.clear_season_cache()
    try:
        allowed = su.task_season_years_set_for_group("3.1 Опрыскивание", REPORT_DATE)
        assert allowed == {"2026", "2027"}
    finally:
        su.load_season_store = original_loader
        su.clear_season_cache()


def test_file_override_beats_shared_spray_env_from_other_group() -> None:
    """3.1 и 3.2 — разные сезоны из редактора, даже если legacy spray env от 3.2."""
    store = {
        "calendar_seasons": {},
        "by_group": {
            "3.1 Опрыскивание": {"season_years": [2026, 2027]},
            "3.2 Опрыскивание (овощи)": {"season_years": [2026]},
        },
    }
    keys = [
        "SVODKI_SPRAY_SEASON_YEARS",
        "SVODKI_SPRAY_TASK_SEASON_YEARS",
        su.group_env_key("3.1 Опрыскивание", "HISTORY_YEARS"),
        su.group_env_key("3.2 Опрыскивание (овощи)", "HISTORY_YEARS"),
    ]
    saved = {k: os.environ.pop(k, None) for k in keys}
    try:
        os.environ["SVODKI_SPRAY_SEASON_YEARS"] = "2026"
        os.environ["SVODKI_SPRAY_TASK_SEASON_YEARS"] = "2026"
        os.environ[su.group_env_key("3.2 Опрыскивание (овощи)", "HISTORY_YEARS")] = "2026"
        os.environ[su.group_env_key("3.1 Опрыскивание", "HISTORY_YEARS")] = "2026,2027"
        os.environ[su.group_env_key("3.1 Опрыскивание", "TASK_SEASON_YEARS")] = "2026,2027"

        cfg31 = su.resolve_group_season_config(
            "3.1 Опрыскивание", REPORT_DATE, store=store, reload=True,
        )
        cfg32 = su.resolve_group_season_config(
            "3.2 Опрыскивание (овощи)", REPORT_DATE, store=store, reload=True,
        )
        assert cfg31["history_years"] == [2026, 2027]
        assert cfg31["task_season_years"] == [2026, 2027]
        assert cfg32["history_years"] == [2026]
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        su.clear_season_cache()


def test_inject_writes_per_group_env_not_shared_spray() -> None:
    store = {
        "calendar_seasons": {},
        "by_group": {
            "3.1 Опрыскивание": {"season_years": [2026, 2027]},
            "3.2 Опрыскивание (овощи)": {"season_years": [2026]},
        },
    }
    original_loader = su.load_season_store
    su.load_season_store = lambda rd, reload=False: store  # noqa: ARG005
    try:
        env: dict[str, str] = {}
        su.inject_season_range_env(env, REPORT_DATE)
        assert "SVODKI_SPRAY_SEASON_YEARS" not in env
        assert env.get(su.group_env_key("3.1 Опрыскивание", "HISTORY_YEARS")) == "2026,2027"
        assert env.get(su.group_env_key("3.2 Опрыскивание (овощи)", "HISTORY_YEARS")) == "2026"
    finally:
        su.load_season_store = original_loader
        su.clear_season_cache()


def test_plow_editor_keeps_both_years_for_deep_tillage() -> None:
    """2.8 не фильтрует задания в сводке, но план «2026 и 2027» нельзя схлопнуть в 2026."""
    store = {
        "calendar_seasons": {},
        "by_group": {
            "2.4 Вспашка": {"season_years": [2026, 2027]},
            "2.8 Глубокорыхление": {"season_years": [2026, 2027]},
        },
    }
    original_loader = su.load_season_store
    su.load_season_store = lambda rd, reload=False: store  # noqa: ARG005
    su.clear_season_cache()
    try:
        for group in ("2.4 Вспашка", "2.8 Глубокорыхление"):
            cfg = su.resolve_group_season_config(
                group,
                REPORT_DATE,
                run_override={"task_season_years": [2026]},
            )
            assert cfg["task_season_years"] == [2026, 2027], group
            assert cfg["history_years"] == [2026, 2027], group
    finally:
        su.load_season_store = original_loader
        su.clear_season_cache()


def test_single_season_still_equals_ui_total() -> None:
    sc = _sc()
    seasons = sc.build_seasons_for_years(
        [2027], 2027, UI_2027_HA, {"Агрофирма А": UI_2027_HA}, API_BY_SEASON,
    )
    total, _ = sc.merge_seasons_for_task_years(seasons, [2027])
    assert total == UI_2027_HA


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ok  {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"SKIP  {name}: {exc}")
    print("Провалено:" if failed else "Все тесты пройдены.", failed or "")
    sys.exit(1 if failed else 0)
