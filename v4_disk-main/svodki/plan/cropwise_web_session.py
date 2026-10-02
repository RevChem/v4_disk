#!/usr/bin/env python3
"""Cropwise Operations web UI session (for /machines/reports/work.json).

API token (X-User-Api-Token) works for /api/v3/* only.
Rails JSON routes need browser cookie _cropio_ses after login.
"""
from __future__ import annotations

import os
import re
from typing import Any

import requests

HOST = "https://operations.cropwise.com"
SIGN_IN_URL = f"{HOST}/d/users/sign_in"


def _parse_cookie_header(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, _, v = part.partition("=")
        out[k.strip()] = v.strip()
    return out


def make_web_session() -> tuple[requests.Session, str]:
    """
    Returns (session, source) where source is 'cookie' | 'none'.
    Set CROPWISE_SESSION_COOKIE in .env (copy from DevTools after login).
    """
    session = requests.Session()
    session.headers.update({"Accept": "application/json, text/html, */*"})

    raw_cookie = (os.environ.get("CROPWISE_SESSION_COOKIE") or "").strip()
    if raw_cookie:
        for name, value in _parse_cookie_header(raw_cookie).items():
            session.cookies.set(name, value, domain="operations.cropwise.com")
        return session, "cookie"

    return session, "none"


def verify_web_session(session: requests.Session) -> bool:
    """True if session can access a protected JSON route."""
    r = session.get(f"{HOST}/machines/reports/work.json", timeout=30)
    return r.status_code == 200


def fetch_work_report_json(
    session: requests.Session,
    *,
    time_from: str,
    time_to: str,
    work_type_ids: list[int] | set[int],
    extra_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params = {
        "from_time": time_from,
        "to_time": time_to,
        "start_time": time_from,
        "end_time": time_to,
        "work_type_ids": ",".join(str(x) for x in sorted(set(work_type_ids))),
    }
    if extra_params:
        params.update(extra_params)
    r = session.get(f"{HOST}/machines/reports/work.json", params=params, timeout=120)
    if r.status_code != 200:
        return {"status": r.status_code, "error": r.text[:500]}
    try:
        return {"status": 200, "body": r.json()}
    except Exception:
        return {"status": 200, "body": {"raw": r.text[:2000]}}


def cookie_setup_hint() -> str:
    return (
        "Чтобы открыть /machines/reports/work.json:\n"
        "1. Войдите в https://operations.cropwise.com в браузере\n"
        "2. DevTools → Network → любой запрос к operations.cropwise.com\n"
        "3. Скопируйте Cookie (нужен _cropio_ses=...)\n"
        "4. В .env: CROPWISE_SESSION_COOKIE=_cropio_ses=...; другие_куки=...\n"
        "Cookie живёт ограниченное время — обновляйте при 401."
    )
