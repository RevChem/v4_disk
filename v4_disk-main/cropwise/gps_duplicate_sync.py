"""
cropwise/gps_duplicate_sync.py
=================================
Вкладка «GPS трекер на нескольких машинах»: ищет случаи, когда один и
тот же физический GPS-логгер числится АКТИВНЫМ (end_time = nil) сразу
у нескольких машин. Это ошибка: например, трекер сняли с одной машины
и поставили на другую, но старую запись забыли закрыть (не проставили
end_time).

Адаптация автономного скрипта gps_tracker.py (check_gps_duplicate.py) —
логика поиска конфликтов перенесена без изменений, HTTP-слой заменён
на общий асинхронный клиент Cropwise (client.py: _fetch_all_pages),
который постранично обходит ровно так же (from_id/limit), как get_all()
в исходном скрипте.

Проверяются отдельно два источника (как в исходном скрипте):
  - GpsLoggerMappingItems  — физическая установка трекера на машину
  - DataSourceGpsLoggers   — источник, из которого реально берутся
                             GPS-данные для машины

Запускается ТОЛЬКО вручную (кнопка «Обновить» в UI) — без Celery
beat-расписания, см. tasks.py / celery_config.py.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp

from app.cropwise.client import CROPWISE_HEADERS, _fetch_all_pages, _CROPWISE_TIMEOUT
from app.logging_config import get_logger

logger = get_logger("cropwise.gps_duplicate")

TZ_MSK = timezone(timedelta(hours=3))
CROPWISE_MACHINE_BASE = "https://operations.cropwise.com/machines"


# =============================================================================
# ДАТА / ВРЕМЯ (перенесено без изменений)
# =============================================================================

def parse_dt(s) -> Optional[datetime]:
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
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_MSK)
    else:
        dt = dt.astimezone(TZ_MSK)
    return dt


def fmt_dt(dt: Optional[datetime]) -> str:
    return dt.strftime("%d.%m.%Y %H:%M") if dt else "—"


# =============================================================================
# ОСНОВНАЯ ЛОГИКА (перенесено без изменений, HTTP — через _fetch_all_pages)
# =============================================================================

async def _find_active_conflicts(
    session: aiohttp.ClientSession, endpoint: str, mappable_type: str = "Machine",
) -> list[dict]:
    """
    Возвращает список ТОЛЬКО активных записей (end_time is None),
    сгруппированных по gps_logger_id, где машин больше одной.
    Формат элемента: {"logger_id": int, "records": [ {machine_id, start_time}, ... ]}
    """
    items = await _fetch_all_pages(session, endpoint, {"mappable_type": mappable_type})
    active = [it for it in items if not it.get("end_time")]

    by_logger: dict[int, list[dict]] = defaultdict(list)
    for it in active:
        lid = it.get("gps_logger_id")
        mid = it.get("mappable_id")
        if lid is None or mid is None:
            continue
        by_logger[int(lid)].append({
            "machine_id": int(mid),
            "start_time": it.get("start_time"),
        })

    conflicts = []
    for lid, records in by_logger.items():
        distinct_machines = {r["machine_id"] for r in records}
        if len(distinct_machines) > 1:
            conflicts.append({"logger_id": lid, "records": records})

    logger.info(
        f"gps_duplicate [{endpoint}]: записей={len(items)}, активных={len(active)}, "
        f"конфликтов={len(conflicts)}"
    )
    return conflicts


def _rows_from_conflicts(
    conflicts: list[dict], machine_names: dict[int, str], loggers: dict[int, dict],
) -> list[dict]:
    """
    Разворачивает список конфликтов (сгруппированных по логгеру) в плоский
    список строк — одна строка на пару (логгер, машина) — для таблицы на
    фронтенде и Excel-экспорта (колонки как в исходном скрипте: GPS
    логгер | IMEI | Машина | Установлен с | Ссылка на машину).
    """
    rows: list[dict] = []
    conflicts_sorted = sorted(conflicts, key=lambda c: c["logger_id"])
    for conf in conflicts_sorted:
        lid = conf["logger_id"]
        imei = loggers.get(lid, {}).get("imei", "—")
        recs = sorted(conf["records"], key=lambda rr: machine_names.get(rr["machine_id"], ""))
        for rec in recs:
            mid = rec["machine_id"]
            rows.append({
                "logger_id":  lid,
                "imei":       imei,
                "machine_id": mid,
                "machine":    machine_names.get(mid, f"machine_{mid}"),
                "start_time": fmt_dt(parse_dt(rec["start_time"])),
                "link":       f"{CROPWISE_MACHINE_BASE}/{mid}",
            })
    return rows


async def refresh_gps_duplicate() -> dict:
    from app.cropwise.gps_duplicate_router import update_cache

    now = datetime.now(tz=TZ_MSK)

    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS, timeout=_CROPWISE_TIMEOUT) as session:
        machines_raw = await _fetch_all_pages(session, "machines")
        machine_names: dict[int, str] = {}
        for m in machines_raw:
            mid = m.get("id")
            if mid is None:
                continue
            machine_names[int(mid)] = (m.get("custom_name") or m.get("name") or f"machine_{mid}")

        mapping_conflicts = await _find_active_conflicts(session, "gps_logger_mapping_items")
        source_conflicts  = await _find_active_conflicts(session, "data_source_gps_loggers")

        all_logger_ids = {c["logger_id"] for c in mapping_conflicts + source_conflicts}
        loggers: dict[int, dict] = {}
        if all_logger_ids:
            all_loggers = await _fetch_all_pages(session, "gps_loggers")
            for lg in all_loggers:
                lgid = lg.get("id")
                if lgid is not None and int(lgid) in all_logger_ids:
                    loggers[int(lgid)] = lg

    mapping_rows = _rows_from_conflicts(mapping_conflicts, machine_names, loggers)
    source_rows  = _rows_from_conflicts(source_conflicts, machine_names, loggers)

    update_cache(mapping_rows, source_rows, now)

    result = {"mapping": len(mapping_rows), "source": len(source_rows)}
    logger.info(f"gps_duplicate refresh done: {result}")
    return result
