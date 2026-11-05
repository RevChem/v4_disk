from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from threading import Lock, Thread
import re

import logging

import requests

from app.config import MISSING_CROPWISE_TOKEN_DETAIL, get_cropwise_token, settings
from app.legacy.json_store_io import atomic_write_json, read_json_dict
from app.legacy.works_summary import (
    UNDEFINED_CROP,
    _build_field_firm,
    _capture_server_time,
    _changes_since,
    _endpoint_has_changes,
    _fmt_dt_param,
    _mass_request,
    crop_of_mapping_item,
)

logger = logging.getLogger(__name__)
CACHE_SCHEMA_VERSION = 1

# Excel «Поля с начала сезона» — как в standalone field.py (заголовки, стили, порядок колонок)
_FIELD_EXCEL_HEADERS: list[str] = [
    "Номер поля",
    "Площадь\nполя, га",
    "Культура",
    "Урожайность,\nц/га",
    "Название\nагрооперации",
    "Название ТМЦ\n(удобрение / СЗР / семена)",
    "Количество обраб.\nгектар",
    "Норма\nвнесения",
    "Факт\nвнесения",
    "Дата начала\nагрооперации",
    "Дата окончания\nагрооперации",
]
_FIELD_COL_W: list[int] = [14, 10, 18, 10, 30, 30, 12, 12, 12, 12, 12]
_C_FIELD_HDR = "4472C4"
_C_FIELD_TITLE = "D9E1F2"
_C_FIELD_TOTAL = "BDD7EE"
_C_FIELD_ODD = "FFFFFF"
_C_FIELD_EVEN = "F2F7FC"

SKIP_CROPS: set[str] = {
    "Залежь, лес", "Залежь", "Введение в оборот 2026 год",
    "Луговые травы", "Обмен", "В пользовании у фермера",
    "Введение в оборот 2027 год", "Введение в оборот",
    "Передано в аренду", "Дороги/лесополосы", "Овраги/неудобия", "crop_None",
}

_state_lock = Lock()
_is_running = False
_last_error = ""


def _pick_first(data: dict, keys: tuple[str, ...], default: str = "") -> str:
    for k in keys:
        v = data.get(k)
        if v not in (None, ""):
            return str(v)
    return default


def _pick_float(data: dict, keys: tuple[str, ...], default: float = 0.0) -> float:
    for k in keys:
        v = data.get(k)
        if v in (None, ""):
            continue
        try:
            return float(v)
        except Exception:
            continue
    return float(default)


def _to_float(value) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("value", "amount", "rate", "norm", "planned_rate", "fact_rate"):
            if key in value:
                nested = _to_float(value.get(key))
                if nested is not None:
                    return nested
        return None
    if isinstance(value, str):
        s = value.strip().replace(" ", "").replace(",", ".")
        if not s:
            return None
        try:
            return float(s)
        except Exception:
            return None
    return None


def _material_name_from_operation(op: dict) -> str:
    for key in ("material_name", "tmc_name", "product_name", "input_name"):
        v = op.get(key)
        if v not in (None, ""):
            return str(v)
    for list_key in ("materials", "material_applications", "consumptions", "inputs", "resources"):
        items = op.get(list_key)
        if not isinstance(items, list):
            continue
        names = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = _pick_first(item, ("name", "material_name", "tmc_name", "product_name", "input_name"))
            if name:
                names.append(name)
        if names:
            return ", ".join(names)
    return ""


def _yield_from_operation(op: dict) -> float | None:
    """Урожайность ц/га = harvested_weight (кг) / completed_area (га) / 100 — как _calc_yield в field.py."""
    try:
        hw = float(op.get("harvested_weight") or 0.0)
        area = float(op.get("completed_area") or 0.0)
        if hw > 0 and area > 0:
            return round(hw / area / 100.0, 2)
    except Exception:
        return None
    return None


def _mix_material_info(op: dict, material_map: dict[str, dict[int, str]]) -> tuple[str, float, float]:
    mix = op.get("application_mix_items") or []
    if not isinstance(mix, list) or not mix:
        return "", 0.0, 0.0
    names: list[str] = []
    planned_vals: list[float] = []
    fact_vals: list[float] = []
    for item in mix:
        if not isinstance(item, dict):
            continue
        mat_id   = item.get("applicable_id")
        mat_type = str(item.get("applicable_type") or "").strip()  # "Fertilizer" / "Chemical" / "Seed"
        mat_name = ""
        if mat_id is not None:
            try:
                mid = int(mat_id)
                # Ищем только в правильном справочнике по applicable_type
                type_map = material_map.get(mat_type) or {}
                mat_name = type_map.get(mid, "")
                # Fallback: перебрать все справочники если тип неизвестен
                if not mat_name and mat_type not in material_map:
                    for sub in material_map.values():
                        mat_name = sub.get(mid, "")
                        if mat_name:
                            break
            except (TypeError, ValueError):
                pass
        if not mat_name:
            mat_name = _pick_first(item, ("applicable_name", "name", "material_name", "tmc_name", "product_name"), "")
        if mat_name:
            names.append(str(mat_name))
        p = None
        for k in ("planned_rate", "planned_value", "norm_rate", "norm", "rate", "dosage"):
            p = _to_float(item.get(k))
            if p is not None:
                break
        f = None
        for k in ("fact_rate", "actual_rate", "value", "fact_value", "actual_value"):
            f = _to_float(item.get(k))
            if f is not None:
                break
        if p is not None:
            planned_vals.append(float(p))
        if f is not None:
            fact_vals.append(float(f))
    planned = round(sum(planned_vals) / len(planned_vals), 3) if planned_vals else 0.0
    fact = round(sum(fact_vals) / len(fact_vals), 3) if fact_vals else 0.0
    return ", ".join(dict.fromkeys(names)), planned, fact


