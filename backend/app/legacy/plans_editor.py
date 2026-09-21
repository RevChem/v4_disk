"""Редактор планов: база из svodki/plan/plans.py, правки только в JSON."""
from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

from app.config import get_cropwise_token, settings

EDITABLE_KEYS: tuple[str, ...] = (
    "HARDCODED_PLAN",
    "AVAILABLE_OWN",
    "PLAN_RATE_PER_GROUP",
    "WORK_GROUPS",
    "CROP_DISPLAY_RANK",
    "CROP_FIRM_HIDDEN",
)

LIST_EDITABLE_KEYS: frozenset[str] = frozenset({"WORK_GROUPS"})

SECTION_DEFS: list[dict[str, str]] = [
    {
        "key": "CALENDAR_SEASONS",
        "label": "Сезоны",
        "kind": "calendar_seasons",
    },
    {
        "key": "HARDCODED_PLAN",
        "label": "Площади план (га): группа работ → фирма → культура",
        "kind": "nested_3",
    },
    {
        "key": "AVAILABLE_OWN",
        "label": "Собственные агрегаты (шт.): группа работ → фирма",
        "kind": "nested_2",
    },
    {
        "key": "PLAN_RATE_PER_GROUP",
        "label": "Плановая выработка (га/сут) по группам работ",
        "kind": "flat",
    },
    {
        "key": "WORK_GROUPS",
        "label": "Группы работ Cropwise (WORK_GROUPS → work_type ID)",
        "kind": "work_groups",
    },
]


def _svodki_plan_dir() -> Path:
    root = Path(settings.svodki_root).expanduser()
    if not root.is_absolute():
        root = (Path.cwd() / root).resolve()
    return root / "plan"


def _ensure_plan_sys_path() -> None:
    plan_dir = _svodki_plan_dir()
    if str(plan_dir) not in sys.path:
        sys.path.insert(0, str(plan_dir))
    paths_mod = plan_dir / "plan_paths.py"
    if paths_mod.is_file():
        spec = importlib.util.spec_from_file_location("svodki_plan_paths", paths_mod)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.ensure_plan_sys_path()


