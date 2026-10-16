"""Топливо за период и аномалии расхода: локальное хранилище плюс ФИО и виды работ из Cropwise."""
from __future__ import annotations

from datetime import date
from typing import Any

from app.adapters.outbound.proverka.driver_work_service import (
    _mod as _driver_mod,  # noqa: F401 — гарантирует sys.path к svodki/plan/utils/shared
    _resolve_people,
    _resolve_work_types,
    _store_dir,
)
from app.adapters.outbound.proverka.service import _ensure_plan_path


def _mod():
    _ensure_plan_path()
    _driver_mod()
    import fuel_analysis

    return fuel_analysis


def fuel_report(date_from: date, date_to: date) -> dict[str, Any]:
    mod = _mod()
    payload = mod.query_fuel(_store_dir(), date_from, date_to)
    if not payload.get("available"):
        payload["names_loaded"] = False
        payload["names_note"] = ""
        return payload
    driver_ids = {int(r["driver_id"]) for r in payload["by_driver"] if r.get("driver_id")}
    wt_ids = {int(r["wt"]) for r in payload["by_operation"] if r.get("wt")}
    names, note = _resolve_people(sorted(driver_ids))
    titles, _ = _resolve_work_types(sorted(wt_ids))
    mod.apply_names(payload, names, titles)
    payload["names_note"] = note
    return payload
