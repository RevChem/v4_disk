# -*- coding: utf-8 -*-
"""Выработка исполнителя за период из локального хранилища заданий.

Читает store.sqlite (задания, гектары по сменным дням, связи с агрооперациями)
и refs.sqlite (поля, группы, папки, названия агроопераций). ФИО в этих файлах нет:
его подставляет вызывающий код по driver_id.
"""
from __future__ import annotations

import json
import sqlite3
import zlib
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_TZ = timezone(timedelta(hours=3))
_SHIFT_HOUR = 6
_MAX_DAYS = 366
_MAX_ROWS = 5000
_TASK_URL = "https://operations.cropwise.com/machines/{machine_id}/tasks/{task_id}"
_DRIVER_KEYS = ("driver_id", "operator_id", "worker_id", "user_id")


def _window(date_from: date, date_to: date) -> tuple[str, str, float, float]:
    if date_to < date_from:
        raise ValueError("Дата «по» раньше даты «с»")
    if (date_to - date_from).days > _MAX_DAYS:
        raise ValueError(f"Период не длиннее {_MAX_DAYS} дней")
    start = datetime(date_from.year, date_from.month, date_from.day, _SHIFT_HOUR, 0, tzinfo=_TZ)
    end = datetime(date_to.year, date_to.month, date_to.day, _SHIFT_HOUR, 0, tzinfo=_TZ) + timedelta(days=1)
    return date_from.isoformat(), date_to.isoformat(), start.timestamp(), end.timestamp()


def _shift_date(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=_TZ)
    d = dt.date() if dt.hour >= _SHIFT_HOUR else (dt - timedelta(days=1)).date()
    return d.isoformat()


def _int_or_none(value: Any) -> int | None:
    if value is None or value is False or value == "":
        return None
    try:
        found = int(value)
    except (TypeError, ValueError):
        return None
    return found or None


def _driver_id(data: dict) -> int | None:
    for key in _DRIVER_KEYS:
        found = _int_or_none(data.get(key))
        if found:
            return found
    return None