def _load_plans_module():
    _ensure_plan_sys_path()
    plans_path = _svodki_plan_dir() / "plans.py"
    if not plans_path.is_file():
        raise FileNotFoundError(f"Не найден {plans_path}")
    spec = importlib.util.spec_from_file_location("svodki_plans_editor", plans_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Не удалось загрузить {plans_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def _serialize_work_groups(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        name = ""
        ids: list[int] = []
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            ids_raw = item.get("ids") or []
            if isinstance(ids_raw, list):
                for x in ids_raw:
                    try:
                        ids.append(int(x))
                    except (TypeError, ValueError):
                        continue
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            name = str(item[0] or "").strip()
            ids_raw = item[1]
            if isinstance(ids_raw, list):
                for x in ids_raw:
                    try:
                        ids.append(int(x))
                    except (TypeError, ValueError):
                        continue
        if name:
            out.append({"name": name, "ids": ids})
    return out


def _deserialize_work_groups(raw: Any) -> list[tuple[str, list[int]]]:
    out: list[tuple[str, list[int]]] = []
    for item in _serialize_work_groups(raw):
        out.append((str(item["name"]), [int(x) for x in item.get("ids") or []]))
    return out


def _work_groups_equal(a: Any, b: Any) -> bool:
    return _serialize_work_groups(a) == _serialize_work_groups(b)


class WorkTypesUnavailableError(RuntimeError):
    """Справочник work_types недоступен (нет токена или ошибка Cropwise)."""


def fetch_work_types_catalog() -> list[dict[str, Any]]:
    import requests

    token = get_cropwise_token()
    if not token:
        raise WorkTypesUnavailableError(
            "Не задан токен Cropwise (CROPWISE_API_TOKEN) — справочник work_types недоступен."
        )
    base_url = (settings.cropwise_base_url or "").strip() or "https://operations.cropwise.com/api/v3"
    session = requests.Session()
    session.headers.update({"X-User-Api-Token": token})
    try:
        r = session.get(f"{base_url}/work_types", params={"limit": 1000}, timeout=60)
        r.raise_for_status()
        body = r.json()
    except Exception as exc:
        raise WorkTypesUnavailableError(f"Cropwise work_types: {exc}") from exc
    items = body.get("data", []) if isinstance(body, dict) else body
    if not isinstance(items, list):
        raise WorkTypesUnavailableError("Cropwise work_types: неожиданный формат ответа.")
    out: list[dict[str, Any]] = []
    for wt in items:
        try:
            wt_id = int(wt.get("id"))
        except (TypeError, ValueError, AttributeError):
            continue
        name = str(wt.get("name") or wt.get("title") or wt.get("label") or "").strip()
        out.append({"id": wt_id, "name": name or f"work_type_{wt_id}"})
    out.sort(key=lambda x: int(x["id"]))
    return out


def extract_base_plans() -> dict[str, Any]:
    mod = _load_plans_module()
    base: dict[str, Any] = {}
    for key in EDITABLE_KEYS:
        val = getattr(mod, key, None)
        if key in LIST_EDITABLE_KEYS:
            if val is not None:
                base[key] = _serialize_work_groups(val)
        elif val is not None:
            base[key] = _json_safe(val)
    firms = getattr(mod, "FIRM_ORDER", None)
    work_group_names = [str(g["name"]) for g in base.get("WORK_GROUPS") or [] if isinstance(g, dict)]
    return {
        "sections": deepcopy(SECTION_DEFS),
        "work_groups": work_group_names,
        "firms": [str(x) for x in firms] if isinstance(firms, list) else [],
        "base": base,
    }


def _resolve_edits_path() -> Path:
    configured = (getattr(settings, "svodki_plans_edits_path", None) or "").strip()
    if configured:
        return Path(configured).expanduser()
    upload = Path(settings.upload_dir).expanduser()
    if not upload.is_absolute():
        upload = (Path.cwd() / upload).resolve()
    return upload / "plans_edits.json"


def plans_edits_path() -> Path:
    return _resolve_edits_path()


def load_edits_store() -> dict[str, Any]:
    path = _resolve_edits_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key in EDITABLE_KEYS:
        val = raw.get(key)
        if key in LIST_EDITABLE_KEYS:
            if isinstance(val, list) and val:
                out[key] = _serialize_work_groups(val)
        elif isinstance(val, dict) and val:
            out[key] = val
    return out


def save_edits_store(data: dict[str, Any]) -> Path:
    path = _resolve_edits_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    cleaned: dict[str, Any] = {}
    for key in EDITABLE_KEYS:
        val = data.get(key)
        if key in LIST_EDITABLE_KEYS:
            if isinstance(val, list) and val:
                cleaned[key] = _serialize_work_groups(val)
        elif isinstance(val, dict) and val:
            cleaned[key] = val
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


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


def merge_plans(base: dict[str, Any], edits: dict[str, Any]) -> dict[str, Any]:
    effective: dict[str, Any] = {}
    for key in EDITABLE_KEYS:
        b = base.get(key)
        e = edits.get(key)
        if key in LIST_EDITABLE_KEYS:
            if isinstance(e, list) and e:
                effective[key] = _serialize_work_groups(e)
            elif isinstance(b, list):
                effective[key] = deepcopy(b)
            continue
        if isinstance(b, dict):
            effective[key] = _deep_merge(deepcopy(b), e if isinstance(e, dict) else {})
        elif b is not None:
            effective[key] = deepcopy(b)
    return effective


def _values_equal(a: Any, b: Any) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 1e-9
        except (TypeError, ValueError):
            return False
    return a == b


def _prune_edits_to_diff(base: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for section_key in EDITABLE_KEYS:
        sec_cand = candidate.get(section_key)
        sec_base = base.get(section_key)
        if section_key in LIST_EDITABLE_KEYS:
            if isinstance(sec_cand, list) and not _work_groups_equal(sec_base, sec_cand):
                out[section_key] = _serialize_work_groups(sec_cand)
            continue
        if not isinstance(sec_cand, dict):
            continue
        if section_key == "HARDCODED_PLAN":
            pruned = _prune_nested_3(sec_base if isinstance(sec_base, dict) else {}, sec_cand)
        elif section_key == "CROP_FIRM_HIDDEN":
            pruned = _prune_nested_3_int(sec_base if isinstance(sec_base, dict) else {}, sec_cand)
        elif section_key in {"AVAILABLE_OWN", "CROP_DISPLAY_RANK"}:
            pruned = _prune_nested_2(sec_base if isinstance(sec_base, dict) else {}, sec_cand)
        else:
            pruned = _prune_flat(sec_base if isinstance(sec_base, dict) else {}, sec_cand)
        if pruned:
            out[section_key] = pruned
    return out


def _prune_flat(base: dict[str, Any], cand: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in cand.items():
        if key not in base or not _values_equal(base.get(key), val):
            out[str(key)] = val
    return out


def _prune_nested_2(base: dict[str, Any], cand: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for g, firms in cand.items():
        if not isinstance(firms, dict):
            continue
        base_firms = base.get(g, {}) if isinstance(base.get(g), dict) else {}
        row: dict[str, Any] = {}
        for firm, val in firms.items():
            if firm not in base_firms or not _values_equal(base_firms.get(firm), val):
                row[str(firm)] = val
        if row:
            out[str(g)] = row
    return out


def _prune_nested_3(base: dict[str, Any], cand: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for g, firms in cand.items():
        if not isinstance(firms, dict):
            continue
        base_firms = base.get(g, {}) if isinstance(base.get(g), dict) else {}
        g_out: dict[str, Any] = {}
        for firm, crops in firms.items():
            if not isinstance(crops, dict):
                continue
            base_crops = base_firms.get(firm, {}) if isinstance(base_firms.get(firm), dict) else {}
            f_out: dict[str, Any] = {}
            for crop, val in crops.items():
                if crop not in base_crops or not _values_equal(base_crops.get(crop), val):
                    f_out[str(crop)] = val
            if f_out:
                g_out[str(firm)] = f_out
        if g_out:
            out[str(g)] = g_out
    return out


def _prune_nested_3_int(base: dict[str, Any], cand: dict[str, Any]) -> dict[str, Any]:
    """Как nested_3, но значения — int (флаги скрытия). 0 не храним."""
    out: dict[str, Any] = {}
    for g, firms in cand.items():
        if not isinstance(firms, dict):
            continue
        base_firms = base.get(g, {}) if isinstance(base.get(g), dict) else {}
        g_out: dict[str, Any] = {}
        for firm, crops in firms.items():
            if not isinstance(crops, dict):
                continue
            base_crops = base_firms.get(firm, {}) if isinstance(base_firms.get(firm), dict) else {}
            f_out: dict[str, Any] = {}
            for crop, val in crops.items():
                try:
                    iv = int(val)
                except (TypeError, ValueError):
                    continue
                if iv <= 0:
                    continue
                base_v = base_crops.get(crop)
                try:
                    base_i = int(base_v) if base_v is not None else 0
                except (TypeError, ValueError):
                    base_i = 0
                if base_i != iv:
                    f_out[str(crop)] = 1
            if f_out:
                g_out[str(firm)] = f_out
        if g_out:
            out[str(g)] = g_out
    return out


def get_editor_state(report_date: date | None = None) -> dict[str, Any]:
    from .season_meta import season_meta_for_groups

    extracted = extract_base_plans()
    base = extracted["base"]
    edits = load_edits_store()
    effective = merge_plans(base, edits)
    work_group_names = [str(g["name"]) for g in effective.get("WORK_GROUPS") or [] if isinstance(g, dict)]
    names = work_group_names or extracted.get("work_groups") or []
    end = report_date or date.today()
    return {
        **extracted,
        "work_groups": names,
        "edits": edits,
        "effective": effective,
        "path": str(_resolve_edits_path()),
        "has_edits": bool(edits),
        "season_meta": season_meta_for_groups(list(names), end),
    }


def set_plan_value(
    section_key: str,
    keys: list[str],
    value: Any,
    report_date: date | None = None,
) -> dict[str, Any]:
    section_key = str(section_key).strip()
    if section_key not in EDITABLE_KEYS:
        raise ValueError(f"Неизвестная секция: {section_key}")
    if section_key in LIST_EDITABLE_KEYS:
        raise ValueError(f"Секция {section_key} редактируется отдельным API (work-groups).")

    state = get_editor_state(report_date)
    base = state["base"]
    edits = deepcopy(load_edits_store())
    effective = merge_plans(base, edits)

    sec_eff = effective.setdefault(section_key, {})
    if section_key == "HARDCODED_PLAN":
        if len(keys) != 3:
            raise ValueError("Для HARDCODED_PLAN нужны keys: [группа, фирма, культура]")
        g, firm, crop = keys
        sec_eff.setdefault(g, {}).setdefault(firm, {})[crop] = float(value)
    elif section_key == "AVAILABLE_OWN":
        if len(keys) != 2:
            raise ValueError("Для AVAILABLE_OWN нужны keys: [группа, фирма]")
        g, firm = keys
        sec_eff.setdefault(g, {})[firm] = int(round(float(value)))
    elif section_key == "CROP_DISPLAY_RANK":
        if len(keys) != 2:
            raise ValueError("Для CROP_DISPLAY_RANK нужны keys: [группа, культура]")
        g, crop = keys
        sec_eff.setdefault(g, {})[crop] = int(round(float(value)))
    elif section_key == "CROP_FIRM_HIDDEN":
        if len(keys) != 3:
            raise ValueError("Для CROP_FIRM_HIDDEN нужны keys: [группа, фирма, культура]")
        g, firm, crop = keys
        flag = 1 if int(round(float(value))) > 0 else 0
        firm_map = sec_eff.setdefault(g, {}).setdefault(firm, {})
        if flag > 0:
            firm_map[crop] = 1
        else:
            firm_map.pop(crop, None)
            if not firm_map:
                sec_eff.get(g, {}).pop(firm, None)
            if isinstance(sec_eff.get(g), dict) and not sec_eff.get(g):
                sec_eff.pop(g, None)
    else:
        if len(keys) != 1:
            raise ValueError(f"Для {section_key} нужен keys: [ключ]")
        sec_eff[keys[0]] = float(value)

    effective[section_key] = sec_eff
    new_edits = _prune_edits_to_diff(base, effective)
    save_edits_store(new_edits)
    return get_editor_state(report_date)


def set_work_groups(groups: list[dict[str, Any]], report_date: date | None = None) -> dict[str, Any]:
    normalized = _serialize_work_groups(groups)
    if not normalized:
        raise ValueError("Добавьте хотя бы одну группу работ.")
    state = get_editor_state(report_date)
    base = state["base"]
    edits = deepcopy(load_edits_store())
    effective = merge_plans(base, edits)
    effective["WORK_GROUPS"] = normalized
    new_edits = _prune_edits_to_diff(base, effective)
    save_edits_store(new_edits)
    return get_editor_state(report_date)


def reset_plans_edits(section_key: str | None = None, report_date: date | None = None) -> dict[str, Any]:
    if section_key:
        key = str(section_key).strip()
        if key not in EDITABLE_KEYS:
            raise ValueError(f"Неизвестная секция: {key}")
        edits = load_edits_store()
        edits.pop(key, None)
        save_edits_store(edits)
    else:
        save_edits_store({})
    return get_editor_state(report_date)


def write_effective_plans_edits_for_generation(dest: Path) -> Path | None:
    edits = load_edits_store()
    if not edits:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(edits, ensure_ascii=False), encoding="utf-8")
    return dest


def apply_edits_to_module(module: Any, edits: dict[str, Any] | None = None) -> Any:
    """Применяет JSON-правки к уже загруженному модулю plans (in-memory)."""
    patch = edits if edits is not None else load_edits_store()
    if not patch:
        return module
    apply_path = _svodki_plan_dir() / "utils" / "season" / "_apply_plans_edits.py"
    spec = importlib.util.spec_from_file_location("svodki_apply_plans_edits_runtime", apply_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Не удалось загрузить {apply_path}")
    apply_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(apply_mod)
    apply_mod.apply_plans_edits_data(patch, module.__dict__)
    return module


def load_effective_plans_module():
    """plans.py + uploads/plans_edits.json — то, что реально видят сводки при генерации."""
    return apply_edits_to_module(_load_plans_module())


def inject_plans_edits_env(env: dict[str, str], output_dir: Path) -> None:
    """Пробрасывает правки редактора планов в subprocess (отчёты, prefetch)."""
    upload = Path(settings.upload_dir).expanduser()
    if not upload.is_absolute():
        upload = (Path.cwd() / upload).resolve()
    env["SVODKI_UPLOAD_DIR"] = str(upload)
    eff_plans = output_dir / f".plans_edits_{uuid.uuid4().hex}.json"
    if write_effective_plans_edits_for_generation(eff_plans) is not None:
        env["SVODKI_PLANS_EDITS_PATH"] = str(eff_plans)
    else:
        env.pop("SVODKI_PLANS_EDITS_PATH", None)
