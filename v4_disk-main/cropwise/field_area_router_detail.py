"""
cropwise/field_area_router_detail.py
=====================================
Эндпоинт: детальный отчёт по обработанной площади полей.
POST /api/field-area-query-detail  — возвращает Excel-файл.

Отличие от /api/field-area-query (field_area_router.py):
  — Строки не схлопываются по field_id: каждая agro_operation — отдельная строка.
  — Колонка называется «Обработанная площадь поля, детальная, га».
  — Значение берётся из covered_area (агро-операция) — ровно так же,
    как «Обработанная площадь, га» в базовом роутере.
  — Дополнительно выводятся: op_id, Вид работы, Дата операции.
  — Сортировка строк внутри поля: done → in_progress → planned → canceled,
    затем по дате.

Тело запроса (JSON) — идентично FieldAreaRequest из field_area_router.py:
  {
    "crop":          "Соя",              # одна культура (null = все)
    "operations":    ["4.2 Посев ..."],  # список видов работ (по имени)
    "operation_ids": [],                 # work_type IDs напрямую
    "date_from":     "2025-08-22",
    "date_to":       "2025-08-23",
    "statuses":      ["planned", "in_progress", "done", "canceled"],
    "work_timetable": [                  # опционально
      ["2025-08-22 15:28:57", "2025-08-22 16:33:38"]
    ]
  }
"""
from __future__ import annotations

import asyncio
import io
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

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
    _fetch_mtaomi_by_agro_op_ids,
    _fetch_mtfmi_by_task_ids,
    _CROPWISE_TIMEOUT,
)
from app.logging_config import get_logger
from app.users.models import User

logger = get_logger("cropwise.field_area_router_detail")

router = APIRouter(
    prefix="/api/field-area-query-detail",
    tags=["Обработанная площадь полей (детальная)"],
)

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

STATUS_PRIORITY = {"done": 3, "in_progress": 2, "planned": 1, "canceled": 0}

# FIX 1: семафоры — одиночные экземпляры на модуль, не создаются в цикле
_OPS_SEM   = asyncio.Semaphore(20)
_MTFMI_SEM = asyncio.Semaphore(15)


# ---------------------------------------------------------------------------
# Схема запроса (идентична FieldAreaRequest из field_area_router.py)
# ---------------------------------------------------------------------------

class FieldAreaDetailRequest(BaseModel):
    crop: Optional[str] = None
    crops: List[str] = []              # список культур (если выбрано несколько в UI)
    operations: List[str]
    operation_ids: List[int] = []
    date_from: str
    date_to: str
    statuses: List[str]
    work_timetable: Optional[List[List[str]]] = None


# ---------------------------------------------------------------------------
# Вспомогательные функции (идентичны field_area_router.py)
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
# Вспомогательные функции для фильтрации по временным интервалам
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
            t_start = datetime.fromisoformat(pair[0])
            t_end   = datetime.fromisoformat(pair[1])
            intervals.append((t_start, t_end))
        except (ValueError, TypeError):
            continue
    return intervals or None


def _datetime_in_timetable(
    dt: datetime | None,
    intervals: list[tuple[datetime, datetime]],
) -> bool:
    if dt is None:
        return False
    dt_naive = dt.replace(tzinfo=None) if dt.tzinfo else dt
    for t_start, t_end in intervals:
        s = t_start.replace(tzinfo=None) if t_start.tzinfo else t_start
        e = t_end.replace(tzinfo=None)   if t_end.tzinfo   else t_end
        if s <= dt_naive <= e:
            return True
    return False


