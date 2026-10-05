import asyncio
import functools

import redis as redis_sync
from celery.signals import worker_ready

from app.celery_app.celery_config import celery_app
from app.config import get_redis_url
from app.cropwise.sync import (
    backfill_downtime_agrofirms,
    fill_missing_task_ids,
    fill_missing_task_ids_history,
    force_refill_task_ids_for_period,
    sync_users_from_cropwise,
    sync_vehicles_from_cropwise,
    sync_vehicle_agrofirms,
)
from app.cropwise.sync_assignments import sync_user_assignments
from app.logging_config import get_logger

from app.forbidden_ops.router import update_cache as _update_forbidden_ops_cache
from app.forbidden_ops.router import get_forbidden_ops_config

logger = get_logger("cropwise.tasks")

# Один синхронный клиент на воркер-процесс — используется только для
# дешёвых SET NX / DELETE операций локов, отдельный пул не нужен.
_lock_redis = redis_sync.from_url(get_redis_url())


def _clear_stale_locks_on_worker_start(**_kwargs) -> None:
    """
    При старте воркера сбрасываем все lock:* из Redis.

    Если воркер был убит посреди задачи (OOM, перезапуск контейнера), блок
    finally в with_lock не выполняется и лок «залипает» до истечения TTL
    (у check12 — 65 минут). Всё это время кнопка «Обновить» получала
    {"skipped": True} и ничего не пересчитывалось.

    ВАЖНО: рассчитано на ОДИН celery-worker (как в docker-compose.yml).
    Если воркеров станет несколько, перезапуск одного сбросит локи задач,
    выполняющихся на других — тогда нужен лок с владельцем (hostname) или
    heartbeat-продлением TTL.
    """
    try:
        deleted = 0
        for key in _lock_redis.scan_iter("lock:*"):
            _lock_redis.delete(key)
            deleted += 1
        if deleted:
            logger.warning(f"worker_ready: сброшено «залипших» локов: {deleted}")
    except Exception as e:
        logger.error(f"worker_ready: не удалось сбросить локи: {e}")


worker_ready.connect(_clear_stale_locks_on_worker_start)


