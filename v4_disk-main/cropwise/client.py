"""
cropwise/client.py
~~~~~~~~~~~~~~~~~~
Асинхронный клиент к Cropwise Operations Platform API v3.
"""

import aiohttp
import asyncio
import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from app.logging_config import get_logger

logger = get_logger("cropwise.client")

CROPWISE_API_TOKEN: str = "6DayNcBDr9BWEZgst1Dy"
CROPWISE_BASE_URL:  str = "https://operations.cropwise.com/api/v3"

CROPWISE_HEADERS = {
    "X-User-Api-Token": CROPWISE_API_TOKEN,
    "Content-Type":     "application/json",
}


def cropwise_season_years_for_range(date_from: date, date_to: date) -> list[int]:
    """Годы season/year для запросов Cropwise API по календарному диапазону.

    Cropwise помечает agro_operations, machine_tasks и history_items полем
    season по агрономическому циклу — обычно это год ЗАВЕРШЕНИЯ цикла.
    Работы весной–летом календарного года Y имеют season=Y+1 (как SEASON_YEAR
    в сводках 2/9). Наивный ``range(date_from.year, …)`` без Y+1 отсекает
    большую часть операций; запрос и Y, и Y+1 для весеннего диапазона в одном
    календарном году даёт лишние операции с season=Y.

    Правило:
      — всегда включаем date_to.year + 1 (основной агросезон);
      — calendar year(s) из диапазона — только если диапазон пересекает
        границу календарных лет или начинается осенью (month >= 9).
    """
    years: set[int] = {date_to.year + 1}
    if date_from.year != date_to.year:
        years.update(range(date_from.year, date_to.year + 1))
    elif date_from.month >= 9:
        years.add(date_from.year)
    return sorted(years)


async def _fetch_history_items_for_date_range(
    session: aiohttp.ClientSession,
    date_from: date,
    date_to: date,
    *,
    sem: asyncio.Semaphore | None = None,
) -> list[dict]:
    """history_items для всех cropwise-сезонов, покрывающих диапазон дат."""
    years = cropwise_season_years_for_range(date_from, date_to)
    if not years:
        return []
    pages = await asyncio.gather(*[
        _fetch_all_pages(session, "history_items", {"year": str(yr)}, sem=sem)
        for yr in years
    ])
    return [hi for page in pages for hi in page]


ALLOWED_SUBTYPES: frozenset[str] = frozenset({
    "autobus",
    "buldozer",
    "car",
    "excavator",
    "fuel_bowser",
    "harvester",
    "lorrie",
    "minibus",
    "motor_grader",
    "other",
    "semi_truck",
    "sprayer",
    "spreader",
    "telehandler",
    "tractor",
    "tipper",
    "maintenance",
    "truck_crane",
})

MACHINE_SUBTYPE_LABELS: dict[str, str] = {
    "autobus":      "Автобус",
    "buldozer":     "Бульдозер",
    "car":          "Легковой автомобиль",
    "excavator":    "Экскаватор",
    "fuel_bowser":  "Топливозаправщик",
    "harvester":    "Комбайн",
    "lorrie":       "Грузовик",
    "minibus":      "Микроавтобус",
    "motor_grader": "Грейдер",
    "other":        "Другое",
    "semi_truck":   "Седельный тягач",
    "sprayer":      "Опрыскиватель",
    "spreader":     "Разбрасыватель",
    "telehandler":  "Телескопический погрузчик",
    "tractor":      "Трактор",
    "tipper":       "Самосвал",
    "maintenance":  "Обслуживание",
    "truck_crane":  "Автокран",
}

WORK_TYPE_LABELS: dict[str, str] = {
    "spraying":    "Опрыскивание",
    "discing":     "Дискование",
    "plowing":     "Вспашка",
    "cultivation": "Культивация",
    "subsoiling":  "Чизелевание",
    "sowing":      "Посев",
    "over_sowing": "Подсев",
    "spreading":   "Разбрасывание",
    "harrowing":   "Боронование",
    "rolling":     "Прикатывание",
    "harvesting":  "Уборка",
    "transport":   "Транспортировка",
    "service":     "Сервис",
    "other":       "Прочее",
}


def localize_work_type(name: str | None) -> str:
    if not name:
        return "—"
    return WORK_TYPE_LABELS.get(name.lower(), name)


# Таймаут на один HTTP-запрос к Cropwise
_CROPWISE_TIMEOUT = aiohttp.ClientTimeout(
    total=120,        # максимум 2 мин на полный цикл запрос→ответ
    connect=15,       # установка TCP-соединения
    sock_read=90,     # чтение тела ответа
)

# Семафор: не более 20 параллельных запросов к Cropwise из одного места вызова.
# Каждый вызывающий код может создать свой семафор; этот — дефолтный fallback.
_DEFAULT_SEM = asyncio.Semaphore(20)


async def _fetch_all_pages(
    session: aiohttp.ClientSession,
    endpoint: str,
    params: dict | None = None,
    *,
    sem: asyncio.Semaphore | None = None,
    retries: int = 6,
) -> list:
    """Постраничная выборка с retry при TimeoutError/ServerError/ConnectionError.

    Parameters
    ----------
    sem:
        Семафор для ограничения параллелизма.  Если не передан — используется
        глобальный _DEFAULT_SEM (20 параллельных запросов).
    retries:
        Количество повторных попыток при временных ошибках (timeout, 5xx,
        обрыв соединения). Раньше было 3 попытки с бэкоффом 1/2/4с (~7с
        суммарно) — этого достаточно для случайного сетевого «моргания»,
        но недостаточно, если внешний Cropwise API временно медленнее или
        менее стабилен продолжительное время (например, ночью, во время
        его собственных фоновых/технических процессов). Теперь 6 попыток
        с бэкоффом до 32с (~63с суммарно на страницу), что даёт больше
        шансов дождаться восстановления, не проваливая всю задачу.
    """
    _sem = sem if sem is not None else _DEFAULT_SEM
    params = dict(params or {})
    params["limit"] = 1000
    results: list = []
    from_id = 0

    while True:
        params["from_id"] = from_id
        url = f"{CROPWISE_BASE_URL}/{endpoint}"

        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                async with _sem:
                    async with session.get(url, params=params) as resp:
                        resp.raise_for_status()
                        body = await resp.json()
                break  # успех — выходим из retry-цикла
            except (aiohttp.ServerTimeoutError, asyncio.TimeoutError, TimeoutError) as exc:
                last_exc = exc
                wait = min(2 ** attempt, 32)  # 1s, 2s, 4s, 8s, 16s, 32s
                logger.warning(
                    f"_fetch_all_pages timeout [{endpoint}] attempt {attempt+1}/{retries}, "
                    f"retry in {wait}s: {exc}"
                )
                await asyncio.sleep(wait)
            except (
                aiohttp.ClientConnectorError,
                aiohttp.ClientOSError,
                aiohttp.ServerDisconnectedError,
                aiohttp.ClientPayloadError,
            ) as exc:
                # Обрыв/отказ соединения (не просто медленный ответ) — раньше
                # это НЕ перехватывалось вообще и роняло задачу с первой же
                # попытки. Именно такой сбой вероятнее всего объясняет, почему
                # обновление не проходило ночью: Cropwise временно не принимал
                # соединения, а не просто отвечал медленнее.
                last_exc = exc
                wait = min(2 ** attempt, 32)
                logger.warning(
                    f"_fetch_all_pages connection error [{endpoint}] "
                    f"attempt {attempt+1}/{retries}, retry in {wait}s: {exc}"
                )
                await asyncio.sleep(wait)
            except aiohttp.ClientResponseError as exc:
                if exc.status and (exc.status >= 500 or exc.status == 429):
                    last_exc = exc
                    wait = min(2 ** attempt, 32)
                    logger.warning(
                        f"_fetch_all_pages server error {exc.status} [{endpoint}] "
                        f"attempt {attempt+1}/{retries}, retry in {wait}s"
                    )
                    await asyncio.sleep(wait)
                else:
                    raise  # 4xx (кроме 429) — не ретраим, это не временный сбой
        else:
            # Все попытки исчерпаны
            raise RuntimeError(
                f"_fetch_all_pages failed after {retries} attempts [{endpoint}]"
            ) from last_exc

        records = body.get("data", [])
        if not records:
            break
        results.extend(records)
        meta     = body.get("meta", {}).get("response", {})
        obtained = meta.get("obtained_records", 0)
        limit    = meta.get("limit", 1000)
        if obtained < limit:
            break
        from_id = meta.get("last_record_id", 0) + 1

    return results


