"""
cropwise/field_area_router.py
==============================
Эндпоинт: отчёт по обработанной площади полей.
POST /api/field-area-query  — возвращает Excel-файл.

Тело запроса (JSON):
  {
    "crop":            "Соя",
    "operations":      ["4.2 Посев ..."],   # базовые операции (общие колонки)
    "operation_ids":   [],                  # work_type IDs напрямую
    "date_from":       "2025-08-22",
    "date_to":         "2025-08-23",
    "statuses":        ["planned", "in_progress", "done", "canceled"],
    "work_timetable":  [                    # опционально: точные временные интервалы
      ["2025-08-22 15:28:57", "2025-08-22 16:33:38"]
    ],
    "column_groups": [                      # опционально: доп. пары колонок
      {
        "label":      "Пересев зерновой",   # подпись группы (необязательна)
        "operations": ["4.10 Пересев зерновой сеялкой без удобрений",
                       "4.11 Пересев пропашной сеялкой без удобрений"]
      },
      {
        "label":      "Посадка/Посев",
        "operations": ["4.1 Посадка картофеля",
                       "4.2 Посев зерновой сеялкой без удобрений"]
      }
    ]
    # Каждая column_group порождает две дополнительные колонки:
    #   «Обработанная площадь, га (label)»
    #   «Обработанная площадь по треку (факт), га (label)»
    # В строке под заголовком — мелким курсивом перечислены операции группы.
    # Таких групп может быть сколько угодно.
  }

Логика:
  — Загружаем agro_operations по каждому wt_id + status из API
  — Фильтруем agro_operations по диапазону дат (date_from / date_to)
  — Фильтруем machine_tasks: уровень 1 — дата, уровень 2 — work_timetable
  — Для каждой column_group отдельно грузим ops+mtfmi и считаем суммы по полям
  — Результат — Excel с динамическим числом колонок
"""
from __future__ import annotations

import asyncio
import io
from collections import defaultdict, OrderedDict
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

import aiohttp
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.auth.dependencies import get_current_admin_user
from app.cropwise.client import (
    CROPWISE_HEADERS,
    cropwise_season_years_for_range,
    _fetch_agro_operations_by_wt,
    _fetch_all_pages,
    _fetch_machine_tasks_by_wt,
    _fetch_mtfmi_by_task_ids,
    _CROPWISE_TIMEOUT,
)

# Семафор: не более 15 одновременных MTFMI-запросов в _fetch_ops_and_mtfmi.
# Cropwise нестабилен при большом числе параллельных запросов.
_MTFMI_SEM = asyncio.Semaphore(15)
from app.logging_config import get_logger
from app.users.models import User

logger = get_logger("cropwise.field_area_router")

router = APIRouter(prefix="/api/field-area-query-custom", tags=["Обработанная площадь полей (кастомный)"])

TZ_MSK = timezone(timedelta(hours=3))

# ── Культуры, которые не должны попадать в отчёт ────────────────────────────
SKIP_CROPS = {
    "Залежь, лес", "В пользовании у фермера",
    "Передано в аренду", "Дороги/лесополосы",
    "Овраги/неудобия", "crop_None",
}

# ── Справочники агрофирм ─────────────────────────────────────────────────────
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

STATUS_COLORS = {
    "planned":     "DDEBF7",
    "in_progress": "FCE4D6",
    "done":        "E2EFDA",
    "canceled":    "F2F2F2",
}


# ---------------------------------------------------------------------------
# Схема запроса
# ---------------------------------------------------------------------------

class ColumnGroup(BaseModel):
    """Группа операций для отдельной пары доп. колонок в Excel."""
    label: Optional[str] = None       # подпись (если None — из имён операций)
    operations: List[str] = []        # имена видов работ
    operation_ids: List[int] = []     # work_type IDs напрямую


class FieldAreaRequest(BaseModel):
    crop: Optional[str] = None
    crops: List[str] = []              # список культур (если выбрано несколько в UI)
    operations: List[str]
    operation_ids: List[int] = []
    date_from: str
    date_to: str
    statuses: List[str]
    work_timetable: Optional[List[List[str]]] = None
    column_groups: Optional[List[ColumnGroup]] = None


# ---------------------------------------------------------------------------
# Вспомогательные функции — иерархия папок
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


def _norm(s: str) -> str:
    return (s or "").strip().casefold().replace(" ", "").replace("\u00a0", "")


_LPF_NEEDLES = ("лениногор", "птицефабрик", "лпф", "lpf")


# ---------------------------------------------------------------------------
# Фильтрация по временным интервалам (work_timetable)
# ---------------------------------------------------------------------------

def _parse_timetable(
    raw: list[list[str]] | None,
) -> list[tuple[datetime, datetime]] | None:
    if not raw:
        return None
    intervals: list[tuple[datetime, datetime]] = []
    for pair in raw:
        if len(pair) != 2:
            continue
        try:
            intervals.append((datetime.fromisoformat(pair[0]),
                               datetime.fromisoformat(pair[1])))
        except (ValueError, TypeError):
            continue
    return intervals or None


