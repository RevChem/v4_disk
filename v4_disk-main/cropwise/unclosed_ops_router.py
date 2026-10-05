"""
cropwise/unclosed_ops_router.py
================================
Эндпоинт: «Незакрытые агрооперации» — вкладка «Кастомный запрос» на панели
«Запрещённые операции по культурам».
POST /api/unclosed-ops-query  — возвращает Excel-файл.

Логика классификации операций перенесена из скрипта
unclosed_agro_operations_report.py (delete_4), адаптирована под клиент
проекта (app.cropwise.client) и дополнена:
  — фильтром по культурам (crops) — как в custom_ops_router.py
  — фильтром по операциям / видам работ (operations)
  — настраиваемой раскладкой категорий по листам Excel (sheets)
  — конструктором дополнительных данных (columns) — см. ниже
  — extend_task_range: true — только для обратной совместим API; Excel как delete_4.py

Категории «незакрытости» (как в оригинальном скрипте):
  planned      → «Запланировано»       (status == planned)
  in_progress  → «В процессе»          (status == in_progress)
  partial      → «Частично выполнено»  (done/canceled, 0% < факт < 100%)
  not_done     → «Не выполнено»        (done/canceled, факт == 0%)
Операции done/canceled с completed_percents >= 100 (полностью закрытые)
в отчёт не попадают.

Конструктор дополнительных данных (поле "columns" в теле запроса,
список любых из следующих значений):
  "machine_info" — Механизатор / Машина / Агрегат / Наработка / Топливо.
                   Данные берутся из тех же записей machine_tasks, что уже
                   используются для диапазона "начало/конец по задачам" —
                   доп. запросов к Cropwise это не добавляет.
  "field_stops"  — Простои техники на конкретном поле (machine_task_field
                   _mapping_items.stops_duration). Требует отдельного
                   запроса к MTFMI на каждую связанную задачу.
  "responsible"  — Ответственный за операцию (responsible_person_id /
                   responsible_user_ids → ФИО). Доп. запрос — только
                   локальная карта ФИО (кэшируется).
  "agro_params"  — Плановые агропараметры операции (скорость, глубина,
                   ширина рядков, норма внесения план/факт). Эти поля уже
                   есть в самой agro_operation — доп. запросов не требует.

extend_task_range (bool): по умолчанию False — диапазон "начало/конец по
задачам" (а вместе с ним machine_info и field_stops, т.к. это те же
задачи) считается только для категорий "Запланировано"/"В процессе", как
и в оригинальном скрипте. Если True — считается для всех 4 категорий
(дороже по числу запросов).

Тело запроса (JSON), пример:
  {
    "crops":      ["Соя", "Пшеница озимая"],   # пусто = все культуры
    "operations": ["2.12 Прикатывание"],        # пусто = все операции
    "date_from":  "2026-02-01",
    "date_to":    "2026-07-31",
    "sheets": [
      {"name": "Незакрытые операции", "categories": ["planned", "in_progress"]},
      {"name": "Частично выполнено",  "categories": ["partial"]},
      {"name": "Не выполнено",        "categories": ["not_done"]}
    ],
    "columns": ["machine_info", "field_stops", "responsible", "agro_params"],
    "extend_task_range": false
  }
"""
from __future__ import annotations

import asyncio
import io
from collections import OrderedDict, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

import aiohttp
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.auth.dependencies import get_current_admin_user
from app.cropwise.client import (
    CROPWISE_HEADERS,
    cropwise_season_years_for_range,
    FIO_NOT_FOUND,
    _fetch_all_pages,
    _fetch_agro_operations_by_wt,
    _fetch_mass_request,
    _fetch_mtfmi_by_task_ids,
    _fetch_mtaomi_by_agro_op_ids,
    _CROPWISE_TIMEOUT,
    fetch_local_fio_map,
)
from app.logging_config import get_logger
from app.users.models import User

logger = get_logger("cropwise.unclosed_ops_router")

router = APIRouter(prefix="/api/unclosed-ops-query", tags=["Незакрытые агрооперации"])

TZ_MSK = timezone(timedelta(hours=3))

_OPS_SEM = asyncio.Semaphore(20)

# ── Справочники агрофирм (как и в остальных роутерах проекта) ──────────────
FIRM_TO_FOLDER = {
    "АФ Чистопольская": 3, "АгроРаздолье": 4, "Аксу-Агро": 5,
    "НурлатАгро": 11, "Дрожжаное Рассвет Агро": 6,
    "Актай": 7, "Лениногорская Птицефабрика": 9,
}
FOLDER_TO_FIRM = {v: k for k, v in FIRM_TO_FOLDER.items()}
FIRM_ORDER = [
    "АФ Чистопольская", "АгроРаздолье", "Аксу-Агро",
    "НурлатАгро", "Дрожжаное Рассвет Агро", "Актай",
    "Лениногорская Птицефабрика",
]

ALL_API_STATUSES = ["planned", "in_progress", "done", "canceled"]

STATUS_LABELS = {
    "planned":     "Запланировано",
    "in_progress": "В процессе",
    "done":        "Выполнено",
    "canceled":    "Отменено",
}