_WINDOW_FMT = "%Y-%m-%dT%H:%M:%S"


def make_created_at_windows(
    start: date,
    step_days: int = 14,
    *,
    until: datetime | None = None,
) -> list[dict]:
    """Разбиение всей оси времени на окна по created_at.

    Окна образуют ТОЧНОЕ разбиение: первое открыто слева (всё, что создано
    раньше start), последнее открыто справа (всё, что создано позже
    последней границы), соседние окна делят одну и ту же строку границы
    ([gt_eq=B, lt=B_next)). Поэтому, как бы сервер ни трактовал часовой
    пояс строки, записи не теряются и не дублируются на стыках.

    Фильтры created_at_gt_eq / created_at_lt на machine_tasks и
    machine_task_agro_operation_mapping_items проверены на реальном API
    (в отличие от синтаксиса created_at[from], который сервер игнорирует).
    created_at не меняется после создания записи — в отличие от start_time,
    который можно править во время выгрузки.
    """
    if step_days < 1:
        raise ValueError("step_days must be >= 1")
    end = (until or datetime.now()) + timedelta(days=2)
    cur = datetime(start.year, start.month, start.day)
    bounds: list[str] = []
    while cur < end:
        bounds.append(cur.strftime(_WINDOW_FMT))
        cur += timedelta(days=step_days)
    windows: list[dict] = [{"lt": bounds[0]}]
    windows += [{"gt_eq": a, "lt": b} for a, b in zip(bounds, bounds[1:])]
    windows.append({"gt_eq": bounds[-1]})
    return windows


async def _fetch_windowed(
    session: aiohttp.ClientSession,
    endpoint: str,
    windows: list[dict],
    params: dict | None = None,
    *,
    project=None,
    sem: asyncio.Semaphore | None = None,
    retries: int = 6,
    label: str = "",
) -> list:
    """Параллельная выгрузка endpoint по окнам created_at (см. make_created_at_windows).

    Каждое окно — свой курсор from_id внутри _fetch_all_pages (с retry);
    общий параллелизм ограничен семафором sem.

    project(records) -> list применяется к каждому окну СРАЗУ после его
    загрузки: сырые объекты не копятся (у machine_tasks ~20-30 полей на
    запись, 300+ тысяч записей целиком в памяти не нужны).

    Ошибка в любом окне отменяет остальные и пробрасывается наружу — молча
    неполный результат хуже явного падения.
    """
    _sem = sem if sem is not None else _DEFAULT_SEM
    total = len(windows)
    done = 0

    async def _one(w: dict) -> list:
        nonlocal done
        p = dict(params or {})
        if "gt_eq" in w:
            p["created_at_gt_eq"] = w["gt_eq"]
        if "lt" in w:
            p["created_at_lt"] = w["lt"]
        recs = await _fetch_all_pages(session, endpoint, p, sem=_sem, retries=retries)
        part = project(recs) if project is not None else recs
        done += 1
        if done % 10 == 0 or done == total:
            logger.info(f"_fetch_windowed[{label or endpoint}]: окон {done}/{total}")
        return part

    tasks = [asyncio.ensure_future(_one(w)) for w in windows]
    try:
        parts = await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return [x for part in parts for x in part]


# Cropwise сравнивает hour_start / start_intersection как время с поясом.
# Без «+03:00» наивная строка читается как UTC и окно съезжает на 3 часа.
_CROPWISE_TZ = timezone(timedelta(hours=3))


def format_cropwise_dt(dt: datetime) -> str:
    """Метка для фильтров Cropwise: локальное время площадки с явным +03:00."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_CROPWISE_TZ)
    else:
        dt = dt.astimezone(_CROPWISE_TZ)
    return dt.strftime("%Y-%m-%dT%H:%M:%S+03:00")


def parse_moi_dt(value) -> datetime | None:
    """Время из ответа machine_object_intersections → МСК. Пустое и null — None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "null":
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_CROPWISE_TZ)
    return dt.astimezone(_CROPWISE_TZ)


def moi_row_to_interval(
    row: dict,
    *,
    open_end: datetime | None = None,
) -> tuple[int, int, datetime, datetime] | None:
    """(machine_id, object_id, start, end).

    Незакрытое пересечение (end_intersection пустой) дотягивается до open_end.
    Без open_end такая строка отбрасывается — иначе нельзя понять, когда машина уехала.
    """
    try:
        mid = int(row["machine_id"])
        oid = int(row.get("object_id") if row.get("object_id") is not None else row["intersected_object_id"])
    except (KeyError, TypeError, ValueError):
        return None
    start = parse_moi_dt(row.get("start_intersection")) or parse_moi_dt(row.get("hour_start"))
    if start is None:
        return None
    end = parse_moi_dt(row.get("end_intersection"))
    if end is None:
        if open_end is None:
            return None
        end = open_end if open_end.tzinfo else open_end.replace(tzinfo=_CROPWISE_TZ)
    if end <= start:
        return None
    return mid, oid, start, end