def _mix_material_rows(op: dict, material_map: dict[str, dict[int, str]]) -> list[dict]:
    mix = op.get("application_mix_items") or []
    if not isinstance(mix, list) or not mix:
        return []

    out: list[dict] = []
    completed_area = float(op.get("completed_area") or 0.0)
    for item in mix:
        if not isinstance(item, dict):
            continue
        mat_id = item.get("applicable_id")
        mat_type = str(item.get("applicable_type") or "").strip()
        material_name = ""
        if mat_id is not None:
            try:
                mid = int(mat_id)
                type_map = material_map.get(mat_type) or {}
                material_name = type_map.get(mid, "")
                if not material_name and mat_type not in material_map:
                    for sub in material_map.values():
                        material_name = sub.get(mid, "")
                        if material_name:
                            break
            except (TypeError, ValueError):
                pass
        if not material_name:
            material_name = _pick_first(
                item,
                ("applicable_name", "name", "material_name", "tmc_name", "product_name"),
                "",
            )
        planned = None
        for k in ("planned_rate", "planned_value", "norm_rate", "norm", "rate", "dosage"):
            planned = _to_float(item.get(k))
            if planned is not None:
                break
        fact = None
        for k in ("fact_rate", "actual_rate", "value", "fact_value", "actual_value"):
            fact = _to_float(item.get(k))
            if fact is not None:
                break
        # 0 в custom_fact_area — это ноль гектаров, а не «поля нет, бери всю операцию».
        custom_area = item.get("custom_fact_area")
        if custom_area not in (None, ""):
            covered = _to_float(custom_area)
        else:
            covered = completed_area
        out.append(
            {
                "material_name": str(material_name or ""),
                "planned_rate": round(float(planned), 3) if planned is not None else 0.0,
                "fact_rate": round(float(fact), 3) if fact is not None else 0.0,
                "covered_area": float(covered) if covered is not None else 0.0,
            }
        )
    return out


def _normalize_firm_name(raw: str) -> str:
    s = " ".join(str(raw or "").strip().split())
    key = s.lower().replace("ё", "е")
    mapping = {
        "аф чистопольская": "АФ Чистопольская",
        "агрораздолье": "АгроРаздолье",
        "аксу-агро": "Аксу-Агро",
        "нурлат агро": "НурлатАгро",
        "нурлатагро": "НурлатАгро",
        "дрожжаное рассвет агро": "Дрожжаное Рассвет Агро",
        "актай": "Актай",
        "лениногорская птицефабрика": "Лениногорская Птицефабрика",
    }
    return mapping.get(key, s or "Без фирмы")


