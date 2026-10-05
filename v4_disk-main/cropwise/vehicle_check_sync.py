"""
cropwise/vehicle_check_sync.py
================================
Логика вкладки «Проверка техники»: техника с активным агрозаданием
(Опрыскивание / Уборка / Культивация / Посев) в Cropwise, у которой
ПРЯМО СЕЙЧАС зафиксирована потеря связи с GPS-терминалом.

Cropwise не отдаёт напрямую признак "отсутствие напряжения с внешних
источников" — вместо этого сигнал строится по отсутствию точек GPS-трека
(machine_gps_aggregated_data). Это тот же принцип, что подтверждён
диагностикой на задаче 406678: "Нет данных" в интерфейсе Cropwise =
отсутствующие/пустые часовые записи в machine_gps_aggregated_data (см.
diag_gps_gaps_406678.py).

Логика:
  1. Активные СЕЙЧАС агрозадания: start_time <= now <= end_time (+буфер
     10 мин — та же логика, что в sync_assignments._fetch_active_assignments),
     у которых work_type_id входит в одну из 4 целевых групп (см.
     TARGET_WORK_GROUPS; id взяты из CHECK1_WORK_GROUPS в violations_sync.py).
  2. Смотрим строго в рамках "операционных суток" — с 06:00 текущего дня
     до 05:59:59 следующего (та же граница, что и DAY_START_HOUR в
     check1_no_agro_ops.py). При пересечении 06:00 отсчёт провала связи
     начинается заново — данные за прошлые сутки не переносятся.
  3. Для каждой машины запрашивается machine_gps_aggregated_data за окно
     [max(начало операционных суток, task.start_time), сейчас]. Из
     часовых записей окна собираются все доступные метки реального
     времени приёма сигнала (last_time / first_move_time / last_move_time
     каждой часовой записи) — это точнее, чем считать пустым весь час
     целиком, и позволяет ловить провал с точностью до минут, а не часов.
  4. Берём последнюю по времени метку сигнала в окне. Если от неё до
     "сейчас" прошло больше MIN_GAP_MINUTES (5 минут) — считаем, что
     связь потеряна прямо сейчас, а началом провала — саму эту метку
     (если меток вообще нет в пределах операционных суток — считаем
     началом провала начало окна, т.е. связи не было с начала суток).
  5. Раздел показывает только ТЕКУЩИЕ (незакрытые к моменту проверки)
     провалы связи — соответствует "оперативное выявление фактов потери
     связи" из ТЗ. Причины отсутствия связи не определяются и не хранятся.

Кэш — Redis (см. vehicle_check_router.py), обновляется Celery-задачей
vehicle_check_refresh.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp

from app.cropwise.client import (
    CROPWISE_HEADERS,
    FIO_NOT_FOUND,
    _CROPWISE_TIMEOUT,
    _fetch_all_pages,
    _fetch_history_items_for_date_range,
    _fetch_mtfmi_by_task_ids,
    fetch_local_fio_map,
)
from app.logging_config import get_logger

logger = get_logger("cropwise.vehicle_check_sync")

TZ_MSK = timezone(timedelta(hours=3))
CROPWISE_TASK_BASE = "https://operations.cropwise.com/machines"
CROPWISE_GPS_MACHINE_URL = "https://operations.cropwise.com/api/v3/machine_gps_aggregated_data"

# Задание считается активным ещё N минут после end_time — та же логика, что
# и в sync_assignments._fetch_active_assignments (защита от погрешностей
# сети/часов/задержек синхронизации).
ACTIVE_END_BUFFER_MINUTES = 10

# Насколько далеко назад смотрим GPS при поиске начала провала связи.
# Операционные сутки: с 06:00 текущего дня до 05:59:59 следующего — та же
# граница, что в check1_no_agro_ops.py (DAY_START_HOUR). При пересечении
# 06:00 отсчёт провала связи начинается заново, старые сутки не переносятся.
DAY_START_HOUR = 6

# Порог срабатывания: если с последнего подтверждённого сигнала GPS прошло
# больше этого времени — машина считается потерявшей связь.
MIN_GAP_MINUTES = 5

# Порог отображения: в раздел попадают только провалы связи
# продолжительностью БОЛЕЕ 3 часов (короткие провалы — не показываем,
# даже если они формально ещё не закрылись).
MIN_DISPLAY_DURATION_SECONDS = 3 * 3600

GPS_SEMAPHORE_LIMIT = 15
GPS_REQUEST_TIMEOUT = 30.0
MTFMI_SEMAPHORE_LIMIT = 20

# ── Агрофирмы — те же корневые папки, что и в violations_sync.py / check1 ───
FIRM_TO_FOLDER: dict[str, int] = {
    "АФ Чистопольская": 3, "АгроРаздолье": 4, "Аксу-Агро": 5,
    "НурлатАгро": 11, "Дрожжаное Рассвет Агро": 6,
    "Актай": 7, "Лениногорская Птицефабрика": 9,
}
FOLDER_TO_FIRM: dict[int, str] = {v: k for k, v in FIRM_TO_FOLDER.items()}

# ── Целевые виды работ — id взяты из CHECK1_WORK_GROUPS (violations_sync.py) ─
TARGET_WORK_GROUPS: dict[str, list[int]] = {
    "Опрыскивание": [1, 64, 65, 66, 153],                                    # 3.1
    "Уборка":       [6, 7, 75, 76, 77, 111, 118, 125, 126, 145],             # 5.0 (+5.4)
    "Культивация":  [20, 50, 52, 63, 96],                                    # 2.11
    "Посев":        [3, 4, 69, 70, 71, 72, 119, 120, 159, 160, 161, 163,     # 4.0
                      68, 148, 172],                                         # 4.1 (овощи)
}
WORK_TYPE_ID_TO_LABEL: dict[int, str] = {
    wt_id: label for label, ids in TARGET_WORK_GROUPS.items() for wt_id in ids
}
TARGET_WORK_TYPE_IDS: frozenset[int] = frozenset(WORK_TYPE_ID_TO_LABEL.keys())


# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =============================================================================

def _parse_dt(s) -> Optional[datetime]:
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


def _operational_day_start(now: datetime) -> datetime:
    """
    Начало "операционных суток", которым принадлежит момент `now`:
    сутки идут с DAY_START_HOUR:00 текущего календарного дня до
    DAY_START_HOUR:00 следующего (не включительно). Если сейчас ещё не
    наступило DAY_START_HOUR — значит текущие операционные сутки
    начались в DAY_START_HOUR:00 ВЧЕРА.
    """
    d = now.date()
    start_today = now.replace(year=d.year, month=d.month, day=d.day,
                               hour=DAY_START_HOUR, minute=0, second=0, microsecond=0)
    return start_today if now >= start_today else start_today - timedelta(days=1)


def _fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    if h and m:
        return f"{h}ч {m}м"
    if h:
        return f"{h}ч"
    return f"{m}м" if m else "<1м"


# ── Агрофирма по полю (как в check1_no_agro_ops.py / violations_sync.py) ────

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


# ── GPS для одной машины ─────────────────────────────────────────────────────

async def _fetch_machine_gps(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    machine_id: int,
    from_dt: datetime,
    to_dt: datetime,
) -> list[dict]:
    """machine_gps_aggregated_data: все 3 параметра обязательны (по документации)."""
    params = {
        "machine_id": machine_id,
        "from_time":  from_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "to_time":    to_dt.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    try:
        async with semaphore:
            async with session.get(
                CROPWISE_GPS_MACHINE_URL, params=params,
                timeout=aiohttp.ClientTimeout(total=GPS_REQUEST_TIMEOUT),
            ) as resp:
                resp.raise_for_status()
                body = await resp.json()
        return body.get("data", body if isinstance(body, list) else [])
    except Exception as e:
        logger.warning(f"vehicle_check: GPS fetch failed for machine_id={machine_id}: {e}")
        return []


def _collect_signal_markers(gps_rows: list[dict]) -> list[datetime]:
    """
    Собирает все доступные метки времени реального приёма сигнала из
    часовых записей: last_time (последняя точка часа) и, если есть,
    first_move_time / last_move_time (точки начала/конца движения внутри
    часа). Это даёт точность до минут вместо "весь час либо есть, либо
    нет" — иначе провал длиной, скажем, 20 минут внутри часа с движением
    в начале и в конце остался бы незамеченным.
    """
    markers: set[datetime] = set()
    for row in gps_rows:
        for key in ("last_time", "first_move_time", "last_move_time"):
            dt = _parse_dt(row.get(key))
            if dt:
                markers.add(dt)
    return sorted(markers)


def _find_ongoing_gap(
    gps_rows: list[dict],
    window_start: datetime,
    now: datetime,
) -> Optional[datetime]:
    """
    Возвращает время НАЧАЛА текущего (ещё не закрытого) провала связи,
    либо None, если сигнал в пределах MIN_GAP_MINUTES есть. Провал
    считается от последней подтверждённой метки сигнала в пределах
    операционных суток (window_start) до "сейчас"; если меток вообще нет —
    связи не было с самого начала операционных суток.
    """
    markers = _collect_signal_markers(gps_rows)
    last_marker = markers[-1] if markers else None

    baseline = last_marker if (last_marker and last_marker >= window_start) else window_start
    gap = now - baseline
    if gap > timedelta(minutes=MIN_GAP_MINUTES):
        return baseline
    return None


# =============================================================================
# ОСНОВНАЯ ЛОГИКА
# =============================================================================

async def refresh_vehicle_check() -> dict:
    from app.cropwise.vehicle_check_router import update_cache

    now = datetime.now(tz=TZ_MSK)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%S")
    end_buffer_str = (now - timedelta(minutes=ACTIVE_END_BUFFER_MINUTES)).strftime("%Y-%m-%dT%H:%M:%S")
    day_start = _operational_day_start(now)

    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS, timeout=_CROPWISE_TIMEOUT) as session:
        (
            tasks_raw, machines_raw, work_types_raw, users_raw,
            fields_raw, field_groups_raw, group_folders_raw,
            crops_raw, history_items_raw, inventory_history_raw,
        ) = await asyncio.gather(
            _fetch_all_pages(session, "machine_tasks", {
                "start_time_lt_eq": now_str,
                "end_time_gt_eq":   end_buffer_str,
            }),
            _fetch_all_pages(session, "machines"),
            _fetch_all_pages(session, "work_types"),
            _fetch_all_pages(session, "users"),
            _fetch_all_pages(session, "fields"),
            _fetch_all_pages(session, "field_groups"),
            _fetch_all_pages(session, "group_folders"),
            _fetch_all_pages(session, "crops"),
            _fetch_history_items_for_date_range(session, now.date(), now.date()),
            # Скрытые машины — те же параметры, что и в violations_sync.py:
            # event_start_at == event_end_at == сейчас отдаёт только АКТУАЛЬНЫЕ
            # на данный момент записи hidden=true (без выкачки всей истории).
            _fetch_all_pages(session, "inventory_history_items", {
                "historyable_type": "Machine",
                "hidden":           "true",
                "event_start_at":   now_str,
                "event_end_at":     now_str,
            }),
        )

        logger.info(
            f"vehicle_check: tasks={len(tasks_raw)}, machines={len(machines_raw)}, "
            f"fields={len(fields_raw)}"
        )

        # ── Справочники ──────────────────────────────────────────────────────
        hidden_ids: set[int] = {
            int(item["historyable_id"]) for item in inventory_history_raw
            if item.get("historyable_id") is not None
        }

        work_type_map: dict[int, str] = {
            w["id"]: (w.get("name") or w.get("title") or f"wt_{w['id']}")
            for w in work_types_raw
        }

        parent_map = _folder_parent_map(group_folders_raw)
        root_ids = set(FOLDER_TO_FIRM.keys())

        machine_name: dict[int, str] = {}
        machine_to_firm: dict[int, str] = {}
        for m in machines_raw:
            mid = m.get("id")
            if mid is None:
                continue
            mid = int(mid)
            name = m.get("name") or m.get("title") or m.get("model") or f"machine_{mid}"
            reg = (m.get("registration_number") or m.get("inventory_number") or "").strip()
            machine_name[mid] = f"{name} ({reg})" if reg else str(name)
            # Фолбэк-агрофирма по самой машине — используется, только если у
            # задания нет ни одного поля с выработкой (см. ниже).
            raw_folder = m.get("group_folder_id")
            if raw_folder is not None:
                root = _resolve_firm(int(raw_folder), parent_map, root_ids)
                if root is not None:
                    firm = FOLDER_TO_FIRM.get(root)
                    if firm:
                        machine_to_firm[mid] = firm

        local_fio_by_id = await fetch_local_fio_map()
        operators_map: dict[int, str] = {}
        for u in users_raw:
            uid = u.get("id")
            if uid is None:
                continue
            uid = int(uid)
            operators_map[uid] = (
                local_fio_by_id.get(uid)
                or u.get("username") or u.get("email")
                or FIO_NOT_FOUND
            )

        field_name: dict[int, str] = {
            int(f["id"]): (f.get("name") or f.get("title") or f"field_{f['id']}")
            for f in fields_raw if f.get("id") is not None
        }
        field_to_firm = _build_field_to_firm(fields_raw, field_groups_raw, group_folders_raw)

        crop_id_name: dict[int, str] = {
            c["id"]: (c.get("name") or c.get("title", "")) for c in crops_raw
        }
        field_to_crop: dict[int, str] = {}
        for hi in history_items_raw:
            fid = hi.get("field_id")
            cid = hi.get("crop_id")
            if fid and cid and hi.get("active"):
                cname = crop_id_name.get(cid, "")
                if cname:
                    field_to_crop[int(fid)] = cname

        # ── Активные задания по целевым видам работ, не скрытые машины ────────
        seen_ids: set[int] = set()
        eligible: list[dict] = []
        for t in tasks_raw:
            tid = t.get("id")
            if tid in seen_ids:
                continue
            seen_ids.add(tid)
            wt_id = t.get("work_type_id")
            mid = t.get("machine_id")
            if wt_id not in TARGET_WORK_TYPE_IDS:
                continue
            if not mid or int(mid) in hidden_ids:
                continue
            eligible.append(t)

        # Если у машины несколько активных заданий одновременно (нештатно) —
        # берём то, что началось позже (та же логика, что в
        # sync_assignments._fetch_active_assignments).
        _epoch = datetime.min.replace(tzinfo=TZ_MSK)
        latest_by_machine: dict[int, dict] = {}
        for t in eligible:
            mid = int(t["machine_id"])
            cur = latest_by_machine.get(mid)
            if cur is None or (_parse_dt(t.get("start_time")) or _epoch) > (_parse_dt(cur.get("start_time")) or _epoch):
                latest_by_machine[mid] = t

        logger.info(f"vehicle_check: активных заданий по целевым видам работ={len(latest_by_machine)}")

        if not latest_by_machine:
            update_cache([], now)
            return {"rows": 0, "checked": 0}

        # ── MTFMI (поле/культура/площадь) для отобранных заданий ──────────────
        task_ids = [t["id"] for t in latest_by_machine.values()]
        mtfmi_map = await _fetch_mtfmi_by_task_ids(
            session, task_ids, sem=asyncio.Semaphore(MTFMI_SEMAPHORE_LIMIT),
        )

        # ── GPS + сборка строк по каждой отобранной машине ────────────────────
        gps_sem = asyncio.Semaphore(GPS_SEMAPHORE_LIMIT)

        async def _check_machine(mid: int, task: dict) -> Optional[dict]:
            start_dt = _parse_dt(task.get("start_time"))
            end_dt = _parse_dt(task.get("end_time"))
            if start_dt is None:
                return None

            window_start = max(day_start, start_dt)
            gps_rows = await _fetch_machine_gps(session, gps_sem, mid, window_start, now)
            gap_start = _find_ongoing_gap(gps_rows, window_start, now)
            if gap_start is None:
                return None  # связь есть — под критерии раздела не попадает

            if (now - gap_start).total_seconds() < MIN_DISPLAY_DURATION_SECONDS:
                return None  # провал короче 3 часов — в разделе не показываем

            field_cov: dict[int, float] = defaultdict(float)
            for item in mtfmi_map.get(task["id"], []):
                fid_raw = item.get("field_id")
                if not fid_raw:
                    continue
                field_cov[int(fid_raw)] += float(item.get("covered_area") or 0)

            main_fid = max(field_cov, key=field_cov.get) if field_cov else None

            firm = field_to_firm.get(main_fid, "—") if main_fid is not None else "—"
            if firm == "—":
                firm = machine_to_firm.get(mid, "—")

            total_area = round(sum(field_cov.values()), 2)
            field_str = field_name.get(main_fid, "—") if main_fid is not None else "—"
            crop_str = field_to_crop.get(main_fid, "—") if main_fid is not None else "—"

            wt_id = task.get("work_type_id")
            work_type_label = WORK_TYPE_ID_TO_LABEL.get(wt_id, work_type_map.get(wt_id, "—"))

            driver_id = task.get("driver_id")
            fio = operators_map.get(int(driver_id), FIO_NOT_FOUND) if driver_id else "—"

            duration_seconds = int((now - gap_start).total_seconds())

            return {
                "firm":              firm,
                "crop":              crop_str,
                "field":             field_str,
                "fio":               fio,
                "machine":           machine_name.get(mid, f"machine_{mid}"),
                "machine_id":        mid,
                "work_type":         work_type_label,
                "area":              total_area,
                "start":             start_dt.strftime("%H:%M:%S %d.%m.%Y"),
                "end":               end_dt.strftime("%H:%M:%S %d.%m.%Y") if end_dt else "—",
                "no_signal_since":   gap_start.strftime("%H:%M:%S %d.%m.%Y"),
                "duration":          _fmt_duration(duration_seconds),
                "duration_seconds":  duration_seconds,
                "task_id":           task["id"],
                "link":              f"{CROPWISE_TASK_BASE}/{mid}/tasks/{task['id']}",
            }

        results = await asyncio.gather(*[
            _check_machine(mid, task) for mid, task in latest_by_machine.items()
        ])
        rows = [r for r in results if r is not None]
        rows.sort(key=lambda r: (r["firm"], -r["duration_seconds"]))

    update_cache(rows, now)
    logger.info(
        f"vehicle_check: {len(rows)} машин без связи из {len(latest_by_machine)} "
        f"проверенных активных заданий по целевым видам работ"
    )
    return {"rows": len(rows), "checked": len(latest_by_machine)}