def with_lock(lock_key: str, ttl: int = 1800):
    """
    Distributed-лок через Redis: не даёт двум запускам одной и той же
    тяжёлой синхронизации выполняться параллельно — будь то двойной клик
    в UI, дублирующийся beat-тик после рестарта или ручной POST,
    совпавший по времени с расписанием.

    TTL — страховка: если воркер упадёт прямо во время выполнения
    (например, OOM kill) не успев снять лок, тот сам истечёт через
    ttl секунд и задача снова станет доступна для запуска. ttl стоит
    брать чуть больше реального времени работы задачи (см. task_soft_time_limit
    / time_limit конкретного таска).
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                acquired = _lock_redis.set(lock_key, "1", nx=True, ex=ttl)
            except Exception as e:
                # Redis недоступен — не блокируем задачу навсегда,
                # просто логируем и выполняем как обычно (fail-open).
                logger.error(f"{lock_key}: lock check failed ({e}), running without lock")
                acquired = True

            if not acquired:
                logger.warning(f"{lock_key}: уже выполняется — пропускаю запуск")
                return {"skipped": True, "reason": "already running"}
            try:
                return func(*args, **kwargs)
            finally:
                try:
                    _lock_redis.delete(lock_key)
                except Exception as e:
                    logger.error(f"{lock_key}: failed to release lock: {e}")
        return wrapper
    return decorator


# _run живёт в app.celery_app.async_runner — один persistent event loop
# на весь воркер-процесс, общий для ВСЕХ модулей с Celery-задачами
# (cropwise, downtime, driver_stops и т.д.). Раньше здесь была собственная
# реализация на asyncio.get_event_loop()/asyncio.run(), которая могла
# создавать loop, не совпадающий с loop'ом, под которым в пул
# app.database.engine попало предыдущее соединение — из-за этого
# pool_pre_ping ронял воркер при чек-ауте "осиротевшего" соединения.
from app.celery_app.async_runner import run as _run


@celery_app.task(name="sync_cropwise_users", queue="default")
def sync_users_task():
    result = _run(sync_users_from_cropwise())
    logger.info(f"sync_users_task done: {result}")
    return result


@celery_app.task(name="sync_cropwise_vehicles", queue="default")
def sync_vehicles_task():
    result = _run(sync_vehicles_from_cropwise())
    logger.info(f"sync_vehicles_task done: {result}")
    return result


@celery_app.task(name="fill_missing_task_ids", queue="default")
def fill_missing_task_ids_task():
    """Активные простои — каждые 5 минут."""
    result = _run(fill_missing_task_ids())
    logger.info(f"fill_missing_task_ids_task done: {result}")
    return result


@celery_app.task(
    name="backfill_downtime_agrofirms",
    queue="default",
    acks_late=True,
    reject_on_worker_lost=False,
)
@with_lock("lock:backfill_downtime_agrofirms", ttl=600)
def backfill_downtime_agrofirms_task():
    """
    Проставляет агрофирму простоям, закрытым с agrofirm = NULL — каждые 5 минут.

    Именно сюда вынесен поход в Cropwise, который раньше делал сам
    POST /api/downtime/end и из-за которого завершение простоя не укладывалось
    в 8-секундный таймаут service worker'а (см. backfill_downtime_agrofirms).
    """
    result = _run(backfill_downtime_agrofirms())
    logger.info(f"backfill_downtime_agrofirms_task done: {result}")
    return result


@celery_app.task(name="fill_missing_task_ids_history", queue="default")
def fill_missing_task_ids_history_task():
    """История простоев за последние 30 дней — каждый час."""
    result = _run(fill_missing_task_ids_history())
    logger.info(f"fill_missing_task_ids_history_task done: {result}")
    return result


@celery_app.task(
    name="force_refill_task_ids_for_period",
    queue="default",
    acks_late=True,
    reject_on_worker_lost=False,
)
@with_lock("lock:force_refill_task_ids_for_period", ttl=1800)
def force_refill_task_ids_for_period_task(date_from_str: str, date_to_str: str):
    """
    Принудительно перезаписывает machine_task_id для ВСЕХ простоев за период
    [date_from_str, date_to_str] (формат YYYY-MM-DD), независимо от того,
    есть у них уже task_id или нет.

    Запускается вручную через API (POST /api/downtime/force-refill-task-ids).
    """
    from datetime import date as _date
    date_from = _date.fromisoformat(date_from_str)
    date_to   = _date.fromisoformat(date_to_str)
    result = _run(force_refill_task_ids_for_period(date_from, date_to))
    logger.info(f"force_refill_task_ids_for_period_task done: {result}")
    return result


@celery_app.task(name="sync_user_assignments", queue="default")
def sync_user_assignments_task():
    """
    Обновляет текущее назначение каждого пользователя из Cropwise
    (машина + задача). Запускается каждые 10 минут.
    """
    result = _run(sync_user_assignments())
    logger.info(f"sync_user_assignments_task done: {result}")
    return result


@celery_app.task(
    name="sync_vehicle_agrofirms",
    queue="default",
    acks_late=True,
    reject_on_worker_lost=False,
)
@with_lock("lock:sync_vehicle_agrofirms", ttl=1800)
def sync_vehicle_agrofirms_task():
    """
    Обновляет поле agrofirm у техники по логике:
      field → field_group → group_folder → агрофирма.
    Запускается раз в сутки в 06:00 МСК.
    """
    result = _run(sync_vehicle_agrofirms())
    logger.info(f"sync_vehicle_agrofirms_task done: {result}")
    return result


"""
Дополнение к backend/app/cropwise/tasks.py
==========================================
Добавить эту задачу в конец файла.