CATEGORY_PLANNED  = "planned"
CATEGORY_PROGRESS = "in_progress"
CATEGORY_PARTIAL  = "partial"
CATEGORY_NOT_DONE = "not_done"

CATEGORY_LABELS = {
    CATEGORY_PLANNED:  "Запланировано",
    CATEGORY_PROGRESS: "В процессе",
    CATEGORY_PARTIAL:  "Частично выполнено",
    CATEGORY_NOT_DONE: "Не выполнено",
}

DEFAULT_SHEETS = [
    {"name": "Незакрытые операции", "categories": [CATEGORY_PLANNED, CATEGORY_PROGRESS]},
    {"name": "Частично выполнено",  "categories": [CATEGORY_PARTIAL]},
    {"name": "Не выполнено",        "categories": [CATEGORY_NOT_DONE]},
]

VALID_COLUMNS = {"machine_info", "field_stops", "responsible", "agro_params"}

# Категории, для которых считаются «Начало/Конец по задачам» (как delete_4.py)
_TASK_RANGE_CATEGORIES = frozenset({CATEGORY_PLANNED, CATEGORY_PROGRESS})

_LPF_NEEDLES = ("лениногор", "птицефабрик", "лпф", "lpf")


def _norm(s: str) -> str:
    return (s or "").strip().casefold().replace(" ", "").replace("\u00a0", "")


def _to_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Схема запроса
# ---------------------------------------------------------------------------

class SheetGroup(BaseModel):
    name: str
    categories: List[str]


class UnclosedOpsRequest(BaseModel):
    crops: List[str] = Field(default_factory=list)          # пусто = все культуры
    operations: List[str] = Field(default_factory=list)     # пусто = все операции
    date_from: str
    date_to: str
    sheets: List[SheetGroup] = Field(default_factory=list)  # пусто = раскладка по умолчанию
    columns: List[str] = Field(default_factory=list)        # конструктор доп. данных
    extend_task_range: bool = False                         # диапазон по задачам на все категории


# ---------------------------------------------------------------------------
# Вспомогательные функции (папки → агрофирма)
# ---------------------------------------------------------------------------

def _folder_parent_map(gfs: list[dict]) -> dict[int, int | None]:
    out: dict[int, int | None] = {}
    for gf in gfs:
        fid = gf.get("id")
        if fid is None:
            continue
        pid = (gf.get("parent_id") or gf.get("parent_group_folder_id")
               or gf.get("group_folder_parent_id"))
        out[int(fid)] = int(pid) if pid is not None else None
    return out


def _resolve_firm(folder_id: int | None, parent_map: dict, root_ids: set) -> int | None:
    if folder_id is None:
        return None
    cur, seen = int(folder_id), set()
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


def _folder_id_chain(folder_id: int | None, parent_map: dict) -> list[int]:
    if folder_id is None:
        return []
    out, cur, seen = [], int(folder_id), set()
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


def _build_field_to_firm(fields_all, fgs_all, gfs_all) -> dict[int, str]:
    fgs_map    = {int(fg["id"]): fg for fg in fgs_all if fg.get("id") is not None}
    parent_map = _folder_parent_map(gfs_all)
    root_ids   = set(FOLDER_TO_FIRM.keys())

    gf_hints: dict[int, str] = {}
    for gf in gfs_all:
        fid = gf.get("id")
        if fid is None:
            continue
        blob = _norm(f"{gf.get('name') or ''} {gf.get('title') or ''}")
        if any(x in blob for x in _LPF_NEEDLES):
            gf_hints[int(fid)] = "Лениногорская Птицефабрика"

    field_to_firm: dict[int, str] = {}
    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        raw_folder = f.get("group_folder_id")
        fgid = f.get("field_group_id")
        if raw_folder is None and fgid is not None:
            raw_folder = fgs_map.get(int(fgid), {}).get("group_folder_id")
        rint = int(raw_folder) if raw_folder is not None else None

        firm_key = None
        for cid in _folder_id_chain(rint, parent_map):
            if cid in gf_hints:
                firm_key = gf_hints[cid]
                break
        if firm_key is None:
            root = _resolve_firm(rint, parent_map, root_ids)
            if root is not None:
                firm_key = FOLDER_TO_FIRM.get(root)
        if firm_key:
            field_to_firm[int(fid)] = firm_key

    return field_to_firm


# ---------------------------------------------------------------------------
# Даты / период
# ---------------------------------------------------------------------------

def _dt_bounds(date_from: date, date_to: date) -> tuple[str, str]:
    dt_from = datetime.combine(date_from, datetime.min.time())
    dt_to   = datetime.combine(date_to, datetime.max.time()).replace(microsecond=0)
    return dt_from.strftime("%Y-%m-%d %H:%M:%S"), dt_to.strftime("%Y-%m-%d %H:%M:%S")


def _parse_date_only(raw) -> Optional[date]:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _parse_datetime_any(raw) -> Optional[datetime]:
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.fromisoformat(text[:19])
        except ValueError:
            return None