async def _fetch_machine_object_intersections(
    session: aiohttp.ClientSession,
    *,
    object_type: str,
    hour_from: datetime,
    hour_to: datetime,
    machine_ids: list[int] | None = None,
    object_ids: list[int] | None = None,
    sem: asyncio.Semaphore | None = None,
) -> list[dict]:
    """Пересечения машина × объект (machine_object_intersections).

    Пагинация — from_id внутри _fetch_all_pages. Окно задаётся по hour_start
    (час, округлённый вниз): hour_start_gt_eq … hour_start_lt.

    object_ids — фильтр API принимает одно object_id за запрос. Если заданы и
    object_ids, и machine_ids, один общий проход пагинации за окно + отбор на
    клиенте (иначе N полей × пагинация — очень медленно). Только object_ids без
    machine_ids — по-прежнему запрос на каждый object_id (узкий срез по объекту).
    Без object_ids — всё object_type за окно; machine_ids режут результат здесь.
    """
    base = {
        "object_type": object_type,
        "hour_start_gt_eq": format_cropwise_dt(hour_from),
        "hour_start_lt": format_cropwise_dt(hour_to),
    }
    machine_set = {int(i) for i in machine_ids} if machine_ids is not None else None

    async def _one(extra: dict) -> list:
        return await _fetch_all_pages(
            session, "machine_object_intersections", {**base, **extra}, sem=sem,
        )

    def _row_object_id(row: dict) -> int | None:
        raw = row.get("object_id")
        if raw is None:
            raw = row.get("intersected_object_id")
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    if object_ids:
        uniq = list(dict.fromkeys(int(i) for i in object_ids))
        if not uniq:
            return []
        field_set = set(uniq)
        if machine_set is not None:
            rows = await _one({})
            rows = [
                row for row in rows
                if row.get("machine_id") in machine_set
                and _row_object_id(row) in field_set
            ]
        else:
            batches = await asyncio.gather(*[_one({"object_id": oid}) for oid in uniq])
            rows = [row for batch in batches for row in batch]
    else:
        rows = await _one({})

    if machine_set is not None and not object_ids:
        rows = [row for row in rows if row.get("machine_id") in machine_set]
    return rows


async def _fetch_mass_request(
    session: aiohttp.ClientSession,
    endpoint: str,
    ids: list[int],
    *,
    sem: asyncio.Semaphore | None = None,
    retries: int = 6,
    chunk_size: int = 500,
) -> list:
    """Массовая выборка объектов по списку их СОБСТВЕННЫХ id через
    POST /api/v3/{endpoint}/mass_request (см. документацию Cropwise:
    "JSON format for requesting of big number of objects").

    Работает только когда известны id самих объектов ресурса (например,
    id записей machine_tasks) — а не значения внешнего ключа (для фильтра
    по внешнему ключу используется обычный _fetch_all_pages с параметром).

    Заметно быстрее, чем один GET-запрос на каждый id: вместо N HTTP-
    запросов делается ceil(N / chunk_size) запросов, выполняемых
    параллельно (с ограничением семафором).

    Parameters
    ----------
    chunk_size:
        Сколько id отправлять в одном POST-запросе. Cropwise явно не
        документирует верхний предел для mass_request, поэтому используем
        консервативный размер порции, чтобы не упереться в лимит на
        стороне сервера.
    """
    _sem = sem if sem is not None else _DEFAULT_SEM
    uniq_ids = list(dict.fromkeys(int(i) for i in ids if i is not None))
    if not uniq_ids:
        return []

    async def _one_chunk(chunk: list[int]) -> list:
        url = f"{CROPWISE_BASE_URL}/{endpoint}/mass_request"
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                async with _sem:
                    async with session.post(url, json={"data": chunk}) as resp:
                        resp.raise_for_status()
                        body = await resp.json()
                return body.get("data", [])
            except (aiohttp.ServerTimeoutError, asyncio.TimeoutError, TimeoutError) as exc:
                last_exc = exc
                wait = min(2 ** attempt, 32)
                logger.warning(
                    f"_fetch_mass_request timeout [{endpoint}] attempt {attempt+1}/{retries}, "
                    f"retry in {wait}s: {exc}"
                )
                await asyncio.sleep(wait)
            except (
                aiohttp.ClientConnectorError,
                aiohttp.ClientOSError,
                aiohttp.ServerDisconnectedError,
                aiohttp.ClientPayloadError,
            ) as exc:
                last_exc = exc
                wait = min(2 ** attempt, 32)
                logger.warning(
                    f"_fetch_mass_request connection error [{endpoint}] "
                    f"attempt {attempt+1}/{retries}, retry in {wait}s: {exc}"
                )
                await asyncio.sleep(wait)
            except aiohttp.ClientResponseError as exc:
                if exc.status and (exc.status >= 500 or exc.status == 429):
                    last_exc = exc
                    wait = min(2 ** attempt, 32)
                    logger.warning(
                        f"_fetch_mass_request server error {exc.status} [{endpoint}] "
                        f"attempt {attempt+1}/{retries}, retry in {wait}s"
                    )
                    await asyncio.sleep(wait)
                else:
                    raise  # 4xx (кроме 429) — не ретраим
        raise RuntimeError(
            f"_fetch_mass_request failed after {retries} attempts [{endpoint}]"
        ) from last_exc

    chunks = [uniq_ids[i:i + chunk_size] for i in range(0, len(uniq_ids), chunk_size)]
    pages = await asyncio.gather(*[_one_chunk(c) for c in chunks])
    return [rec for page in pages for rec in page]


# Сколько machine_task_id передавать в одном GET к MTFMI (через запятую).
# Альтернатива mass_request: фильтр по внешнему ключу machine_task_id, а не
# по id самих mapping_items (см. check12_season_mismatch.py).
MTFMI_TASK_BATCH_SIZE = 300

# Сколько work_type_id передавать в одном GET machine_tasks (через запятую).
MT_WT_ID_BATCH_SIZE = max(
    1,
    int(os.environ.get("CROPWISE_MACHINE_TASKS_WT_BATCH_SIZE", "25") or 25),
)


def _filter_wt_batch_rows(
    rows: list[dict],
    *,
    season: int | str | None,
    wt_ids: set[int] | None,
) -> list[dict]:
    """Post-filter после batch work_type_id: API иногда игнорирует season/wt."""
    if season is None and not wt_ids:
        return rows

    out: list[dict] = []
    for row in rows:
        if season is not None:
            raw = row.get("season")
            if raw is not None and str(raw) != str(season):
                continue

        if wt_ids:
            try:
                wt = int(row.get("work_type_id"))
            except (TypeError, ValueError):
                continue
            if wt not in wt_ids:
                continue

        out.append(row)
    return out


