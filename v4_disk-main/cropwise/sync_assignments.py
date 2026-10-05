from datetime import datetime, date, timedelta, timezone

import aiohttp

from app.cropwise.client import (
    CROPWISE_HEADERS,
    MACHINE_SUBTYPE_LABELS,
    _fetch_all_pages,
    fetch_cropwise_users_raw,
    localize_work_type,
)
from app.database import async_session_maker
from app.logging_config import get_logger

logger = get_logger("cropwise.sync_assignments")

TZ_MSK = timezone(timedelta(hours=3))

_FIO_NOT_FOUND = "ФИО не обнаружено"


def _build_cropwise_fio_map(users_raw: list[dict]) -> dict[int, str]:
    """
    ФИО-фолбэк 2-го уровня: username/email из живых данных Cropwise.
    Используется, только если пользователя нет (или у него пустое fio)
    в локальной БД — см. _resolve_fio.
    """
    result: dict[int, str] = {}
    for u in users_raw:
        uid = u.get("id")
        if uid is None:
            continue
        name = (u.get("username") or u.get("email") or "").strip()
        if name:
            result[int(uid)] = name
    return result


def _resolve_fio(
    driver_id: int,
    fio_by_id: dict[int, str],
    cropwise_fio_by_id: dict[int, str],
) -> str:
    """
    Разрешает ФИО водителя по цепочке:
    1) локальная БД (fio_by_id) — НЕЗАВИСИМО от статуса is_active,
       уволенные/деактивированные пользователи всё равно должны иметь ФИО;
    2) живой запрос в Cropwise (username/email), если в БД пользователя
       нет вообще или fio там пустое;
    3) "ФИО не обнаружено" — только если и там, и там пусто.
    """
    fio = fio_by_id.get(driver_id)
    if fio:
        return fio
    fio = cropwise_fio_by_id.get(driver_id)
    if fio:
        return fio
    return _FIO_NOT_FOUND

# Стандартные (hour, minute) окончания смены в МСК
_STANDARD_END_TIMES: frozenset[tuple[int, int]] = frozenset({(17, 59), (5, 59)})

# Стандартные (hour, minute) начала смены в МСК — задача должна открываться
# ровно на границе смены (одна задача = вся смена целиком), симметрично
# проверке _STANDARD_END_TIMES.
_STANDARD_START_TIMES: frozenset[tuple[int, int]] = frozenset({(6, 0), (18, 0)})

# Смена не может длиться дольше суток — если фактическая длительность задачи
# больше этого порога, это аномалия сама по себе, независимо от того,
# попал ли end_time на "стандартную" минуту (17:59/05:59).
_MAX_TASK_DURATION = timedelta(hours=24)


async def _fetch_changes(
    session: aiohttp.ClientSession,
    resource: str,
    gt_eq: str,
    lt: str,
) -> list[dict]:
    """
    Постранично тянет /{resource}/changes за период [gt_eq, lt) —
    ОДИН (максимум несколько при большом объёме) запрос вместо одного
    запроса на каждый отдельный id ресурса.

    Подтверждено диагностикой на реальном API (2026-07-20): фильтр
    updated_at_gt_eq/updated_at_lt работает как обычные comparison-фильтры,
    записи в ответе отсортированы по updated_at УБЫВАНИЕМ (first_record_time
    > last_record_time), лимит страницы — 1000 (meta.response.limit).
    Пагинация — сдвигаем updated_at_lt на last_record_time предыдущей
    страницы, пока obtained_records не станет меньше limit.
    """
    all_items: list[dict] = []
    cursor_lt = lt
    while True:
        async with session.get(
            f"https://operations.cropwise.com/api/v3/{resource}/changes",
            params={"updated_at_gt_eq": gt_eq, "updated_at_lt": cursor_lt},
        ) as resp:
            body = await resp.json()
        chunk = body.get("data", [])
        if not chunk:
            break
        all_items.extend(chunk)
        meta      = body.get("meta", {}).get("response", {})
        obtained  = meta.get("obtained_records", 0)
        page_lim  = meta.get("limit") or 1000
        last_time = meta.get("last_record_time")
        if obtained < page_lim or not last_time:
            break
        cursor_lt = last_time
    return all_items



# =============================================================================
# FETCH АКТИВНЫХ НАЗНАЧЕНИЙ
# =============================================================================

async def _fetch_active_assignments() -> dict[int, dict]:
    """
    Возвращает текущие активные назначения пользователей.
    Задача считается активной, если она уже началась и ещё не завершилась:
      start_time <= now <= end_time
    """

    now = datetime.now(tz=TZ_MSK)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%S")
    # Буфер 10 минут: задача считается активной ещё 10 минут после end_time.
    # Это защищает от ложного «исчезновения» задачи на границе временного окна
    # из-за погрешностей сети, задержек Celery или расхождения часов.
    end_time_buffer = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S")

    import asyncio

    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS) as session:
        machine_tasks, machines, work_types = await asyncio.gather(
            _fetch_all_pages(session, "machine_tasks", {
                "start_time_lt_eq": now_str,      # задача уже началась
                "end_time_gt_eq": end_time_buffer, # задача не закончилась (с буфером 10 мин)
            }),
            _fetch_all_pages(session, "machines"),
            _fetch_all_pages(session, "work_types"),
        )

    logger.info(
        f"sync_assignments: now={now_str}, fetched tasks={len(machine_tasks)}, "
        f"machines={len(machines)}, work_types={len(work_types)}"
    )

    machine_dict: dict[int, dict] = {m["id"]: m for m in machines}

    work_type_dict: dict[int, str] = {
        wt["id"]: localize_work_type(wt.get("name")) for wt in work_types
    }

    # Агрофирма из локальной БД: machine_id → agrofirm / legal_company
    from sqlalchemy import select
    from app.venchies.models import Vehicle

    async with async_session_maker() as _sess:
        veh_rows = await _sess.execute(
            select(Vehicle.id, Vehicle.agrofirm, Vehicle.legal_company)
        )
        agrofirm_by_machine_id: dict[int, str] = {
            row.id: (row.agrofirm or row.legal_company or "")
            for row in veh_rows
        }

    # =============================================================================
    # ВЫБОР АКТУАЛЬНОЙ ЗАДАЧИ — если у пользователя несколько активных,
    # берём ту, у которой end_time позже (самую "свежую")
    # =============================================================================

    latest_task_by_user: dict[int, dict] = {}

    def _end_ts(t: dict) -> datetime:
        s = t.get("end_time") or ""
        try:
            return datetime.fromisoformat(s)
        except Exception:
            return datetime.min

    for task in machine_tasks:
        driver_id = task.get("driver_id")
        if not driver_id:
            continue

        existing = latest_task_by_user.get(driver_id)

        if existing is None or _end_ts(task) > _end_ts(existing):
            latest_task_by_user[driver_id] = task

    # =============================================================================
    # СБОРКА РЕЗУЛЬТАТА
    # =============================================================================

    assignments: dict[int, dict] = {}

    for driver_id, task in latest_task_by_user.items():
        machine_id = task.get("machine_id")
        machine = machine_dict.get(machine_id) if machine_id else None

        work_type_id = task.get("work_type_id")
        work_type = work_type_dict.get(work_type_id, "—") if work_type_id else "—"

        subtype = (machine.get("machine_subtype") or "") if machine else ""
        subtype_label = (
            MACHINE_SUBTYPE_LABELS.get(subtype, subtype)
            if subtype else None
        )

        task_label = f"#{task['id']}"
        if task.get("description"):
            task_label += f" | {task['description']}"

        reg = None
        if machine:
            reg = (
                (machine.get("registration_number") or "").strip()
                or (machine.get("inventory_number") or "").strip()
                or None
            )

        assignments[driver_id] = {
            "registration_number": reg,
            "machine_subtype": subtype or None,
            "machine_subtype_label": subtype_label,
            "work_type": work_type,
            "task_label": task_label,
            "machine_task_id": task["id"],
            "machine_id": machine_id,
            "agrofirm": agrofirm_by_machine_id.get(machine_id, "") if machine_id else "",
            "task_end_time": task.get("end_time"),  # плановое время конца задачи (для snap-логики)
            "synced_at": datetime.now(),
        }

    logger.info(f"sync_assignments: built {len(assignments)} active assignments")


    return assignments


# =============================================================================
# SYNC В БД
# =============================================================================