def _op_in_period(op: dict, date_from: date, date_to: date) -> bool:
    """Проверка попадания agro_operation в период отчёта.
    Приоритет: фактическая дата закрытия -> плановый интервал."""
    completed = _parse_date_only(op.get("completed_datetime") or op.get("completed_date"))
    if completed is not None:
        return date_from <= completed <= date_to

    p_start = _parse_date_only(op.get("planned_start_date"))
    p_end   = _parse_date_only(op.get("planned_end_date")) or p_start

    if p_start is None and p_end is None:
        return True

    if p_start is None:
        p_start = p_end
    if p_end is None:
        p_end = p_start

    return p_start <= date_to and p_end >= date_from


# ---------------------------------------------------------------------------
# Категория «незакрытости»
# ---------------------------------------------------------------------------

def _completion_percent(op: dict) -> Optional[float]:
    cp = _to_float(op.get("completed_percents"))
    if cp is not None:
        return cp
    planned_area = _to_float(op.get("planned_area"))
    completed_area = _to_float(op.get("completed_area"))
    if planned_area:
        return (completed_area or 0.0) / planned_area * 100
    return None


def _classify(op: dict) -> Optional[str]:
    """Возвращает ключ категории незакрытой операции, либо None, если операция
    полностью закрыта (в отчёт не попадает)."""
    status = (op.get("status") or "").lower()

    if status == "planned":
        return CATEGORY_PLANNED
    if status == "in_progress":
        return CATEGORY_PROGRESS

    if status in ("done", "canceled"):
        partially = bool(op.get("partially_completed"))
        cp = _completion_percent(op)
        if partially or (cp is not None and cp < 100 - 1e-9):
            if cp is not None and cp <= 0 + 1e-9:
                return CATEGORY_NOT_DONE
            return CATEGORY_PARTIAL
        return None  # полностью закрыта

    return None


# ---------------------------------------------------------------------------
# Основной сбор данных (agro_operations → строки отчёта)
# ---------------------------------------------------------------------------