async def _fetch_machine_tasks_by_wt(
    session: aiohttp.ClientSession,
    wt_ids: list[int] | set[int],
    base_params: dict,
    *,
    sem: asyncio.Semaphore | None = None,
    batch_size: int = MT_WT_ID_BATCH_SIZE,
    log_prefix: str = "",
) -> list[dict]:
    """machine_tasks с фильтром work_type_id на стороне API (batch + fallback).

    Сначала batch work_type_id=1,2,3,...; при пустом ответе — по одному id
    на запрос.
    """
    if not wt_ids:
        return []

    wt_list = sorted({int(w) for w in wt_ids})
    _sem = sem if sem is not None else _DEFAULT_SEM
    prefix = log_prefix or ""
    all_tasks: list[dict] = []
    failed_batches: list[list[int]] = []

    async def _fetch_one(wt_id: int) -> list[dict]:
        return await _fetch_all_pages(
            session,
            "machine_tasks",
            {**base_params, "work_type_id": wt_id},
            sem=_sem,
        )

    async def _fetch_batch(batch: list[int]) -> list[dict]:
        return await _fetch_all_pages(
            session,
            "machine_tasks",
            {**base_params, "work_type_id": ",".join(str(w) for w in batch)},
            sem=_sem,
        )

    wt_set = set(wt_list)
    season = base_params.get("season")

    if len(wt_list) == 1:
        rows = await _fetch_one(wt_list[0])
        return _filter_wt_batch_rows(rows, season=season, wt_ids=wt_set)

    batches = [wt_list[i:i + batch_size] for i in range(0, len(wt_list), batch_size)]
    if len(wt_list) > 1:
        logger.info(
            f"{prefix}machine_tasks: {len(wt_list)} work types "
            f"({len(batches)} batch), params={base_params}"
        )

    batch_results = await asyncio.gather(
        *[_fetch_batch(batch) for batch in batches],
        return_exceptions=True,
    )
    for batch, res in zip(batches, batch_results):
        if isinstance(res, BaseException):
            logger.warning(
                f"{prefix}machine_tasks batch wt (n={len(batch)}) failed: {res}"
            )
            failed_batches.append(batch)
        else:
            all_tasks.extend(res)

    if not all_tasks:
        logger.info(
            f"{prefix}machine_tasks: batch work_type_id filter returned 0, "
            "fallback to one request per work_type_id"
        )
        per_wt = await asyncio.gather(*[_fetch_one(wid) for wid in wt_list])
        for chunk in per_wt:
            all_tasks.extend(chunk)
    elif failed_batches:
        retry_ids = [wid for batch in failed_batches for wid in batch]
        retry_chunks = await asyncio.gather(*[_fetch_one(wid) for wid in retry_ids])
        for chunk in retry_chunks:
            all_tasks.extend(chunk)

    all_tasks = _filter_wt_batch_rows(all_tasks, season=season, wt_ids=wt_set)

    by_id: dict[int, dict] = {}
    for row in all_tasks:
        tid = row.get("id")
        if tid is not None:
            by_id[int(tid)] = row
    return list(by_id.values())


async def _fetch_agro_operations_by_wt(
    session: aiohttp.ClientSession,
    wt_ids: list[int] | set[int],
    base_params: dict,
    *,
    sem: asyncio.Semaphore | None = None,
    batch_size: int = MT_WT_ID_BATCH_SIZE,
    log_prefix: str = "",
) -> list[dict]:
    """agro_operations с фильтром work_type_id на стороне API (batch + fallback)."""
    if not wt_ids:
        return []

    wt_list = sorted({int(w) for w in wt_ids})
    _sem = sem if sem is not None else _DEFAULT_SEM
    prefix = log_prefix or ""
    all_ops: list[dict] = []
    failed_batches: list[list[int]] = []

    async def _fetch_one(wt_id: int) -> list[dict]:
        return await _fetch_all_pages(
            session,
            "agro_operations",
            {**base_params, "work_type_id": wt_id},
            sem=_sem,
        )

    async def _fetch_batch(batch: list[int]) -> list[dict]:
        return await _fetch_all_pages(
            session,
            "agro_operations",
            {**base_params, "work_type_id": ",".join(str(w) for w in batch)},
            sem=_sem,
        )

    wt_set = set(wt_list)
    season = base_params.get("season")

    if len(wt_list) == 1:
        rows = await _fetch_one(wt_list[0])
        return _filter_wt_batch_rows(rows, season=season, wt_ids=wt_set)

    batches = [wt_list[i:i + batch_size] for i in range(0, len(wt_list), batch_size)]
    logger.info(
        f"{prefix}agro_operations: {len(wt_list)} work types "
        f"({len(batches)} batch), params={base_params}"
    )

    batch_results = await asyncio.gather(
        *[_fetch_batch(batch) for batch in batches],
        return_exceptions=True,
    )
    for batch, res in zip(batches, batch_results):
        if isinstance(res, BaseException):
            logger.warning(
                f"{prefix}agro_operations batch wt (n={len(batch)}) failed: {res}"
            )
            failed_batches.append(batch)
        else:
            all_ops.extend(res)

    if not all_ops:
        logger.info(
            f"{prefix}agro_operations: batch work_type_id filter returned 0, "
            "fallback to one request per work_type_id"
        )
        per_wt = await asyncio.gather(*[_fetch_one(wid) for wid in wt_list])
        for chunk in per_wt:
            all_ops.extend(chunk)
    elif failed_batches:
        retry_ids = [wid for batch in failed_batches for wid in batch]
        retry_chunks = await asyncio.gather(*[_fetch_one(wid) for wid in retry_ids])
        for chunk in retry_chunks:
            all_ops.extend(chunk)

    all_ops = _filter_wt_batch_rows(all_ops, season=season, wt_ids=wt_set)

    by_id: dict[int, dict] = {}
    for row in all_ops:
        oid = row.get("id")
        if oid is not None:
            by_id[int(oid)] = row
    return list(by_id.values())


async def _fetch_mtfmi_by_task_ids(
    session: aiohttp.ClientSession,
    task_ids: list[int],
    *,
    batch_size: int = MTFMI_TASK_BATCH_SIZE,
    sem: asyncio.Semaphore | None = None,
) -> dict[int, list[dict]]:
    """machine_task_field_mapping_items для списка machine_task_id пачками.

    Вместо N отдельных GET (?machine_task_id=123) — ceil(N/batch_size)
    запросов с machine_task_id=1,2,3,... (как в coefficient_api/calc.py).
    """
    uniq = list(dict.fromkeys(int(t) for t in task_ids if t is not None))
    mtfmi_map: dict[int, list[dict]] = {tid: [] for tid in uniq}
    if not uniq:
        return mtfmi_map

    _sem = sem if sem is not None else _DEFAULT_SEM
    batches = [uniq[i:i + batch_size] for i in range(0, len(uniq), batch_size)]

    async def _one_batch(batch: list[int]) -> list[dict]:
        return await _fetch_all_pages(
            session,
            "machine_task_field_mapping_items",
            {"machine_task_id": ",".join(str(t) for t in batch)},
            sem=_sem,
        )

    for chunk in await asyncio.gather(*[_one_batch(b) for b in batches]):
        for item in chunk:
            tid_raw = item.get("machine_task_id")
            if tid_raw is not None:
                mtfmi_map.setdefault(int(tid_raw), []).append(item)
    return mtfmi_map


