"""
cropwise/speed_violations_sync.py
===================================
Логика вкладки «Превышение скорости» панели «Нарушения».

Логика перенесена из диагностического скрипта diagnose_speed_norms.py
(блок «Надёжные превышения») — раньше вкладка проверяла только уборку
против фиксированного порога 40 км/ч по avg_speed. Теперь:

  - Проверяется ВСЯ техника с заданиями (machine_tasks) за окно, а не
    только «Уборка» — вид работ (work_type) просто отображается в
    строке для контекста, но не фильтрует проверку.
  - Норма скорости берётся из ДВУХ независимых источников (если оба
    заполнены — берётся более строгий, т.е. меньший):
      1) справочник «Жатки» — оборудование "по паспорту" (implement_id ->
         норма = верхняя граница оптимальной скорости; JSON-файл
         speed_implements.json, редактируется в модалке «Жатки» на вкладке,
         см. cropwise/speed_implements_dao.py; раньше — Excel optim.xlsx);
      2) readonly-поле plan_speed_max прямо на MachineTask в Cropwise.
    Если НИ ОДИН источник не заполнен — по этой технике проверка не
    делается (не с чем сверять).
  - Сверяется MAX_speed за час (не avg_speed) — разовый пик внутри часа
    не должен теряться при усреднении.
  - Час засчитывается как "надёжный" (пригодный для сверки с нормой)
    ТОЛЬКО если подтверждён РЕАЛЬНОЙ обработкой площади поля —
    covered_area_hourly в machine_task_field_mapping_items за этот час
    (порог FIELD_WORK_AREA_THRESHOLD_HA), work_duration_hourly >= 30 мин,
    и при этом в этот час НЕТ движения по дороге (movements_on_road_duration_hourly
    > 0 или пересечение movements_on_road_timetable). Сверка max_speed —
    по gps_logger_data_items в интервалах machine_object_intersections
    (машина × поля задачи, object_type=Field). Строка таблицы — один час
    (hour_start, ключ sp_{machine}_{task}_{ts}); max_speed — пик logger-точек
    в MOI за этот час (часы без MOI/точек не попадают в таблицу).
    Задачи БЕЗ covered_area_hourly данных здесь просто ПРОПУСКАЮТСЯ (без
    отката на грубый интервал [start_time, end_time]) — вкладка должна
    показывать только то, чему можно доверять.
  - Час дополнительно отбрасывается, если сырые данные физически
    неправдоподобны: пробег (distance) за час не может быть больше
    max_speed * 1 час больше, чем на допуск (см. _is_raw_gps_anomaly) —
    иначе это, скорее всего, брак GPS-трека (битый фикс/«телепорт»
    точки), а не реальное превышение.

Кэш — Redis (см. speed_violations_router.py), обновляется Celery-задачей
speed_violations_refresh раз в час.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Optional

import aiohttp

from app.cropwise.client import (
    CROPWISE_HEADERS,
    FIO_NOT_FOUND,
    fetch_local_fio_map,
    _fetch_all_pages,
    _fetch_mass_request,
    _fetch_machine_object_intersections,
    _fetch_mtfmi_by_task_ids,
    moi_row_to_interval,
    _CROPWISE_TIMEOUT,
)
from app.logging_config import get_logger

logger = get_logger("cropwise.speed_violations_sync")

TZ_MSK = timezone(timedelta(hours=3))
CROPWISE_TASK_BASE = "https://operations.cropwise.com/machines"

DAY_START_HOUR = 6

# Окно проверки: "текущая половина суток" — 06:00–17:59:59 или
# 18:00–05:59:59. При пересечении границы (06:00 или 18:00) окно и отсчёт
# начинаются заново — данные из прошлой половины суток не переносятся.
HALF_DAY_BOUNDARY_HOURS = (6, 18)

GPS_SEMAPHORE_LIMIT = 15
MTFMI_SEMAPHORE_LIMIT = 20

# Пакетные запросы (см. client._fetch_mtfmi_by_task_ids, violations_sync.py):
# machine_task_id и machine_ids через запятую в одном GET.
GPS_CHUNK_SIZE = 10          # машин в одном gps_aggregated_data-запросе
LOGGER_CHUNK_SIZE = 10         # машин в одном gps_logger_data_items-запросе
GPS_REQUEST_TIMEOUT = 30.0
GPS_AIOHTTP_TIMEOUT = aiohttp.ClientTimeout(
    total=60.0, connect=10.0, sock_connect=10.0, sock_read=GPS_REQUEST_TIMEOUT,
)
CROPWISE_GPS_URL = "https://operations.cropwise.com/api/v3/gps_aggregated_data"

# Минимальная обработанная площадь за час (га, из covered_area_hourly в
# machine_task_field_mapping_items), чтобы считать этот час "часом
# полевой работы" по этой задаче.
FIELD_WORK_AREA_THRESHOLD_HA = 0.01

# Сколько секунд движения по дороге за час (movements_on_road_duration_hourly
# на самой задаче) допускаем. Диагностика по Excel 13.08.2026 показала: даже
# 10–14 сек дороги в часе дают max_speed 8–38 км/ч в агрегате GPS — допуск 60
# сек не защищает. Любая зафиксированная дорога в часе → час исключается.
ROAD_MOVEMENT_MAX_SECONDS_PER_HOUR = 0

# Минимум work_duration_hourly (сумма по MTFMI) за час, чтобы считать его
# полноценной полевой работой. Часы с 10–15 мин «работы» и высоким gps max
# — типичный переезд между полями (см. task 427199 07:00, work=602 с).
FIELD_WORK_MIN_SECONDS_PER_HOUR = 1800

GPS_LOGGER_SEMAPHORE_LIMIT = 10

# ── Проверка «Уборка: всё остальное оборудование» ───────────────────────────
# Предельная скорость, список видов работ («Уборка») и учёт заданий без
# оборудования настраиваются из интерфейса (кнопка «Настройки уборки» на вкладке)
# и хранятся в Redis — см. speed_violations_router.get_harvest_config().

# Минимум GPS-точек logger в MOI-интервалах за час — иначе не сверяем скорость.
MIN_GPS_POINTS_ON_FIELD = 3

# MOI: hour_start — час начала заезда; lookback как во «Возможном отсутствии задачи».
MOI_HOUR_LOOKBACK = timedelta(hours=12)
MOI_VISIT_MERGE_GAP_MIN = 30
MOI_SEMAPHORE_LIMIT = 15

# Физическая проверка правдоподобия GPS-записи за час: пробег (км) не
# может превышать max_speed (км/ч) * 1 час больше, чем на этот допуск.
RAW_DISTANCE_ANOMALY_RATIO = 1.2
RAW_DISTANCE_ANOMALY_BUFFER_KM = 0.5

# ── Агрофирмы — те же корневые папки, что и в остальных проверках ───────────
FIRM_ORDER: list[str] = [
    "АФ Чистопольская", "АгроРаздолье", "Аксу-Агро",
    "НурлатАгро", "Дрожжаное Рассвет Агро", "Актай",
    "Лениногорская Птицефабрика",
]
FIRM_TO_FOLDER: dict[str, int] = {
    "АФ Чистопольская": 3, "АгроРаздолье": 4, "Аксу-Агро": 5,
    "НурлатАгро": 11, "Дрожжаное Рассвет Агро": 6,
    "Актай": 7, "Лениногорская Птицефабрика": 9,
}
FOLDER_TO_FIRM: dict[int, str] = {v: k for k, v in FIRM_TO_FOLDER.items()}


# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =============================================================================

def _parse_dt(s) -> Optional[datetime]:
    """Парсит ISO8601-подобные метки времени (machine_gps_aggregated_data,
    machine_tasks) — с 'Z' или явным offset, либо unix timestamp."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        ts = float(s)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ_MSK)
        except Exception:
            return None
    s = str(s).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.replace(tzinfo=TZ_MSK) if dt.tzinfo is None else dt.astimezone(TZ_MSK)