async def _fetch_unclosed_operations(
    date_from: date,
    date_to: date,
    crops_filter: set[str],
    operations_filter: set[str],
) -> list[dict]:
    season_years: list[int] = cropwise_season_years_for_range(date_from, date_to)
    range_fully_past = date_to < date.today()
    dt_from_str, dt_to_str = _dt_bounds(date_from, date_to)

    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS, timeout=_CROPWISE_TIMEOUT) as session:
        base_coros = [
            _fetch_all_pages(session, "fields", sem=_OPS_SEM),
            _fetch_all_pages(session, "field_groups", sem=_OPS_SEM),
            _fetch_all_pages(session, "group_folders", sem=_OPS_SEM),
            _fetch_all_pages(session, "work_types", sem=_OPS_SEM),
        ]
        if crops_filter:
            his_pages = await asyncio.gather(*[
                _fetch_all_pages(
                    session, "history_items", {"year": str(yr)}, sem=_OPS_SEM,
                )
                for yr in season_years
            ])
            his_all = [hi for page in his_pages for hi in page]
            fields_all, fgs_all, gfs_all, wt_raw, crops_raw = await asyncio.gather(
                *base_coros,
                _fetch_all_pages(session, "crops", sem=_OPS_SEM),
            )
        else:
            fields_all, fgs_all, gfs_all, wt_raw = await asyncio.gather(*base_coros)
            crops_raw, his_all = [], []

    field_name_map: dict[int, str] = {}
    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        field_name_map[int(fid)] = (f.get("name") or f.get("title") or f"Поле #{fid}").strip()

    field_to_firm = _build_field_to_firm(fields_all, fgs_all, gfs_all)

    work_type_name: dict[int, str] = {}
    for wt in wt_raw:
        wid = wt.get("id")
        if wid is not None:
            work_type_name[int(wid)] = (wt.get("name") or wt.get("title") or f"Вид работы #{wid}").strip()

    # ── Культура по полю — только если задан фильтр crops ─────────────────
    field_to_crop: dict[int, str] = {}
    if crops_filter:
        crop_id_name = {c["id"]: (c.get("name") or c.get("title", "")) for c in crops_raw}
        for hi in his_all:
            fid = hi.get("field_id")
            cid = hi.get("crop_id")
            if fid and cid and hi.get("active"):
                cname = crop_id_name.get(cid, "")
                if cname:
                    field_to_crop[int(fid)] = cname

    # ── Загружаем agro_operations ────────────────────────────────────────────
    # Если задан фильтр operations — сужаем выборку work_type_id на API
    # (batch work_type_id=1,2,3,...). Иначе — status × season без wt-фильтра.
    wt_ids_filter: list[int] | None = None
    if operations_filter:
        wt_ids_filter = sorted({
            wid for wid, name in work_type_name.items()
            if name in operations_filter
        })
        logger.info(
            f"agro_operations: filter by {len(wt_ids_filter)} work_type_id "
            f"(operations={sorted(operations_filter)})"
        )
        if not wt_ids_filter:
            logger.warning("operations filter matched no work_type_id — empty result")
            return []

    all_ops: list[dict] = []
    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS, timeout=_CROPWISE_TIMEOUT) as session:
        fetch_coros = []
        for api_status in ALL_API_STATUSES:
            for season_yr in season_years:
                params: dict = {"status": api_status, "season": season_yr}
                if range_fully_past and api_status in ("done", "canceled"):
                    params["completed_datetime_gt_eq"] = dt_from_str
                    params["completed_datetime_lt_eq"] = dt_to_str
                else:
                    params["planned_start_date_lt_eq"] = dt_to_str
                if wt_ids_filter:
                    fetch_coros.append(
                        _fetch_agro_operations_by_wt(
                            session, wt_ids_filter, params, sem=_OPS_SEM,
                            log_prefix="unclosed_ops ",
                        )
                    )
                else:
                    fetch_coros.append(
                        _fetch_all_pages(session, "agro_operations", params, sem=_OPS_SEM)
                    )
        results = await asyncio.gather(*fetch_coros)
        seen_ids: set[int] = set()
        for page in results:
            for op in page:
                oid = op.get("id")
                if oid is not None and oid not in seen_ids:
                    seen_ids.add(oid)
                    all_ops.append(op)

    logger.info(f"agro_operations fetched (deduped): {len(all_ops)}")

    # ── Фильтр по периоду + классификация «незакрытости» + фильтры ─────────
    rows: list[dict] = []
    for op in all_ops:
        if not _op_in_period(op, date_from, date_to):
            continue

        category = _classify(op)
        if category is None:
            continue  # полностью закрытая операция — не входит в отчёт

        fid = op.get("field_id")
        if fid is None:
            continue
        fid = int(fid)

        firm = field_to_firm.get(fid)
        if not firm:
            continue

        wt_id = op.get("work_type_id")
        work_type = work_type_name.get(int(wt_id), "—") if wt_id is not None else "—"

        # Фильтр по операциям (если список непуст)
        if operations_filter and work_type not in operations_filter:
            continue

        crop = field_to_crop.get(fid, "—") if crops_filter else "—"
        if crops_filter and crop not in crops_filter:
            continue

        status_key = (op.get("status") or "").lower()

        planned_area = _to_float(op.get("planned_area")) or 0.0

        covered_area = _to_float(op.get("covered_area"))
        if covered_area is None:
            covered_area = _to_float(op.get("covered_area_by_track")) or 0.0

        # Операции с обработанной площадью (по треку) < 1 га в отчёт не включаем
        if covered_area < 1.0:
            continue

        responsible_ids = set()
        rp = op.get("responsible_person_id")
        if rp is not None:
            responsible_ids.add(rp)
        for uid in (op.get("responsible_user_ids") or []):
            if uid is not None:
                responsible_ids.add(uid)

        rows.append({
            "firm":            firm,
            "fid":             fid,
            "field_name":      field_name_map.get(fid, f"Поле #{fid}"),
            "crop":            crop,
            "op_id":           op.get("id"),
            "planned_start":   _parse_date_only(op.get("planned_start_date")),
            "planned_end":     _parse_date_only(op.get("planned_end_date")),
            "work_type":       work_type,
            "status_key":      status_key,
            "status_label":    STATUS_LABELS.get(status_key, status_key or "—"),
            "category":        category,
            "planned_area":    round(planned_area, 2),
            "covered_area":    round(covered_area, 2),
            # ── enrichment (заполняется в _attach_enrichment) ──────────────
            "task_start":      None,
            "task_end":        None,
            "driver_name":     "—",
            "machine_name":    "—",
            "implement_name":  "—",
            "work_duration_h": None,
            "fuel_consumption": None,
            "stops_duration_h": None,
            "responsible_ids": responsible_ids,
            "responsible_name": "—",
            # ── агропараметры (есть сразу в agro_operations, без доп. запросов) ─
            "planned_speed":        _to_float(op.get("planned_speed")),
            "planned_depth":        _to_float(op.get("planned_depth")),
            "planned_rows_spacing": _to_float(op.get("planned_rows_spacing")),
            "planned_water_rate":   _to_float(op.get("planned_water_rate")),
            "fact_water_rate":      _to_float(op.get("fact_water_rate")),
        })

    logger.info(f"Незакрытых операций в периоде (обработанная площадь >= 1 га): {len(rows)}")
    return rows


# ---------------------------------------------------------------------------
# Обогащение: диапазон по задачам + механизатор/машина/агрегат/наработка/
# топливо + простои на поле + ответственный
# ---------------------------------------------------------------------------

