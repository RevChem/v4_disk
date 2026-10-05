"""
cropwise/sync.py
~~~~~~~~~~~~~~~~
Синхронизация пользователей из Cropwise API в локальную БД.
User.id = Cropwise id (не autoincrement).
"""

from datetime import datetime, timedelta

from app.cropwise.client import fetch_cropwise_users_raw, fetch_cropwise_machines_raw, fetch_machine_agrofirm_map
from app.date.dao import SyncLogDAO
from app.users.models import detect_role

# Роли, назначаемые вручную — Cropwise не должен их перезаписывать
PROTECTED_ROLES = {"admin", "developer", "situation_center", "dispatcher"}
from app.auth.dao import UserDAO as AuthUserDAO
from app.venchies.dao import VehicleDAO
from app.logging_config import get_logger

logger = get_logger("cropwise.sync")

DEFAULT_CROPWISE_PASSWORD = "1"


# ── SyncLog helpers ───────────────────────────────────────────────────────────

async def _get_last_synced(entity: str) -> datetime | None:
    log = await SyncLogDAO.find_one_or_none(entity=entity)
    return log.last_synced_at if log else None


async def _set_last_synced(entity: str) -> None:
    now = datetime.now()
    existing = await SyncLogDAO.find_one_or_none(entity=entity)
    if existing:
        await SyncLogDAO.update({"entity": entity}, last_synced_at=now)
    else:
        await SyncLogDAO.add(entity=entity, last_synced_at=now)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _phone_for_user(cw_user: dict) -> str | None:
    raw = (cw_user.get("mobile_phone") or "").strip()
    if not raw or raw == "—" or len(raw) < 10:
        return None
    if raw.startswith("+7"):
        raw = "8" + raw[2:]
    return raw


def _fio_for_user(cw_user: dict) -> str:
    name = (cw_user.get("username") or cw_user.get("email") or "").strip()
    return name or f"User {cw_user['id']}"


# ── Sync functions ────────────────────────────────────────────────────────────

async def sync_users_from_cropwise() -> dict:
    result = {"created": 0, "updated": 0, "skipped": 0, "errors": []}

    last_synced = await _get_last_synced("users")
    logger.info(f"sync_users: last_synced={last_synced}")

    try:
        users = await fetch_cropwise_users_raw(updated_after=last_synced)
    except Exception as e:
        logger.error(f"Failed to fetch Cropwise users: {e}", exc_info=True)
        result["errors"].append(str(e))
        return result

    for cw in users:
        cropwise_id = cw.get("id")
        if cropwise_id is None:
            logger.warning("Cropwise user without id, skipping")
            continue

        phone    = _phone_for_user(cw)
        fio      = _fio_for_user(cw)
        role     = detect_role(cw)
        position = (cw.get("position") or "").strip() or None

        try:
            existing = await AuthUserDAO.get_user_by_id(cropwise_id)

            if existing:
                updates = {}
                if existing.fio != fio:
                    updates["fio"] = fio
                # Не перезаписываем роли, назначенные вручную
                if existing.role not in PROTECTED_ROLES and existing.role != role:
                    updates["role"] = role
                if existing.position != position:
                    updates["position"] = position
                if phone and existing.phone != phone:
                    updates["phone"] = phone

                if updates:
                    logger.info(f"User id={cropwise_id} updates: {updates}")
                    await AuthUserDAO.update_user(existing.id, **updates)
                    result["updated"] += 1
                else:
                    logger.info(f"User id={cropwise_id} ({fio}) skipped: no changes")
                    result["skipped"] += 1

            else:
                created = await AuthUserDAO.create_user(
                    id=cropwise_id,
                    phone=phone,
                    password=DEFAULT_CROPWISE_PASSWORD,
                    fio=fio,
                    role=role,
                )
                if created:
                    if position:
                        await AuthUserDAO.update_user(created.id, position=position)
                    result["created"] += 1
                else:
                    result["skipped"] += 1

        except Exception as e:
            logger.warning(f"Cropwise user id={cropwise_id}: {e}")
            result["errors"].append(f"id={cropwise_id}: {e}")

    await _set_last_synced("users")

    logger.info(
        f"sync_users: created={result['created']}, updated={result['updated']}, "
        f"skipped={result['skipped']}, errors={len(result['errors'])}"
    )
    return result