def _parse_hourly_key(s) -> Optional[datetime]:
    """Парсит ключ часа из covered_area_hourly / movements_on_road_duration_hourly
    (формат вида '2026-08-11 04:00:00', наивный, локальное время Cropwise) —
    ОТДЕЛЬНО от _parse_dt, т.к. эти поля приходят в другом формате, чем
    hour_start в machine_gps_aggregated_data."""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt.replace(tzinfo=TZ_MSK) if dt.tzinfo is None else dt.astimezone(TZ_MSK)


def _floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _half_day_start(now: datetime) -> datetime:
    early_h, late_h = HALF_DAY_BOUNDARY_HOURS
    d = now.date()
    boundary_early = now.replace(year=d.year, month=d.month, day=d.day,
                                  hour=early_h, minute=0, second=0, microsecond=0)
    boundary_late = now.replace(year=d.year, month=d.month, day=d.day,
                                 hour=late_h, minute=0, second=0, microsecond=0)
    if now >= boundary_late:
        return boundary_late
    if now >= boundary_early:
        return boundary_early
    return boundary_late - timedelta(days=1)


def _window(now: datetime) -> tuple[datetime, datetime]:
    return _half_day_start(now), now


def _fmt_duration(seconds: float) -> str:
    secs = int(seconds)
    return f"{secs // 60} мин" if secs < 3600 else f"{secs / 3600:.1f} ч"


def _parse_hourly_pairs(raw) -> list[tuple[datetime, float]]:
    """Разбирает readonly-поля вида *_hourly (work_duration_hourly и т.п.).
    Ключи часа — тот же наивный формат 'YYYY-MM-DD HH:MM:SS', что и у
    covered_area_hourly (см. _parse_hourly_key), а не ISO8601 hour_start GPS."""
    out = []
    if not raw:
        return out
    if isinstance(raw, dict):
        entries = raw.items()
    elif isinstance(raw, list):
        entries = (
            (e[0], e[1]) for e in raw if isinstance(e, (list, tuple)) and len(e) >= 2
        )
    else:
        return out
    for ts_raw, val in entries:
        dt = _parse_hourly_key(ts_raw)
        if dt is None:
            continue
        out.append((_floor_hour(dt), float(val or 0)))
    return out


def _parse_timetable(raw) -> list[tuple[datetime, datetime]]:
    """work_timetable / movements_on_road_timetable → [(start, end), ...]."""
    out: list[tuple[datetime, datetime]] = []
    if not raw:
        return out
    for entry in raw:
        start = end = None
        if isinstance(entry, dict):
            start = entry.get("start") or entry.get("start_time") or entry.get("from")
            end = entry.get("end") or entry.get("end_time") or entry.get("to")
        elif isinstance(entry, (list, tuple)) and len(entry) == 2:
            start, end = entry
        s, e = _parse_dt(start), _parse_dt(end)
        if s is not None and e is not None:
            out.append((s, e))
    return out


def _seconds_in_hour(intervals: list[tuple[datetime, datetime]], hour: datetime) -> float:
    hour_end = hour + timedelta(hours=1)
    total = 0.0
    for s, e in intervals:
        a = max(s, hour)
        b = min(e, hour_end)
        if b > a:
            total += (b - a).total_seconds()
    return total


def _work_duration_by_hour(mtfmi_items: list[dict]) -> dict[datetime, float]:
    out: dict[datetime, float] = {}
    for item in mtfmi_items:
        for hour, secs in _parse_hourly_pairs(item.get("work_duration_hourly")):
            out[hour] = out.get(hour, 0.0) + secs
    return out


def _task_field_ids(task: dict, mtfmi_items: list[dict]) -> set[int]:
    fids: set[int] = set()
    if task.get("field_id") is not None:
        fids.add(int(task["field_id"]))
    for item in mtfmi_items:
        fid = item.get("field_id")
        if fid is not None:
            fids.add(int(fid))
    return fids


def _merge_intervals(
    ivs: list[tuple[datetime, datetime]],
    gap: timedelta,
) -> list[tuple[datetime, datetime]]:
    if not ivs:
        return []
    ordered = sorted(ivs, key=lambda x: x[0])
    merged: list[list[datetime]] = [[ordered[0][0], ordered[0][1]]]
    for a, b in ordered[1:]:
        if a <= merged[-1][1] + gap:
            if b > merged[-1][1]:
                merged[-1][1] = b
        else:
            merged.append([a, b])
    return [(x[0], x[1]) for x in merged]


def _build_moi_visits_map(
    moi_rows: list[dict],
    machine_ids: set[int],
    win_lo: datetime,
    win_hi: datetime,
    *,
    merge_gap_min: int = MOI_VISIT_MERGE_GAP_MIN,
) -> dict[tuple[int, int], list[tuple[datetime, datetime]]]:
    """{(machine_id, field_id): [интервал, ...]} — склеенные пересечения MOI в окне."""
    raw: dict[tuple[int, int], list[tuple[datetime, datetime]]] = defaultdict(list)
    for row in moi_rows:
        iv = moi_row_to_interval(row, open_end=win_hi)
        if iv is None:
            continue
        mid, fid, a, b = iv
        if mid not in machine_ids:
            continue
        a, b = max(a, win_lo), min(b, win_hi)
        if b > a:
            raw[(mid, fid)].append((a, b))
    gap = timedelta(minutes=merge_gap_min)
    return {k: _merge_intervals(v, gap) for k, v in raw.items()}


