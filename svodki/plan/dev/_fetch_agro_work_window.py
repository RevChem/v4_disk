#!/usr/bin/env python3
"""Fetch Cropwise machine work data for a shift window and print summary."""
from __future__ import annotations

import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

TZ_MSK = timezone(timedelta(hours=3))
WINDOW_FROM = datetime(2026, 9, 10, 6, 0, 0, tzinfo=TZ_MSK)
WINDOW_TO = datetime(2026, 9, 11, 6, 0, 0, tzinfo=TZ_MSK)


def load_env() -> None:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


def get_all(session: requests.Session, base: str, endpoint: str, params: dict | None = None) -> list[dict]:
    p = dict(params or {})
    results: list[dict] = []
    from_id = 0
    for _ in range(500):
        r = session.get(
            f"{base}/{endpoint}",
            params={**p, "from_id": from_id, "limit": 1000},
            timeout=120,
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
        from_id = last_id + 1
    return results


def fmt_dt(raw: str | None) -> str:
    if not raw:
        return "—"
    return str(raw).replace("T", " ")[:19]


def main() -> int:
    load_env()
    token = (os.environ.get("CROPWISE_API_TOKEN") or "").strip()
    base = (os.environ.get("CROPWISE_BASE_URL") or "https://operations.cropwise.com/api/v3").rstrip("/")
    if not token:
        print("ERROR: CROPWISE_API_TOKEN not set", file=sys.stderr)
        return 1

    session = requests.Session()
    session.headers.update({"X-User-Api-Token": token, "Accept": "application/json"})

    params = {
        "end_time_gt_eq": WINDOW_FROM.strftime("%Y-%m-%dT%H:%M:%S"),
        "end_time_lt": WINDOW_TO.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    print("=" * 80)
    print("Cropwise — агро работы машин")
    print(f"Период (МСК): {WINDOW_FROM.strftime('%Y-%m-%d %H:%M')} — {WINDOW_TO.strftime('%Y-%m-%d %H:%M')}")
    print("Фильтр: все типы и подтипы работ")
    print("=" * 80)

    print("\nЗагрузка справочников...")
    work_types = {
        int(w["id"]): (w.get("name") or f"wt_{w['id']}")
        for w in get_all(session, base, "work_types")
        if w.get("id") is not None
    }
    machines = {
        int(m["id"]): (
            m.get("name")
            or m.get("registration_number")
            or f"machine_{m['id']}"
        )
        for m in get_all(session, base, "machines")
        if m.get("id") is not None
    }
    users: dict[int, str] = {}
    for ep in ("users", "operators", "workers"):
        try:
            rows = get_all(session, base, ep)
        except requests.HTTPError:
            continue
        for u in rows:
            uid = u.get("id")
            if uid is None:
                continue
            users[int(uid)] = (
                u.get("full_name")
                or u.get("name")
                or f"{u.get('first_name', '')} {u.get('last_name', '')}".strip()
                or f"user_{uid}"
            )
        if users:
            break

    print(f"  work_types: {len(work_types)}, machines: {len(machines)}, users: {len(users)}")

    print("\nЗагрузка machine_tasks...")
    tasks = get_all(session, base, "machine_tasks", params)
    # dedupe
    by_id: dict[int, dict] = {}
    for t in tasks:
        tid = t.get("id")
        if tid is not None:
            by_id[int(tid)] = t
    tasks = list(by_id.values())
    print(f"  machine_tasks: {len(tasks)}")

    if not tasks:
        print("\nНет данных за указанный период.")
        return 0

    print("\nЗагрузка machine_task_field_mapping_items (mtfmi)...")
    task_ids = [int(t["id"]) for t in tasks]
    mtfmi_by_task: dict[int, list[dict]] = defaultdict(list)
    batch_size = 50
    for i in range(0, len(task_ids), batch_size):
        chunk = task_ids[i : i + batch_size]
        rows = get_all(
            session,
            base,
            "machine_task_field_mapping_items",
            {"machine_task_id": ",".join(str(x) for x in chunk)},
        )
        for row in rows:
            mtid = row.get("machine_task_id")
            if mtid is not None:
                mtfmi_by_task[int(mtid)].append(row)

    total_mtfmi = sum(len(v) for v in mtfmi_by_task.values())
    print(f"  mtfmi записей: {total_mtfmi}")

    wt_counter: Counter[str] = Counter()
    subtype_counter: Counter[str] = Counter()
    rows_out: list[tuple] = []

    for task in sorted(tasks, key=lambda x: (x.get("start_time") or "", x.get("id") or 0)):
        tid = int(task["id"])
        wt_id = task.get("work_type_id")
        wt_name = work_types.get(int(wt_id), f"wt_{wt_id}") if wt_id is not None else "—"
        action_subtype = task.get("action_subtype") or "—"
        wt_counter[wt_name] += 1
        subtype_counter[f"{wt_name} / {action_subtype}"] += 1

        machine = machines.get(int(task["machine_id"]), f"machine_{task.get('machine_id')}") if task.get("machine_id") else "—"
        driver_id = task.get("driver_id") or task.get("operator_id") or task.get("user_id")
        driver = users.get(int(driver_id), f"id_{driver_id}") if driver_id else "—"
        start = fmt_dt(task.get("start_time"))
        end = fmt_dt(task.get("end_time"))
        covered = float(task.get("covered_area") or 0)

        mtfmi_items = mtfmi_by_task.get(tid, [])
        if mtfmi_items:
            for item in mtfmi_items:
                field_id = item.get("field_id", "—")
                area = float(item.get("covered_area") or 0)
                rows_out.append((start, end, wt_name, action_subtype, machine, driver, field_id, area, tid))
        else:
            rows_out.append((start, end, wt_name, action_subtype, machine, driver, "—", covered, tid))

    print("\n--- Сводка по типам работ ---")
    for name, cnt in wt_counter.most_common():
        print(f"  {cnt:4d}  {name}")

    print("\n--- Сводка по подтипам (work_type / action_subtype) ---")
    for name, cnt in subtype_counter.most_common(30):
        print(f"  {cnt:4d}  {name}")
    if len(subtype_counter) > 30:
        print(f"  ... ещё {len(subtype_counter) - 30} комбинаций")

    print(f"\n--- Детализация ({len(rows_out)} строк) ---")
    header = f"{'Старт':<20} {'Конец':<20} {'Тип работ':<28} {'Подтип':<12} {'Машина':<30} {'Водитель':<22} {'Поле':<8} {'Га':>8}"
    print(header)
    print("-" * len(header))
    for start, end, wt_name, subtype, machine, driver, field_id, area, tid in rows_out[:200]:
        print(
            f"{start:<20} {end:<20} {wt_name[:28]:<28} {str(subtype)[:12]:<12} "
            f"{str(machine)[:30]:<30} {str(driver)[:22]:<22} {str(field_id):<8} {area:8.2f}"
        )
    if len(rows_out) > 200:
        print(f"\n... показано 200 из {len(rows_out)} строк")

    print("\n--- Итого ---")
    print(f"  Заданий (machine_tasks): {len(tasks)}")
    print(f"  Строк детализации:       {len(rows_out)}")
    print(f"  Уникальных типов работ:  {len(wt_counter)}")
    print(f"  Сумма covered_area (mtfmi + task): {sum(r[7] for r in rows_out):.2f} га")

    return 0


if __name__ == "__main__":
    sys.exit(main())
