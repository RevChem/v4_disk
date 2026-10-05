"""
cropwise/elevator_transfer_sync.py
====================================
Логика вкладки «Перемещение между элеваторами»: подозрительно быстрые
переезды грузовой техники между разными элеваторами
(tractor, lorrie, tipper, semi_truck).

Визит — пересечение машины с полигоном AdditionalObject
(machine_object_intersections): точные start_intersection / end_intersection.
Нарушение — разрыв между соседними визитами в разных геозонах не больше часа.
Полигоны одной площадки склеиваются. Пауза до 30 минут внутри геозоны тоже
склеивается: API режет стоянку у границы часа. Более длинный разрыв — новый визит.

В маршрут попадают все визиты машины за окно, а не только самая длинная
цепочка. Короткие заезды, которые почасовой GPS раньше выносил в
«не зафиксирован», остаются отдельными отрезками, если полигон их видит.

object_id из elevator_transfer_data.csv (28xx) — не id AdditionalObject
(GET /additional_objects/2862 → 404). Площадки сопоставлены по названию,
см. ELEVATOR_MOI_OBJECTS. Очередь ЧАТ объектом не является.

Окно: фиксированная граница «сегодня 06:00» − 24 ч (не скользящее).
Кэш — Redis (elevator_transfer_router.py), Celery elevator_transfer_refresh.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta, timezone

import aiohttp

from app.cropwise.client import (
    CROPWISE_HEADERS,
    _fetch_all_pages,
    _fetch_machine_object_intersections,
    _CROPWISE_TIMEOUT,
    moi_row_to_interval,
)
from app.logging_config import get_logger

logger = get_logger("cropwise.elevator_transfer")

TZ_MSK = timezone(timedelta(hours=3))

DAY_START_HOUR = 6
WINDOW_HOURS = 24.0
MAX_GAP_HOURS = 1.0
# Куски одного заезда API рвёт у границы часа, пауза около 12 минут.
# Более длинный разрыв — машина уехала и вернулась, это другой визит.
VISIT_MERGE_GAP = timedelta(minutes=30)
MIN_VISIT_MINUTES = 3.0

# Запись MOI может начаться до окна и длиться несколько часов (hour_start — час старта).
MOI_HOUR_LOOKBACK = timedelta(hours=12)
# id AdditionalObject → (геозона, подпись). Несколько полигонов одной площадки
# склеиваются как alias. Площадь объектов 3–27 га, это территория ЗСК, не район.
ELEVATOR_MOI_OBJECTS: dict[int, tuple[str, str]] = {
    522: ("CHAT", "Зерносклад АФ6 ЧАТ"),
    # «Аэропорт» в названии, по координатам это та же площадка ЧАТ (130–210 м от ЗСК).
    102: ("CHAT", "Зерносклад АФ6 ЧАТ"),
    346: ("CHAT", "Зерносклад АФ6 ЧАТ"),
    100: ("ETT_AFCH", "Элеватор Крутая Гора ЭТТ"),
    521: ("ETT_AFCH", "Элеватор Крутая Гора ЭТТ"),
    337: ("ETT_AFCH", "Элеватор Крутая Гора ЭТТ"),
    357: ("ETT_AFCH", "Элеватор Крутая Гора ЭТТ"),
    199: ("ETT_AFCH", "Элеватор Крутая Гора ЭТТ"),
    78:  ("AR", "Элеватор «Красный Октябрь» АР"),
    340: ("AR", "Элеватор «Красный Октябрь» АР"),
    520: ("AR", "Элеватор «Красный Октябрь» АР"),
    335: ("AA", "Зерносклад Автомат АА"),
    519: ("AA", "Зерносклад Автомат АА"),
}
ELEVATOR_MACHINE_SUBTYPES = frozenset({"tractor", "lorrie", "tipper", "semi_truck"})


def _geozone(object_id: int, geozone_by_object: dict[int, str]) -> str:
    return geozone_by_object.get(object_id, f"obj_{object_id}")


def _machine_label(m: dict) -> str:
    return (m.get("name") or m.get("custom_name") or m.get("model")
            or f"machine_{m.get('id')}")


def _fmt_duration(delta: timedelta) -> str:
    total_min = max(0, int(delta.total_seconds() // 60))
    return f"{total_min // 60}:{total_min % 60:02d}"


def _elevator_moi_maps() -> tuple[dict[int, str], dict[str, str]]:
    geozone_by_object: dict[int, str] = {}
    geozone_names: dict[str, str] = {}
    for oid, (gz, name) in ELEVATOR_MOI_OBJECTS.items():
        geozone_by_object[oid] = gz
        geozone_names.setdefault(gz, name)
    return geozone_by_object, geozone_names


def _merge_visits(
    points: list[tuple[int, datetime, datetime]],
    geozone_by_object: dict[int, str],
) -> list[tuple[int, datetime, datetime]]:
    """Склеивает визиты одной геозоны, если пауза не длиннее VISIT_MERGE_GAP."""
    if not points:
        return []
    gap = VISIT_MERGE_GAP
    visits: list[tuple[int, datetime, datetime]] = []
    cur_eid, cur_start, cur_end = points[0]
    cur_gz = _geozone(cur_eid, geozone_by_object)
    for eid, start, end in points[1:]:
        gz = _geozone(eid, geozone_by_object)
        if gz == cur_gz and (start - cur_end) <= gap:
            cur_end = max(cur_end, end)
        else:
            visits.append((cur_eid, cur_start, cur_end))
            cur_eid, cur_start, cur_end = eid, start, end
            cur_gz = gz
    visits.append((cur_eid, cur_start, cur_end))
    return visits


def _build_visits_from_moi(
    rows: list[dict],
    *,
    win_lo: datetime,
    win_hi: datetime,
    geozone_by_object: dict[int, str],
) -> list[tuple[int, datetime, datetime]]:
    """Визиты одной машины по MOI. id в кортеже — id AdditionalObject.

    Незакрытый end_intersection дотягивается до win_hi.
    """
    hits: list[tuple[int, datetime, datetime]] = []
    for row in rows:
        iv = moi_row_to_interval(row, open_end=win_hi)
        if iv is None:
            continue
        _mid, oid, start, end = iv
        if oid not in geozone_by_object:
            continue
        start, end = max(start, win_lo), min(end, win_hi)
        if end <= start:
            continue
        hits.append((oid, start, end))
    if not hits:
        return []
    hits.sort(key=lambda x: x[1])
    merged = _merge_visits(hits, geozone_by_object)
    thr = timedelta(minutes=MIN_VISIT_MINUTES)
    return [(o, s, e) for o, s, e in merged if (e - s) >= thr]


def _has_short_gap_transfer(
    visits: list[tuple[int, datetime, datetime]],
    geozone_by_object: dict[int, str],
) -> bool:
    """Нарушение — соседние визиты в разных геозонах с разрывом ≤ 1 ч."""
    ordered = sorted(visits, key=lambda v: v[1])
    threshold = timedelta(hours=MAX_GAP_HOURS)
    for prev, nxt in zip(ordered, ordered[1:]):
        if _geozone(prev[0], geozone_by_object) == _geozone(nxt[0], geozone_by_object):
            continue
        if nxt[1] - prev[2] <= threshold:
            return True
    return False


def _format_route_display(segments: list[dict]) -> str:
    lines: list[str] = []
    for i, seg in enumerate(segments):
        part = f"{seg['name']} ({seg['start'].strftime('%H:%M')}-{seg['end'].strftime('%H:%M')})"
        if i > 0:
            part = f"→ {part}"
        lines.append(part)
    return "\n".join(lines)


def _route_from_visits(
    visits: list[tuple[int, datetime, datetime]],
    geozone_by_object: dict[int, str],
    geozone_names: dict[str, str],
) -> tuple[str, list[dict], list[tuple[int, datetime, datetime]]]:
    """Все визиты окна по времени. Короткая остановка не выбрасывается из-за более длинной цепочки."""
    ordered = sorted(visits, key=lambda v: v[1])
    if len(ordered) < 2:
        return "", [], []
    segs = []
    for oid, start, end in ordered:
        gz = _geozone(oid, geozone_by_object)
        segs.append({
            "geozone": gz,
            "canon_id": oid,
            "name": geozone_names.get(gz, gz),
            "start": start,
            "end": end,
        })
    if len({s["geozone"] for s in segs}) < 2:
        return "", [], []
    return _format_route_display(segs), segs, ordered


def _violators_in_window(
    by_machine: dict[int, list[dict]],
    transport: dict[int, dict],
    lo: datetime,
    hi: datetime,
    geozone_by_object: dict[int, str],
    geozone_names: dict[str, str],
) -> list[dict]:
    """Машины-нарушители в одном окне [lo, hi). Правила те же, что у ежечасного расчёта."""
    violators: list[dict] = []
    for mid, rows in by_machine.items():
        visits = _build_visits_from_moi(
            rows, win_lo=lo, win_hi=hi, geozone_by_object=geozone_by_object,
        )
        if not _has_short_gap_transfer(visits, geozone_by_object):
            continue
        route, segs, chain_visits = _route_from_visits(visits, geozone_by_object, geozone_names)
        if not route:
            continue
        machine = transport[mid]
        violators.append({
            "machine_id": mid,
            "machine": _machine_label(machine),
            "route": route,
            "route_warnings": [],
            "n_elevators": len({s["geozone"] for s in segs if s.get("geozone")}),
            "first_start": segs[0]["start"],
            "last_start": segs[-1]["start"],
            "visits": chain_visits,
        })
    violators.sort(key=lambda v: (v["first_start"], v["machine_id"]))
    return violators


def _rows_from_violators(
    violators: list[dict],
    geozone_by_object: dict[int, str],
    geozone_names: dict[str, str],
    *,
    day: str | None = None,
    summary_no: int = 0,
    detail_no: int = 0,
) -> tuple[list[dict], list[dict]]:
    """Строки «сводки» и «детализации». day — подпись суток (только для прогона за период);
    нумерация продолжается с summary_no / detail_no."""
    summary_rows: list[dict] = []
    detail_rows: list[dict] = []
    for v in violators:
        summary_no += 1
        srow = {
            "no": summary_no,
            "machine": v["machine"],
            "machine_id": v["machine_id"],
            "n_elevators": v["n_elevators"],
            "route": v["route"],
            "route_warnings": [],
            "route_warnings_text": "",
            "first_visit": v["first_start"].strftime("%d.%m.%Y %H:%M"),
            "first_visit_ts": int(v["first_start"].timestamp()),
            "last_visit": v["last_start"].strftime("%d.%m.%Y %H:%M"),
            "last_visit_ts": int(v["last_start"].timestamp()),
        }
        if day is not None:
            srow["day"] = day
        summary_rows.append(srow)
        for oid, start, end in v["visits"]:
            detail_no += 1
            drow = {
                "no": detail_no,
                "machine": v["machine"],
                "machine_id": v["machine_id"],
                "elevator": geozone_names.get(
                    _geozone(oid, geozone_by_object),
                    f"object_{oid}",
                ),
                "elevator_id": oid,
                "visit_start": start.strftime("%d.%m.%Y %H:%M"),
                "visit_start_ts": int(start.timestamp()),
                "visit_end": end.strftime("%d.%m.%Y %H:%M"),
                "visit_end_ts": int(end.timestamp()),
                "duration": _fmt_duration(end - start),
            }
            if day is not None:
                drow["day"] = day
            detail_rows.append(drow)
    return summary_rows, detail_rows


async def _fetch_transport_and_moi(
    lo: datetime, hi: datetime, object_ids: list[int],
) -> tuple[dict[int, dict], list[dict]]:
    """Грузовые машины и их MOI по площадкам за [lo - 12 ч, hi)."""
    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS, timeout=_CROPWISE_TIMEOUT) as session:
        subtype_batches = await asyncio.gather(*[
            _fetch_all_pages(session, "machines", {"machine_subtype": st})
            for st in sorted(ELEVATOR_MACHINE_SUBTYPES)
        ])
        machines_raw = [m for batch in subtype_batches for m in batch]
        transport = {
            m["id"]: m for m in machines_raw
            if (m.get("machine_subtype") or "").lower() in ELEVATOR_MACHINE_SUBTYPES
        }
        logger.info(
            f"elevator_transfer: грузовых машин "
            f"(subtypes={sorted(ELEVATOR_MACHINE_SUBTYPES)})={len(transport)}"
        )
        if not transport:
            return {}, []
        moi_rows = await _fetch_machine_object_intersections(
            session,
            object_type="AdditionalObject",
            hour_from=lo - MOI_HOUR_LOOKBACK,
            hour_to=hi,
            object_ids=object_ids,
            machine_ids=list(transport),
        )
    logger.info(f"elevator_transfer: MOI записей={len(moi_rows)}")
    return transport, moi_rows


def _group_moi_by_machine(moi_rows: list[dict], transport: dict[int, dict]) -> dict[int, list[dict]]:
    by_machine: dict[int, list[dict]] = defaultdict(list)
    for row in moi_rows:
        mid = row.get("machine_id")
        if mid in transport:
            by_machine[int(mid)].append(row)
    return by_machine


async def refresh_elevator_transfer() -> dict:
    from app.cropwise.elevator_transfer_router import update_cache

    now = datetime.now(tz=TZ_MSK)
    hi = now.replace(hour=DAY_START_HOUR, minute=0, second=0, microsecond=0)
    lo = hi - timedelta(hours=WINDOW_HOURS)
    geozone_by_object, geozone_names = _elevator_moi_maps()

    logger.info(
        f"elevator_transfer refresh start: {lo:%Y-%m-%d %H:%M} — {hi:%Y-%m-%d %H:%M}, "
        f"additional_objects={sorted(ELEVATOR_MOI_OBJECTS)}"
    )

    transport, moi_rows = await _fetch_transport_and_moi(lo, hi, list(ELEVATOR_MOI_OBJECTS))
    if not transport:
        update_cache([], [], now)
        return {"rows": 0, "checked_machines": 0}

    by_machine = _group_moi_by_machine(moi_rows, transport)
    violators = _violators_in_window(
        by_machine, transport, lo, hi, geozone_by_object, geozone_names,
    )
    summary_rows, detail_rows = _rows_from_violators(violators, geozone_by_object, geozone_names)

    update_cache(summary_rows, detail_rows, now)
    logger.info(
        f"elevator_transfer: нарушителей={len(summary_rows)}, "
        f"визитов={len(detail_rows)}, машин с MOI={len(by_machine)}"
    )
    return {
        "violators": len(summary_rows),
        "visits": len(detail_rows),
        "checked_machines": len(transport),
        "moi_records": len(moi_rows),
    }


# ─── Прогон за произвольный период (модальное окно во вкладке) ───────────────
MAX_RANGE_DAYS = 31


def range_day_windows(
    date_from: date, date_to: date, now: datetime,
) -> list[tuple[date, datetime, datetime]]:
    """Сутки периода как окна «06:00 → 06:00 следующего дня» — та же граница суток,
    что у ежечасного расчёта. Сутки, ещё не начавшиеся, пропускаются; текущие
    обрезаются по now. Каждые сутки проверяются отдельно: цепочка заездов не
    переходит через 06:00."""
    out: list[tuple[date, datetime, datetime]] = []
    d = date_from
    while d <= date_to:
        lo = datetime.combine(d, dtime(DAY_START_HOUR), tzinfo=TZ_MSK)
        if lo >= now:
            break
        out.append((d, lo, min(lo + timedelta(hours=WINDOW_HOURS), now)))
        d += timedelta(days=1)
    return out


async def compute_elevator_transfer_range(date_from: date, date_to: date) -> dict:
    """Те же правила, что у ежечасного расчёта, но за период date_from … date_to
    включительно (сутки 06:00–06:00). Кэш вкладки НЕ трогает — результат отдаётся
    вызывающему. MOI грузится одним запросом на весь период."""
    if date_to < date_from:
        raise ValueError("Дата «по» раньше даты «с»")
    if (date_to - date_from).days + 1 > MAX_RANGE_DAYS:
        raise ValueError(f"Период не должен быть длиннее {MAX_RANGE_DAYS} суток")

    now = datetime.now(tz=TZ_MSK)
    windows = range_day_windows(date_from, date_to, now)
    if not windows:
        raise ValueError("Выбранный период ещё не начался")

    geozone_by_object, geozone_names = _elevator_moi_maps()
    lo_all, hi_all = windows[0][1], windows[-1][2]
    logger.info(
        f"elevator_transfer range start: {lo_all:%Y-%m-%d %H:%M} — {hi_all:%Y-%m-%d %H:%M}, "
        f"суток={len(windows)}"
    )

    transport, moi_rows = await _fetch_transport_and_moi(lo_all, hi_all, list(ELEVATOR_MOI_OBJECTS))
    summary_rows: list[dict] = []
    detail_rows: list[dict] = []
    by_machine = _group_moi_by_machine(moi_rows, transport) if transport else {}

    for d, lo, hi in windows:
        violators = _violators_in_window(
            by_machine, transport, lo, hi, geozone_by_object, geozone_names,
        )
        s_rows, d_rows = _rows_from_violators(
            violators, geozone_by_object, geozone_names,
            day=d.strftime("%d.%m.%Y"),
            summary_no=len(summary_rows), detail_no=len(detail_rows),
        )
        summary_rows += s_rows
        detail_rows += d_rows

    last_day = windows[-1][0]
    return {
        "summary": summary_rows,
        "detail": detail_rows,
        "date_from": date_from.strftime("%d.%m.%Y"),
        "date_to": date_to.strftime("%d.%m.%Y"),
        "days": len(windows),
        "partial_last_day": windows[-1][2] < windows[-1][1] + timedelta(hours=WINDOW_HOURS),
        "last_day": last_day.strftime("%d.%m.%Y"),
        "checked_machines": len(transport),
        "moi_records": len(moi_rows),
        "generated_at": now.strftime("%d.%m.%Y %H:%M:%S"),
    }