async def _attach_enrichment(
    rows: list[dict], columns: set[str], extend_task_range: bool,
) -> None:
    # ── Ответственный: не требует данных о machine_tasks, считаем отдельно ─
    if "responsible" in columns:
        all_resp_ids: set[int] = set()
        for row in rows:
            all_resp_ids |= {int(i) for i in row["responsible_ids"] if i is not None}
        if all_resp_ids:
            fio_map = await fetch_local_fio_map()
            for row in rows:
                ids = {int(i) for i in row["responsible_ids"] if i is not None}
                if not ids:
                    continue
                names = sorted({fio_map.get(i) or FIO_NOT_FOUND for i in ids})
                row["responsible_name"] = "; ".join(names) if names else "—"

    target_rows = rows if extend_task_range else [
        row for row in rows if row["category"] in _TASK_RANGE_CATEGORIES
    ]
    if not target_rows:
        return

    op_ids = list(dict.fromkeys(int(row["op_id"]) for row in target_rows if row.get("op_id") is not None))
    if not op_ids:
        return

    # ── 1) mtaomi по agro_operation_id (официальный фильтр API, batch) ───
    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS, timeout=_CROPWISE_TIMEOUT) as session:
        op_to_task_ids = await _fetch_mtaomi_by_agro_op_ids(
            session, op_ids, sem=_OPS_SEM, log_prefix="unclosed_ops ",
        )

    task_ids = list({tid for tids in op_to_task_ids.values() for tid in tids})
    if not task_ids:
        return

    # ── 2-3) machine_tasks (mass_request) + machines/implements/ФИО ───────
    need_machine_info = "machine_info" in columns
    need_field_stops  = "field_stops" in columns

    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS, timeout=_CROPWISE_TIMEOUT) as session:
        coros = [_fetch_mass_request(session, "machine_tasks", task_ids, sem=_OPS_SEM)]
        if need_machine_info:
            coros.append(_fetch_all_pages(session, "machines", sem=_OPS_SEM))
            coros.append(_fetch_all_pages(session, "implements", sem=_OPS_SEM))
            coros.append(fetch_local_fio_map())  # локальная БД — не бьёт по Cropwise
        results = await asyncio.gather(*coros)

    idx = 0
    task_records = results[idx]; idx += 1
    task_map: dict[int, dict] = {
        int(t["id"]): t for t in task_records if t.get("id") is not None
    }
    del task_records

    machine_name_map: dict[int, str] = {}
    implement_name_map: dict[int, str] = {}
    driver_fio_map: dict[int, str] = {}
    if need_machine_info:
        machines_all    = results[idx]; idx += 1
        implements_all  = results[idx]; idx += 1
        driver_fio_map  = results[idx]; idx += 1
        machine_name_map = {
            int(m["id"]): (m.get("name") or m.get("title") or m.get("model") or f"machine_{m['id']}")
            for m in machines_all if m.get("id") is not None
        }
        implement_name_map = {
            int(i["id"]): (i.get("custom_name") or i.get("name") or f"impl_{i['id']}")
            for i in implements_all if i.get("id") is not None
        }

    # ── 4) MTFMI (простои на поле) — batch по machine_task_id ───────────────
    stops_index: dict[tuple[int, int], float] = {}
    if need_field_stops:
        async with aiohttp.ClientSession(headers=CROPWISE_HEADERS, timeout=_CROPWISE_TIMEOUT) as session:
            mtfmi_map = await _fetch_mtfmi_by_task_ids(session, task_ids, sem=_OPS_SEM)
        for tid, items in mtfmi_map.items():
            for item in items:
                fid_raw = item.get("field_id")
                if fid_raw is None:
                    continue
                sd = _to_float(item.get("stops_duration"))
                if sd is None:
                    continue
                key = (int(fid_raw), int(tid))
                stops_index[key] = stops_index.get(key, 0.0) + sd

    # ── 5) Заполняем строки ──────────────────────────────────────────────
    for row in target_rows:
        op_id = row.get("op_id")
        if op_id is None:
            continue
        tids = op_to_task_ids.get(int(op_id), set())
        if not tids:
            continue

        starts: list[datetime] = []
        ends: list[datetime] = []
        drivers: set[str] = set()
        machines_: set[str] = set()
        implements_: set[str] = set()
        work_dur_sum = 0.0
        fuel_sum = 0.0
        stops_sum = 0.0
        has_work_dur = False
        has_fuel = False
        has_stops = False

        for tid in tids:
            task = task_map.get(tid)
            if task:
                st = _parse_datetime_any(task.get("start_time"))
                en = _parse_datetime_any(task.get("end_time"))
                if st is not None:
                    starts.append(st)
                if en is not None:
                    ends.append(en)

                if "machine_info" in columns:
                    did = task.get("driver_id")
                    if did is not None:
                        drivers.add(driver_fio_map.get(int(did)) or FIO_NOT_FOUND)
                    mid = task.get("machine_id")
                    if mid is not None:
                        machines_.add(machine_name_map.get(int(mid), f"machine_{mid}"))
                    iid = task.get("implement_id")
                    if iid is not None:
                        implements_.add(implement_name_map.get(int(iid), f"impl_{iid}"))
                    wd = _to_float(task.get("work_duration"))
                    if wd is not None:
                        work_dur_sum += wd
                        has_work_dur = True
                    fc = _to_float(task.get("fuel_consumption"))
                    if fc is not None:
                        fuel_sum += fc
                        has_fuel = True

            if "field_stops" in columns and row.get("fid") is not None:
                sd = stops_index.get((int(row["fid"]), int(tid)))
                if sd is not None:
                    stops_sum += sd
                    has_stops = True

        row["task_start"] = min(starts) if starts else None
        row["task_end"] = max(ends) if ends else None

        if "machine_info" in columns:
            row["driver_name"] = "; ".join(sorted(drivers)) if drivers else "—"
            row["machine_name"] = "; ".join(sorted(machines_)) if machines_ else "—"
            row["implement_name"] = "; ".join(sorted(implements_)) if implements_ else "—"
            row["work_duration_h"] = round(work_dur_sum / 3600, 2) if has_work_dur else None
            row["fuel_consumption"] = round(fuel_sum, 1) if has_fuel else None

        if "field_stops" in columns:
            row["stops_duration_h"] = round(stops_sum / 3600, 2) if has_stops else None