def _task_in_timetable(
    task: dict,
    intervals: list[tuple[datetime, datetime]],
) -> bool:
    def _parse(s: str | None) -> datetime | None:
        if not s:
            return None
        try:
            return datetime.fromisoformat(str(s)[:19])
        except ValueError:
            return None

    t_start = _parse(task.get("started_at"))
    t_end   = _parse(task.get("completed_at"))

    if t_start is None and t_end is None:
        return True

    if _datetime_in_timetable(t_start, intervals):
        return True
    if _datetime_in_timetable(t_end, intervals):
        return True

    if t_start is not None and t_end is not None:
        ts = t_start.replace(tzinfo=None) if t_start.tzinfo else t_start
        te = t_end.replace(tzinfo=None)   if t_end.tzinfo   else t_end
        for iv_s, iv_e in intervals:
            s = iv_s.replace(tzinfo=None) if iv_s.tzinfo else iv_s
            e = iv_e.replace(tzinfo=None) if iv_e.tzinfo else iv_e
            if ts <= s and te >= e:
                return True

    return False


def _build_field_to_firm(
    fields_all: list[dict],
    fgs_all: list[dict],
    gfs_all: list[dict],
) -> dict[int, str]:
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


def _build_manual_index(
    mtaomi_all: list[dict],
    mtfmi_map: dict[int, list[dict]],
    op_field_map: dict[int, int],
) -> dict[int, list[int]]:
    """
    Строит индекс: agro_operation_id -> [machine_task_id, ...] где
    manually_defined_covered_area=True именно для поля данной операции.
    """
    op_to_tasks: dict[int, set[int]] = {}
    for rec in mtaomi_all:
        aoid = rec.get("agro_operation_id")
        mtid = rec.get("machine_task_id")
        if aoid is not None and mtid is not None:
            op_to_tasks.setdefault(int(aoid), set()).add(int(mtid))

    result: dict[int, list[int]] = {}
    for aoid, task_ids in op_to_tasks.items():
        target_fid = op_field_map.get(aoid)
        if target_fid is None:
            continue
        manual_tasks: list[int] = []
        for tid in sorted(task_ids):
            for item in mtfmi_map.get(tid, []):
                if (item.get("field_id") == target_fid
                        and item.get("manually_defined_covered_area")):
                    manual_tasks.append(tid)
                    break
        if manual_tasks:
            result[aoid] = manual_tasks
    return result


# ---------------------------------------------------------------------------
# Основная логика сбора данных
# ---------------------------------------------------------------------------

