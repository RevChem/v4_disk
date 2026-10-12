"""Редактор планов: база из svodki/plan/plans.py.

Каждое сохранение сразу записывает эффективные значения в plans.py
(том ./svodki в Docker — это же локальный файл). JSON хранит ещё не
перенесённую разницу и при следующем чтении с ней сливается.
"""
from __future__ import annotations

from app.legacy.json_store_io import adopt_legacy_json, atomic_write_text, read_json_dict, serialized_rmw

import ast
import importlib.util
import json
import sys
import threading
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
    "CROP_REPORT_MODE",
    "OTHER_CROPS_LAYOUT",
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
    # plans.py сам применяет JSON из SVODKI_PLANS_EDITS_PATH при импорте. Для редактора это
    # «загрязняет» базу (правки считаются исходными и стираются при следующем сохранении),
    # поэтому отключаем: правки накладывает apply_edits_to_module / merge_plans.
    module._SVODKI_SKIP_RUNTIME_EDITS = True  # type: ignore[attr-defined]
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


_BASE_CACHE: dict[str, Any] = {"key": None, "value": None}
_BASE_CACHE_LOCK = threading.Lock()


def _base_cache_key() -> tuple[str, int, int] | None:
    plans_path = _svodki_plan_dir() / "plans.py"
    try:
        st = plans_path.stat()
    except OSError:
        return None
    return (str(plans_path), st.st_mtime_ns, st.st_size)


def extract_base_plans() -> dict[str, Any]:
    """База из plans.py. Кэшируется по mtime файла: plans.py исполняется только при изменении."""
    key = _base_cache_key()
    if key is not None:
        with _BASE_CACHE_LOCK:
            if _BASE_CACHE["key"] == key and _BASE_CACHE["value"] is not None:
                return deepcopy(_BASE_CACHE["value"])
    value = _extract_base_plans_uncached()
    if key is not None:
        with _BASE_CACHE_LOCK:
            _BASE_CACHE["key"] = key
            _BASE_CACHE["value"] = deepcopy(value)
    return value


