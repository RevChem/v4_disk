"""Порог сверки файла сводки с суммой work.json."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("SVODKI_ROOT", str(ROOT / "svodki"))
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "svodki" / "plan"))

from app.application.cropwise_file_check import ha_matches, work_json_total  # noqa: E402
from app.schemas import GenerateReportsIn  # noqa: E402
from datetime import date


def test_match_uses_five_hectares_or_two_percent():
    assert ha_matches(100, 104)
    assert not ha_matches(100, 106)
    assert ha_matches(1000, 1015)
    assert not ha_matches(1000, 1030)


def test_work_json_total_sums_loaded_seasons(monkeypatch):
    import proverka.cropwise_ui as ui

    monkeypatch.setattr(ui, "cropwise_total_ha", lambda _slot, _hints: 12.5)
    total, note = work_json_total(
        {
            "by_year": {
                2026: {"load_status": "loaded"},
                2027: {"load_status": "empty"},
            }
        },
        (),
    )
    assert total == 12.5
    assert note == ""


def test_work_json_total_skips_seasons_outside_the_group(monkeypatch):
    import proverka.cropwise_ui as ui

    monkeypatch.setattr(ui, "cropwise_total_ha", lambda slot, _hints: float(slot["ha"]))
    total, note = work_json_total(
        {
            "by_year": {
                2026: {"load_status": "loaded", "ha": 100},
                2027: {"load_status": "loaded", "ha": 40},
            }
        },
        (),
        seasons={"2027"},
    )
    assert total == 40
    assert note == ""


def test_work_json_total_none_when_nothing_loaded():
    total, note = work_json_total(
        {"by_year": {2026: {"load_status": "not_loaded", "error": "таймаут"}}},
        (),
    )
    assert total is None
    assert "таймаут" in note


def test_report_slots_stay_inside_its_groups():
    from app.application.cropwise_file_check import _slots_for_report

    packed = {
        "2.4 Вспашка": {"by_year": {2026: {}}},
        "3.1 Опрыскивание": {"by_year": {2026: {}}},
        "4.0 Посев": {"by_year": {2026: {}}},
    }
    spray = [name for name, _slot in _slots_for_report(
        "6", packed, ("3.1 Опрыскивание", "3.2 Опрыскивание (овощи)"),
    )]
    assert spray == ["3.1 Опрыскивание"]
    summary = {name for name, _slot in _slots_for_report("1", packed, ())}
    assert summary == {"2.4 Вспашка", "3.1 Опрыскивание", "4.0 Посев"}


def test_works_window_stays_in_calendar_year_after_july():
    from datetime import date

    from app.legacy.works_summary import _works_bounds

    start, end, _prev_start, _prev_end = _works_bounds(date.today().year + 1)
    assert start == date(date.today().year, 1, 1)
    assert end == date.today()


def test_generate_options_keep_cropwise_flag():
    body = GenerateReportsIn(summary_date=date(2026, 10, 7), compare_cropwise=True)
    assert body.generation_job_options() == {"compare_cropwise": True}
    off = GenerateReportsIn(summary_date=date(2026, 10, 7))
    assert off.generation_job_options() is None
