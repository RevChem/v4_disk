"""Применение ручных правок каталога планов (plans_1 / plans) из SVODKI_MANUAL_OVERRIDES_PATH."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

PLAN_FLAT_SEP = " | "
_APPLIED = False


def flatten_nested_plan(nested: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    if not isinstance(nested, dict):
        return out
    for group_name, firms in nested.items():
        if not isinstance(firms, dict):
            continue
        for firm_name, crops in firms.items():
            if not isinstance(crops, dict):
                continue
            for crop_name, raw in crops.items():
                key = f"{group_name}{PLAN_FLAT_SEP}{firm_name}{PLAN_FLAT_SEP}{crop_name}"
                try:
                    out[key] = float(raw)
                except (TypeError, ValueError):
                    out[key] = 0.0
    return out


def unflatten_plan_ha(flat: dict[str, Any]) -> dict[str, dict[str, dict[str, float]]]:
    nested: dict[str, dict[str, dict[str, float]]] = {}
    if not isinstance(flat, dict):
        return nested
    for raw_key, raw_val in flat.items():
        parts = [p.strip() for p in str(raw_key).split("|")]
        if len(parts) != 3:
            continue
        group_name, firm_name, crop_name = parts[0], parts[1], parts[2]
        try:
            val = float(raw_val)
        except (TypeError, ValueError):
            val = 0.0
        nested.setdefault(group_name, {}).setdefault(firm_name, {})[crop_name] = val
    return nested


def _safe_float(raw: Any, default: float = 0.0) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _rebuild_plans_1_derivatives(p1: Any) -> None:
    nested = getattr(p1, "HARDCODED_PLAN", None)
    if not isinstance(nested, dict):
        return

    flat = getattr(p1, "HARDCODED_PLAN_FLAT", None)
    if isinstance(flat, dict):
        flat.clear()
        flat.update(
            {
                (group, firm, crop): float(val)
                for group, firms in nested.items()
                if isinstance(firms, dict)
                for firm, crops in firms.items()
                if isinstance(crops, dict)
                for crop, val in crops.items()
            }
        )

    manual_crops = getattr(p1, "_PLAN_11_MANUAL_CROPS", None)
    plan_11 = getattr(p1, "PLAN_11_HA", None)
    src_11 = nested.get("1.1 Внесение минеральных удобрений", {})
    if isinstance(plan_11, dict) and isinstance(src_11, dict):
        plan_11.clear()
        for firm, crops in src_11.items():
            if not isinstance(crops, dict):
                continue
            row = {
                crop: float(val)
                for crop, val in crops.items()
                if manual_crops is None or crop in manual_crops
            }
            if row:
                plan_11[firm] = row

    seeding_order = getattr(p1, "SEEDING_CROP_ORDER", ())
    plan_seeding = getattr(p1, "PLAN_SEEDING_HA", None)
    firms_meta = getattr(p1, "AGROFIRMS_META", {}) or {}
    src_seed = nested.get("4.0 Посев", {})
    if isinstance(plan_seeding, dict) and isinstance(src_seed, dict):
        plan_seeding.clear()
        for firm in firms_meta:
            crops = src_seed.get(firm, {}) if isinstance(src_seed.get(firm), dict) else {}
            plan_seeding[firm] = {
                crop: (
                    float(crops[crop])
                    if crop in crops and crops[crop] is not None
                    else None
                )
                for crop in seeding_order
            }


def _sync_plans_py_flat(p1: Any) -> None:
    """Синхронизирует tuple-ключи HARDCODED_PLAN в plans.py (отчёт 4 и legacy)."""
    try:
        import plans as p_mod  # noqa: WPS433
    except Exception:
        return
    nested = getattr(p1, "HARDCODED_PLAN", None)
    if not isinstance(nested, dict):
        return
    flat_py = getattr(p_mod, "HARDCODED_PLAN", None)
    if not isinstance(flat_py, dict):
        return
    new_flat = {
        (group, firm, crop): float(val)
        for group, firms in nested.items()
        if isinstance(firms, dict)
        for firm, crops in firms.items()
        if isinstance(crops, dict)
        for crop, val in crops.items()
    }
    flat_py.clear()
    flat_py.update(new_flat)


def _patch_dict_in_place(target: dict[str, Any], source: dict[str, Any], *, coerce_float: bool) -> None:
    if not isinstance(target, dict) or not isinstance(source, dict):
        return
    for key, raw in source.items():
        sk = str(key)
        if coerce_float:
            target[sk] = _safe_float(raw, target.get(sk, 0.0))
        else:
            target[sk] = raw


def _patch_available_own_nested(target: dict[str, Any], flat: dict[str, Any]) -> None:
    if not isinstance(target, dict) or not isinstance(flat, dict):
        return
    for raw_key, raw_val in flat.items():
        parts = [p.strip() for p in str(raw_key).split("|")]
        if len(parts) != 2:
            continue
        group_name, firm_name = parts[0], parts[1]
        try:
            cnt = int(round(float(raw_val)))
        except (TypeError, ValueError):
            cnt = 0
        target.setdefault(group_name, {})
        if isinstance(target[group_name], dict):
            target[group_name][firm_name] = cnt


def apply_plan_catalog_from_store(store: dict[str, Any]) -> None:
    global _APPLIED
    block = store.get("plans")
    if not isinstance(block, dict) or not block:
        return

    try:
        import plans_1 as p1  # noqa: WPS433
    except Exception:
        return

    nested = getattr(p1, "HARDCODED_PLAN", None)
    flat_ha = block.get("hardcoded_plan_ha")
    if isinstance(nested, dict) and isinstance(flat_ha, dict) and flat_ha:
        overlay = unflatten_plan_ha(flat_ha)
        for group_name, firms in overlay.items():
            nested.setdefault(group_name, {})
            if not isinstance(nested[group_name], dict):
                nested[group_name] = {}
            for firm_name, crops in firms.items():
                nested[group_name].setdefault(firm_name, {})
                if not isinstance(nested[group_name][firm_name], dict):
                    nested[group_name][firm_name] = {}
                if isinstance(crops, dict):
                    for crop_name, val in crops.items():
                        nested[group_name][firm_name][crop_name] = float(val)

    rate = block.get("plan_rate_per_group")
    if isinstance(rate, dict):
        tgt = getattr(p1, "PLAN_RATE_PER_GROUP", None)
        if isinstance(tgt, dict):
            _patch_dict_in_place(tgt, rate, coerce_float=True)

    ha_status = block.get("plan_ha_status")
    if isinstance(ha_status, dict):
        tgt = getattr(p1, "PLAN_HA_STATUS", None)
        if isinstance(tgt, dict):
            _patch_dict_in_place(tgt, ha_status, coerce_float=True)

    veg = block.get("veg_crop_plan_ha")
    if isinstance(veg, dict):
        tgt = getattr(p1, "VEG_CROP_PLAN_HA", None)
        if isinstance(tgt, dict):
            _patch_dict_in_place(tgt, veg, coerce_float=True)

    own_flat = block.get("available_own_by_group_firm")
    if isinstance(own_flat, dict):
        tgt = getattr(p1, "AVAILABLE_OWN", None)
        if isinstance(tgt, dict):
            _patch_available_own_nested(tgt, own_flat)

    _rebuild_plans_1_derivatives(p1)
    _sync_plans_py_flat(p1)

    # plans.py — ставки и статусы (если модуль уже загружен)
    try:
        import plans as p_mod  # noqa: WPS433
    except Exception:
        p_mod = None
    if p_mod is not None:
        if isinstance(rate, dict):
            tgt = getattr(p_mod, "PLAN_RATE_PER_GROUP", None)
            if isinstance(tgt, dict):
                _patch_dict_in_place(tgt, rate, coerce_float=True)
        if isinstance(ha_status, dict):
            tgt = getattr(p_mod, "PLAN_HA_STATUS", None)
            if isinstance(tgt, dict):
                _patch_dict_in_place(tgt, ha_status, coerce_float=True)
        if isinstance(veg, dict):
            tgt = getattr(p_mod, "VEG_CROP_PLAN_HA", None)
            if isinstance(tgt, dict):
                _patch_dict_in_place(tgt, veg, coerce_float=True)
        if isinstance(own_flat, dict):
            tgt = getattr(p_mod, "AVAILABLE_OWN", None)
            if isinstance(tgt, dict):
                _patch_available_own_nested(tgt, own_flat)

    _APPLIED = True


def apply_manual_plan_catalog_if_needed() -> None:
    global _APPLIED
    if _APPLIED:
        return
    path = os.environ.get("SVODKI_MANUAL_OVERRIDES_PATH", "").strip()
    if not path:
        return
    p = Path(path)
    if not p.is_file():
        return
    try:
        store = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(store, dict):
        return
    apply_plan_catalog_from_store(store)


def _ensure_plan_dir_on_path() -> None:
    plan_dir = Path(__file__).resolve().parent
    s = str(plan_dir)
    if s not in sys.path:
        sys.path.insert(0, s)


_ensure_plan_dir_on_path()