def _intervals_for_task_on_fields(
    moi_map: dict[tuple[int, int], list[tuple[datetime, datetime]]],
    machine_id: int,
    field_ids: set[int],
) -> list[tuple[datetime, datetime]]:
    parts: list[tuple[datetime, datetime]] = []
    for fid in field_ids:
        parts.extend(moi_map.get((machine_id, fid), []))
    return _merge_intervals(parts, timedelta(minutes=MOI_VISIT_MERGE_GAP_MIN))


def _clip_intervals_to_hour(
    intervals: list[tuple[datetime, datetime]],
    hour: datetime,
) -> list[tuple[datetime, datetime]]:
    hour_end = hour + timedelta(hours=1)
    out: list[tuple[datetime, datetime]] = []
    for s, e in intervals:
        a, b = max(s, hour), min(e, hour_end)
        if b > a:
            out.append((a, b))
    return out


def _max_speed_in_intervals(
    logger_items: list[dict],
    hour: datetime,
    intervals: list[tuple[datetime, datetime]],
) -> tuple[Optional[float], int, Optional[datetime]]:
    """max(speed) из gps_logger_data_items в заданных интервалах внутри часа."""
    if not logger_items or not intervals:
        return None, 0, None
    hour_end = hour + timedelta(hours=1)
    best_speed: Optional[float] = None
    best_time: Optional[datetime] = None
    n_points = 0
    for it in logger_items:
        t = _parse_dt(it.get("time"))
        sp = it.get("speed")
        if t is None or sp is None:
            continue
        if not (hour <= t < hour_end):
            continue
        if not any(s <= t < e for s, e in intervals):
            continue
        n_points += 1
        sp = float(sp)
        if best_speed is None or sp > best_speed:
            best_speed, best_time = sp, t
    return best_speed, n_points, best_time


def _task_road_seconds_by_hour(task: dict) -> dict[datetime, float]:
    """movements_on_road_duration_hourly задачи -> {час: секунд движения по
    дороге}. Поле встречается и как dict, и как список пар — обрабатываем оба."""
    raw = task.get("movements_on_road_duration_hourly")
    if not raw:
        return {}
    if isinstance(raw, dict):
        pairs = raw.items()
    elif isinstance(raw, list):
        pairs = ((e[0], e[1]) for e in raw if isinstance(e, (list, tuple)) and len(e) == 2)
    else:
        return {}
    out: dict[datetime, float] = {}
    for hour_key, seconds in pairs:
        dt = _parse_hourly_key(hour_key)
        if dt is None or seconds is None:
            continue
        out[dt] = out.get(dt, 0.0) + float(seconds)
    return out


def _task_field_hours(
    task: dict,
    mtfmi_items: list[dict],
    area_threshold_ha: float = FIELD_WORK_AREA_THRESHOLD_HA,
    road_seconds_threshold: float = ROAD_MOVEMENT_MAX_SECONDS_PER_HOUR,
    min_work_seconds: float = FIELD_WORK_MIN_SECONDS_PER_HOUR,
) -> set[datetime]:
    """
    Множество часов (datetime, начало часа), в которые задача РЕАЛЬНО
    обрабатывала поле — по covered_area_hourly из привязок к полям,
    просуммированному по всем полям задачи за этот час, БЕЗ часов, где:
      - movements_on_road_duration_hourly > road_seconds_threshold (0 сек),
      - movements_on_road_timetable пересекает этот час,
      - work_duration_hourly < min_work_seconds (переезд/начало смены).
    """
    area_by_hour: dict[datetime, float] = {}
    for item in mtfmi_items:
        for entry in (item.get("covered_area_hourly") or []):
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                continue
            hour_key, area = entry
            dt = _parse_hourly_key(hour_key)
            if dt is None or area is None:
                continue
            area_by_hour[dt] = area_by_hour.get(dt, 0.0) + float(area)
    hours = {dt for dt, area in area_by_hour.items() if area >= area_threshold_ha}

    road_by_hour = _task_road_seconds_by_hour(task)
    work_by_hour = _work_duration_by_hour(mtfmi_items)
    road_tt = _parse_timetable(task.get("movements_on_road_timetable"))

    filtered: set[datetime] = set()
    for dt in hours:
        if road_by_hour.get(dt, 0.0) > road_seconds_threshold:
            continue
        if _seconds_in_hour(road_tt, dt) > 0:
            continue
        if work_by_hour.get(dt, 0.0) < min_work_seconds:
            continue
        filtered.add(dt)
    return filtered


def _is_raw_gps_anomaly(distance_km: Optional[float], max_speed: Optional[float]) -> bool:
    """Пробег за час физически не может превышать max_speed * 1ч больше,
    чем на допуск — иначе это брак сырых GPS-данных, а не превышение."""
    if distance_km is None or max_speed is None:
        return False
    return distance_km > max_speed * RAW_DISTANCE_ANOMALY_RATIO + RAW_DISTANCE_ANOMALY_BUFFER_KM


# ── Агрофирма по полю (как в остальных проверках) ───────────────────────────

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


def _resolve_firm(folder_id, parent_map: dict, root_ids: set) -> int | None:
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


def _build_field_to_firm(fields_all, fgs_all, gfs_all) -> dict[int, str]:
    fgs_map = {int(fg["id"]): fg for fg in fgs_all if fg.get("id") is not None}
    parent_map = _folder_parent_map(gfs_all)
    root_ids = set(FOLDER_TO_FIRM.keys())
    out: dict[int, str] = {}
    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        raw_folder = f.get("group_folder_id")
        fgid = f.get("field_group_id")
        if raw_folder is None and fgid is not None:
            raw_folder = fgs_map.get(int(fgid), {}).get("group_folder_id")
        rint = int(raw_folder) if raw_folder is not None else None
        root = _resolve_firm(rint, parent_map, root_ids)
        if root is not None:
            firm = FOLDER_TO_FIRM.get(root)
            if firm:
                out[int(fid)] = firm
    return out


# =============================================================================
# ПАКЕТНАЯ ЗАГРУЗКА ДАННЫХ CROPWISE
# =============================================================================

