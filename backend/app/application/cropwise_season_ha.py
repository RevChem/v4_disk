"""Гектары группы работ за сезоны редактора планов. Ответ work.json не кэшируется."""
from __future__ import annotations

from datetime import date
from typing import Any

from app.adapters.outbound.proverka.service import (
    _ensure_plan_path,
    _inject_proverka_runtime_env,
    session_cookie_from_env,
)


def cropwise_season_hectares(group_name: str, report_date: date) -> dict[str, Any]:
    _ensure_plan_path()
    _inject_proverka_runtime_env(report_date)
    from report_platform import reload_editor_modules

    reload_editor_modules()
    cookie = session_cookie_from_env()
    from proverka.season_hectares import fetch_season_hectares

    return fetch_season_hectares(cookie, group_name, report_date)


def cropwise_season_hectares_all(report_date: date) -> dict[str, Any]:
    _ensure_plan_path()
    _inject_proverka_runtime_env(report_date)
    from report_platform import reload_editor_modules

    reload_editor_modules()
    cookie = session_cookie_from_env()
    from proverka.season_hectares import fetch_all_season_hectares

    return fetch_all_season_hectares(cookie, report_date)