async def _fetch_task_agro_op_links_by_task_ids(
    session: aiohttp.ClientSession,
    task_ids: list[int],
    *,
    batch_size: int = MTFMI_TASK_BATCH_SIZE,
    sem: asyncio.Semaphore | None = None,
) -> dict[int, list[int]]:
    """machine_task_agro_operation_mapping_items → {task_id: [agro_operation_id]}.

    Batch GET с machine_task_id=1,2,3,... (как MTFMI и 2_main_ideal_compact.py),
    вместо одного GET на каждую задачу. При падении batch — fallback поштучно.
    """
    uniq = list(dict.fromkeys(int(t) for t in task_ids if t is not None))
    result: dict[int, list[int]] = {tid: [] for tid in uniq}
    if not uniq:
        return result

    _sem = sem if sem is not None else _DEFAULT_SEM
    batches = [uniq[i:i + batch_size] for i in range(0, len(uniq), batch_size)]
    endpoint = "machine_task_agro_operation_mapping_items"

    async def _one_batch(batch: list[int]) -> list[dict]:
        return await _fetch_all_pages(
            session,
            endpoint,
            {"machine_task_id": ",".join(str(t) for t in batch)},
            sem=_sem,
        )

    async def _one_task(tid: int) -> list[dict]:
        return await _fetch_all_pages(
            session,
            endpoint,
            {"machine_task_id": tid},
            sem=_sem,
        )

    batch_results = await asyncio.gather(
        *[_one_batch(b) for b in batches],
        return_exceptions=True,
    )

    failed_ids: list[int] = []
    for batch, res in zip(batches, batch_results):
        if isinstance(res, BaseException):
            logger.warning(
                f"mtaomi batch failed ({len(batch)} tasks), fallback per-task: {res}"
            )
            failed_ids.extend(batch)
            continue
        for item in res:
            tid_raw = item.get("machine_task_id")
            aoid_raw = item.get("agro_operation_id")
            if tid_raw is None or aoid_raw is None:
                continue
            tid = int(tid_raw)
            if tid in result:
                result[tid].append(int(aoid_raw))

    if failed_ids:
        logger.info(f"mtaomi per-task fallback: {len(failed_ids)} tasks")
        retry_results = await asyncio.gather(
            *[_one_task(tid) for tid in failed_ids],
            return_exceptions=True,
        )
        for tid, res in zip(failed_ids, retry_results):
            if isinstance(res, BaseException):
                logger.warning(f"mtaomi single-task failed task_id={tid}: {res}")
                continue
            for item in res:
                aoid_raw = item.get("agro_operation_id")
                if aoid_raw is not None:
                    result[tid].append(int(aoid_raw))

    return result


# Сколько agro_operation_id передавать в одном GET к mtaomi (через запятую).
MTAOMI_OP_BATCH_SIZE = 300


async def _fetch_mtaomi_by_agro_op_ids(
    session: aiohttp.ClientSession,
    op_ids: list[int] | set[int],
    *,
    batch_size: int = MTAOMI_OP_BATCH_SIZE,
    sem: asyncio.Semaphore | None = None,
    log_prefix: str = "",
) -> dict[int, set[int]]:
    """mtaomi → {agro_operation_id: {machine_task_id, ...}}.

    Официальный фильтр Cropwise API — agro_operation_id (batch через запятую).
    """
    uniq = list(dict.fromkeys(int(i) for i in op_ids if i is not None))
    result: dict[int, set[int]] = {oid: set() for oid in uniq}
    if not uniq:
        return result

    _sem = sem if sem is not None else _DEFAULT_SEM
    endpoint = "machine_task_agro_operation_mapping_items"
    batches = [uniq[i:i + batch_size] for i in range(0, len(uniq), batch_size)]
    prefix = log_prefix or ""

    async def _one_batch(batch: list[int]) -> list[dict]:
        return await _fetch_all_pages(
            session,
            endpoint,
            {"agro_operation_id": ",".join(str(i) for i in batch)},
            sem=_sem,
        )

    async def _one_op(op_id: int) -> list[dict]:
        return await _fetch_all_pages(
            session,
            endpoint,
            {"agro_operation_id": op_id},
            sem=_sem,
        )

    batch_results = await asyncio.gather(
        *[_one_batch(b) for b in batches],
        return_exceptions=True,
    )

    failed_ids: list[int] = []
    for batch, res in zip(batches, batch_results):
        if isinstance(res, BaseException):
            logger.warning(
                f"{prefix}mtaomi batch failed ({len(batch)} ops), fallback per-op: {res}"
            )
            failed_ids.extend(batch)
            continue
        for item in res:
            aoid_raw = item.get("agro_operation_id")
            tid_raw = item.get("machine_task_id")
            if aoid_raw is None or tid_raw is None:
                continue
            try:
                aoid, tid = int(aoid_raw), int(tid_raw)
            except (TypeError, ValueError):
                continue
            if aoid in result:
                result[aoid].add(tid)

    if failed_ids:
        logger.info(f"{prefix}mtaomi per-op fallback: {len(failed_ids)} ops")
        retry_results = await asyncio.gather(
            *[_one_op(oid) for oid in failed_ids],
            return_exceptions=True,
        )
        for op_id, res in zip(failed_ids, retry_results):
            if isinstance(res, BaseException):
                logger.warning(f"{prefix}mtaomi single-op failed op_id={op_id}: {res}")
                continue
            for item in res:
                tid_raw = item.get("machine_task_id")
                if tid_raw is not None:
                    try:
                        result[int(op_id)].add(int(tid_raw))
                    except (TypeError, ValueError):
                        pass

    logger.info(
        f"{prefix}mtaomi by agro_operation_id: {len(uniq)} ops, "
        f"{sum(len(v) for v in result.values())} task links"
    )
    return result


