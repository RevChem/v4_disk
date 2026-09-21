"""
Cropwise Operations — отчёт "0. Статус ВПР"
===========================================

Краткая сводка по выполнению сезонных работ:
- посев по выбранным культурам;
- статус ключевых видов полевых работ.

Данные берутся напрямую из API:
- план: из `fields.tillable_area`;
- факт: из `machine_task_field_mapping_items.covered_area_hourly` (до cutoff 06:00 МСК),
  как в сводке 2. Для боронования (2.1) и заданий без hourly — fallback на covered_area.
  Порог: item с cov <= 0.5 га игнорируются (как в сводке 2).

Нюанс номенклатуры:
- "Рапс яровой" считается отдельно от "Рапс озимый" (как в сводке 0.1).
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import httpx
import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.pagebreak import Break
import requests
from plans_1 import *

REPORT_DATE = date.today()
if os.environ.get("REPORT_DATE"):
    REPORT_DATE = date.fromisoformat(os.environ["REPORT_DATE"])

# Базовый старт сезона синхронизирован с 1-й сводкой.
# Приоритетно используем переменные окружения (SVODKI_SEASON_START_DATE/SEASON_START_DATE).
SEASON_START = date(REPORT_DATE.year, 3, 1)
_season_start_raw = (
    os.environ.get("SVODKI_SEASON_START_DATE")
    or os.environ.get("SEASON_START_DATE")
    or ""
).strip()
if _season_start_raw:
    try:
        SEASON_START = date.fromisoformat(_season_start_raw)
    except ValueError:
        pass

SEMAPHORE_LIMIT = 8

# Группы посева — как в 0.1: все work_type_id групп «4.*».
SEEDING_WT_IDS: set[int] = {
    wid
    for name, ids in WORK_GROUPS
    if name.startswith("4.")
    for wid in ids
}

# Виды работ для второй части — берём из WORK_GROUPS в plans_1.
# Исключаем «4.1 Посев (овощи)» (овощи в блоке «ПОСЕВ ОВОЩЕЙ») и уборку.
_EXCLUDE_GROUPS = {
    "4.1 Посев (овощи)",
    "5.0 Уборка",
    "5.4 Уборка картофеля, лука",
}
OPERATIONS: list[tuple[str, set[int]]] = [
    (name, set(ids))
    for name, ids in WORK_GROUPS
    if name not in _EXCLUDE_GROUPS
]

# work_type_id для боронования — берём из WORK_GROUPS plans_1.
BORON_WT_IDS: set[int] = {
    wt_id
    for name, ids in WORK_GROUPS
    if name == "2.1 Боронование"
    for wt_id in ids
}


def is_skip_crop(crop_name: str) -> bool:
    if not crop_name:
        return True
    if crop_name in SKIP_CROPS:
        return True
    return False


@dataclass(frozen=True)
class CropRow:
    label: str
    aliases: tuple[str, ...]


CROP_BLOCKS: list[tuple[str, list[CropRow], str]] = [
    (
        "ПОСЕВ ЗЕРНОВЫХ И ТЕХНИЧЕСКИХ КУЛЬТУР",
        [
            CropRow("Подсолнечник", ("подсолнечник", "sunflower")),
            CropRow("Рапс яровой", ("рапс яровой",)),
        ],
        "Итого яровые",
    ),
    (
        "ПОСЕВ ОВОЩЕЙ",
        [
            CropRow("Картофель",      ("картоф", "картошка", "potato")),
            CropRow("Морковь",        ("морков",)),
            CropRow("Свекла столовая",("свекл",)),
        ],
        "Итого овощей",
    ),
]

session = requests.Session()
session.headers.update({"X-User-Api-Token": API_TOKEN})


def parse_dt(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        ts = float(raw)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ_MSK)
        except (OSError, ValueError, OverflowError):
            return None
    s = str(raw).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_MSK)
    else:
        dt = dt.astimezone(TZ_MSK)
    return dt


def get_all_from_id(endpoint: str, extra_params: dict[str, Any] | None = None) -> list[dict]:
    params = dict(extra_params or {})
    out: list[dict] = []
    from_id = 0
    while True:
        r = session.get(
            f"{BASE_URL}/{endpoint}",
            params={**params, "from_id": from_id, "limit": 1000},
            timeout=60,
        )
        r.raise_for_status()
        body = r.json()
        chunk = body.get("data", [])
        if not chunk:
            break
        out.extend(chunk)
        meta = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        if not last_id or meta.get("obtained_records", 0) < 100:
            break
        from_id = int(last_id) + 1
    return out


async def fetch_pages(
    client: httpx.AsyncClient,
    endpoint: str,
    params: dict[str, Any],
    sem: asyncio.Semaphore,
) -> list[dict]:
    out: list[dict] = []
    from_id = 0
    url = f"{BASE_URL}/{endpoint}"
    while True:
        async with sem:
            r = await client.get(url, params={**params, "from_id": from_id, "limit": 1000})
            r.raise_for_status()
            body = r.json()
            chunk = body.get("data", [])
        if not chunk:
            break
        out.extend(chunk)
        meta = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        if not last_id or meta.get("obtained_records", 0) < 100:
            break
        from_id = int(last_id) + 1
    return out


def normalize_crop_name(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    if not s:
        return ""
    s = s.replace("ё", "е")
    # Унифицируем разделители: "рапс-озимый" / "рапс/озимый" -> "рапс озимый".
    for ch in "-_/,.":
        s = s.replace(ch, " ")
    s = " ".join(s.split())
    return s


def crop_matches(crop_name: str, aliases: tuple[str, ...]) -> bool:
    c = normalize_crop_name(crop_name)
    return any(a in c for a in aliases)


def percent(plan: float, fact: float) -> float:
    if plan <= 0:
        return 0.0
    return round((fact / plan) * 100.0, 1)


def _sum_plan_from_hardcoded(group_key: str, crop_label: str) -> float:
    """Суммирует план по всем агрофирмам из HARDCODED_PLAN[group_key][firm][crop_label]."""
    total = 0.0
    for firm_crops in HARDCODED_PLAN.get(group_key, {}).values():
        v = firm_crops.get(crop_label)
        if v is not None:
            total += float(v)
    return round(total, 1)


def get_seeding_plan(label: str) -> float:
    """
    Возвращает план посева для культуры (строки CROP_BLOCKS).
    Приоритеты:
      1. Сумма по агрофирмам из HARDCODED_PLAN["4.0 Посев"] (яровые).
      2. Сумма по агрофирмам из HARDCODED_PLAN["4.1 Посев (овощи)"] (овощи).
      3. Заглушка 0.0.
    """
    seeding_total = _sum_plan_from_hardcoded("4.0 Посев", label)
    if seeding_total > 0:
        return seeding_total

    veg_total = _sum_plan_from_hardcoded("4.1 Посев (овощи)", label)
    if veg_total > 0:
        return veg_total

    return 0.0


def _harvest_reference_plan(title: str) -> float:
    """Справочный план посева для строк уборки (5.0 / 5.4)."""
    if title == "5.0 Уборка":
        total = sum(
            float(v)
            for firm_crops in HARDCODED_PLAN.get("4.0 Посев", {}).values()
            for v in firm_crops.values()
            if v is not None
        )
        return round(total, 1) if total > 0 else 0.0
    if title == "5.4 Уборка картофеля, лука":
        total = get_seeding_plan("Картофель")
        return round(total, 1) if total > 0 else 0.0
    return 0.0


def get_operation_plan(title: str) -> float:
    """План из HARDCODED_PLAN; для уборки — справочный план посева (как было)."""
    op_data = HARDCODED_PLAN.get(title)
    if op_data:
        total = sum(
            v for firm_crops in op_data.values()
            for v in firm_crops.values()
            if v is not None
        )
        if total > 0:
            return round(total, 1)

    ref = _harvest_reference_plan(title)
    if ref > 0:
        return ref

    return 0.0


def load_field_crop_plan() -> tuple[dict[int, tuple[str, float]], dict[int, str]]:
    """
    Как в 1-й сводке: культура поля берётся из history_items активного сезона,
    а не из текущего fields.crop_name.
    """
    fields = get_all_from_id("fields", {"only_active": "true"})
    crops = get_all_from_id("crops")
    history_items = get_all_from_id("history_items", {"year": str(SEASON_START.year)})

    crop_map: dict[int, str] = {
        int(c["id"]): str(c.get("name") or c.get("title") or "").strip()
        for c in crops
        if c.get("id")
    }
    field_area: dict[int, float] = {
        int(f["id"]): float(f.get("tillable_area") or f.get("calculated_area") or 0.0)
        for f in fields
        if f.get("id")
    }
    # field_id -> crop_name по активной записи сезона
    field_to_crop: dict[int, str] = {}
    for hi in history_items:
        if not hi.get("active"):
            continue
        fid = hi.get("field_id")
        crop_id = hi.get("crop_id")
        if not fid or not crop_id:
            continue
        crop_name = crop_map.get(int(crop_id), "")
        if not crop_name:
            continue
        if is_skip_crop(crop_name):
            continue
        field_to_crop[int(fid)] = crop_name

    # Только поля с активной культурой в history_items — как в 2_main_ideal_compact.
    # Fallback на crop_id/crop_name напрямую из поля намеренно убран:
    # он включал бы поля без активной записи в сезоне и давал бы расхождение с эталоном.
    out: dict[int, tuple[str, float]] = {}
    for fid, crop_name in field_to_crop.items():
        out[fid] = (crop_name, field_area.get(fid, 0.0))
    return out, crop_map


async def _fetch_mtfmi(
    client:    httpx.AsyncClient,
    task_id:   int,
    sem:       asyncio.Semaphore,
) -> tuple[int, list[dict]]:
    items = await fetch_pages(client, "machine_task_field_mapping_items",
                              {"machine_task_id": task_id}, sem)
    return task_id, items


async def load_tasks_and_mtfmi(
    cutoff: datetime,
) -> tuple[dict[int, list[dict]], dict[int, int], dict[int, dict]]:
    """
    Загружает все machine_tasks за сезон (end_time >= SEASON_START, end_time < cutoff)
    по всем нужным work_type_id, затем mtfmi для каждого задания.
    Возвращает:
        mtfmi_map:   task_id → [items]
        task_wt_map: task_id → work_type_id (строка)
    """
    season_start_dt = datetime(
        SEASON_START.year, SEASON_START.month, SEASON_START.day, 6, 0, 0, tzinfo=TZ_MSK
    )
    season_start_str = season_start_dt.strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_str       = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    all_wt_ids: set[int] = set(SEEDING_WT_IDS)
    for _, wt_ids in OPERATIONS:
        all_wt_ids |= wt_ids

    headers = {"X-User-Api-Token": API_TOKEN}
    sem     = asyncio.Semaphore(SEMAPHORE_LIMIT)

    async with httpx.AsyncClient(headers=headers, timeout=120) as client:
        # Загружаем machine_tasks за сезон по каждому work_type_id
        task_chunks = await asyncio.gather(*[
            fetch_pages(client, "machine_tasks", {
                "work_type_id":   wt_id,
                "end_time_gt_eq": season_start_str,
                "end_time_lt":    cutoff_str,
            }, sem)
            for wt_id in sorted(all_wt_ids)
        ])

        # Дедупликация
        all_tasks: dict[int, dict] = {}
        for chunk in task_chunks:
            for t in chunk:
                tid = t.get("id")
                if tid:
                    all_tasks[int(tid)] = t

        print(f"  machine_tasks за сезон: {len(all_tasks)}")

        # mtfmi для всех заданий — не фильтруем по covered_area,
        # т.к. площадь считается из covered_area_hourly (может быть > 0 при covered_area == 0).
        tasks_with_area = list(all_tasks.values())
        print(f"  Загружаем mtfmi для {len(tasks_with_area)} заданий...")
        mtfmi_results = await asyncio.gather(*[
            _fetch_mtfmi(client, t["id"], sem) for t in tasks_with_area
        ], return_exceptions=True)

    mtfmi_map:   dict[int, list[dict]] = {}
    task_wt_map: dict[int, int]        = {}
    for t in all_tasks.values():
        task_wt_map[int(t["id"])] = int(t.get("work_type_id") or 0)
    for res in mtfmi_results:
        if isinstance(res, BaseException):
            continue
        tid, items = res
        mtfmi_map[tid] = items

    return mtfmi_map, task_wt_map, all_tasks


def _item_covered_area(item: dict, cutoff: datetime, use_boron_logic: bool) -> float:
    """
    Извлекает покрытую площадь из mtfmi-item по аналогии с 2_main_ideal_compact:
    - Боронование (use_boron_logic=True): берём covered_area напрямую (как в отчёте 4).
    - Остальные: суммируем covered_area_hourly[i][1] для записей, где timestamp < cutoff.
      Если hourly пустой — fallback на covered_area (кроме логики отчёта 3 для wt_id=55,
      которой здесь нет).
    Возвращает 0.0 если cov <= MIN_DETAIL_AREA_HA (порог из plans_1).
    """
    if use_boron_logic:
        cov = float(item.get("covered_area") or 0.0)
    else:
        hourly = item.get("covered_area_hourly") or []
        cov = 0.0
        for entry in hourly:
            h = parse_dt(entry[0])
            if h and h < cutoff:
                cov += entry[1] or 0
        if not hourly:
            cov = float(item.get("covered_area") or 0.0)
    return cov if cov > MIN_DETAIL_AREA_HA else 0.0


def _seeding_shift_cutoff(task: dict) -> datetime | None:
    """Cutoff смены для посева — как в 0_1_main_status_vpr_morning."""
    end_dt = parse_dt(task.get("end_time"))
    if end_dt is None:
        return None
    cal_date = end_dt.date()
    anchor = datetime(cal_date.year, cal_date.month, cal_date.day, 6, 0, 0, tzinfo=TZ_MSK)
    if end_dt < anchor:
        anchor = anchor - timedelta(days=1)
    return anchor + timedelta(days=1)


def _seeding_item_covered_area(
    item: dict,
    shift_co: datetime | None,
    *,
    is_veg: bool = False,
) -> float:
    """Площадь посева по item. Овощи — covered_area как в 5_main; яровые — hourly + порог 0.5 га."""
    if is_veg:
        cov = float(item.get("covered_area") or 0.0)
        return cov if cov > 0 else 0.0
    if shift_co is None:
        cov = float(item.get("covered_area") or 0.0)
    else:
        hourly = item.get("covered_area_hourly") or []
        cov = 0.0
        for entry in hourly:
            h = parse_dt(entry[0])
            if h and h < shift_co:
                cov += float(entry[1] or 0.0)
        if not hourly:
            cov = float(item.get("covered_area") or 0.0)
    return cov if cov > MIN_DETAIL_AREA_HA else 0.0


def _task_shift_cutoff(task: dict, cutoff: datetime) -> datetime:
    """Возвращает shift_cutoff для задания: 06:00 МСК следующего дня после end_time."""
    end_dt = parse_dt(task.get("end_time"))
    if end_dt is None:
        return cutoff
    cal_date = end_dt.date()
    sc = datetime(cal_date.year, cal_date.month, cal_date.day, 6, 0, 0, tzinfo=TZ_MSK)
    if end_dt >= sc:
        sc += timedelta(days=1)
    return sc


def _resolve_crop_name(
    item: dict,
    task: dict,
    field_crop_plan: dict[int, tuple[str, float]],
    crop_map: dict[int, str],
) -> str:
    """Возвращает название культуры для item: сначала из field_crop_plan, затем fallback на crop_id задания."""
    fid = item.get("field_id")
    if not fid:
        return ""
    crop_name = field_crop_plan.get(int(fid), ("", 0.0))[0]
    if not crop_name:
        cid = task.get("crop_id")
        if cid:
            crop_name = crop_map.get(int(cid), "")
    return crop_name


def build_all_rows(
    field_crop_plan: dict[int, tuple[str, float]],
    crop_map:        dict[int, str],
    mtfmi_map:       dict[int, list[dict]],
    task_wt_map:     dict[int, int],
    all_tasks:       dict[int, dict],
    cutoff:          datetime,
) -> tuple[
    list[tuple[str, float, float, float]],
    list[tuple[str, float, float, float]],
]:
    """
    Единый проход по всем заданиям сезона.
    Возвращает (crop_rows, op_rows) — данные для обеих таблиц отчёта.

    Логика crop_name и shift_cutoff одинакова для обеих таблиц:
    - посев: культура только из field_crop_plan, cutoff смены как в 0.1;
    - операции: field_crop_plan + fallback на crop_id задания, shift_cutoff по end_time.
    """
    # --- инициализация аккумуляторов ---
    fact_by_label: dict[str, float] = {
        row.label: 0.0
        for _, rows, _ in CROP_BLOCKS
        for row in rows
    }
    fact_by_op: dict[str, float] = {title: 0.0 for title, _ in OPERATIONS}

    # Быстрый поиск: wt_id → список названий операций
    wt_to_ops: dict[int, list[str]] = {}
    for title, wt_ids in OPERATIONS:
        for wt_id in wt_ids:
            wt_to_ops.setdefault(wt_id, []).append(title)

    for tid, wt_id in task_wt_map.items():
        items = mtfmi_map.get(tid, [])
        if not items:
            continue
        task      = all_tasks.get(tid, {})
        sc        = _task_shift_cutoff(task, cutoff)
        seed_sc   = _seeding_shift_cutoff(task)
        use_boron = wt_id in BORON_WT_IDS
        op_titles = wt_to_ops.get(wt_id, [])
        is_seeding = wt_id in SEEDING_WT_IDS
        is_veg     = wt_id in VEG_WT_IDS

        for item in items:
            matched = False
            seed_cov = 0.0
            if is_seeding:
                fid = item.get("field_id")
                if fid:
                    crop_name = field_crop_plan.get(int(fid), ("", 0.0))[0]
                    seed_cov = _seeding_item_covered_area(item, seed_sc, is_veg=is_veg)
                    if seed_cov > 0:
                        if crop_name:
                            for _, rows, _ in CROP_BLOCKS:
                                for row in rows:
                                    if crop_matches(crop_name, row.aliases):
                                        fact_by_label[row.label] += seed_cov
                                        matched = True
                        if is_veg and not matched:
                            fallback = VEG_WT_FALLBACK_CROP.get(wt_id)
                            if fallback:
                                fact_by_label[fallback] += seed_cov
                                matched = True

            if is_seeding:
                cov = seed_cov
                if cov <= 0:
                    continue
            else:
                crop_name = _resolve_crop_name(item, task, field_crop_plan, crop_map)
                if not crop_name or is_skip_crop(crop_name):
                    continue
                cov = _item_covered_area(item, sc, use_boron)
                if cov <= 0:
                    continue

            # Таблица операций.
            # Для овощного посева засчитываем только если культура совпала
            # с одной из строк CROP_BLOCKS — ровно та же логика, что выше.
            for title in op_titles:
                if is_veg and not matched:
                    continue
                fact_by_op[title] += cov

    # --- формируем crop_rows ---
    crop_rows: list[tuple[str, float, float, float]] = []
    for _, rows, total_label in CROP_BLOCKS:
        block_plan = 0.0
        block_fact = 0.0
        for row in rows:
            p = get_seeding_plan(row.label)
            f = round(fact_by_label[row.label], 1)
            block_plan += p
            block_fact += f
            crop_rows.append((row.label, p, f, percent(p, f)))
        crop_rows.append((
            total_label,
            round(block_plan, 1),
            round(block_fact, 1),
            percent(block_plan, block_fact),
        ))

    # --- формируем op_rows ---
    op_rows: list[tuple[str, float, float, float]] = []
    for title, _ in OPERATIONS:
        plan_v = get_operation_plan(title)
        fact_v = round(fact_by_op[title], 1)
        op_rows.append((title, plan_v, fact_v, percent(plan_v, fact_v)))

    return crop_rows, op_rows


def make_styles() -> dict[str, Any]:
    thin = Side(style="thin")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    return {
        "border": border,
        "title": Font(name="Times New Roman", size=16, bold=True),
        "section": Font(name="Times New Roman", size=13, bold=True),
        "hdr": Font(name="Times New Roman", size=11, bold=True),
        "cell": Font(name="Times New Roman", size=11),
        "bold_cell": Font(name="Times New Roman", size=11, bold=True),
        "align_c": Alignment(horizontal="center", vertical="center"),
        "align_l": Alignment(horizontal="left", vertical="center"),
        "fill_section": PatternFill("solid", fgColor="E6E6E6"),
    }


def style_cell(cell, s: dict[str, Any], *, bold: bool = False) -> None:
    cell.border = s["border"]
    cell.alignment = s["align_c"]
    cell.font = s["bold_cell"] if bold else s["cell"]
    if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool):
        v = float(cell.value)
        if abs(v - round(v)) < 1e-9:
            cell.number_format = "# ##0"
        else:
            cell.number_format = "# ##0.0"


def write_report(
    report_date: date,
    crop_rows: list[tuple[str, float, float, float]],
    op_rows: list[tuple[str, float, float, float]],
) -> str:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Сводка"
    s = make_styles()

    ws.column_dimensions["A"].width = 6
    ws.column_dimensions["B"].width = 40
    ws.column_dimensions["C"].width = 16
    ws.column_dimensions["D"].width = 16
    ws.column_dimensions["E"].width = 10

    row = 1
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=5)
    c = ws.cell(row=row, column=1, value=f"СТАТУС ВПР на утро {report_date.strftime('%d.%m.%Y')}")
    c.font = s["title"]
    c.alignment = s["align_c"]
    row += 2

    # Блок "Посев зерновых и технических культур"
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=5)
    c = ws.cell(row=row, column=1, value=CROP_BLOCKS[0][0])
    c.font = s["section"]
    c.alignment = s["align_c"]
    c.fill = s["fill_section"]
    row += 1

    # Единая шапка как в 0.1 и эталонном PDF.
    headers = ["№", "КУЛЬТУРА", "ПЛАН, ГА", "ФАКТ, ГА", "%"]
    for i, h in enumerate(headers, start=1):
        ch = ws.cell(row=row, column=i, value=h)
        ch.font = s["hdr"]
        ch.alignment = s["align_c"]
        ch.border = s["border"]
    row += 1

    idx = 1
    # Первые три строки crop_rows относятся к первому блоку (2 культуры + Итого).
    for label, plan_v, fact_v, pct in crop_rows[:3]:
        ws.cell(row=row, column=1, value="" if label.startswith("Итого") else idx)
        ws.cell(row=row, column=2, value=label)
        ws.cell(row=row, column=3, value=plan_v)
        ws.cell(row=row, column=4, value=fact_v)
        ws.cell(row=row, column=5, value=pct / 100.0)
        for col in range(1, 6):
            style_cell(ws.cell(row=row, column=col), s, bold=label.startswith("Итого"))
        ws.cell(row=row, column=2).alignment = s["align_l"]
        ws.cell(row=row, column=5).number_format = "0.0%"
        if not label.startswith("Итого"):
            idx += 1
        row += 1

    row += 1

    # Блок "Посев овощей"
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=5)
    c = ws.cell(row=row, column=1, value=CROP_BLOCKS[1][0])
    c.font = s["section"]
    c.alignment = s["align_c"]
    c.fill = s["fill_section"]
    row += 1

    for i, h in enumerate(headers, start=1):
        ch = ws.cell(row=row, column=i, value=h)
        ch.font = s["hdr"]
        ch.alignment = s["align_c"]
        ch.border = s["border"]
    row += 1

    idx = 1
    for label, plan_v, fact_v, pct in crop_rows[3:]:
        ws.cell(row=row, column=1, value="" if label.startswith("Итого") else idx)
        ws.cell(row=row, column=2, value=label)
        ws.cell(row=row, column=3, value=plan_v)
        ws.cell(row=row, column=4, value=fact_v)
        ws.cell(row=row, column=5, value=pct / 100.0)
        for col in range(1, 6):
            style_cell(ws.cell(row=row, column=col), s, bold=label.startswith("Итого"))
        ws.cell(row=row, column=2).alignment = s["align_l"]
        ws.cell(row=row, column=5).number_format = "0.0%"
        idx += 1
        row += 1

    row += 1

    # Блоки по видам работ
    rows_on_page = row - 1
    max_rows_on_page = 51
    op_block_rows = 3  # заголовок + шапка + строка данных

    def ensure_operation_block_fits() -> None:
        nonlocal rows_on_page
        if rows_on_page + op_block_rows > max_rows_on_page:
            ws.row_breaks.append(Break(id=row - 1))
            rows_on_page = 0

    for title, plan_v, fact_v, pct in op_rows:
        ensure_operation_block_fits()
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=5)
        c = ws.cell(row=row, column=1, value=title)
        c.font = s["section"]
        c.alignment = s["align_c"]
        c.fill = s["fill_section"]
        row += 1

        op_headers = ["№", "ВИД РАБОТ", "ПЛАН, ГА", "ФАКТ, ГА", "%"]
        for i, h in enumerate(op_headers, start=1):
            ch = ws.cell(row=row, column=i, value=h)
            ch.font = s["hdr"]
            ch.alignment = s["align_c"]
            ch.border = s["border"]
        row += 1

        ws.cell(row=row, column=1, value=1)
        ws.cell(row=row, column=2, value=title)
        ws.cell(row=row, column=3, value=plan_v)
        ws.cell(row=row, column=4, value=fact_v)
        ws.cell(row=row, column=5, value=pct / 100.0)
        for col in range(1, 6):
            style_cell(ws.cell(row=row, column=col), s)
        ws.cell(row=row, column=2).alignment = s["align_l"]
        ws.cell(row=row, column=5).number_format = "0.0%"
        row += 2
        rows_on_page += 4

    out = f"status_vpr_{report_date}.xlsx"
    wb.save(out)
    return out


async def build_report_async(report_date: date = REPORT_DATE) -> None:
    cutoff = datetime(report_date.year, report_date.month, report_date.day, 6, 0, 0, tzinfo=TZ_MSK)
    print(f"  Cutoff: {cutoff}")

    field_crop_plan, crop_map = load_field_crop_plan()

    print("  Загружаем machine_tasks + mtfmi за сезон...")
    mtfmi_map, task_wt_map, all_tasks = await load_tasks_and_mtfmi(cutoff)

    crop_rows, op_rows = build_all_rows(field_crop_plan, crop_map, mtfmi_map, task_wt_map, all_tasks, cutoff)
    output = write_report(report_date, crop_rows, op_rows)
    print(f"\nСохранено: {output}")


def build_report(report_date: date = REPORT_DATE) -> None:
    asyncio.run(build_report_async(report_date))


if __name__ == "__main__":
    build_report()