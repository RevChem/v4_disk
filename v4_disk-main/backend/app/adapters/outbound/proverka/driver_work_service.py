"""Выработка исполнителя за период: локальное хранилище плюс ФИО из Cropwise."""
from __future__ import annotations

import time
from datetime import date
from pathlib import Path
from typing import Any

from app.adapters.outbound.proverka.service import _ensure_plan_path
from app.config import get_cropwise_token, settings

_TTL_SEC = 600
_names: dict[int, tuple[float, str]] = {}
_work_types: dict[int, tuple[float, str]] = {}


def _store_dir() -> Path:
    upload = Path(settings.upload_dir)
    raw = (settings.svodki_task_store_dir or "").strip()
    return Path(raw) if raw else upload / ".task_store"


def _clean(text: str) -> str:
    return " ".join(str(text or "").split())


def person_name(user: dict[str, Any] | None) -> str:
    if not user:
        return ""
    parts = [user.get("last_name"), user.get("first_name"), user.get("middle_name")]
    fio = _clean(" ".join(str(p).strip() for p in parts if p and str(p).strip()))
    if fio:
        return fio
    for key in ("full_name", "name", "username"):
        text = _clean(str(user.get(key) or ""))
        if text:
            return text
    return ""


def _cached(store: dict[int, tuple[float, str]], ids: list[int]) -> tuple[dict[int, str], list[int]]:
    now = time.time()
    found: dict[int, str] = {}
    pending: list[int] = []
    for raw in ids:
        try:
            i = int(raw)
        except (TypeError, ValueError):
            continue
        hit = store.get(i)
        if hit and now - hit[0] <= _TTL_SEC and hit[1]:
            found[i] = hit[1]
        elif i not in pending:
            pending.append(i)
    return found, pending


def _remember(store: dict[int, tuple[float, str]], rows: dict[int, str]) -> None:
    now = time.time()
    for i, name in rows.items():
        if name:
            store[i] = (now, name)


def _mass(endpoint: str, ids: list[int], token: str) -> list[dict]:
    import httpx

    base = (settings.cropwise_base_url or "https://operations.cropwise.com/api/v3").rstrip("/")
    url = f"{base}/{endpoint}/mass_request"
    out: list[dict] = []
    with httpx.Client(headers={"X-User-Api-Token": token}, timeout=30) as client:
        for i in range(0, len(ids), 500):
            response = client.post(url, json={"data": ids[i : i + 500]})
            response.raise_for_status()
            body = response.json()
            chunk = body.get("data") if isinstance(body, dict) else body
            if isinstance(chunk, list):
                out.extend(row for row in chunk if isinstance(row, dict))
    return out


def _resolve_people(ids: list[int]) -> tuple[dict[int, str], str]:
    found, pending = _cached(_names, ids)
    if not pending:
        return found, ""
    token = get_cropwise_token()
    if not token:
        return found, "Токен Cropwise не задан: в списке номер исполнителя, а не ФИО."
    try:
        for endpoint in ("users", "workers", "operators"):
            if not pending:
                break
            rows = _mass(endpoint, pending, token)
            named: dict[int, str] = {}
            for row in rows:
                try:
                    rid = int(row.get("id"))
                except (TypeError, ValueError):
                    continue
                name = person_name(row)
                if name:
                    named[rid] = name
            _remember(_names, named)
            found.update(named)
            pending = [i for i in pending if i not in named]
    except Exception as exc:  # noqa: BLE001 — отчёт остаётся читаемым и без справочника
        return found, f"Справочник людей Cropwise не ответил ({exc.__class__.__name__})."
    if pending and not found:
        return found, "В Cropwise не нашлось ФИО для этих исполнителей."
    return found, ""


def _resolve_work_types(ids: list[int]) -> tuple[dict[int, str], str]:
    found, pending = _cached(_work_types, ids)
    if not pending:
        return found, ""
    token = get_cropwise_token()
    if not token:
        return found, ""
    try:
        rows = _mass("work_types", pending, token)
    except Exception:
        return found, ""
    named: dict[int, str] = {}
    for row in rows:
        try:
            rid = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        title = _clean(str(row.get("name") or row.get("title") or ""))
        if title:
            named[rid] = title
    _remember(_work_types, named)
    found.update(named)
    return found, ""


def _mod():
    _ensure_plan_path()
    import driver_work

    return driver_work


def _apply_names(payload: dict[str, Any], names: dict[int, str], note: str) -> None:
    payload["names_loaded"] = bool(names)
    payload["names_note"] = note
    for person in payload.get("people") or []:
        name = names.get(person.get("id"))
        if name:
            person["name"] = name
    people = payload.get("people") or []
    people.sort(key=lambda r: (-float(r.get("ha") or 0), str(r.get("name") or ""), int(r.get("id") or 0)))
    driver_id = payload.get("driver_id")
    if driver_id and names.get(int(driver_id)):
        payload["driver"] = names[int(driver_id)]
    for row in payload.get("rows") or []:
        name = names.get(row.get("driver_id"))
        if name:
            row["driver"] = name


def _apply_operations(payload: dict[str, Any], titles: dict[int, str]) -> None:
    if not titles:
        return
    for row in payload.get("rows") or []:
        title = titles.get(row.get("work_type_id"))
        if title:
            row["operation"] = title


def driver_work_people(date_from: date, date_to: date) -> dict[str, Any]:
    payload = _mod().query_driver_work(_store_dir(), date_from, date_to)
    if not payload.get("available"):
        payload["names_loaded"] = False
        payload["names_note"] = ""
        return payload
    names, note = _resolve_people([p["id"] for p in payload.get("people") or []])
    _apply_names(payload, names, note)
    return payload


def driver_work_detail(date_from: date, date_to: date, driver_id: int) -> dict[str, Any]:
    payload = _mod().query_driver_work(_store_dir(), date_from, date_to, driver_id=driver_id)
    if not payload.get("available"):
        payload["names_loaded"] = False
        payload["names_note"] = ""
        return payload
    names, note = _resolve_people([driver_id])
    titles, _ = _resolve_work_types(
        [row["work_type_id"] for row in payload.get("rows") or [] if row.get("work_type_id")]
    )
    _apply_names(payload, names, note)
    _apply_operations(payload, titles)
    return payload
