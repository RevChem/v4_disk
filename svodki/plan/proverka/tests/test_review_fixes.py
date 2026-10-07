"""Правки по ревью: сезоны в env, культура строки, окно смены, загрузчик UI."""
from __future__ import annotations

import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import requests

PLAN = Path(__file__).resolve().parents[2]
for extra in (PLAN, PLAN / "utils" / "season", PLAN / "utils" / "api"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import proverka.cropwise_ui as ui  # noqa: E402
from proverka.cropwise_ui import WorkLoadJob  # noqa: E402


# --------------------------------------------------------------------------- season_runtime

def test_season_context_applies_all_groups_and_restores_env() -> None:
    from proverka.reports_config import REPORTS
    from proverka.season_runtime import season_context

    spec = REPORTS["5"]  # в work_groups только «4.0 Посев»
    before = {k: v for k, v in os.environ.items() if k.startswith("SVODKI_GRP_")}
    with season_context(spec, date(2026, 10, 7)):
        grp = {k for k in os.environ if k.startswith("SVODKI_GRP_")}
        # Сезоны групп, которых нет в spec, раньше собирались и выбрасывались.
        assert any("Внесение_минеральных_удобрений" in k for k in grp)
        # Тематическая сводка сама задаёт начало сезона.
        assert os.environ.get("SVODKI_SEASON_START_DATE") is None
    after = {k: v for k, v in os.environ.items() if k.startswith("SVODKI_GRP_")}
    assert after == before


# --------------------------------------------------------------------------- окно смены

def _item(entries: list[tuple[str, float]]) -> dict:
    return {"field_id": 1, "covered_area_hourly": [[ts, ha] for ts, ha in entries]}


def test_shift_window_lower_bound_only_with_pad(monkeypatch: pytest.MonkeyPatch) -> None:
    from proverka.season_cropwise import _area_by_field_in_shift, _area_in_shift, _parse_dt

    day_start = _parse_dt("2026-10-07T06:00:00")
    cutoff = _parse_dt("2026-10-08T06:00:00")
    items = [_item([("2026-10-06T22:00:00", 3.0), ("2026-10-07T10:00:00", 5.0)])]

    monkeypatch.delenv("PROVERKA_TASK_WINDOW_PAD_HOURS", raising=False)
    assert _area_in_shift(items, day_start, cutoff) == 8.0  # как в сводках
    assert _area_by_field_in_shift(items, day_start, cutoff) == {1: 8.0}

    monkeypatch.setenv("PROVERKA_TASK_WINDOW_PAD_HOURS", "6")
    assert _area_in_shift(items, day_start, cutoff) == 5.0  # соседняя смена не удваивается
    assert _area_by_field_in_shift(items, day_start, cutoff) == {1: 5.0}
    # day_start == cutoff — старый признак «границы нет»
    assert _area_in_shift(items, cutoff, cutoff) == 8.0


# --------------------------------------------------------------------------- культура строки

def test_api_crop_comes_from_row_history_item(monkeypatch: pytest.MonkeyPatch) -> None:
    import mtfmi_batch_fetch as mb
    from proverka import cropwise_crops as cc
    from proverka.season_cropwise import _parse_dt

    monkeypatch.setattr(mb, "_HISTORY_CROP", {101: "Соя", 102: "Пшеница яровая"})
    monkeypatch.setattr(mb, "_HISTORY_FIELD", {101: 1, 102: 999})
    cutoff = _parse_dt("2026-10-08T06:00:00")
    hours = [["2026-10-07T10:00:00", 10.0]]
    mtfmi = {
        7: [
            {"field_id": 1, "history_item_id": 101, "covered_area_hourly": hours},
            {"field_id": 2, "history_item_id": None, "covered_area_hourly": hours},
            # запись истории принадлежит чужому полю
            {"field_id": 3, "history_item_id": 102, "covered_area_hourly": hours},
        ],
    }
    tasks = [{"id": 7, "season": "2027", "work_type_id": 1}]
    f2f = {1: "АФ-1", 2: "АФ-1", 3: "АФ-1"}

    out = cc.api_area_by_firm_crop(tasks, mtfmi, f2f, {"2027"}, cutoff)

    assert out["Соя"] == {"АФ-1": 10.0}
    # строки без культуры не теряются и не получают культуру поля
    assert out[mb.UNDEFINED_CROP] == {"АФ-1": 20.0}
    assert "Пшеница яровая" not in out


def test_api_crop_ignores_tasks_of_other_seasons(monkeypatch: pytest.MonkeyPatch) -> None:
    import mtfmi_batch_fetch as mb
    from proverka import cropwise_crops as cc
    from proverka.season_cropwise import _parse_dt

    monkeypatch.setattr(mb, "_HISTORY_CROP", {101: "Соя"})
    monkeypatch.setattr(mb, "_HISTORY_FIELD", {101: 1})
    cutoff = _parse_dt("2026-10-08T06:00:00")
    mtfmi = {7: [{"field_id": 1, "history_item_id": 101,
                  "covered_area_hourly": [["2026-10-07T10:00:00", 4.0]]}]}
    tasks = [{"id": 7, "season": "2026", "work_type_id": 1}]
    assert cc.api_area_by_firm_crop(tasks, mtfmi, {1: "АФ-1"}, {"2027"}, cutoff) == {}


# --------------------------------------------------------------------------- загрузчик UI

def _work_ok(year: int) -> dict:
    return {
        "ok": True,
        "body": {
            "regionColumns": [
                {"key": "name", "name": "Поле"},
                {"key": "f", "name": "Факт, га", "work_type_names": ["x"]},
            ],
            "regionRows": [{"name": "Всего", "f": "10"}],
            "cropRows": [],
            "cropColumns": [],
        },
        "ui_session_seasonValue": year,
    }


def test_single_error_then_data_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = [{"ok": False, "error": "HTTP 500", "body": None}, _work_ok(2027)]
    monkeypatch.setattr(ui, "fetch_work_report", lambda *a, **k: answers.pop(0))
    job = WorkLoadJob("g", "2026-10-07T06:00:00", "2026-10-08T06:00:00", [1], ("x",))
    got = ui._fetch_job_once_or_retry(object(), "csrf", job, 2027)
    assert got["load_status"] == "loaded"
    assert got["ui_session_seasonValue"] == 2027


def test_error_then_blank_is_still_unstable(monkeypatch: pytest.MonkeyPatch) -> None:
    blank = {"ok": True, "body": {"reportBlank": True}}
    answers = [{"ok": False, "error": "HTTP 500", "body": None}, blank]
    monkeypatch.setattr(ui, "fetch_work_report", lambda *a, **k: answers.pop(0))
    job = WorkLoadJob("g", "2026-10-07T06:00:00", "2026-10-08T06:00:00", [1], ("x",))
    assert ui._fetch_job_once_or_retry(object(), "csrf", job, 2027)["load_status"] == "unstable"


def test_failed_season_restore_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path,
) -> None:
    monkeypatch.setenv("PROVERKA_UI_LOCK_PATH", str(tmp_path / "ui.lock"))
    calls: list[int] = []

    def set_year(web, csrf, year):
        calls.append(int(year))
        if int(year) == 2026:  # возврат исходного сезона
            raise requests.RequestException("boom")
        return int(year)

    monkeypatch.setattr(ui, "warm_web_session", lambda cookie: (object(), "csrf", [], 2026))
    monkeypatch.setattr(ui, "set_ui_session_year", set_year)
    monkeypatch.setattr(ui, "read_ui_session_season", lambda web: 2027)
    monkeypatch.setattr(ui, "fetch_work_report", lambda *a, **k: _work_ok(2027))
    job = WorkLoadJob("g", "2026-10-07T06:00:00", "2026-10-08T06:00:00", [1], ("x",))
    with caplog.at_level(logging.ERROR, logger=ui.logger.name):
        got = ui.load_works_for_seasons("cookie", [job], years=[2027])
    assert got["g"][2027]["load_status"] == "loaded"
    assert calls == [2027, 2026]
    assert any("вернуть сезон" in r.getMessage() for r in caplog.records)