# ---------------------------------------------------------------------------
# Excel builder
# ---------------------------------------------------------------------------

# key, header, width, kind ("text" | "sum1" | "sum2" | "num1")
# sum1/sum2 — числовая колонка, суммируется в итогах (1/2 знака после запятой)
# num1      — числовая колонка, но НЕ суммируется в итогах (агропараметр)
EXTRA_COLUMN_DEFS: dict[str, list[tuple[str, str, int, str]]] = {
    "machine_info": [
        ("driver_name",      "Механизатор",         22, "text"),
        ("machine_name",     "Машина",               22, "text"),
        ("implement_name",   "Агрегат",              22, "text"),
        ("work_duration_h",  "Наработка, ч",         14, "sum2"),
        ("fuel_consumption", "Расход топлива, л",    16, "sum1"),
    ],
    "field_stops": [
        ("stops_duration_h", "Простои на поле, ч",   16, "sum2"),
    ],
    "responsible": [
        ("responsible_name", "Ответственный",        24, "text"),
    ],
    "agro_params": [
        ("planned_speed",        "Скорость, км/ч (план)",        16, "num1"),
        ("planned_depth",        "Глубина, см (план)",           14, "num1"),
        ("planned_rows_spacing", "Ширина рядков, см (план)",     16, "num1"),
        ("planned_water_rate",   "Норма внесения, л/га (план)",  18, "num1"),
        ("fact_water_rate",      "Норма внесения, л/га (факт)",  18, "num1"),
    ],
}
COLUMN_ORDER = ["machine_info", "field_stops", "responsible", "agro_params"]


def _sheet_wants_task_range(cats: list[str]) -> bool:
    """Колонки «Начало/Конец по задачам» — только для «Запланировано»/«В процессе»."""
    return bool(cats) and set(cats) <= _TASK_RANGE_CATEGORIES


def _default_sheet_title(cats: list[str], period_str: str) -> str:
    s = set(cats)
    if s <= _TASK_RANGE_CATEGORIES and s:
        return (
            f"Незакрытые агрооперации (Запланировано / В процессе) "
            f"в разрезе агрофирм {period_str}"
        )
    if s == {CATEGORY_PARTIAL}:
        return f"Частично выполненные агрооперации в разрезе агрофирм {period_str}"
    if s == {CATEGORY_NOT_DONE}:
        return f"Не выполненные агрооперации в разрезе агрофирм {period_str}"
    cats_label = ", ".join(CATEGORY_LABELS.get(c, c) for c in cats)
    return f"Агрооперации ({cats_label}) в разрезе агрофирм {period_str}"