def _extract_base_plans_uncached() -> dict[str, Any]:
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
    crops = getattr(mod, "ALL_REPORT_CROPS", ())
    work_group_names = [str(g["name"]) for g in base.get("WORK_GROUPS") or [] if isinstance(g, dict)]
    return {
        "sections": deepcopy(SECTION_DEFS),
        "work_groups": work_group_names,
        "firms": [str(x) for x in firms] if isinstance(firms, (list, tuple)) else [],
        "crops": [str(x) for x in crops] if isinstance(crops, (list, tuple)) else [],
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
    legacy = Path(settings.upload_dir).expanduser() / "plans_edits.json"
    adopt_legacy_json(path, legacy)
    raw = read_json_dict(path, label="plans_edits")
    if not raw:
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
    atomic_write_text(path, json.dumps(cleaned, ensure_ascii=False, indent=2))
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
        elif isinstance(e, dict) and e:
            effective[key] = deepcopy(e)
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
        elif section_key in {"AVAILABLE_OWN", "CROP_DISPLAY_RANK", "CROP_REPORT_MODE"}:
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
    """Как nested_3, но значения — int-флаги скрытия.

    1 храним, если в базе флага нет; 0 храним ТОЛЬКО если в базе стоит 1
    (иначе нельзя снять скрытие, заданное в plans.py — deep-merge ничего не удаляет).
    """
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
                    iv = 1 if int(val) > 0 else 0
                except (TypeError, ValueError):
                    continue
                base_v = base_crops.get(crop)
                try:
                    base_i = 1 if (base_v is not None and int(base_v) > 0) else 0
                except (TypeError, ValueError):
                    base_i = 0
                if base_i != iv:
                    f_out[str(crop)] = iv
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


@serialized_rmw(name="plans_edits")
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

    base = extract_base_plans()["base"]
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
    elif section_key == "CROP_REPORT_MODE":
        if len(keys) != 2:
            raise ValueError("Для CROP_REPORT_MODE нужны keys: [группа, культура]")
        g, crop = keys
        mode = int(round(float(value)))
        if mode not in (0, 1, 2):
            raise ValueError("Учёт культуры: 0 — считать, 1 — иные культуры, 2 — не считать")
        bucket = sec_eff.setdefault(g, {})
        base_bucket = (base.get("CROP_REPORT_MODE") or {}).get(g) or {}
        try:
            base_mode = int(base_bucket.get(crop) or 0)
        except (TypeError, ValueError):
            base_mode = 0
        # 0 в базе не хранится. Если в базе уже 2, «Считать» нужно записать нулём,
        # иначе правка сотрётся и культура снова не попадёт в сводки.
        if mode == 0 and base_mode == 0:
            bucket.pop(crop, None)
        else:
            bucket[crop] = mode
    elif section_key == "OTHER_CROPS_LAYOUT":
        if len(keys) != 1 or not str(keys[0]).strip():
            raise ValueError("Для вывода иных культур нужен keys: [группа работ]")
        g = str(keys[0]).strip()
        mode = int(round(float(value)))
        if mode not in (0, 1):
            raise ValueError("Вывод иных культур: 0 — одной строкой, 1 — по культурам")
        base_bucket = base.get("OTHER_CROPS_LAYOUT") or {}
        try:
            base_mode = int(base_bucket.get(g) or 0)
        except (TypeError, ValueError):
            base_mode = 0
        # 0 при пустой базе не пишем: площади и режимы культур не затрагиваются.
        if mode == 0 and base_mode == 0:
            sec_eff.pop(g, None)
        else:
            sec_eff[g] = mode
    elif section_key == "CROP_FIRM_HIDDEN":
        if len(keys) != 3:
            raise ValueError("Для CROP_FIRM_HIDDEN нужны keys: [группа, фирма, культура]")
        g, firm, crop = keys
        flag = 1 if int(round(float(value))) > 0 else 0
        # 0 сохраняем в effective: _prune_nested_3_int решит, нужен ли он (есть ли 1 в базе).
        sec_eff.setdefault(g, {}).setdefault(firm, {})[crop] = flag
    else:
        if len(keys) != 1:
            raise ValueError(f"Для {section_key} нужен keys: [ключ]")
        sec_eff[keys[0]] = float(value)

    effective[section_key] = sec_eff
    new_edits = _prune_edits_to_diff(base, effective)
    save_edits_store(new_edits)
    state = get_editor_state(report_date)
    return _state_with_plan_sync(state, report_date)


@serialized_rmw(name="plans_edits")
def set_work_groups(groups: list[dict[str, Any]], report_date: date | None = None) -> dict[str, Any]:
    normalized = _serialize_work_groups(groups)
    if not normalized:
        raise ValueError("Добавьте хотя бы одну группу работ.")
    seen_names: set[str] = set()
    for g in normalized:
        key = str(g["name"]).strip().casefold()
        if key in seen_names:
            raise ValueError(f"Название группы повторяется: «{g['name']}».")
        seen_names.add(key)
        g["ids"] = list(dict.fromkeys(int(x) for x in g["ids"]))
    base = extract_base_plans()["base"]
    edits = deepcopy(load_edits_store())
    effective = merge_plans(base, edits)
    locked = [
        str(g["name"])
        for g in _serialize_work_groups(base.get("WORK_GROUPS"))
        if str(g.get("name") or "").strip()
    ]
    present = {str(g["name"]) for g in normalized}
    missing = [name for name in locked if name not in present]
    if missing:
        shown = ", ".join(f"«{name}»" for name in missing)
        raise ValueError(
            "Нельзя переименовать или удалить группы, на которые ссылаются сводки: "
            f"{shown}. У этих групп можно менять только ID."
        )
    effective["WORK_GROUPS"] = normalized
    new_edits = _prune_edits_to_diff(base, effective)
    save_edits_store(new_edits)
    return _state_with_plan_sync(get_editor_state(report_date), report_date)


def _format_plan_number(value: Any) -> str:
    num = float(value)
    if abs(num - round(num)) < 1e-9:
        return str(int(round(num)))
    return f"{num:.6f}".rstrip("0").rstrip(".")


def _py_string(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _line_starts(source: str) -> list[int]:
    starts = [0]
    for idx, ch in enumerate(source):
        if ch == "\n":
            starts.append(idx + 1)
    return starts


def _utf8_col_to_char(line: str, col: int) -> int:
    """ast.col_offset — смещение в байтах UTF-8, не в символах."""
    raw = line.encode("utf-8")
    col = max(0, min(int(col), len(raw)))
    return len(raw[:col].decode("utf-8"))


def _abs_pos(source: str, starts: list[int], lineno: int, col: int) -> int:
    line_start = starts[lineno - 1]
    line = source[line_start:].split("\n", 1)[0]
    return line_start + _utf8_col_to_char(line, col)


def _indent_of_keys(node: ast.Dict, fallback: str) -> str:
    for key in node.keys:
        if isinstance(key, ast.Constant) and isinstance(key.value, str) and key.col_offset is not None:
            return " " * int(key.col_offset)
    return fallback


def _const_number(node: ast.AST) -> float | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return float(node.value)
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float))
    ):
        return -float(node.operand.value)
    return None


def _replace_assigned_literal(source: str, var_name: str, literal: str) -> str:
    """Меняет правую часть присваивания целиком. Так из файла уходят числа, которых нет в json."""
    tree = ast.parse(source)
    value = _find_assigned(tree, var_name)
    if value.end_lineno is None or value.end_col_offset is None:
        raise ValueError(f"В plans.py нет позиции у {var_name}")
    starts = _line_starts(source)
    start = _abs_pos(source, starts, value.lineno, value.col_offset)
    end = _abs_pos(source, starts, value.end_lineno, value.end_col_offset)
    text = source[:start] + literal + source[end:]
    ast.parse(text)
    return text


def _as_number(raw: Any, label: str) -> float:
    if isinstance(raw, bool) or raw is None:
        raise ValueError(f"{label}: нужно число.")
    if isinstance(raw, str):
        raw = raw.replace(",", ".").replace(" ", "").strip()
    try:
        num = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}: нужно число.") from exc
    if num != num or num in (float("inf"), float("-inf")):
        raise ValueError(f"{label}: нужно число.")
    return num


def _as_mode(raw: Any, label: str, allowed: tuple[int, ...]) -> int:
    num = int(round(_as_number(raw, label)))
    if num not in allowed:
        raise ValueError(f"{label}: допустимо {', '.join(str(x) for x in allowed)}.")
    return num


def _replace_group_map(
    current: dict[str, Any],
    operations: dict[str, Any],
    field: str,
    convert,
) -> dict[str, Any]:
    out = deepcopy(current) if isinstance(current, dict) else {}
    for name, op in operations.items():
        if not isinstance(op, dict) or field not in op:
            continue
        raw = op.get(field)
        if not isinstance(raw, dict):
            raise ValueError(f"«{name}»: {field} должен быть объектом.")
        out[str(name)] = convert(str(name), raw)
    return out