async def sync_user_assignments() -> dict:
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from app.users.models import User
    from app.cropwise.assignment_model import UserAssignment
    from app.downtime.dao import DowntimeDAO

    result = {"upserted": 0, "cleared": 0, "skipped": 0, "auto_ended": 0, "errors": []}

    try:
        assignments = await _fetch_active_assignments()
    except Exception as e:
        logger.error(
            f"sync_user_assignments: failed to fetch from Cropwise: {e}",
            exc_info=True
        )
        result["errors"].append(str(e))
        return result

    async with async_session_maker() as session:
        try:
            stmt = select(User.id).where(User.is_active == True)  # noqa: E712
            active_user_ids: list[int] = list(
                (await session.execute(stmt)).scalars().all()
            )

            now = datetime.now()

            for user_id in active_user_ids:
                assignment_data = assignments.get(user_id)

                if assignment_data:
                    # machine_id и task_end_time не являются колонками UserAssignment — убираем перед upsert
                    db_data = {k: v for k, v in assignment_data.items() if k not in ("machine_id", "task_end_time", "agrofirm")}

                    upsert_stmt = (
                        pg_insert(UserAssignment)
                        .values(user_id=user_id, **db_data)
                        .on_conflict_do_update(
                            index_elements=["user_id"],
                            set_=db_data,
                        )
                    )
                    await session.execute(upsert_stmt)
                    result["upserted"] += 1
                    current_task_id = assignment_data.get("machine_task_id")
                    current_task_end_time_str = assignment_data.get("task_end_time")
                else:
                    # Задача из Cropwise исчезла. Перед очисткой проверяем:
                    # если у пользователя есть активный простой с machine_task_id — не трогаем
                    # это поле, чтобы фронтенд не показывал "задача не найдена".
                    # machine_task_id будет очищен автоматически, когда простой закроется.
                    from app.downtime.dao import ActiveDowntimeDAO
                    from app.downtime.models import ActiveDowntime
                    active_rows = await ActiveDowntimeDAO.find_all_with_options(
                        ActiveDowntime.user_id == user_id,
                        limit=1,
                    )
                    active_downtime = active_rows[0] if active_rows else None
                    preserve_task_id = (
                        active_downtime is not None
                        and active_downtime.machine_task_id is not None
                    )

                    if preserve_task_id:
                        # Сохраняем task_id — простой ещё активен, задача привязана
                        clear_fields = {
                            "registration_number": None,
                            "machine_subtype": None,
                            "machine_subtype_label": None,
                            "work_type": None,
                            "task_label": None,
                            "synced_at": now,
                            # machine_task_id НЕ очищаем
                        }
                        logger.debug(
                            f"sync_assignments: preserving machine_task_id="
                            f"{active_downtime.machine_task_id} for user={user_id} "
                            f"(active downtime exists)"
                        )
                    else:
                        clear_fields = {
                            "registration_number": None,
                            "machine_subtype": None,
                            "machine_subtype_label": None,
                            "work_type": None,
                            "task_label": None,
                            "machine_task_id": None,
                            "synced_at": now,
                        }

                    clear_stmt = (
                        pg_insert(UserAssignment)
                        .values(user_id=user_id, **clear_fields)
                        .on_conflict_do_update(
                            index_elements=["user_id"],
                            set_=clear_fields,
                        )
                    )
                    await session.execute(clear_stmt)
                    result["cleared"] += 1
                    current_task_id = None
                    current_task_end_time_str = None

                # Завершаем активный простой, если задача сменилась или исчезла
                # Парсим task_end_time для snap-логики (17:59 / 05:59)
                task_end_time_dt = None
                if current_task_end_time_str:
                    try:
                        # Парсим ISO-строку из Cropwise (UTC), конвертируем в МСК,
                        # затем убираем tzinfo — dao работает с naive-МСК datetime.
                        _parsed = datetime.fromisoformat(
                            str(current_task_end_time_str).replace("Z", "+00:00")
                        )
                        if _parsed.tzinfo is not None:
                            task_end_time_dt = _parsed.astimezone(TZ_MSK).replace(tzinfo=None)
                        else:
                            task_end_time_dt = _parsed  # уже naive, считаем МСК
                    except Exception:
                        pass

                try:
                    ended = await DowntimeDAO.auto_end_downtime_on_task_change(
                        user_id=user_id,
                        current_task_id=current_task_id,
                        task_end_time=task_end_time_dt,
                    )
                    if ended:
                        result["auto_ended"] += 1
                except Exception as e:
                    logger.error(
                        f"sync_user_assignments: auto_end failed for user={user_id}: {e}",
                        exc_info=True,
                    )

            await session.commit()

        except Exception as e:
            await session.rollback()
            logger.error(f"sync_user_assignments: DB error: {e}", exc_info=True)
            result["errors"].append(str(e))
            return result

    # Обновляем кэш нестандартных окончаний, переиспользуя данные из
    # assignments и raw_tasks. Дополнительных запросов к Cropwise API нет.
    try:
        await _update_nonstandard_endtimes_cache(assignments)
    except Exception as e:
        logger.error(f"sync_user_assignments: nonstandard cache failed: {e}", exc_info=True)

    # Обновляем кэш низкой производительности водителей.
    # Запросы к Cropwise MTFMI делаются здесь — они нужны только для
    # активных задач (обычно 5–15 машин), поэтому нагрузка минимальна.
    try:
        await _update_low_productivity_cache(assignments)
    except Exception as e:
        logger.error(f"sync_user_assignments: low_productivity cache failed: {e}", exc_info=True)

    # Кэш истории смен (вчера 06-18 / вчера 18 - сегодня 06) — пересчитывается
    # только один раз за сутки (когда меняется "вчера"), чтобы не дёргать
    # Cropwise на каждой синхронизации.
    try:
        await _update_shift_history_cache()
    except Exception as e:
        logger.error(f"sync_user_assignments: shift history cache failed: {e}", exc_info=True)

    logger.info(
        f"sync_user_assignments done: "
        f"upserted={result['upserted']}, "
        f"cleared={result['cleared']}, "
        f"auto_ended={result['auto_ended']}, "
        f"errors={len(result['errors'])}"
    )
    return result


# =============================================================================
# КЭШИРОВАНИЕ НЕСТАНДАРТНЫХ ОКОНЧАНИЙ
# =============================================================================