async def _fetch_field_area_detail(
    crop: str | None,
    operations: list[str],
    operation_ids: list[int],
    date_from: date,
    date_to: date,
    statuses: list[str],
    work_timetable: list[list[str]] | None = None,
    crops: list[str] | None = None,
) -> list[dict]:
    """
    Возвращает список строк — по одной на каждую agro_operation:
      firm, field_group, field_name, field_id, crop, field_area,
      op_id, work_type_name,
      op_date,                     # completed_date / started_date
      covered_area_detail,         # covered_area из agro_operation
      status_label, status_key
    """
    # Годы "сезона" — см. подробное объяснение в field_area_router.py::
    # _fetch_field_area. Наивное season = date_from.year часто неверно,
    # когда диапазон начинается осенью предыдущего календарного года —
    # запрашиваем все годы, которые затрагивает [date_from, date_to].
    season_years: list[int] = cropwise_season_years_for_range(date_from, date_to)
    timetable_intervals = _parse_timetable(work_timetable)

    # Диапазон целиком в прошлом? Если да — можно безопасно фильтровать
    # machine_tasks/agro_operations по датовым comparison-полям
    # (end_time / completed_datetime): все интересующие записи уже закрыты,
    # и незакрытые ("в процессе", end_time/completed_datetime не проставлены)
    # к этому диапазону не относятся. Если date_to захватывает сегодня/
    # будущее — такой фильтр рискует отбросить незакрытые задания/операции,
    # которые как раз являются "в процессе" на сегодня, поэтому для них
    # датовый фильтр не применяем и оставляем season-only (как раньше).
    range_fully_past = date_to < date.today()

    logger.info(
        f"_fetch_field_area_detail: crop={crop!r}, ops={operations}, "
        f"date_from={date_from}, date_to={date_to}, season_years={season_years}, statuses={statuses}"
    )

    # FIX 2: одна сессия для всех справочных данных + work_types
    # Все 6 запросов летят параллельно в одном gather
    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS, timeout=_CROPWISE_TIMEOUT) as session:
        (
            fields_all, fgs_all, gfs_all,
            crops_raw, his_all_pages,
            wt_raw,
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

    # ── Справочники ──────────────────────────────────────────────────────────

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

    field_area_map: dict[int, float] = {}
    field_name_map: dict[int, str] = {}
    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        field_area_map[int(fid)] = float(f.get("tillable_area") or f.get("calculated_area") or 0.0)
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
    wt_id_to_name: dict[int, str] = {}
    for wt in wt_raw:
        if wt.get("id") is not None:
            wt_name = wt.get("name") or wt.get("title", "")
            wt_name_to_ids[wt_name].append(wt["id"])
            wt_id_to_name[wt["id"]] = wt_name

    # ── Резолвинг wt_ids ─────────────────────────────────────────────────────

    wt_ids: list[int] = list(operation_ids)

    for op_name in operations:
        ids = wt_name_to_ids.get(op_name, [])
        wt_ids.extend(ids)

    if not wt_ids:
        for op_name in operations:
            op_lower = op_name.lower()
            for wt_name, ids in wt_name_to_ids.items():
                if op_lower in wt_name.lower() or wt_name.lower() in op_lower:
                    wt_ids.extend(ids)

    logger.info(f"Resolved wt_ids={wt_ids} for operations={operations}")

    # FIX 3: agro_operations и machine_tasks — в одном gather.
    # machine_tasks фильтруются по дате на стороне API через end_time_gt_eq/lt_eq —
    # это кардинально сокращает количество тасков и, следовательно, mtfmi-запросов.
    # Тот же принцип теперь применён и к agro_operations (completed_datetime) —
    # раньше они грузились за весь сезон, а диапазон дат вырезался только
    # в Python-фильтре ниже, что при запросе за весь сезон означало
    # перекачку практически всех операций.
    #
    # Оба фильтра включаются ТОЛЬКО если весь диапазон в прошлом
    # (range_fully_past) — иначе рискуем потерять незакрытые задания/операции
    # без end_time/completed_datetime, которые как раз "в процессе" сегодня.

    # +1 день к date_to чтобы включить сам день (lt_eq на границе суток)
    mt_date_to = (date_to + timedelta(days=1)).isoformat()

    all_ops: list[dict] = []
    all_mt_pages: list[list[dict]] = []

    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS, timeout=_CROPWISE_TIMEOUT) as session:
        # FIX 3: agro_operations (batch work_type_id) и machine_tasks — в одном gather
        ops_tasks = []
        for api_status in statuses:
            for season_yr in season_years:
                params: dict = {
                    "status": api_status,
                    "season": season_yr,
                }
                if range_fully_past and api_status in ("done", "canceled"):
                    params["completed_datetime_gt_eq"] = date_from.isoformat()
                    params["completed_datetime_lt_eq"] = mt_date_to
                ops_tasks.append(
                    _fetch_agro_operations_by_wt(
                        session, set(wt_ids), params, sem=_OPS_SEM,
                        log_prefix="field_area_detail ops ",
                    )
                )

        mt_tasks = []
        for season_yr in season_years:
            params: dict = {"season": season_yr}
            if range_fully_past:
                params["end_time_gt_eq"] = date_from.isoformat()
                params["end_time_lt_eq"] = mt_date_to
            else:
                params["start_time_lt_eq"] = mt_date_to
            mt_tasks.append(
                _fetch_machine_tasks_by_wt(
                    session, set(wt_ids), params, sem=_OPS_SEM,
                    log_prefix="field_area_detail mt ",
                )
            )

        n_ops = len(ops_tasks)
        combined = await asyncio.gather(*(ops_tasks + mt_tasks))

    # Разбираем agro_operations
    seen_op_ids: set[int] = set()
    for r in combined[:n_ops]:
        for op in r:
            oid = op.get("id")
            if oid is not None and oid not in seen_op_ids:
                seen_op_ids.add(oid)
                all_ops.append(op)

    # Разбираем machine_tasks (каждый элемент — list[dict] из batch-fetch)
    seen_tids: set[int] = set()
    relevant_task_ids: list[int] = []
    for mt_chunk in combined[n_ops:]:
        for t in mt_chunk:
            tid = t.get("id")
            if tid is None or int(tid) in seen_tids:
                continue
            # Дополнительный фильтр по work_timetable если передан
            if timetable_intervals is not None:
                if not _task_in_timetable(t, timetable_intervals):
                    continue
            seen_tids.add(int(tid))
            relevant_task_ids.append(int(tid))

    logger.info(
        f"Total agro_operations fetched (deduped): {len(all_ops)}, "
        f"relevant machine_tasks: {len(relevant_task_ids)}"
    )

    # ── Фильтрация agro_operations по датам ──────────────────────────────────

    filtered: list[dict] = []
    seen_filtered_ids: set[int] = set()
    for op in all_ops:
        oid = op.get("id")
        if oid is not None:
            if oid in seen_filtered_ids:
                continue
            seen_filtered_ids.add(oid)
        raw_date = (op.get("completed_date") or op.get("completed_datetime")
                    or op.get("started_date"))
        if raw_date:
            try:
                op_date = date.fromisoformat(str(raw_date)[:10])
            except ValueError:
                op_date = None
            if op_date is not None and not (date_from <= op_date <= date_to):
                continue
        filtered.append(op)

    logger.info(f"After date filter: {len(filtered)} ops")

    # ── Загружаем mtaomi + mtfmi для manually_defined_covered_area ───────────
    #
    # mtaomi — batch по agro_operation_id (официальный фильтр API).
    # relevant_task_ids уже отфильтрован по дате через API — mtfmi только для них.

    filtered_op_ids: set[int] = {
        int(op["id"]) for op in filtered if op.get("id") is not None
    }

    manual_index: dict[int, list[int]] = {}

    if filtered_op_ids and relevant_task_ids:
        async with aiohttp.ClientSession(headers=CROPWISE_HEADERS, timeout=_CROPWISE_TIMEOUT) as session:
            op_to_task_ids = await _fetch_mtaomi_by_agro_op_ids(
                session, list(filtered_op_ids), sem=_OPS_SEM,
                log_prefix="field_area_detail ",
            )
            mtaomi_all = [
                {"agro_operation_id": aoid, "machine_task_id": tid}
                for aoid, tids in op_to_task_ids.items()
                for tid in tids
            ]
            logger.info(f"mtaomi records for filtered ops: {len(mtaomi_all)}")

            relevant_set = set(relevant_task_ids)
            mtaomi_task_ids: list[int] = list({
                tid
                for aoid, tids in op_to_task_ids.items()
                for tid in tids
                if aoid in filtered_op_ids and tid in relevant_set
            })
            logger.info(f"mtaomi relevant machine_task_ids: {len(mtaomi_task_ids)}")

            mtfmi_map: dict[int, list[dict]] = {}
            if mtaomi_task_ids:
                mtfmi_map = await _fetch_mtfmi_by_task_ids(
                    session, mtaomi_task_ids, sem=_MTFMI_SEM,
                )

        op_field_map: dict[int, int] = {
            int(op["id"]): int(op["field_id"])
            for op in filtered
            if op.get("id") is not None and op.get("field_id") is not None
        }
        manual_index = _build_manual_index(mtaomi_all, mtfmi_map, op_field_map)
        logger.info(f"ops with manually defined area: {len(manual_index)}")

    # ── Сборка детальных строк: одна строка = одна agro_operation ────────────

    rows: list[dict] = []
    for op in filtered:
        fid = op.get("field_id")
        if not fid:
            continue
        fid = int(fid)
        if fid not in field_to_crop:
            continue
        if not field_to_firm.get(fid):
            continue

        api_status = (op.get("status") or "").lower()
        raw_date   = (op.get("completed_date") or op.get("completed_datetime")
                      or op.get("started_date"))
        op_date_str = str(raw_date)[:10] if raw_date else None

        op_id = op.get("id")
        wt_id = op.get("work_type_id")
        wt_name = wt_id_to_name.get(wt_id, f"wt#{wt_id}") if wt_id else "—"

        manual_task_ids = manual_index.get(int(op_id), []) if op_id is not None else []
        if manual_task_ids:
            manually_defined = "Вручную (" + ", ".join(str(t) for t in manual_task_ids) + ")"
        else:
            manually_defined = ""

        rows.append({
            "firm":                 field_to_firm.get(fid, "—"),
            "field_group":          field_to_group.get(fid, "Без группы"),
            "field_name":           field_name_map.get(fid, f"Поле #{fid}"),
            "field_id":             fid,
            "crop":                 field_to_crop.get(fid, "—"),
            "field_area":           round(field_area_map.get(fid, 0.0), 2),
            "op_id":                op_id,
            "work_type_name":       wt_name,
            "op_date":              op_date_str,
            "covered_area_detail":  round(float(op.get("covered_area") or 0.0), 2),
            "status_key":           api_status,
            "status_label":         STATUS_LABELS.get(api_status, api_status),
            "partially_completed":  op.get("partially_completed"),
            "manually_defined":     manually_defined,
        })

    logger.info(f"_fetch_field_area_detail: {len(rows)} result rows")
    return rows


