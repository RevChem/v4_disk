"""Гектары операции за сезоны редактора планов. Сеть не вызывается."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

PLAN = Path(__file__).resolve().parents[2]
for extra in (PLAN, PLAN / "utils" / "season"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from proverka import season_hectares as sh  # noqa: E402

REPORT_DATE = date(2026, 10, 14)


def _body(total: str, firm: str, firm_ha: str) -> dict:
    return {
        "regionColumns": [
            {"key": "name", "name": "Группа полей"},
            {"key": "f", "name": "Обработка СЗР, га", "work_type_names": ["3.1 Опрыскивание"]},
        ],
        "regionRows": [
            {"name": "Всего", "f": total},
            {"name": firm, "f": firm_ha},
        ],
        "cropRows": [],
        "cropColumns": [],
    }


def test_fetch_uses_editor_window_and_splits_years(monkeypatch):
    import plans
    import proverka.cropwise_ui as ui
    import season_unified

    captured = {"calls": []}

    def fake_load(cookie, jobs, years=None):
        captured["calls"].append({
            "years": list(years),
            "window": (jobs[0].time_from, jobs[0].time_to),
            "wt": list(jobs[0].work_type_ids),
            "key": jobs[0].key,
        })
        return {
            "3.1 Опрыскивание": {
                2026: {"load_status": "loaded", "body": _body("100,5", "АФ А", "100,5")},
                2027: {"load_status": "empty"},
            }
        }

    monkeypatch.setattr(
        season_unified,
        "resolve_group_season_config",
        lambda name, rd: {
            "task_season_years": [2026, 2027],
            "history_years": [2026, 2027],
            "period_start": "2025-07-01",
            "period_end": "2026-10-14",
        },
    )
    monkeypatch.setattr(plans, "work_group_ids", lambda name: [64])
    monkeypatch.setattr(plans, "FIRM_ORDER", ["АФ А"])
    monkeypatch.setattr(ui, "load_works_for_seasons", fake_load)

    out = sh.fetch_season_hectares("cookie", "3.1 Опрыскивание", REPORT_DATE)
    assert [call["years"] for call in captured["calls"]] == [[2026], [2027]]
    assert captured["calls"][0]["window"] == ("2025-07-01T06:00:00", "2026-10-15T06:00:00")
    assert captured["calls"][0]["wt"] == [64]
    assert out["period_start"] == "2025-07-01"
    assert out["seasons"][0]["status"] == "loaded"
    assert out["seasons"][0]["total_ha"] == 100.5
    assert out["seasons"][0]["by_firm"] == [{"firm": "АФ А", "ha": 100.5}]
    assert out["seasons"][0]["column"] == "Обработка СЗР, га"
    assert out["seasons"][1]["status"] == "empty"
    assert out["seasons"][1]["total_ha"] is None


def test_missing_editor_years_raise(monkeypatch):
    import season_unified

    monkeypatch.setattr(
        season_unified,
        "resolve_group_season_config",
        lambda name, rd: {"task_season_years": [], "history_years": [], "period_start": "2025-07-01", "period_end": "2026-10-14"},
    )
    try:
        sh.fetch_season_hectares("cookie", "3.1 Опрыскивание", REPORT_DATE)
    except ValueError as exc:
        assert "не заданы сезоны" in str(exc)
    else:
        raise AssertionError("ожидали ошибку")