def _datetime_in_timetable(dt: datetime | None,
                            intervals: list[tuple[datetime, datetime]]) -> bool:
    if dt is None:
        return False
    dn = dt.replace(tzinfo=None) if dt.tzinfo else dt
    for ts, te in intervals:
        s = ts.replace(tzinfo=None) if ts.tzinfo else ts
        e = te.replace(tzinfo=None) if te.tzinfo else te
        if s <= dn <= e:
            return True
    return False


def _task_in_timetable(task: dict,
                        intervals: list[tuple[datetime, datetime]]) -> bool:
    def _p(s: str | None) -> datetime | None:
        if not s:
            return None
        try:
            return datetime.fromisoformat(str(s)[:19])
        except ValueError:
            return None

    t0 = _p(task.get("started_at"))
    t1 = _p(task.get("completed_at"))
    if t0 is None and t1 is None:
        return True  # нет дат — не фильтруем
    if _datetime_in_timetable(t0, intervals):
        return True
    if _datetime_in_timetable(t1, intervals):
        return True
    # задача целиком перекрывает интервал
    if t0 is not None and t1 is not None:
        a = t0.replace(tzinfo=None) if t0.tzinfo else t0
        b = t1.replace(tzinfo=None) if t1.tzinfo else t1
        for ts, te in intervals:
            s = ts.replace(tzinfo=None) if ts.tzinfo else ts
            e = te.replace(tzinfo=None) if te.tzinfo else te
            if a <= s and b >= e:
                return True
    return False


# ---------------------------------------------------------------------------
# Справочник поле → агрофирма
# ---------------------------------------------------------------------------

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
# Резолвинг имён операций → wt_ids
# ---------------------------------------------------------------------------

def _resolve_wt_ids(operations: list[str],
                    operation_ids: list[int],
                    wt_name_to_ids: dict[str, list[int]]) -> list[int]:
    wt_ids: list[int] = list(operation_ids)
    for op_name in operations:
        wt_ids.extend(wt_name_to_ids.get(op_name, []))
    if not wt_ids:
        for op_name in operations:
            op_lower = op_name.lower()
            for wt_name, ids in wt_name_to_ids.items():
                if op_lower in wt_name.lower() or wt_name.lower() in op_lower:
                    wt_ids.extend(ids)
    return wt_ids


# ---------------------------------------------------------------------------
# Ядро: загрузка ops + mtfmi для заданного набора wt_ids
# ---------------------------------------------------------------------------