async def sync_vehicles_from_cropwise() -> dict:
    result = {"created": 0, "updated": 0, "skipped": 0, "errors": []}

    last_synced = await _get_last_synced("machines")
    logger.info(f"sync_vehicles: last_synced={last_synced}")

    try:
        machines = await fetch_cropwise_machines_raw(updated_after=last_synced)
    except Exception as e:
        logger.error(f"Failed to fetch Cropwise machines: {e}", exc_info=True)
        result["errors"].append(str(e))
        return result

    for m in machines:
        mid = m.get("id")
        if mid is None:
            continue

        data = {
            "name":                (m.get("name") or "").strip() or None,
            "year":                m.get("year"),
            "registration_number": (m.get("registration_number") or "").strip() or None,
            "inventory_number":    (m.get("inventory_number") or "").strip() or None,
            "machine_type":        (m.get("machine_type") or "").strip() or None,
            "machine_subtype":     (m.get("machine_subtype") or "").strip() or None,
            "legal_company":       (m.get("legal_company") or "").strip() or None,
        }

        try:
            existing = await VehicleDAO.find_one_or_none_by_id(mid)
            if existing:
                updates = {k: v for k, v in data.items() if v is not None}
                if updates:
                    await VehicleDAO.update({"id": mid}, **updates)
                    result["updated"] += 1
                else:
                    result["skipped"] += 1
            else:
                await VehicleDAO.add(id=mid, is_active=True, **data)
                logger.info(f"Machine data: {data}")
                result["created"] += 1
            # ← здесь НЕТ второго else
        except Exception as e:
            logger.warning(f"Cropwise machine id={mid}: {e}")
            result["errors"].append(f"id={mid}: {e}")

    await _set_last_synced("machines")

    logger.info(
        f"sync_vehicles: created={result['created']}, updated={result['updated']}, "
        f"skipped={result['skipped']}, errors={len(result['errors'])}"
    )
    return result


# ── Синхронизация агрофирмы по полям (раз в сутки) ───────────────────────────

# backend/app/cropwise/sync.py

async def sync_vehicle_agrofirms() -> dict:
    """
    1. Обновляет vehicles.agrofirm из Cropwise по цепочке поле → папка → агрофирма.
       Для машин, у которых нет привязки к полям, используется fallback на legal_company.
    2. Backfill: заполняет downtime.agrofirm и active_downtime.agrofirm
       для всех записей где agrofirm IS NULL — берёт значение из vehicles,
       никакого Cropwise API не нужно.
    """
    result = {"updated": 0, "skipped": 0, "fallback_legal": 0, "backfilled_history": 0, "backfilled_active": 0, "errors": []}

    # ── 1. Обновляем vehicles.agrofirm через поля ─────────────────────────────
    try:
        agrofirm_map = await fetch_machine_agrofirm_map()
    except Exception as e:
        logger.error(f"sync_vehicle_agrofirms: failed to fetch map: {e}", exc_info=True)
        result["errors"].append(str(e))
        return result

    # Один bulk UPDATE вместо тысяч отдельных запросов на каждую машину
    try:
        from app.database import async_session_maker
        from sqlalchemy import text

        mids      = list(agrofirm_map.keys())
        agrofirms = [agrofirm_map[m] for m in mids]
        async with async_session_maker() as session:
            res = await session.execute(
                text("""
                    UPDATE vehicles v
                    SET agrofirm = d.agrofirm
                    FROM (
                        SELECT unnest(:mids::int[]) AS id,
                               unnest(:agrofirms::text[]) AS agrofirm
                    ) d
                    WHERE v.id = d.id
                      AND (v.agrofirm IS DISTINCT FROM d.agrofirm)
                """),
                {"mids": mids, "agrofirms": agrofirms},
            )
            await session.commit()
        result["updated"] = res.rowcount
        result["skipped"] = len(agrofirm_map) - res.rowcount
        logger.info(f"sync_vehicle_agrofirms: bulk updated {res.rowcount} of {len(agrofirm_map)} vehicles")
    except Exception as e:
        logger.error(f"sync_vehicle_agrofirms: bulk update failed: {e}", exc_info=True)
        result["errors"].append(str(e))

    # ── 1б. Fallback: машины без агрофирмы → legal_company ───────────────────
    #    Если после шага 1 у машины agrofirm IS NULL, но есть legal_company —
    #    используем legal_company как агрофирму.
    try:
        from app.database import async_session_maker
        from sqlalchemy import text

        async with async_session_maker() as session:
            res = await session.execute(text("""
                UPDATE vehicles
                SET agrofirm = legal_company
                WHERE agrofirm IS NULL
                  AND legal_company IS NOT NULL
                  AND legal_company <> ''
            """))
            result["fallback_legal"] = res.rowcount
            await session.commit()
        logger.info(f"sync_vehicle_agrofirms: fallback legal_company applied to {res.rowcount} vehicles")
    except Exception as e:
        logger.error(f"sync_vehicle_agrofirms: fallback legal_company failed: {e}", exc_info=True)
        result["errors"].append(f"fallback_legal: {e}")

    # ── 2. Backfill downtime.agrofirm ─────────────────────────────────────────
    backfill = await backfill_downtime_agrofirms()
    result["backfilled_history"] = backfill["backfilled_history"]
    result["backfilled_active"] = backfill["backfilled_active"]
    result["errors"].extend(backfill["errors"])

    logger.info(
        f"sync_vehicle_agrofirms: updated={result['updated']}, "
        f"skipped={result['skipped']}, errors={len(result['errors'])}"
    )
    return result