def _dates_in_window(window_start: datetime, window_end: datetime) -> list[str]:
    return sorted({
        (window_start + timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range((window_end.date() - window_start.date()).days + 1)
    })


async def _gps_chunk_aiohttp(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    date_at: str,
    machine_ids: list[int],
) -> list[dict]:
    """gps_aggregated_data за календарные сутки для чанка машин."""
    params = {
        "date_at": date_at,
        "machine_ids": ",".join(str(mid) for mid in machine_ids),
        "per_page": 1000,
    }

    async def get_page(page: int) -> list[dict]:
        async with semaphore:
            async with session.get(
                CROPWISE_GPS_URL,
                params={**params, "page": page},
                timeout=GPS_AIOHTTP_TIMEOUT,
            ) as resp:
                body = await resp.json()
        return body if isinstance(body, list) else body.get("data", [])

    first = await get_page(1)
    results: list[dict] = list(first)
    if len(first) < 1000:
        return results

    page = 2
    while True:
        batch = await asyncio.gather(*(get_page(p) for p in range(page, page + 4)))
        stop = False
        for chunk in batch:
            results.extend(chunk)
            if len(chunk) < 1000:
                stop = True
        if stop:
            break
        page += 4
    return results


async def _fetch_gps_map(
    session: aiohttp.ClientSession,
    machine_ids: list[int],
    window_start: datetime,
    window_end: datetime,
) -> dict[int, list[dict]]:
    """Агрегированный GPS по машинам: date_at + machine_ids чанками."""
    gps_map: dict[int, list[dict]] = {mid: [] for mid in machine_ids}
    if not machine_ids:
        return gps_map

    gps_dates = _dates_in_window(window_start, window_end)
    gps_sem = asyncio.Semaphore(GPS_SEMAPHORE_LIMIT)
    coros = [
        _gps_chunk_aiohttp(session, gps_sem, date_str, machine_ids[i:i + GPS_CHUNK_SIZE])
        for date_str in gps_dates
        for i in range(0, len(machine_ids), GPS_CHUNK_SIZE)
    ]
    chunks_per_date = max(1, (len(machine_ids) + GPS_CHUNK_SIZE - 1) // GPS_CHUNK_SIZE)
    logger.info(
        f"speed_violations GPS: {len(coros)} запросов "
        f"({len(gps_dates)} дат × {chunks_per_date} чанков по {GPS_CHUNK_SIZE} машин)"
    )
    all_chunks = await asyncio.gather(*coros)
    skipped_outside = 0
    for chunk in all_chunks:
        for rec in chunk:
            mid_raw = rec.get("machine_id")
            hour_dt = _parse_dt(rec.get("hour_start"))
            if mid_raw is None or hour_dt is None:
                continue
            if not (window_start <= hour_dt < window_end):
                skipped_outside += 1
                continue
            gps_map.setdefault(int(mid_raw), []).append(rec)
    if skipped_outside:
        logger.debug(
            f"speed_violations GPS: отброшено {skipped_outside} записей "
            f"вне окна [{window_start}, {window_end})"
        )
    return gps_map


async def _prefetch_logger_cache(
    session: aiohttp.ClientSession,
    machine_ids: list[int],
    dates: list[str],
) -> dict[tuple[int, str], list[dict]]:
    """gps_logger_data_items: все (машина, день) параллельно, до цикла проверки."""
    logger_cache: dict[tuple[int, str], list[dict]] = {}
    if not machine_ids or not dates:
        return logger_cache

    logger_sem = asyncio.Semaphore(GPS_LOGGER_SEMAPHORE_LIMIT)

    async def _one_chunk(date_str: str, mids: list[int]) -> list[dict]:
        async with logger_sem:
            try:
                return await _fetch_all_pages(
                    session,
                    "gps_logger_data_items",
                    {"date_at": date_str, "machine_ids": ",".join(str(m) for m in mids)},
                    sem=logger_sem,
                )
            except Exception as e:
                logger.warning(
                    f"speed_violations: gps_logger_data_items failed "
                    f"date={date_str} machines={len(mids)}: {e}"
                )
                return []

    coros = [
        _one_chunk(date_str, machine_ids[i:i + LOGGER_CHUNK_SIZE])
        for date_str in dates
        for i in range(0, len(machine_ids), LOGGER_CHUNK_SIZE)
    ]
    chunks_per_date = max(1, (len(machine_ids) + LOGGER_CHUNK_SIZE - 1) // LOGGER_CHUNK_SIZE)
    logger.info(
        f"speed_violations logger: {len(coros)} запросов "
        f"({len(dates)} дат × {chunks_per_date} чанков по {LOGGER_CHUNK_SIZE} машин)"
    )
    for chunk in await asyncio.gather(*coros):
        for item in chunk:
            mid_raw = item.get("machine_id")
            t = _parse_dt(item.get("time"))
            if mid_raw is None or t is None:
                continue
            key = (int(mid_raw), t.strftime("%Y-%m-%d"))
            logger_cache.setdefault(key, []).append(item)
    return logger_cache


# =============================================================================
# ОСНОВНАЯ ЛОГИКА
# =============================================================================

async def refresh_speed_violations() -> dict:
    from app.cropwise.speed_violations_router import (
        update_cache, update_implement_max_cache, get_harvest_config,
    )
    from app.cropwise.speed_implements_dao import get_norms_map

    t_start = time.monotonic()
    harvest_cfg = get_harvest_config()
    HARVEST_MAX_SPEED_KMH = float(harvest_cfg["max_speed_kmh"])
    HARVEST_WORK_TYPE_IDS = frozenset(int(x) for x in harvest_cfg["work_type_ids"])
    HARVEST_INCLUDE_NO_IMPLEMENT = bool(harvest_cfg["include_no_implement"])

    def _stats(**kw) -> dict:
        """Показатели прогона для индикатора «здоровья» вкладки."""
        return {"duration_sec": round(time.monotonic() - t_start, 1), **kw}

    now = datetime.now(tz=TZ_MSK)
    day_start, cutoff = _window(now)
    day_start_str = day_start.strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    # {implement_id: {"speed_kmh": ..., "name": ...}} — справочник «Жатки» из JSON-файла.
    # Ошибку чтения файла НЕ глушим: лучше упасть и оставить прошлый кэш, чем
    # перезаписать его результатом без норм по оборудованию.
    implement_norms = await get_norms_map()
    logger.info(f"speed_violations refresh start: {day_start_str} — {cutoff_str}, "
                f"норм по оборудованию («Жатки») загружено: {len(implement_norms)}; "
                f"уборка: лимит={HARVEST_MAX_SPEED_KMH:g} км/ч, видов работ={len(HARVEST_WORK_TYPE_IDS)}, "
                f"без оборудования={'да' if HARVEST_INCLUDE_NO_IMPLEMENT else 'нет'}")

    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS, timeout=_CROPWISE_TIMEOUT) as session:
        (
            tasks_raw, machines_raw, work_types_raw,
            fields_raw, field_groups_raw, group_folders_raw,
            users_raw, local_fio_by_id,
        ) = await asyncio.gather(
            _fetch_all_pages(session, "machine_tasks", {
                "start_time_gt_eq": day_start_str,
                "start_time_lt":    cutoff_str,
            }),
            _fetch_all_pages(session, "machines"),
            _fetch_all_pages(session, "work_types"),
            _fetch_all_pages(session, "fields"),
            _fetch_all_pages(session, "field_groups"),
            _fetch_all_pages(session, "group_folders"),
            _fetch_all_pages(session, "users"),
            fetch_local_fio_map(),
        )
        logger.info(
            f"speed_violations fetch done: tasks={len(tasks_raw)}, "
            f"machines={len(machines_raw)}, fields={len(fields_raw)}"
        )

        machine_name: dict[int, str] = {}
        for m in machines_raw:
            mid = m.get("id")
            if mid is None:
                continue
            machine_name[int(mid)] = (m.get("custom_name") or m.get("name") or
                                       m.get("title") or m.get("model") or f"machine_{mid}")

        work_type_name: dict[int, str] = {
            w["id"]: (w.get("name") or w.get("title") or f"wt_{w['id']}")
            for w in work_types_raw
        }

        field_to_firm = _build_field_to_firm(fields_raw, field_groups_raw, group_folders_raw)

        # Механизаторы: id пользователя Cropwise → ФИО (как в «Запрещённых операциях»).
        operators_map: dict[int, str] = {}
        for u in users_raw:
            uid = u.get("id")
            if uid is not None:
                uid = int(uid)
                operators_map[uid] = (
                    local_fio_by_id.get(uid) or u.get("username") or u.get("email") or FIO_NOT_FOUND
                )

        # ── Задачи за окно: ВСЯ техника, любой вид работ ────────────────────
        seen_ids: set[int] = set()
        tasks: list[dict] = []
        for t in tasks_raw:
            tid = t.get("id")
            if tid in seen_ids or not t.get("machine_id"):
                continue
            seen_ids.add(tid)
            tasks.append(t)
        logger.info(f"speed_violations: задач за окно (вся техника)={len(tasks)}")

        if not tasks:
            update_cache([], now, [], _stats(tasks=0, work_items=0, checked_tasks=0), harvest_cfg)
            update_implement_max_cache({}, day_start, now)
            return {"rows": 0, "harvest_rows": 0, "checked_tasks": 0}

        # ── Что проверяем ────────────────────────────────────────────────────
        #  1) «Жатки»/plan_speed_max — как раньше: есть хоть один источник нормы;
        #  2) «Уборка»: любое оборудование, которого НЕТ в справочнике «Жатки»,
        #     с пределом HARVEST_MAX_SPEED_KMH. Задачи отбираем по work_type_id из
        #     уже загруженных machine_tasks (отдельный запрос не нужен).
        # Для остальных сверять не с чем — MTFMI/GPS по ним не тянем.
        work_items: list[tuple[dict, Optional[int], Optional[float], Optional[float], bool]] = []
        n_harvest_other = 0
        for t in tasks:
            impl_id = int(t["implement_id"]) if t.get("implement_id") else None
            impl_norm = implement_norms.get(impl_id, {}).get("speed_kmh") if impl_id else None
            plan_max = t.get("plan_speed_max")
            plan_max = float(plan_max) if plan_max is not None else None
            is_harvest_other = (
                t.get("work_type_id") in HARVEST_WORK_TYPE_IDS
                and impl_id not in implement_norms
                and (impl_id is not None or HARVEST_INCLUDE_NO_IMPLEMENT)
            )
            if impl_norm is None and plan_max is None and not is_harvest_other:
                continue
            n_harvest_other += 1 if is_harvest_other else 0
            work_items.append((t, impl_id, impl_norm, plan_max, is_harvest_other))

        # Для логов и «раннего выхода» — задачи именно под нормы «Жатки»/plan.
        tasks_with_norm = [(t, i, n, p) for t, i, n, p, _h in work_items if n is not None or p is not None]
        n_skipped_no_norm = len(tasks) - len(work_items)
        logger.info(
            f"speed_violations: к проверке задач={len(work_items)} "
            f"(норма «Жатки»/plan={len(tasks_with_norm)}, "
            f"уборка вне справочника, лимит {HARVEST_MAX_SPEED_KMH:g} км/ч={n_harvest_other}), "
            f"без нормы (пропущены)={n_skipped_no_norm}"
        )
        if not work_items:
            update_cache([], now, [], _stats(tasks=len(tasks), work_items=0, checked_tasks=0), harvest_cfg)
            update_implement_max_cache({}, day_start, now)
            return {"rows": 0, "harvest_rows": 0, "checked_tasks": 0}

        task_ids = [t["id"] for t, *_ in work_items]
        machine_ids = sorted({int(t["machine_id"]) for t, *_ in work_items})
        implement_ids = sorted({impl_id for _, impl_id, *_ in work_items if impl_id})

        implements_raw = (
            await _fetch_mass_request(session, "implements", implement_ids)
            if implement_ids else []
        )
        implement_name = {
            int(i["id"]): (i.get("name") or f"impl_{i['id']}")
            for i in implements_raw if i.get("id") is not None
        }

        # ── MTFMI пачками (machine_task_id через запятую) ───────────────────
        mtfmi_map = await _fetch_mtfmi_by_task_ids(
            session, task_ids, sem=asyncio.Semaphore(MTFMI_SEMAPHORE_LIMIT),
        )
        logger.info(
            f"speed_violations: MTFMI загружено для "
            f"{sum(1 for v in mtfmi_map.values() if v)}/{len(task_ids)} задач"
        )

        all_field_ids: set[int] = set()
        for t, *_ in work_items:
            all_field_ids |= _task_field_ids(t, mtfmi_map.get(t["id"], []))

        moi_map: dict[tuple[int, int], list[tuple[datetime, datetime]]] = {}
        if all_field_ids:
            moi_rows = await _fetch_machine_object_intersections(
                session,
                object_type="Field",
                hour_from=day_start - MOI_HOUR_LOOKBACK,
                hour_to=cutoff,
                machine_ids=machine_ids,
                object_ids=sorted(all_field_ids),
                sem=asyncio.Semaphore(MOI_SEMAPHORE_LIMIT),
            )
            moi_map = _build_moi_visits_map(
                moi_rows, set(machine_ids), day_start, cutoff,
            )
            logger.info(
                f"speed_violations: MOI rows={len(moi_rows)}, "
                f"machine×field pairs={len(moi_map)}"
            )

        # ── Агрегированный GPS: date_at + machine_ids чанками ──────────────
        gps_map = await _fetch_gps_map(session, machine_ids, day_start, cutoff)

        # ── gps_logger_data_items: предзагрузка до цикла проверки ──────────
        gps_dates = _dates_in_window(day_start, cutoff)
        logger_cache = await _prefetch_logger_cache(session, machine_ids, gps_dates)

        def _get_logger_items(mid: int, hour: datetime) -> list[dict]:
            return logger_cache.get((mid, hour.strftime("%Y-%m-%d")), [])

        # ── Сборка НАДЁЖНЫХ превышений ──────────────────────────────────────
        rows: list[dict] = []
        harvest_rows: list[dict] = []
        # Максимальная надёжная скорость по каждому оборудованию за окно —
        # для колонки «Макс. скорость» в модалке «Жатки» (считается по ВСЕМ
        # надёжным часам, а не только по превышениям).
        impl_max: dict[int, dict] = {}
        n_checked_tasks = 0
        n_skipped_no_field_hours = 0
        n_raw_anomalies_skipped = 0
        n_skipped_no_field_speed = 0

        for t, impl_id, impl_norm, plan_max, is_harvest_other in work_items:
            tid = t["id"]
            mid = int(t["machine_id"])
            mtfmi_items = mtfmi_map.get(tid, [])

            field_hours = _task_field_hours(t, mtfmi_items)
            if not field_hours:
                n_skipped_no_field_hours += 1
                continue  # нет надёжного подтверждения реальной полевой работы — пропускаем
            n_checked_tasks += 1

            # Длительность для отображения — сумма work_duration_hourly по
            # часам, которые прошли фильтр field_hours (реальное время
            # работы В ПОЛЕ в этот час, а не полный час целиком).
            duration_seconds_by_hour: dict[datetime, float] = {}
            fids = _task_field_ids(t, mtfmi_items)
            task_moi_iv = _intervals_for_task_on_fields(moi_map, mid, fids)
            for item in mtfmi_items:
                for hour, secs in _parse_hourly_pairs(item.get("work_duration_hourly")):
                    if hour in field_hours:
                        duration_seconds_by_hour[hour] = duration_seconds_by_hour.get(hour, 0.0) + secs

            firm = "—"
            for fid in fids:
                f = field_to_firm.get(fid)
                if f:
                    firm = f
                    break

            wt_name = work_type_name.get(t.get("work_type_id"), "—")
            machine_disp = machine_name.get(mid, f"machine_{mid}")
            implement_disp = implement_name.get(impl_id, "—") if impl_id else "—"
            try:
                driver_id = int(t["driver_id"]) if t.get("driver_id") is not None else None
            except (TypeError, ValueError):
                driver_id = None
            driver_disp = operators_map.get(driver_id, "—") if driver_id else "—"
            for rec in gps_map.get(mid, []):
                hour_start = _parse_dt(rec.get("hour_start"))
                if hour_start is None:
                    continue
                hour_start = _floor_hour(hour_start)
                if hour_start not in field_hours:
                    continue  # не подтверждённый полевой час этой задачи — пропускаем

                logger_items = _get_logger_items(mid, hour_start)
                moi_hour_iv = _clip_intervals_to_hour(task_moi_iv, hour_start)
                field_max, n_on_field, peak_time = _max_speed_in_intervals(
                    logger_items, hour_start, moi_hour_iv,
                )
                if not moi_hour_iv or n_on_field < MIN_GPS_POINTS_ON_FIELD or field_max is None:
                    n_skipped_no_field_speed += 1
                    continue
                max_speed = field_max

                distance_km = (rec.get("distance") / 1000.0) if rec.get("distance") is not None else None
                if _is_raw_gps_anomaly(distance_km, max_speed):
                    n_raw_anomalies_skipped += 1
                    continue  # брак GPS-трека — не доверяем max_speed этого часа

                # Запоминаем рекорд скорости по оборудованию (даже без превышения).
                if impl_id:
                    prev = impl_max.get(impl_id)
                    if prev is None or max_speed > prev["max_speed"]:
                        peak = peak_time or hour_start
                        impl_max[impl_id] = {
                            "max_speed":  round(max_speed, 1),
                            "time":       peak.strftime("%d.%m %H:%M"),
                            "time_ts":    int(peak.timestamp()),
                            "task_id":    tid,
                            "machine_id": mid,
                            "machine":    machine_disp,
                            "link":       f"{CROPWISE_TASK_BASE}/{mid}/tasks/{tid}",
                        }

                avg_speed = rec.get("avg_speed")

                def _make_row(norm_speed: float, norm_source: str, impl_norm_v, plan_v) -> dict:
                    excess_kmh = max_speed - norm_speed
                    excess_pct = (excess_kmh / norm_speed) * 100.0 if norm_speed else None
                    return {
                        "firm":           firm,
                        "machine":        machine_disp,
                        "machine_id":     mid,
                        "driver":         driver_disp,
                        "implement":      implement_disp,
                        "implement_id":   impl_id,
                        "work_type":      wt_name,
                        "hour_start":     hour_start.strftime("%d.%m %H:%M"),
                        "hour_start_ts":  int(hour_start.timestamp()),
                        "max_speed":      round(max_speed, 1),
                        "avg_speed":      round(float(avg_speed), 1) if avg_speed is not None else None,
                        "norm_speed":     round(norm_speed, 1),
                        "norm_source":    norm_source,
                        "implement_norm": round(impl_norm_v, 1) if impl_norm_v is not None else None,
                        "plan_speed_max": round(plan_v, 1) if plan_v is not None else None,
                        "excess_kmh":     round(excess_kmh, 1),
                        "excess_pct":     round(excess_pct, 1) if excess_pct is not None else None,
                        "duration":       _fmt_duration(duration_seconds_by_hour.get(hour_start, 0.0)),
                        "task_id":        tid,
                        "link":           f"{CROPWISE_TASK_BASE}/{mid}/tasks/{tid}",
                    }

                # ── Левая таблица: «Жатки» и/или plan_speed_max ──────────────
                exceeded_impl = impl_norm is not None and max_speed > impl_norm
                exceeded_plan = plan_max is not None and max_speed > plan_max
                if exceeded_impl or exceeded_plan:
                    applicable_norms = [n for n in (impl_norm, plan_max) if n is not None]
                    norm_speed = min(applicable_norms)
                    if impl_norm is not None and plan_max is not None:
                        norm_source = "Жатки + Cropwise"
                    elif impl_norm is not None:
                        norm_source = "Жатки"
                    else:
                        norm_source = "Cropwise (план)"
                    rows.append(_make_row(norm_speed, norm_source, impl_norm, plan_max))

                # ── Правая таблица: уборка, оборудование вне справочника ─────
                if is_harvest_other and max_speed > HARVEST_MAX_SPEED_KMH:
                    harvest_rows.append(_make_row(
                        HARVEST_MAX_SPEED_KMH, f"Уборка (лимит {HARVEST_MAX_SPEED_KMH:g} км/ч)", None, None,
                    ))

    rows.sort(key=lambda r: (
        FIRM_ORDER.index(r["firm"]) if r["firm"] in FIRM_ORDER else 999,
        r["machine"], r["hour_start_ts"],
    ))

    harvest_rows.sort(key=lambda r: (
        FIRM_ORDER.index(r["firm"]) if r["firm"] in FIRM_ORDER else 999,
        r["machine"], r["hour_start_ts"],
    ))

    update_cache(
        rows, now, harvest_rows,
        _stats(tasks=len(tasks), work_items=len(work_items), checked_tasks=n_checked_tasks),
        harvest_cfg,
    )
    update_implement_max_cache(impl_max, day_start, now)
    logger.info(
        f"speed_violations: {len(rows)} надёжных превышений (уборка вне справочника: {len(harvest_rows)}); оборудования с замером скорости={len(impl_max)}; проверено задач={n_checked_tasks} "
        f"(без нормы={n_skipped_no_norm}, без подтверждённых полевых часов={n_skipped_no_field_hours}, "
        f"без скорости в полевых интервалах={n_skipped_no_field_speed}, "
        f"часов отброшено как брак GPS-трека={n_raw_anomalies_skipped})"
    )
    return {"rows": len(rows), "harvest_rows": len(harvest_rows), "checked_tasks": n_checked_tasks}



# =============================================================================
# МАКСИМАЛЬНЫЕ СКОРОСТИ ЗА ПРОИЗВОЛЬНЫЙ ДЕНЬ (кнопка «Макс. скорости за день»)
# =============================================================================

async def compute_day_max_speeds(
    day: date,
    implement_ids: list[int],
    include_harvest: bool,
) -> dict:
    """Максимальная НАДЁЖНАЯ скорость по каждому оборудованию за сутки day
    (06:00 → 06:00 следующего дня, как граница суток во всех проверках).

    Что берём:
      - implement_ids — выбранные позиции справочника «Жатки», по заданиям ЛЮБОГО
        вида работ (жатка может стоять не только на «Уборке»);
      - include_harvest — всё оборудование на заданиях видов работ из «Настроек
        уборки» (в т.ч. не из справочника и самоходная техника без оборудования —
        такая строка группируется по машине).

    «Надёжность» замера — ровно как в основной проверке (_task_field_hours,
    MOI + gps_logger_data_items, MIN_GPS_POINTS_ON_FIELD, отсев брака GPS), поэтому цифры
    совпадают с колонкой «Макс. скорость» в модалке «Жатки». Кэши вкладки и
    справочника НЕ трогает — результат уходит вызывающему.
    """
    from app.cropwise.speed_violations_router import get_harvest_config
    from app.cropwise.speed_implements_dao import get_norms_map

    now = datetime.now(tz=TZ_MSK)
    day_start = datetime.combine(day, dtime(DAY_START_HOUR), tzinfo=TZ_MSK)
    if day_start >= now:
        raise ValueError("Этот день ещё не начался")
    day_end = min(day_start + timedelta(days=1), now)
    day_start_str = day_start.strftime("%Y-%m-%dT%H:%M:%S")
    day_end_str = day_end.strftime("%Y-%m-%dT%H:%M:%S")

    selected: set[int] = {int(x) for x in implement_ids}
    if not selected and not include_harvest:
        raise ValueError("Выберите жатки или включите «Всё оборудование на уборке»")

    harvest_cfg = get_harvest_config()
    harvest_wt = frozenset(int(x) for x in harvest_cfg["work_type_ids"])
    harvest_limit = float(harvest_cfg["max_speed_kmh"])
    norms = await get_norms_map()
    t0 = time.monotonic()
    logger.info(
        f"speed day_max start: {day_start_str} — {day_end_str}, выбрано жаток={len(selected)}, "
        f"вся уборка={'да' if include_harvest else 'нет'}"
    )

    def _meta(info: dict) -> dict:
        return {
            "window": f"{day_start:%d.%m %H:%M} — {day_end:%d.%m %H:%M}",
            "day": day.strftime("%d.%m.%Y"),
            "partial_day": day_end < day_start + timedelta(days=1),
            "harvest_limit": harvest_limit,
            "duration_sec": round(time.monotonic() - t0, 1),
            "generated_at": now.strftime("%d.%m.%Y %H:%M:%S"),
            **info,
        }

    def _no_tasks_rows() -> list[dict]:
        return [
            {
                "status": "no_tasks", "implement_id": iid,
                "implement": norms.get(iid, {}).get("name") or f"оборудование #{iid}",
                "inventory_number": norms.get(iid, {}).get("inventory_number") or "",
                "norm_speed": norms.get(iid, {}).get("speed_kmh"),
                "in_catalog": iid in norms, "selected": True,
            }
            for iid in sorted(selected)
        ]

    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS, timeout=_CROPWISE_TIMEOUT) as session:
        tasks_raw, machines_raw, work_types_raw = await asyncio.gather(
            _fetch_all_pages(session, "machine_tasks", {
                "start_time_gt_eq": day_start_str, "start_time_lt": day_end_str,
            }),
            _fetch_all_pages(session, "machines"),
            _fetch_all_pages(session, "work_types"),
        )
        machine_name = {
            int(m["id"]): (m.get("custom_name") or m.get("name") or m.get("title")
                           or m.get("model") or f"machine_{m['id']}")
            for m in machines_raw if m.get("id") is not None
        }
        work_type_name = {
            w["id"]: (w.get("name") or w.get("title") or f"wt_{w['id']}") for w in work_types_raw
        }

        # ── Отбор заданий ───────────────────────────────────────────────────
        seen: set = set()
        work: list[tuple[dict, Optional[int], bool]] = []   # (task, implement_id, is_selected_impl)
        for t in tasks_raw:
            tid = t.get("id")
            if tid in seen or not t.get("machine_id"):
                continue
            seen.add(tid)
            impl_id = int(t["implement_id"]) if t.get("implement_id") else None
            is_sel = impl_id is not None and impl_id in selected
            is_harvest = include_harvest and t.get("work_type_id") in harvest_wt
            if is_sel or is_harvest:
                work.append((t, impl_id, is_sel))
        logger.info(f"speed day_max: заданий за сутки={len(seen)}, к проверке={len(work)}")

        if not work:
            return {"rows": [], "no_data": _no_tasks_rows(), "stats": _meta({"tasks": 0, "checked_tasks": 0})}

        task_ids = [t["id"] for t, *_ in work]
        machine_ids = sorted({int(t["machine_id"]) for t, *_ in work})
        impl_ids_found = sorted({i for _, i, _s in work if i})
        implements_raw = await _fetch_mass_request(session, "implements", impl_ids_found) if impl_ids_found else []
        implement_name = {
            int(i["id"]): (i.get("name") or f"impl_{i['id']}")
            for i in implements_raw if i.get("id") is not None
        }

        mtfmi_map = await _fetch_mtfmi_by_task_ids(
            session, task_ids, sem=asyncio.Semaphore(MTFMI_SEMAPHORE_LIMIT),
        )

        all_field_ids: set[int] = set()
        for t, *_ in work:
            all_field_ids |= _task_field_ids(t, mtfmi_map.get(t["id"], []))

        moi_map: dict[tuple[int, int], list[tuple[datetime, datetime]]] = {}
        if all_field_ids:
            moi_rows = await _fetch_machine_object_intersections(
                session,
                object_type="Field",
                hour_from=day_start - MOI_HOUR_LOOKBACK,
                hour_to=day_end,
                machine_ids=machine_ids,
                object_ids=sorted(all_field_ids),
                sem=asyncio.Semaphore(MOI_SEMAPHORE_LIMIT),
            )
            moi_map = _build_moi_visits_map(
                moi_rows, set(machine_ids), day_start, day_end,
            )

        gps_map = await _fetch_gps_map(session, machine_ids, day_start, day_end)
        logger_cache = await _prefetch_logger_cache(session, machine_ids, _dates_in_window(day_start, day_end))

    # ── Сбор максимумов по оборудованию ─────────────────────────────────────
    # ключ: ("i", implement_id) либо ("m", machine_id) для техники без оборудования
    entities: dict[tuple, dict] = {}
    n_checked = 0
    for t, impl_id, is_sel in work:
        tid, mid = t["id"], int(t["machine_id"])
        key = ("i", impl_id) if impl_id else ("m", mid)
        ent = entities.setdefault(key, {
            "implement_id": impl_id,
            "implement": (norms.get(impl_id, {}).get("name") or implement_name.get(impl_id)
                          or f"оборудование #{impl_id}") if impl_id
                         else f"{machine_name.get(mid, f'machine_{mid}')} (без оборудования)",
            "inventory_number": norms.get(impl_id, {}).get("inventory_number") or "" if impl_id else "",
            "in_catalog": bool(impl_id and impl_id in norms),
            "selected": False,
            "norm_speed": (norms.get(impl_id, {}).get("speed_kmh") if impl_id and impl_id in norms
                           else harvest_limit),
            "tasks": 0, "best": None, "first_task": (tid, mid),
        })
        ent["tasks"] += 1
        ent["selected"] = ent["selected"] or is_sel

        mtfmi_items = mtfmi_map.get(tid, [])
        field_hours = _task_field_hours(t, mtfmi_items)
        if not field_hours:
            continue
        n_checked += 1
        fids = _task_field_ids(t, mtfmi_items)
        task_moi_iv = _intervals_for_task_on_fields(moi_map, mid, fids)

        for rec in gps_map.get(mid, []):
            hs = _parse_dt(rec.get("hour_start"))
            if hs is None:
                continue
            hs = _floor_hour(hs)
            if hs not in field_hours:
                continue
            items = logger_cache.get((mid, hs.strftime("%Y-%m-%d")), [])
            moi_hour_iv = _clip_intervals_to_hour(task_moi_iv, hs)
            fmax, n_on_field, peak = _max_speed_in_intervals(items, hs, moi_hour_iv)
            if not moi_hour_iv or n_on_field < MIN_GPS_POINTS_ON_FIELD or fmax is None:
                continue
            dist = (rec.get("distance") / 1000.0) if rec.get("distance") is not None else None
            if _is_raw_gps_anomaly(dist, fmax):
                continue
            best = ent["best"]
            if best is None or fmax > best["max_speed"]:
                pk = peak or hs
                ent["best"] = {
                    "max_speed": round(fmax, 1),
                    "time": pk.strftime("%d.%m %H:%M"),
                    "time_ts": int(pk.timestamp()),
                    "machine": machine_name.get(mid, f"machine_{mid}"),
                    "machine_id": mid,
                    "work_type": work_type_name.get(t.get("work_type_id"), "—"),
                    "task_id": tid,
                    "link": f"{CROPWISE_TASK_BASE}/{mid}/tasks/{tid}",
                }

    rows: list[dict] = []
    no_data: list[dict] = []
    for ent in entities.values():
        b = ent["best"]
        base = {k: ent[k] for k in ("implement_id", "implement", "inventory_number",
                                    "in_catalog", "selected", "norm_speed", "tasks")}
        if b is None:
            ft_tid, ft_mid = ent["first_task"]
            no_data.append({**base, "status": "no_reliable", "machine": machine_name.get(ft_mid, f"machine_{ft_mid}"),
                            "task_id": ft_tid, "link": f"{CROPWISE_TASK_BASE}/{ft_mid}/tasks/{ft_tid}"})
            continue
        norm = base["norm_speed"]
        rows.append({
            **base, **b, "status": "ok",
            "over": bool(norm is not None and b["max_speed"] > norm),
        })
    rows.sort(key=lambda r: (-r["max_speed"], r["implement"]))

    found_impl = {e["implement_id"] for e in entities.values() if e["implement_id"]}
    no_data += [r for r in _no_tasks_rows() if r["implement_id"] not in found_impl]
    no_data.sort(key=lambda r: (r["status"], r["implement"]))

    logger.info(
        f"speed day_max done: оборудования с замером={len(rows)}, без замера={len(no_data)}, "
        f"проверено заданий={n_checked}/{len(work)}"
    )
    return {
        "rows": rows, "no_data": no_data,
        "stats": _meta({"tasks": len(work), "checked_tasks": n_checked}),
    }