def _loads(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        body = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _operation_labels() -> dict[int, str]:
    """Вид работ → название группы сводок. Один id может входить в несколько групп."""
    try:
        from proverka.work_groups import work_group_wt_map

        groups = work_group_wt_map()
    except Exception:
        from plans import WORK_GROUPS

        groups = {name: list(ids) for name, ids in WORK_GROUPS}
    bucket: dict[int, list[str]] = {}
    for name, ids in groups.items():
        for raw in ids:
            wt = int(raw)
            names = bucket.setdefault(wt, [])
            if name not in names:
                names.append(name)
    return {wt: ", ".join(names) for wt, names in bucket.items()}


def _norm(text: str) -> str:
    return (text or "").strip().casefold().replace(" ", "").replace("\u00a0", "")


def _folder_parent_map(group_folders: list[dict]) -> dict[int, int | None]:
    parent: dict[int, int | None] = {}
    for gf in group_folders:
        fid = _int_or_none(gf.get("id"))
        if fid is None:
            continue
        pid = gf.get("parent_id") or gf.get("parent_group_folder_id") or gf.get("group_folder_parent_id")
        parent[fid] = _int_or_none(pid)
    return parent


def _folder_chain(folder_id: int | None, parent_map: dict[int, int | None]) -> list[int]:
    if folder_id is None:
        return []
    out: list[int] = []
    cur = int(folder_id)
    seen: set[int] = set()
    for _ in range(64):
        if cur in seen:
            break
        seen.add(cur)
        out.append(cur)
        nxt = parent_map.get(cur)
        if nxt is None:
            break
        cur = int(nxt)
    return out


def _holding_root(folder_id: int | None, parent_map: dict[int, int | None], root_ids: set[int]) -> int | None:
    if folder_id is None:
        return None
    cur = int(folder_id)
    seen: set[int] = set()
    for _ in range(64):
        if cur in root_ids:
            return cur
        if cur in seen:
            return None
        seen.add(cur)
        nxt = parent_map.get(cur)
        if nxt is None:
            return None
        cur = int(nxt)
    return None


def field_firms(fields: list[dict], field_groups: list[dict], group_folders: list[dict]) -> dict[int, str]:
    """field_id → агрофирма, та же цепочка, что в сводках: поле → группа → папка холдинга."""
    from plans import FIELD_NAME_FORCE_RULES, FIRM_TO_FOLDER, FOLDER_TO_FIRM

    groups = {int(g["id"]): g for g in field_groups if _int_or_none(g.get("id")) is not None}
    parent_map = _folder_parent_map(group_folders)
    root_ids = set(FIRM_TO_FOLDER.values())
    hints: dict[int, str] = {}
    for gf in group_folders:
        fid = _int_or_none(gf.get("id"))
        blob = _norm(f"{gf.get('name') or ''} {gf.get('title') or ''}")
        if fid and blob and any(x in blob for x in ("лениногор", "птицефабрик", "лпф", "lpf")):
            hints[fid] = "Лениногорская Птицефабрика"

    out: dict[int, str] = {}
    for field in fields:
        fid = _int_or_none(field.get("id"))
        if fid is None:
            continue
        fgid = _int_or_none(field.get("field_group_id"))
        raw_folder = _int_or_none(field.get("group_folder_id"))
        if raw_folder is None and fgid is not None:
            raw_folder = _int_or_none(groups.get(fgid, {}).get("group_folder_id"))
        firm = None
        for cid in _folder_chain(raw_folder, parent_map):
            if cid in hints:
                firm = hints[cid]
                break
        if firm is None:
            root = _holding_root(raw_folder, parent_map, root_ids)
            if root is not None:
                firm = FOLDER_TO_FIRM.get(root)
        if firm:
            out[fid] = firm
    for field in fields:
        fid = _int_or_none(field.get("id"))
        if fid is None:
            continue
        blob = _norm(f"{field.get('name') or ''} {field.get('title') or ''}")
        for needle, firm_key in FIELD_NAME_FORCE_RULES:
            if needle in blob:
                out[fid] = firm_key
                break
    return out


def _load_ref(path: Path, endpoint: str) -> list[dict]:
    if not path.is_file():
        return []
    con = _connect(path)
    try:
        if not _has_table(con, "ref"):
            return []
        row = con.execute(
            "SELECT rows FROM ref WHERE endpoint=? ORDER BY fetched_at DESC LIMIT 1",
            (endpoint,),
        ).fetchone()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    if row is None or not row["rows"]:
        return []
    try:
        body = json.loads(zlib.decompress(row["rows"]).decode("utf-8"))
    except (OSError, ValueError, zlib.error):
        return []
    return body if isinstance(body, list) else []


def _field_names(rows: list[dict]) -> dict[int, str]:
    out: dict[int, str] = {}
    for row in rows:
        fid = _int_or_none(row.get("id"))
        if fid is None:
            continue
        name = str(row.get("name") or row.get("title") or row.get("additional_name") or "").strip()
        if name:
            out[fid] = name
    return out


def _agro_names(rows: list[dict]) -> dict[int, str]:
    out: dict[int, str] = {}
    for row in rows:
        oid = _int_or_none(row.get("id"))
        if oid is None:
            continue
        name = str(row.get("name") or row.get("custom_name") or row.get("title") or "").strip()
        if name:
            out[oid] = name
    return out


def _agro_by_task(con: sqlite3.Connection, task_ids: list[int]) -> dict[int, list[int]]:
    out: dict[int, list[int]] = defaultdict(list)
    if not task_ids or not _has_table(con, "mtaomi"):
        return out
    for i in range(0, len(task_ids), 500):
        chunk = task_ids[i : i + 500]
        q = ",".join("?" * len(chunk))
        for row in con.execute(
            f"SELECT task_id, agro_operation_id FROM mtaomi WHERE task_id IN ({q})",
            chunk,
        ):
            oid = _int_or_none(row["agro_operation_id"])
            if oid:
                out[int(row["task_id"])].append(oid)
    for tid, ids in out.items():
        out[tid] = sorted(set(ids))
    return out


def _accumulate(bucket: dict, task_id: int, machine_id: int | None, wt: int | None,
                driver_id: int, field_id: int | None, shift: str, ha: float) -> None:
    key = (task_id, field_id, shift)
    rec = bucket.get(key)
    if rec is None:
        bucket[key] = {
            "task_id": task_id,
            "machine_id": machine_id,
            "work_type_id": wt,
            "driver_id": driver_id,
            "field_id": field_id,
            "date": shift,
            "ha": ha,
        }
        return
    rec["ha"] += ha


def _scan(con: sqlite3.Connection, date_from: str, date_to: str, ts_from: float, ts_to: float,
          driver_id: int | None) -> tuple[dict[int, dict], dict, int]:
    """people[id] и строки одного исполнителя (если driver_id задан)."""
    people: dict[int, dict] = {}
    lines: dict[tuple, dict] = {}
    skipped = 0

    def take(task_id: int, machine_id, wt, data: str, field_id, shift: str, ha: float) -> None:
        nonlocal skipped
        if ha <= 0 or not shift:
            return
        did = _driver_id(_loads(data))
        if did is None:
            skipped += 1
            return
        if driver_id is not None and did != driver_id:
            return
        person = people.get(did)
        if person is None:
            person = {"id": did, "ha": 0.0, "tasks": set()}
            people[did] = person
        person["ha"] += ha
        person["tasks"].add(task_id)
        if driver_id is not None:
            _accumulate(lines, task_id, _int_or_none(machine_id), _int_or_none(wt), did,
                        _int_or_none(field_id), shift, ha)

    if _has_table(con, "mtfmi_daily") and _has_table(con, "mtfmi_fact") and _has_table(con, "tasks"):
        for row in con.execute(
            "SELECT t.id, t.machine_id, t.wt, t.data, f.field_id, d.shift_date, d.ha "
            "FROM mtfmi_daily d JOIN mtfmi_fact f ON f.rid=d.rid JOIN tasks t ON t.id=f.task_id "
            "WHERE d.shift_date>=? AND d.shift_date<=? AND d.ha>0",
            (date_from, date_to),
        ):
            take(int(row["id"]), row["machine_id"], row["wt"], row["data"], row["field_id"],
                 str(row["shift_date"]), float(row["ha"] or 0))
        for row in con.execute(
            "SELECT t.id, t.machine_id, t.wt, t.data, f.field_id, t.end_ts, f.covered_area "
            "FROM mtfmi_fact f JOIN tasks t ON t.id=f.task_id "
            "WHERE f.hourly_n=0 AND f.covered_area>0 AND t.end_ts>=? AND t.end_ts<?",
            (ts_from, ts_to),
        ):
            take(int(row["id"]), row["machine_id"], row["wt"], row["data"], row["field_id"],
                 _shift_date(float(row["end_ts"])), float(row["covered_area"] or 0))
    return people, lines, skipped


def _people_out(people: dict[int, dict], names: dict[int, str] | None) -> list[dict]:
    names = names or {}
    out = []
    for did, rec in people.items():
        out.append({
            "id": did,
            "name": names.get(did) or f"id {did}",
            "ha": round(rec["ha"], 2),
            "tasks": len(rec["tasks"]),
        })
    out.sort(key=lambda r: (-r["ha"], r["name"], r["id"]))
    return out


def _agro_label(ids: list[int], names: dict[int, str]) -> str:
    parts = []
    for oid in ids:
        title = names.get(oid)
        parts.append(f"{title} ({oid})" if title else str(oid))
    return ", ".join(parts)


def _detail_rows(con: sqlite3.Connection, lines: dict, refs_path: Path,
                 work_names: dict[int, str] | None, person_names: dict[int, str] | None) -> list[dict]:
    labels = _operation_labels()
    work_names = work_names or {}
    person_names = person_names or {}
    task_ids = sorted({rec["task_id"] for rec in lines.values()})
    agro = _agro_by_task(con, task_ids)
    fields = _load_ref(refs_path, "fields")
    names = _field_names(fields)
    firms = field_firms(fields, _load_ref(refs_path, "field_groups"), _load_ref(refs_path, "group_folders"))
    agro_names = _agro_names(_load_ref(refs_path, "agro_operations"))
    rows = []
    for rec in lines.values():
        fid = rec["field_id"]
        wt = rec["work_type_id"]
        mid = rec["machine_id"]
        tid = rec["task_id"]
        op_ids = agro.get(tid) or []
        operation = ""
        if wt is not None:
            operation = work_names.get(wt) or labels.get(wt) or f"вид работ {wt}"
        rows.append({
            "date": rec["date"],
            "driver_id": rec["driver_id"],
            "driver": person_names.get(rec["driver_id"]) or f"id {rec['driver_id']}",
            "firm": (firms.get(fid) or "") if fid is not None else "",
            "field": (names.get(fid) or f"поле {fid}") if fid is not None else "",
            "field_id": fid,
            "operation": operation,
            "work_type_id": wt,
            "agro_operation": _agro_label(op_ids, agro_names),
            "agro_operation_ids": op_ids,
            "ha": round(rec["ha"], 2),
            "task_id": tid,
            "machine_id": mid,
            "task_url": _TASK_URL.format(machine_id=mid, task_id=tid) if mid and tid else "",
        })
    rows.sort(key=lambda r: (r["date"], r["firm"], r["field"], r["task_id"]))
    return rows


def _empty(date_from: date, date_to: date, detail: str, driver_id: int | None = None) -> dict[str, Any]:
    return {
        "available": False,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "driver_id": driver_id,
        "detail": detail,
        "people": [],
        "rows": [],
        "ha_total": 0.0,
        "tasks": 0,
        "truncated": False,
        "skipped_no_driver": 0,
    }


def query_driver_work(
    store_dir: Path,
    date_from: date,
    date_to: date,
    *,
    driver_id: int | None = None,
    names: dict[int, str] | None = None,
    work_type_names: dict[int, str] | None = None,
) -> dict[str, Any]:
    """Люди за период и, если задан driver_id, строки день × поле × задание."""
    d_from, d_to, ts_from, ts_to = _window(date_from, date_to)
    store = Path(store_dir) / "store.sqlite"
    if not store.is_file():
        return _empty(date_from, date_to, "Хранилище заданий не найдено. Сначала синхронизируйте сводки.", driver_id)
    try:
        con = _connect(store)
    except sqlite3.Error as exc:
        return _empty(date_from, date_to, f"Не удалось открыть хранилище: {exc}", driver_id)
    try:
        people, lines, skipped = _scan(con, d_from, d_to, ts_from, ts_to, driver_id)
        rows: list[dict] = []
        truncated = False
        if driver_id is not None:
            rows = _detail_rows(con, lines, Path(store_dir) / "refs.sqlite", work_type_names, names)
            if len(rows) > _MAX_ROWS:
                truncated = True
                rows = rows[:_MAX_ROWS]
    except sqlite3.Error as exc:
        return _empty(date_from, date_to, f"Хранилище заданий без нужных таблиц: {exc}", driver_id)
    finally:
        con.close()

    listed = _people_out(people, names)
    if driver_id is not None:
        listed = [p for p in listed if p["id"] == driver_id]
    person = listed[0] if len(listed) == 1 else None
    ha_total = round(sum(p["ha"] for p in listed), 2) if driver_id is None else round(
        people.get(driver_id, {}).get("ha", 0.0), 2,
    )
    task_count = (
        sum(p["tasks"] for p in listed) if driver_id is None
        else len(people.get(driver_id, {}).get("tasks") or [])
    )
    return {
        "available": True,
        "date_from": d_from,
        "date_to": d_to,
        "driver_id": driver_id,
        "driver": (person or {}).get("name") or (names or {}).get(driver_id or 0) or (
            f"id {driver_id}" if driver_id else ""
        ),
        "detail": "",
        "people": listed if driver_id is None else [],
        "rows": rows,
        "ha_total": ha_total,
        "tasks": task_count,
        "truncated": truncated,
        "skipped_no_driver": skipped,
    }