async def _update_nonstandard_endtimes_cache(assignments: dict[int, dict]) -> None:
    """
    Запрашивает все задачи за текущие сутки (06:00 МСК → сейчас) для машин
    с machine_type == "agri" (агротехника; транспорт "transport" сюда не
    входит — у него своих смен день/ночь нет), кладёт в Redis те, что не
    укладываются в диапазон: одна задача должна ЦЕЛИКОМ покрывать смену —
    начало ровно 06:00 или 18:00 МСК, конец ровно 17:59 или 05:59 МСК.
    Аномалия — несовпадение начала ИЛИ конца с этими границами, либо
    фактическая длительность задачи больше суток (_MAX_TASK_DURATION) —
    последнее ловится ДАЖЕ если и начало, и конец совпали с границами смены
    (например, машина отработала несколько смен подряд без разрыва, но с
    "зависшим" не закрытым вовремя заданием).

    Делает один дополнительный запрос к Cropwise (machine_tasks за сутки).
    Причина: _fetch_active_assignments тянет только текущие активные задачи
    (end_time >= now-10min), поэтому уже завершившиеся задачи (например,
    закончившиеся в 06:59) в ту выборку не попадают.
    """
    import asyncio as _asyncio
    from sqlalchemy import select
    from app.users.models import User
    from app.cropwise.nonstandard_endtimes_router import update_cache

    now = datetime.now(tz=TZ_MSK)

    # Границы текущих суток: 06:00 МСК сегодня → 05:59 МСК завтра.
    # До 06:00 — ещё «вчерашние» сутки.
    day_start = now.replace(hour=6, minute=0, second=0, microsecond=0)
    if now.hour < 6:
        day_start -= timedelta(days=1)
    day_end = day_start + timedelta(hours=23, minutes=59, seconds=59)

    day_start_str = day_start.strftime("%Y-%m-%dT%H:%M:%S")
    day_end_str   = day_end.strftime("%Y-%m-%dT%H:%M:%S")

    # Фильтруем по end_time — ловим все задачи, которые ЗАКАНЧИВАЮТСЯ
    # в текущие сутки, независимо от того, когда они начались.
    # Например: задача 17:59 → завтра 06:59 стартует в текущих сутках,
    # но заканчивается тоже в них (06:59 < 05:59+24h = следующие сутки).
    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS) as session:
        day_tasks, work_types_raw, users_raw = await _asyncio.gather(
            _fetch_all_pages(session, "machine_tasks", {
                "end_time_gt_eq": day_start_str,
                "end_time_lt":    day_end_str,
            }),
            _fetch_all_pages(session, "work_types"),
            _fetch_all_pages(session, "users"),
        )

    work_type_dict: dict[int, str] = {
        wt["id"]: localize_work_type(wt.get("name")) for wt in work_types_raw
    }
    cropwise_fio_by_id = _build_cropwise_fio_map(users_raw)

    # ФИО ВСЕХ пользователей (не только активных — уволенный/деактивированный
    # водитель тоже должен подтягиваться по ФИО, а не превращаться в
    # "ФИО не обнаружено") + таблица vehicles из локальной БД.
    # Vehicle.id == Cropwise machine_id (autoincrement=False, внешний ID)
    from app.venchies.models import Vehicle
    async with async_session_maker() as session:
        rows_db = await session.execute(
            select(User.id, User.fio)
        )
        fio_by_id: dict[int, str] = {row.id: row.fio for row in rows_db}

        vehicles_db = await session.execute(
            select(Vehicle.id, Vehicle.machine_type, Vehicle.agrofirm, Vehicle.legal_company)
        )
        vehicles_rows = vehicles_db.all()
        valid_machine_ids: set[int] = {row.id for row in vehicles_rows}
        machine_meta: dict[int, dict] = {
            row.id: {
                "machine_type":  row.machine_type or "—",
                "agrofirm":      row.agrofirm or "",
                "legal_company": row.legal_company or "",
            }
            for row in vehicles_rows
        }

    # machine_id из task.get("machine_id") — но Cropwise иногда отдаёт
    # в этом поле ID задачи, а не машины. Проверяем через valid_machine_ids:
    # если значение есть в таблице vehicles — это настоящий machine_id,
    # иначе фолбэк из assignments (активные задачи).
    machine_id_by_task: dict[int, int] = {
        a["machine_task_id"]: a["machine_id"]
        for a in assignments.values()
        if a.get("machine_task_id") and a.get("machine_id")
    }

    rows: list[dict] = []
    seen_tasks: set[int] = set()

    # ── Диагностика: чтобы не гадать вслепую, если строк снова окажется 0 ────
    from collections import defaultdict
    _diag_total = 0
    _diag_bad_end = 0
    _diag_not_agri = 0
    _diag_machine_type_seen: dict[str, int] = defaultdict(int)
    _diag_unresolved_machine = 0

    for task in day_tasks:
        _diag_total += 1
        tid       = task.get("id")
        driver_id = task.get("driver_id")
        if not tid or not driver_id or tid in seen_tasks:
            continue
        seen_tasks.add(tid)

        end_str   = task.get("end_time") or ""
        start_str = task.get("start_time") or ""

        try:
            end_dt = datetime.fromisoformat(
                end_str.replace("Z", "+00:00")
            ).astimezone(TZ_MSK)
        except Exception:
            _diag_bad_end += 1
            continue

        # Резолвим машину/тип ДО решения об аномалии — нас интересуют
        # только агромашины, а не транспорт (грузовики и т.п.), у которого
        # своих рабочих смен 06:00-17:59/18:00-05:59 нет и быть не может.
        # По докам Cropwise machine_id в MachineTask — обязательное поле,
        # прямая ссылка на Machine. Единственный известный edge case —
        # оно иногда совпадает с id самой задачи (баг Cropwise); доверяем
        # raw_mid, если он не равен tid, а не требуем обязательного
        # присутствия в локальной таблице vehicles (которая может отставать
        # от Cropwise и не содержать свежедобавленную технику).
        raw_mid = task.get("machine_id")
        if raw_mid and raw_mid != tid:
            machine_id = raw_mid
        else:
            machine_id = machine_id_by_task.get(tid) or raw_mid or 0
        if not machine_id:
            _diag_unresolved_machine += 1

        meta = machine_meta.get(machine_id, {})
        _diag_machine_type_seen[str(meta.get("machine_type") or "—")] += 1
        # Fail-open: исключаем ТОЛЬКО подтверждённый transport. Строгое
        # "== agri" роняло строки, где machine_id не удалось зарезолвить
        # (частый случай для уже ЗАВЕРШИВШИХСЯ задач — machine_id_by_task
        # строится только из активных assignments, см. комментарий выше),
        # хотя реальная машина была agri — просто её тип остался неизвестен
        # в эту миллисекунду. Лучше показать строку с machine_type="—",
        # чем молча потерять реальную аномалию.
        if (meta.get("machine_type") or "").strip().lower() == "transport":
            _diag_not_agri += 1
            continue

        start_dt = None
        try:
            start_dt = datetime.fromisoformat(
                start_str.replace("Z", "+00:00")
            ).astimezone(TZ_MSK)
        except Exception:
            pass

        is_long_duration = False
        is_start_mismatch = False
        if start_dt is not None:
            # Базовая проверка: конец должен быть ровно 17:59 или 05:59 —
            # это покрывает и обычную одну смену, и НЕПРЕРЫВНУЮ работу
            # несколько смен подряд без разрыва (например, старт 06:00
            # (начало дня) → конец 05:59 следующих суток (конец ночи) —
            # это два легитимных стыкующихся конца, а не аномалия.
            # РАНЬШЕ здесь была ещё проверка "дата конца должна совпадать
            # с датой конца ИМЕННО первой смены от старта" — она ошибочно
            # флагала такие непрерывные многосменные задачи как аномалию.
            # Убрано: это была непроверенная гипотеза, а не подтверждённое
            # вами требование.
            is_shift_mismatch = (end_dt.hour, end_dt.minute) not in _STANDARD_END_TIMES
            # Симметричная проверка начала: задача должна ОТКРЫВАТЬСЯ ровно
            # на границе смены (06:00/18:00) — подтверждённое требование:
            # одна задача = вся смена целиком, а не только "закрыта вовремя".
            is_start_mismatch = (start_dt.hour, start_dt.minute) not in _STANDARD_START_TIMES
            # Отдельно — длительность самой задачи. Даже если конец совпал
            # с границей смены, задача длиной больше суток сама по себе
            # аномалия (водитель не закрыл предыдущее задание, и Cropwise
            # "растянул" его на несколько смен подряд).
            is_long_duration = (end_dt - start_dt) > _MAX_TASK_DURATION
            is_nonstandard = is_shift_mismatch or is_start_mismatch or is_long_duration
        else:
            # start_time не распарсился — проверить границу начала нечем,
            # используем ту же базовую проверку по концу.
            is_shift_mismatch = (end_dt.hour, end_dt.minute) not in _STANDARD_END_TIMES
            is_nonstandard = is_shift_mismatch

        if not is_nonstandard:
            continue

        start_fmt = start_dt.strftime("%d.%m %H:%M") if start_dt is not None else "—"
        duration_hours = round((end_dt - start_dt).total_seconds() / 3600, 1) if start_dt is not None else None
        reasons = []
        if is_long_duration:
            reasons.append("long_duration")
        if is_start_mismatch:
            reasons.append("start_mismatch")
        if is_shift_mismatch:
            reasons.append("end_mismatch")
        reason = ",".join(reasons) if reasons else "end_mismatch"

        wt_id     = task.get("work_type_id")
        work_type = work_type_dict.get(wt_id, "—") if wt_id else "—"

        # Агрофирма: если vehicle.agrofirm заполнена через поля (fetch_machine_agrofirm_map) — берём её.
        # Если нет (нет привязки к полям) — fallback на legal_company машины.
        agrofirm_val = meta.get("agrofirm") or meta.get("legal_company") or "—"
        rows.append({
            "fio":            _resolve_fio(driver_id, fio_by_id, cropwise_fio_by_id),
            "task_id":        tid,
            "machine_id":     machine_id,
            "machine_type":   meta.get("machine_type", "—"),
            "agrofirm":       agrofirm_val,
            "start":          start_fmt,
            "end":            end_dt.strftime("%d.%m %H:%M"),
            "start_ts":       start_dt.isoformat() if start_dt is not None else None,
            "end_ts":         end_dt.isoformat(),
            "work_type":      work_type,
            "duration_hours": duration_hours,
            "reason":         reason,
        })

    rows.sort(key=lambda r: r["fio"])

    logger.info(
        f"_update_nonstandard_endtimes_cache: diag total={_diag_total}, "
        f"bad_end_time={_diag_bad_end}, unresolved_machine={_diag_unresolved_machine}, "
        f"excluded_as_transport={_diag_not_agri}, "
        f"machine_type_distribution={dict(_diag_machine_type_seen)}, "
        f"final_rows={len(rows)}"
    )

    # ── Поиск пересечений задач по машинам (текущие + прошлые сутки) ─────────
    # Для прошлых суток — один дополнительный запрос к Cropwise.
    # work_types и valid_machine_ids переиспользуем: они уже загружены выше.
    from collections import defaultdict

    prev_day_start = day_start - timedelta(days=1)
    prev_day_end   = day_start - timedelta(seconds=1)

    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS) as session:
        prev_day_tasks = await _fetch_all_pages(session, "machine_tasks", {
            "end_time_gt_eq": prev_day_start.strftime("%Y-%m-%dT%H:%M:%S"),
            "end_time_lt":    prev_day_end.strftime("%Y-%m-%dT%H:%M:%S"),
        })

    all_tasks_for_overlaps = day_tasks + prev_day_tasks

    machine_intervals: dict[int, list[tuple[datetime, datetime, int, str]]] = defaultdict(list)
    for task in all_tasks_for_overlaps:
        tid = task.get("id")
        if not tid:
            continue
        raw_mid = task.get("machine_id")
        if raw_mid and raw_mid in valid_machine_ids:
            mid = raw_mid
        else:
            mid = machine_id_by_task.get(tid) or 0
        if not mid:
            continue
        s_str = task.get("start_time") or ""
        e_str = task.get("end_time") or ""
        try:
            s_dt = datetime.fromisoformat(s_str.replace("Z", "+00:00")).astimezone(TZ_MSK)
            e_dt = datetime.fromisoformat(e_str.replace("Z", "+00:00")).astimezone(TZ_MSK)
        except Exception:
            continue
        day_label = "вчера" if e_dt < day_start else "сегодня"
        machine_intervals[mid].append((s_dt, e_dt, tid, day_label))

    overlaps: list[dict] = []
    seen_overlap_pairs: set[frozenset] = set()
    for mid, intervals in machine_intervals.items():
        intervals.sort(key=lambda x: x[0])
        for i in range(len(intervals)):
            for j in range(i + 1, len(intervals)):
                s1, e1, tid1, day1 = intervals[i]
                s2, e2, tid2, day2 = intervals[j]
                pair = frozenset({tid1, tid2})
                if pair in seen_overlap_pairs:
                    continue
                if s1 < e2 and s2 < e1:
                    seen_overlap_pairs.add(pair)
                    ometa = machine_meta.get(mid, {})
                    if ometa.get("machine_type", "").lower() == "transport":
                        overlap_agrofirm = ometa.get("legal_company") or ometa.get("agrofirm") or "—"
                    else:
                        overlap_agrofirm = ometa.get("agrofirm") or ometa.get("legal_company") or "—"         
                                   
                    overlaps.append({
                        "machine_id":   mid,
                        "machine_type": ometa.get("machine_type", "—"),
                        "agrofirm":     overlap_agrofirm,
                        "task_id_1":    tid1,
                        "start_1":      s1.strftime("%d.%m %H:%M"),
                        "end_1":        e1.strftime("%d.%m %H:%M"),
                        "day_1":        day1,
                        "task_id_2":    tid2,
                        "start_2":      s2.strftime("%d.%m %H:%M"),
                        "end_2":        e2.strftime("%d.%m %H:%M"),
                        "day_2":        day2,
                    })

    overlaps.sort(key=lambda o: o["machine_id"])
    # ─────────────────────────────────────────────────────────────────────────

    update_cache(rows, generated_at=now, overlaps=overlaps)
    logger.info(
        f"_update_nonstandard_endtimes_cache: {len(rows)} rows, "
        f"{len(overlaps)} overlapping machine tasks "
        f"(prev window: {prev_day_start.strftime('%d.%m %H:%M')} – {prev_day_end.strftime('%d.%m %H:%M')})"
    )


