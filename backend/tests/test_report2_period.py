"""Сводка 2 за период: параметры запуска и приоритет выбранных сезонов."""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "svodki" / "plan" / "utils" / "season"))

from app.legacy.report_generation import (  # noqa: E402
    _apply_report2_selection_env,
    _build_report2_period_window_iso,
    _report2_group_env_key,
    _report2_period_storage_title,
)
from app.schemas import GenerateReportsIn  # noqa: E402
from season_unified import (  # noqa: E402
    clear_season_cache,
    group_env_key,
    resolve_group_season_config,
)

REPORT_DATE = date(2026, 9, 24)


def _body(**kwargs):
    return GenerateReportsIn(summary_date=REPORT_DATE, report_ids=["2"], **kwargs)


def test_report2_period_options_keep_groups_and_seasons():
    body = _body(
        report2_date_from=date(2025, 2, 14),
        report2_date_to=date(2026, 9, 24),
        report2_groups=["2.4 Вспашка", "4.0 Посев"],
        report2_seasons={"2.4 Вспашка": [2025, 2026], "4.0 Посев": [2026]},
    )
    opts = body.generation_job_options()
    assert opts["report2_date_from"] == "2025-02-14"
    assert opts["report2_date_to"] == "2026-09-24"
    assert opts["report2_time_from"] == "06:00"
    assert opts["report2_cutoff"] == "05:59"
    timed = _body(
        report2_date_from=date(2025, 2, 14),
        report2_date_to=date(2026, 9, 24),
        report2_time_from="08:30",
        report2_groups=["1.1 Внесение минеральных удобрений"],
        report2_seasons={"1.1 Внесение минеральных удобрений": [2027]},
    ).generation_job_options()
    assert timed["report2_time_from"] == "08:30"
    assert opts["report2_groups"] == ["2.4 Вспашка", "4.0 Посев"]
    assert opts["report2_seasons"]["2.4 Вспашка"] == [2025, 2026]
    assert opts["report2_seasons"]["4.0 Посев"] == [2026]


def test_report2_period_rejects_other_reports_and_empty_season():
    with pytest.raises(ValueError, match="report2_"):
        GenerateReportsIn(
            summary_date=REPORT_DATE,
            report_ids=["2", "1"],
            report2_date_from=date(2025, 2, 14),
            report2_date_to=date(2026, 9, 24),
            report2_groups=["2.4 Вспашка"],
            report2_seasons={"2.4 Вспашка": [2026]},
        ).generation_job_options()
    with pytest.raises(ValueError, match="сезон"):
        _body(
            report2_date_from=date(2025, 2, 14),
            report2_date_to=date(2026, 9, 24),
            report2_groups=["2.4 Вспашка"],
            report2_seasons={"2.4 Вспашка": []},
        ).generation_job_options()


def test_report2_window_includes_last_shift_day():
    start, end = _build_report2_period_window_iso(
        date(2025, 2, 14), "06:00", date(2026, 9, 24), "06:00",
    )
    assert start == "2025-02-14T06:00:00"
    assert end == "2026-09-25T06:00:00"
    start_late, end_late = _build_report2_period_window_iso(
        date(2025, 2, 14), "08:30", date(2026, 9, 24), "06:00",
    )
    assert start_late == "2025-02-14T08:30:00"
    assert end_late == "2026-09-25T06:00:00"
    start_shift, end_shift = _build_report2_period_window_iso(
        date(2026, 10, 1), "06:00", date(2026, 11, 1), "05:59",
    )
    assert start_shift == "2026-10-01T06:00:00"
    assert end_shift == "2026-11-01T06:00:00"


def test_report2_env_key_matches_season_resolver():
    assert _report2_group_env_key("4.1 Посев (овощи)", "HISTORY_YEARS") == group_env_key(
        "4.1 Посев (овощи)", "HISTORY_YEARS",
    )


def test_report2_title_uses_full_dates():
    title = _report2_period_storage_title("2. Сводка ХК Детальная", date(2025, 2, 14), date(2026, 9, 24))
    assert title == "2.Сводка ХК Детальная excel с 14.02.2025 по 24.09.2026"


def test_forced_seasons_beat_editor_file(tmp_path, monkeypatch):
    path = tmp_path / "season_ranges.json"
    path.write_text(
        json.dumps({"by_group": {"2.4 Вспашка": {"season_years": [2027]}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SVODKI_SEASON_RANGES_PATH", str(path))
    monkeypatch.delenv("SVODKI_FORCE_GROUP_SEASONS", raising=False)
    hist_key = group_env_key("2.4 Вспашка", "HISTORY_YEARS")
    task_key = group_env_key("2.4 Вспашка", "TASK_SEASON_YEARS")
    monkeypatch.delenv(hist_key, raising=False)
    monkeypatch.delenv(task_key, raising=False)
    clear_season_cache()
    plain = resolve_group_season_config("2.4 Вспашка", REPORT_DATE)
    assert 2027 in plain["history_years"]

    env: dict[str, str] = {}
    _apply_report2_selection_env(env, ["2.4 Вспашка"], {"2.4 Вспашка": [2025, 2026]})
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    clear_season_cache()
    forced = resolve_group_season_config("2.4 Вспашка", REPORT_DATE)
    assert forced["history_years"] == [2025, 2026]
    assert forced["task_season_years"] == [2025, 2026]
    clear_season_cache()