async def backfill_downtime_agrofirms() -> dict:
    """
    Дозаполняет agrofirm у простоев, где она ещё не определена (NULL).

    Приоритет источника — по задаче, шедшей в момент конкретного простоя
    (machine_task_id → MTFMI → field → agrofirm), а НЕ по текущей сезонной
    агрофирме машины (vehicles.agrofirm): та могла смениться между моментом
    простоя и моментом backfill'а. vehicles.agrofirm — только fallback для
    записей без machine_task_id (простой без привязки к задаче — резолвить
    по полю нечем).

    ЭТО ОСНОВНОЙ путь определения агрофирмы для завершённых простоев.
    Раньше её резолвил сам обработчик POST /api/downtime/end, синхронно
    дёргая Cropwise прямо в запросе. Завершение простоя из-за этого занимало
    до 36 секунд при том, что service worker обрывает любой запрос к API
    через 8 секунд: страница считала это потерей сети и клала «Стоп» в
    офлайн-очередь, сервер тем временем всё равно досчитывал запрос и
    закрывал простой, а все повторные попытки получали «активного простоя
    нет». Теперь /end пишет NULL и отвечает мгновенно, а агрофирму
    подставляет эта задача (каждые 5 минут) либо фоновый вызов
    /admin-details/enrich при открытии панели «Простои».
    """
    from app.database import async_session_maker
    from sqlalchemy import text
    from app.cropwise.client import resolve_agrofirms_for_tasks

    result: dict = {"backfilled_history": 0, "backfilled_active": 0, "errors": []}
    try:
        async with async_session_maker() as session:
            # Записи без agrofirm, у которых ЕСТЬ machine_task_id — резолвим по задаче
            task_rows_hist = (await session.execute(text("""
                SELECT id, machine_task_id FROM downtime
                WHERE agrofirm IS NULL AND machine_task_id IS NOT NULL
            """))).all()
            task_rows_active = (await session.execute(text("""
                SELECT id, machine_task_id FROM active_downtime
                WHERE agrofirm IS NULL AND machine_task_id IS NOT NULL
            """))).all()

            all_task_ids = list({r.machine_task_id for r in task_rows_hist + task_rows_active})
            task_to_agrofirm = await resolve_agrofirms_for_tasks(all_task_ids) if all_task_ids else {}

            backfilled_history = 0
            for r in task_rows_hist:
                ag = task_to_agrofirm.get(r.machine_task_id)
                if ag:
                    await session.execute(
                        text("UPDATE downtime SET agrofirm = :ag WHERE id = :id"),
                        {"ag": ag, "id": r.id},
                    )
                    backfilled_history += 1

            backfilled_active = 0
            for r in task_rows_active:
                ag = task_to_agrofirm.get(r.machine_task_id)
                if ag:
                    await session.execute(
                        text("UPDATE active_downtime SET agrofirm = :ag WHERE id = :id"),
                        {"ag": ag, "id": r.id},
                    )
                    backfilled_active += 1

            # Fallback на vehicles.agrofirm — только для записей БЕЗ machine_task_id
            # (резолвить по полю нечем; та же сезонная агрофирма, что и раньше).
            res = await session.execute(text("""
                UPDATE downtime d
                SET agrofirm = v.agrofirm
                FROM vehicles v
                WHERE d.vehicle_id = v.id
                  AND d.agrofirm IS NULL
                  AND d.machine_task_id IS NULL
                  AND v.agrofirm IS NOT NULL
            """))
            backfilled_history += res.rowcount

            res = await session.execute(text("""
                UPDATE active_downtime d
                SET agrofirm = v.agrofirm
                FROM vehicles v
                WHERE d.vehicle_id = v.id
                  AND d.agrofirm IS NULL
                  AND d.machine_task_id IS NULL
                  AND v.agrofirm IS NOT NULL
            """))
            backfilled_active += res.rowcount

            result["backfilled_history"] = backfilled_history
            result["backfilled_active"] = backfilled_active

            await session.commit()

        logger.info(
            f"backfill_downtime_agrofirms: "
            f"history={result['backfilled_history']}, "
            f"active={result['backfilled_active']} "
            f"(task-based: {len(task_to_agrofirm)} of {len(all_task_ids)} tasks resolved)"
        )
    except Exception as e:
        logger.error(f"backfill_downtime_agrofirms failed: {e}", exc_info=True)
        result["errors"].append(f"backfill: {e}")

    return result


