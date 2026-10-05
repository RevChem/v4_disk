"""
cropwise/field_area_router.py
==============================
Эндпоинт: отчёт по обработанной площади полей.
POST /api/field-area-query  — возвращает Excel-файл.

Тело запроса (JSON):
  {
    "crop":            "Соя",              # одна культура (null = все)
    "operations":      ["4.2 Посев ..."],  # список видов работ (подтипов)
    "date_from":       "2025-08-22",
    "date_to":         "2025-08-23",
    "statuses":        ["planned", "in_progress", "done", "canceled"],
    "work_timetable":  [                   # опционально: точные временные интервалы
      ["2025-08-22 15:28:57", "2025-08-22 16:33:38"],
      ["2025-08-22 16:34:22", "2025-08-22 17:02:18"]
    ]
      # Если work_timetable передан — machine_tasks фильтруются по точному
      # вхождению в один из интервалов (started_at ИЛИ completed_at).
      # Если не передан — фильтрация только по date_from / date_to.
  }

Логика адаптирована из square_4.py:
  — Загружаем agro_operations по каждому wt_id + status из API
  — Фильтруем agro_operations по диапазону дат (date_from / date_to)
  — Фильтруем machine_tasks по дате, и опционально по work_timetable-интервалам
  — Из площадных полей используем только covered_area
  — Результат — Excel: Агрофирма / Группа полей / Культура / Поле / Площадь / Обработано / Статус
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
    _fetch_mtfmi_by_task_ids,
    _CROPWISE_TIMEOUT,
)

# Семафор: не более 15 параллельных MTFMI-запросов
_MTFMI_SEM = asyncio.Semaphore(15)
# Семафор для agro_operations и machine_tasks (их меньше, можно чуть шире)
_OPS_SEM = asyncio.Semaphore(20)
from app.logging_config import get_logger
from app.users.models import User

logger = get_logger("cropwise.field_area_router")

router = APIRouter(prefix="/api/field-area-query", tags=["Обработанная площадь полей"])

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

# Статусы, которые принимает Cropwise API
ALL_API_STATUSES = ["planned", "in_progress", "done", "canceled"]

STATUS_LABELS = {
    "planned":     "Запланировано",
    "in_progress": "В процессе",
    "done":        "Выполнено",
    "canceled":    "Отменено",
}

STATUS_COLORS = {
    "planned":     "DDEBF7",   # голубой
    "in_progress": "FCE4D6",   # оранжевый
    "done":        "E2EFDA",   # зелёный
    "canceled":    "F2F2F2",   # серый
}


# ---------------------------------------------------------------------------
# Схема запроса
# ---------------------------------------------------------------------------

class FieldAreaRequest(BaseModel):
    crop: Optional[str] = None                          # одна культура (null = все) — для обратной совместимости
    crops: List[str] = []                                # список культур (если выбрано несколько в UI)
    operations: List[str]                               # список видов работ (по имени)
    operation_ids: List[int] = []                       # work_type IDs напрямую (минуя резолвинг)
    date_from: str                                      # "YYYY-MM-DD"
    date_to: str                                        # "YYYY-MM-DD"
    statuses: List[str]                                 # planned | in_progress | done | canceled
    work_timetable: Optional[List[List[str]]] = None    # [["YYYY-MM-DD HH:MM:SS", "YYYY-MM-DD HH:MM:SS"], ...]


# ---------------------------------------------------------------------------
# Вспомогательные функции (идентично square_4.py)
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
    """
    Парсит work_timetable из строк в список пар (datetime, datetime).
    Возвращает None если raw пустой или не передан.
    """
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
    """Возвращает True если dt попадает хотя бы в один из интервалов."""
    if dt is None:
        return False
    # Убираем tzinfo для единообразного сравнения (Cropwise отдаёт naive-строки)
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
    """
    Проверяет machine_task на попадание в timetable-интервалы.
    Считаем задачу подходящей если:
      - started_at  попадает в интервал, ИЛИ
      - completed_at попадает в интервал, ИЛИ
      - задача целиком перекрывает хотя бы один интервал
        (started_at <= interval_start AND completed_at >= interval_end)
    """
    def _parse(s: str | None) -> datetime | None:
        if not s:
            return None
        try:
            return datetime.fromisoformat(str(s)[:19])
        except ValueError:
            return None

    t_start = _parse(task.get("started_at"))
    t_end   = _parse(task.get("completed_at"))

    # Если нет дат — пропускаем (не выбрасываем, берём по умолчанию)
    if t_start is None and t_end is None:
        return True

    if _datetime_in_timetable(t_start, intervals):
        return True
    if _datetime_in_timetable(t_end, intervals):
        return True

    # Проверка полного перекрытия интервала задачей
    if t_start is not None and t_end is not None:
        ts = t_start.replace(tzinfo=None) if t_start.tzinfo else t_start
        te = t_end.replace(tzinfo=None)   if t_end.tzinfo   else t_end
        for iv_s, iv_e in intervals:
            s = iv_s.replace(tzinfo=None) if iv_s.tzinfo else iv_s
            e = iv_e.replace(tzinfo=None) if iv_e.tzinfo else iv_e
            if ts <= s and te >= e:
                return True

    return False


def _dt_bounds(date_from: date, date_to: date) -> tuple[str, str]:
    """
    Границы диапазона в виде строк "YYYY-MM-DD HH:MM:SS" для передачи в
    comparison-фильтры Cropwise API (*_gt_eq / *_lt_eq): начало суток
    date_from и конец суток date_to.
    """
    dt_from = datetime.combine(date_from, datetime.min.time())
    dt_to   = datetime.combine(date_to, datetime.max.time()).replace(microsecond=0)
    return dt_from.strftime("%Y-%m-%d %H:%M:%S"), dt_to.strftime("%Y-%m-%d %H:%M:%S")


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


# ---------------------------------------------------------------------------
# Основная логика сбора данных (адаптировано из square_4.py)
# ---------------------------------------------------------------------------

async def _fetch_field_area(
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
    Возвращает список строк:
      firm, field_group, field_name, field_id, crop,
      field_area, covered_area_by_track, machine_work_area,
      covered_area, mtfmi_covered_area_by_track, status_label, status_key

    work_timetable — опциональный список временных интервалов для точной
    фильтрации machine_tasks. Если передан, задача включается только если
    её started_at или completed_at попадает в один из интервалов.
    """
    season_years: list[int] = cropwise_season_years_for_range(date_from, date_to)
    # Парсим timetable один раз — используем в фильтрах ниже
    timetable_intervals = _parse_timetable(work_timetable)

    # Границы диапазона для comparison-фильтров API (*_gt_eq / *_lt_eq).
    # Раньше agro_operations/machine_tasks грузились ПОЛНОСТЬЮ за весь сезон
    # (только season=year), а фильтрация по date_from/date_to происходила
    # уже в Python после скачивания — при запросе за весь сезон или большой
    # диапазон это означало перекачку почти всего объёма данных сезона на
    # каждый запрос. Теперь диапазон передаётся прямо в запрос к API, что
    # резко уменьшает объём пагинации.
    #
    # Ограничение: если date_to попадает на сегодня/будущее, среди
    # machine_tasks/agro_operations могут быть ещё не закрытые записи
    # (end_time / completed_datetime не проставлены — работа в процессе).
    # Comparison-фильтр по этим полям в таком случае отфильтрует их вместе
    # с реально старыми записями, занизив площадь "в процессе". Поэтому
    # точный фильтр по end_time/completed_datetime применяем только если
    # весь диапазон целиком в прошлом; иначе используем более широкий
    # фильтр только по верхней границе (start_time/planned_start_date),
    # который всё равно сильно сокращает объём по сравнению с "весь сезон".
    range_fully_past = date_to < date.today()
    dt_from_str, dt_to_str = _dt_bounds(date_from, date_to)

    logger.info(
        f"_fetch_field_area: crop={crop!r}, crops={crops!r}, ops={operations}, "
        f"date_from={date_from}, date_to={date_to}, season_years={season_years}, statuses={statuses}"
    )

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

    # field → текущая культура
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

    # Несколько культур, выбранных в UI: раньше при выборе более одной
    # культуры фронтенд передавал crop=None ("все культуры"), и бэкенд
    # молча выгружал ВЕСЬ сезон по всем культурам — на порядок больше
    # данных из Cropwise, чем реально требовалось, что приводило к
    # долгому/тяжёлому запросу и обрыву соединения (сетевая ошибка /
    # 503 на фронте). Теперь фильтруем по полному списку выбранных
    # культур, если он передан.
    if crops:
        crops_set = set(crops)
        field_to_crop = {fid: c for fid, c in field_to_crop.items() if c in crops_set}

    # Исключаем служебные культуры
    field_to_crop = {fid: c for fid, c in field_to_crop.items() if c not in SKIP_CROPS}

    # Площади и названия полей
    field_area: dict[int, float] = {}
    field_name_map: dict[int, str] = {}
    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        field_area[int(fid)] = float(f.get("tillable_area") or f.get("calculated_area") or 0.0)
        field_name_map[int(fid)] = (f.get("name") or f.get("title") or f"Поле #{fid}").strip()

    # Группы полей
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

    # work_type name → ids
    wt_name_to_ids: dict[str, list[int]] = defaultdict(list)
    for wt in wt_raw:
        if wt.get("id") is not None:
            wt_name_to_ids[wt.get("name") or wt.get("title", "")].append(wt["id"])

    # ── Загружаем agro_operations по каждому wt_id × status ─────────────────
    #
    # Статусы передаются напрямую в API (planned / in_progress / done / canceled)
    # — в точности как в square_4.py

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

    all_ops: list[dict] = []
    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS, timeout=_CROPWISE_TIMEOUT) as session:
        tasks_to_run = []
        for api_status in statuses:
            for season_yr in season_years:
                params: dict = {
                    "status": api_status,
                    "season": season_yr,
                }
                if range_fully_past and api_status in ("done", "canceled"):
                    params["completed_datetime_gt_eq"] = dt_from_str
                    params["completed_datetime_lt_eq"] = dt_to_str
                tasks_to_run.append(
                    _fetch_agro_operations_by_wt(
                        session, set(wt_ids), params, sem=_OPS_SEM,
                        log_prefix="field_area ops ",
                    )
                )
        if tasks_to_run:
            results = await asyncio.gather(*tasks_to_run)
            seen_op_ids: set[int] = set()
            for r in results:
                for op in r:
                    oid = op.get("id")
                    if oid is not None and oid not in seen_op_ids:
                        seen_op_ids.add(oid)
                        all_ops.append(op)

    logger.info(f"Total agro_operations fetched (deduped): {len(all_ops)}")

    # ── Загружаем mtfmi и строим индекс field_id → covered_area_by_track ────
    #
    # Быстрый путь: идём через machine_tasks, а не через поля.
    #   1. machine_tasks?work_type_id=X&season=Y  — N запросов (N = кол-во wt_ids)
    #   2. mtfmi?machine_task_id=Z               — M запросов (M = кол-во тасков)
    # Итого десятки запросов вместо тысяч.

    # (field_id, agro_operation_id) → сумма covered_area_by_track
    # Ключ по паре (поле + операция), чтобы у каждой строки было своё значение,
    # а не одно суммарное на всё поле.
    mtfmi_track: dict[tuple[int, int | None], float] = {}

    # ── Оптимизация: covered_area_by_track уже есть на самой AgroOperation ──
    #
    # По документации Cropwise API (agro_operations): поле `covered_area`
    # readonly и является алиасом `covered_area_by_track` ("actual covered
    # area by machines, excluding intersections") — то есть именно то
    # значение, которое раньше вычислялось отдельным дорогим проходом через
    # machine_tasks (N запросов) → machine_task_field_mapping_items (по ОДНОМУ
    # запросу на КАЖДУЮ задачу — при широком диапазоне дат/операций это
    # десятки тысяч запросов, что и приводило к таймауту/обрыву соединения
    # на фронте при тяжёлых отчётах).
    #
    # Единственный случай, когда точность agro_operations недостаточна —
    # фильтр `work_timetable` (интервалы времени суток): он работает на
    # уровне отдельных machine_tasks (started_at/completed_at конкретной
    # задачи), а не агрегата операции. Поэтому дорогой точный путь через
    # MTFMI сохраняем, но запускаем ТОЛЬКО когда work_timetable реально
    # передан — это редкий, явно включаемый пользователем расширенный
    # фильтр. Во всех остальных (подавляющее большинство) случаев считаем
    # covered_area_by_track напрямую из уже загруженных agro_operations —
    # без единого дополнительного запроса к Cropwise.
    if wt_ids and timetable_intervals:
        async with aiohttp.ClientSession(headers=CROPWISE_HEADERS, timeout=_CROPWISE_TIMEOUT) as session:
            # Шаг 1: таски нужных типов — N запросов (N = кол-во wt_ids).
            #
            # Раньше здесь брался ВЕСЬ сезон (season=year) и диапазон дат
            # вырезался уже в Python — это и было основной причиной, почему
            # запрос за весь сезон/большой диапазон был на порядки медленнее
            # суточного: перекачивался практически весь объём задач сезона.
            # Теперь диапазон передаётся в сам API-запрос (comparison-фильтр
            # по end_time), что резко сокращает объём пагинации.
            #
            # Незакрытые задания (машина ещё в поле, end_time не проставлен)
            # при фильтре по end_time_gt_eq/lt_eq не попадут в выборку — это
            # ок, если date_to целиком в прошлом (такие задания там не
            # относятся к делу). Если date_to захватывает сегодня/будущее,
            # используем более мягкий фильтр только по start_time (верхняя
            # граница), чтобы не потерять "в процессе" задания без end_time.
            all_mt_tasks: list[dict] = []
            for season_yr in season_years:
                params: dict = {"season": season_yr}
                if range_fully_past:
                    params["end_time_gt_eq"] = dt_from_str
                    params["end_time_lt_eq"] = dt_to_str
                else:
                    params["start_time_lt_eq"] = dt_to_str
                all_mt_tasks.extend(await _fetch_machine_tasks_by_wt(
                    session, set(wt_ids), params, sem=_OPS_SEM,
                    log_prefix="field_area mt ",
                ))

        seen_tids: set[int] = set()
        relevant_task_ids: list[int] = []
        # machine_task_id → agro_operation_id (для привязки mtfmi к операции)
        task_to_op: dict[int, int | None] = {}
        for t in all_mt_tasks:
            tid = t.get("id")
            if tid is None or int(tid) in seen_tids:
                continue

            # ── Уровень 1: фильтр по дате (date_from / date_to) ──────────
            raw_dt = t.get("completed_at") or t.get("started_at")
            if raw_dt:
                try:
                    t_date = date.fromisoformat(str(raw_dt)[:10])
                    if not (date_from <= t_date <= date_to):
                        continue   # таск вне диапазона дат — пропускаем
                except ValueError:
                    pass           # нет даты — берём таск

            # ── Уровень 2: фильтр по work_timetable-интервалам ───────────
            if timetable_intervals is not None:
                if not _task_in_timetable(t, timetable_intervals):
                    continue       # таск не попадает ни в один интервал

            seen_tids.add(int(tid))
            relevant_task_ids.append(int(tid))
            aoid = t.get("agro_operation_id")
            task_to_op[int(tid)] = int(aoid) if aoid is not None else None

        logger.info(
            f"mtfmi: {len(relevant_task_ids)} machine_tasks "
            f"for wt_ids={list(set(wt_ids))}, season_years={season_years}, "
            f"date_range={date_from}/{date_to}, "
            f"timetable_intervals={len(timetable_intervals) if timetable_intervals else 0}"
        )

        if relevant_task_ids:
            async with aiohttp.ClientSession(headers=CROPWISE_HEADERS, timeout=_CROPWISE_TIMEOUT) as session:
                mtfmi_map = await _fetch_mtfmi_by_task_ids(
                    session, relevant_task_ids, sem=_MTFMI_SEM,
                )

            for tid in relevant_task_ids:
                items_list = mtfmi_map.get(tid, [])
                op_id = task_to_op.get(tid)
                for item in items_list:
                    fid = item.get("field_id")
                    val = item.get("covered_area_by_track")
                    if fid is None or val is None:
                        continue
                    # Пробуем получить agro_operation_id из самого item (если API его отдаёт),
                    # иначе берём из task_to_op
                    item_aoid = item.get("agro_operation_id")
                    key_op = int(item_aoid) if item_aoid is not None else op_id
                    key = (int(fid), key_op)
                    try:
                        mtfmi_track[key] = mtfmi_track.get(key, 0.0) + float(val)
                    except (TypeError, ValueError):
                        pass

    logger.info(f"mtfmi_track computed for {len(mtfmi_track)} (field, op) pairs")

    # Суммируем covered_area_by_track по полю — по всем операциям.
    # mtfmi_track уже отфильтрован по дате и work_timetable, поэтому
    # этот агрегат корректно отражает фактическую площадь за указанный период.
    mtfmi_by_field: dict[int, float] = {}
    for (fid, op_id), val in mtfmi_track.items():
        mtfmi_by_field[fid] = mtfmi_by_field.get(fid, 0.0) + val

    # ── Фильтрация по датам ──────────────────────────────────────────────────
    # Статус уже отфильтрован API-запросом; дополнительно фильтруем только даты.

    seen_filtered_ids: set[int] = set()
    filtered: list[dict] = []
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

    # Диагностика дублей: логируем операции сгруппированные по field_id
    from collections import defaultdict as _dd
    _by_field: dict = _dd(list)
    for op in filtered:
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

    # Быстрый путь (см. комментарий выше про covered_area_by_track): если
    # MTFMI не считался (нет work_timetable), берём covered_area_by_track
    # прямо из уже загруженных agro_operations, просуммировав по полю.
    covered_by_track_source = mtfmi_by_field
    if not timetable_intervals:
        ops_covered_area_by_field: dict[int, float] = {}
        for op in filtered:
            fid = op.get("field_id")
            if fid is None:
                continue
            val = op.get("covered_area_by_track")
            if val is None:
                val = op.get("covered_area")
            try:
                ops_covered_area_by_field[int(fid)] = (
                    ops_covered_area_by_field.get(int(fid), 0.0) + float(val or 0.0)
                )
            except (TypeError, ValueError):
                pass
        covered_by_track_source = ops_covered_area_by_field

    # ── Сборка строк: схлопываем все операции одного поля в одну строку ───────
    # Приоритет статусов при объединении: done > in_progress > planned > canceled
    STATUS_PRIORITY = {"done": 3, "in_progress": 2, "planned": 1, "canceled": 0}

    # Сначала агрегируем по field_id
    from collections import OrderedDict as _OD
    agg: dict[int, dict] = {}
    for op in filtered:
        fid = op.get("field_id")
        if not fid:
            continue
        fid = int(fid)
        if fid not in field_to_crop:
            continue
        if not field_to_firm.get(fid):
            continue

        api_status     = (op.get("status") or "").lower()
        mach_work_area = float(op.get("machine_work_area") or 0.0)
        covered        = float(op.get("covered_area")      or 0.0)

        if fid not in agg:
            agg[fid] = {
                "firm":                        field_to_firm.get(fid, "—"),
                "field_group":                 field_to_group.get(fid, "Без группы"),
                "field_name":                  field_name_map.get(fid, f"Поле #{fid}"),
                "field_id":                    fid,
                "crop":                        field_to_crop.get(fid, "—"),
                "field_area":                  round(field_area.get(fid, 0.0), 2),
                # Точный путь (work_timetable задан): из mtfmi_by_field.
                # Быстрый путь (обычный случай): из covered_area самих
                # agro_operations — без единого лишнего запроса к Cropwise.
                "covered_area_by_track":       round(covered_by_track_source.get(fid, 0.0), 2),
                "machine_work_area":           mach_work_area,
                "covered_area":                covered,
                "mtfmi_covered_area_by_track": round(covered_by_track_source.get(fid, 0.0), 2),
                "status_key":                  api_status,
                "status_label":                STATUS_LABELS.get(api_status, api_status),
            }
        else:
            # covered_area_by_track уже установлен из covered_by_track_source — не накапливаем
            agg[fid]["machine_work_area"] += mach_work_area
            agg[fid]["covered_area"]      += covered
            # Выбираем статус с наибольшим приоритетом
            cur_priority = STATUS_PRIORITY.get(agg[fid]["status_key"], 0)
            new_priority = STATUS_PRIORITY.get(api_status, 0)
            if new_priority > cur_priority:
                agg[fid]["status_key"]   = api_status
                agg[fid]["status_label"] = STATUS_LABELS.get(api_status, api_status)

    # Округляем суммированные поля
    rows: list[dict] = []
    for rec in agg.values():
        rec["covered_area_by_track"] = round(rec["covered_area_by_track"], 2)
        rec["machine_work_area"]     = round(rec["machine_work_area"],     2)
        rec["covered_area"]          = round(rec["covered_area"],          2)
        rows.append(rec)

    logger.info(f"_fetch_field_area: {len(rows)} result rows")
    return rows


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
) -> bytes:
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except ImportError as e:
        raise RuntimeError(f"openpyxl not installed: {e}")

    _C_HDR   = "8093B3"
    _C_TITLE = "4472C4"
    _C_TOTAL = "BDD7EE"
    _C_FIRM  = "D9E1F2"
    _C_GROUP = "EBF0F8"
    _C_CROP  = "E2EFDA"
    _C_ODD   = "FFFFFF"
    _C_EVEN  = "F5F8FF"

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

    # Заголовки и ширины колонок — 10 колонок
    HEADERS = [
        "№", "Агрофирма / Группа полей", "Наименование поля",
        "Культура", "Площадь поля, га",
        "Покрытая площадь (трек), га", "Площадь работы машины, га",
        "Обработанная площадь, га",
        "Обработанная площадь по треку (факт), га",
        "Статус",
    ]
    COL_W = [5, 28, 30, 22, 16, 22, 22, 20, 24, 22]

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Обработанная площадь"

    for ci, w in enumerate(COL_W, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = w

    r = 1
    ops_str  = ", ".join(operations) if operations else "все"
    crop_str = crop or "все культуры"
    title_text = (
        f"Обработанная площадь полей  |  Культура: {crop_str}  |  "
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
            return (fi, row["field_group"], row["crop"], row["field_name"])

        rows_sorted = sorted(rows, key=sort_key)

        from collections import OrderedDict
        by_firm: dict = OrderedDict()
        for row in rows_sorted:
            f      = row["firm"]
            g      = row["field_group"]
            c_name = row["crop"]
            if f not in by_firm:
                by_firm[f] = OrderedDict()
            if g not in by_firm[f]:
                by_firm[f][g] = OrderedDict()
            if c_name not in by_firm[f][g]:
                by_firm[f][g][c_name] = []
            by_firm[f][g][c_name].append(row)

        grand_area = 0.0
        grand_cov  = 0.0

        for firm, groups in by_firm.items():
            _cell(ws, r, 1, "", bg=_C_FIRM)
            ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=len(HEADERS))
            _cell(ws, r, 2, firm, bold=True, bg=_C_FIRM, size=9)
            ws.row_dimensions[r].height = 13
            r += 1

            firm_area = 0.0
            firm_cov  = 0.0

            for grp_name, crops_dict in groups.items():
                _cell(ws, r, 1, "", bg=_C_GROUP)
                ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=len(HEADERS))
                _cell(ws, r, 2, f"  {grp_name}", bold=True, bg=_C_GROUP, size=9)
                ws.row_dimensions[r].height = 13
                r += 1

                grp_area = 0.0
                grp_cov  = 0.0

                for crop_name, field_rows in crops_dict.items():
                    _cell(ws, r, 1, "", bg=_C_CROP)
                    ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=len(HEADERS))
                    _cell(ws, r, 2, f"    {crop_name}", bold=True, bg=_C_CROP, size=9)
                    ws.row_dimensions[r].height = 13
                    r += 1

                    crop_area = 0.0
                    crop_cov  = 0.0

                    num = 1
                    for frow in sorted(field_rows, key=lambda x: x["field_name"].lower()):
                        row_bg = STATUS_COLORS.get(frow["status_key"], _C_ODD)
                        _cell(ws, r, 1, num,                                      bg=row_bg, halign="center")
                        _cell(ws, r, 2, grp_name,                                 bg=row_bg)
                        _cell(ws, r, 3, frow["field_name"],                       bg=row_bg)
                        _cell(ws, r, 4, frow["crop"],                             bg=row_bg)
                        _cell(ws, r, 5, frow["field_area"],                       bg=row_bg, halign="right", fmt="#,##0.00")
                        _cell(ws, r, 6, frow["covered_area_by_track"],            bg=row_bg, halign="right", fmt="#,##0.00")
                        _cell(ws, r, 7, frow["machine_work_area"],                bg=row_bg, halign="right", fmt="#,##0.00")
                        _cell(ws, r, 8, frow["covered_area"],                     bg=row_bg, halign="right", fmt="#,##0.00")
                        _cell(ws, r, 9, frow["mtfmi_covered_area_by_track"],      bg=row_bg, halign="right", fmt="#,##0.00")
                        _cell(ws, r, 10, frow["status_label"],                    bg=row_bg, halign="center")
                        ws.row_dimensions[r].height = 12
                        crop_area += frow["field_area"]
                        crop_cov  += frow["covered_area"]
                        grp_area  += frow["field_area"]
                        grp_cov   += frow["covered_area"]
                        firm_area += frow["field_area"]
                        firm_cov  += frow["covered_area"]
                        r += 1
                        num += 1

                    # Итоги по культуре
                    _C_CROP_TOTAL = "C6E0B4"
                    _cell(ws, r, 1, "",                          bg=_C_CROP_TOTAL)
                    _cell(ws, r, 2, f"    Итого {crop_name}",    bold=True, bg=_C_CROP_TOTAL, size=9)
                    _cell(ws, r, 3, "",                          bg=_C_CROP_TOTAL)
                    _cell(ws, r, 4, "",                          bg=_C_CROP_TOTAL)
                    _cell(ws, r, 5, round(crop_area, 2),         bold=True, bg=_C_CROP_TOTAL, halign="right", fmt="#,##0.00")
                    _cell(ws, r, 6, "",                          bg=_C_CROP_TOTAL)
                    _cell(ws, r, 7, "",                          bg=_C_CROP_TOTAL)
                    _cell(ws, r, 8, round(crop_cov,  2),         bold=True, bg=_C_CROP_TOTAL, halign="right", fmt="#,##0.00")
                    _cell(ws, r, 9, "",                          bg=_C_CROP_TOTAL)
                    _cell(ws, r, 10, "",                         bg=_C_CROP_TOTAL)
                    ws.row_dimensions[r].height = 13
                    r += 1

                # Итоги по группе полей
                _C_GRP_TOTAL = "D9E1F2"
                _cell(ws, r, 1, "",                            bg=_C_GRP_TOTAL)
                _cell(ws, r, 2, f"  Итого {grp_name}",        bold=True, bg=_C_GRP_TOTAL, size=9)
                _cell(ws, r, 3, "",                            bg=_C_GRP_TOTAL)
                _cell(ws, r, 4, "",                            bg=_C_GRP_TOTAL)
                _cell(ws, r, 5, round(grp_area, 2),            bold=True, bg=_C_GRP_TOTAL, halign="right", fmt="#,##0.00")
                _cell(ws, r, 6, "",                            bg=_C_GRP_TOTAL)
                _cell(ws, r, 7, "",                            bg=_C_GRP_TOTAL)
                _cell(ws, r, 8, round(grp_cov,  2),            bold=True, bg=_C_GRP_TOTAL, halign="right", fmt="#,##0.00")
                _cell(ws, r, 9, "",                            bg=_C_GRP_TOTAL)
                _cell(ws, r, 10, "",                           bg=_C_GRP_TOTAL)
                ws.row_dimensions[r].height = 13
                r += 1

            _cell(ws, r, 1, "",                    bg=_C_TOTAL)
            _cell(ws, r, 2, f"Итого {firm}",       bold=True, bg=_C_TOTAL, size=9)
            _cell(ws, r, 3, "",                    bg=_C_TOTAL)
            _cell(ws, r, 4, "",                    bg=_C_TOTAL)
            _cell(ws, r, 5, round(firm_area, 2),   bold=True, bg=_C_TOTAL, halign="right", fmt="#,##0.00")
            _cell(ws, r, 6, "",                    bg=_C_TOTAL)
            _cell(ws, r, 7, "",                    bg=_C_TOTAL)
            _cell(ws, r, 8, round(firm_cov,  2),   bold=True, bg=_C_TOTAL, halign="right", fmt="#,##0.00")
            _cell(ws, r, 9, "",                    bg=_C_TOTAL)
            _cell(ws, r, 10, "",                   bg=_C_TOTAL)
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
        _cell(ws, r, 10, "",                  bg=_C_HDR)
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
    summary="Обработанная площадь полей — Excel",
)
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

    valid_statuses = set(ALL_API_STATUSES)
    bad = set(body.statuses) - valid_statuses
    if bad:
        raise HTTPException(status_code=422, detail=f"Неизвестные статусы: {bad}. Допустимые: {sorted(valid_statuses)}")

    # Список выбранных культур: приоритет — новое поле `crops` (может быть
    # несколько), иначе одиночное `crop` для обратной совместимости.
    selected_crops = [c for c in (body.crops or []) if c] or ([body.crop] if body.crop else [])
    crop_label = ", ".join(selected_crops) if selected_crops else None

    try:
        rows = await _fetch_field_area(
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
        logger.error(f"field_area_query error: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Ошибка получения данных из Cropwise: {e}")

    try:
        xlsx_bytes = _build_excel(
            rows, crop_label, body.operations, date_from, date_to, body.statuses
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
