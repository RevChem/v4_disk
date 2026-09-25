"""Карточка machine_task для вкладки Cropwise: исполнитель, техника, поле, топливо.

work.json даёт только строку отчёта. Здесь по номеру задания догружаются
machine_tasks, привязка к полям, машина, орудие и человек.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from collections import defaultdict
from typing import Any

_CACHE_TTL_SEC = 600
_CACHE_MAX = 4000
_cache: dict[int, tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()

# Расход в литрах. Части не складываем с итогом: итог уже их включает.
_FUEL_TOTAL_KEYS = (
    "fuel_consumption",
    "total_fuel_consumption",
    "fuel_used",
    "consumed_fuel",
)
_FUEL_PART_KEYS = (
    "fuel_consumption_in_fields",
    "fuel_consumption_on_fields",
    "fuel_consumption_on_road",
)
_FUEL_LEVEL_KEYS = ("fuel_level", "fuel_remaining")
_DRIVER_KEYS = ("driver_id", "operator_id", "worker_id", "user_id")


def detail_limit() -> int:
    try:
        return max(1, int(os.environ.get("PROVERKA_CROPWISE_DETAIL_LIMIT", "500")))
    except ValueError:
        return 500


def empty_card() -> dict[str, Any]:
    return {
        "driver": "",
        "machine": "",
        "implement": "",
        "field": "",
        "fuel": "",
        "fuel_l": None,
        "field_items": [],
    }


def _num(value: Any) -> float | None:
    if value is None or value is False or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_id(value: Any) -> int | None:
    if value is None or value is False or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean(text: str) -> str:
    return " ".join(text.split())


def person_name(user: dict[str, Any] | None) -> str:
    """В Cropwise ФИО обычно лежит в username («Джабаров Т.С.»), не в first/last name."""
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


def machine_label(machine: dict[str, Any] | None, machine_id: int | None) -> str:
    if not machine:
        return f"машина {machine_id}" if machine_id else ""
    name = _clean(str(
        machine.get("name") or machine.get("custom_name") or machine.get("model") or ""
    ))
    reg = _clean(str(
        machine.get("registration_number")
        or machine.get("inventory_number")
        or machine.get("reg_number")
        or ""
    ))
    if name and reg and reg not in name:
        return f"{name} {reg}"
    return name or reg or (f"машина {machine_id}" if machine_id else "")


def implement_label(implement: dict[str, Any] | None) -> str:
    if not implement:
        return ""
    name = _clean(str(implement.get("custom_name") or implement.get("name") or ""))
    if name:
        return name
    brand = _clean(str(implement.get("manufacturer") or ""))
    model = _clean(str(implement.get("model") or ""))
    return _clean(" ".join(p for p in (brand, model) if p))


def driver_id_of(task: dict[str, Any]) -> int | None:
    for key in _DRIVER_KEYS:
        found = _int_id(task.get(key))
        if found:
            return found
    return None


def fuel_of(task: dict[str, Any]) -> tuple[float | None, str]:
    """Расход в литрах — поле fuel_consumption карточки.

    fuel_consumption_on_road и средний расход по дороге в итог не входят:
    это срез и норма, а не полный расход задания. start/end_fuel_level —
    уровень бака в литрах, не процент и не расход.
    """
    if any(key in task for key in _FUEL_TOTAL_KEYS):
        for key in _FUEL_TOTAL_KEYS:
            total = _num(task.get(key))
            if total is not None:
                return total, f"{total:.1f} л"
        return None, ""
    parts = [n for key in _FUEL_PART_KEYS if (n := _num(task.get(key))) is not None]
    if not parts:
        extra = _num(task.get("additional_fuel_consumption"))
        if extra is not None:
            parts = [extra]
    if parts:
        total = sum(parts)
        return total, f"{total:.1f} л"
    scanned = 0.0
    found = False
    for key, val in task.items():
        low = str(key).lower()
        if "fuel" not in low or "consum" not in low:
            continue
        if any(skip in low for skip in ("level", "per_ha", "per_hour", "price", "cost", "average", "manual")):
            continue
        number = _num(val)
        if number is None:
            continue
        scanned += number
        found = True
    if found:
        return scanned, f"{scanned:.1f} л"
    for key in _FUEL_LEVEL_KEYS:
        level = _num(task.get(key))
        if level is not None:
            return None, f"остаток {round(level)}%"
    return None, ""


def field_of(
    items: list[dict[str, Any]] | None,
    field_names: dict[int, str],
) -> tuple[str, list[dict[str, Any]]]:
    by_field: dict[int, float] = defaultdict(float)
    for item in items or []:
        fid = _int_id(item.get("field_id"))
        if fid is None:
            continue
        by_field[fid] += float(item.get("covered_area") or 0)
    packed: list[dict[str, Any]] = []
    for fid, ha in sorted(by_field.items(), key=lambda kv: (-kv[1], kv[0])):
        packed.append({
            "name": field_names.get(fid) or f"поле {fid}",
            "ha": round(ha, 2),
        })
    labels: list[str] = []
    for item in packed:
        if item["ha"] > 0:
            labels.append(f"{item['name']} ({item['ha']:.2f} га)")
        else:
            labels.append(str(item["name"]))
    return "; ".join(labels), packed


def build_cards(
    tasks: list[dict[str, Any]],
    mtfmi_map: dict[int, list[dict[str, Any]]],
    machines: dict[int, dict[str, Any]],
    implements: dict[int, dict[str, Any]],
    field_names: dict[int, str],
    people: dict[int, str],
) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for task in tasks:
        tid = _int_id(task.get("id"))
        if tid is None:
            continue
        mid = _int_id(task.get("machine_id"))
        iid = _int_id(task.get("implement_id"))
        did = driver_id_of(task)
        fuel_l, fuel = fuel_of(task)
        field, field_items = field_of(mtfmi_map.get(tid), field_names)
        driver = people.get(did, "") if did else ""
        if did and not driver:
            driver = f"id {did}"
        out[tid] = {
            "driver": driver,
            "machine": machine_label(machines.get(mid) if mid else None, mid),
            "implement": implement_label(implements.get(iid) if iid else None),
            "field": field,
            "fuel": fuel,
            "fuel_l": fuel_l,
            "field_items": field_items,
        }
    return out


def apply_cards(rows: list[dict[str, Any]], cards: dict[int, dict[str, Any]]) -> None:
    keys = tuple(empty_card())
    for row in rows:
        tid = _int_id(row.get("task_id"))
        card = dict(empty_card())
        if tid is not None and tid in cards:
            card.update(cards[tid])
        for key in keys:
            row[key] = card.get(key)


def _index(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        rid = _int_id(row.get("id"))
        if rid is not None:
            out[rid] = row
    return out


def _people_from_rows(rows: list[dict[str, Any]]) -> dict[int, str]:
    out: dict[int, str] = {}
    for row in rows:
        rid = _int_id(row.get("id"))
        name = person_name(row)
        if rid and name and rid not in out:
            out[rid] = name
    return out


async def _load_people(client: Any, base_url: str, ids: list[int], sem: asyncio.Semaphore) -> dict[int, str]:
    from mtaomi_batch_fetch import mass_request_async

    found: dict[int, str] = {}
    pending = list(dict.fromkeys(ids))
    for endpoint in ("users", "workers", "operators"):
        if not pending:
            break
        try:
            rows = await mass_request_async(client, base_url, endpoint, pending, sem)
        except Exception:
            rows = []
        found.update(_people_from_rows(rows))
        pending = [i for i in pending if i not in found]
    return found


async def _fetch_async(task_ids: list[int]) -> dict[int, dict[str, Any]]:
    import httpx

    from plan_paths import ensure_plan_sys_path

    ensure_plan_sys_path()
    from mtaomi_batch_fetch import mass_request_async
    from mtfmi_batch_fetch import fetch_mtfmi_map_for_tasks
    from plans import API_TOKEN, BASE_URL

    if not API_TOKEN:
        raise RuntimeError("не задан токен API Cropwise")
    sem = asyncio.Semaphore(8)
    headers = {"X-User-Api-Token": API_TOKEN}
    async with httpx.AsyncClient(headers=headers, timeout=120) as client:
        task_rows = await mass_request_async(client, BASE_URL, "machine_tasks", task_ids, sem)
        if not task_rows:
            raise RuntimeError("Cropwise не вернул карточки заданий")
        machine_ids: list[int] = []
        implement_ids: list[int] = []
        driver_ids: list[int] = []
        for task in task_rows:
            mid = _int_id(task.get("machine_id"))
            iid = _int_id(task.get("implement_id"))
            did = driver_id_of(task)
            if mid:
                machine_ids.append(mid)
            if iid:
                implement_ids.append(iid)
            if did:
                driver_ids.append(did)
        mtfmi, machine_rows, implement_rows, people = await asyncio.gather(
            fetch_mtfmi_map_for_tasks(client, sem, BASE_URL, task_ids),
            mass_request_async(client, BASE_URL, "machines", machine_ids, sem),
            mass_request_async(client, BASE_URL, "implements", implement_ids, sem),
            _load_people(client, BASE_URL, driver_ids, sem),
        )
        field_ids = [
            fid
            for items in mtfmi.values()
            for item in items
            if (fid := _int_id(item.get("field_id")))
        ]
        field_rows = await mass_request_async(client, BASE_URL, "fields", field_ids, sem)
    field_names: dict[int, str] = {}
    for row in field_rows:
        fid = _int_id(row.get("id"))
        title = _clean(str(row.get("name") or row.get("title") or ""))
        if fid and title:
            field_names[fid] = title
    return build_cards(
        task_rows,
        mtfmi,
        _index(machine_rows),
        _index(implement_rows),
        field_names,
        people,
    )


def _fetch(task_ids: list[int]) -> dict[int, dict[str, Any]]:
    return asyncio.run(_fetch_async(task_ids))


def _trim_cache(now: float) -> None:
    stale = [k for k, (ts, _) in _cache.items() if now - ts > _CACHE_TTL_SEC]
    for key in stale:
        _cache.pop(key, None)
    if len(_cache) <= _CACHE_MAX:
        return
    overflow = len(_cache) - _CACHE_MAX
    for key, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[:overflow]:
        _cache.pop(key, None)


def cards_for(task_ids: list[int]) -> tuple[dict[int, dict[str, Any]], str | None]:
    """Карточки по id. Повтор в пределах 10 минут берётся из памяти."""
    if not task_ids:
        return {}, None
    now = time.time()
    out: dict[int, dict[str, Any]] = {}
    missing: list[int] = []
    with _lock:
        _trim_cache(now)
        for tid in task_ids:
            hit = _cache.get(tid)
            if hit and now - hit[0] <= _CACHE_TTL_SEC:
                out[tid] = hit[1]
            else:
                missing.append(tid)
    if not missing:
        return out, None
    try:
        fresh = _fetch(missing)
    except Exception as exc:  # noqa: BLE001 — ошибка уходит в интерфейс, строки отчёта остаются
        return out, f"Карточки заданий не загрузились: {exc}"
    now = time.time()
    with _lock:
        for tid in missing:
            card = fresh.get(tid) or empty_card()
            _cache[tid] = (now, card)
            out[tid] = card
        _trim_cache(now)
    return out, None


def enrich_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Дописывает карточки в строки отчёта. Пустая карточка — задание не открылось."""
    ids: list[int] = []
    seen: set[int] = set()
    for row in rows:
        tid = _int_id(row.get("task_id"))
        if tid is None or tid in seen:
            continue
        seen.add(tid)
        ids.append(tid)
    limit = detail_limit()
    fetch_ids = ids[:limit]
    note = ""
    if len(ids) > len(fetch_ids):
        note = (
            f"Карточки открыты у {len(fetch_ids)} из {len(ids)} заданий "
            "(сначала с большей площадью)."
        )
    cards, error = cards_for(fetch_ids)
    apply_cards(rows, cards)
    return {
        "details_error": error,
        "details_note": note,
        "details_loaded": len(cards),
    }