# ── Дозаполнение task_id для активных простоев (каждые 5 минут) ──────────────

async def fill_missing_task_ids() -> dict:
    """
    Заполняет machine_task_id для активных простоев (ActiveDowntime) где он NULL.
    Запускается каждые 5 минут.
    Активный простой ещё не завершён — задача Cropwise скорее всего ещё активна,
    поэтому точность высокая.
    """
    from app.downtime.models import ActiveDowntime
    from app.downtime.dao import ActiveDowntimeDAO
    from app.cropwise.client import fetch_active_task_id_for_vehicle

    result = {"filled": 0, "skipped": 0, "errors": []}

    actives = await ActiveDowntimeDAO.find_all_with_options(
        ActiveDowntime.machine_task_id == None  # noqa: E711
    )

    if not actives:
        logger.debug("fill_missing_task_ids: nothing to fill")
        return result

    logger.info(f"fill_missing_task_ids: {len(actives)} active records without task_id")

    updates = []
    for active in actives:
        try:
            at_time = datetime.combine(active.start_date, active.start_time)
            task_id = await fetch_active_task_id_for_vehicle(active.vehicle_id, at_time)
            if task_id:
                updates.append((active.id, task_id))
            else:
                result["skipped"] += 1
        except Exception as e:
            logger.warning(f"fill_missing_task_ids: active_id={active.id}, error={e}")
            result["errors"].append(f"id={active.id}: {e}")

    for active_id, task_id in updates:
        try:
            await ActiveDowntimeDAO.update({"id": active_id}, machine_task_id=task_id)
            logger.info(f"Filled task_id={task_id} for active_downtime id={active_id}")
            result["filled"] += 1
        except Exception as e:
            logger.warning(f"fill_missing_task_ids update failed: active_id={active_id}, error={e}")
            result["errors"].append(f"id={active_id}: {e}")

    return result


# ── Дозаполнение task_id для истории простоев (каждый час) ───────────────────

async def fill_missing_task_ids_history() -> dict:
    """
    Заполняет machine_task_id для завершённых простоев (Downtime) где он NULL.
    Запускается каждый час.

    Ищет только записи за последние 30 дней — более старые не трогаем,
    так как задачи Cropwise могут быть удалены или изменены.

    Покрывает два сценария:
      1. Celery был недоступен и активный простой завершился без заполнения task_id
      2. Офлайн-синхронизация — простой пришёл с задержкой до нескольких дней
    """
    from app.downtime.models import Downtime
    from app.downtime.dao import DowntimeDAO
    from app.cropwise.client import fetch_active_task_id_for_vehicle

    result = {"filled": 0, "skipped": 0, "errors": []}

    # Ищем только за последние 30 дней
    cutoff = (datetime.now() - timedelta(days=30)).date()

    downtimes = await DowntimeDAO.find_all_with_options(
        Downtime.machine_task_id == None,  # noqa: E711
        Downtime.start_date >= cutoff,
    )

    if not downtimes:
        logger.debug("fill_missing_task_ids_history: nothing to fill")
        return result

    logger.info(f"fill_missing_task_ids_history: {len(downtimes)} history records without task_id")

    updates = []
    for d in downtimes:
        try:
            at_time = datetime.combine(d.start_date, d.start_time)
            task_id = await fetch_active_task_id_for_vehicle(d.vehicle_id, at_time)
            if task_id:
                updates.append((d.id, task_id))
            else:
                result["skipped"] += 1
        except Exception as e:
            logger.warning(f"fill_missing_task_ids_history: downtime_id={d.id}, error={e}")
            result["errors"].append(f"id={d.id}: {e}")

    for downtime_id, task_id in updates:
        try:
            await DowntimeDAO.update({"id": downtime_id}, machine_task_id=task_id)
            logger.info(f"Filled task_id={task_id} for downtime id={downtime_id}")
            result["filled"] += 1
        except Exception as e:
            logger.warning(f"fill_missing_task_ids_history update failed: downtime_id={downtime_id}, error={e}")
            result["errors"].append(f"id={downtime_id}: {e}")

    return result

# ── Принудительное переписывание task_id для истории простоев за период ───────

async def force_refill_task_ids_for_period(
    date_from: "date",
    date_to: "date",
) -> dict:
    """
    Принудительно перезаписывает machine_task_id для ВСЕХ завершённых простоев
    (Downtime) за указанный период [date_from, date_to], независимо от того,
    есть у них уже task_id или нет.

    Используется когда задачи Cropwise изменились задним числом и нужно
    принудительно актуализировать привязки.
    """
    from app.downtime.models import Downtime
    from app.downtime.dao import DowntimeDAO
    from app.cropwise.client import fetch_active_task_id_for_vehicle
    from datetime import date, timedelta, time as dtime
    from sqlalchemy import or_, and_

    result = {"total": 0, "filled": 0, "cleared": 0, "skipped": 0, "errors": []}

    # Используем ту же логику смены (06:00), что и в get_admin_details
    SHIFT = dtime(6, 0)

    filters = []
    filters.append(or_(
        Downtime.start_date > date_from,
        and_(Downtime.start_date == date_from, Downtime.start_time >= SHIFT),
    ))
    date_to_plus1 = date_to + timedelta(days=1)
    filters.append(or_(
        Downtime.start_date < date_to_plus1,
        and_(Downtime.start_date == date_to_plus1, Downtime.start_time < SHIFT),
    ))

    downtimes = await DowntimeDAO.find_all_with_options(*filters)

    if not downtimes:
        logger.info(f"force_refill_task_ids_for_period: no records in [{date_from}, {date_to}]")
        return result

    result["total"] = len(downtimes)
    logger.info(
        f"force_refill_task_ids_for_period: {len(downtimes)} records in "
        f"[{date_from}, {date_to}], overwriting ALL task_ids"
    )

    for d in downtimes:
        try:
            at_time = datetime.combine(d.start_date, d.start_time)
            task_id = await fetch_active_task_id_for_vehicle(d.vehicle_id, at_time)
            if task_id:
                await DowntimeDAO.update({"id": d.id}, machine_task_id=task_id)
                logger.info(
                    f"force_refill: downtime_id={d.id} task_id "
                    f"{d.machine_task_id!r} -> {task_id}"
                )
                result["filled"] += 1
            else:
                # Cropwise не нашёл задачу — обнуляем, чтобы не хранить устаревший id
                if d.machine_task_id is not None:
                    await DowntimeDAO.update({"id": d.id}, machine_task_id=None)
                    result["cleared"] += 1
                    logger.info(f"force_refill: downtime_id={d.id} task_id cleared (no task in Cropwise)")
                else:
                    result["skipped"] += 1
        except Exception as e:
            logger.warning(f"force_refill: downtime_id={d.id}, error={e}")
            result["errors"].append(f"id={d.id}: {e}")

    logger.info(
        f"force_refill done: total={result['total']}, filled={result['filled']}, "
        f"cleared={result['cleared']}, skipped={result['skipped']}, errors={len(result['errors'])}"
    )
    return result