async def fetch_users_with_machines(
    hours:                  Optional[int] = None,
    filter_username:        Optional[str] = None,
    filter_machine_subtype: Optional[str] = None,
    filter_work_type:       Optional[str] = None,
) -> list[dict]:
    logger.info(
        f"fetch_users_with_machines: hours={hours}, "
        f"filter_username={filter_username}, "
        f"filter_machine_subtype={filter_machine_subtype}, "
        f"filter_work_type={filter_work_type}"
    )

    task_params: dict = {}
    if hours is not None:
        date_from = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
        task_params["start_time_gt_eq"] = date_from

    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS) as session:
        users, machines, work_types, machine_tasks = await asyncio.gather(
            _fetch_all_pages(session, "users"),
            _fetch_all_pages(session, "machines"),
            _fetch_all_pages(session, "work_types"),
            _fetch_all_pages(session, "machine_tasks", task_params),
        )

    logger.info(
        f"Loaded: users={len(users)}, machines={len(machines)}, "
        f"work_types={len(work_types)}, tasks={len(machine_tasks)}"
    )

    user_dict: dict[int, dict] = {u["id"]: u for u in users}

    machine_dict: dict[int, dict] = {}
    for m in machines:
        subtype = m.get("machine_subtype") or ""
        if subtype not in ALLOWED_SUBTYPES:
            continue
        machine_dict[m["id"]] = {
            "reg":     m.get("registration_number") or m.get("inventory_number") or "—",
            "subtype": subtype,
        }

    work_type_dict: dict[int, str] = {
        wt["id"]: localize_work_type(wt.get("name"))
        for wt in work_types
    }

    rows: list[dict] = []
    seen: set = set()

    for t in machine_tasks:
        driver_id  = t.get("driver_id")
        machine_id = t.get("machine_id")

        if not driver_id or machine_id not in machine_dict:
            continue

        key = (driver_id, machine_id, t.get("work_type_id"))
        if key in seen:
            continue
        seen.add(key)

        user         = user_dict.get(driver_id, {})
        username     = user.get("username") or user.get("email") or f"ID:{driver_id}"
        phone        = user.get("phone") or "—"
        machine_info = machine_dict[machine_id]
        work_type_id = t.get("work_type_id")
        work_type    = work_type_dict.get(work_type_id, "—") if work_type_id else "—"
        task_label   = f"#{t['id']}"
        if t.get("description"):
            task_label += f" | {t['description']}"

        rows.append({
            "username":        username,
            "phone":           phone,
            "reg_number":      machine_info["reg"],
            "machine_subtype": machine_info["subtype"],
            "work_type":       work_type,
            "task_label":      task_label,
        })

    if filter_username:
        needle = filter_username.lower()
        rows = [r for r in rows if needle in r["username"].lower()]

    if filter_machine_subtype:
        needle = filter_machine_subtype.lower()
        rows = [r for r in rows if r["machine_subtype"].lower() == needle]

    if filter_work_type:
        needle = filter_work_type.lower()
        rows = [r for r in rows if needle in r["work_type"].lower()]

    rows.sort(key=lambda r: r["username"])

    logger.info(f"fetch_users_with_machines result: {len(rows)} records")
    return rows


# ── Сырые данные для sync.py ──────────────────────────────────────────────────

async def fetch_cropwise_users_raw(updated_after: datetime | None = None) -> list[dict]:
    params = {}
    if updated_after:
        params["updated_at_gt"] = updated_after.strftime("%Y-%m-%dT%H:%M:%S")
    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS) as session:
        return await _fetch_all_pages(session, "users", params)


async def fetch_cropwise_machines_raw(updated_after: datetime | None = None) -> list[dict]:
    params = {}
    if updated_after:
        params["updated_at_gt"] = updated_after.strftime("%Y-%m-%dT%H:%M:%S")
    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS) as session:
        return await _fetch_all_pages(session, "machines", params)


# ── Маппинг machine_id → агрофирма (через поля) ──────────────────────────────

# Справочник корневых папок group_folders, соответствующих агрофирмам.
# Те же ID, что в field_area_router.py / tasks.py и других местах проекта —
# держим в одном виде, чтобы все модули резолвили агрофирму одинаково.
FIRM_TO_FOLDER: dict[str, int] = {
    "АФ Чистопольская": 3, "АгроРаздолье": 4, "Аксу-Агро": 5,
    "НурлатАгро": 11, "Дрожжаное Рассвет Агро": 6,
    "Актай": 7, "Лениногорская Птицефабрика": 9,
}
FOLDER_TO_FIRM: dict[int, str] = {v: k for k, v in FIRM_TO_FOLDER.items()}

_LPF_NEEDLES = ("лениногор", "птицефабрик", "лпф", "lpf")


def _norm(s: str) -> str:
    return (s or "").strip().casefold().replace(" ", "").replace("\u00a0", "")


def _folder_parent_map(group_folders: list[dict]) -> dict[int, int | None]:
    """folder_id → id родительской папки (или None для корня)."""
    out: dict[int, int | None] = {}
    for gf in group_folders:
        fid = gf.get("id")
        if fid is None:
            continue
        pid = (gf.get("parent_id") or gf.get("parent_group_folder_id")
               or gf.get("group_folder_parent_id"))
        out[int(fid)] = int(pid) if pid is not None else None
    return out


def _folder_id_chain(folder_id: int | None, parent_map: dict) -> list[int]:
    """Цепочка id от данной папки вверх до корня (включительно)."""
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


def _resolve_firm_folder(folder_id: int | None, parent_map: dict, root_ids: set) -> int | None:
    """Поднимается по родителям от folder_id до известной корневой папки агрофирмы."""
    for cid in _folder_id_chain(folder_id, parent_map):
        if cid in root_ids:
            return cid
    return None


def _build_field_to_agrofirm(
    fields: list[dict],
    field_groups: list[dict],
    group_folders: list[dict],
) -> dict[int, str]:
    """
    field_id → название агрофирмы.

    group_folders — вложенная структура (папка может лежать в папке), поэтому
    нельзя просто взять имя ближайшей папки поля: нужно подняться по цепочке
    parent_id до корневой папки агрофирмы (FIRM_TO_FOLDER). Дополнительно
    поддерживается резолв group_folder_id как напрямую с поля, так и через
    его field_group (на случай, когда оно не указано на самом поле).
    """
    fgs_map    = {int(fg["id"]): fg for fg in field_groups if fg.get("id") is not None}
    parent_map = _folder_parent_map(group_folders)
    root_ids   = set(FOLDER_TO_FIRM.keys())

    # Папки, чьё название явно намекает на агрофирму (доп. подсказка для
    # случаев, когда структура не доходит до известного корня) —
    # тот же приём, что и в field_area_router.py.
    gf_hints: dict[int, str] = {}
    for gf in group_folders:
        fid = gf.get("id")
        if fid is None:
            continue
        blob = _norm(f"{gf.get('name') or ''} {gf.get('title') or ''}")
        if any(x in blob for x in _LPF_NEEDLES):
            gf_hints[int(fid)] = "Лениногорская Птицефабрика"

    field_to_agrofirm: dict[int, str] = {}
    for f in fields:
        fid = f.get("id")
        if fid is None:
            continue
        raw_folder = f.get("group_folder_id")
        fgid = f.get("field_group_id")
        if raw_folder is None and fgid is not None:
            raw_folder = fgs_map.get(int(fgid), {}).get("group_folder_id")
        if raw_folder is None:
            continue
        folder_id = int(raw_folder)

        agrofirm = None
        for cid in _folder_id_chain(folder_id, parent_map):
            if cid in gf_hints:
                agrofirm = gf_hints[cid]
                break
        if agrofirm is None:
            root = _resolve_firm_folder(folder_id, parent_map, root_ids)
            if root is not None:
                agrofirm = FOLDER_TO_FIRM.get(root)

        if agrofirm:
            field_to_agrofirm[int(fid)] = agrofirm

    return field_to_agrofirm