def _cache_path() -> Path:
    root = Path(settings.upload_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root / "field_season_summary_cache.json"


def _pdf_dir() -> Path:
    root = Path(settings.upload_dir) / "field-season-pdf"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _excel_dir() -> Path:
    root = Path(settings.upload_dir) / "field-season-excel"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _get_all(session: requests.Session, base_url: str, endpoint: str, params: dict | None = None) -> list[dict]:
    p = dict(params or {})
    results, from_id = [], 0
    for _ in range(500):
        r = session.get(
            f"{base_url}/{endpoint}",
            params={**p, "from_id": from_id, "limit": 1000},
            timeout=90,
        )
        r.raise_for_status()
        body = r.json()
        chunk = body.get("data", [])
        if not chunk:
            break
        results.extend(chunk)
        meta = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        if not last_id or meta.get("obtained_records", 0) < 100:
            break
        next_id = int(last_id) + 1
        if next_id <= from_id:
            break
        from_id = next_id
    return results


def _get_all_page(session: requests.Session, base_url: str, endpoint: str, params: dict | None = None) -> list[dict]:
    p = dict(params or {})
    p.setdefault("per_page", 1000)
    results, page = [], 1
    for _ in range(500):
        r = session.get(f"{base_url}/{endpoint}", params={**p, "page": page}, timeout=90)
        r.raise_for_status()
        body = r.json()
        chunk = body if isinstance(body, list) else body.get("data", [])
        results.extend(chunk)
        if len(chunk) < p["per_page"]:
            break
        page += 1
    return results


def _operation_count(rows: list[dict]) -> int:
    ids: list[int] = []
    for row in rows:
        oid = row.get("operation_id")
        if oid is None:
            continue
        try:
            ids.append(int(oid))
        except (TypeError, ValueError):
            continue
    return len(set(ids)) if ids else len(rows)


def _op_dates(op: dict) -> tuple[str, str]:
    start = str(op.get("actual_start_datetime") or op.get("planned_start_date") or "")[:10]
    end = str(op.get("completed_date") or op.get("planned_end_date") or "")[:10]
    return start, end


def _load_material_map(session: requests.Session, base_url: str) -> tuple[dict[str, dict[int, str]], list[str]]:
    material_map: dict[str, dict[int, str]] = {"Fertilizer": {}, "Chemical": {}, "Seed": {}}
    warnings: list[str] = []
    for ep, type_key in (("fertilizers", "Fertilizer"), ("chemicals", "Chemical"), ("seeds", "Seed")):
        try:
            items = _get_all(session, base_url, ep) or _get_all_page(session, base_url, ep)
        except Exception as exc:
            logger.warning("field_season: справочник %s не загружен", ep, exc_info=True)
            warnings.append(f"справочник {ep} не загружен: {exc}")
            items = []
        for item in items:
            try:
                mid = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            material_map[type_key][mid] = str(item.get("name") or item.get("title") or f"{ep}_{mid}")
    return material_map, warnings


def _history_for_operations(
    session: requests.Session,
    base_url: str,
    operations: list[dict],
    crop_map: dict[int, str],
) -> tuple[dict[int, str], dict[int, int], list[str]]:
    packed = [
        {"history_item_id": op.get("history_item_id"), "field_id": op.get("field_id")}
        for op in operations
    ]
    return _history_from_items(session, base_url, packed, crop_map)


def _history_from_items(
    session: requests.Session,
    base_url: str,
    items: list[dict],
    crop_map: dict[int, str],
) -> tuple[dict[int, str], dict[int, int], list[str]]:
    from app.legacy.works_summary import _history_maps_for_items

    return _history_maps_for_items(session, base_url, {0: items}, crop_map)


def _rows_for_operations(
    operations: list[dict],
    *,
    field_name_map: dict[int, str],
    field_area_map: dict[int, float],
    field_firm: dict[int, str],
    work_type_map: dict[int, str],
    material_map: dict[str, dict[int, str]],
    history_crop: dict[int, str],
    history_field: dict[int, int],
) -> dict[int, list[dict]]:
    by_field: dict[int, list[dict]] = defaultdict(list)
    for op in operations:
        fid = op.get("field_id")
        if not fid:
            continue
        try:
            fid_i = int(fid)
        except (TypeError, ValueError):
            continue
        crop = crop_of_mapping_item(op, history_crop, history_field)
        if crop in SKIP_CROPS:
            continue
        op = dict(op)
        op["_crop"] = crop
        by_field[fid_i].append(op)

    rows_by_field: dict[int, list[dict]] = defaultdict(list)
    for fid, ops in by_field.items():
        ops.sort(key=lambda o: (_date_sort_key(_op_dates(o)[0]), _date_sort_key(_op_dates(o)[1]), int(o.get("id") or 0)))
        for op in ops:
            start, end = _op_dates(op)
            wt_id = op.get("work_type_id")
            try:
                oid = int(op["id"]) if op.get("id") is not None else None
            except (TypeError, ValueError):
                oid = None
            hid = op.get("history_item_id")
            try:
                hid_i = int(hid) if hid is not None else None
            except (TypeError, ValueError):
                hid_i = None
            covered_raw = op.get("covered_area")
            if covered_raw in (None, ""):
                covered_raw = op.get("completed_area")
            own_yield = _yield_from_operation(op)
            base_row = {
                "operation_id": oid,
                "history_item_id": hid_i,
                "field_id": fid,
                "field_name": field_name_map.get(fid, f"field_{fid}"),
                "field_area": field_area_map.get(fid, 0.0),
                "crop": str(op.get("_crop") or UNDEFINED_CROP),
                "work_type": work_type_map.get(int(wt_id), f"wt_{wt_id}") if wt_id else "",
                "yield_value": 0.0,
                "op_yield": float(own_yield) if own_yield else 0.0,
                "firm_name": field_firm.get(fid) or "Без фирмы",
                "completed_area": float(op.get("completed_area") or 0.0),
                "covered_area": float(covered_raw or 0.0),
                "date_start": start,
                "date_end": end,
                "folder_id": None,
            }
            mix_rows = _mix_material_rows(op, material_map)
            if mix_rows:
                for mix_row in mix_rows:
                    row = dict(base_row)
                    row.update(mix_row)
                    rows_by_field[fid].append(row)
            else:
                row = dict(base_row)
                row["material_name"] = _material_name_from_operation(op)
                row["fact_rate"] = _pick_float(
                    op,
                    ("fact_rate", "actual_rate", "actual_application_rate", "application_rate_fact"),
                    0.0,
                )
                row["planned_rate"] = _pick_float(
                    op,
                    (
                        "planned_rate",
                        "norm_rate",
                        "application_rate_norm",
                        "application_rate_plan",
                        "rate",
                        "dosage",
                        "norm",
                    ),
                    0.0,
                )
                rows_by_field[fid].append(row)
        _restamp_field_yield(rows_by_field[fid])
    return rows_by_field


def _restamp_field_yield(rows: list[dict]) -> None:
    """Одна урожайность поля: первая по дате начала среди операций с урожайностью > 0."""
    ordered = sorted(
        rows,
        key=lambda row: (_date_sort_key(row.get("date_start")), _date_sort_key(row.get("date_end"))),
    )
    found = 0.0
    for row in ordered:
        try:
            y = float(row.get("op_yield") or 0)
        except (TypeError, ValueError):
            y = 0.0
        if y > 0:
            found = y
            break
    for row in rows:
        row["yield_value"] = found


def _field_catalog(fields_all: list[dict]) -> tuple[dict[int, str], dict[int, float]]:
    names: dict[int, str] = {}
    areas: dict[int, float] = {}
    for field in fields_all:
        try:
            fid = int(field["id"])
        except (KeyError, TypeError, ValueError):
            continue
        names[fid] = field.get("name") or field.get("title") or f"field_{fid}"
        areas[fid] = float(field.get("tillable_area") or field.get("calculated_area") or 0.0)
    return names, areas


def _build_rows() -> dict:
    token = get_cropwise_token()
    if not token:
        raise ValueError(MISSING_CROPWISE_TOKEN_DETAIL)

    base_url = (settings.cropwise_base_url or "").strip() or "https://operations.cropwise.com/api/v3"
    season_year = int(settings.cropwise_season_year or date.today().year)

    session = requests.Session()
    session.headers.update({"X-User-Api-Token": token})

    from datetime import datetime as _dt

    last_synced_at = _capture_server_time(session, base_url, _fmt_dt_param(_dt.now(timezone.utc)))
    fields_all = _get_all(session, base_url, "fields")
    fgs_all = _get_all(session, base_url, "field_groups")
    gfs_all = _get_all(session, base_url, "group_folders") or _get_all_page(session, base_url, "group_folders")
    crops_raw = _get_all(session, base_url, "crops") or _get_all_page(session, base_url, "crops")
    wt_raw = _get_all(session, base_url, "work_types") or _get_all_page(session, base_url, "work_types")
    material_map, warnings = _load_material_map(session, base_url)
    all_ops_raw = _get_all(session, base_url, "agro_operations", {"season": str(season_year)})
    all_ops = [op for op in all_ops_raw if str(op.get("status") or "").strip().lower() != "canceled"]

    field_name_map, field_area_map = _field_catalog(fields_all)
    field_firm = _build_field_firm(fields_all, fgs_all, gfs_all)
    crop_map = {
        int(c["id"]): str(c.get("name") or c.get("title") or "")
        for c in crops_raw
        if c.get("id") is not None
    }
    history_crop, history_field, history_warnings = _history_for_operations(session, base_url, all_ops, crop_map)
    warnings.extend(history_warnings)
    work_type_map: dict[int, str] = {
        int(w["id"]): (w.get("name") or w.get("title") or f"wt_{w['id']}")
        for w in wt_raw
        if w.get("id") is not None
    }
    rows_by_field = _rows_for_operations(
        all_ops,
        field_name_map=field_name_map,
        field_area_map=field_area_map,
        field_firm=field_firm,
        work_type_map=work_type_map,
        material_map=material_map,
        history_crop=history_crop,
        history_field=history_field,
    )
    fields = [
        {"field_id": fid, "field_name": field_name_map.get(fid, f"field_{fid}")}
        for fid in sorted(rows_by_field.keys(), key=lambda x: field_name_map.get(x, ""))
    ]
    return {
        "cache_version": CACHE_SCHEMA_VERSION,
        "created_at": date.today().isoformat(),
        "season_year": season_year,
        "last_synced_at": last_synced_at,
        "warnings": warnings,
        "fields": fields,
        "rows_by_field": {str(fid): rows for fid, rows in rows_by_field.items()},
    }


def load_cache() -> dict | None:
    data = read_json_dict(_cache_path(), label="field_season")
    if data.get("season_year") is None and not data.get("rows_by_field"):
        return None
    return data


def generate_cache() -> dict:
    payload = _build_rows()
    atomic_write_json(_cache_path(), payload)
    return payload


def _field_reference_changed(session: requests.Session, base_url: str, from_time: str) -> bool:
    for endpoint in (
        "history_items",
        "fields",
        "field_groups",
        "group_folders",
        "crops",
        "fertilizers",
        "chemicals",
        "seeds",
        "work_types",
    ):
        if _endpoint_has_changes(session, base_url, endpoint, from_time, None):
            logger.info("field_season: изменился справочник %s — полный пересчёт", endpoint)
            return True
    return False


def update_cache_incremental() -> dict:
    """Обновляет агрооперации по /changes. Смена культуры, фирмы или справочника ТМЦ
    пересобирает кэш целиком: эти правки не отмечены на самой операции."""
    cache = load_cache()
    season_year = int(settings.cropwise_season_year or date.today().year)
    ready = bool(
        cache
        and int(cache.get("cache_version") or 0) >= CACHE_SCHEMA_VERSION
        and cache.get("last_synced_at")
        and int(cache.get("season_year") or 0) == season_year
        and isinstance(cache.get("rows_by_field"), dict)
    )
    if not ready:
        return generate_cache()
    assert cache is not None

    token = get_cropwise_token()
    if not token:
        raise ValueError(MISSING_CROPWISE_TOKEN_DETAIL)
    base_url = (settings.cropwise_base_url or "").strip() or "https://operations.cropwise.com/api/v3"
    session = requests.Session()
    session.headers.update({"X-User-Api-Token": token})
    from_time = str(cache.get("last_synced_at"))
    try:
        if _field_reference_changed(session, base_url, from_time):
            return generate_cache()
    except Exception:
        logger.warning("field_season: проверка справочников не удалась, полный пересчёт", exc_info=True)
        return generate_cache()

    changed, server_time = _changes_since(session, base_url, "agro_operations", from_time)
    new_synced = server_time or from_time
    changed_ids: set[int] = set()
    for op in changed:
        oid = op.get("id")
        if oid is not None:
            try:
                changed_ids.add(int(oid))
            except (TypeError, ValueError):
                continue
    if not changed_ids:
        updated = dict(cache)
        updated["last_synced_at"] = new_synced
        atomic_write_json(_cache_path(), updated)
        return updated

    fresh = _mass_request(session, base_url, "agro_operations", sorted(changed_ids))
    fresh_by_id = {}
    for op in fresh:
        if op.get("id") is None:
            continue
        try:
            fresh_by_id[int(op["id"])] = op
        except (TypeError, ValueError):
            continue
    live_ops = []
    for op in fresh_by_id.values():
        if str(op.get("status") or "").strip().lower() == "canceled":
            continue
        season = op.get("season")
        if season not in (None, "") and str(season) != str(season_year):
            continue
        live_ops.append(op)
    fields_all = _get_all(session, base_url, "fields")
    fgs_all = _get_all(session, base_url, "field_groups")
    gfs_all = _get_all(session, base_url, "group_folders") or _get_all_page(session, base_url, "group_folders")
    crops_raw = _get_all(session, base_url, "crops") or _get_all_page(session, base_url, "crops")
    wt_raw = _get_all(session, base_url, "work_types") or _get_all_page(session, base_url, "work_types")
    material_map, warnings = _load_material_map(session, base_url)
    field_name_map, field_area_map = _field_catalog(fields_all)
    field_firm = _build_field_firm(fields_all, fgs_all, gfs_all)
    crop_map = {
        int(c["id"]): str(c.get("name") or c.get("title") or "")
        for c in crops_raw
        if c.get("id") is not None
    }
    history_crop, history_field, history_warnings = _history_for_operations(session, base_url, live_ops, crop_map)
    warnings.extend(history_warnings)
    work_type_map = {
        int(w["id"]): (w.get("name") or w.get("title") or f"wt_{w['id']}")
        for w in wt_raw
        if w.get("id") is not None
    }
    replacement = _rows_for_operations(
        live_ops,
        field_name_map=field_name_map,
        field_area_map=field_area_map,
        field_firm=field_firm,
        work_type_map=work_type_map,
        material_map=material_map,
        history_crop=history_crop,
        history_field=history_field,
    )
    kept: dict[str, list[dict]] = {}
    for key, rows in (cache.get("rows_by_field") or {}).items():
        if not isinstance(rows, list):
            continue
        stayed = []
        for row in rows:
            try:
                oid = int(row.get("operation_id"))
            except (TypeError, ValueError):
                stayed.append(row)
                continue
            if oid not in changed_ids:
                fid = row.get("field_id")
                try:
                    fid_i = int(fid)
                except (TypeError, ValueError):
                    stayed.append(row)
                    continue
                row = dict(row)
                row["firm_name"] = field_firm.get(fid_i) or row.get("firm_name") or "Без фирмы"
                if fid_i in field_name_map:
                    row["field_name"] = field_name_map[fid_i]
                    row["field_area"] = field_area_map.get(fid_i, row.get("field_area"))
                stayed.append(row)
        if stayed:
            kept[str(key)] = stayed
    for fid, rows in replacement.items():
        kept.setdefault(str(fid), []).extend(rows)
    for rows in kept.values():
        _restamp_field_yield(rows)
    fields = [
        {"field_id": int(fid), "field_name": field_name_map.get(int(fid), f"field_{fid}")}
        for fid in sorted(kept.keys(), key=lambda x: field_name_map.get(int(x), ""))
        if str(fid).isdigit()
    ]
    payload = {
        "cache_version": CACHE_SCHEMA_VERSION,
        "created_at": cache.get("created_at") or date.today().isoformat(),
        "season_year": season_year,
        "last_synced_at": new_synced,
        "warnings": list(cache.get("warnings") or []) + warnings,
        "fields": fields,
        "rows_by_field": kept,
    }
    atomic_write_json(_cache_path(), payload)
    return payload


def _resolve_rows(payload: dict, field_id: int | None = None, field_ids: list[int] | None = None) -> tuple[list[dict], str, bool]:
    rows_by_field = payload.get("rows_by_field", {}) or {}
    selected_ids = [int(x) for x in (field_ids or []) if int(x) > 0]
    if selected_ids:
        rows = []
        fields_map = {
            int(item.get("field_id") or 0): str(item.get("field_name") or "")
            for item in (payload.get("fields") or [])
        }
        for fid in selected_ids:
            rows.extend(rows_by_field.get(str(fid), []))
            if fid not in fields_map:
                raise ValueError(f"Поле {fid} не найдено в готовой сводке.")
        title = "Выбранные поля"
        return rows, title, False

    is_all = field_id in (None, 0)
    if is_all:
        rows = []
        for fid_rows in rows_by_field.values():
            if isinstance(fid_rows, list):
                rows.extend(fid_rows)
        rows = sorted(
            rows,
            key=lambda x: (
                str(x.get("field_name") or ""),
                _date_sort_key(x.get("date_start")),
                _date_sort_key(x.get("date_end")),
                str(x.get("work_type") or ""),
                str(x.get("material_name") or ""),
            ),
        )
        return rows, "Общая_сводка", True

    rows = rows_by_field.get(str(field_id), [])
    field_name = ""
    for item in payload.get("fields") or []:
        if int(item.get("field_id") or 0) == int(field_id):
            field_name = str(item.get("field_name") or "")
            break
    if not field_name:
        raise ValueError("Поле не найдено в готовой сводке.")
    return rows, field_name, False


def _date_sort_key(raw: str) -> tuple[int, datetime]:
    s = str(raw or "").strip()[:10]
    if not s:
        return (1, datetime.max)
    try:
        return (0, datetime.fromisoformat(s))
    except Exception:
        return (1, datetime.max)


def _fmt_date_ru(raw) -> str:
    if not raw:
        return ""
    s = str(raw).strip()[:10]
    if not s:
        return ""
    try:
        return date.fromisoformat(s).strftime("%d.%m.%Y")
    except ValueError:
        return s


def _first_yield_by_field_name(rows: list[dict]) -> dict[str, float | None]:
    """Первая по хронологии урожайность (ц/га) > 0 для каждого поля — как в field.py."""
    by_f: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        fn = str(r.get("field_name") or "")
        if fn:
            by_f[fn].append(r)
    out: dict[str, float | None] = {}
    for fn, lst in by_f.items():
        lst.sort(
            key=lambda x: (
                _date_sort_key(x.get("date_start")),
                _date_sort_key(x.get("date_end")),
            )
        )
        found: float | None = None
        for r in lst:
            y = float(r.get("yield_value") or 0)
            if y > 0:
                found = y
                break
        out[fn] = found
    return out


def _output_stem(field_id: int | None = None, field_ids: list[int] | None = None) -> str:
    selected = [int(x) for x in (field_ids or []) if int(x) > 0]
    if selected:
        return f"field_selected_{len(selected)}"
    if field_id in (None, 0):
        return "field_all"
    return f"field_{int(field_id)}"


def build_field_pdf(field_id: int | None = None, field_ids: list[int] | None = None) -> tuple[Path, str]:
    payload = load_cache()
    if not payload:
        raise ValueError("Сводка по полям ещё не создана.")

    rows, field_name, is_all = _resolve_rows(payload, field_id=field_id, field_ids=field_ids)
    rows = sorted(
        rows,
        key=lambda x: (
            str(x.get("field_name") or ""),
            _date_sort_key(x.get("date_start")),
            _date_sort_key(x.get("date_end")),
        ),
    )
    if not rows:
        raise ValueError("Для выбранного поля нет данных для PDF.")
    season_year = payload.get("season_year") or ""
    yield_by_field = _first_yield_by_field_name(rows)
    n_fields = len({int(r.get("field_id") or 0) for r in rows if int(r.get("field_id") or 0) > 0})
    if n_fields == 0:
        n_fields = len({str(r.get("field_name") or "") for r in rows if str(r.get("field_name") or "").strip()})
    n_ops = _operation_count(rows)

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A3, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

    font_name = "Helvetica"
    for fpath in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ):
        p = Path(fpath)
        if p.exists():
            pdfmetrics.registerFont(TTFont("FieldSeasonDejaVu", str(p)))
            font_name = "FieldSeasonDejaVu"
            break

    out_path = _pdf_dir() / f"{_output_stem(field_id, field_ids)}.pdf"

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=landscape(A3),
        leftMargin=10 * mm,
        rightMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
    )
    styles = getSampleStyleSheet()
    title_style = styles["Heading2"].clone("fs_title")
    title_style.fontName = font_name
    body_style = styles["BodyText"].clone("fs_body")
    body_style.fontName = font_name
    cell_style = styles["BodyText"].clone("fs_cell")
    cell_style.fontName = font_name
    cell_style.fontSize = 6.5
    cell_style.leading = 7.4
    header_style = styles["BodyText"].clone("fs_hdr")
    header_style.fontName = font_name
    header_style.fontSize = 7
    header_style.leading = 8

    header = [
        Paragraph("Номер поля", header_style),
        Paragraph("Площадь<br/>поля, га", header_style),
        Paragraph("Культура", header_style),
        Paragraph("Урожайность,<br/>ц/га", header_style),
        Paragraph("Название<br/>агрооперации", header_style),
        Paragraph("Название ТМЦ<br/>(удобрение / СЗР / семена)", header_style),
        Paragraph("Количество обраб.<br/>гектар", header_style),
        Paragraph("Норма<br/>внесения", header_style),
        Paragraph("Факт<br/>внесения", header_style),
        Paragraph("Дата начала<br/>агрооперации", header_style),
        Paragraph("Дата окончания<br/>агрооперации", header_style),
    ]
    table_rows = [header]

    def _p(v: str) -> Paragraph:
        txt = str(v or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return Paragraph(txt, cell_style)

    prev_fn = ""
    for row in rows:
        fn = str(row.get("field_name") or "")
        is_first = fn != prev_fn
        prev_fn = fn
        yv = yield_by_field.get(fn) if is_first else None
        y_txt = f"{float(yv):.2f}" if yv is not None else ""
        fa = float(row.get("field_area") or 0)
        cov = float(row.get("covered_area") or 0)
        pr = float(row.get("planned_rate") or 0)
        fr = float(row.get("fact_rate") or 0)
        table_rows.append(
            [
                _p(fn if is_first else ""),
                _p(f"{fa:.2f}" if is_first and fa else ""),
                _p(str(row.get("crop") or "") if is_first else ""),
                _p(y_txt),
                _p(str(row.get("work_type") or "")),
                _p(str(row.get("material_name") or "")),
                _p(f"{cov:.2f}" if cov else ""),
                _p(f"{pr:.3f}" if pr else ""),
                _p(f"{fr:.3f}" if fr else ""),
                _p(_fmt_date_ru(row.get("date_start"))),
                _p(_fmt_date_ru(row.get("date_end"))),
            ]
        )

    table = Table(
        table_rows,
        repeatRows=1,
        colWidths=[14 * mm, 10 * mm, 18 * mm, 10 * mm, 28 * mm, 32 * mm, 12 * mm, 12 * mm, 12 * mm, 14 * mm, 14 * mm],
    )
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), font_name),
                ("FONTSIZE", (0, 0), (-1, -1), 6.5),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4472C4")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("ALIGN", (0, 0), (-1, 0), "CENTER"),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (1, 1), (1, -1), "RIGHT"),
                ("ALIGN", (3, 1), (3, -1), "RIGHT"),
                ("ALIGN", (6, 1), (8, -1), "RIGHT"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F7FC")]),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )

    title_text = (
        f"Поля с начала сезона {season_year}"
        if is_all
        else f"Поля с начала сезона {season_year} — {field_name}"
    )
    story = [
        Paragraph(title_text, title_style),
        Spacer(1, 4 * mm),
        Paragraph(f"Полей: {n_fields}   Операций: {n_ops}", body_style),
        Spacer(1, 4 * mm),
        table,
    ]
    doc.build(story)
    return out_path, field_name


def build_field_excel(field_id: int | None = None, field_ids: list[int] | None = None) -> tuple[Path, str]:
    payload = load_cache()
    if not payload:
        raise ValueError("Сводка по полям ещё не создана.")

    rows, field_name, is_all = _resolve_rows(payload, field_id=field_id, field_ids=field_ids)
    if not rows:
        raise ValueError("Для выбранного поля нет данных для Excel.")

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    out_path = _excel_dir() / f"{_output_stem(field_id, field_ids)}.xlsx"
    season_year = payload.get("season_year") or ""

    n_hdr = len(_FIELD_EXCEL_HEADERS)
    last_col_letter = get_column_letter(n_hdr)

    def _sanitize_sheet_title(name: str) -> str:
        cleaned = re.sub(r'[:\\/*?\[\]]+', "_", str(name or "")).strip()
        return (cleaned or "Лист")[:31]

    def _write_sheet(ws, firm_title: str, sheet_rows: list[dict]) -> None:
        for ci, w in enumerate(_FIELD_COL_W, start=1):
            ws.column_dimensions[get_column_letter(ci)].width = w

        thin = Side(style="thin", color="AAAAAA")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)

        def _cell(
            row: int,
            col: int,
            value=None,
            *,
            bold: bool = False,
            bg: str | None = None,
            color: str = "000000",
            halign: str = "center",
            wrap: bool = True,
            fmt: str | None = None,
            size: int = 10,
        ) -> None:
            cell = ws.cell(row=row, column=col, value=value)
            cell.font = Font(name="Calibri", size=size, bold=bold, color=color)
            cell.alignment = Alignment(horizontal=halign, vertical="center", wrap_text=wrap)
            cell.border = border
            if bg:
                cell.fill = PatternFill(fill_type="solid", fgColor=bg)
            if fmt:
                cell.number_format = fmt

        row = 1
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=n_hdr)
        c = ws.cell(row=row, column=1, value=f"Поля с начала сезона {season_year} — {firm_title}")
        c.font = Font(name="Calibri", size=13, bold=True)
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.fill = PatternFill(fill_type="solid", fgColor=_C_FIELD_TITLE)
        c.border = border
        ws.row_dimensions[row].height = 22
        row += 1

        for ci, h in enumerate(_FIELD_EXCEL_HEADERS, start=1):
            _cell(row, ci, h, bold=True, bg=_C_FIELD_HDR, color="FFFFFF", halign="center", wrap=True, size=10)
        ws.row_dimensions[row].height = 38
        hdr_row = row
        row += 1
        ws.freeze_panes = f"A{row}"

        sorted_rows = sorted(
            sheet_rows,
            key=lambda x: (
                str(x.get("field_name") or ""),
                _date_sort_key(x.get("date_start")),
                _date_sort_key(x.get("date_end")),
            ),
        )
        yield_by_field = _first_yield_by_field_name(sorted_rows)

        field_parity: dict[str, int] = {}
        parity_counter = 0
        prev_fn = ""
        last_data_row = hdr_row

        for rdict in sorted_rows:
            fn = str(rdict.get("field_name") or "")
            is_first = fn != prev_fn
            prev_fn = fn
            if is_first and fn:
                if fn not in field_parity:
                    field_parity[fn] = parity_counter % 2
                    parity_counter += 1
            bg_idx = field_parity.get(fn, 0)
            bg = _C_FIELD_ODD if bg_idx == 0 else _C_FIELD_EVEN

            fa = float(rdict.get("field_area") or 0)
            cov = float(rdict.get("covered_area") or 0)
            pr = float(rdict.get("planned_rate") or 0)
            fr = float(rdict.get("fact_rate") or 0)
            fy = yield_by_field.get(fn) if is_first else None

            vals: list = [
                fn if is_first else None,
                fa if is_first else None,
                str(rdict.get("crop") or "") if is_first else None,
                fy,
                str(rdict.get("work_type") or ""),
                str(rdict.get("material_name") or ""),
                cov if cov else None,
                pr if pr else None,
                fr if fr else None,
                _fmt_date_ru(rdict.get("date_start")),
                _fmt_date_ru(rdict.get("date_end")),
            ]

            for ci, v in enumerate(vals, start=1):
                fmt = None
                if ci == 2 and isinstance(v, float):
                    fmt = "# ##0.00"
                elif ci == 4 and isinstance(v, float):
                    fmt = "# ##0.00"
                elif ci == 7 and isinstance(v, (int, float)):
                    fmt = "# ##0.00"
                elif ci in (8, 9) and isinstance(v, (int, float)):
                    fmt = "# ##0.000"
                halign = "left" if ci in (1, 3, 5, 6) else "center"
                _cell(row, ci, v, bg=bg, halign=halign, wrap=False, fmt=fmt)
            ws.row_dimensions[row].height = 16
            last_data_row = row
            row += 1

        n_fields = len(
            {
                int(r.get("field_id") or 0)
                for r in sheet_rows
                if int(r.get("field_id") or 0) > 0
            }
        )
        if n_fields == 0:
            n_fields = len({str(r.get("field_name") or "") for r in sheet_rows if str(r.get("field_name") or "").strip()})
        n_ops = _operation_count(sorted_rows)

        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=n_hdr)
        _cell(
            row,
            1,
            f"Полей: {n_fields}   Операций: {n_ops}",
            bold=True,
            bg=_C_FIELD_TOTAL,
            halign="left",
            wrap=False,
        )
        for ci in range(2, n_hdr + 1):
            _cell(row, ci, None, bg=_C_FIELD_TOTAL, wrap=False)

        if last_data_row > hdr_row:
            ws.auto_filter.ref = f"A{hdr_row}:{last_col_letter}{last_data_row}"

    wb = Workbook()
    if is_all:
        rows_by_firm: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            key = _normalize_firm_name(str(row.get("firm_name") or ""))
            rows_by_firm[key].append(row)

        sorted_keys = sorted(rows_by_firm.keys())
        first_key = sorted_keys[0]
        ws = wb.active
        ws.title = _sanitize_sheet_title(first_key)
        _write_sheet(ws, first_key, rows_by_firm[first_key])
        for key in sorted_keys[1:]:
            nws = wb.create_sheet(title=_sanitize_sheet_title(key))
            _write_sheet(nws, key, rows_by_firm[key])
    else:
        ws = wb.active
        ws.title = _sanitize_sheet_title(field_name or "Поля_с_начала_сезона")
        _write_sheet(ws, field_name or "Поле", rows)

    wb.save(out_path)
    return out_path, field_name


def field_generation_status() -> dict:
    with _state_lock:
        return {"running": bool(_is_running), "error": str(_last_error or "")}


def generate_cache_async() -> None:
    global _is_running, _last_error
    with _state_lock:
        if _is_running:
            return
        _is_running = True
        _last_error = ""

    def _worker() -> None:
        global _is_running, _last_error
        try:
            update_cache_incremental()
        except Exception as exc:
            with _state_lock:
                _last_error = str(exc)
        finally:
            with _state_lock:
                _is_running = False

    Thread(target=_worker, daemon=True, name="field-season-generator").start()