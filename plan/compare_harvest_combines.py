#!/usr/bin/env python3
"""Сравнение «Количество комбайнов на полях» между сводками 2, 11 и 14."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPORT_DATE = date(2026, 9, 17)
from task_area_shared import HARVESTER_GROUP_IDS
from plans_1 import HIRED_GROUP_ID


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _is_combine(mid: int, machine_group: dict[int, int]) -> bool:
    grp = int(machine_group.get(int(mid), 0) or 0)
    return grp in HARVESTER_GROUP_IDS or grp == HIRED_GROUP_ID


def _print_table(title: str, rows: dict[str, int]) -> int:
    total = 0
    print(f"\n=== {title} ===")
    for firm, n in rows.items():
        print(f"  {firm:30s} {n:3d}")
        total += n
    print(f"  {'ИТОГО':30s} {total:3d}")
    return total


def _compare(label_a: str, a: dict[str, int], label_b: str, b: dict[str, int]) -> None:
    firms = sorted(set(a) | set(b))
    print(f"\n--- Сравнение {label_a} vs {label_b} ---")
    ok = True
    for firm in firms:
        va, vb = a.get(firm, 0), b.get(firm, 0)
        mark = "OK" if va == vb else "РАСХОЖДЕНИЕ"
        if va != vb:
            ok = False
        print(f"  {firm:30s} {va:3d}  {vb:3d}  {mark}")
    ta, tb = sum(a.values()), sum(b.values())
    mark = "OK" if ta == tb else "РАСХОЖДЕНИЕ"
    if ta != tb:
        ok = False
    print(f"  {'ИТОГО':30s} {ta:3d}  {tb:3d}  {mark}")
    print("  Итог:", "СОВПАДАЮТ" if ok else "ЕСТЬ РАСХОЖДЕНИЯ")


async def count_11() -> dict[str, int]:
    os.environ["REPORT_DATE"] = REPORT_DATE.isoformat()
    m11 = _load_module("main11_cmp", ROOT / "11_main_harvest.py")
    day_start, cutoff = m11.compute_time_range(
        REPORT_DATE, m11.DAY_START_HOUR, m11.DAY_END_HOUR,
    )
    season_start = datetime(
        m11.SEASON_START.year, m11.SEASON_START.month, m11.SEASON_START.day,
        m11.DAY_START_HOUR, 0, 0, tzinfo=m11.TZ_MSK,
    )
    m11.machines_map = {m["id"]: m for m in m11.get_all_from_id("machines")}
    field_to_firm = m11.load_field_to_firm()
    m10 = m11._load_main10_harvest()
    field_to_crop = m10.get_field_to_crop()
    veg_ctx = m11._veg_harvest_context()
    implements_map = m11._load_implements_map()
    import httpx

    headers = {"X-User-Api-Token": m11.API_TOKEN}
    semaphore = asyncio.Semaphore(m11.SEMAPHORE_LIMIT)
    pool = httpx.Limits(max_connections=60, max_keepalive_connections=30)
    async with httpx.AsyncClient(headers=headers, timeout=120, limits=pool) as client:
        tasks_today, tasks_season, mtfmi_map = await m11.fetch_all_tasks_and_mtfmi(
            client, semaphore, day_start, cutoff, season_start,
        )
    all_details = m11.aggregate_harvest_by_firm(
        tasks_today, tasks_season, mtfmi_map, field_to_firm, day_start, cutoff,
        field_to_crop=field_to_crop,
        veg_ctx=veg_ctx,
        implements_map=implements_map,
    )
    return {
        firm: int(all_details[firm]["machines_grain"])
        for firm in m11.FIRM_ORDER
    }


def count_14() -> dict[str, int]:
    os.environ["REPORT_DATE"] = REPORT_DATE.isoformat()
    m14 = _load_module("main14_cmp", ROOT / "14.main.py")
    report_date = REPORT_DATE
    all_machines = m14.load_machines()
    harvesters, hired_machines, harvester_ids, hired_ids, machine_group = (
        m14.split_machines(all_machines)
    )
    (
        field_to_fg, fg_to_folder, folder_to_name, field_to_firm_folder,
        field_name_to_firm_folder,
    ) = m14.load_geo_reference()
    task_machine_ids = harvester_ids | hired_ids
    tasks = m14.fetch_machine_tasks(task_machine_ids, report_date)
    task_ids = {t["id"] for t in tasks}
    fmi_list = m14.fetch_fmi(task_ids, report_date)
    fmi_list = m14._normalize_fmi_for_shift(fmi_list, report_date)
    summary = m14.build_summary_report(
        report_date,
        folder_to_name,
        tasks=tasks,
        fmi_list=fmi_list,
        field_to_fg=field_to_fg,
        fg_to_folder=fg_to_folder,
        field_to_firm_folder=field_to_firm_folder,
    )
    df = summary[summary["Агрофирмы"] != "Итого"]
    out: dict[str, int] = {}
    for _, row in df.iterrows():
        name = str(row["Агрофирмы"])
        # Обратное отображение display name → ключ FIRM_ORDER
        firm_key = None
        for fk, (fid, _) in m14.AGROFIRMS.items():
            disp = m14.get_firm_display_name(fk, fid, folder_to_name)
            if disp == name:
                firm_key = fk
                break
        key = firm_key or name
        out[key] = int(row["Количество комбайнов на полях"])
    return out


def count_2_both() -> tuple[dict[str, int], dict[str, int]]:
    os.environ["REPORT_DATE"] = REPORT_DATE.isoformat()
    m2 = _load_module("main2_cmp", ROOT / "2_main_ideal_compact.py")
    m2._apply_manual_plan_rates()
    (
        field_to_crop, field_to_crop_general, field_to_crop_harvest,
        field_to_crop_plow, field_to_crop_veg, field_to_crop_report3,
        field_to_firm, field_to_firm_general, field_to_firm_veg,
        field_to_firm_all, crop_field_ids_report3,
        period_available_ids, period_available_ids_general,
        field_area, machine_group,
        firm_plan_ha, firm_crop_area_ha, firm_crop_area_ha_general,
        history_item_to_crop, history_item_to_year,
    ) = m2.load_reference_data()
    headers = {"X-User-Api-Token": m2.API_TOKEN}
    work_data = m2.collect_work_data(
        field_to_crop, field_to_crop_general, field_to_crop_harvest,
        field_to_crop_plow, field_to_crop_veg, field_to_crop_report3,
        field_to_firm, field_to_firm_general, field_to_firm_veg, field_to_firm_all,
        crop_field_ids_report3,
        period_available_ids, period_available_ids_general,
        field_area, machine_group, headers,
        history_item_to_crop=history_item_to_crop,
        history_item_to_year=history_item_to_year,
    )
    gd = work_data[m2.HARVEST_GROUP_NAME]
    shift_date = REPORT_DATE - timedelta(days=1)
    all_out: dict[str, int] = {}
    comb_out: dict[str, int] = {}
    for firm in m2.FIRM_ORDER:
        bag = gd.get("machines", {}).get(firm, {}).get(shift_date, {"own": set(), "hired": set()})
        mids = set(bag.get("own") or set()) | set(bag.get("hired") or set())
        all_out[firm] = len(mids)
        comb_out[firm] = sum(1 for mid in mids if _is_combine(mid, machine_group))
    return all_out, comb_out


def main() -> None:
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    print(f"Дата отчёта (смена до {REPORT_DATE} 06:00): {REPORT_DATE}")

    print("\n[1/3] Сводка 11 — расчёт machines_grain...")
    c11 = asyncio.run(count_11())
    t11 = _print_table("Сводка 11", c11)

    print("\n[2/3] Сводка 14 — build_summary_report...")
    c14 = count_14()
    t14 = _print_table("Сводка 14", c14)

    print("\n[3/3] Сводка 2 — collect_work_data (5.0 Уборка)...")
    c2_all, c2_comb = count_2_both()
    t2_all = _print_table("Сводка 2 (все агрегаты)", c2_all)
    t2_comb = _print_table("Сводка 2 (только комбайны 5/9/7)", c2_comb)

    _compare("11", c11, "14", c14)
    _compare("11", c11, "2 комбайны", c2_comb)
    _compare("14", c14, "2 комбайны", c2_comb)
    if t2_all != t2_comb:
        print(f"\nПримечание: сводка 2 «все агрегаты»={t2_all}, «только комбайны»={t2_comb}")


if __name__ == "__main__":
    main()
