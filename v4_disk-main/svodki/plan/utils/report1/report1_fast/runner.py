"""Оркестратор сводки 1 v2 — те же листы, быстрая загрузка данных."""

from __future__ import annotations

import asyncio
import os
import time

import httpx
import openpyxl

try:
    from svodki_timing import stage, stage_add, timing_finish, timing_start
except ImportError:  # таймеры необязательны
    from contextlib import contextmanager as _cm

    @_cm
    def stage(name):  # type: ignore[misc]
        yield

    def stage_add(name, seconds):  # type: ignore[misc]
        pass

    def timing_start():  # type: ignore[misc]
        pass

    def timing_finish():  # type: ignore[misc]
        pass

from .extra_sheets import (
    append_harvest_sheets,
    prepare_harvest_cache,
)
from .legacy import load_legacy
from .patches import apply_patches
from .preload import preload_all


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(1, int(str(raw).strip()))
    except ValueError:
        return default


def _is_standalone_mode() -> bool:
    raw = (os.environ.get("REPORT1_STANDALONE") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _skip_harvest_api_fetch() -> bool:
    raw = (os.environ.get("REPORT1_SKIP_HARVEST_FETCH") or "").strip().lower()
    return raw in {"1", "true", "yes", "on", "y"}


async def build_report_async_v2() -> None:
    timing_start()
    legacy = load_legacy()
    apply_patches(legacy)

    if hasattr(legacy, "apply_report1_thresholds"):
        legacy.apply_report1_thresholds()

    standalone = _is_standalone_mode()
    tag = "standalone" if standalone else "v2"

    t0 = time.monotonic()
    with stage("справочники"):
        legacy.load_reference_data()
    print(f"[{tag}] справочники: {time.monotonic() - t0:.1f} с")

    print(f"[{tag}] Строим план...")
    with stage("план"):
        plan_data = legacy.build_plan_ha()

    day_start, cutoff = legacy.compute_time_range(legacy.REPORT_DATE)
    headers = {"X-User-Api-Token": legacy.API_TOKEN}
    concurrency = _env_int("REPORT1_V2_CONCURRENCY", 40)
    sem = asyncio.Semaphore(concurrency)

    other_groups = [wg for wg in legacy.WORK_GROUPS if wg[0] != legacy.WORK_TYPE_NAME]
    if standalone:
        legacy.REPORT1_SKIP_PERF_GROUPS = set()
        skipped_perf_groups: set[str] = set()
        print(f"[{tag}] автономный режим: все группы работ из API (без доноров 3–8)")
    else:
        skipped_perf_groups = set(legacy.REPORT1_SKIP_PERF_GROUPS)
        if skipped_perf_groups:
            print(
                "[v2] fast-path: метрики подставятся из доноров "
                f"({len(skipped_perf_groups)} групп без API)"
            )

    t1 = time.monotonic()
    limits = httpx.Limits(max_connections=concurrency + 10, max_keepalive_connections=concurrency)
    harvest_prepared = None
    veg_prepared = None
    async with httpx.AsyncClient(headers=headers, timeout=120, limits=limits) as client:
        # Сначала полный preload (иначе 1.1 МУ может прочитать неполный prefetch-кэш
        # и «Сводка по культурам» занижает факт, напр. 13k вместо 79k).
        print(f"[{tag}] preload machine_tasks/mtfmi (concurrency={concurrency})...")
        with stage("v2 preload (machine_tasks + mtfmi)"):
            await preload_all(legacy, client, sem, day_start, cutoff, skipped_perf_groups)
        print(f"[{tag}] 1.1 МУ + остальные группы...")
        # 1.1 и группы идут параллельно: карту поле→фирма сбрасываем один раз здесь,
        # а 1.1 дописывает в неё (merge_firm_map) — иначе его полный пересбор затёр бы
        # поля, уже добавленные группами.
        reset = getattr(legacy, "reset_field_firm_resolution", None)
        if callable(reset):
            reset()
        with stage("v2 расчёт 1.1 + группы (wall)"):
            fert_bundle, other_group_stats = await asyncio.gather(
                legacy.fetch_fertilizer_11_mtfmi(
                    client, sem, day_start, cutoff, merge_firm_map=True,
                ),
                legacy._fetch_other_group_stats(
                    client, sem, day_start, cutoff, other_groups, skipped_perf_groups,
                ),
            )
    harvest_skip = _skip_harvest_api_fetch()
    standalone = _is_standalone_mode()
    harvest_prepared = None
    veg_prepared = None
    if harvest_skip:
        print(f"[{tag}] harvest fast-path: листы уборки из доноров 10/11 (без API)")
    elif standalone:
        print(f"[{tag}] harvest: блок 5.0 + сводки 10/11 внутри скрипта (без доноров)")
    else:
        harvest_prepared = await prepare_harvest_cache(legacy)
    print(f"[{tag}] загрузка данных: {time.monotonic() - t1:.1f} с")

    season_data, day_stats_main, fert_data, _cmp_mtfmi, _cmp_agro, today_ops_detail = fert_bundle
    all_group_stats: dict[str, dict[str, dict]] = {legacy.WORK_TYPE_NAME: day_stats_main}
    all_group_stats.update(other_group_stats)

    print(f"\n[{tag}] Формируем Excel...")
    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    if legacy.REPORT1_INCLUDE_CROP_SUMMARY:
        legacy.write_crop_summary_sheet(
            wb, season_data, day_stats_main, plan_data, fert_data,
        )
    legacy.write_sheet1(wb, season_data, day_stats_main, plan_data, fert_data)

    for title, crops in legacy.FERT_TABLE_SLIDES:
        legacy.write_fert_slide_sheet(
            wb,
            crops=crops,
            plan_data=plan_data,
            title=title,
            index=len(wb.sheetnames),
            day_stats=day_stats_main,
            season_data=season_data,
        )

    if (not legacy.REPORT1_HIDE_EMPTY_TABLES) or (
        not legacy.perf_stats_is_effectively_empty(day_stats_main)
    ):
        legacy.write_perf_sheet(
            wb,
            day_stats_main,
            "Выработка техники",
            len(wb.sheetnames),
            legacy._fleet_for_group(legacy.WORK_TYPE_NAME),
        )

    for firm in legacy.AGROFIRMS:
        legacy.write_firm_sheet(
            wb, firm, season_data, day_stats_main, plan_data, fert_data,
        )

    if legacy.REPORT1_ENABLE_DEBUG_SHEETS:
        legacy.write_today_fert_detail_sheet(wb, today_ops_detail)

    tillage_groups = {legacy.PLOW_GROUP_NAME, legacy.DEEP_GROUP_NAME}
    legacy.write_tillage_combined_sheet(
        wb,
        stats_dict=all_group_stats,
        index=len(wb.sheetnames),
    )

    write_group = getattr(legacy, "_write_group_perf_sheet", None)
    append_harvest_block = getattr(legacy, "_append_harvest_block_async", None)
    pre_groups = getattr(legacy, "REPORT1_PRE_HARVEST_PERF_GROUPS", ())
    post_groups = getattr(legacy, "REPORT1_POST_HARVEST_PERF_GROUPS", ())
    harvest_group = getattr(legacy, "REPORT1_HARVEST_PERF_GROUP", legacy.HARVEST_GRAIN_GROUP_NAME)

    if standalone and write_group and append_harvest_block:
        for group_name in pre_groups:
            write_group(wb, all_group_stats, group_name, skip_tillage=tillage_groups)
        await append_harvest_block(wb, all_group_stats)
        for group_name in post_groups:
            write_group(wb, all_group_stats, group_name, skip_tillage=tillage_groups)
    else:
        for group_name in legacy.REPORT1_PERF_GROUP_ORDER:
            if group_name in tillage_groups:
                continue
            if group_name in skipped_perf_groups:
                gstats = {firm: legacy._empty_stats() for firm in legacy.AGROFIRMS}
            elif group_name not in all_group_stats:
                continue
            else:
                gstats = all_group_stats[group_name]
            if legacy.REPORT1_HIDE_EMPTY_TABLES and legacy.perf_stats_is_effectively_empty(gstats):
                continue
            sheet_title = f"Выраб. — {group_name}"
            own_count = legacy._fleet_for_group(group_name)
            legacy.write_perf_sheet(
                wb,
                gstats,
                sheet_title,
                len(wb.sheetnames),
                own_count,
            )

    await legacy._append_dop_report1_tables(wb)
    stub_veg = getattr(legacy, "_append_veg_harvest_sheet_stub", None)
    stub_namolot = getattr(legacy, "_append_namolot_sheets_stub", None)
    if harvest_skip:
        if stub_veg:
            stub_veg(wb)
        if stub_namolot:
            stub_namolot(wb)
    elif not standalone:
        if stub_veg:
            stub_veg(wb)
        await append_harvest_sheets(wb, legacy, harvest_prepared)

    reorder = getattr(legacy, "_reorder_r1_sheets", None)
    if reorder is not None:
        try:
            changed = reorder(wb)
            if changed:
                print(f"  [{tag}] Порядок листов переставлен по эталону сводки 1.")
        except Exception as exc:
            print(f"  [{tag}] Предупреждение: не удалось переставить листы — {exc}")

    output_file = f"1. fertilizer_report_{legacy.REPORT_DATE}.xlsx"
    with stage("сохранение xlsx"):
        wb.save(output_file)
    print(f"\n[{tag}] Сохранено: {output_file} (всего {time.monotonic() - t0:.1f} с)")
    timing_finish()


def build_report() -> None:
    asyncio.run(build_report_async_v2())


def run_report1_v2() -> None:
    build_report()
