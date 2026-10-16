"""Хаб аналитики: обёртка над svodki/plan/utils/shared/svodki_hub.py.

Добавляет к срезам то, что знает только backend: эффективные планы из редактора
и ФИО исполнителей из Cropwise (в хранилище заданий есть только driver_id).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .analytics import module as _analytics_module


def hub():
    _analytics_module()  # настраивает sys.path и SVODKI_TASK_STORE_DIR
    import svodki_hub

    return svodki_hub


def _effective_plan() -> dict[str, Any]:
    from . import plans_editor as pe

    base = pe.extract_base_plans()["base"]
    effective = pe.merge_plans(base, pe.load_edits_store())
    plan = effective.get("HARDCODED_PLAN")
    return plan if isinstance(plan, dict) else {}


def _driver_names(ids: list[int]) -> tuple[dict[int, str], str]:
    if not ids:
        return {}, ""
    try:
        from app.adapters.outbound.proverka.driver_work_service import _resolve_people

        return _resolve_people(ids)
    except Exception as exc:  # noqa: BLE001 — без ФИО срез остаётся читаемым
        return {}, f"ФИО из Cropwise недоступны ({exc.__class__.__name__}): показаны номера исполнителей."


def _exclusive(date_to: str) -> str:
    """Интерфейс присылает «по» включительно, хранилище считает [с, по)."""
    return (date.fromisoformat(date_to) + timedelta(days=1)).isoformat()


def run_query(query_id: str, *, date_from: str, date_to: str, group: str | None, driver_id: int | None,
              machine_id: int | None, field_id: int | None, threshold: float | None) -> dict[str, Any]:
    plan = _effective_plan() if query_id == "plan_progress" else None
    payload = hub().run(
        query_id, date_from=date_from, date_to=_exclusive(date_to), group=group, driver_id=driver_id,
        machine_id=machine_id, field_id=field_id, threshold=threshold, plan=plan,
    )
    payload["date_to"] = date_to
    ids = sorted({int(r["driver_id"]) for r in payload.get("rows") or [] if r.get("driver_id")})
    names, note = _driver_names(ids)
    for row in payload.get("rows") or []:
        name = names.get(row.get("driver_id")) if row.get("driver_id") else None
        if name:
            row["driver"] = name
    if note:
        payload["note"] = (payload.get("note") or "") + ("\n" if payload.get("note") else "") + note
    return payload


def run_options(kind: str, date_from: str, date_to: str) -> list[dict[str, Any]]:
    items = hub().options(kind, date_from, _exclusive(date_to))
    if kind == "drivers":
        names, _ = _driver_names([int(i["id"]) for i in items])
        for item in items:
            item["label"] = names.get(int(item["id"])) or item["label"]
    return items
