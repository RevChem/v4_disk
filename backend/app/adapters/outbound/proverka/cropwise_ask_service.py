"""API-обёртка независимого запроса Cropwise → вопрос DeepSeek."""
from __future__ import annotations

from datetime import date
from typing import Any

from app.adapters.outbound.proverka.service import _ensure_plan_path


def _mod():
    _ensure_plan_path()
    from proverka import cropwise_ask

    return cropwise_ask


def cropwise_groups() -> dict[str, Any]:
    mod = _mod()
    return {"groups": mod.list_groups(), "cookie_configured": bool(mod.env_session_cookie())}


def cropwise_query(
    cookie: str,
    date_from: date,
    date_to: date,
    groups: list[str],
    *,
    firm: str | None = None,
    crop: str | None = None,
    season: int | None = None,
) -> dict[str, Any]:
    return _mod().query_tasks(
        cookie, date_from, date_to, groups, firm=firm, crop=crop, season=season,
    )


def cropwise_ask(
    cookie: str,
    date_from: date,
    date_to: date,
    groups: list[str],
    question: str,
    *,
    firm: str | None = None,
    crop: str | None = None,
    season: int | None = None,
    use_llm: bool = True,
) -> dict[str, Any]:
    return _mod().ask_tasks(
        cookie,
        date_from,
        date_to,
        groups,
        question,
        firm=firm,
        crop=crop,
        season=season,
        use_llm=use_llm,
    )