def _plan_group(group: str, raw: dict[str, Any]) -> dict[str, dict[str, float]]:
    firms: dict[str, dict[str, float]] = {}
    for firm, crops in raw.items():
        if not isinstance(crops, dict):
            raise ValueError(f"«{group}» / «{firm}»: план культуры должен быть объектом.")
        row: dict[str, float] = {}
        for crop, val in crops.items():
            row[str(crop)] = _as_number(val, f"«{group}» / «{firm}» / «{crop}»")
        firms[str(firm)] = row
    return firms


def _firm_ints(group: str, raw: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for firm, val in raw.items():
        out[str(firm)] = int(round(_as_number(val, f"«{group}» / «{firm}»")))
    return out


def _crop_ints(group: str, raw: dict[str, Any], *, allowed: tuple[int, ...] | None = None) -> dict[str, int]:
    out: dict[str, int] = {}
    for crop, val in raw.items():
        label = f"«{group}» / «{crop}»"
        out[str(crop)] = _as_mode(val, label, allowed) if allowed else int(round(_as_number(val, label)))
    return out


def _hidden_group(group: str, raw: dict[str, Any]) -> dict[str, dict[str, int]]:
    firms: dict[str, dict[str, int]] = {}
    for firm, crops in raw.items():
        if not isinstance(crops, dict):
            raise ValueError(f"«{group}» / «{firm}»: скрытие культур должно быть объектом.")
        row: dict[str, int] = {}
        for crop, val in crops.items():
            row[str(crop)] = 1 if int(round(_as_number(val, f"«{group}» / «{firm}» / «{crop}»"))) > 0 else 0
        firms[str(firm)] = row
    return firms


def _work_ids(raw: Any, group: str) -> list[int]:
    if not isinstance(raw, list):
        raise ValueError(f"«{group}»: work_type_ids должен быть списком.")
    ids: list[int] = []
    for item in raw:
        ids.append(int(round(_as_number(item, f"«{group}»: work_type id"))))
    return ids


def effective_from_agro_operations(effective: dict[str, Any], operations: dict[str, Any]) -> dict[str, Any]:
    """Подставляет в эффективные планы операции из agro_operations.json.

    Группа из файла заменяет свои гектары целиком. Группы, которых в файле нет, остаются.
    """
    eff = deepcopy(effective) if isinstance(effective, dict) else {}
    eff["HARDCODED_PLAN"] = _replace_group_map(eff.get("HARDCODED_PLAN") or {}, operations, "HARDCODED_PLAN", _plan_group)
    eff["AVAILABLE_OWN"] = _replace_group_map(eff.get("AVAILABLE_OWN") or {}, operations, "AVAILABLE_OWN", _firm_ints)
    eff["CROP_DISPLAY_RANK"] = _replace_group_map(
        eff.get("CROP_DISPLAY_RANK") or {}, operations, "CROP_DISPLAY_RANK", _crop_ints
    )
    eff["CROP_REPORT_MODE"] = _replace_group_map(
        eff.get("CROP_REPORT_MODE") or {},
        operations,
        "CROP_REPORT_MODE",
        lambda group, raw: _crop_ints(group, raw, allowed=(0, 1, 2)),
    )
    eff["CROP_FIRM_HIDDEN"] = _replace_group_map(
        eff.get("CROP_FIRM_HIDDEN") or {}, operations, "CROP_FIRM_HIDDEN", _hidden_group
    )
    layout = dict(eff.get("OTHER_CROPS_LAYOUT") or {})
    rates = dict(eff.get("PLAN_RATE_PER_GROUP") or {})
    groups = [dict(item) for item in (eff.get("WORK_GROUPS") or []) if isinstance(item, dict)]
    by_name = {str(item.get("name")): item for item in groups}
    for name, op in operations.items():
        if not isinstance(op, dict):
            raise ValueError(f"«{name}»: операция должна быть объектом.")
        key = str(name).strip()
        if not key:
            continue
        if "OTHER_CROPS_LAYOUT" in op:
            layout[key] = _as_mode(op.get("OTHER_CROPS_LAYOUT"), f"«{key}»: вывод иных культур", (0, 1))
        if "PLAN_RATE_PER_GROUP" in op and op.get("PLAN_RATE_PER_GROUP") is not None:
            rates[key] = _as_number(op.get("PLAN_RATE_PER_GROUP"), f"«{key}»: плановая выработка")
        if "work_type_ids" in op:
            ids = _work_ids(op.get("work_type_ids"), key)
            if key in by_name:
                by_name[key]["ids"] = ids
            else:
                item = {"name": key, "ids": ids}
                groups.append(item)
                by_name[key] = item
    eff["OTHER_CROPS_LAYOUT"] = layout
    eff["PLAN_RATE_PER_GROUP"] = rates
    eff["WORK_GROUPS"] = groups
    return eff


def _format_work_groups_literal(groups: list[Any]) -> str:
    lines = ["["]
    for item in groups:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        ids = _work_ids(item.get("ids") or [], name)
        lines.append(f"    ({_py_string(name)}, [{', '.join(str(i) for i in ids)}]),")
    lines.append("]")
    return "\n".join(lines)


def _write_imported_effective(effective: dict[str, Any]) -> str:
    """Пишет снимок в plans.py и очищает JSON-разницу, чтобы она не вернула старые числа."""
    path = _svodki_plan_dir() / "plans.py"
    try:
        source = path.read_text(encoding="utf-8")
        original = source
        sections = (
            "HARDCODED_PLAN",
            "AVAILABLE_OWN",
            "PLAN_RATE_PER_GROUP",
            "CROP_DISPLAY_RANK",
            "CROP_FIRM_HIDDEN",
            "OTHER_CROPS_LAYOUT",
        )
        for name in sections:
            data = effective.get(name)
            if not isinstance(data, dict):
                continue
            try:
                _find_assigned(ast.parse(source), name)
            except ValueError:
                if name == "OTHER_CROPS_LAYOUT" and data:
                    source, _n = _render_layout_dict(source, name, data)
                continue
            literal = _format_mapping_literal(data, "    ") if data else "{}"
            source = _replace_assigned_literal(source, name, literal)
        groups = effective.get("WORK_GROUPS")
        if isinstance(groups, list) and groups:
            source = _replace_assigned_literal(source, "WORK_GROUPS", _format_work_groups_literal(groups))
        plan = effective.get("HARDCODED_PLAN")
        if isinstance(plan, dict) and plan and not _source_contains_plan(source, plan):
            return "Не удалось записать plans.py: не все площади из json попали в файл."
        base_modes = (extract_base_plans().get("base") or {}).get("CROP_REPORT_MODE") or {}
        eff_modes = effective.get("CROP_REPORT_MODE") or {}
        if isinstance(base_modes, dict) and isinstance(eff_modes, dict):
            mode_diff = _prune_nested_2(base_modes, eff_modes)
            if mode_diff:
                source, _n = render_crop_report_mode_editor(source, mode_diff)
        if source != original:
            atomic_write_text(path, source)
        with _BASE_CACHE_LOCK:
            _BASE_CACHE["key"] = None
            _BASE_CACHE["value"] = None
        save_edits_store({})
    except OSError as exc:
        return f"Не удалось записать plans.py: {exc}"
    except (ValueError, SyntaxError) as exc:
        return f"Не удалось записать plans.py: {exc}"
    return "Планы записаны из json."


def _import_season_snapshot(payload: dict[str, Any], report_date: date | None) -> str:
    from .season_meta import set_calendar_seasons, set_season_range_override

    notes: list[str] = []
    calendar = payload.get("calendar_seasons")
    if isinstance(calendar, dict) and calendar:
        set_calendar_seasons(calendar, report_date=report_date)
        notes.append("календарь сезонов")
    operations = payload.get("operations") or {}
    saved = 0
    for name, op in operations.items():
        if not isinstance(op, dict) or "season_years" not in op:
            continue
        years = op.get("season_years") or []
        if not isinstance(years, list) or not years:
            continue
        ranges = op.get("year_ranges") if isinstance(op.get("year_ranges"), dict) else None
        set_season_range_override(
            str(name),
            season_years=[int(y) for y in years],
            year_ranges=ranges,
        )
        saved += 1
    if saved:
        notes.append(f"сезоны операций: {saved}")
    if not notes:
        return ""
    return " Также обновлены " + ", ".join(notes) + "."


@serialized_rmw(name="plans_edits")
def import_agro_operations(payload: dict[str, Any], report_date: date | None = None) -> dict[str, Any]:
    """Файл «Скачать json» становится текущими планами редактора и plans.py."""
    if not isinstance(payload, dict):
        raise ValueError("json должен быть объектом.")
    operations = payload.get("operations")
    if not isinstance(operations, dict) or not operations:
        raise ValueError(
            "В файле нет operations. Нужен agro_operations.json из кнопки «Скачать json»."
        )
    state = get_editor_state(report_date)
    effective = effective_from_agro_operations(state.get("effective") or {}, operations)
    note = _write_imported_effective(effective)
    if note.startswith("Не удалось"):
        state = get_editor_state(report_date)
        state["plans_py_note"] = note
        return state
    try:
        note += _import_season_snapshot(payload, report_date)
    except ValueError as exc:
        note += f" Планы записаны, сезоны нет: {exc}"
    state = get_editor_state(report_date)
    state["plans_py_note"] = note
    return state


def _find_assigned(tree: ast.AST, var_name: str) -> ast.AST:
    for node in tree.body:
        target = ""
        value: ast.AST | None = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
            value = node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target = node.targets[0].id
            value = node.value
        if target == var_name and value is not None:
            return value
    raise ValueError(f"В plans.py не найден {var_name}")


def _find_hardcoded_plan(tree: ast.AST, var_name: str = "HARDCODED_PLAN") -> ast.Dict:
    value = _find_assigned(tree, var_name)
    if isinstance(value, ast.Dict):
        return value
    raise ValueError(f"В plans.py не найден словарь {var_name}")


def render_hardcoded_plan_source(
    source: str,
    plan: dict[str, Any],
    var_name: str = "HARDCODED_PLAN",
) -> tuple[str, int]:
    """Подставляет площади плана в исходник plans.py. Комментарии и прочие секции не трогает."""
    tree = ast.parse(source)
    root = _find_hardcoded_plan(tree, var_name)
    starts = _line_starts(source)
    edits: list[tuple[int, int, str]] = []
    changed = 0

    def _queue_comma_after_last(node: ast.AST) -> None:
        """У последнего элемента словаря должна быть запятая, иначе вставка после него — SyntaxError."""
        values = getattr(node, "values", None)
        if not values:
            return
        last = values[-1]
        if last.end_lineno is None or last.end_col_offset is None:
            return
        pos = _abs_pos(source, starts, last.end_lineno, last.end_col_offset)
        i = pos
        while i < len(source) and source[i] != "}":
            if source[i] == ",":
                return  # запятая уже есть
            if source[i] == "#":  # комментарий до конца строки
                while i < len(source) and source[i] != "\n":
                    i += 1
                continue
            i += 1
        edits.append((pos, pos, ","))

    def _queue_before_close(node: ast.AST, block: str) -> None:
        if not block or node.end_lineno is None or node.end_col_offset is None:
            return
        payload = block if block.endswith("\n") else block + "\n"
        line = source.splitlines()[node.end_lineno - 1]
        if line.lstrip().startswith("}"):
            edits.append((starts[node.end_lineno - 1], starts[node.end_lineno - 1], payload))
        else:
            brace = _abs_pos(source, starts, node.end_lineno, node.end_col_offset) - 1
            edits.append((brace, brace, "\n" + payload))
        # Важно: после постановки вставки. При равных позициях (однострочный {"a": 1}) правки
        # применяются в порядке очереди, и запятая должна оказаться в тексте раньше блока.
        _queue_comma_after_last(node)

    groups: dict[str, ast.Dict] = {}
    for key, val in zip(root.keys, root.values):
        if isinstance(key, ast.Constant) and isinstance(key.value, str) and isinstance(val, ast.Dict):
            groups[str(key.value)] = val

    new_groups: list[tuple[str, dict[str, Any]]] = []
    group_indent = _indent_of_keys(root, "    ")
    for group, firms in plan.items():
        if not isinstance(firms, dict):
            continue
        group_node = groups.get(str(group))
        if group_node is None:
            new_groups.append((str(group), firms))
            continue
        firm_nodes: dict[str, ast.Dict] = {}
        for key, val in zip(group_node.keys, group_node.values):
            if isinstance(key, ast.Constant) and isinstance(key.value, str) and isinstance(val, ast.Dict):
                firm_nodes[str(key.value)] = val
        firm_indent = _indent_of_keys(group_node, group_indent + "    ")
        new_firms: list[tuple[str, dict[str, Any]]] = []
        for firm, crops in firms.items():
            if not isinstance(crops, dict):
                continue
            firm_node = firm_nodes.get(str(firm))
            if firm_node is None:
                new_firms.append((str(firm), crops))
                continue
            crop_nodes: dict[str, ast.AST] = {}
            for key, val in zip(firm_node.keys, firm_node.values):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    crop_nodes[str(key.value)] = val
            crop_indent = _indent_of_keys(firm_node, firm_indent + "    ")
            fresh: list[str] = []
            for crop, raw in crops.items():
                try:
                    num = float(raw)
                except (TypeError, ValueError):
                    continue
                current = crop_nodes.get(str(crop))
                if current is None:
                    fresh.append(f"{crop_indent}{_py_string(str(crop))}: {_format_plan_number(num)},")
                    changed += 1
                    continue
                old = _const_number(current)
                if old is None or current.end_lineno is None or current.end_col_offset is None:
                    continue
                if abs(old - num) < 1e-9:
                    continue
                start = _abs_pos(source, starts, current.lineno, current.col_offset)
                end = _abs_pos(source, starts, current.end_lineno, current.end_col_offset)
                edits.append((start, end, _format_plan_number(num)))
                changed += 1
            if fresh:
                _queue_before_close(firm_node, "\n".join(fresh) + "\n")
        if new_firms:
            lines: list[str] = []
            crop_indent = firm_indent + "    "
            for firm, crops in new_firms:
                lines.append(f"{firm_indent}{_py_string(firm)}: {{")
                for crop, raw in crops.items():
                    try:
                        num = float(raw)
                    except (TypeError, ValueError):
                        continue
                    lines.append(f"{crop_indent}{_py_string(str(crop))}: {_format_plan_number(num)},")
                    changed += 1
                lines.append(f"{firm_indent}}},")
            if lines:
                _queue_before_close(group_node, "\n".join(lines) + "\n")
    if new_groups:
        lines = []
        firm_indent = group_indent + "    "
        crop_indent = firm_indent + "    "
        for group, firms in new_groups:
            lines.append(f"{group_indent}{_py_string(group)}: {{")
            for firm, crops in firms.items():
                if not isinstance(crops, dict):
                    continue
                lines.append(f"{firm_indent}{_py_string(str(firm))}: {{")
                for crop, raw in crops.items():
                    try:
                        num = float(raw)
                    except (TypeError, ValueError):
                        continue
                    lines.append(f"{crop_indent}{_py_string(str(crop))}: {_format_plan_number(num)},")
                    changed += 1
                lines.append(f"{firm_indent}}},")
            lines.append(f"{group_indent}}},")
        if lines:
            _queue_before_close(root, "\n".join(lines) + "\n")

    text = source
    for start, end, repl in sorted(edits, key=lambda item: (item[0], item[1]), reverse=True):
        text = text[:start] + repl + text[end:]
    if text != source:
        ast.parse(text)
    return text, changed


def _source_contains_plan(
    source: str,
    plan: dict[str, Any],
    var_name: str = "HARDCODED_PLAN",
) -> bool:
    tree = ast.parse(source)
    root = _find_hardcoded_plan(tree, var_name)
    groups: dict[str, ast.Dict] = {}
    for key, val in zip(root.keys, root.values):
        if isinstance(key, ast.Constant) and isinstance(key.value, str) and isinstance(val, ast.Dict):
            groups[str(key.value)] = val
    for group, firms in plan.items():
        if not isinstance(firms, dict):
            continue
        group_node = groups.get(str(group))
        if group_node is None:
            return False
        firm_nodes: dict[str, ast.Dict] = {}
        for key, val in zip(group_node.keys, group_node.values):
            if isinstance(key, ast.Constant) and isinstance(key.value, str) and isinstance(val, ast.Dict):
                firm_nodes[str(key.value)] = val
        for firm, crops in firms.items():
            if not isinstance(crops, dict):
                continue
            firm_node = firm_nodes.get(str(firm))
            if firm_node is None:
                return False
            crop_nodes: dict[str, ast.AST] = {}
            for key, val in zip(firm_node.keys, firm_node.values):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    crop_nodes[str(key.value)] = val
            for crop, raw in crops.items():
                try:
                    num = float(raw)
                except (TypeError, ValueError):
                    continue
                old = _const_number(crop_nodes.get(str(crop)) or ast.Constant(value=None))
                if old is None or abs(old - num) >= 1e-9:
                    return False
    return True


def _count_numbers(raw: Any) -> int:
    if isinstance(raw, dict):
        return sum(_count_numbers(v) for v in raw.values())
    try:
        float(raw)
    except (TypeError, ValueError):
        return 0
    return 1


def _format_number_tree(key: str, raw: Any, key_indent: str) -> list[str]:
    if isinstance(raw, dict):
        inner = key_indent + "    "
        lines = [f"{key_indent}{_py_string(key)}: {{"]
        for child_key, child_val in raw.items():
            lines.extend(_format_number_tree(str(child_key), child_val, inner))
        lines.append(f"{key_indent}}},")
        return lines
    return [f"{key_indent}{_py_string(key)}: {_format_plan_number(float(raw))},"]


def _format_mapping_literal(data: dict[str, Any], key_indent: str) -> str:
    if not data:
        return "{}"
    lines = ["{"]
    for key, raw in data.items():
        if isinstance(raw, dict):
            inner = _format_mapping_literal(raw, key_indent + "    ")
            lines.append(f"{key_indent}{_py_string(str(key))}: {inner},")
        else:
            lines.append(
                f"{key_indent}{_py_string(str(key))}: {_format_plan_number(float(raw))},"
            )
    close_indent = key_indent[:-4] if key_indent.endswith("    ") else ""
    lines.append(f"{close_indent}}}")
    return "\n".join(lines)


def _apply_span_edits(source: str, edits: list[tuple[int, int, str]]) -> str:
    text = source
    for start, end, repl in sorted(edits, key=lambda item: (item[0], item[1]), reverse=True):
        text = text[:start] + repl + text[end:]
    if text != source:
        ast.parse(text)
    return text


def _queue_insert_before_close(
    source: str,
    starts: list[int],
    edits: list[tuple[int, int, str]],
    node: ast.AST,
    block: str,
    close_char: str,
) -> None:
    if not block or node.end_lineno is None or node.end_col_offset is None:
        return
    payload = block if block.endswith("\n") else block + "\n"
    line = source.splitlines()[node.end_lineno - 1]
    if line.lstrip().startswith(close_char):
        edits.append((starts[node.end_lineno - 1], starts[node.end_lineno - 1], payload))
    else:
        brace = _abs_pos(source, starts, node.end_lineno, node.end_col_offset) - 1
        edits.append((brace, brace, "\n" + payload))
    children = getattr(node, "values", None) or getattr(node, "elts", None) or []
    if not children:
        return
    last = children[-1]
    if last.end_lineno is None or last.end_col_offset is None:
        return
    pos = _abs_pos(source, starts, last.end_lineno, last.end_col_offset)
    i = pos
    while i < len(source) and source[i] != close_char:
        if source[i] == ",":
            return
        if source[i] == "#":
            while i < len(source) and source[i] != "\n":
                i += 1
            continue
        i += 1
    edits.append((pos, pos, ","))


def _render_layout_dict(source: str, var_name: str, data: dict[str, Any]) -> tuple[str, int]:
    """Пишет плоский словарь. Если имени ещё нет в plans.py — дописывает его в конец, не трогая площади."""
    tree = ast.parse(source)
    try:
        _find_hardcoded_plan(tree, var_name)
    except ValueError:
        literal = _format_mapping_literal({str(k): v for k, v in data.items()}, "    ")
        text = source.rstrip() + f"\n\n{var_name}: dict[str, int] = {literal}\n"
        ast.parse(text)
        return text, _count_numbers(data)
    return render_nested_number_dict(source, var_name, data)


def render_nested_number_dict(source: str, var_name: str, data: dict[str, Any]) -> tuple[str, int]:
    """Обновляет словарь чисел любой вложенности. Комментарии рядом не трогает."""
    tree = ast.parse(source)
    root = _find_hardcoded_plan(tree, var_name)
    starts = _line_starts(source)
    edits: list[tuple[int, int, str]] = []
    changed = 0

    def sync(node: ast.Dict, payload: dict[str, Any], key_indent: str) -> None:
        nonlocal changed
        existing: dict[str, ast.AST] = {}
        for key, val in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                existing[str(key.value)] = val
        fresh: list[str] = []
        for key, raw in payload.items():
            name = str(key)
            if isinstance(raw, dict):
                child = existing.get(name)
                if isinstance(child, ast.Dict):
                    sync(child, raw, _indent_of_keys(child, key_indent + "    "))
                else:
                    fresh.extend(_format_number_tree(name, raw, key_indent))
                    changed += _count_numbers(raw)
                continue
            try:
                num = float(raw)
            except (TypeError, ValueError):
                continue
            current = existing.get(name)
            if not isinstance(current, ast.AST):
                fresh.append(f"{key_indent}{_py_string(name)}: {_format_plan_number(num)},")
                changed += 1
                continue
            old = _const_number(current)
            if old is None or current.end_lineno is None or current.end_col_offset is None:
                continue
            if abs(old - num) < 1e-9:
                continue
            start = _abs_pos(source, starts, current.lineno, current.col_offset)
            end = _abs_pos(source, starts, current.end_lineno, current.end_col_offset)
            edits.append((start, end, _format_plan_number(num)))
            changed += 1
        if fresh:
            _queue_insert_before_close(source, starts, edits, node, "\n".join(fresh) + "\n", "}")

    sync(root, data, _indent_of_keys(root, "    "))
    return _apply_span_edits(source, edits), changed


def _source_contains_number_dict(source: str, var_name: str, data: dict[str, Any]) -> bool:
    tree = ast.parse(source)
    root = _find_hardcoded_plan(tree, var_name)

    def walk(node: ast.AST, payload: Any) -> bool:
        if isinstance(payload, dict):
            if not isinstance(node, ast.Dict):
                return False
            kids: dict[str, ast.AST] = {}
            for key, val in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    kids[str(key.value)] = val
            return all(str(key) in kids and walk(kids[str(key)], raw) for key, raw in payload.items())
        try:
            num = float(payload)
        except (TypeError, ValueError):
            return True
        old = _const_number(node)
        return old is not None and abs(old - num) < 1e-9

    return walk(root, data)


def _work_group_id_lists(source: str) -> dict[str, list[int]]:
    tree = ast.parse(source)
    root = _find_assigned(tree, "WORK_GROUPS")
    if not isinstance(root, ast.List):
        raise ValueError("WORK_GROUPS не список")
    out: dict[str, list[int]] = {}
    for elt in root.elts:
        if not isinstance(elt, ast.Tuple) or len(elt.elts) < 2:
            continue
        name_n, ids_n = elt.elts[0], elt.elts[1]
        if not (
            isinstance(name_n, ast.Constant)
            and isinstance(name_n.value, str)
            and isinstance(ids_n, ast.List)
        ):
            continue
        ids: list[int] = []
        for item in ids_n.elts:
            if isinstance(item, ast.Constant) and isinstance(item.value, int) and not isinstance(item.value, bool):
                ids.append(int(item.value))
        out[str(name_n.value)] = ids
    return out


def render_work_groups_source(source: str, groups: list[Any]) -> tuple[str, int]:
    """Обновляет ID групп работ и дописывает новые группы. Существующие названия не удаляет."""
    tree = ast.parse(source)
    root = _find_assigned(tree, "WORK_GROUPS")
    if not isinstance(root, ast.List):
        raise ValueError("WORK_GROUPS не список")
    starts = _line_starts(source)
    edits: list[tuple[int, int, str]] = []
    changed = 0
    existing: dict[str, ast.List] = {}
    for elt in root.elts:
        if not isinstance(elt, ast.Tuple) or len(elt.elts) < 2:
            continue
        name_n, ids_n = elt.elts[0], elt.elts[1]
        if (
            isinstance(name_n, ast.Constant)
            and isinstance(name_n.value, str)
            and isinstance(ids_n, ast.List)
        ):
            existing[str(name_n.value)] = ids_n
    fresh: list[str] = []
    for item in groups:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        ids: list[int] = []
        for raw in item.get("ids") or []:
            try:
                ids.append(int(raw))
            except (TypeError, ValueError):
                continue
        node = existing.get(name)
        if node is None:
            fresh.append(f"    ({_py_string(name)}, [{', '.join(str(i) for i in ids)}]),")
            changed += 1
            continue
        old = _work_group_id_lists(source).get(name)
        if old == ids or node.end_lineno is None or node.end_col_offset is None:
            continue
        start = _abs_pos(source, starts, node.lineno, node.col_offset)
        end = _abs_pos(source, starts, node.end_lineno, node.end_col_offset)
        edits.append((start, end, "[" + ", ".join(str(i) for i in ids) + "]"))
        changed += 1
    if fresh:
        _queue_insert_before_close(source, starts, edits, root, "\n".join(fresh) + "\n", "]")
    text = _apply_span_edits(source, edits)
    return text, changed


def _source_contains_work_groups(source: str, groups: list[Any]) -> bool:
    found = _work_group_id_lists(source)
    for item in groups:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        ids = []
        for raw in item.get("ids") or []:
            try:
                ids.append(int(raw))
            except (TypeError, ValueError):
                continue
        if found.get(name) != ids:
            return False
    return True


def render_crop_report_mode_editor(source: str, updates: dict[str, Any]) -> tuple[str, int]:
    """Дописывает точечные режимы культур в CROP_REPORT_MODE_EDITOR."""
    if not updates:
        return source, 0
    tree = ast.parse(source)
    node = _find_hardcoded_plan(tree, "CROP_REPORT_MODE_EDITOR")
    current = ast.literal_eval(ast.unparse(node))
    if not isinstance(current, dict):
        current = {}
    merged = _deep_merge(current, updates)
    if merged == current:
        return source, 0
    formatted = _format_mapping_literal(merged, "    ")
    starts = _line_starts(source)
    if node.end_lineno is None or node.end_col_offset is None:
        raise ValueError("CROP_REPORT_MODE_EDITOR без позиции")
    start = _abs_pos(source, starts, node.lineno, node.col_offset)
    end = _abs_pos(source, starts, node.end_lineno, node.end_col_offset)
    text = source[:start] + formatted + source[end:]
    ast.parse(text)
    return text, _count_numbers(updates)


def _sync_hardcoded_plan_note(effective: dict[str, Any]) -> str:
    """Пишет HARDCODED_PLAN из редактора в plans.py. JSON редактора не меняет."""
    plan = effective.get("HARDCODED_PLAN")
    if not isinstance(plan, dict):
        return "В редакторе нет площадей плана."
    path = _svodki_plan_dir() / "plans.py"
    try:
        source = path.read_text(encoding="utf-8")
        updated, changed = render_hardcoded_plan_source(source, plan)
        if not _source_contains_plan(updated, plan):
            return "Не удалось записать plans.py: не все площади попали в файл."
        if updated != source:
            atomic_write_text(path, updated)
        with _BASE_CACHE_LOCK:
            _BASE_CACHE["key"] = None
            _BASE_CACHE["value"] = None
    except OSError as exc:
        return f"Не удалось записать plans.py: {exc}"
    except (ValueError, SyntaxError) as exc:
        return f"Не удалось записать plans.py: {exc}"
    if changed:
        return f"plans.py обновлён: {changed} значений."
    return "plans.py уже совпадает с редактором."


_NUMBER_DICT_SECTIONS = (
    "AVAILABLE_OWN",
    "PLAN_RATE_PER_GROUP",
    "CROP_DISPLAY_RANK",
    "OTHER_CROPS_LAYOUT",
)


def _persist_editor_to_plans_py(effective: dict[str, Any]) -> str:
    """Пишет все секции редактора в plans.py. JSON редактора не меняет."""
    path = _svodki_plan_dir() / "plans.py"
    try:
        source = path.read_text(encoding="utf-8")
        original = source
        changed = 0
        plan = effective.get("HARDCODED_PLAN")
        if isinstance(plan, dict) and plan:
            source, n = render_hardcoded_plan_source(source, plan)
            changed += n
            if not _source_contains_plan(source, plan):
                return "Не удалось записать plans.py: не все площади попали в файл."
        for name in _NUMBER_DICT_SECTIONS:
            data = effective.get(name)
            if not isinstance(data, dict) or not data:
                continue
            if name == "OTHER_CROPS_LAYOUT":
                source, n = _render_layout_dict(source, name, data)
            else:
                source, n = render_nested_number_dict(source, name, data)
            changed += n
            if not _source_contains_number_dict(source, name, data):
                return f"Не удалось записать plans.py: секция {name} записана не полностью."
        hidden = effective.get("CROP_FIRM_HIDDEN")
        if isinstance(hidden, dict) and hidden:
            source, n = render_hardcoded_plan_source(source, hidden, "CROP_FIRM_HIDDEN")
            changed += n
            if not _source_contains_plan(source, hidden, "CROP_FIRM_HIDDEN"):
                return "Не удалось записать plans.py: скрытие культур записано не полностью."
        groups = effective.get("WORK_GROUPS")
        if isinstance(groups, list) and groups:
            source, n = render_work_groups_source(source, groups)
            changed += n
            if not _source_contains_work_groups(source, groups):
                return "Не удалось записать plans.py: группы работ записаны не полностью."
        mode_diff: dict[str, Any] = {}
        eff_modes = effective.get("CROP_REPORT_MODE")
        if isinstance(eff_modes, dict) and eff_modes:
            try:
                base_modes = (extract_base_plans().get("base") or {}).get("CROP_REPORT_MODE") or {}
            except Exception:
                base_modes = None
            if isinstance(base_modes, dict) and base_modes:
                mode_diff = _prune_nested_2(base_modes, eff_modes)
        if mode_diff:
            source, n = render_crop_report_mode_editor(source, mode_diff)
            changed += n
        if source != original:
            atomic_write_text(path, source)
        with _BASE_CACHE_LOCK:
            _BASE_CACHE["key"] = None
            _BASE_CACHE["value"] = None
    except OSError as exc:
        return f"Не удалось записать plans.py: {exc}"
    except (ValueError, SyntaxError) as exc:
        return f"Не удалось записать plans.py: {exc}"
    if changed:
        return f"plans.py обновлён: {changed} значений."
    return "plans.py уже совпадает с редактором."


def _state_with_plan_sync(state: dict[str, Any], report_date: date | None) -> dict[str, Any]:
    note = _persist_editor_to_plans_py(state.get("effective") or {})
    if not note.startswith("Не удалось"):
        state = get_editor_state(report_date)
    state["plans_py_note"] = note
    return state


@serialized_rmw(name="plans_edits")
def sync_hardcoded_plan_to_file(report_date: date | None = None) -> dict[str, Any]:
    """Кнопка «Синхронизация plans.py»: все секции редактора пишутся в plans.py, JSON не меняется."""
    state = get_editor_state(report_date)
    return _state_with_plan_sync(state, report_date)


def write_effective_plans_edits_for_generation(dest: Path) -> Path | None:
    edits = load_edits_store()
    if not edits:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(dest, json.dumps(edits, ensure_ascii=False))
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