# ---------------------------------------------------------------------------
# Excel builder
# ---------------------------------------------------------------------------

def _build_excel_detail(
    rows: list[dict],
    crop: str | None,
    operations: list[str],
    date_from: date,
    date_to: date,
    statuses: list[str],
) -> bytes:
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except ImportError as e:
        raise RuntimeError(f"openpyxl not installed: {e}")

    _C_HDR        = "8093B3"
    _C_TITLE      = "4472C4"
    _C_TOTAL      = "BDD7EE"
    _C_FIRM       = "D9E1F2"
    _C_GROUP      = "EBF0F8"
    _C_CROP       = "E2EFDA"
    _C_CROP_TOTAL = "C6E0B4"
    _C_GRP_TOTAL  = "D9E1F2"

    def _side():
        t = Side(style="thin", color="BBBBBB")
        return Border(left=t, right=t, top=t, bottom=t)

    def _cell(ws, row, col, value=None, bold=False, bg=None, color="000000",
              halign="left", wrap=False, fmt=None, size=9):
        c = ws.cell(row=row, column=col, value=value)
        c.font = Font(name="Calibri", size=size, bold=bold, color=color)
        c.alignment = Alignment(horizontal=halign, vertical="center", wrap_text=wrap)
        c.border = _side()
        if bg:
            c.fill = PatternFill("solid", fgColor=bg)
        if fmt:
            c.number_format = fmt
        return c

    # 12 колонок
    HEADERS = [
        "№",
        "Агрофирма / Группа полей",
        "Наименование поля",
        "Культура",
        "Площадь поля, га",
        "op_id",
        "Вид работы",
        "Дата операции",
        "Обработанная площадь поля, детальная, га",
        "Статус",
        "Частично выполнено",
        "Площадь задана вручную",
    ]
    COL_W = [5, 28, 30, 22, 16, 10, 36, 14, 24, 22, 18, 24]

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Детальная обработанная площадь"

    for ci, w in enumerate(COL_W, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = w

    r = 1
    ops_str  = ", ".join(operations) if operations else "все"
    crop_str = crop or "все культуры"
    title_text = (
        f"Обработанная площадь полей (детальная)  |  Культура: {crop_str}  |  "
        f"Операция: {ops_str}  |  "
        f"{date_from.strftime('%d.%m.%Y')} – {date_to.strftime('%d.%m.%Y')}"
    )
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(HEADERS))
    c = ws.cell(row=r, column=1, value=title_text)
    c.font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.fill = PatternFill("solid", fgColor=_C_TITLE)
    c.border = _side()
    ws.row_dimensions[r].height = 26
    r += 1

    for ci, h in enumerate(HEADERS, start=1):
        _cell(ws, r, ci, h, bold=True, bg=_C_HDR, color="FFFFFF",
              halign="center", wrap=True, size=9)
    ws.row_dimensions[r].height = 36
    ws.freeze_panes = "B3"
    r += 1

    if not rows:
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(HEADERS))
        c = ws.cell(row=r, column=1, value="Данных не найдено")
        c.font = Font(name="Calibri", size=10, bold=True, color="375623")
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.fill = PatternFill("solid", fgColor="E2EFDA")
        c.border = _side()
        ws.row_dimensions[r].height = 18
    else:
        def sort_key(row):
            fi = FIRM_ORDER.index(row["firm"]) if row["firm"] in FIRM_ORDER else 999
            op_date_sort = row["op_date"] or "9999-99-99"
            return (
                fi,
                row["field_group"],
                row["crop"],
                row["field_name"],
                STATUS_PRIORITY.get(row["status_key"], 0) * -1,
                op_date_sort,
            )

        rows_sorted = sorted(rows, key=sort_key)

        from collections import OrderedDict
        by_firm: dict = OrderedDict()
        for row in rows_sorted:
            f      = row["firm"]
            g      = row["field_group"]
            c_name = row["crop"]
            fn     = row["field_name"]
            by_firm.setdefault(f, OrderedDict()) \
                   .setdefault(g, OrderedDict()) \
                   .setdefault(c_name, OrderedDict()) \
                   .setdefault(fn, []) \
                   .append(row)

        grand_cov = 0.0

        for firm, groups in by_firm.items():
            _cell(ws, r, 1, "", bg=_C_FIRM)
            ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=len(HEADERS))
            _cell(ws, r, 2, firm, bold=True, bg=_C_FIRM, size=9)
            ws.row_dimensions[r].height = 13
            r += 1

            firm_cov = 0.0

            for grp_name, crops_dict in groups.items():
                _cell(ws, r, 1, "", bg=_C_GROUP)
                ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=len(HEADERS))
                _cell(ws, r, 2, f"  {grp_name}", bold=True, bg=_C_GROUP, size=9)
                ws.row_dimensions[r].height = 13
                r += 1

                grp_cov = 0.0

                for crop_name, fields_dict in crops_dict.items():
                    _cell(ws, r, 1, "", bg=_C_CROP)
                    ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=len(HEADERS))
                    _cell(ws, r, 2, f"    {crop_name}", bold=True, bg=_C_CROP, size=9)
                    ws.row_dimensions[r].height = 13
                    r += 1

                    crop_cov = 0.0
                    num = 1

                    for field_name, op_rows in fields_dict.items():
                        for frow in op_rows:
                            row_bg = STATUS_COLORS.get(frow["status_key"], "FFFFFF")
                            _cell(ws, r, 1, num,                             bg=row_bg, halign="center")
                            _cell(ws, r, 2, grp_name,                        bg=row_bg)
                            _cell(ws, r, 3, frow["field_name"],               bg=row_bg)
                            _cell(ws, r, 4, frow["crop"],                     bg=row_bg)
                            _cell(ws, r, 5, frow["field_area"],               bg=row_bg, halign="right", fmt="#,##0.00")
                            _cell(ws, r, 6, frow["op_id"],                    bg=row_bg, halign="center")
                            _cell(ws, r, 7, frow["work_type_name"],           bg=row_bg)
                            _cell(ws, r, 8, frow["op_date"] or "—",          bg=row_bg, halign="center")
                            _cell(ws, r, 9, frow["covered_area_detail"],      bg=row_bg, halign="right", fmt="#,##0.00")
                            _cell(ws, r, 10, frow["status_label"],            bg=row_bg, halign="center")
                            partially = frow.get("partially_completed")
                            if partially in (0, "0", False):
                                partially_text = "выполнено"
                            elif partially in (1, "1", True):
                                partially_text = "частично завершенная операция"
                            else:
                                partially_text = "—"
                            _cell(ws, r, 11, partially_text, bg=row_bg, halign="center")
                            _cell(ws, r, 12, frow.get("manually_defined", ""), bg=row_bg, halign="center")
                            ws.row_dimensions[r].height = 12
                            crop_cov += frow["covered_area_detail"]
                            r += 1
                            num += 1

                    # Итог по культуре
                    _cell(ws, r, 1, "",                          bg=_C_CROP_TOTAL)
                    _cell(ws, r, 2, f"    Итого {crop_name}",    bold=True, bg=_C_CROP_TOTAL, size=9)
                    for ci in range(3, 13):
                        _cell(ws, r, ci, "", bg=_C_CROP_TOTAL)
                    _cell(ws, r, 9, round(crop_cov, 2),          bold=True, bg=_C_CROP_TOTAL, halign="right", fmt="#,##0.00")
                    ws.row_dimensions[r].height = 13
                    grp_cov += crop_cov
                    r += 1

                # Итог по группе полей
                _cell(ws, r, 1, "",                            bg=_C_GRP_TOTAL)
                _cell(ws, r, 2, f"  Итого {grp_name}",        bold=True, bg=_C_GRP_TOTAL, size=9)
                for ci in range(3, 13):
                    _cell(ws, r, ci, "", bg=_C_GRP_TOTAL)
                _cell(ws, r, 9, round(grp_cov, 2),             bold=True, bg=_C_GRP_TOTAL, halign="right", fmt="#,##0.00")
                ws.row_dimensions[r].height = 13
                firm_cov += grp_cov
                r += 1

            # Итог по агрофирме
            _cell(ws, r, 1, "",                    bg=_C_TOTAL)
            _cell(ws, r, 2, f"Итого {firm}",       bold=True, bg=_C_TOTAL, size=9)
            for ci in range(3, 13):
                _cell(ws, r, ci, "", bg=_C_TOTAL)
            _cell(ws, r, 9, round(firm_cov, 2),    bold=True, bg=_C_TOTAL, halign="right", fmt="#,##0.00")
            ws.row_dimensions[r].height = 14
            grand_cov += firm_cov
            r += 1

        # Гранд-итог
        _cell(ws, r, 1, "",                   bg=_C_HDR)
        _cell(ws, r, 2, "ИТОГО",              bold=True, bg=_C_HDR, color="FFFFFF", size=10)
        for ci in range(3, 13):
            _cell(ws, r, ci, "", bg=_C_HDR)
        _cell(ws, r, 9, round(grand_cov, 2),  bold=True, bg=_C_HDR, color="FFFFFF", halign="right", fmt="#,##0.00", size=10)
        ws.row_dimensions[r].height = 16

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# Эндпоинт
# ---------------------------------------------------------------------------

