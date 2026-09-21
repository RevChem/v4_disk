"""
Cropwise Operations — отчёт "0.1 Статус ВПР на утро"
====================================================

Новая сводка на базе "0. Статус ВПР":
- посев яровых культур;
- посев овощей;
- подкормка озимых культур (1.1) из API.
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
import requests
from plans_1 import (
    API_TOKEN, BASE_URL, TZ_MSK,
    PLAN_HA, HARDCODED_PLAN, PLAN_11_OVERRIDES,
    WORK_GROUPS, MIN_DETAIL_AREA_HA, SKIP_CROPS,
    VEG_WT_IDS, VEG_WT_FALLBACK_CROP,
)

# Суммируем план посева по агрофирмам из HARDCODED_PLAN.
# Яровые культуры — из "4.0 Посев", овощи — из "4.1 Посев (овощи)".
def _sum_plan_from_hardcoded(group_key: str, crop_label: str) -> float:
    total = 0.0
    for firm_crops in HARDCODED_PLAN.get(group_key, {}).values():
        v = firm_crops.get(crop_label)
        if v is not None:
            total += float(v)
    return round(total, 1)


REPORT_DATE = date.today()
if os.environ.get("REPORT_DATE"):
    REPORT_DATE = date.fromisoformat(os.environ["REPORT_DATE"])

SEASON_START = date(REPORT_DATE.year, 3, 10)
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
FERTILIZER_WORK_TYPE_ID = 55
SEASON = REPORT_DATE.year  # используется при запросе agro_operations

# Группы посева — объединяем все work_type_id групп "4.*" из plans_1.WORK_GROUPS.
SEEDING_WT_IDS: set[int] = {
    wid
    for name, ids in WORK_GROUPS
    if name.startswith("4.")
    for wid in ids
}


@dataclass(frozen=True)
class CropRow:
    label: str
    aliases: tuple[str, ...]


CROP_BLOCKS: list[tuple[str, list[CropRow], str]] = [
    (
        "ПОСЕВ ЯРОВЫХ КУЛЬТУР",
        [
            CropRow("Рапс яровой", ("рапс яровой",)),
            CropRow("Подсолнечник", ("подсолнечник", "sunflower")),
        ],
        "Итого яровые",
    ),
    (
        "ПОСЕВ ОВОЩЕЙ",
        [
            CropRow("Картофель",      ("картоф", "картошка", "potato")),
            CropRow("Лук",            ("лук",)),
            CropRow("Морковь",        ("морков",)),
            CropRow("Свекла столовая",("свекл",)),
        ],
        "Итого овощи",
    ),
]

WINTER_FEEDING_ROWS: list[tuple[str, str]] = [
    ("Подкормка озимой пшеницы Селитра аммиачная 34,4", "wheat_nitrate"),
    ("Подкормка озимого рапса - Селитра аммиачная 34,4", "raps_nitrate"),
    ("Подкормка озимого рапса - Сульфат аммония",        "raps_sulfate"),
]

session = requests.Session()
session.headers.update({"X-User-Api-Token": API_TOKEN})


def parse_dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    s = str(raw).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_MSK)
    return dt.astimezone(TZ_MSK)


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


async def fetch_pages(client: httpx.AsyncClient, endpoint: str, params: dict[str, Any], sem: asyncio.Semaphore) -> list[dict]:
    out: list[dict] = []
    from_id = 0
    url = f"{BASE_URL}/{endpoint}"
    async with sem:
        while True:
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
    for ch in "-_/,.":
        s = s.replace(ch, " ")
    return " ".join(s.split())


def crop_matches(crop_name: str, aliases: tuple[str, ...]) -> bool:
    c = normalize_crop_name(crop_name)
    return any(a in c for a in aliases)


def _resolve_fact_crop_key(task_wt: int, crop_name: str) -> str:
    """Ключ для накопления факта: овощи — как в 5_main (fallback по work_type_id)."""
    if task_wt in VEG_WT_IDS:
        if crop_name:
            for _, rows, _ in CROP_BLOCKS:
                for row in rows:
                    if crop_matches(crop_name, row.aliases):
                        return row.label
        return VEG_WT_FALLBACK_CROP.get(task_wt) or crop_name
    return crop_name


def op_fact_area(op: dict[str, Any]) -> float:
    completed = float(op.get("completed_area") or 0.0)
    if completed > 0:
        return completed
    covered = float(op.get("covered_area") or 0.0)
    if covered > 0:
        return covered
    return 0.0


def percent(plan: float, fact: float) -> float:
    if plan <= 0:
        return 0.0
    return round((fact / plan) * 100.0, 1)


def load_field_crop_plan() -> tuple[dict[int, tuple[str, float]], dict[int, str]]:
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

    field_to_crop: dict[int, str] = {}
    for hi in history_items:
        if not hi.get("active"):
            continue
        fid = hi.get("field_id")
        crop_id = hi.get("crop_id")
        if not fid or not crop_id:
            continue
        crop_name = crop_map.get(int(crop_id), "")
        if crop_name:
            field_to_crop[int(fid)] = crop_name

    # Только поля с активной культурой в history_items — как в 2_main_ideal_compact.
    # Fallback на crop_id/crop_name напрямую из поля намеренно убран:
    # он включал бы поля без активной записи в сезоне и давал бы расхождение с эталоном.
    out: dict[int, tuple[str, float]] = {}
    for fid, crop_name in field_to_crop.items():
        out[fid] = (crop_name, field_area.get(fid, 0.0))
    return out, crop_map




async def load_fact_by_crop(
    wt_ids: set[int],
    field_to_crop: dict[int, str],
    cutoff: datetime,
) -> dict[str, float]:
    """
    Загружает machine_tasks по work_type_id за сезон до cutoff,
    затем для каждого задания берёт machine_task_field_mapping_items
    и суммирует covered_area по культуре поля.
    Логика точно как в 2_main_ideal_compact.py (эталон):
      - площадь из covered_area_hourly, только записи h < cutoff смены;
      - порог per-item 0.5 га для яровых; овощи (VEG_WT_IDS) — covered_area как в 5_main;
      - дата смены определяется через end_time (смена D = [D 06:00, D+1 06:00)).
    """
    DAY_START_HOUR = 6
    season_start = datetime(SEASON_START.year, 1, 1, 0, 0, 0, tzinfo=TZ_MSK)
    cutoff_str   = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    season_str   = season_start.strftime("%Y-%m-%dT%H:%M:%S")
    headers = {"X-User-Api-Token": API_TOKEN}
    sem = asyncio.Semaphore(SEMAPHORE_LIMIT)

    async with httpx.AsyncClient(headers=headers, timeout=120) as client:
        chunks = await asyncio.gather(*[
            fetch_pages(client, "machine_tasks", {
                "work_type_id":   wt,
                "end_time_gt_eq": season_str,
                "end_time_lt":    cutoff_str,
            }, sem)
            for wt in sorted(wt_ids)
        ])

    uniq_tasks: dict[int, dict] = {}
    for chunk in chunks:
        for t in chunk:
            tid = t.get("id")
            # Не фильтруем по machine_tasks.covered_area: площадь в MTFMI
            # может быть > 0 при нулевом covered_area задания (как в 5_main).
            if tid:
                uniq_tasks[int(tid)] = t

    if not uniq_tasks:
        return {}

    # Определяем cutoff смены для каждого задания
    def _shift_cutoff(end_time_raw) -> datetime | None:
        """Возвращает cutoff смены (D+1 06:00 МСК) для задания, или None если нет end_time."""
        end_dt = parse_dt(end_time_raw)
        if end_dt is None:
            return None
        cal_date = end_dt.date()
        anchor = datetime(cal_date.year, cal_date.month, cal_date.day,
                          DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK)
        if end_dt < anchor:
            # end_time до 06:00 — смена предыдущего дня
            anchor = anchor - timedelta(days=1)
        return anchor + timedelta(days=1)  # cutoff = D+1 06:00

    task_ids = list(uniq_tasks.keys())
    async with httpx.AsyncClient(headers=headers, timeout=120) as client:
        mtfmi_chunks = await asyncio.gather(*[
            fetch_pages(client, "machine_task_field_mapping_items",
                        {"machine_task_id": tid}, sem)
            for tid in task_ids
        ])

    fact_by_crop: dict[str, float] = {}
    for tid, items in zip(task_ids, mtfmi_chunks):
        task = uniq_tasks[tid]
        task_wt = int(task.get("work_type_id") or 0)
        shift_co = _shift_cutoff(task.get("end_time"))
        # Задания без end_time — используем covered_area напрямую (no_shift fallback)
        use_hourly = shift_co is not None

        is_veg_wt = task_wt in VEG_WT_IDS
        for it in items:
            fid = it.get("field_id")
            if not fid:
                continue

            if is_veg_wt:
                # Сезонный факт овощей — как в 5_main__4_.py (veg_fact_by_firm_crop).
                cov = float(it.get("covered_area") or 0.0)
                if cov <= 0:
                    continue
            elif use_hourly:
                hourly = it.get("covered_area_hourly") or []
                cov = 0.0
                for entry in hourly:
                    h = parse_dt(entry[0])
                    if h and h < shift_co:
                        cov += float(entry[1] or 0.0)
                if not hourly:
                    cov = float(it.get("covered_area") or 0.0)
            else:
                cov = float(it.get("covered_area") or 0.0)

            if not is_veg_wt and cov <= MIN_DETAIL_AREA_HA:
                continue

            crop_name = field_to_crop.get(int(fid), "")
            key = _resolve_fact_crop_key(task_wt, crop_name)
            if not key:
                continue
            fact_by_crop[key] = fact_by_crop.get(key, 0.0) + cov

    return fact_by_crop


def _get_seeding_plan(label: str) -> float:
    """
    Возвращает план посева для культуры.
    Приоритеты:
      1. Сумма по агрофирмам из HARDCODED_PLAN["4.0 Посев"] (яровые).
      2. Сумма по агрофирмам из HARDCODED_PLAN["4.1 Посев (овощи)"] (овощи).
      3. Заглушка 0.0.
    """
    # 1. Яровые — из "4.0 Посев"
    seeding_total = _sum_plan_from_hardcoded("4.0 Посев", label)
    if seeding_total > 0:
        return seeding_total

    # 2. Овощи — из "4.1 Посев (овощи)"
    veg_total = _sum_plan_from_hardcoded("4.1 Посев (овощи)", label)
    if veg_total > 0:
        return veg_total

    return 0.0


def build_crop_rows(
    fact_by_crop: dict[str, float],
) -> list[tuple[str, float, float, float]]:
    """
    Строит строки таблиц посева.
    fact_by_crop — словарь {название_культуры: га}, полученный из load_fact_by_crop.
    Для каждой культуры из CROP_BLOCKS суммируем covered_area по совпадающим алиасам.
    """
    fact_by_label: dict[str, float] = {}
    for _, rows, _ in CROP_BLOCKS:
        for row in rows:
            fact_by_label[row.label] = 0.0

    for crop_name, cov in fact_by_crop.items():
        for _, rows, _ in CROP_BLOCKS:
            for row in rows:
                if crop_matches(crop_name, row.aliases):
                    fact_by_label[row.label] += cov

    result: list[tuple[str, float, float, float]] = []
    for _, rows, total_label in CROP_BLOCKS:
        block_plan = 0.0
        block_fact = 0.0
        for row in rows:
            p = _get_seeding_plan(row.label)
            f = round(fact_by_label[row.label], 1)
            block_plan += p
            block_fact += f
            result.append((row.label, p, f, percent(p, f)))
        result.append((total_label, round(block_plan, 1), round(block_fact, 1), percent(block_plan, block_fact)))
    return result


async def load_fert_facts_from_agro_ops(
    field_to_crop: dict[int, str],
    cutoff: datetime,
) -> dict[str, float]:
    """
    Загружает agro_operations (work_type_id=55) за сезон до cutoff.
    Для каждой операции смотрит application_mix_items → applicable_type == "Fertilizer",
    определяет название удобрения через API /fertilizers и считает площадь отдельно
    по каждому удобрению для пшеницы озимой и рапса озимого.

    Возвращает:
        {
            "wheat_nitrate": га,
            "wheat_sulfate": га,
            "raps_nitrate":  га,
            "raps_sulfate":  га,
        }

    Логика площади совпадает с эталоном (fetch_fertilizer_11_mtfmi):
    - статус canceled пропускаем;
    - для planned берём только если covered_area > 0;
    - площадь = completed_area, fallback covered_area;
    - если в операции несколько удобрений — площадь делится поровну между ними
      (area_share = area / len(fert_ids_in_op)).
    """
    headers = {"X-User-Api-Token": API_TOKEN}
    sem = asyncio.Semaphore(SEMAPHORE_LIMIT)

    async with httpx.AsyncClient(headers=headers, timeout=120) as client:
        # Загружаем справочник удобрений
        ferts_raw = await fetch_pages(client, "fertilizers", {}, sem)
        fert_name_map: dict[int, str] = {}
        for f in ferts_raw:
            fid = f.get("id")
            name = str(f.get("name") or f.get("title") or "").strip().lower()
            if fid and name:
                fert_name_map[int(fid)] = name

        # Загружаем agro_operations за сезон
        ops_raw = await fetch_pages(client, "agro_operations", {
            "work_type_id": FERTILIZER_WORK_TYPE_ID,
            "season":       str(SEASON),
        }, sem)

    SKIP_STATUSES = {"canceled"}
    result: dict[str, float] = {
        "wheat_nitrate": 0.0,
        "wheat_sulfate": 0.0,
        "raps_nitrate":  0.0,
        "raps_sulfate":  0.0,
    }

    season_start_dt = datetime(SEASON_START.year, 1, 1, tzinfo=TZ_MSK)

    for op in ops_raw:
        status = op.get("status", "")
        if status in SKIP_STATUSES:
            continue

        completed = float(op.get("completed_area") or 0.0)
        covered   = float(op.get("covered_area")   or 0.0)
        if status == "planned":
            if covered <= 0:
                continue
        else:
            if completed <= 0 and covered <= 0:
                continue

        field_id = op.get("field_id")
        if not field_id:
            continue
        crop = field_to_crop.get(int(field_id), "")
        if not crop:
            continue
        cn = normalize_crop_name(crop)
        if "пшениц" in cn and "озим" in cn:
            crop_key = "wheat"
        elif "рапс" in cn and "озим" in cn:
            crop_key = "raps"
        else:
            continue

        # Фильтр по дате — не раньше начала сезона
        op_dt = parse_dt(
            op.get("completed_datetime") or op.get("actual_start_datetime")
        )
        if op_dt and op_dt < season_start_dt:
            continue

        area = completed if completed > 0 else covered

        # Собираем уникальные fert_id из application_mix_items
        fert_ids_in_op: list[int] = []
        for ami in (op.get("application_mix_items") or []):
            if ami.get("applicable_type") == "Fertilizer":
                fid_fert = ami.get("applicable_id")
                if fid_fert and int(fid_fert) not in fert_ids_in_op:
                    fert_ids_in_op.append(int(fid_fert))

        if not fert_ids_in_op:
            continue

        area_share = area / len(fert_ids_in_op)

        for fid_fert in fert_ids_in_op:
            fname = fert_name_map.get(fid_fert, "")
            # Определяем тип удобрения по названию
            if (
                "селитр" in fname
                or ("аммиачн" in fname and "сульфат" not in fname)
                or "нитрат аммония" in fname
                or "nitrate" in fname
            ):
                fert_type = "nitrate"
            elif any(kw in fname for kw in ("сульфат", "sulfate")):
                fert_type = "sulfate"
            else:
                # Неизвестное удобрение — пропускаем (не делим 50/50)
                continue
            key = f"{crop_key}_{fert_type}"
            if key in result:
                result[key] += area_share

    return {k: round(v, 1) for k, v in result.items()}


def _build_winter_feeding_rows(
    fact_by_key: dict[str, float],
) -> list[tuple[str, float, float, float]]:
    """
    Строит строки таблицы подкормки озимых.
    fact_by_key — словарь {wheat_nitrate, wheat_sulfate, raps_nitrate, raps_sulfate: га}
    из load_fert_facts_from_agro_ops — факт по каждому удобрению из agro_operations.
    """
    rows: list[tuple[str, float, float, float]] = []
    for label, key in WINTER_FEEDING_ROWS:
        plan_v = float(PLAN_HA.get(key, 0.0) or 0.0)
        fact_v = fact_by_key.get(key, 0.0)
        rows.append((label, round(plan_v, 1), round(fact_v, 1), percent(plan_v, fact_v)))

    total_plan = sum(r[1] for r in rows)
    total_fact = sum(r[2] for r in rows)
    rows.append(("ИТОГО", round(total_plan, 1), round(total_fact, 1), percent(total_plan, total_fact)))
    return rows


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


def _write_table_block(
    ws,
    row: int,
    title: str,
    headers: list[str],
    rows_data: list[tuple[str, float, float, float]],
    s: dict[str, Any],
) -> int:
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=5)
    c = ws.cell(row=row, column=1, value=title)
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

    for idx, (label, plan_v, fact_v, pct) in enumerate(rows_data, start=1):
        ws.cell(row=row, column=1, value="" if label.lower().startswith("итого") else idx)
        ws.cell(row=row, column=2, value=label)
        ws.cell(row=row, column=3, value=plan_v)
        ws.cell(row=row, column=4, value=fact_v)
        ws.cell(row=row, column=5, value=pct / 100.0)
        is_total = label.lower().startswith("итого")
        for col in range(1, 6):
            style_cell(ws.cell(row=row, column=col), s, bold=is_total)
        ws.cell(row=row, column=2).alignment = s["align_l"]
        ws.cell(row=row, column=5).number_format = "0.0%"
        row += 1
    return row + 1


def write_report(
    report_date: date,
    crop_rows: list[tuple[str, float, float, float]],
    winter_rows: list[tuple[str, float, float, float]],
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

    # Разбиваем crop_rows по блокам динамически: каждый блок содержит
    # len(rows) строк культур + 1 итоговую строку.
    block_titles = [
        ("1. ПОСЕВ ЯРОВЫХ КУЛЬТУР", len(CROP_BLOCKS[0][1]) + 1),
        ("2. ПОСЕВ ОВОЩЕЙ",          len(CROP_BLOCKS[1][1]) + 1),
    ]
    offset = 0
    for idx, (title, size) in enumerate(block_titles, start=1):
        block_data = crop_rows[offset: offset + size]
        offset += size
        row = _write_table_block(
            ws, row, title,
            ["№", "КУЛЬТУРА", "ПЛАН, ГА", "ФАКТ, ГА", "%"],
            block_data, s,
        )

    _write_table_block(
        ws,
        row,
        "3. ПОДКОРМКА ОЗИМЫХ КУЛЬТУР",
        ["№", "КУЛЬТУРА", "ПЛАН, ГА", "ФАКТ, ГА", "%"],
        winter_rows,
        s,
    )

    out = f"status_vpr_morning_{report_date}.xlsx"
    wb.save(out)
    return out


async def build_report_async(report_date: date = REPORT_DATE) -> None:
    cutoff = datetime(report_date.year, report_date.month, report_date.day, 6, 0, 0, tzinfo=TZ_MSK)

    # Загружаем справочник полей → культура
    field_crop_plan, crop_map = load_field_crop_plan()
    # Нам нужен просто dict[int, str]: field_id -> crop_name
    field_to_crop: dict[int, str] = {fid: info[0] for fid, info in field_crop_plan.items()}

    # Параллельно загружаем факт по посеву и по подкормке
    seeding_fact_by_crop, fert_facts = await asyncio.gather(
        load_fact_by_crop(SEEDING_WT_IDS, field_to_crop, cutoff),
        load_fert_facts_from_agro_ops(field_to_crop, cutoff),
    )

    crop_rows   = build_crop_rows(seeding_fact_by_crop)
    winter_rows = _build_winter_feeding_rows(fert_facts)
    output = write_report(report_date, crop_rows, winter_rows)
    print(f"\nСохранено: {output}")


def build_report(report_date: date = REPORT_DATE) -> None:
    asyncio.run(build_report_async(report_date))


if __name__ == "__main__":
    build_report()