async def fetch_machine_agrofirm_map(recent_days: int = 30) -> dict[int, str]:
    """
    Строит маппинг machine_id → название агрофирмы.

    Логика поля → агрофирма:
      field → field_group (group_id) → group_folder (folder_id)
            → подъём по цепочке родительских папок (parent_id) до корневой
              папки агрофирмы (см. FIRM_TO_FOLDER) → название агрофирмы.
    (см. _build_field_to_agrofirm).

    Агрофирма машины = агрофирма ПОЛЯ её САМОЙ ПОСЛЕДНЕЙ (по start_time)
    задачи — а НЕ «большинство голосов» по всем полям за весь сезон, как
    было раньше. Раньше, если машина отработала полсезона в агрофирме А
    и только потом переехала в Б, majority-vote за сезон всё ещё выдавал
    «А» (у неё было больше голосов), хотя машина уже давно в Б. Простой и
    его агрофирма (см. DowntimeDAO._resolve_close_agrofirm) уже считаются
    по этому же принципу — «текущее поле», а не сезонная сумма; здесь та
    же логика для vehicles.agrofirm.

    ОПТИМИЗАЦИЯ (и заодно ответ на «зачем грузить весь сезон целиком»):
    раз нужна только ПОСЛЕДНЯЯ задача каждой машины, весь сезон целиком
    не нужен вовсе. Тянем задачи только за последние `recent_days` дней
    (start_time_gt_eq) — этого достаточно почти для всех активных машин.
    Для машин, у которых в этом окне НЕТ ни одной задачи (простаивают
    дольше recent_days — например, законсервированная техника или машина
    вне сезона), точечно догружаем полную историю ТОЛЬКО по этим машинам
    (фильтр machine_id=, без ограничения по датам) — таких обычно немного,
    так что это не отменяет выигрыш от сужения основного запроса.
    MTFMI (запрос полей для задач без прямого field_id) тоже нужен теперь
    только для одной, последней задачи каждой машины — а не для всех задач
    сезона, как было раньше.
    """
    from collections import Counter

    def _parse_dt(s: str | None) -> datetime:
        if not s:
            return datetime.min
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return datetime.min

    date_from_str = (datetime.now() - timedelta(days=recent_days)).strftime("%Y-%m-%dT%H:%M:%S")

    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS) as session:
        fields, field_groups, group_folders, machines_raw, recent_tasks = await asyncio.gather(
            _fetch_all_pages(session, "fields"),
            _fetch_all_pages(session, "field_groups"),
            _fetch_all_pages(session, "group_folders"),
            _fetch_all_pages(session, "machines"),
            _fetch_all_pages(session, "machine_tasks", {"start_time_gt_eq": date_from_str}),
        )

    logger.info(
        f"fetch_machine_agrofirm_map: loaded fields={len(fields)}, "
        f"machines={len(machines_raw)}, recent_tasks={len(recent_tasks)} "
        f"(window={recent_days}d, from {date_from_str})"
    )

    field_to_agrofirm = _build_field_to_agrofirm(fields, field_groups, group_folders)

    logger.info(
        f"fetch_machine_agrofirm_map: folders={len(group_folders)}, "
        f"groups={len(field_groups)}, fields mapped={len(field_to_agrofirm)} of {len(fields)}"
    )

    # Транспортные машины — агрофирма напрямую из legal_company (у них нет полей)
    transport_agrofirm: dict[int, str] = {}
    for m in machines_raw:
        mid = m.get("id")
        if not mid:
            continue
        if (m.get("machine_type") or "").lower() == "transport":
            lc = (m.get("legal_company") or "").strip()
            if lc:
                transport_agrofirm[mid] = lc

    # Последняя (по start_time) задача каждой не-транспортной машины в пределах окна
    latest_task: dict[int, dict] = {}
    for task in recent_tasks:
        mid = task.get("machine_id")
        tid = task.get("id")
        if not tid or not mid or mid in transport_agrofirm:
            continue
        cur = latest_task.get(mid)
        if cur is None or _parse_dt(task.get("start_time")) > _parse_dt(cur.get("start_time")):
            latest_task[mid] = task

    # Не-транспортные машины, у которых в окне вообще нет задач — простаивают
    # дольше recent_days. Догружаем ИМ полную историю поштучно (machine_id=),
    # без ограничения по датам, и берём максимум по start_time.
    all_machine_ids = {
        m.get("id") for m in machines_raw
        if m.get("id") and (m.get("machine_type") or "").lower() != "transport"
    }
    missing_ids = all_machine_ids - set(latest_task.keys())

    if missing_ids:
        logger.info(
            f"fetch_machine_agrofirm_map: {len(missing_ids)} machines have no task "
            f"in last {recent_days}d, fetching full history individually"
        )
        SEM_FALLBACK = asyncio.Semaphore(20)

        async def _fetch_latest_for_machine(session: aiohttp.ClientSession, mid: int) -> tuple[int, dict | None]:
            async with SEM_FALLBACK:
                tasks = await _fetch_all_pages(session, "machine_tasks", {"machine_id": mid})
            if not tasks:
                return mid, None
            tasks.sort(key=lambda t: _parse_dt(t.get("start_time")), reverse=True)
            return mid, tasks[0]

        async with aiohttp.ClientSession(headers=CROPWISE_HEADERS) as session:
            fallback_results = await asyncio.gather(
                *[_fetch_latest_for_machine(session, mid) for mid in missing_ids]
            )
        for mid, task in fallback_results:
            if task:
                latest_task[mid] = task

    # Прямой field_id vs нужен MTFMI — теперь только для ОДНОЙ (последней)
    # задачи каждой машины, а не для всех задач сезона.
    machine_agrofirm: dict[int, str] = {}
    tasks_need_mtfmi: list[tuple[int, int]] = []  # (task_id, machine_id)

    for mid, task in latest_task.items():
        tid = task.get("id")
        direct_field = task.get("field_id")
        if direct_field:
            agrofirm = field_to_agrofirm.get(int(direct_field))
            if agrofirm:
                machine_agrofirm[mid] = agrofirm
        else:
            tasks_need_mtfmi.append((tid, mid))

    logger.info(
        f"fetch_machine_agrofirm_map: {len(tasks_need_mtfmi)} tasks need MTFMI lookup "
        f"(latest task per machine only)"
    )

    # MTFMI batch (machine_task_id через запятую)
    SEM_MTFMI = asyncio.Semaphore(20)
    if tasks_need_mtfmi:
        task_id_to_machine: dict[int, int] = {tid: mid for tid, mid in tasks_need_mtfmi}
        async with aiohttp.ClientSession(headers=CROPWISE_HEADERS) as session:
            mtfmi_map = await _fetch_mtfmi_by_task_ids(
                session,
                [tid for tid, _ in tasks_need_mtfmi],
                sem=SEM_MTFMI,
            )
        # Задача может покрывать несколько полей (агрегат работает широким
        # захватом по нескольким полям сразу) — внутри ОДНОЙ задачи всё ещё
        # голосуем по числу полей, но между задачами голосования больше нет.
        for task_id, items in mtfmi_map.items():
            mid = task_id_to_machine.get(task_id)
            if mid is None:
                continue
            votes: Counter = Counter()
            for item in items:
                field_id = item.get("field_id")
                if not field_id:
                    continue
                agrofirm = field_to_agrofirm.get(int(field_id))
                if agrofirm:
                    votes[agrofirm] += 1
            if votes:
                machine_agrofirm[mid] = votes.most_common(1)[0][0]

    result: dict[int, str] = dict(machine_agrofirm)

    # Транспортные машины: добавляем через legal_company (не перетираем если уже есть из полей)
    for machine_id, agrofirm in transport_agrofirm.items():
        if machine_id not in result:
            result[machine_id] = agrofirm

    logger.info(
        f"fetch_machine_agrofirm_map: mapped {len(result)} machines to agrofirms "
        f"({len(transport_agrofirm)} via legal_company for transport, "
        f"{len(missing_ids)} resolved via full-history fallback)"
    )
    return result