async def _fetch_ops_and_mtfmi(
    wt_ids: list[int],
    statuses: list[str],
    season_years: list[int],
    date_from: date,
    date_to: date,
    timetable_intervals: list[tuple[datetime, datetime]] | None,
    label: str = "",
) -> tuple[list[dict], dict[tuple[int, int | None], float]]:
    """
    Возвращает:
      filtered_ops  — agro_operations прошедшие фильтр по датам
      mtfmi_track   — dict[(field_id, agro_op_id)] → covered_area_by_track
                      из machine_tasks, прошедших фильтр дата + timetable
    """
    if not wt_ids:
        return [], {}

    # 1. agro_operations (batch work_type_id)
    all_ops: list[dict] = []
    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS, timeout=_CROPWISE_TIMEOUT) as session:
        results = await asyncio.gather(*[
            _fetch_agro_operations_by_wt(
                session, set(wt_ids),
                {"status": api_status, "season": season_yr},
                sem=_MTFMI_SEM,
                log_prefix=f"[{label}] ops ",
            )
            for api_status in statuses
            for season_yr in season_years
        ])
    seen_op_ids: set[int] = set()
    for r in results:
        for op in r:
            oid = op.get("id")
            if oid is not None and oid not in seen_op_ids:
                seen_op_ids.add(oid)
                all_ops.append(op)

    logger.info(f"[{label}] agro_operations fetched (deduped): {len(all_ops)}")

    # Фильтр по датам
    seen_flt: set[int] = set()
    filtered_ops: list[dict] = []
    for op in all_ops:
        oid = op.get("id")
        if oid is not None:
            if oid in seen_flt:
                continue
            seen_flt.add(oid)
        raw_date = (op.get("completed_date") or op.get("completed_datetime")
                    or op.get("started_date"))
        if raw_date:
            try:
                op_date = date.fromisoformat(str(raw_date)[:10])
            except ValueError:
                op_date = None
            if op_date is not None and not (date_from <= op_date <= date_to):
                continue
        filtered_ops.append(op)

    logger.info(f"[{label}] agro_operations after date filter: {len(filtered_ops)}")

    # 2. machine_tasks → mtfmi
    mtfmi_track: dict[tuple[int, int | None], float] = {}

    # ── Оптимизация: covered_area_by_track уже есть на самой AgroOperation ──
    # (см. подробный комментарий в field_area_router.py::_fetch_field_area).
    # covered_area на agro_operations — readonly алиас covered_area_by_track,
    # поэтому дорогой путь через machine_tasks (N запросов) →
    # machine_task_field_mapping_items (пачками по MTFMI_BATCH_SIZE
    # machine_task_id за один запрос — а не по одному запросу на КАЖДУЮ задачу)
    # нужен только когда задан
    # work_timetable (интервалы времени суток, доступные лишь на уровне
    # отдельных machine_tasks). В остальных случаях считаем mtfmi_track
    # напрямую из filtered_ops — без единого лишнего запроса к Cropwise.
    if not timetable_intervals:
        for op in filtered_ops:
            fid = op.get("field_id")
            if fid is None:
                continue
            val = op.get("covered_area_by_track")
            if val is None:
                val = op.get("covered_area")
            oid = op.get("id")
            key = (int(fid), int(oid) if oid is not None else None)
            try:
                mtfmi_track[key] = mtfmi_track.get(key, 0.0) + float(val or 0.0)
            except (TypeError, ValueError):
                pass
        logger.info(f"[{label}] mtfmi_track (fast path, no work_timetable): {len(mtfmi_track)} (field, op) pairs")
        return filtered_ops, mtfmi_track

    all_mt_tasks: list[dict] = []
    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS, timeout=_CROPWISE_TIMEOUT) as session:
        for season_yr in season_years:
            all_mt_tasks.extend(await _fetch_machine_tasks_by_wt(
                session, set(wt_ids), {"season": season_yr}, sem=_MTFMI_SEM,
                log_prefix=f"[{label}] mt ",
            ))

    seen_tids: set[int] = set()
    relevant_task_ids: list[int] = []
    task_to_op: dict[int, int | None] = {}

    for t in all_mt_tasks:
        tid = t.get("id")
        if tid is None or int(tid) in seen_tids:
            continue

        # Уровень 1: фильтр по дате
        raw_dt = t.get("completed_at") or t.get("started_at")
        if raw_dt:
            try:
                t_date = date.fromisoformat(str(raw_dt)[:10])
                if not (date_from <= t_date <= date_to):
                    continue
            except ValueError:
                pass

        # Уровень 2: фильтр по work_timetable
        if timetable_intervals is not None:
            if not _task_in_timetable(t, timetable_intervals):
                continue

        seen_tids.add(int(tid))
        relevant_task_ids.append(int(tid))
        aoid = t.get("agro_operation_id")
        task_to_op[int(tid)] = int(aoid) if aoid is not None else None

    logger.info(
        f"[{label}] machine_tasks after filters: {len(relevant_task_ids)}, "
        f"timetable_intervals={len(timetable_intervals) if timetable_intervals else 0}"
    )

    if relevant_task_ids:
        async with aiohttp.ClientSession(headers=CROPWISE_HEADERS, timeout=_CROPWISE_TIMEOUT) as session:
            mtfmi_items_by_task = await _fetch_mtfmi_by_task_ids(
                session, relevant_task_ids, sem=_MTFMI_SEM,
            )

        for tid in relevant_task_ids:
            op_id = task_to_op.get(tid)
            for item in mtfmi_items_by_task.get(tid, []):
                fid = item.get("field_id")
                val = item.get("covered_area_by_track")
                if fid is None or val is None:
                    continue
                item_aoid = item.get("agro_operation_id")
                key_op = int(item_aoid) if item_aoid is not None else op_id
                key = (int(fid), key_op)
                try:
                    mtfmi_track[key] = mtfmi_track.get(key, 0.0) + float(val)
                except (TypeError, ValueError):
                    pass

    logger.info(f"[{label}] mtfmi_track: {len(mtfmi_track)} (field, op) pairs")
    return filtered_ops, mtfmi_track


# ---------------------------------------------------------------------------
# Основная логика сбора данных
# ---------------------------------------------------------------------------