def _build_excel(
    rows: list[dict], date_from: date, date_to: date,
    sheets_cfg: list[dict], columns: set[str],
) -> bytes:
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, Side
    from openpyxl.utils import get_column_letter

    extra_columns: list[tuple[str, str, int, str]] = []
    for key in COLUMN_ORDER:
        if key in columns:
            extra_columns.extend(EXTRA_COLUMN_DEFS[key])

    def _format_dt(dt: Optional[datetime]) -> str:
        return dt.strftime("%d.%m.%Y %H:%M") if dt is not None else "—"

    def _side():
        t = Side(style="thin", color="BBBBBB")
        return Border(left=t, right=t, top=t, bottom=t)

    def _cell(ws, row, col, value=None, bold=False,
              halign="left", valign="center", wrap=False, fmt=None, size=9):
        c = ws.cell(row=row, column=col, value=value)
        c.font = Font(name="Calibri", size=size, bold=bold, color="000000")
        c.alignment = Alignment(horizontal=halign, vertical=valign, wrap_text=wrap)
        c.border = _side()
        if fmt:
            c.number_format = fmt
        return c

    MAIN_HEADERS = [
        "Агрофирма", "Поле", "Номер агрооперации", "Начало (план)",
        "Конец (план)", "Начало (по задачам)", "Конец (по задачам)",
        "Вид работы", "Статус", "Категория",
        "Запланированная площадь, га", "Обработанная площадь, га (по треку)",
    ]
    MAIN_COL_W = [24, 16, 18, 14, 14, 20, 20, 34, 16, 20, 22, 24]

    BASE_HEADERS = [
        "Агрофирма", "Поле", "Номер агрооперации", "Начало (план)",
        "Конец (план)", "Вид работы", "Статус", "Категория",
        "Запланированная площадь, га", "Обработанная площадь, га (по треку)",
    ]
    BASE_COL_W = [24, 16, 18, 14, 14, 34, 16, 20, 22, 24]

    def sort_key(row):
        fi = FIRM_ORDER.index(row["firm"]) if row["firm"] in FIRM_ORDER else 999
        ps = row["planned_start"] or date.min
        return (fi, ps, row["field_name"])

    def _write_extra_cell(ws, r, col, key, kind, row):
        val = row.get(key)
        if kind == "text":
            _cell(ws, r, col, val if val not in (None, "") else "—")
        elif kind in ("sum1", "sum2"):
            fmt = "#,##0.0" if kind == "sum1" else "#,##0.00"
            _cell(ws, r, col, val if val is not None else "—",
                  halign="right", fmt=fmt if val is not None else None)
        else:
            _cell(ws, r, col, val if val is not None else "—",
                  halign="right", fmt="#,##0.0" if val is not None else None)

    def _write_sheet(
        ws, title_text: str, sheet_rows: list[dict], *, task_range_columns: bool = False,
    ) -> None:
        base_headers = (MAIN_HEADERS if task_range_columns else BASE_HEADERS)
        base_col_w = (MAIN_COL_W if task_range_columns else BASE_COL_W)
        headers = base_headers + [h for (_, h, _, _) in extra_columns]
        col_w = base_col_w + [w for (_, _, w, _) in extra_columns]
        n_base = len(base_headers)
        n_total = len(headers)

        for ci, w in enumerate(col_w, start=1):
            ws.column_dimensions[get_column_letter(ci)].width = w

        r = 1
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=n_total)
        c = ws.cell(row=r, column=1, value=title_text)
        c.font = Font(name="Calibri", size=11, bold=True, color="000000")
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = _side()
        ws.row_dimensions[r].height = 26
        r += 1

        for ci, h in enumerate(headers, start=1):
            _cell(ws, r, ci, h, bold=True, halign="center", wrap=True, size=9)
        ws.row_dimensions[r].height = 30
        ws.freeze_panes = "C3"
        r += 1

        if not sheet_rows:
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=n_total)
            c = ws.cell(row=r, column=1, value="Операций не найдено")
            c.font = Font(name="Calibri", size=10, bold=True, color="000000")
            c.alignment = Alignment(horizontal="center", vertical="center")
            c.border = _side()
            ws.row_dimensions[r].height = 18
            return

        rows_sorted = sorted(sheet_rows, key=sort_key)
        by_firm: "OrderedDict[str, list[dict]]" = OrderedDict()
        for row in rows_sorted:
            by_firm.setdefault(row["firm"], []).append(row)

        grand_planned = 0.0
        grand_covered = 0.0
        grand_extra: dict[str, float] = {
            key: 0.0 for (key, _, _, kind) in extra_columns if kind in ("sum1", "sum2")
        }

        # Индексы колонок площадей (1-based) — зависят от наличия task_range
        planned_col = 11 if task_range_columns else 9
        covered_col = 12 if task_range_columns else 10
        total_merge_end = 10 if task_range_columns else 8

        for firm, firm_rows in by_firm.items():
            firm_first_row = r
            firm_planned = 0.0
            firm_covered = 0.0
            firm_extra: dict[str, float] = {key: 0.0 for key in grand_extra}

            for row in firm_rows:
                cat_label = CATEGORY_LABELS.get(row["category"], row["category"])
                _cell(ws, r, 1, "")
                _cell(ws, r, 2, row["field_name"])
                _cell(ws, r, 3, row["op_id"], halign="center")
                _cell(ws, r, 4, row["planned_start"].strftime("%d.%m.%Y") if row["planned_start"] else "—", halign="center")
                _cell(ws, r, 5, row["planned_end"].strftime("%d.%m.%Y") if row["planned_end"] else "—", halign="center")
                if task_range_columns:
                    _cell(ws, r, 6, _format_dt(row["task_start"]), halign="center")
                    _cell(ws, r, 7, _format_dt(row["task_end"]), halign="center")
                    _cell(ws, r, 8, row["work_type"])
                    _cell(ws, r, 9, row["status_label"], halign="center")
                    _cell(ws, r, 10, cat_label, halign="center", bold=True)
                    _cell(ws, r, 11, row["planned_area"], halign="right", fmt="#,##0.00")
                    _cell(ws, r, 12, row["covered_area"], halign="right", fmt="#,##0.00")
                else:
                    _cell(ws, r, 6, row["work_type"])
                    _cell(ws, r, 7, row["status_label"], halign="center")
                    _cell(ws, r, 8, cat_label, halign="center", bold=True)
                    _cell(ws, r, 9, row["planned_area"], halign="right", fmt="#,##0.00")
                    _cell(ws, r, 10, row["covered_area"], halign="right", fmt="#,##0.00")

                for offset, (key, _, _, kind) in enumerate(extra_columns):
                    col = n_base + 1 + offset
                    _write_extra_cell(ws, r, col, key, kind, row)
                    if kind in ("sum1", "sum2"):
                        val = row.get(key)
                        if val is not None:
                            firm_extra[key] += val

                ws.row_dimensions[r].height = 14
                firm_planned += row["planned_area"]
                firm_covered += row["covered_area"]
                r += 1

            firm_last_row = r - 1
            if firm_last_row > firm_first_row:
                ws.merge_cells(start_row=firm_first_row, start_column=1, end_row=firm_last_row, end_column=1)
            fc = ws.cell(row=firm_first_row, column=1, value=firm)
            fc.font = Font(name="Calibri", size=9, bold=True)
            fc.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            for rr in range(firm_first_row, firm_last_row + 1):
                ws.cell(row=rr, column=1).border = _side()

            _cell(ws, r, 1, "")
            ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=total_merge_end)
            _cell(ws, r, 2, f"Всего ({firm})", bold=True, size=9)
            _cell(ws, r, planned_col, round(firm_planned, 2), bold=True, halign="right", fmt="#,##0.00")
            _cell(ws, r, covered_col, round(firm_covered, 2), bold=True, halign="right", fmt="#,##0.00")
            for offset, (key, _, _, kind) in enumerate(extra_columns):
                col = n_base + 1 + offset
                if kind in ("sum1", "sum2"):
                    fmt = "#,##0.0" if kind == "sum1" else "#,##0.00"
                    _cell(ws, r, col, round(firm_extra[key], 2), bold=True, halign="right", fmt=fmt)
                else:
                    _cell(ws, r, col, "", bold=True)
            ws.row_dimensions[r].height = 15
            r += 1

            grand_planned += firm_planned
            grand_covered += firm_covered
            for key in grand_extra:
                grand_extra[key] += firm_extra[key]

        _cell(ws, r, 1, "")
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=total_merge_end)
        _cell(ws, r, 2, "ИТОГО", bold=True, size=10)
        _cell(ws, r, planned_col, round(grand_planned, 2), bold=True, halign="right", fmt="#,##0.00", size=10)
        _cell(ws, r, covered_col, round(grand_covered, 2), bold=True, halign="right", fmt="#,##0.00", size=10)
        for offset, (key, _, _, kind) in enumerate(extra_columns):
            col = n_base + 1 + offset
            if kind in ("sum1", "sum2"):
                fmt = "#,##0.0" if kind == "sum1" else "#,##0.00"
                _cell(ws, r, col, round(grand_extra[key], 2), bold=True, halign="right", fmt=fmt, size=10)
            else:
                _cell(ws, r, col, "", bold=True, size=10)
        ws.row_dimensions[r].height = 16

    now_msk = datetime.now(TZ_MSK).strftime("%d.%m.%Y %H:%M")
    period_str = (
        f"за период с {date_from.strftime('%d.%m.%Y')} "
        f"по {date_to.strftime('%d.%m.%Y')} (на {now_msk})"
    )

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    used_titles: set[str] = set()

    for i, sheet_cfg in enumerate(sheets_cfg):
        cats = [c for c in sheet_cfg.get("categories", []) if c in CATEGORY_LABELS]
        if not cats:
            continue
        sheet_rows = [row for row in rows if row["category"] in cats]
        task_range_columns = _sheet_wants_task_range(cats)

        raw_name = (sheet_cfg.get("name") or " / ".join(CATEGORY_LABELS[c] for c in cats)).strip()
        safe_name = raw_name[:31] or f"Лист {i + 1}"
        base_name = safe_name
        n = 2
        while safe_name in used_titles:
            suffix = f" ({n})"
            safe_name = base_name[:31 - len(suffix)] + suffix
            n += 1
        used_titles.add(safe_name)

        ws = wb.create_sheet(safe_name)
        _write_sheet(
            ws,
            _default_sheet_title(cats, period_str),
            sheet_rows,
            task_range_columns=task_range_columns,
        )

    if not wb.worksheets:
        ws = wb.create_sheet("Незакрытые операции")
        _write_sheet(
            ws,
            f"Незакрытые агрооперации в разрезе агрофирм {period_str}",
            rows,
            task_range_columns=False,
        )

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# Эндпоинт
# ---------------------------------------------------------------------------