async def fetch_field_to_agrofirm() -> dict[int, str]:
    """
    Асинхронная обёртка над _build_field_to_agrofirm: сама тянет fields/
    field_groups/group_folders и строит field_id → agrofirm (с подъёмом по
    цепочке родительских папок). Единая точка входа для всех мест, которым
    нужен этот справочник — чтобы не плодить копии без подъёма по родителям.
    """
    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS) as session:
        fields, field_groups, group_folders = await asyncio.gather(
            _fetch_all_pages(session, "fields"),
            _fetch_all_pages(session, "field_groups"),
            _fetch_all_pages(session, "group_folders"),
        )
    return _build_field_to_agrofirm(fields, field_groups, group_folders)


async def resolve_agrofirms_for_tasks(task_ids: list[int]) -> dict[int, str]:
    """
    task_id (machine_task_id) → название агрофирмы, определённое ПО ПОЛЮ,
    на котором реально шла именно эта задача (machine_task_field_mapping_items),
    а не «текущее» сезонное большинство машины (см. fetch_machine_agrofirm_map).

    Используется для простоев (Downtime/ActiveDowntime): агрофирма простоя
    должна определяться тем полем, где машина работала в момент,
    непосредственно примыкающий к простою (задача, к которой привязан
    machine_task_id простоя) — а не общей агрофирмой машины за весь сезон,
    которая могла с тех пор смениться.

    Переиспользует ЕДИНСТВЕННУЮ корректную реализацию field → agrofirm
    (_build_field_to_agrofirm ниже, с подъёмом по цепочке родительских папок
    до корня FOLDER_TO_FIRM) — ту же, что использует fetch_machine_agrofirm_map.
    Раньше в notifications/report.py и downtime/router.py существовала
    отдельная, более простая её копия БЕЗ подъёма по родителям — из-за этого
    вложенные под-папки полей (обычный случай в Cropwise) агрофирму не находили.
    """
    task_ids = [t for t in task_ids if t]
    if not task_ids:
        return {}

    field_to_agrofirm = await fetch_field_to_agrofirm()

    SEM_MTFMI = asyncio.Semaphore(20)
    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS) as session:
        mtfmi_map = await _fetch_mtfmi_by_task_ids(
            session, task_ids, sem=SEM_MTFMI,
        )

    from collections import Counter
    result: dict[int, str] = {}
    for tid, items in mtfmi_map.items():
        votes: Counter = Counter()
        for item in items:
            fid = item.get("field_id")
            if not fid:
                continue
            agrofirm = field_to_agrofirm.get(int(fid))
            if agrofirm:
                votes[agrofirm] += 1
        if votes:
            result[tid] = votes.most_common(1)[0][0]

    logger.info(
        f"resolve_agrofirms_for_tasks: resolved {len(result)} of {len(task_ids)} tasks"
    )
    return result


# ── Поиск активной задачи для машины в момент времени ────────────────────────

async def fetch_active_task_id_for_vehicle(
    vehicle_id: int,
    at_time: datetime,
) -> int | None:
    """
    Возвращает machine_task_id из Cropwise, активный в момент at_time
    для машины vehicle_id (vehicle.id == Cropwise machine_id).

    Ищет задачу где: start_time <= at_time AND end_time >= at_time.
    Если таких несколько — берёт с наибольшим start_time.
    Возвращает None при любой ошибке, чтобы не блокировать старт простоя.
    """
    t_str = at_time.strftime("%Y-%m-%dT%H:%M:%S+03:00")
    params = {
        "machine_id":       vehicle_id,
        "end_time_gt_eq":   t_str,   # задача ещё не закончилась к моменту at_time
        "start_time_lt_eq": t_str,   # задача уже началась к моменту at_time
        "limit":            10,
        "from_id":          0,
    }
    try:
        async with aiohttp.ClientSession(headers=CROPWISE_HEADERS) as session:
            async with session.get(
                f"{CROPWISE_BASE_URL}/machine_tasks", params=params
            ) as resp:
                resp.raise_for_status()
                body = await resp.json()
                tasks = body.get("data", [])
                logger.info(f"Cropwise machine_tasks response: params={params}, count={len(tasks)}, data={tasks[:2]}")

        if not tasks:
            logger.debug(f"No active task for vehicle_id={vehicle_id} at {t_str}")
            return None

        def _parse_dt(s: str | None) -> datetime:
            if not s:
                return datetime.min
            try:
                return datetime.fromisoformat(s)
            except ValueError:
                return datetime.min

        tasks.sort(key=lambda t: _parse_dt(t.get("start_time")), reverse=True)
        task_id = tasks[0]["id"]
        logger.debug(f"Found task_id={task_id} for vehicle_id={vehicle_id} at {t_str}")
        logger.debug(f"Cropwise response for vehicle_id={vehicle_id}: {tasks}")
        return task_id

    except Exception as e:
        logger.warning(
            f"fetch_active_task_id_for_vehicle failed: "
            f"vehicle_id={vehicle_id}, at_time={t_str}, error={e}"
        )
        return None

# ── Единая логика разрешения ФИО (используется во всех cropwise-отчётах) ──────
#
# Приоритет:
#   1) локальная БД (User.fio) — НЕЗАВИСИМО от статуса is_active: уволенный/
#      деактивированный пользователь всё равно должен показываться по ФИО;
#   2) живые данные Cropwise (username/email) — если пользователя нет в
#      локальной БД вообще, либо там пустое fio;
#   3) FIO_NOT_FOUND — только если и там, и там пусто.

FIO_NOT_FOUND = "ФИО не обнаружено"


async def fetch_local_fio_map() -> dict[int, str]:
    """ФИО всех пользователей локальной БД, включая деактивированных."""
    from sqlalchemy import select
    from app.database import async_session_maker
    from app.users.models import User

    async with async_session_maker() as session:
        rows = await session.execute(select(User.id, User.fio))
        return {row.id: row.fio for row in rows if row.fio}


def resolve_fio_from_cropwise_user(u: dict) -> str:
    """username/email из сырой записи пользователя Cropwise (без похода в БД)."""
    return (u.get("username") or u.get("email") or "").strip()
