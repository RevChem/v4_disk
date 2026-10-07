"""season_context не должен подменять сезоны сводок 1/2 общим стартом уборки."""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

PLAN = Path(__file__).resolve().parents[2]
for extra in (PLAN, PLAN / "utils" / "season"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from season_unified import clear_season_cache, group_env_key, resolve_group_season_config  # noqa: E402


def _restore(saved: dict[str, str | None]) -> None:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_report2_ignores_harvest_season_and_keeps_manual_start(tmp_path) -> None:
    from proverka.reports_config import REPORTS
    from proverka.season_runtime import season_context

    keys = (
        "SVODKI_UPLOAD_DIR",
        "UPLOAD_DIR",
        "SVODKI_SEASON_RANGES_PATH",
        "SVODKI_SEASON_START_DATE",
        "SEASON_START_DATE",
        "SEASON_YEAR",
        "SVODKI_SEASON_YEARS",
        "SEASON_TASK_YEAR_STR",
        "SVODKI_PIPELINE",
        "SVODKI_REPORT2_PIPELINE",
    )
    saved = {key: os.environ.get(key) for key in keys}
    os.environ["SVODKI_UPLOAD_DIR"] = str(tmp_path)
    os.environ["UPLOAD_DIR"] = str(tmp_path)
    os.environ.pop("SVODKI_SEASON_RANGES_PATH", None)
    os.environ["SVODKI_SEASON_START_DATE"] = "2026-06-01"
    os.environ["SEASON_START_DATE"] = "2026-06-01"
    os.environ["SEASON_YEAR"] = "2026"
    os.environ["SVODKI_SEASON_YEARS"] = "2026"
    os.environ.pop("SEASON_TASK_YEAR_STR", None)
    os.environ.pop("SVODKI_PIPELINE", None)
    os.environ.pop("SVODKI_REPORT2_PIPELINE", None)
    clear_season_cache()
    try:
        spec = REPORTS["2"]
        with season_context(spec, date(2026, 10, 7), {"period_start": "2026-03-01"}):
            assert os.environ.get("SVODKI_SEASON_START_DATE") == "2026-03-01"
            assert os.environ.get("SEASON_START_DATE") == "2026-03-01"
            assert "SEASON_YEAR" not in os.environ
            assert "SVODKI_SEASON_YEARS" not in os.environ
            assert "SEASON_TASK_YEAR_STR" not in os.environ
            assert os.environ.get(group_env_key("2.4 Вспашка", "PERIOD_START")) == "2025-07-01"
            assert os.environ.get(group_env_key("2.11 Культивация", "PERIOD_START")) == "2026-03-14"
            assert os.environ.get("SVODKI_PIPELINE") == "1"
            assert os.environ.get("SVODKI_REPORT2_PIPELINE") == "1"
        assert os.environ.get("SEASON_YEAR") == "2026"
        assert os.environ.get("SVODKI_SEASON_YEARS") == "2026"
        assert os.environ.get("SVODKI_SEASON_START_DATE") == "2026-06-01"
        assert "SVODKI_PIPELINE" not in os.environ
        assert "SVODKI_REPORT2_PIPELINE" not in os.environ
        assert group_env_key("2.4 Вспашка", "PERIOD_START") not in os.environ
    finally:
        _restore(saved)
        clear_season_cache()


def test_saved_harvest_years_keep_combine_start() -> None:
    report = date(2026, 10, 7)
    saved = {key: os.environ.get(key) for key in (
        "SVODKI_SEASON_START_DATE",
        "SEASON_START_DATE",
        "SVODKI_COMBINE_SEASON_START",
        "SVODKI_COMBINE_SEASON_START_DATE",
    )}
    os.environ["SVODKI_SEASON_START_DATE"] = "2025-07-01"
    os.environ["SEASON_START_DATE"] = "2025-07-01"
    os.environ.pop("SVODKI_COMBINE_SEASON_START", None)
    os.environ.pop("SVODKI_COMBINE_SEASON_START_DATE", None)
    store = {
        "calendar_seasons": {},
        "by_group": {"5.0 Уборка": {"season_years": [2026], "season_year": 2026}},
    }
    try:
        bare = resolve_group_season_config(
            "5.0 Уборка", report, store={"calendar_seasons": {}, "by_group": {}},
        )
        saved_years = resolve_group_season_config("5.0 Уборка", report, store=store)
    finally:
        _restore(saved)
        clear_season_cache()
    assert bare["period_start"] == "2026-06-10"
    assert saved_years["period_start"] == "2026-06-10"
    assert saved_years["history_years"] == [2026]


def test_report5_summary_reads_daily_total_including_zero():
    from proverka.file_values import file_ha_for_operation, read_summary_blocks

    class _Sheet:
        def iter_rows(self, values_only=True):
            assert values_only
            yield ("КУЛЬТУРЫ",)
            yield ("Агрофирма", "Общий объем выполненной\nработы за сутки, га")
            yield ("Итого", 0)

    blocks = read_summary_blocks(_Sheet())
    assert blocks == [{"title": "КУЛЬТУРЫ", "file_ha": 0.0}]
    ha = file_ha_for_operation({"found": True, "blocks": blocks}, "4.0 Посев", "5")
    assert ha == 0.0


def test_inject_drops_shared_harvest_start():
    from season_unified import inject_season_range_env

    env = {
        "SVODKI_SEASON_START_DATE": "2026-06-10",
        "SEASON_START_DATE": "2026-06-10",
    }
    env["SEASON_YEAR"] = "2027"
    env["SVODKI_SEASON_YEARS"] = "2026,2027"
    inject_season_range_env(env, date(2026, 10, 7))
    assert "SVODKI_SEASON_START_DATE" not in env
    assert "SEASON_START_DATE" not in env
    assert "SEASON_YEAR" not in env
    assert "SVODKI_SEASON_YEARS" not in env
    assert env.get(group_env_key("3.1 Опрыскивание", "HISTORY_YEARS"))


def test_combine_start_reads_generation_env_when_settings_import_fails():
    from season_unified import combine_season_start_for_date

    keys = (
        "SVODKI_COMBINE_SEASON_START_DATE",
        "SVODKI_COMBINE_SEASON_START",
        group_env_key("5.0 Уборка", "PERIOD_START"),
    )
    saved = {key: os.environ.get(key) for key in keys}
    os.environ.pop("SVODKI_COMBINE_SEASON_START_DATE", None)
    os.environ["SVODKI_COMBINE_SEASON_START"] = "2026-06-20"
    os.environ.pop(group_env_key("5.0 Уборка", "PERIOD_START"), None)
    try:
        assert combine_season_start_for_date(date(2026, 10, 7)) == date(2026, 6, 20)
        os.environ.pop("SVODKI_COMBINE_SEASON_START", None)
        os.environ[group_env_key("5.0 Уборка", "PERIOD_START")] = "2026-06-18"
        assert combine_season_start_for_date(date(2026, 10, 7)) == date(2026, 6, 18)
    finally:
        _restore(saved)