async def _fetch_field_area(
    crop: str | None,
    operations: list[str],
    operation_ids: list[int],
    date_from: date,
    date_to: date,
    statuses: list[str],
    work_timetable: list[list[str]] | None = None,
    column_groups: list[ColumnGroup] | None = None,
    crops: list[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Возвращает:
      rows        — строки для Excel (базовые поля)
      group_data  — список dict per column_group:
                    {label, operations, covered: {fid: float}, mtfmi: {fid: float}}
    """
    season_years: list[int] = cropwise_season_years_for_range(date_from, date_to)
    timetable_intervals = _parse_timetable(work_timetable)

    logger.info(
        f"_fetch_field_area: crop={crop!r}, ops={operations}, "
        f"date_from={date_from}, date_to={date_to}, season_years={season_years}, statuses={statuses}, "
        f"column_groups={len(column_groups) if column_groups else 0}"
    )

    # Общие справочники
    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS, timeout=_CROPWISE_TIMEOUT) as session:
        (
            fields_all, fgs_all, gfs_all,
            crops_raw, his_all_pages, wt_raw,
        ) = await asyncio.gather(
            _fetch_all_pages(session, "fields"),
            _fetch_all_pages(session, "field_groups"),
            _fetch_all_pages(session, "group_folders"),
            _fetch_all_pages(session, "crops"),
            asyncio.gather(*[
                _fetch_all_pages(session, "history_items", {"year": str(yr)})
                for yr in season_years
            ]),
            _fetch_all_pages(session, "work_types"),
        )
    his_all: list[dict] = [hi for page in his_all_pages for hi in page]

    crop_id_name: dict[int, str] = {
        c["id"]: (c.get("name") or c.get("title", ""))
        for c in crops_raw if c.get("id") is not None
    }

    field_to_crop: dict[int, str] = {}
    for hi in his_all:
        fid = hi.get("field_id")
        cid = hi.get("crop_id")
        if fid and cid and hi.get("active"):
            cname = crop_id_name.get(cid, "")
            if cname:
                field_to_crop[int(fid)] = cname
    if crop:
        field_to_crop = {fid: c for fid, c in field_to_crop.items() if c == crop}
    if crops:
        crops_set = set(crops)
        field_to_crop = {fid: c for fid, c in field_to_crop.items() if c in crops_set}
    field_to_crop = {fid: c for fid, c in field_to_crop.items() if c not in SKIP_CROPS}

    field_area: dict[int, float] = {}
    field_name_map: dict[int, str] = {}
    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        field_area[int(fid)]     = float(f.get("tillable_area") or f.get("calculated_area") or 0.0)
        field_name_map[int(fid)] = (f.get("name") or f.get("title") or f"Поле #{fid}").strip()

    fgs_name_map: dict[int, str] = {}
    for fg in fgs_all:
        fgid = fg.get("id")
        if fgid is not None:
            fgs_name_map[int(fgid)] = (fg.get("name") or fg.get("title") or f"Группа #{fgid}").strip()

    field_to_group: dict[int, str] = {}
    for f in fields_all:
        fid  = f.get("id")
        fgid = f.get("field_group_id")
        if fid is not None and fgid is not None and int(fgid) in fgs_name_map:
            field_to_group[int(fid)] = fgs_name_map[int(fgid)]

    field_to_firm = _build_field_to_firm(fields_all, fgs_all, gfs_all)

    wt_name_to_ids: dict[str, list[int]] = defaultdict(list)
    for wt in wt_raw:
        if wt.get("id") is not None:
            wt_name_to_ids[wt.get("name") or wt.get("title", "")].append(wt["id"])

    # Базовые операции
    base_wt_ids = _resolve_wt_ids(operations, operation_ids, wt_name_to_ids)
    logger.info(f"Base wt_ids={base_wt_ids}")

    filtered_ops, mtfmi_track = await _fetch_ops_and_mtfmi(
        wt_ids              = base_wt_ids,
        statuses            = statuses,
        season_years        = season_years,
        date_from           = date_from,
        date_to             = date_to,
        timetable_intervals = timetable_intervals,
        label               = "base",
    )

    # Диагностика дублей
    _by_field: dict = defaultdict(list)
    for op in filtered_ops:
        fid = op.get("field_id")
        if fid:
            _by_field[int(fid)].append({
                "op_id":   op.get("id"),
                "status":  op.get("status"),
                "wt_id":   op.get("work_type_id"),
                "date":    op.get("completed_date") or op.get("started_date"),
                "covered": op.get("covered_area"),
            })
    for fid, ops in _by_field.items():
        if len(ops) > 1:
            logger.warning(f"DUPLICATE field_id={fid}: {ops}")

    # Агрегация по field_id
    STATUS_PRIORITY = {"done": 3, "in_progress": 2, "planned": 1, "canceled": 0}
    agg: dict[int, dict] = {}

    for op in filtered_ops:
        fid = op.get("field_id")
        if not fid:
            continue
        fid = int(fid)
        if fid not in field_to_crop or not field_to_firm.get(fid):
            continue

        api_status       = (op.get("status") or "").lower()
        covered_by_track = float(op.get("covered_area_by_track") or 0.0)
        mach_work_area   = float(op.get("machine_work_area")     or 0.0)
        covered          = float(op.get("covered_area")          or 0.0)

        if fid not in agg:
            agg[fid] = {
                "firm":                        field_to_firm.get(fid, "—"),
                "field_group":                 field_to_group.get(fid, "Без группы"),
                "field_name":                  field_name_map.get(fid, f"Поле #{fid}"),
                "field_id":                    fid,
                "crop":                        field_to_crop.get(fid, "—"),
                "field_area":                  round(field_area.get(fid, 0.0), 2),
                "covered_area_by_track":       covered_by_track,
                "machine_work_area":           mach_work_area,
                "covered_area":                covered,
                "mtfmi_covered_area_by_track": round(mtfmi_track.get((fid, None), 0.0), 2),
                "status_key":                  api_status,
                "status_label":                STATUS_LABELS.get(api_status, api_status),
            }
        else:
            agg[fid]["covered_area_by_track"] += covered_by_track
            agg[fid]["machine_work_area"]     += mach_work_area
            agg[fid]["covered_area"]          += covered
            cur_p = STATUS_PRIORITY.get(agg[fid]["status_key"], 0)
            new_p = STATUS_PRIORITY.get(api_status, 0)
            if new_p > cur_p:
                agg[fid]["status_key"]   = api_status
                agg[fid]["status_label"] = STATUS_LABELS.get(api_status, api_status)

    rows: list[dict] = []
    for rec in agg.values():
        rec["covered_area_by_track"] = round(rec["covered_area_by_track"], 2)
        rec["machine_work_area"]     = round(rec["machine_work_area"],     2)
        rec["covered_area"]          = round(rec["covered_area"],          2)
        rows.append(rec)

    logger.info(f"_fetch_field_area: {len(rows)} base rows")

    # column_groups: отдельная загрузка для каждой группы
    group_data: list[dict] = []
    for gi, grp in enumerate(column_groups or []):
        grp_wt_ids = _resolve_wt_ids(grp.operations, grp.operation_ids, wt_name_to_ids)
        logger.info(f"Group[{gi}] '{grp.label}' wt_ids={grp_wt_ids}")

        grp_ops, grp_mtfmi = await _fetch_ops_and_mtfmi(
            wt_ids              = grp_wt_ids,
            statuses            = statuses,
            season_years        = season_years,
            date_from           = date_from,
            date_to             = date_to,
            timetable_intervals = timetable_intervals,
            label               = f"group[{gi}]",
        )

        grp_covered: dict[int, float]   = defaultdict(float)
        grp_mtfmi_sum: dict[int, float] = defaultdict(float)

        for op in grp_ops:
            fid = op.get("field_id")
            if fid:
                grp_covered[int(fid)] += float(op.get("covered_area") or 0.0)

        for (fid, _aoid), val in grp_mtfmi.items():
            grp_mtfmi_sum[fid] += val

        effective_label = grp.label or "; ".join(grp.operations)

        group_data.append({
            "label":      effective_label,
            "operations": grp.operations,
            "covered":    dict(grp_covered),
            "mtfmi":      dict(grp_mtfmi_sum),
        })

    return rows, group_data


# ---------------------------------------------------------------------------
# Excel builder
# ---------------------------------------------------------------------------

def _build_excel(
    rows: list[dict],
    crop: str | None,
    operations: list[str],
    date_from: date,
    date_to: date,
    statuses: list[str],
    group_data: list[dict] | None = None,
) -> bytes:
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except ImportError as e:
        raise RuntimeError(f"openpyxl not installed: {e}")

    _C_HDR     = "8093B3"
    _C_TITLE   = "4472C4"
    _C_TOTAL   = "BDD7EE"
    _C_FIRM    = "D9E1F2"
    _C_GROUP   = "EBF0F8"
    _C_CROP    = "E2EFDA"
    _C_ODD     = "FFFFFF"
    _C_GRP_COL = "FFF2CC"   # светло-жёлтый для заголовков доп. колонок

    def _side():
        t = Side(style="thin", color="BBBBBB")
        return Border(left=t, right=t, top=t, bottom=t)

    def _cell(ws, row, col, value=None, bold=False, italic=False, bg=None,
              color="000000", halign="left", valign="center",
              wrap=False, fmt=None, size=9):
        c = ws.cell(row=row, column=col, value=value)
        c.font      = Font(name="Calibri", size=size, bold=bold, italic=italic, color=color)
        c.alignment = Alignment(horizontal=halign, vertical=valign, wrap_text=wrap)
        c.border    = _side()
        if bg:
            c.fill = PatternFill("solid", fgColor=bg)
        if fmt:
            c.number_format = fmt
        return c

    # ── Динамическая разметка колонок ────────────────────────────────────────
    # Фиксированные: 1-9  (№, Агрофирма, Поле, Культура, Площадь, Трек, МашРабота, Обраб, МтфмиТрек)
    # Группы:        по 2 колонки на группу — covered + mtfmi
    # Статус:        последняя колонка

    N_FIXED = 9
    n_groups = len(group_data) if group_data else 0

    def _col_cov(gi: int) -> int:   # covered_area для группы gi
        return N_FIXED + 1 + gi * 2

    def _col_mfm(gi: int) -> int:   # mtfmi для группы gi
        return N_FIXED + 2 + gi * 2

    STATUS_COL = N_FIXED + 1 + n_groups * 2
    TOTAL_COLS = STATUS_COL

    COL_W: dict[int, float] = {
        1: 5, 2: 28, 3: 30, 4: 22, 5: 16,
        6: 22, 7: 22, 8: 20, 9: 24,
    }
    for gi in range(n_groups):
        COL_W[_col_cov(gi)] = 22
        COL_W[_col_mfm(gi)] = 24
    COL_W[STATUS_COL] = 22

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Обработанная площадь"

    for ci, w in COL_W.items():
        ws.column_dimensions[get_column_letter(ci)].width = w

    # ── Строка 1: заголовок отчёта ───────────────────────────────────────────
    r = 1
    ops_str   = ", ".join(operations) if operations else "все"
    crop_str  = crop or "все культуры"
    title_text = (
        f"Обработанная площадь полей  |  Культура: {crop_str}  |  "
        f"Операция: {ops_str}  |  "
        f"{date_from.strftime('%d.%m.%Y')} – {date_to.strftime('%d.%m.%Y')}"
    )
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=TOTAL_COLS)
    c = ws.cell(row=r, column=1, value=title_text)
    c.font      = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.fill      = PatternFill("solid", fgColor=_C_TITLE)
    c.border    = _side()
    ws.row_dimensions[r].height = 26
    r += 1

    # ── Строка 2: основные заголовки колонок ─────────────────────────────────
    FIXED_HEADERS = {
        1: "№",
        2: "Агрофирма / Группа полей",
        3: "Наименование поля",
        4: "Культура",
        5: "Площадь поля, га",
        6: "Покрытая площадь (трек), га",
        7: "Площадь работы машины, га",
        8: "Обработанная площадь, га",
        9: "Обработанная площадь по треку (факт), га",
    }
    for ci, h in FIXED_HEADERS.items():
        _cell(ws, r, ci, h, bold=True, bg=_C_HDR, color="FFFFFF",
              halign="center", valign="center", wrap=True, size=9)

    for gi, gd in enumerate(group_data or []):
        lbl = gd["label"]
        _cell(ws, r, _col_cov(gi),
              f"Обработанная площадь, га\n({lbl})",
              bold=True, bg=_C_GRP_COL, color="000000",
              halign="center", valign="center", wrap=True, size=9)
        _cell(ws, r, _col_mfm(gi),
              f"Обработанная площадь по треку (факт), га\n({lbl})",
              bold=True, bg=_C_GRP_COL, color="000000",
              halign="center", valign="center", wrap=True, size=9)

    _cell(ws, r, STATUS_COL, "Статус", bold=True, bg=_C_HDR, color="FFFFFF",
          halign="center", valign="center", wrap=True, size=9)
    ws.row_dimensions[r].height = 52
    r += 1

    # ── Строка 3: подписи операций для групп (курсив, мелкий шрифт) ─────────
    for ci in range(1, N_FIXED + 1):
        _cell(ws, r, ci, "", bg=_C_HDR)
    for gi, gd in enumerate(group_data or []):
        ops_text = " / ".join(gd["operations"])
        _cell(ws, r, _col_cov(gi), ops_text,
              italic=True, bg=_C_GRP_COL, color="595959",
              halign="center", valign="center", wrap=True, size=7)
        _cell(ws, r, _col_mfm(gi), ops_text,
              italic=True, bg=_C_GRP_COL, color="595959",
              halign="center", valign="center", wrap=True, size=7)
    _cell(ws, r, STATUS_COL, "", bg=_C_HDR)
    ws.row_dimensions[r].height = 28
    ws.freeze_panes = "B4"
    r += 1

    # ── Данные ───────────────────────────────────────────────────────────────
    if not rows:
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=TOTAL_COLS)
        c = ws.cell(row=r, column=1, value="Данных не найдено")
        c.font      = Font(name="Calibri", size=10, bold=True, color="375623")
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.fill      = PatternFill("solid", fgColor="E2EFDA")
        c.border    = _side()
        ws.row_dimensions[r].height = 18
    else:
        def sort_key(row):
            fi = FIRM_ORDER.index(row["firm"]) if row["firm"] in FIRM_ORDER else 999
            return (fi, row["field_group"], row["crop"], row["field_name"])

        rows_sorted = sorted(rows, key=sort_key)

        by_firm: dict = OrderedDict()
        for row in rows_sorted:
            f      = row["firm"]
            g      = row["field_group"]
            c_name = row["crop"]
            by_firm.setdefault(f, OrderedDict()).setdefault(g, OrderedDict()).setdefault(c_name, []).append(row)

        grand_area      = 0.0
        grand_cov       = 0.0
        grand_grp_cov   = [0.0] * n_groups
        grand_grp_mtfmi = [0.0] * n_groups

        for firm, groups in by_firm.items():
            # Заголовок агрофирмы
            _cell(ws, r, 1, "", bg=_C_FIRM)
            ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=TOTAL_COLS)
            _cell(ws, r, 2, firm, bold=True, bg=_C_FIRM, size=9)
            ws.row_dimensions[r].height = 13
            r += 1

            firm_area      = 0.0
            firm_cov       = 0.0
            firm_grp_cov   = [0.0] * n_groups
            firm_grp_mtfmi = [0.0] * n_groups

            for grp_name, crops_dict in groups.items():
                # Заголовок группы полей
                _cell(ws, r, 1, "", bg=_C_GROUP)
                ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=TOTAL_COLS)
                _cell(ws, r, 2, f"  {grp_name}", bold=True, bg=_C_GROUP, size=9)
                ws.row_dimensions[r].height = 13
                r += 1

                grp_area      = 0.0
                grp_cov       = 0.0
                grp_grp_cov   = [0.0] * n_groups
                grp_grp_mtfmi = [0.0] * n_groups

                for crop_name, field_rows in crops_dict.items():
                    # Заголовок культуры
                    _cell(ws, r, 1, "", bg=_C_CROP)
                    ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=TOTAL_COLS)
                    _cell(ws, r, 2, f"    {crop_name}", bold=True, bg=_C_CROP, size=9)
                    ws.row_dimensions[r].height = 13
                    r += 1

                    crop_area      = 0.0
                    crop_cov       = 0.0
                    crop_grp_cov   = [0.0] * n_groups
                    crop_grp_mtfmi = [0.0] * n_groups

                    num = 1
                    for frow in sorted(field_rows, key=lambda x: x["field_name"].lower()):
                        row_bg = STATUS_COLORS.get(frow["status_key"], _C_ODD)
                        fid    = frow["field_id"]

                        _cell(ws, r, 1, num,                                 bg=row_bg, halign="center")
                        _cell(ws, r, 2, grp_name,                            bg=row_bg)
                        _cell(ws, r, 3, frow["field_name"],                  bg=row_bg)
                        _cell(ws, r, 4, frow["crop"],                        bg=row_bg)
                        _cell(ws, r, 5, frow["field_area"],                  bg=row_bg, halign="right", fmt="#,##0.00")
                        _cell(ws, r, 6, frow["covered_area_by_track"],       bg=row_bg, halign="right", fmt="#,##0.00")
                        _cell(ws, r, 7, frow["machine_work_area"],           bg=row_bg, halign="right", fmt="#,##0.00")
                        _cell(ws, r, 8, frow["covered_area"],                bg=row_bg, halign="right", fmt="#,##0.00")
                        _cell(ws, r, 9, frow["mtfmi_covered_area_by_track"], bg=row_bg, halign="right", fmt="#,##0.00")

                        # Доп. колонки групп
                        for gi, gd in enumerate(group_data or []):
                            gc  = round(gd["covered"].get(fid, 0.0), 2)
                            gm  = round(gd["mtfmi"].get(fid, 0.0),   2)
                            _cell(ws, r, _col_cov(gi), gc, bg=row_bg, halign="right", fmt="#,##0.00")
                            _cell(ws, r, _col_mfm(gi), gm, bg=row_bg, halign="right", fmt="#,##0.00")
                            crop_grp_cov[gi]   += gc
                            crop_grp_mtfmi[gi] += gm

                        _cell(ws, r, STATUS_COL, frow["status_label"], bg=row_bg, halign="center")
                        ws.row_dimensions[r].height = 12

                        crop_area += frow["field_area"]
                        crop_cov  += frow["covered_area"]
                        grp_area  += frow["field_area"]
                        grp_cov   += frow["covered_area"]
                        firm_area += frow["field_area"]
                        firm_cov  += frow["covered_area"]
                        r += 1
                        num += 1

                    # Итого по культуре
                    _CT = "C6E0B4"
                    _cell(ws, r, 1, "",                       bg=_CT)
                    _cell(ws, r, 2, f"    Итого {crop_name}", bold=True, bg=_CT, size=9)
                    _cell(ws, r, 3, "",                       bg=_CT)
                    _cell(ws, r, 4, "",                       bg=_CT)
                    _cell(ws, r, 5, round(crop_area, 2),      bold=True, bg=_CT, halign="right", fmt="#,##0.00")
                    _cell(ws, r, 6, "",                       bg=_CT)
                    _cell(ws, r, 7, "",                       bg=_CT)
                    _cell(ws, r, 8, round(crop_cov, 2),       bold=True, bg=_CT, halign="right", fmt="#,##0.00")
                    _cell(ws, r, 9, "",                       bg=_CT)
                    for gi in range(n_groups):
                        _cell(ws, r, _col_cov(gi), round(crop_grp_cov[gi],   2), bold=True, bg=_CT, halign="right", fmt="#,##0.00")
                        _cell(ws, r, _col_mfm(gi), round(crop_grp_mtfmi[gi], 2), bold=True, bg=_CT, halign="right", fmt="#,##0.00")
                        grp_grp_cov[gi]   += crop_grp_cov[gi]
                        grp_grp_mtfmi[gi] += crop_grp_mtfmi[gi]
                    _cell(ws, r, STATUS_COL, "", bg=_CT)
                    ws.row_dimensions[r].height = 13
                    r += 1

                # Итого по группе полей
                _CG = "D9E1F2"
                _cell(ws, r, 1, "",                       bg=_CG)
                _cell(ws, r, 2, f"  Итого {grp_name}",   bold=True, bg=_CG, size=9)
                _cell(ws, r, 3, "",                       bg=_CG)
                _cell(ws, r, 4, "",                       bg=_CG)
                _cell(ws, r, 5, round(grp_area, 2),       bold=True, bg=_CG, halign="right", fmt="#,##0.00")
                _cell(ws, r, 6, "",                       bg=_CG)
                _cell(ws, r, 7, "",                       bg=_CG)
                _cell(ws, r, 8, round(grp_cov, 2),        bold=True, bg=_CG, halign="right", fmt="#,##0.00")
                _cell(ws, r, 9, "",                       bg=_CG)
                for gi in range(n_groups):
                    _cell(ws, r, _col_cov(gi), round(grp_grp_cov[gi],   2), bold=True, bg=_CG, halign="right", fmt="#,##0.00")
                    _cell(ws, r, _col_mfm(gi), round(grp_grp_mtfmi[gi], 2), bold=True, bg=_CG, halign="right", fmt="#,##0.00")
                    firm_grp_cov[gi]   += grp_grp_cov[gi]
                    firm_grp_mtfmi[gi] += grp_grp_mtfmi[gi]
                _cell(ws, r, STATUS_COL, "", bg=_CG)
                ws.row_dimensions[r].height = 13
                r += 1

            # Итого по агрофирме
            _cell(ws, r, 1, "",                  bg=_C_TOTAL)
            _cell(ws, r, 2, f"Итого {firm}",     bold=True, bg=_C_TOTAL, size=9)
            _cell(ws, r, 3, "",                  bg=_C_TOTAL)
            _cell(ws, r, 4, "",                  bg=_C_TOTAL)
            _cell(ws, r, 5, round(firm_area, 2), bold=True, bg=_C_TOTAL, halign="right", fmt="#,##0.00")
            _cell(ws, r, 6, "",                  bg=_C_TOTAL)
            _cell(ws, r, 7, "",                  bg=_C_TOTAL)
            _cell(ws, r, 8, round(firm_cov, 2),  bold=True, bg=_C_TOTAL, halign="right", fmt="#,##0.00")
            _cell(ws, r, 9, "",                  bg=_C_TOTAL)
            for gi in range(n_groups):
                _cell(ws, r, _col_cov(gi), round(firm_grp_cov[gi],   2), bold=True, bg=_C_TOTAL, halign="right", fmt="#,##0.00")
                _cell(ws, r, _col_mfm(gi), round(firm_grp_mtfmi[gi], 2), bold=True, bg=_C_TOTAL, halign="right", fmt="#,##0.00")
                grand_grp_cov[gi]   += firm_grp_cov[gi]
                grand_grp_mtfmi[gi] += firm_grp_mtfmi[gi]
            _cell(ws, r, STATUS_COL, "", bg=_C_TOTAL)
            ws.row_dimensions[r].height = 14
            r += 1

            grand_area += firm_area
            grand_cov  += firm_cov

        # Гранд-итог
        _cell(ws, r, 1, "",                   bg=_C_HDR)
        _cell(ws, r, 2, "ИТОГО",              bold=True, bg=_C_HDR, color="FFFFFF", size=10)
        _cell(ws, r, 3, "",                   bg=_C_HDR)
        _cell(ws, r, 4, "",                   bg=_C_HDR)
        _cell(ws, r, 5, round(grand_area, 2), bold=True, bg=_C_HDR, color="FFFFFF", halign="right", fmt="#,##0.00", size=10)
        _cell(ws, r, 6, "",                   bg=_C_HDR)
        _cell(ws, r, 7, "",                   bg=_C_HDR)
        _cell(ws, r, 8, round(grand_cov,  2), bold=True, bg=_C_HDR, color="FFFFFF", halign="right", fmt="#,##0.00", size=10)
        _cell(ws, r, 9, "",                   bg=_C_HDR)
        for gi in range(n_groups):
            _cell(ws, r, _col_cov(gi), round(grand_grp_cov[gi],   2),
                  bold=True, bg=_C_HDR, color="FFFFFF", halign="right", fmt="#,##0.00", size=10)
            _cell(ws, r, _col_mfm(gi), round(grand_grp_mtfmi[gi], 2),
                  bold=True, bg=_C_HDR, color="FFFFFF", halign="right", fmt="#,##0.00", size=10)
        _cell(ws, r, STATUS_COL, "", bg=_C_HDR)
        ws.row_dimensions[r].height = 16

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# Эндпоинт
# ---------------------------------------------------------------------------

@router.post("", summary="Обработанная площадь полей — Excel")
async def post_field_area_query(
    body: FieldAreaRequest,
    current_user: User = Depends(get_current_admin_user),
):
    try:
        date_from = date.fromisoformat(body.date_from)
        date_to   = date.fromisoformat(body.date_to)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Неверный формат даты: {e}")

    if date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from не может быть позже date_to")
    if not body.operations and not body.operation_ids:
        raise HTTPException(status_code=422, detail="Укажите хотя бы одну операцию")
    if not body.statuses:
        raise HTTPException(status_code=422, detail="Укажите хотя бы один статус")

    bad = set(body.statuses) - set(ALL_API_STATUSES)
    if bad:
        raise HTTPException(
            status_code=422,
            detail=f"Неизвестные статусы: {bad}. Допустимые: {sorted(ALL_API_STATUSES)}"
        )

    for gi, grp in enumerate(body.column_groups or []):
        if not grp.operations and not grp.operation_ids:
            raise HTTPException(
                status_code=422,
                detail=f"column_groups[{gi}]: укажите хотя бы одну операцию"
            )

    selected_crops = [c for c in (body.crops or []) if c] or ([body.crop] if body.crop else [])
    crop_label = ", ".join(selected_crops) if selected_crops else None

    try:
        rows, group_data = await _fetch_field_area(
            crop           = body.crop or None,
            crops          = selected_crops or None,
            operations     = body.operations,
            operation_ids  = body.operation_ids,
            date_from      = date_from,
            date_to        = date_to,
            statuses       = body.statuses,
            work_timetable = body.work_timetable,
            column_groups  = body.column_groups,
        )
    except Exception as e:
        logger.error(f"field_area_query error: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Ошибка получения данных из Cropwise: {e}")

    try:
        xlsx_bytes = _build_excel(
            rows       = rows,
            crop       = crop_label,
            operations = body.operations,
            date_from  = date_from,
            date_to    = date_to,
            statuses   = body.statuses,
            group_data = group_data,
        )
    except Exception as e:
        logger.error(f"excel build error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ошибка формирования Excel: {e}")

    from urllib.parse import quote
    if len(selected_crops) > 1:
        crop_part = f"{selected_crops[0][:12]}_i_esche_{len(selected_crops) - 1}".replace(" ", "_")
    else:
        crop_part = (selected_crops[0] if selected_crops else "vse")[:15].replace(" ", "_")
    fname = f"field_area_{crop_part}_{body.date_from}_{body.date_to}.xlsx"
    return StreamingResponse(
        io.BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )