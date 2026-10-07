"""SEASON_START тематических сводок — как в модулях, не season_unified."""
from __future__ import annotations

import os
from datetime import date
from types import SimpleNamespace

from proverka.season_runtime import MODULES_OWN_SEASON_START, sync_module_season_start


def test_modules_own_season_start_includes_thematic_reports():
    assert "4_main_fixed" in MODULES_OWN_SEASON_START
    assert "6_main_3" in MODULES_OWN_SEASON_START
    assert "11_main_harvest" in MODULES_OWN_SEASON_START


def test_sync_spray_uses_january_not_march():
    mod = SimpleNamespace(__name__="6_main_3", SEASON_START=date(2026, 3, 1))
    rd = date(2026, 9, 4)
    saved = {k: os.environ.pop(k, None) for k in ("SVODKI_SEASON_START_DATE", "SEASON_START_DATE")}
    try:
        result = sync_module_season_start(mod, rd)
        assert result == date(2026, 1, 1)
        assert mod.SEASON_START == date(2026, 1, 1)
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_sync_cultivation_uses_module_default_not_unified_march14():
    mod = SimpleNamespace(__name__="8_main_fixed", SEASON_START=date(2026, 3, 14))
    rd = date(2026, 9, 4)
    saved = {k: os.environ.pop(k, None) for k in ("SVODKI_SEASON_START_DATE", "SEASON_START_DATE")}
    try:
        result = sync_module_season_start(mod, rd)
        assert result == date(2026, 3, 25)
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_sync_harvest_11_uses_june10_not_june1():
    mod = SimpleNamespace(__name__="11_main_harvest", SEASON_START=date(2026, 6, 1))
    rd = date(2026, 9, 4)
    saved = {
        k: os.environ.pop(k, None)
        for k in ("SVODKI_SEASON_START_DATE", "SEASON_START_DATE", "SVODKI_COMBINE_SEASON_START")
    }
    try:
        result = sync_module_season_start(mod, rd)
        assert result == date(2026, 6, 10)
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_run_override_period_start_wins():
    mod = SimpleNamespace(__name__="6_main_3", SEASON_START=date(2026, 1, 1))
    rd = date(2026, 9, 4)
    result = sync_module_season_start(
        mod, rd, run_override={"period_start": "2026-02-15"},
    )
    assert result == date(2026, 2, 15)
