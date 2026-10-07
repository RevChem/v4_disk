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


def test_work_json_total_none_when_nothing_loaded():
    total, note = work_json_total(
        {"by_year": {2026: {"load_status": "not_loaded", "error": "таймаут"}}},
        (),
    )
    assert total is None
    assert "таймаут" in note


def test_generate_options_keep_cropwise_flag():
    body = GenerateReportsIn(summary_date=date(2026, 10, 7), compare_cropwise=True)
    assert body.generation_job_options() == {"compare_cropwise": True}
    off = GenerateReportsIn(summary_date=date(2026, 10, 7))
    assert off.generation_job_options() is None