Задача привязана к fill_missing_task_ids_history и запускается
каждый час в celery_config.py через тот же crontab(minute=0).
"""

@celery_app.task(
    name="sync_forbidden_ops",
    queue="default",
    acks_late=True,
    reject_on_worker_lost=False,
)
@with_lock("lock:sync_forbidden_ops", ttl=1800)
def sync_forbidden_ops_task():
    """
    Собирает данные о запрещённых операциях по культурам (логика os_new.py)
    и кладёт результат в in-memory кэш forbidden_ops роутера.
    Запускается каждый час вместе с fill_missing_task_ids_history.
    Использует только aiohttp через _fetch_all_pages из client.py.
    """
    result = _run(_sync_forbidden_ops_async())
    logger.info(f"sync_forbidden_ops done: {result}")
    return result


async def _sync_forbidden_ops_async() -> dict:
    import asyncio
    import time
    from collections import defaultdict
    from datetime import date, datetime, timedelta, timezone

    import aiohttp

    from app.cropwise.client import (
        CROPWISE_HEADERS,
        FIO_NOT_FOUND,
        _fetch_all_pages,
        _fetch_history_items_for_date_range,
        _fetch_machine_tasks_by_wt,
        _fetch_mtfmi_by_task_ids,
        fetch_local_fio_map,
    )
    from app.forbidden_ops.router import update_cache

    TZ_MSK         = timezone(timedelta(hours=3))
    DAY_START_HOUR = 6
    SEMAPHORE_LIMIT = 5

    FIRM_TO_FOLDER = {
        "АФ Чистопольская": 3, "АгроРаздолье": 4, "Аксу-Агро": 5,
        "НурлатАгро": 11, "Дрожжаное Рассвет Агро": 6,
        "Актай": 7, "Лениногорская Птицефабрика": 9,
    }
    FOLDER_TO_FIRM = {v: k for k, v in FIRM_TO_FOLDER.items()}
    SKIP_CROPS = {
            "Залежь, лес", "Залежь", "Введение в оборот 2026 год", "Введение в оборот 2024 год","Введение в оборот 2025 год",
            "Луговые травы", "Обмен", "В пользовании у фермера",
            "Введение в оборот 2027 год", "Введение в оборот", "Многолетние травы 2025", "Многолетние травы 2021", "Многолетние травы 2022", "Многолетние травы 2024",
            "Передано в аренду", "Дороги/лесополосы", "Овраги/неудобия", "crop_None", "Введение в оборот 2026 год"
    }
    def _norm(s) -> str:
        """Нормализация названия: лишние пробелы, регистр."""
        return " ".join(str(s or "").split()).casefold()

    SKIP_CROPS_NORM = {_norm(c) for c in SKIP_CROPS}
    FORBIDDEN_OPS_RAW = get_forbidden_ops_config()
    FORBIDDEN_OPS: dict[str, set[str]] = {
        _norm(crop): {_norm(op) for op in ops}
        for crop, ops in FORBIDDEN_OPS_RAW.items()
    }
    logger.info(f"_sync_forbidden_ops_async: loaded FORBIDDEN_OPS from Redis: {len(FORBIDDEN_OPS)} cultures")

    t_start = time.monotonic()
    REPORT_DATE = date.today()

    # ── двухсуточное окно: 06:00 вчера → 06:00 завтра ────────────────────────
    today_start = datetime(
        REPORT_DATE.year, REPORT_DATE.month, REPORT_DATE.day,
        DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK,
    )
    day_start = today_start - timedelta(days=1)   # 06:00 вчера
    cutoff    = today_start + timedelta(days=1)    # 06:00 завтра
    day_start_str = day_start.strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_str    = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    logger.info(f"_sync_forbidden_ops_async: window {day_start_str} – {cutoff_str}")

    # ── загрузка справочников, затем machine_tasks (batch work_type_id) ─────
    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS) as session:
        (
            fields_all,
            fgs_all,
            gfs_all,
            crops_raw,
            his_all,
            machines_all,
            users_raw,
            impl_raw,
            wt_raw,
        ) = await asyncio.gather(
            _fetch_all_pages(session, "fields"),
            _fetch_all_pages(session, "field_groups"),
            _fetch_all_pages(session, "group_folders"),
            _fetch_all_pages(session, "crops"),
            _fetch_history_items_for_date_range(session, REPORT_DATE, REPORT_DATE),
            _fetch_all_pages(session, "machines"),
            _fetch_all_pages(session, "users"),
            _fetch_all_pages(session, "implements"),
            _fetch_all_pages(session, "work_types"),
        )

        # ВАЖНО: тянем ВСЕ задачи окна, без фильтра work_type_id.
        # Раньше здесь фильтровали по видам работ из конфига запрещённых
        # операций (по умолчанию только «Прикатывание»), из-за чего операции
        # на «Овраги/неудобия», «Дороги/лесополосы», «Залежь, лес» и т.п.
        # (SKIP_CROPS — любая операция считается нарушением) вообще не
        # попадали в выборку, и таблица всегда была пустой.
        # Отбор по культуре и виду работ выполняется ниже, в Python.
        mt_params = {
            "end_time_gt_eq": day_start_str,
            "end_time_lt":    cutoff_str,
        }
        all_tasks = await _fetch_all_pages(session, "machine_tasks", mt_params)

    logger.info(
        f"Loaded: fields={len(fields_all)}, crops={len(crops_raw)}, "
        f"machines={len(machines_all)}, tasks={len(all_tasks)}"
    )

    # ── справочники ───────────────────────────────────────────────────────────

    # поля
    field_name: dict[int, str] = {}
    for f in fields_all:
        fid = f.get("id")
        if fid is not None:
            field_name[int(fid)] = f.get("name") or f.get("title") or f"field_{fid}"

    # агрофирмы по полям
    def _folder_parent_map(gfs):
        out = {}
        for gf in gfs:
            fid = gf.get("id")
            if fid is None:
                continue
            pid = (gf.get("parent_id") or gf.get("parent_group_folder_id")
                   or gf.get("group_folder_parent_id"))
            out[int(fid)] = int(pid) if pid is not None else None
        return out

    def _resolve_firm(folder_id, parent_map, root_ids):
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

    fgs_map    = {int(fg["id"]): fg for fg in fgs_all if fg.get("id") is not None}
    parent_map = _folder_parent_map(gfs_all)
    root_ids   = set(FOLDER_TO_FIRM.keys())

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
        root = _resolve_firm(rint, parent_map, root_ids)
        if root is not None:
            firm = FOLDER_TO_FIRM.get(root)
            if firm:
                field_to_firm[int(fid)] = firm

    # культуры
    crop_id_name = {c["id"]: (c.get("name") or c.get("title", "")) for c in crops_raw}
    field_to_crop: dict[int, str] = {}
    for hi in his_all:
        fid = hi.get("field_id")
        cid = hi.get("crop_id")
        if fid and cid and hi.get("active"):
            cname = crop_id_name.get(cid, "")
            if cname:
                field_to_crop[fid] = cname

    # машины
    machine_name: dict[int, str] = {}
    for m in machines_all:
        mid = m.get("id")
        if mid is not None:
            machine_name[int(mid)] = (
                m.get("name") or m.get("title") or m.get("model") or f"machine_{mid}"
            )

    # водители
    local_fio_by_id = await fetch_local_fio_map()
    operators_map: dict[int, str] = {}
    for u in users_raw:
        uid = u.get("id")
        if uid is not None:
            uid = int(uid)
            operators_map[uid] = (
                local_fio_by_id.get(uid)
                or u.get("username") or u.get("email")
                or FIO_NOT_FOUND
            )

    # орудия
    implements_map: dict[int, dict] = {
        impl["id"]: impl for impl in impl_raw if impl.get("id") is not None
    }

    # виды работ
    work_type_map: dict[int, str] = {
        w["id"]: (w.get("name") or w.get("title") or f"wt_{w['id']}") for w in wt_raw
    }

    # ── дедупликация задач ────────────────────────────────────────────────────
    seen_ids: set[int] = set()
    unique_tasks = []
    for t in all_tasks:
        tid = t.get("id")
        if tid not in seen_ids:
            seen_ids.add(tid)
            unique_tasks.append(t)

    logger.info(f"Unique tasks: {len(unique_tasks)}")

    def _stats() -> dict:
        """Показатели прогона для индикатора «здоровья» на вкладке."""
        return {
            "tasks":        len(unique_tasks),
            "fields":       len(fields_all),
            "duration_sec": round(time.monotonic() - t_start, 1),
        }

    if not unique_tasks:
        update_cache([], REPORT_DATE, _stats())
        return {"rows": 0, "tasks": 0}

    # ── MTFMI: batch по machine_task_id ─────────────────────────────────────
    task_ids = [t["id"] for t in unique_tasks]
    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS) as session:
        mtfmi_map = await _fetch_mtfmi_by_task_ids(session, task_ids)

    # ── parse_dt helper ───────────────────────────────────────────────────────
    def parse_dt(s):
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

    # ── сборка строк отчёта ───────────────────────────────────────────────────
    rows: list[dict] = []
    stat_skip_crop = stat_no_crop = stat_forbidden = 0

    for task in unique_tasks:
        tid     = task["id"]
        mid     = task.get("machine_id")
        wt_id   = task.get("work_type_id")
        wt_name = work_type_map.get(wt_id, "—") if wt_id else "—"
        mach    = machine_name.get(int(mid), f"machine_{mid}") if mid else "—"

        try:
            op_id = int(task["driver_id"]) if task.get("driver_id") is not None else None
        except (TypeError, ValueError):
            op_id = None
        operator_name = operators_map.get(op_id, "—") if op_id else "—"

        impl_id = task.get("implement_id")
        impl_rec = implements_map.get(impl_id) if impl_id else None
        implement_name = "—"
        if impl_rec:
            implement_name = str(
                impl_rec.get("custom_name") or impl_rec.get("name") or "—"
            ).strip() or "—"

        field_cov: dict[int, float] = defaultdict(float)
        for item in mtfmi_map.get(tid, []):
            fid_raw = item.get("field_id")
            if not fid_raw:
                continue
            fid = int(fid_raw)
            hourly = item.get("covered_area_hourly") or []
            cov = sum(
                float(e[1] or 0) for e in hourly
                if parse_dt(e[0]) and parse_dt(e[0]) < cutoff
            )
            if not hourly:
                cov = float(item.get("covered_area") or 0)
            if cov < 0.5:
                continue
            field_cov[fid] += cov

        if not field_cov:
            direct_fid = task.get("field_id")
            if direct_fid:
                field_cov[int(direct_fid)] = float(
                    task.get("completed_area") or task.get("covered_area") or 0
                )
        for fid, area in field_cov.items():
            crop = field_to_crop.get(fid, "—")

            crop_n = _norm(crop)
            if crop_n in SKIP_CROPS_NORM:
                stat_skip_crop += 1  # овраги/дороги/залежь и т.п. — любая операция
            elif crop == "—":
                stat_no_crop += 1    # поле есть в MTFMI, но нет активной культуры — тоже включаем
            else:
                forbidden = FORBIDDEN_OPS.get(crop_n)
                if forbidden is None or _norm(wt_name) not in forbidden:
                    continue
                stat_forbidden += 1

            rows.append({
                "task_id":   tid,
                "machine_id": mid,
                "firm":      field_to_firm.get(fid, "—"),
                "crop":      crop,
                "field":     field_name.get(fid, f"field_{fid}"),
                "operator":  operator_name,
                "machine":   mach,
                "implement": implement_name,
                "work_type": wt_name,
                "area":      round(area, 2),
            })

    logger.info(
        f"_sync_forbidden_ops_async: {len(rows)} rows "
        f"(skip_crops={stat_skip_crop}, no_crop={stat_no_crop}, "
        f"forbidden_by_config={stat_forbidden}), updating cache"
    )
    update_cache(rows, REPORT_DATE, _stats())
    return {"rows": len(rows), "tasks": len(unique_tasks)}


@celery_app.task(name="check_overdue_downtimes", queue="default")
def check_overdue_downtimes_task():
    """
    Каждый час в начале часа проверяет все активные простои и принудительно
    закрывает те, у которых плановое время окончания задачи уже прошло.

    Логика:
      1. Получаем все активные простои с machine_task_id (привязаны к задаче).
      2. Из Cropwise читаем задачи, которые закончились за последние ~2 часа
         (окно с запасом, чтобы не упустить edge-cases).
      3. Для каждого простоя находим его задачу и проверяем: если task_end_time
         уже прошёл (now_msk > task_end_msk) — закрываем простой ровно в
         task_end_time (аналогично snap-логике в auto_end_downtime_on_task_change).
      4. Простои без machine_task_id (открытые вручную) не трогаем.
    """
    result = _run(_check_overdue_downtimes_async())
    logger.info(f"check_overdue_downtimes_task done: {result}")
    return result


async def _check_overdue_downtimes_async() -> dict:
    from datetime import timezone, timedelta, datetime, time as dtime
    import aiohttp

    from app.cropwise.client import CROPWISE_HEADERS, _fetch_all_pages
    from app.downtime.dao import ActiveDowntimeDAO, DowntimeDAO
    from app.downtime.models import ActiveDowntime
    from sqlalchemy.orm import joinedload

    TZ_MSK = timezone(timedelta(hours=3))
    result = {"checked": 0, "closed": 0, "skipped": 0, "errors": []}

    # ── 1. Все активные простои, привязанные к задаче ─────────────────────────
    all_actives = await ActiveDowntimeDAO.find_all_with_options(
        ActiveDowntime.machine_task_id.isnot(None),
        options=[joinedload(ActiveDowntime.vehicle)],
    )
    if not all_actives:
        logger.info("[OVERDUE] No active downtimes with machine_task_id, skipping")
        return result

    result["checked"] = len(all_actives)
    task_ids_needed: set[int] = {a.machine_task_id for a in all_actives}
    logger.info(f"[OVERDUE] Found {len(all_actives)} active downtimes, task_ids={task_ids_needed}")

    # ── 2. Загружаем задачи из Cropwise за окно: от 24 часов назад до сейчас ──
    # Берём окно с запасом: задача могла закончиться несколько часов назад,
    # а простой так и остался открытым (например, воркер не запускался).
    now_msk = datetime.now(TZ_MSK)
    window_start = (now_msk - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
    window_end   = now_msk.strftime("%Y-%m-%dT%H:%M:%S")

    try:
        async with aiohttp.ClientSession(headers=CROPWISE_HEADERS) as session:
            machine_tasks = await _fetch_all_pages(
                session,
                "machine_tasks",
                {
                    "end_time_gt_eq": window_start,
                    "end_time_lt":    window_end,
                },
            )
    except Exception as e:
        logger.error(f"[OVERDUE] Failed to fetch machine_tasks from Cropwise: {e}", exc_info=True)
        result["errors"].append(str(e))
        return result

    # Индекс задач по id: task_id → end_time (naive МСК)
    task_end_by_id: dict[int, datetime] = {}
    for task in machine_tasks:
        tid = task.get("id")
        end_str = task.get("end_time") or ""
        if not tid or not end_str:
            continue
        try:
            parsed = datetime.fromisoformat(str(end_str).replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                task_end_by_id[tid] = parsed.astimezone(TZ_MSK).replace(tzinfo=None)
            else:
                task_end_by_id[tid] = parsed  # считаем уже МСК
        except Exception:
            continue

    now_msk_naive = now_msk.replace(tzinfo=None)
    logger.info(f"[OVERDUE] Loaded {len(task_end_by_id)} tasks from Cropwise for window check")

    # ── 3. Проверяем каждый активный простой ─────────────────────────────────
    for active in all_actives:
        task_id = active.machine_task_id
        task_end_msk = task_end_by_id.get(task_id)

        if task_end_msk is None:
            # Задача не попала в окно — либо ещё активна (end_time > now),
            # либо слишком старая. Не трогаем.
            logger.debug(
                f"[OVERDUE] active_id={active.id} user={active.user_id}: "
                f"task_id={task_id} not in window, skipping"
            )
            result["skipped"] += 1
            continue

        if now_msk_naive <= task_end_msk:
            # Задача ещё не закончилась — всё в порядке.
            result["skipped"] += 1
            continue

        # Задача уже закончилась. Закрываем простой ровно в task_end_time.
        # Секунды фиксируем в :59 — аналогично snap-логике в auto_end_downtime_on_task_change.
        end_date = task_end_msk.date()
        end_time = task_end_msk.time().replace(second=59, microsecond=0)

        logger.info(
            f"[OVERDUE] Closing overdue downtime: active_id={active.id}, "
            f"user_id={active.user_id}, fio={active.fio!r}, "
            f"vehicle={active.license_plate!r}, task_id={task_id}, "
            f"task_end={task_end_msk}, now={now_msk_naive}"
        )

        try:
            closed, discarded = await DowntimeDAO.end_downtime_by_active_id(
                active_id=active.id,
                end_date=end_date,
                end_time=end_time,
                closed_by_user_id=None,
                closed_by_fio="celery:check_overdue",
                closed_by_ip=None,
            )
            if closed:
                result["closed"] += 1
                if discarded:
                    logger.info(
                        f"[OVERDUE] Discarded (< 5 min): active_id={active.id}"
                    )
                else:
                    logger.info(
                        f"[OVERDUE] Closed successfully: active_id={active.id}"
                    )
            else:
                logger.warning(
                    f"[OVERDUE] end_downtime_by_active_id returned False "
                    f"for active_id={active.id} (already deleted?)"
                )
                result["skipped"] += 1
        except Exception as e:
            logger.error(
                f"[OVERDUE] Failed to close active_id={active.id}: {e}",
                exc_info=True,
            )
            result["errors"].append(f"active_id={active.id}: {e}")

    logger.info(
        f"[OVERDUE] Done: checked={result['checked']}, closed={result['closed']}, "
        f"skipped={result['skipped']}, errors={len(result['errors'])}"
    )
    return result


@celery_app.task(
    name="violations_refresh_check1",
    queue="default",
    bind=True,
    acks_late=True,
    reject_on_worker_lost=False,
)
@with_lock("lock:violations_refresh_check1", ttl=2400)
def violations_refresh_check1_task(self):
    """Только check1 («Нет агроопераций») — отдельный процесс и lock."""
    from app.cropwise.violations_sync import refresh_violations

    progress_lines: list[str] = []

    def on_progress(message: str) -> None:
        progress_lines.append(message)
        self.update_state(
            state="PROGRESS",
            meta={"message": message, "lines": list(progress_lines)},
        )

    on_progress("check1: старт обновления")
    result = _run(refresh_violations(["check1"], progress=on_progress))
    logger.info(f"violations_refresh_check1_task done: {result}")
    return result


@celery_app.task(
    name="violations_refresh",
    queue="default",
    acks_late=True,
    reject_on_worker_lost=False,
)
@with_lock("lock:violations_refresh_main", ttl=1800)
def violations_refresh_task(checks: list[str] | None = None):
    """
    Обновляет кэш нарушений (все checks КРОМЕ check1 по умолчанию).
    checks=None → NON_CHECK1_VIOLATION_CHECKS (часовой beat).
    check1 — отдельная задача violations_refresh_check1.
    """
    from app.cropwise.violations_sync import (
        NON_CHECK1_VIOLATION_CHECKS,
        refresh_violations,
    )
    effective = list(checks) if checks else list(NON_CHECK1_VIOLATION_CHECKS)
    if "check1" in effective:
        effective = [c for c in effective if c != "check1"]
    if not effective:
        logger.info("violations_refresh_task: nothing to refresh (check1-only request)")
        return {"skipped": True, "reason": "check1 uses violations_refresh_check1"}
    result = _run(refresh_violations(effective))
    logger.info(f"violations_refresh_task done: {result}")
    return result


@celery_app.task(
    name="check12_refresh",
    queue="default",
    time_limit=3600,
    acks_late=True,
    reject_on_worker_lost=False,
)
@with_lock("lock:check12_refresh", ttl=3900)
def check12_refresh_task():
    """
    Тяжёлая проверка «Несоответствие сезона задачи и агрооперации».
    Запускается только вручную (кнопка в UI). Результат кладётся в Redis.
    time_limit=3600 — запас с избытком: задачи и mapping_items грузятся
    параллельными окнами по created_at (раньше последовательно, 20-40 минут).
    """
    import json
    from datetime import datetime, timezone, timedelta

    TZ_MSK = timezone(timedelta(hours=3))

    try:
        from app.cropwise.check12_season_mismatch import (
            load_reference_data_async,
            collect_data_async,
        )
        import redis as redis_lib
        from app.config import get_redis_url

        logger.info("check12_refresh_task: started")

        async def _run_check12():
            field_to_firm, field_slug, field_name, work_type_map = (
                await load_reference_data_async()
            )
            rows = await collect_data_async(
                field_to_firm, field_slug, field_name, work_type_map,
            )
            return rows

        rows = _run(_run_check12())

        # Сериализуем даты/datetime в строки
        def _serialize(obj):
            if isinstance(obj, (datetime,)):
                return obj.isoformat()
            return str(obj)

        now = datetime.now(tz=TZ_MSK)
        data = {
            "generated_at": now.strftime("%d.%m.%Y %H:%M"),
            "total": len(rows),
            "rows": rows,
        }

        _r = redis_lib.from_url(get_redis_url(), decode_responses=True)
        _r.set("check12_cache", json.dumps(data, ensure_ascii=False, default=_serialize))

        logger.info(f"check12_refresh_task: done, {len(rows)} violations")
        return {"total": len(rows), "generated_at": data["generated_at"]}

    except Exception as e:
        logger.error(f"check12_refresh_task: FAILED: {e}", exc_info=True)
        raise


@celery_app.task(
    name="vehicle_check_refresh",
    queue="default",
    acks_late=True,
    reject_on_worker_lost=False,
)
@with_lock("lock:vehicle_check_refresh", ttl=600)
def vehicle_check_refresh_task():
    """
    Вкладка «Проверка техники»: техника с активным агрозаданием
    (Опрыскивание/Уборка/Культивация/Посев), у которой прямо сейчас
    зафиксирована потеря связи с GPS-терминалом. Запускается каждые
    15 минут (см. celery_config.py).
    """
    from app.cropwise.vehicle_check_sync import refresh_vehicle_check
    result = _run(refresh_vehicle_check())
    logger.info(f"vehicle_check_refresh_task done: {result}")
    return result


@celery_app.task(
    name="speed_violations_refresh",
    queue="default",
    acks_late=True,
    reject_on_worker_lost=False,
)
@with_lock("lock:speed_violations_refresh", ttl=1800)   # расчёт стал дольше (уборка вне справочника)
def speed_violations_refresh_task():
    """
    Вкладка «Превышение скорости»: превышение скоростного режима на уборке
    (avg_speed > 40 км/ч в часы реальной полевой работы). Запускается
    каждые 15 минут (см. celery_config.py).
    """
    from app.cropwise.speed_violations_sync import refresh_speed_violations
    result = _run(refresh_speed_violations())
    logger.info(f"speed_violations_refresh_task done: {result}")
    return result


@celery_app.task(
    name="missing_task_refresh",
    queue="default",
    acks_late=True,
    reject_on_worker_lost=False,
)
@with_lock("lock:missing_task_refresh", ttl=1800)
def missing_task_refresh_task():
    """
    Вкладка «Возможное отсутствие задачи»: машина была на поле
    (machine_object_intersections), а задания или агрооперации нет.
    Запускается раз в час (см. celery_config.py).
    """
    from app.cropwise.missing_task_sync import refresh_missing_task
    # Предохранитель: меньше TTL лока (1800 с). При зависании корутина будет отменена,
    # сработает finally в with_lock и лок снимется, а не провисит весь TTL.
    result = _run(asyncio.wait_for(refresh_missing_task(), timeout=1500))
    logger.info(f"missing_task_refresh_task done: {result}")
    return result


@celery_app.task(
    name="elevator_transfer_refresh",
    queue="default",
    acks_late=True,
    reject_on_worker_lost=False,
)
@with_lock("lock:elevator_transfer_refresh", ttl=1800)
def elevator_transfer_refresh_task():
    """
    Вкладка «Перемещение между элеваторами»: подозрительно быстрые
    переезды транспорта между разными элеваторами (по GPS, координаты —
    из elevator_objects_with_coords.csv). Запускается каждые 30 минут
    (см. celery_config.py).
    """
    from app.cropwise.elevator_transfer_sync import refresh_elevator_transfer
    result = _run(refresh_elevator_transfer())
    logger.info(f"elevator_transfer_refresh_task done: {result}")
    return result


@celery_app.task(
    name="zsk_downtime_refresh",
    queue="default",
    acks_late=True,
    reject_on_worker_lost=False,
    time_limit=25 * 60,
    soft_time_limit=24 * 60,
)
@with_lock("lock:zsk_downtime_refresh", ttl=1800)
def zsk_downtime_refresh_task():
    """
    Вкладка «Простои ЗСК»: простои по датчику «Работа»/«Вентилятор» на
    объектах FortMonitor (smsr.online). Запускается каждые 30 минут
    (см. celery_config.py). Диапазон дат — настраиваемый (см.
    zsk_downtime_router.get_zsk_date_range); окно расчёта каждый раз
    пересчитывается заново, а не продолжается от предыдущего запуска.

    Синхронный HTTP (httpx.Client, ~31 объект с паузой между запросами) —
    выполняется в самом теле задачи, отдельный event loop не нужен.
    """
    from datetime import datetime, timezone, timedelta
    from app.cropwise.zsk_downtime_sync import collect_downtime_rows
    from app.cropwise.zsk_downtime_router import update_cache, get_zsk_date_range

    TZ_MSK = timezone(timedelta(hours=3))

    date_from, date_to = get_zsk_date_range()
    rows, meta = collect_downtime_rows(date_from, date_to)
    update_cache(rows, datetime.now(tz=TZ_MSK), meta)

    result = {"rows": len(rows), "date_from": str(date_from), "date_to": str(date_to)}
    logger.info(f"zsk_downtime_refresh_task done: {result}")
    return result


@celery_app.task(
    name="gps_duplicate_refresh",
    queue="default",
    acks_late=True,
    reject_on_worker_lost=False,
)
@with_lock("lock:gps_duplicate_refresh", ttl=600)
def gps_duplicate_refresh_task():
    """
    Вкладка «GPS трекер на нескольких машинах»: физический GPS-логгер
    числится активным сразу у нескольких машин. Запускается ТОЛЬКО
    вручную (кнопка «Обновить» в UI) — нет записи в beat_schedule
    (см. celery_config.py и ТЗ).
    """
    from app.cropwise.gps_duplicate_sync import refresh_gps_duplicate
    result = _run(refresh_gps_duplicate())
    logger.info(f"gps_duplicate_refresh_task done: {result}")
    return result


@celery_app.task(
    name="duplicates_refresh",
    queue="default",
    acks_late=True,
    reject_on_worker_lost=False,
)
@with_lock("lock:duplicates_refresh", ttl=600)
def duplicates_refresh_task():
    """
    Вкладка «Дубли»: дубли ФИО пользователей и дубли машин по госномеру
    (точные и похожие — опечатки). Запускается ТОЛЬКО вручную (кнопка
    «Обновить» в UI) — нет записи в beat_schedule (тот же принцип, что и
    gps_duplicate_refresh_task выше).
    """
    from app.cropwise.duplicates_sync import refresh_duplicates
    result = _run(refresh_duplicates())
    logger.info(f"duplicates_refresh_task done: {result}")
    return result