# =============================================================================
# КЭШИРОВАНИЕ НИЗКОЙ ПРОИЗВОДИТЕЛЬНОСТИ
# =============================================================================

async def _update_low_productivity_cache(assignments: dict[int, dict]) -> None:
    """
    Для каждого активного водителя тянет MTFMI с covered_area_hourly,
    считает производительность за текущий час, прошлый и с начала задачи,
    джойнит с ActiveDowntime (причина/время простоя) и кладёт в Redis.

    MTFMI тянется одним(-двумя) пагинированными запросами к /changes
    (см. _fetch_changes) вместо запроса на каждую активную машину —
    тот же приём, что и в _update_shift_history_cache.
    """
    from datetime import datetime, timedelta, timezone, date
    from sqlalchemy import select

    import aiohttp

    from app.cropwise.client import CROPWISE_HEADERS
    from app.cropwise.low_productivity_router import update_cache
    from app.downtime.models import ActiveDowntime, Downtime
    from app.driver_stops.models import CropwiseStop
    from app.users.models import User

    # Порог убран: храним ВСЕ строки с total_area > 0.1 га.
    # Фильтрация по порогу делается на клиенте из кэша.
    MIN_AREA_HA     = 0.1

    TZ_MSK = timezone(timedelta(hours=3))
    now    = datetime.now(tz=TZ_MSK)

    # Начало смены (06:00 МСК) — для расчёта day_area
    shift_start = now.replace(hour=6, minute=0, second=0, microsecond=0)
    if now.hour < 6:
        shift_start -= timedelta(days=1)

    curr_hour_start = now.replace(minute=0, second=0, microsecond=0)
    prev_hour_start = curr_hour_start - timedelta(hours=1)
    curr_hour_end   = curr_hour_start + timedelta(hours=1)
    # Дополнительные часы для показа 4 последних часов
    h2_start = curr_hour_start - timedelta(hours=2)
    h3_start = curr_hour_start - timedelta(hours=3)

    # ── Активные задачи из assignments ───────────────────────────────────────
    # assignments: {driver_id: {machine_task_id, registration_number, work_type, ...}}
    active_tasks: list[tuple[int, dict]] = [
        (driver_id, info)
        for driver_id, info in assignments.items()
        if info.get("machine_task_id")
    ]

    # ВАЖНО: раньше при пустом active_tasks функция сразу выходила с пустым
    # кэшем (update_cache([], now); return). Из-за этого, например, при
    # временном сбое Cropwise или просто в моменты, когда вообще ни у кого
    # нет активного задания, с панели пропадали ВСЕ простои разом. Теперь
    # ранний выход убран — ниже водители с зафиксированным простоем
    # добавляются в кэш отдельно (см. блок «orphan»), даже если
    # active_tasks пуст.
    driver_ids_with_task = {d for d, _ in active_tasks}
    task_ids = [info["machine_task_id"] for _, info in active_tasks]

    # ── MTFMI: один(-два) пагинированных запроса /changes вместо одного ──────
    # запроса на каждый task_id (тот же приём, что и в _update_shift_history_cache).
    # Активные задачи могли начаться в начале смены (shift_start), поэтому окно
    # берём от shift_start с запасом назад на случай задержки обработки Cropwise,
    # затем фильтруем на своей стороне по task_ids активных водителей.
    MTFMI_CHANGES_LOOKBACK_BUFFER = timedelta(hours=12)
    changes_gt_eq = (shift_start - MTFMI_CHANGES_LOOKBACK_BUFFER).strftime("%Y-%m-%dT%H:%M:%S")
    changes_lt    = (now + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")

    relevant_task_ids = set(task_ids)
    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS) as session:
        changed_mtfmi = await _fetch_changes(
            session, "machine_task_field_mapping_items", changes_gt_eq, changes_lt,
        )

    mtfmi_map: dict[int, list[dict]] = {}
    for item in changed_mtfmi:
        tid = item.get("machine_task_id")
        if tid in relevant_task_ids:
            mtfmi_map.setdefault(tid, []).append(item)

    logger.info(
        f"_update_low_productivity_cache: /changes вернул {len(changed_mtfmi)} MTFMI-записей "
        f"за [{changes_gt_eq}, {changes_lt}), из них relevant="
        f"{sum(len(v) for v in mtfmi_map.values())} по {len(mtfmi_map)}/{len(relevant_task_ids)} "
        f"задачам (вместо {len(relevant_task_ids)} поштучных запросов)"
    )

    # ── Агрофирма по MTFMI (как в _resolve_agrofirms / admin-details/enrich) ─
    # Строим справочник field_id → агрофирма один раз для всех активных задач,
    # затем голосуем по полям каждой задачи. Это тот же источник истины, что
    # используется в "Истории простоев всех пользователей за период".
    # Fallback: vehicles.agrofirm (из assignments), если задача не покрывает полей.
    try:
        if mtfmi_map:
            from app.cropwise.client import fetch_field_to_agrofirm
            from collections import Counter as _Counter
            field_to_agrofirm: dict[int, str] = await fetch_field_to_agrofirm()
            task_to_agrofirm: dict[int, str] = {}
            for tid, items in mtfmi_map.items():
                votes: _Counter = _Counter()
                for item in items:
                    fid = item.get("field_id")
                    if fid:
                        ag = field_to_agrofirm.get(fid)
                        if ag:
                            votes[ag] += 1
                if votes:
                    task_to_agrofirm[tid] = votes.most_common(1)[0][0]
        else:
            task_to_agrofirm = {}
    except Exception as _e:
        logger.warning(f"_update_low_productivity_cache: agrofirm resolve failed, using vehicles.agrofirm: {_e}")
        task_to_agrofirm = {}

    # ── Парсер дат ───────────────────────────────────────────────────────────
    def _parse_dt(s):
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
        return dt.replace(tzinfo=timezone.utc).astimezone(TZ_MSK) if dt.tzinfo is None else dt.astimezone(TZ_MSK)

    # ── Расчёт почасовой производительности ──────────────────────────────────
    def _calc_hourly(items_for_task: list[dict]) -> dict:
        total = h1_area = h2_area = h3_area = curr_area = day_area = 0.0
        fuel_total = 0.0
        covered_area_total = 0.0
        for item in items_for_task:
            # Суммируем расход топлива и обработанную площадь по всем item-ам задачи
            fuel_total += float(item.get("fuel_consumption") or 0)
            covered_area_total += float(item.get("covered_area") or 0)
            for entry in (item.get("covered_area_hourly") or []):
                h = _parse_dt(entry[0])
                if h is None:
                    continue
                val = float(entry[1] or 0)
                total += val
                if h >= shift_start:
                    day_area += val
                if h3_start <= h < h2_start:
                    h3_area += val
                elif h2_start <= h < prev_hour_start:
                    h2_area += val
                elif prev_hour_start <= h < curr_hour_start:
                    h1_area += val
                elif curr_hour_start <= h < curr_hour_end:
                    curr_area += val
        return {
            "total_area":      round(total, 2),
            "day_area":        round(day_area, 2),
            # 3 завершённых часа (h3 = самый старый, h1 = самый свежий завершённый)
            "h3_area":         round(h3_area, 2),
            "h3_label":        f"{h3_start.strftime('%H:%M')}–{h2_start.strftime('%H:%M')}",
            "h2_area":         round(h2_area, 2),
            "h2_label":        f"{h2_start.strftime('%H:%M')}–{prev_hour_start.strftime('%H:%M')}",
            "prev_hour_area":  round(h1_area, 2),
            "prev_hour_label": f"{prev_hour_start.strftime('%H:%M')}–{curr_hour_start.strftime('%H:%M')}",
            # Текущий незавершённый час
            "curr_hour_area":  round(curr_area, 2),
            "curr_hour_label": f"{curr_hour_start.strftime('%H:%M')}–{curr_hour_end.strftime('%H:%M')}",
            # Расход топлива и обработанная площадь (для подсветки нулевого топлива)
            "fuel_consumption": round(fuel_total, 2),
            "covered_area":     round(covered_area_total, 2),
        }

    # ── FIO водителей + machine_type из локальной БД ─────────────────────────
    from app.venchies.models import Vehicle

    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS) as session:
        cropwise_users_raw = await _fetch_all_pages(session, "users")
    cropwise_fio_by_id = _build_cropwise_fio_map(cropwise_users_raw)

    async with async_session_maker() as session:
        rows_db = await session.execute(
            select(User.id, User.fio, User.is_active)
        )
        rows_db_list = rows_db.all()
        # ФИО для отображения — по ВСЕМ пользователям (в т.ч. деактивированным),
        # чтобы уволенный водитель не превращался в "ФИО не обнаружено".
        fio_by_id: dict[int, str] = {row.id: row.fio for row in rows_db_list}
        # А вот список для ПОИСКА ПРОСТОЕВ ниже — намеренно только активные
        # (см. пояснение в комментарии ниже про driver_ids).
        active_driver_ids: set[int] = {row.id for row in rows_db_list if row.is_active}

        # Запрашиваем ВСЕ машины (а не только те, что сейчас на активных
        # заданиях): нужно для строк-«сирот» — водителей с простоем, у
        # которых прямо сейчас нет активного задания в Cropwise (см. ниже).
        veh_rows = await session.execute(
            select(
                Vehicle.id, Vehicle.registration_number,
                Vehicle.machine_type, Vehicle.agrofirm, Vehicle.legal_company,
            )
        )
        veh_list = veh_rows.all()
        machine_type_by_plate: dict[str, str] = {
            row.registration_number: (row.machine_type or "")
            for row in veh_list if row.registration_number
        }
        # vehicle_id (= machine_id) → метаданные техники, для orphan-строк
        vehicle_meta_by_id: dict[int, dict] = {
            row.id: {
                "registration_number": row.registration_number or "—",
                "machine_type":        row.machine_type or "",
                "agrofirm":            row.agrofirm or row.legal_company or "—",
            }
            for row in veh_list
        }

    # ── Активные/завершённые простои: по ВСЕМ активным пользователям ─────────
    # ВАЖНО: раньше тут был driver_ids = [d for d, _ in active_tasks], то есть
    # простои искались только для водителей, у которых ПРЯМО СЕЙЧАС есть
    # активное задание в Cropwise. Но именно во время простоя задание у
    # водителя в Cropwise чаще всего уже закрыто/ещё не выдано — и такой
    # водитель в active_tasks не попадал вовсе, а значит его простой никак
    # не подтягивался на панель. Поэтому теперь простои ищем по всем активным
    # пользователям, а не только по тем, у кого сейчас есть задание.
    # ВАЖНО: именно active_driver_ids, а не fio_by_id.keys() — fio_by_id
    # теперь содержит и деактивированных пользователей (нужно для ФИО в
    # отображении), но простои по ним искать не нужно.
    driver_ids = list(active_driver_ids)
    today = date.today()
    yesterday = today - timedelta(days=1)

    async with async_session_maker() as session:
        active_dt_rows = await session.execute(
            select(ActiveDowntime).where(ActiveDowntime.user_id.in_(driver_ids))
        )
        active_downtimes: dict[int, ActiveDowntime] = {
            row.user_id: row for row in active_dt_rows.scalars().all()
        }

        # Завершённые простои за смену.
        # Ночная смена (18:00–06:00) охватывает два календарных дня,
        # поэтому до 06:00 МСК тянем простои и за вчера, и за сегодня.
        from sqlalchemy import or_ as _or_
        shift_dates = [today] if now.hour >= 6 else [yesterday, today]
        finished_dt_rows = await session.execute(
            select(Downtime).where(
                Downtime.user_id.in_(driver_ids),
                _or_(*[Downtime.start_date == d for d in shift_dates]),
            )
        )
        finished_downtimes_by_user: dict[int, list[Downtime]] = {}
        for row in finished_dt_rows.scalars().all():
            finished_downtimes_by_user.setdefault(row.user_id, []).append(row)

        # ── Простои из панели «Простои» (cropwise_stops) ─────────────────────
        # Дополнительный источник: причину сюда вписывает сам водитель на
        # странице «Мои простои из Cropwise» (может не совпадать по времени
        # заполнения с записью в downtime — иногда там причина появляется
        # раньше/вместо неё, см. work_time_report_router.py). Берём только
        # записи с уже заполненной причиной — пустые (ещё не отмеченные
        # водителем) не считаем зафиксированным простоем.
        cw_stop_rows = await session.execute(
            select(CropwiseStop).where(
                CropwiseStop.driver_user_id.in_(driver_ids),
                CropwiseStop.shift_date == shift_start.strftime("%Y-%m-%d"),
                CropwiseStop.reason.is_not(None),
            )
        )
        cropwise_stops_by_user: dict[int, list[CropwiseStop]] = {}
        for row in cw_stop_rows.scalars().all():
            cropwise_stops_by_user.setdefault(row.driver_user_id, []).append(row)

    # ── Формат длительности ───────────────────────────────────────────────────
    def _fmt_duration(seconds: int) -> str:
        if seconds <= 0:
            return "0м"
        h = seconds // 3600
        m = (seconds % 3600) // 60
        if h and m:
            return f"{h}ч {m}м"
        if h:
            return f"{h}ч"
        return f"{m}м"

    # ── Простой: общий хелпер (используется и для строк с заданием, и для ──
    #    «orphan»-строк без текущего задания, см. ниже) ────────────────────────
    def _build_downtime_info(driver_id: int) -> list[dict]:
        info: list[dict] = []
        # Интервалы, уже учтённые ниже (downtime/active_downtime) — чтобы не
        # задвоить простой, если тот же интервал есть и в cropwise_stops.
        existing_intervals: list[tuple[datetime, datetime]] = []

        # Активный простой (ещё не закрыт)
        active_dt = active_downtimes.get(driver_id)
        if active_dt:
            start_dt = datetime.combine(active_dt.start_date, active_dt.start_time, tzinfo=TZ_MSK)
            existing_intervals.append((start_dt, now))
            start_str = f"{active_dt.start_date.strftime('%d.%m')} {active_dt.start_time.strftime('%H:%M')}"
            info.append({
                "reason":   active_dt.reason,
                "start":    start_str,
                "end":      None,        # ещё идёт
                "duration": None,
                "active":   True,
            })

        # Завершённые простои за сегодня
        for dt in sorted(
            finished_downtimes_by_user.get(driver_id, []),
            key=lambda x: (x.start_date, x.start_time),
        ):
            start_dt = datetime.combine(dt.start_date, dt.start_time, tzinfo=TZ_MSK)
            end_dt   = datetime.combine(dt.end_date, dt.end_time, tzinfo=TZ_MSK)
            existing_intervals.append((start_dt, end_dt))
            start_str = f"{dt.start_date.strftime('%d.%m')} {dt.start_time.strftime('%H:%M')}"
            end_str   = f"{dt.end_date.strftime('%d.%m')} {dt.end_time.strftime('%H:%M')}"
            info.append({
                "reason":   dt.reason,
                "start":    start_str,
                "end":      end_str,
                "duration": _fmt_duration(dt.duration),
                "active":   False,
            })

        # ── Дополняем данными из панели «Простои» (cropwise_stops) ───────────
        # Добавляем только интервалы, ещё НЕ покрытые downtime/active_downtime
        # выше — иначе один и тот же простой отобразится дважды.
        for stop in sorted(cropwise_stops_by_user.get(driver_id, []), key=lambda s: s.stop_start):
            s_start = stop.stop_start.astimezone(TZ_MSK)
            s_end   = stop.stop_end.astimezone(TZ_MSK)
            if any(s_start < e and s < s_end for s, e in existing_intervals):
                continue
            info.append({
                "reason":   stop.reason,
                "start":    s_start.strftime("%d.%m %H:%M"),
                "end":      s_end.strftime("%d.%m %H:%M"),
                "duration": _fmt_duration(stop.duration_sec),
                "active":   False,
                "source":   "cropwise_stops",
            })
            existing_intervals.append((s_start, s_end))

        return info

    # ── Сборка строк ──────────────────────────────────────────────────────────
    rows: list[dict] = []

    for driver_id, info in active_tasks:
        task_id    = info["machine_task_id"]
        items      = mtfmi_map.get(task_id, [])
        hp         = _calc_hourly(items)

        # Фильтр по типу работ: пропускаем подтипы 7.x и 8.x
        # (транспортировка, перегон, погрузка и т.п. — не агрополевые работы)
        work_type = info.get("work_type") or ""
        work_prefix = work_type.lstrip().split(".")[0] if work_type else ""
        if work_prefix in ("7", "8"):
            continue

        # Индикатор: сумма за последний завершённый + текущий неполный час
        avg_ha_h = hp["prev_hour_area"] + hp["curr_hour_area"]

        downtime_info = _build_downtime_info(driver_id)

        # Фильтр: минимальная обработанная площадь за сутки > 0.1 га
        # (исключаем простаивающие машины совсем без работы). Порог остаётся
        # прежним, НО не применяется к водителям с зафиксированным простоем —
        # иначе именно те, у кого ничего не намолочено ИЗ-ЗА простоя,
        # выпадали бы из кэша раньше, чем для них успевал подставиться
        # has_downtime, и их простой не было видно на панели.
        if hp["day_area"] <= MIN_AREA_HA and not downtime_info:
            continue

        rows.append({
            "fio":             _resolve_fio(driver_id, fio_by_id, cropwise_fio_by_id),
            "user_id":         driver_id,
            "task_id":         task_id,
            "machine_id":      info.get("machine_id") or 0,
            "license_plate":   info.get("registration_number") or "—",
            "machine_type":    machine_type_by_plate.get(info.get("registration_number") or "", ""),
            "agrofirm":        task_to_agrofirm.get(task_id) or info.get("agrofirm") or "—",
            "work_type":       info.get("work_type") or "—",
            "avg_ha_h":        round(avg_ha_h, 2),
            "day_area":        hp["day_area"],
            "shift_start":     shift_start.strftime("%H:%M"),
            "total_area":      hp["total_area"],
            "h3_area":         hp["h3_area"],
            "h3_label":        hp["h3_label"],
            "h2_area":         hp["h2_area"],
            "h2_label":        hp["h2_label"],
            "prev_hour_area":  hp["prev_hour_area"],
            "prev_hour_label": hp["prev_hour_label"],
            "curr_hour_area":  hp["curr_hour_area"],
            "curr_hour_label": hp["curr_hour_label"],
            # has_downtime = True если есть хотя бы один активный или завершённый простой сегодня
            "has_downtime":    bool(downtime_info),
            "downtime":        downtime_info,  # список всех простоев (активных + завершённых)
            "no_active_task":  False,
        })

    # ── Orphan-строки: водители с простоем, у которых ПРЯМО СЕЙЧАС нет ──────
    # активного задания в Cropwise (и поэтому они не попали в active_tasks
    # выше). Это основная причина «потерянных» простоев на панели — водитель
    # ставит простой, его задание в Cropwise к этому моменту уже закрыто
    # (или новое ещё не выдано), и раньше он просто исчезал из кэша целиком.
    downtime_driver_ids = (
        set(active_downtimes.keys())
        | set(finished_downtimes_by_user.keys())
        | set(cropwise_stops_by_user.keys())
    )

    def _orphan_row(driver_id: int, downtime_info: list[dict]) -> dict:
        active_dt = active_downtimes.get(driver_id)
        finished_list = sorted(
            finished_downtimes_by_user.get(driver_id, []),
            key=lambda x: (x.start_date, x.start_time),
        )
        # Источник метаданных о технике: активный простой важнее завершённого,
        # завершённый — берём самый свежий за смену.
        src = active_dt or (finished_list[-1] if finished_list else None)

        # Если ни в downtime, ни в active_downtime записи нет (простой известен
        # только из cropwise_stops), берём метаданные машины/агрофирмы оттуда.
        cw_stops_list = sorted(cropwise_stops_by_user.get(driver_id, []), key=lambda s: s.stop_start)
        cw_src = cw_stops_list[-1] if cw_stops_list else None

        vehicle_id = getattr(src, "vehicle_id", None) if src else None
        veh = vehicle_meta_by_id.get(vehicle_id, {}) if vehicle_id else {}
        license_plate = (
            (getattr(src, "license_plate", None) if src else None)
            or veh.get("registration_number")
            or (getattr(cw_src, "machine_name", None) if cw_src else None)
            or "—"
        )
        agrofirm = (
            (getattr(src, "agrofirm", None) if src else None)
            or veh.get("agrofirm")
            or (getattr(cw_src, "agrofirm", None) if cw_src else None)
            or "—"
        )
        machine_type = veh.get("machine_type") or machine_type_by_plate.get(license_plate, "")
        # machine_task_id может быть None — если простой не был привязан к
        # заданию, или сохранённое значение уже не актуально.
        task_id_src = getattr(src, "machine_task_id", None) if src else None

        zero_hp = _calc_hourly([])  # все площади = 0, подписи часов — как у всех остальных строк

        return {
            "fio":             fio_by_id.get(driver_id) or (getattr(src, "fio", None) if src else None) or cropwise_fio_by_id.get(driver_id) or _FIO_NOT_FOUND,
            "user_id":         driver_id,
            "task_id":         task_id_src,
            "machine_id":      vehicle_id or 0,
            "license_plate":   license_plate,
            "machine_type":    machine_type,
            "agrofirm":        agrofirm,
            "work_type":       "—",
            "avg_ha_h":        0.0,
            "day_area":        zero_hp["day_area"],
            "shift_start":     shift_start.strftime("%H:%M"),
            "total_area":      zero_hp["total_area"],
            "h3_area":         zero_hp["h3_area"],
            "h3_label":        zero_hp["h3_label"],
            "h2_area":         zero_hp["h2_area"],
            "h2_label":        zero_hp["h2_label"],
            "prev_hour_area":  zero_hp["prev_hour_area"],
            "prev_hour_label": zero_hp["prev_hour_label"],
            "curr_hour_area":  zero_hp["curr_hour_area"],
            "curr_hour_label": zero_hp["curr_hour_label"],
            "has_downtime":    True,
            "downtime":        downtime_info,
            # Пометка для фронтенда/отладки: у водителя сейчас нет активного
            # задания в Cropwise, строка построена только по данным простоя.
            "no_active_task":  True,
        }

    for driver_id in downtime_driver_ids - driver_ids_with_task:
        downtime_info = _build_downtime_info(driver_id)
        if not downtime_info:
            continue
        rows.append(_orphan_row(driver_id, downtime_info))

    # Сортировка: без простоя (🔴) → с простоем (🟡), внутри — по убыванию day_area
    rows.sort(key=lambda r: (r["has_downtime"], -r["day_area"]))

    update_cache(rows, now)
    logger.info(
        f"_update_low_productivity_cache: {len(rows)} drivers with day_area > 0.1 га "
        f"(shift from {shift_start.strftime('%H:%M')} МСК)"
    )