@router.post(
    "",
    summary="Обработанная площадь полей (детальная) — Excel",
)
async def post_field_area_query_detail(
    body: FieldAreaDetailRequest,
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

    valid_statuses = set(ALL_API_STATUSES)
    bad = set(body.statuses) - valid_statuses
    if bad:
        raise HTTPException(
            status_code=422,
            detail=f"Неизвестные статусы: {bad}. Допустимые: {sorted(valid_statuses)}",
        )

    selected_crops = [c for c in (body.crops or []) if c] or ([body.crop] if body.crop else [])
    crop_label = ", ".join(selected_crops) if selected_crops else None

    try:
        rows = await _fetch_field_area_detail(
            crop=body.crop or None,
            crops=selected_crops or None,
            operations=body.operations,
            operation_ids=body.operation_ids,
            date_from=date_from,
            date_to=date_to,
            statuses=body.statuses,
            work_timetable=body.work_timetable,
        )
    except Exception as e:
        logger.error(f"field_area_query_detail error: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Ошибка получения данных из Cropwise: {e}")

    try:
        xlsx_bytes = _build_excel_detail(
            rows, crop_label, body.operations, date_from, date_to, body.statuses
        )
    except Exception as e:
        logger.error(f"excel build error (detail): {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ошибка формирования Excel: {e}")

    from urllib.parse import quote
    if len(selected_crops) > 1:
        crop_part = f"{selected_crops[0][:12]}_i_esche_{len(selected_crops) - 1}".replace(" ", "_")
    else:
        crop_part = (selected_crops[0] if selected_crops else "vse")[:15].replace(" ", "_")
    fname = f"field_area_detail_{crop_part}_{body.date_from}_{body.date_to}.xlsx"
    return StreamingResponse(
        io.BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )
