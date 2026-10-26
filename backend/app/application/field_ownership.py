"""Поля, у которых с выбранной даты сменилась принадлежность агрофирме.

Смотрит записи Namolot с этой даты и текущую папку поля в Cropwise:
пометка «(АФ3)» / префикс «АФ5-» менялась во времени, либо она не совпадает
с папкой. Таблица FIELD_FIRM_TRANSFERS такие поля не прячет: их и нужно увидеть.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from app.application.field_firm_check import firm_from_tags
from app.config import settings
from app.infrastructure.logging import get_logger

log = get_logger(__name__)

_CHUNK_DAYS = 90


def changed_ownership(
    records: list[dict],
    code_to_firm: dict[str, str],
    since: date,
    *,
    field_code: Callable[[object], str],
    record_time: Callable,
    number_to_firm: dict[int, str],
    known_codes: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Поля с сменой пометки или с пометкой, которая не равна текущей папке."""
    known = known_codes or set()
    buckets: dict[str, dict[str, dict[str, Any]]] = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        when = record_time(rec.get("date"))
        if when is None or when.date() < since:
            continue
        code = field_code(rec.get("spp"))
        tag = firm_from_tags(rec.get("spp"), number_to_firm)
        if not code or not tag:
            continue
        try:
            tonnes = float(rec.get("quantity") or 0) / 1000.0
        except (TypeError, ValueError):
            tonnes = 0.0
        day = when.date().isoformat()
        cell = buckets.setdefault(code, {}).setdefault(
            tag, {"from": day, "to": day, "tonnes": 0.0, "records": 0}
        )
        if day < cell["from"]:
            cell["from"] = day
        if day > cell["to"]:
            cell["to"] = day
        cell["tonnes"] += tonnes
        cell["records"] += 1

    items: list[dict[str, Any]] = []
    for code, firms in buckets.items():
        folder = code_to_firm.get(code) or ""
        tag_changed = len(firms) > 1
        folder_differs = bool(folder) and any(name != folder for name in firms)
        if not tag_changed and not folder_differs:
            continue
        periods = [
            {
                "firm": name,
                "from": cell["from"],
                "to": cell["to"],
                "tonnes": round(cell["tonnes"], 2),
                "records": cell["records"],
            }
            for name, cell in firms.items()
        ]
        periods.sort(key=lambda row: (row["from"], row["firm"]))
        items.append(
            {
                "code": code,
                "cropwise_firm": folder,
                "tag_changed": tag_changed,
                "folder_differs": folder_differs,
                "known_transfer": code in known,
                "periods": periods,
            }
        )
    items.sort(key=lambda row: row["code"])
    return items


def ownership_report(since: date) -> dict[str, Any]:
    """Срез с даты. Кэш Namolot берётся сразу; более ранний хвост дочитывается из 1С."""
    from app.application.namolot_view import (
        _NUMBER_TO_FIRM,
        field_base_code,
        field_code_index,
        msk_wall,
    )

    records, covered_from, covered_to, source, warning = _records_from(since)
    window = []
    for rec in records:
        when = msk_wall(rec.get("date")) if isinstance(rec, dict) else None
        if when is not None and when.date() >= since:
            window.append(rec)
    items = changed_ownership(
        window,
        field_code_index(),
        since,
        field_code=field_base_code,
        record_time=msk_wall,
        number_to_firm=_NUMBER_TO_FIRM,
        known_codes=_known_codes(),
    )
    return {
        "since": since.isoformat(),
        "covered_from": covered_from,
        "covered_to": covered_to,
        "source": source,
        "warning": warning,
        "records": len(window),
        "items": items,
    }


def _known_codes() -> set[str]:
    try:
        from plans import FIELD_FIRM_TRANSFERS
    except Exception:
        return set()
    return {str(code) for code in FIELD_FIRM_TRANSFERS}


def _cache_path() -> Path:
    return Path(settings.upload_dir) / ".namolot_cache" / "records.json"


def _read_cache() -> tuple[list[dict], date | None, date | None]:
    path = _cache_path()
    if not path.is_file():
        return [], None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], None, None
    if not isinstance(payload, dict):
        return [], None, None
    records = [row for row in payload.get("records") or [] if isinstance(row, dict)]
    return records, _parse_day(payload.get("date_begin")), _parse_day(payload.get("date_end"))


def _parse_day(value: object) -> date | None:
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _records_from(since: date) -> tuple[list[dict], str | None, str | None, str, str]:
    cached, cache_from, cache_to = _read_cache()
    today = date.today()
    parts: list[dict] = list(cached)
    sources: list[str] = ["кэш Namolot"] if cached else []
    warning = ""
    fetched = False
    if cache_from is None or cache_from > since:
        gap_end = (cache_from - timedelta(days=1)) if cache_from else today
        if gap_end >= since:
            extra, warning = _fetch_range(since, gap_end)
            parts.extend(extra)
            fetched = bool(extra)
            if fetched:
                sources.append("Namolot API")
    parts = _dedupe(parts)
    if not parts and not warning:
        warning = "Записей Namolot за этот период нет"
    if fetched or cache_from is None:
        covered_from = since
    else:
        covered_from = max(cache_from, since)
    covered_to = cache_to or today
    return parts, covered_from.isoformat(), covered_to.isoformat(), " и ".join(sources) or "нет данных", warning


def _dedupe(rows: list[dict]) -> list[dict]:
    seen: set[tuple[str, ...]] = set()
    out: list[dict] = []
    for rec in rows:
        if not isinstance(rec, dict):
            continue
        key = (
            str(rec.get("spp_guid") or rec.get("spp") or ""),
            str(rec.get("date") or ""),
            str(rec.get("quantity") or ""),
            str(rec.get("combine_harvester_guid") or rec.get("combine_harvester_id") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(rec)
    return out


def _fetch_range(start: date, end: date) -> tuple[list[dict], str]:
    from app.application.namolot_view import fetch_live

    rows: list[dict] = []
    cursor = start
    try:
        while cursor <= end:
            chunk_end = min(end, cursor + timedelta(days=_CHUNK_DAYS))
            rows.extend(fetch_live(cursor, chunk_end, max_days=_CHUNK_DAYS + 1))
            cursor = chunk_end + timedelta(days=1)
    except Exception as exc:
        log.warning("field_ownership.namolot_failed", error=str(exc))
        if rows:
            return rows, "Часть периода из 1С не загрузилась. Показаны записи, которые уже есть."
        return rows, "Namolot не ответил, а локального кэша за эту дату нет."
    return rows, ""