# =============================================================================
# КЭШИРОВАНИЕ ИСТОРИИ СМЕН (вчера 06-18 / вчера 18 - сегодня 06)
# =============================================================================

async def _update_shift_history_cache() -> None:
    """
    Считает производительность водителей за два прошедших окна:
      prev_day   — вчера 06:00–18:00 МСК (дневная смена)
      prev_night — вчера 18:00 – сегодня 06:00 МСК (ночная смена)

    В отличие от _update_low_productivity_cache (только активные сейчас
    задачи), здесь берутся ВСЕ задачи, пересекающиеся с этими окнами,
    независимо от того, активен ли водитель прямо сейчас.

    Все три окна (prev_day, prev_night, today_day) пересчитываются на
    КАЖДОМ цикле sync (раз в 15 минут) — без суточного кэширования.
    Раньше prev_day пересчитывался только при смене даты, что приводило
    к тому, что вкладки истории не обновлялись постоянно.
    """
    from sqlalchemy import select

    from app.cropwise.low_productivity_router import update_shift_history_cache
    from app.downtime.models import ActiveDowntime, Downtime
    from app.driver_stops.models import CropwiseStop
    from app.users.models import User
    from app.venchies.models import Vehicle

    MIN_AREA_HA     = 0.1

    now = datetime.now(tz=TZ_MSK)

    # "Сегодняшние" сутки начинаются в 06:00 МСК. До 06:00 текущие сутки
    # считаются ещё "вчерашними".
    today_start = now.replace(hour=6, minute=0, second=0, microsecond=0)
    if now.hour < 6:
        today_start -= timedelta(days=1)

    prev_day_start   = today_start - timedelta(days=1)               # вчера 06:00
    prev_day_end     = prev_day_start + timedelta(hours=12)           # вчера 18:00
    prev_night_start = prev_day_end                                   # вчера 18:00
    prev_night_end   = today_start                                   # сегодня 06:00
    today_day_start  = today_start                                   # сегодня 06:00
    today_day_end    = today_day_start + timedelta(hours=12)          # сегодня 18:00

    for_date = prev_day_start.strftime("%Y-%m-%d")

    prev_day_label   = f"{prev_day_start.strftime('%d.%m.')} {prev_day_start.strftime('%H:%M')} - {prev_day_end.strftime('%d.%m.')} {prev_day_end.strftime('%H:%M')}"
    prev_night_label = f"{prev_night_start.strftime('%d.%m.')} {prev_night_start.strftime('%H:%M')} - {prev_night_end.strftime('%d.%m.')} {prev_night_end.strftime('%H:%M')}"
    today_day_label  = f"{today_day_start.strftime('%d.%m.')} {today_day_start.strftime('%H:%M')} - {today_day_end.strftime('%d.%m.')} {today_day_end.strftime('%H:%M')}"

    window_start = prev_day_start
    window_end   = today_day_end

    window_start_str = window_start.strftime("%Y-%m-%dT%H:%M:%S")
    window_end_str   = window_end.strftime("%Y-%m-%dT%H:%M:%S")

    import asyncio as _asyncio

    # ── Задачи, пересекающиеся с [prev_day_start, today_day_end) ─────────────
    # ВАЖНО: используем ДВА запроса и объединяем результат. Фильтр
    # end_time_gt_eq + start_time_lt_eq в Cropwise может терять задачи,
    # у которых end_time ещё не закрыт (задача активна прямо сейчас) —
    # такие "открытые" задачи самые важные для "Сегодня, день" в начале/
    # середине смены. Второй запрос берёт все задачи, СТАРТОВАВШИЕ в
    # пределах общего окна, независимо от состояния end_time.
    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS) as session:
        tasks_by_range, tasks_by_start, work_types_raw = await _asyncio.gather(
            _fetch_all_pages(session, "machine_tasks", {
                "end_time_gt_eq":   window_start_str,
                "start_time_lt_eq": window_end_str,
            }),
            _fetch_all_pages(session, "machine_tasks", {
                "start_time_gt_eq": window_start_str,
                "start_time_lt_eq": window_end_str,
            }),
            _fetch_all_pages(session, "work_types"),
        )

    tasks_raw_by_id: dict[int, dict] = {}
    for t in (tasks_by_range + tasks_by_start):
        tid = t.get("id")
        if tid is not None:
            tasks_raw_by_id[tid] = t
    tasks_raw = list(tasks_raw_by_id.values())

    work_type_dict: dict[int, str] = {
        wt["id"]: localize_work_type(wt.get("name")) for wt in work_types_raw
    }

    # Берём по одной (последней по end_time) задаче на водителя в каждом из
    # окон. Если у водителя несколько задач, попавших в окно — суммируем
    # площадь по всем, но метаданные (машина/вид работ) берём из самой
    # длинной/последней задачи.
    relevant_tasks: list[dict] = []
    for t in tasks_raw:
        driver_id = t.get("driver_id")
        tid       = t.get("id")
        if not driver_id or not tid:
            continue
        relevant_tasks.append(t)

    if not relevant_tasks:
        update_shift_history_cache([], [], [], now, for_date, prev_day_label, prev_night_label, today_day_label)
        logger.info(f"_update_shift_history_cache: no tasks for {for_date}")
        return

    relevant_task_ids: set[int] = {t["id"] for t in relevant_tasks}

    # ── MTFMI: один(-два) пагинированных запроса /changes вместо одного ──────
    # запроса на КАЖДЫЙ task_id (при 300-800+ задачах за 2 часа, по вашей же
    # диагностике, за 36-часовое окно раньше уходили тысячи HTTP-запросов).
    #
    # /changes фильтрует по updated_at, а не по machine_task_id, поэтому:
    #  1) берём окно с запасом НАЗАД (на случай, если Cropwise досчитывает
    #     MTFMI не мгновенно, а с задержкой) — запас настраивается ниже;
    #  2) результат фильтруем на своей стороне по relevant_task_ids, так что
    #     лишний запас окна не добавляет шума, только подстраховку по полноте.
    MTFMI_CHANGES_LOOKBACK_BUFFER = timedelta(hours=12)
    changes_gt_eq = (window_start - MTFMI_CHANGES_LOOKBACK_BUFFER).strftime("%Y-%m-%dT%H:%M:%S")
    changes_lt    = (now + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")

    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS) as session:
        changed_mtfmi = await _fetch_changes(
            session, "machine_task_field_mapping_items", changes_gt_eq, changes_lt,
        )

    mtfmi_map: dict[int, list[dict]] = {}
    for item in changed_mtfmi:
        tid = item.get("machine_task_id")
        if tid in relevant_task_ids:
            mtfmi_map.setdefault(tid, []).append(item)

    logger.info(
        f"_update_shift_history_cache: /changes вернул {len(changed_mtfmi)} MTFMI-записей "
        f"за [{changes_gt_eq}, {changes_lt}), из них relevant={sum(len(v) for v in mtfmi_map.values())} "
        f"по {len(mtfmi_map)}/{len(relevant_task_ids)} задачам "
        f"(вместо {len(relevant_task_ids)} поштучных запросов)"
    )

    # ── Парсер дат (как в _update_low_productivity_cache) ────────────────────
    def _parse_dt(s):
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
        return dt.replace(tzinfo=timezone.utc).astimezone(TZ_MSK) if dt.tzinfo is None else dt.astimezone(TZ_MSK)

    def _area_in_window(items_for_task: list[dict], win_start: datetime, win_end: datetime) -> float:
        total = 0.0
        for item in items_for_task:
            for entry in (item.get("covered_area_hourly") or []):
                h = _parse_dt(entry[0])
                if h is None:
                    continue
                if win_start <= h < win_end:
                    total += float(entry[1] or 0)
        return total

    # ── FIO + machine_type/agrofirm по гос. номеру ───────────────────────────
    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS) as session:
        cropwise_users_raw = await _fetch_all_pages(session, "users")
    cropwise_fio_by_id = _build_cropwise_fio_map(cropwise_users_raw)

    async with async_session_maker() as session:
        rows_db = await session.execute(
            select(User.id, User.fio, User.is_active)
        )
        rows_db_list = rows_db.all()
        # ФИО для отображения — по ВСЕМ пользователям, включая деактивированных.
        fio_by_id: dict[int, str] = {row.id: row.fio for row in rows_db_list}
        # Список для поиска простоев ниже — намеренно только активные
        # (см. комментарий у driver_ids чуть ниже).
        active_driver_ids: set[int] = {row.id for row in rows_db_list if row.is_active}

        veh_rows = await session.execute(
            select(Vehicle.id, Vehicle.registration_number, Vehicle.machine_type, Vehicle.agrofirm, Vehicle.legal_company)
        )
        veh_list = veh_rows.all()
        vehicle_by_machine_id: dict[int, dict] = {
            row.id: {
                "registration_number": row.registration_number or "—",
                "machine_type":        row.machine_type or "",
                "agrofirm":            row.agrofirm or row.legal_company or "—",
            }
            for row in veh_list
        }

    # ── Активные + завершённые простои за вчера/сегодня (для обоих окон) ─────
    # ВАЖНО: раньше тут был driver_ids = list({t["driver_id"] for t in
    # relevant_tasks}) — простои искались только для водителей, у которых
    # есть ХОТЬ КАКОЕ-ТО задание Cropwise в общем 36-часовом окне. Водитель,
    # который весь период простоял без единого задания, в эту выборку не
    # попадал, и его простой не подтягивался ни в одну из вкладок истории.
    # Теперь ищем по всем активным пользователям.
    # ВАЖНО: active_driver_ids, а не fio_by_id.keys() — fio_by_id теперь
    # содержит и деактивированных пользователей (нужно для ФИО в отображении),
    # но простои по ним искать не нужно.
    driver_ids = list(active_driver_ids)

    async with async_session_maker() as session:
        active_dt_rows = await session.execute(
            select(ActiveDowntime).where(ActiveDowntime.user_id.in_(driver_ids))
        )
        active_downtimes: dict[int, ActiveDowntime] = {
            row.user_id: row for row in active_dt_rows.scalars().all()
        }

        finished_dt_rows = await session.execute(
            select(Downtime).where(
                Downtime.user_id.in_(driver_ids),
                Downtime.start_date.in_([prev_day_start.date(), today_start.date()]),
            )
        )
        finished_downtimes_by_user: dict[int, list[Downtime]] = {}
        for row in finished_dt_rows.scalars().all():
            finished_downtimes_by_user.setdefault(row.user_id, []).append(row)

        # ── Простои из панели «Простои» (cropwise_stops) ─────────────────────
        # Дополнительный источник: причину вписывает сам водитель на странице
        # «Мои простои из Cropwise». shift_date у cropwise_stops — это сутки
        # 06:00–06:00 (тот же агрономический день, что и prev_day/prev_night
        # окна), поэтому одного значения на "вчера" достаточно для обоих окон,
        # плюс отдельно "сегодня" для today_day.
        cw_stop_rows = await session.execute(
            select(CropwiseStop).where(
                CropwiseStop.driver_user_id.in_(driver_ids),
                CropwiseStop.shift_date.in_([
                    prev_day_start.strftime("%Y-%m-%d"),
                    today_start.strftime("%Y-%m-%d"),
                ]),
                CropwiseStop.reason.is_not(None),
            )
        )
        cropwise_stops_by_user: dict[int, list[CropwiseStop]] = {}
        for row in cw_stop_rows.scalars().all():
            cropwise_stops_by_user.setdefault(row.driver_user_id, []).append(row)

    def _fmt_duration(seconds: int) -> str:
        if seconds <= 0:
            return "0м"
        h = seconds // 3600
        m = (seconds % 3600) // 60
        if h and m:
            return f"{h}ч {m}м"
        if h:
            return f"{h}ч"
        return f"{m}м"

    def _downtime_overlaps_window(dt, win_start: datetime, win_end: datetime) -> bool:
        dt_start = datetime.combine(dt.start_date, dt.start_time, tzinfo=TZ_MSK)
        if getattr(dt, "end_date", None) and getattr(dt, "end_time", None):
            dt_end = datetime.combine(dt.end_date, dt.end_time, tzinfo=TZ_MSK)
        else:
            dt_end = win_end  # активный простой — считаем, что длится по сей момент
        return dt_start < win_end and dt_end > win_start

    def _build_downtime_info(driver_id: int, win_start: datetime, win_end: datetime) -> list[dict]:
        info: list[dict] = []
        # Интервалы, уже учтённые из downtime/active_downtime — чтобы не
        # задвоить простой, если тот же интервал есть и в cropwise_stops.
        existing_intervals: list[tuple[datetime, datetime]] = []

        active_dt = active_downtimes.get(driver_id)
        if active_dt and _downtime_overlaps_window(active_dt, win_start, win_end):
            start_dt = datetime.combine(active_dt.start_date, active_dt.start_time, tzinfo=TZ_MSK)
            existing_intervals.append((start_dt, win_end))
            start_str = f"{active_dt.start_date.strftime('%d.%m')} {active_dt.start_time.strftime('%H:%M')}"
            info.append({
                "reason":   active_dt.reason,
                "start":    start_str,
                "end":      None,
                "duration": None,
                "active":   True,
            })

        for dt in sorted(
            finished_downtimes_by_user.get(driver_id, []),
            key=lambda x: (x.start_date, x.start_time),
        ):
            if not _downtime_overlaps_window(dt, win_start, win_end):
                continue
            start_dt = datetime.combine(dt.start_date, dt.start_time, tzinfo=TZ_MSK)
            end_dt   = datetime.combine(dt.end_date, dt.end_time, tzinfo=TZ_MSK)
            existing_intervals.append((start_dt, end_dt))
            start_str = f"{dt.start_date.strftime('%d.%m')} {dt.start_time.strftime('%H:%M')}"
            end_str   = f"{dt.end_date.strftime('%d.%m')} {dt.end_time.strftime('%H:%M')}"
            info.append({
                "reason":   dt.reason,
                "start":    start_str,
                "end":      end_str,
                "duration": _fmt_duration(dt.duration),
                "active":   False,
            })

        # ── Дополняем данными из панели «Простои» (cropwise_stops), только
        #    интервалы, пересекающиеся с этим окном и ещё не покрытые выше.
        for stop in sorted(cropwise_stops_by_user.get(driver_id, []), key=lambda s: s.stop_start):
            s_start = stop.stop_start.astimezone(TZ_MSK)
            s_end   = stop.stop_end.astimezone(TZ_MSK)
            if not (s_start < win_end and s_end > win_start):
                continue
            if any(s_start < e and s < s_end for s, e in existing_intervals):
                continue
            info.append({
                "reason":   stop.reason,
                "start":    s_start.strftime("%d.%m %H:%M"),
                "end":      s_end.strftime("%d.%m %H:%M"),
                "duration": _fmt_duration(stop.duration_sec),
                "active":   False,
                "source":   "cropwise_stops",
            })
            existing_intervals.append((s_start, s_end))

        return info

    # ── Группировка задач по водителю ────────────────────────────────────────
    tasks_by_driver: dict[int, list[dict]] = {}
    for t in relevant_tasks:
        tasks_by_driver.setdefault(t["driver_id"], []).append(t)

    def _task_overlaps_window(t: dict, win_start: datetime, win_end: datetime) -> bool:
        """Пересекается ли период выполнения задачи [start_time, end_time) с окном смены."""
        s_dt = _parse_dt(t.get("start_time"))
        e_dt = _parse_dt(t.get("end_time"))
        if s_dt is None or e_dt is None:
            return False
        return s_dt < win_end and e_dt > win_start

    def _build_rows_for_window(win_start: datetime, win_end: datetime) -> list[dict]:
        rows: list[dict] = []
        processed_driver_ids: set[int] = set()

        for driver_id, tasks in tasks_by_driver.items():
            # ВАЖНО: tasks_by_driver собран по ОБЩЕМУ объединённому диапазону
            # (вчера 06:00 – сегодня 18:00), одному на все три окна (prev_day /
            # prev_night / today_day). Раньше best_task (а с ним № задания,
            # машина, гос. номер, вид работ) выбирался как самая длинная задача
            # СРЕДИ ВСЕХ задач водителя за все 36 часов — из-за этого, например,
            # длинная вчерашняя задача "перетягивала" на себя метаданные строки
            # в окне "Сегодня, день", хотя площадь (shift_area) считалась верно.
            # Поэтому сначала отбираем только задачи, реально пересекающиеся
            # с текущим окном.
            window_tasks = [t for t in tasks if _task_overlaps_window(t, win_start, win_end)]
            if not window_tasks:
                continue

            # Водитель рассмотрен для этого окна (есть хотя бы одна задача
            # Cropwise) — независимо от того, попадёт ли он дальше в rows.
            # Это нужно, чтобы ниже не задвоить его orphan-строкой.
            processed_driver_ids.add(driver_id)

            shift_area = 0.0
            fuel_total = 0.0
            covered_area_total = 0.0
            best_task  = None
            best_dur   = timedelta(0)

            for t in window_tasks:
                items = mtfmi_map.get(t["id"], [])
                shift_area += _area_in_window(items, win_start, win_end)
                for item in items:
                    fuel_total += float(item.get("fuel_consumption") or 0)
                    covered_area_total += float(item.get("covered_area") or 0)

                s_dt = _parse_dt(t.get("start_time"))
                e_dt = _parse_dt(t.get("end_time"))
                dur  = (e_dt - s_dt) if (s_dt and e_dt) else timedelta(0)
                if dur > best_dur or best_task is None:
                    best_task = t
                    best_dur  = dur

            shift_area = round(shift_area, 2)
            downtime_info = _build_downtime_info(driver_id, win_start, win_end)

            # Фильтр по минимальной площади остаётся (0.1 га), НО не
            # применяется к водителям с зафиксированным за это окно простоем —
            # иначе именно те, кто не намолотил ничего ИЗ-ЗА простоя,
            # выпадали бы из истории смен.
            if shift_area <= MIN_AREA_HA and not downtime_info:
                continue

            work_type_id = best_task.get("work_type_id")
            work_type    = work_type_dict.get(work_type_id, "—") if work_type_id else "—"
            work_prefix  = work_type.lstrip().split(".")[0] if work_type else ""
            if work_prefix in ("7", "8"):
                continue

            machine_id = best_task.get("machine_id") or 0
            veh        = vehicle_by_machine_id.get(machine_id, {})

            rows.append({
                "fio":              _resolve_fio(driver_id, fio_by_id, cropwise_fio_by_id),
                "user_id":          driver_id,
                "task_id":          best_task.get("id"),
                "machine_id":       machine_id,
                "license_plate":    veh.get("registration_number", "—"),
                "machine_type":     veh.get("machine_type", ""),
                "agrofirm":         veh.get("agrofirm", "—"),
                "work_type":        work_type,
                "fuel_consumption": round(fuel_total, 2),
                "covered_area":     round(covered_area_total, 2),
                "shift_area":    shift_area,
                "has_downtime":  bool(downtime_info),
                "downtime":      downtime_info,
                "no_active_task": False,
            })

        # ── Orphan-строки: водители с простоем за это окно, у которых вообще ──
        # не было ни одной задачи Cropwise, пересекающейся с окном (поэтому
        # цикл выше их не рассматривал). Без этого блока такой простой нигде
        # не появляется — ни в "Сегодня", ни в "Вчера, день/ночь".
        downtime_driver_ids = (
            set(active_downtimes.keys())
            | set(finished_downtimes_by_user.keys())
            | set(cropwise_stops_by_user.keys())
        )

        for driver_id in downtime_driver_ids - processed_driver_ids:
            downtime_info = _build_downtime_info(driver_id, win_start, win_end)
            if not downtime_info:
                continue

            active_dt = active_downtimes.get(driver_id)
            finished_list = sorted(
                finished_downtimes_by_user.get(driver_id, []),
                key=lambda x: (x.start_date, x.start_time),
            )
            src = active_dt or (finished_list[-1] if finished_list else None)

            # Если ни в downtime, ни в active_downtime записи нет (простой
            # известен только из cropwise_stops), берём метаданные оттуда.
            cw_stops_list = sorted(cropwise_stops_by_user.get(driver_id, []), key=lambda s: s.stop_start)
            cw_src = cw_stops_list[-1] if cw_stops_list else None

            machine_id = getattr(src, "vehicle_id", None) if src else None
            veh = vehicle_by_machine_id.get(machine_id, {}) if machine_id else {}
            license_plate = (
                (getattr(src, "license_plate", None) if src else None)
                or veh.get("registration_number")
                or (getattr(cw_src, "machine_name", None) if cw_src else None)
                or "—"
            )
            agrofirm = (
                (getattr(src, "agrofirm", None) if src else None)
                or veh.get("agrofirm")
                or (getattr(cw_src, "agrofirm", None) if cw_src else None)
                or "—"
            )
            machine_type = veh.get("machine_type", "")

            rows.append({
                "fio":              fio_by_id.get(driver_id) or (getattr(src, "fio", None) if src else None) or cropwise_fio_by_id.get(driver_id) or _FIO_NOT_FOUND,
                "user_id":          driver_id,
                "task_id":          getattr(src, "machine_task_id", None) if src else None,
                "machine_id":       machine_id or 0,
                "license_plate":    license_plate,
                "machine_type":     machine_type,
                "agrofirm":         agrofirm,
                "work_type":        "—",
                "fuel_consumption": 0.0,
                "covered_area":     0.0,
                "shift_area":    0.0,
                "has_downtime":  True,
                "downtime":      downtime_info,
                "no_active_task": True,
            })

        rows.sort(key=lambda r: (r["has_downtime"], -r["shift_area"]))
        return rows

    def _safe_build(label: str, win_start: datetime, win_end: datetime) -> list[dict]:
        try:
            return _build_rows_for_window(win_start, win_end)
        except Exception as e:
            logger.error(
                f"_update_shift_history_cache: build rows failed for {label}: {e}",
                exc_info=True,
            )
            return []

    prev_day_rows    = _safe_build("prev_day", prev_day_start, prev_day_end)
    prev_night_rows  = _safe_build("prev_night", prev_night_start, prev_night_end)
    today_day_rows   = _safe_build("today_day", today_day_start, today_day_end)

    update_shift_history_cache(
        prev_day_rows,
        prev_night_rows,
        today_day_rows,
        now,
        for_date,
        prev_day_label,
        prev_night_label,
        today_day_label,
    )
    logger.info(
        f"_update_shift_history_cache: prev_day={len(prev_day_rows)} rows "
        f"({prev_day_label}), prev_night={len(prev_night_rows)} rows ({prev_night_label}), "
        f"today_day={len(today_day_rows)} rows ({today_day_label})"
    )