def test_busy_ui_session_gives_not_loaded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PROVERKA_UI_LOCK_PATH", str(tmp_path / "ui.lock"))
    monkeypatch.setenv("PROVERKA_UI_LOCK_TIMEOUT", "0.2")
    called: list[str] = []
    monkeypatch.setattr(
        ui, "warm_web_session", lambda cookie: called.append(cookie) or (object(), "c", [], 2026),
    )
    job = WorkLoadJob("g", "2026-10-07T06:00:00", "2026-10-08T06:00:00", [1], ("x",))
    with ui.ui_session_lock(timeout=1):
        got = ui.load_works_for_seasons("cookie", [job], years=[2026, 2027])
    assert called == []  # самостоятельная загрузка под чужой блокировкой не стартует
    assert {y: v["load_status"] for y, v in got["g"].items()} == {
        2026: "not_loaded", 2027: "not_loaded",
    }


def test_lock_is_released_after_load(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PROVERKA_UI_LOCK_PATH", str(tmp_path / "ui.lock"))
    monkeypatch.setattr(ui, "warm_web_session", lambda cookie: (object(), "c", [], 2026))
    monkeypatch.setattr(ui, "read_ui_session_season", lambda web: 2026)
    monkeypatch.setattr(ui, "fetch_work_report", lambda *a, **k: _work_ok(2026))
    job = WorkLoadJob("g", "2026-10-07T06:00:00", "2026-10-08T06:00:00", [1], ("x",))
    ui.load_works_for_seasons("cookie", [job], years=[2026])
    with ui.ui_session_lock(timeout=0.5):  # не должно зависнуть
        pass
