"""Подстановка правок plans из JSON (env SVODKI_PLANS_EDITS_PATH).

Файлы plans.py / plans.py на диске не меняются — только runtime globals модуля.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _deep_merge(base: Any, patch: Any) -> Any:
    if not isinstance(patch, dict):
        return patch
    if not isinstance(base, dict):
        base = {}
    out = dict(base)
    for key, val in patch.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def _rebuild_hardcoded_plan_flat(globals_dict: dict[str, Any]) -> None:
    hp = globals_dict.get("HARDCODED_PLAN")
    if not isinstance(hp, dict):
        return
    flat: dict[tuple[str, str, str], float] = {}
    for group, firms in hp.items():
        if not isinstance(firms, dict):
            continue
        for firm, crops in firms.items():
            if not isinstance(crops, dict):
                continue
            for crop, val in crops.items():
                try:
                    flat[(str(group), str(firm), str(crop))] = float(val)
                except (TypeError, ValueError):
                    flat[(str(group), str(firm), str(crop))] = 0.0
    globals_dict["HARDCODED_PLAN_FLAT"] = flat


def _rebuild_plan_11_ha(globals_dict: dict[str, Any]) -> None:
    manual_crops = globals_dict.get("_PLAN_11_MANUAL_CROPS")
    if not isinstance(manual_crops, set):
        manual_crops = {
            "Рапс озимый",
            "Пшеница озимая",
            "Тритикале озимая",
            "Многолетние травы",
            "Многолетние травы 2019 года",
            "Многолетние травы 2021 года",
            "Многолетние травы 2022 года",
            "Многолетние травы 2023 года",
            "Многолетние травы 2024 года",
            "Многолетние травы 2025 года",
        }
    hp = globals_dict.get("HARDCODED_PLAN")
    if not isinstance(hp, dict):
        return
    fert = hp.get("1.1 Внесение минеральных удобрений", {})
    if not isinstance(fert, dict):
        return
    out: dict[str, dict[str, float]] = {}
    for firm, crops in fert.items():
        if not isinstance(crops, dict):
            continue
        row: dict[str, float] = {}
        for crop, val in crops.items():
            if crop in manual_crops:
                try:
                    row[str(crop)] = float(val)
                except (TypeError, ValueError):
                    row[str(crop)] = 0.0
        if row:
            out[str(firm)] = row
    globals_dict["PLAN_11_HA"] = out


def _rebuild_plan_seeding_ha(globals_dict: dict[str, Any]) -> None:
    order = globals_dict.get("SEEDING_CROP_ORDER")
    meta = globals_dict.get("AGROFIRMS_META")
    hp = globals_dict.get("HARDCODED_PLAN")
    if not isinstance(order, tuple) or not isinstance(meta, dict) or not isinstance(hp, dict):
        return
    seeding = hp.get("4.0 Посев", {})
    if not isinstance(seeding, dict):
        return
    out: dict[str, dict[str, float | None]] = {}
    for firm in meta.keys():
        firm_crops = seeding.get(firm, {})
        if not isinstance(firm_crops, dict):
            firm_crops = {}
        row: dict[str, float | None] = {}
        for crop in order:
            val = firm_crops.get(crop)
            if val is None:
                row[str(crop)] = None
            else:
                try:
                    row[str(crop)] = float(val)
                except (TypeError, ValueError):
                    row[str(crop)] = None
        out[str(firm)] = row
    globals_dict["PLAN_SEEDING_HA"] = out


def _rebuild_own_aggregates(globals_dict: dict[str, Any]) -> None:
    avail = globals_dict.get("AVAILABLE_OWN")
    meta = globals_dict.get("AGROFIRMS_META")
    if not isinstance(avail, dict) or not isinstance(meta, dict):
        return
    fert = avail.get("1.1 Внесение минеральных удобрений", {})
    if not isinstance(fert, dict):
        return
    globals_dict["OWN_AGGREGATES"] = {
        str(firm): int(fert.get(firm, 0) or 0) for firm in meta.keys()
    }


def _rebuild_crop_display_rank(globals_dict: dict[str, Any]) -> None:
    """
    CROP_DISPLAY_RANK: группа → культура → ранг.
    0 = скрыть только в этой группе работ;
    1+ = порядок строк в этой группе (и общий PREFERRED как fallback).
    """
    ranks_by_group = globals_dict.get("CROP_DISPLAY_RANK")
    if not isinstance(ranks_by_group, dict) or not ranks_by_group:
        globals_dict["CROP_RANK_HIDDEN_SET"] = frozenset()
        globals_dict["CROP_GROUP_RANK_INDEX"] = {}
        return

    crop_to_rank: dict[str, int] = {}
    hidden_pairs: list[tuple[str, str]] = []
    group_index: dict[str, dict[str, int]] = {}

    for group, crops in ranks_by_group.items():
        if not isinstance(crops, dict):
            continue
        g_name = str(group)
        g_idx: dict[str, int] = {}
        for crop, raw in crops.items():
            try:
                rank = int(raw)
            except (TypeError, ValueError):
                continue
            name = str(crop).strip()
            if not name:
                continue
            if rank <= 0:
                hidden_pairs.append((g_name, name))
                continue
            g_idx[name.lower()] = rank
            prev = crop_to_rank.get(name)
            if prev is None or rank < prev:
                crop_to_rank[name] = rank
        if g_idx:
            group_index[g_name] = g_idx

    globals_dict["CROP_RANK_HIDDEN_SET"] = frozenset(hidden_pairs)
    globals_dict["CROP_GROUP_RANK_INDEX"] = group_index

    old_pref = [str(x) for x in (globals_dict.get("PREFERRED_CROP_ORDER") or [])]
    ranked = sorted(crop_to_rank.keys(), key=lambda c: (crop_to_rank[c], c.casefold()))
    rest = [c for c in old_pref if c not in crop_to_rank]
    # Культуры с рангом 0 в какой-то группе не выкидываем из общего PREFERRED —
    # скрытие действует только в своей группе (см. is_crop_rank_hidden).
    new_order = ranked + rest
    if new_order:
        globals_dict["PREFERRED_CROP_ORDER"] = new_order
        globals_dict["_PREFERRED_CROP_INDEX"] = {
            name.lower(): i for i, name in enumerate(new_order)
        }

    seeding_ranks = ranks_by_group.get("4.0 Посев")
    if isinstance(seeding_ranks, dict) and seeding_ranks:
        seeding_order: list[str] = []
        for crop, raw in sorted(
            seeding_ranks.items(),
            key=lambda kv: (
                int(kv[1]) if str(kv[1]).lstrip("-").isdigit() else 10_000,
                str(kv[0]).casefold(),
            ),
        ):
            try:
                rank = int(raw)
            except (TypeError, ValueError):
                continue
            if rank > 0:
                seeding_order.append(str(crop).strip())
        if seeding_order:
            globals_dict["SEEDING_CROP_ORDER"] = tuple(seeding_order)


def _rebuild_crop_firm_hidden(globals_dict: dict[str, Any]) -> None:
    """
    CROP_FIRM_HIDDEN: группа → фирма → культура → 1.
    Строим lookup-сет для фильтров сводок и убираем пары из HARDCODED_PLAN
    (чтобы план/строки по АФ не показывали культуру даже при факте).
    """
    hidden = globals_dict.get("CROP_FIRM_HIDDEN")
    if not isinstance(hidden, dict) or not hidden:
        globals_dict["CROP_FIRM_HIDDEN_SET"] = frozenset()
        globals_dict["CROP_FIRM_HIDDEN_PAIRS"] = ()
        return
    hp = globals_dict.get("HARDCODED_PLAN")
    if not isinstance(hp, dict):
        hp = {}
    pairs: list[tuple[str, str, str]] = []
    for group, firms in hidden.items():
        if not isinstance(firms, dict):
            continue
        g_hp = hp.get(group) if isinstance(hp.get(group), dict) else None
        for firm, crops in firms.items():
            if not isinstance(crops, dict):
                continue
            f_hp = g_hp.get(firm) if isinstance(g_hp, dict) and isinstance(g_hp.get(firm), dict) else None
            for crop, raw in crops.items():
                try:
                    flag = int(raw)
                except (TypeError, ValueError):
                    continue
                if flag <= 0:
                    continue
                name = str(crop).strip()
                if not name:
                    continue
                pairs.append((str(group), str(firm), name))
                if isinstance(f_hp, dict) and name in f_hp:
                    f_hp.pop(name, None)
    globals_dict["CROP_FIRM_HIDDEN_PAIRS"] = tuple(pairs)
    globals_dict["CROP_FIRM_HIDDEN_SET"] = frozenset(pairs)


def _rebuild_crop_report_mode(globals_dict: dict[str, Any]) -> None:
    """CROP_REPORT_MODE: группа → культура → 1 (иные) или 2 (не считать)."""
    raw = globals_dict.get("CROP_REPORT_MODE")
    other: list[tuple[str, str]] = []
    skip: list[tuple[str, str]] = []
    if isinstance(raw, dict):
        for group, crops in raw.items():
            if not isinstance(crops, dict):
                continue
            for crop, mode_raw in crops.items():
                try:
                    mode = int(mode_raw)
                except (TypeError, ValueError):
                    continue
                name = str(crop).strip()
                if not name:
                    continue
                pair = (str(group), name)
                if mode == 1:
                    other.append(pair)
                elif mode == 2:
                    skip.append(pair)
    globals_dict["CROP_OTHER_SET"] = frozenset(other)
    globals_dict["CROP_SKIP_REPORT_SET"] = frozenset(skip)


def _deserialize_work_groups(raw: Any) -> list[tuple[str, list[int]]]:
    """Формат редактора [{"name", "ids"}] → пары (название, [id]), как в plans.WORK_GROUPS.

    Повтор логики plans_editor._deserialize_work_groups: сводки делают
    ``for name, ids in WORK_GROUPS``, и список словарей ломает этот разбор.
    """
    if not isinstance(raw, list):
        return []
    out: list[tuple[str, list[int]]] = []
    for item in raw:
        name = ""
        ids_raw: Any = None
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            ids_raw = item.get("ids")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            name = str(item[0] or "").strip()
            ids_raw = item[1]
        if not name:
            continue
        ids: list[int] = []
        if isinstance(ids_raw, (list, tuple)):
            for x in ids_raw:
                try:
                    ids.append(int(x))
                except (TypeError, ValueError):
                    continue
        out.append((name, ids))
    return out


def _ids_of_group(groups: Any, group_name: str) -> set[int]:
    if not isinstance(groups, list):
        return set()
    out: set[int] = set()
    for item in groups:
        if not isinstance(item, (list, tuple)) or len(item) < 2 or item[0] != group_name:
            continue
        for x in item[1] or []:
            try:
                out.add(int(x))
            except (TypeError, ValueError):
                continue
    return out


def _replace_id_set(globals_dict: dict[str, Any], key: str, ids: set[int]) -> None:
    current = globals_dict.get(key)
    if isinstance(current, set):
        current.clear()
        current.update(ids)
        return
    globals_dict[key] = set(ids)


def _rebuild_work_group_id_sets(globals_dict: dict[str, Any]) -> None:
    """ALL_WT_IDS и VEG_WT_IDS — это группы «4.0» и «4.1», а не отдельный список."""
    groups = globals_dict.get("WORK_GROUPS")
    _replace_id_set(globals_dict, "ALL_WT_IDS", _ids_of_group(groups, "4.0 Посев"))
    _replace_id_set(globals_dict, "VEG_WT_IDS", _ids_of_group(groups, "4.1 Посев (овощи)"))


def _rebuild_derived(globals_dict: dict[str, Any]) -> None:
    _rebuild_crop_firm_hidden(globals_dict)
    _rebuild_hardcoded_plan_flat(globals_dict)
    _rebuild_plan_11_ha(globals_dict)
    _rebuild_crop_display_rank(globals_dict)
    _rebuild_crop_report_mode(globals_dict)
    _rebuild_plan_seeding_ha(globals_dict)
    _rebuild_own_aggregates(globals_dict)
    _rebuild_work_group_id_sets(globals_dict)


def apply_plans_edits_data(raw: dict[str, Any], globals_dict: dict[str, Any]) -> None:
    """То же, что apply_plans_edits, но из уже загруженного dict."""
    if not isinstance(raw, dict):
        return
    for key, patch in raw.items():
        if key.startswith("__"):
            continue
        if key == "WORK_GROUPS":
            groups = _deserialize_work_groups(patch)
            if groups:
                globals_dict[key] = groups
            continue
        current = globals_dict.get(key)
        if isinstance(patch, dict):
            globals_dict[key] = _deep_merge(current if isinstance(current, dict) else {}, patch)
        else:
            globals_dict[key] = patch
    _rebuild_derived(globals_dict)


def apply_plans_edits(json_path: str, globals_dict: dict[str, Any]) -> None:
    path = Path(json_path)
    if not path.is_file():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(raw, dict):
        return
    apply_plans_edits_data(raw, globals_dict)