@router.post(
    "",
    summary="Незакрытые агрооперации — Excel",
    description=(
        "Отчёт по незакрытым агрооперациям (запланировано / в процессе / "
        "частично выполнено / не выполнено) в разрезе агрофирм. "
        "Поддерживает фильтр по культурам и операциям, настраиваемую "
        "раскладку категорий по листам Excel и конструктор доп. данных "
        "(механизатор/машина/агрегат, простои на поле, ответственный, "
        "агропараметры)."
    ),
)
async def post_unclosed_ops_query(
    body: UnclosedOpsRequest,
    current_user: User = Depends(get_current_admin_user),
):
    try:
        date_from = date.fromisoformat(body.date_from)
        date_to   = date.fromisoformat(body.date_to)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Неверный формат даты: {e}")

    if date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from не может быть позже date_to")

    sheets_cfg = [s.model_dump() for s in body.sheets] if body.sheets else DEFAULT_SHEETS

    crops_filter = {c for c in body.crops if c}
    operations_filter = {o for o in body.operations if o}
    columns = {c for c in body.columns if c in VALID_COLUMNS}

    try:
        rows = await _fetch_unclosed_operations(
            date_from, date_to, crops_filter, operations_filter,
        )
        # «Начало/Конец по задачам» — всегда для planned/in_progress (как delete_4.py)
        await _attach_enrichment(rows, columns, extend_task_range=False)
    except Exception as e:
        logger.error(f"unclosed_ops_query error: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Ошибка получения данных из Cropwise: {e}")

    try:
        xlsx_bytes = _build_excel(rows, date_from, date_to, sheets_cfg, columns)
    except Exception as e:
        logger.error(f"excel build error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ошибка формирования Excel: {e}")

    from urllib.parse import quote
    fname = f"unclosed_ops_{body.date_from}_{body.date_to}.xlsx"
    return StreamingResponse(
        io.BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )
