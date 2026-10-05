"""
cropwise/zsk_downtime_sync.py
================================
Вкладка «Простои ЗСК»: простои по датчику «Вентилятор»
на объектах FortMonitor (smsr.online), логика val=0.0 = простой.

Адаптация автономного скрипта fortmonitor_zsk_downtime_by_value.py —
вся логика резолвинга объектов/датчиков и построения интервалов
(build_value_intervals) перенесена БЕЗ ИЗМЕНЕНИЙ, за исключением списка
искомых датчиков (TARGET_KEYWORDS_PRIORITY) — датчики «Работа» и
«Зажиг» убраны, используется только «Вентилятор». Прочие отличия от
исходного скрипта:
  - результат кладётся в Redis (см. zsk_downtime_router.py), а не в CSV;
  - окно расчёта не жёстко "сегодня 06:00 -> сейчас", а настраивается
    через /api/zsk-downtime/config (date_from / date_to, обе даты —
    КАЛЕНДАРНЫЕ ДНИ по местному времени). Начало дня (06:00) всегда
    фиксировано и не редактируется — см. compute_window().
  - нет продолжения от предыдущего запуска (state.json) — каждый
    прогон считает окно целиком заново, т.к. диапазон теперь может
    быть выбран пользователем (а не только "с последнего запуска").

ЧАСОВОЙ ПОЯС (как в исходном скрипте): сервер FortMonitor отдаёт метки
времени в своём collections-времени; локальное (Татарстан) время -
TIMEZONE_OFFSET_HOURS = +3 часа. Смещение применяется ТОЛЬКО при
переводе в отображаемое время (to_local) - сами запросы к API и
внутренние сравнения идут в исходном времени сервера.

ВАЖНАЯ ОГОВОРКА (перенесена из исходного скрипта, см. его README-блок
в конце файла): логика val=0.0 подтверждена вживую только на 2 из 31
объекта ("Сушилка (SGR-100) (ЭТТ)" и "Сушилка (SGR-50) (ЭТТ)").
"""
from __future__ import annotations

import time
from datetime import datetime, date, time as dtime, timedelta

import httpx

from app.config import settings
from app.logging_config import get_logger

logger = get_logger("cropwise.zsk_downtime")

# =============================================================================
# НАСТРОЙКИ (перенесены из fortmonitor_zsk_downtime_by_value.py)
# =============================================================================

BASE_URL = settings.FM_BASE_URL
LOGIN    = settings.FM_LOGIN
PASSWORD = settings.FM_PASSWORD

FMT = "%Y-%m-%d %H:%M:%S"
SENTINEL = -9999999.0

REQUEST_TIMEOUT = httpx.Timeout(connect=15.0, read=90.0, write=30.0, pool=15.0)
MAX_RETRIES = 5
RETRY_BACKOFF_SECONDS = 5
RATE_LIMIT_BACKOFF_SECONDS = 10
PER_OBJECT_DELAY_SECONDS = 1.5

TIMEZONE_OFFSET_HOURS = 3  # местное время (Татарстан) относительно сервера FortMonitor
DAY_START_HOUR = 6         # граница суток отчёта — ВСЕГДА 06:00, не настраивается

TARGET_KEYWORDS_PRIORITY = ["вентил"]

# --- ДИАГНОСТИКА (временно) ---
DIAG_LONG_DOWN_THRESHOLD_SECONDS = 2 * 3600  # порог "подозрительно длинного" down-интервала
DIAG_MAX_LOGGED_POINTS = 200                 # ограничение на кол-во точек в одном логе

ZSK_OBJECT_NAMES = [
    "1 ПОСТ (DBB-2513) 1 (ЧАТ)",
    "1 ПОСТ (RCI-1) 1 (ЧАТ)",
    "2 ПОСТ (DBB-2513) 1 (ЧАТ)",
    "2 ПОСТ (RCI-1) 2 (ЧАТ)",
    "3 ПОСТ (DBB-2513) 1 (ЧАТ)",
    "3 ПОСТ (DBB-2513) 2 (ЧАТ)",
    "Веста-50 (зерносушилка Р1-С50) (ЧАТ)",
    "Зерносушилка Большая Акса (DBB-2513 х2) (ДРА) 1",
    "Зерносушилка Большая Акса (DBB-2513 х2) (ДРА)2",
    "Зерносушилка Каракитян (DBB-2513) 1 (ДРА)",
    "Зерносушилка Каракитян (DBB-2513) 2 (ДРА)",
    "Зерносушилка Новый убей (DBB-2513) 1 (ДРА)",
    "Зерносушилка Новый убей (DBB-2513) 2 (ДРА)",
    "М-819 (АР)",
    "СОБ-3 (ДСП-24) (ЭТТ)",
    "СОБ-5 (ДСП-32) (ЭТТ)",
    "СОБ-7 (ДСП-32) (ЭТТ)",
    "Сушилка (DBB-2513) (АА)",
    "Сушилка (DBB-92413) №1 (АА)",
    "Сушилка (DBB-92413) №2 (АА)",
    "Сушилка (GD-1344) (ЭТТ)",
    "Сушилка (SGH-30 №1) (АФЧ)",
    "Сушилка (SGH-30 №2) (АФЧ)",
    "Сушилка (SGR-100) (АР)",
    "Сушилка (SGR-100) (ЭТТ)",
    "Сушилка (SGR-50) (ЭТТ)",
    "Сушилка (МИГ-80) (НЭ)",
    "Сушилка (Целинная-30) (РСХПП)",
    "Сушилка СОБ - 1 (ДСП-24) (НЭ)",
    "Сушилка СОБ-24 (ДСП-24) (РСХПП)",
    "Широкко-2000 (АР)",
]


# =============================================================================
# HTTP (перенесено без изменений)
# =============================================================================

def request_with_retry(session: httpx.Client, method: str, url: str, **kwargs):
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            wait = RETRY_BACKOFF_SECONDS * attempt
            logger.warning(f"zsk_downtime: {method} {url} попытка {attempt}/{MAX_RETRIES} не удалась ({exc!r}), жду {wait}с")
            if attempt < MAX_RETRIES:
                time.sleep(wait)
            continue

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    wait = float(retry_after)
                except ValueError:
                    wait = RATE_LIMIT_BACKOFF_SECONDS * attempt
            else:
                wait = RATE_LIMIT_BACKOFF_SECONDS * attempt
            logger.warning(f"zsk_downtime: {method} {url} 429, попытка {attempt}/{MAX_RETRIES}, жду {wait}с")
            last_exc = httpx.HTTPStatusError("429 Too Many Requests", request=resp.request, response=resp)
            if attempt < MAX_RETRIES:
                time.sleep(wait)
            continue

        return resp

    raise last_exc


def connect(session: httpx.Client) -> str:
    resp = request_with_retry(
        session, "POST", f"{BASE_URL}/api/integration/v1/connect",
        json={"login": LOGIN, "password": PASSWORD, "lang": "ru", "timezone": 0},
        headers={"accept": "application/json", "Content-Type": "application/json-patch+json"},
    )
    resp.raise_for_status()
    session_id = resp.headers.get("Sessionid") or resp.headers.get("SessionId")
    if not session_id or resp.text.strip('"') != "Ok":
        raise RuntimeError(f"Не удалось открыть сессию FortMonitor: {resp.status_code} {resp.text}")
    return session_id


def ensure_session(session: httpx.Client, session_id: str) -> str:
    if session_id:
        headers = {"accept": "application/json", "SessionId": session_id}
        try:
            r = session.get(f"{BASE_URL}/api/integration/v1/ping", headers=headers, timeout=15)
            if r.status_code == 200 and r.text.strip().lower() == "true":
                return session_id
        except httpx.HTTPError:
            pass
    return connect(session)


def normalize_name(s):
    if s is None:
        return ""
    s = s.replace("\xa0", " ")
    return " ".join(s.split()).strip().lower()


def flatten_tree(node, out):
    if isinstance(node, dict):
        if node.get("leaf"):
            out.append({"real_id": node.get("real_id"), "name": node.get("name")})
        for c in node.get("children") or []:
            flatten_tree(c, out)
    return out


def fetch_all_objects(session, headers):
    r = request_with_retry(
        session, "GET", f"{BASE_URL}/api/integration/v1/gettree",
        headers=headers, params={"all": "true"},
    )
    r.raise_for_status()
    tree = r.json()
    items = []
    for root in tree.get("children", []):
        flatten_tree(root, items)
    return items


def resolve_object_ids(session, headers, names):
    objects = fetch_all_objects(session, headers)
    by_norm = {}
    for o in objects:
        by_norm.setdefault(normalize_name(o["name"]), (o["real_id"], o["name"]))

    resolved = {}
    missing = []
    for name in names:
        norm = normalize_name(name)
        if norm in by_norm:
            real_id, original_name = by_norm[norm]
            resolved[real_id] = original_name
        else:
            missing.append(name)
    if missing:
        logger.warning(f"zsk_downtime: не найдены точно ({len(missing)} из {len(names)}): {missing}")
    return resolved


def find_target_sid(session, headers, oid):
    """Возвращает (sid, sensor_name) для датчика 'Вентилятор'."""
    r = request_with_retry(session, "GET", f"{BASE_URL}/api/integration/v1/objectinfo",
                            headers=headers, params={"oid": oid})
    r.raise_for_status()
    sensors = r.json().get("sensors") or []

    # --- ДИАГНОСТИКА (временно): если под keyword подходит больше одного
    # датчика, find_target_sid молча берёт первый и теряет остальные -
    # логируем все совпадения, чтобы проверить, не читаем ли не тот датчик.
    # Убрать после диагностики.
    for kw in TARGET_KEYWORDS_PRIORITY:
        matches = [(s.get("sid"), s.get("name")) for s in sensors if kw in (s.get("name") or "").lower()]
        if len(matches) > 1:
            logger.warning(
                f"zsk_downtime DIAG AMBIGUOUS SENSOR: oid={oid}, keyword={kw!r}, "
                f"найдено {len(matches)} датчиков: {matches} — будет использован первый: {matches[0]}"
            )

    by_keyword = {}
    for s in sensors:
        name = (s.get("name") or "")
        low = name.lower()
        for kw in TARGET_KEYWORDS_PRIORITY:
            if kw in low and kw not in by_keyword:
                by_keyword[kw] = (s.get("sid"), name)
    for kw in TARGET_KEYWORDS_PRIORITY:
        if kw in by_keyword:
            return by_keyword[kw]
    return None, None


def fetch_paramsvals(session, headers, oid, sid, date_from, date_to):
    params = {
        "oid": oid, "sids": str(sid),
        "from": date_from.strftime(FMT), "to": date_to.strftime(FMT),
    }
    r = request_with_retry(session, "GET", f"{BASE_URL}/api/integration/v1/paramsvals",
                            headers=headers, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    for series in data.get("lparams", []):
        if series.get("sid") == sid:
            return series.get("vals", [])
    return []


def build_value_intervals(vals, window_from, window_to):
    """Подтверждённая логика val=0.0 -> down (см. модульный докстринг)."""
    pts = [(datetime.strptime(v["tm"], FMT), v["val"]) for v in vals if v["val"] != SENTINEL]
    pts.sort(key=lambda p: p[0])
    deduped = []
    for tm, val in pts:
        if deduped and deduped[-1][0] == tm:
            deduped[-1] = (tm, val)
        else:
            deduped.append((tm, val))

    if not deduped:
        return []

    intervals = []
    cursor = max(window_from, deduped[0][0])
    for i, (tm, val) in enumerate(deduped):
        if tm < cursor:
            continue
        next_tm = deduped[i + 1][0] if i + 1 < len(deduped) else window_to
        seg_end = min(next_tm, window_to)
        if seg_end <= cursor:
            continue
        state = "down" if val < 0.5 else "work"
        intervals.append({"start": cursor, "end": seg_end, "state": state, "val": val})
        cursor = seg_end
        if cursor >= window_to:
            break

    merged = []
    for iv in intervals:
        if merged and merged[-1]["state"] == iv["state"]:
            merged[-1]["end"] = iv["end"]
        else:
            merged.append(dict(iv))
    return merged


def fmt_td(td) -> str:
    total = int(td.total_seconds())
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def to_local(dt: datetime) -> datetime:
    return dt + timedelta(hours=TIMEZONE_OFFSET_HOURS)


# =============================================================================
# ОКНО РАСЧЁТА (новое — настраиваемый диапазон дат вместо жёсткого "сегодня")
# =============================================================================

def compute_window(date_from_local: date, date_to_local: date, now_server: datetime) -> tuple[datetime, datetime]:
    """
    Начало окна = date_from_local, 06:00 ПО МЕСТНОМУ времени — ВСЕГДА,
    не настраивается (см. DAY_START_HOUR). Конец окна = date_to_local,
    23:59:59 по местному времени, но не позже текущего момента (если
    date_to_local — сегодня или в будущем, конец окна = "сейчас").

    Обе границы переводятся обратно в системное время сервера
    FortMonitor (запросы к API идут в исходном времени сервера — как и
    в исходном скрипте, см. TIMEZONE_OFFSET_HOURS выше).
    """
    start_local = datetime.combine(date_from_local, dtime(DAY_START_HOUR, 0, 0))
    start_server = start_local - timedelta(hours=TIMEZONE_OFFSET_HOURS)

    local_today = (now_server + timedelta(hours=TIMEZONE_OFFSET_HOURS)).date()
    if date_to_local >= local_today:
        end_server = now_server
    else:
        end_local = datetime.combine(date_to_local, dtime(23, 59, 59))
        end_server = end_local - timedelta(hours=TIMEZONE_OFFSET_HOURS)

    if end_server <= start_server:
        end_server = start_server + timedelta(minutes=1)

    return start_server, end_server


def default_date_range(now_server: datetime) -> tuple[date, date]:
    """По умолчанию — текущий (местный) день, и с начала (date_from), и с конца (date_to)."""
    local_today = (now_server + timedelta(hours=TIMEZONE_OFFSET_HOURS)).date()
    return local_today, local_today


# =============================================================================
# ОСНОВНОЙ ПРОГОН
# =============================================================================

def collect_downtime_rows(date_from_local: date, date_to_local: date) -> tuple[list[dict], dict]:
    """
    Синхронный прогон (вызывается из Celery-задачи). Возвращает
    (rows, meta), где rows — список простоев за окно [date_from_local
    06:00 .. date_to_local 23:59:59/сейчас], а meta — статистика прогона
    (сколько объектов резолвилось и т.п.) для логов.

    Формат строки — как колонки в CSV исходного скрипта:
      zsk_id, zsk_name, sensor_name, sensor_sid,
      start_local, end_local, duration_seconds, duration_hms
    """
    now_server = datetime.now().replace(microsecond=0)  # naive — совместимо со strptime из API
    date_from_server, date_to_server = compute_window(date_from_local, date_to_local, now_server)

    rows: list[dict] = []
    resolved_count = 0
    skipped_no_sensor: list[str] = []
    skipped_fetch_error: list[str] = []

    with httpx.Client(timeout=REQUEST_TIMEOUT) as session:
        session_id = ensure_session(session, "")
        headers = {"accept": "application/json", "SessionId": session_id}

        object_names = resolve_object_ids(session, headers, ZSK_OBJECT_NAMES)
        resolved_count = len(object_names)
        if not object_names:
            logger.warning("zsk_downtime: ни один объект не резолвился по имени")
            return [], {"resolved": 0, "total": len(ZSK_OBJECT_NAMES)}

        for oid, name in object_names.items():
            session_id = ensure_session(session, session_id)
            headers = {"accept": "application/json", "SessionId": session_id}

            sid, sensor_name = find_target_sid(session, headers, oid)
            if sid is None:
                skipped_no_sensor.append(name)
                continue

            try:
                vals = fetch_paramsvals(session, headers, oid, sid, date_from_server, date_to_server)
            except httpx.HTTPError as exc:
                logger.warning(f"zsk_downtime: oid={oid} ({name}): paramsvals не удалось получить ({exc!r})")
                skipped_fetch_error.append(name)
                continue

            # --- ДИАГНОСТИКА (временно): проверка гипотезы про downsampling
            # FortMonitor на широких окнах — сравниваем сырое кол-во точек
            # в пересчёте на день между узким и широким диапазоном. Убрать
            # после подтверждения/опровержения гипотезы.
            window_days = max((date_to_server - date_from_server).total_seconds() / 86400, 1e-9)
            raw_count = len(vals)
            non_sentinel_count = sum(1 for v in vals if v["val"] != SENTINEL)
            logger.info(
                f"zsk_downtime DIAG: oid={oid} ({name!r}) sensor={sensor_name!r} "
                f"окно={date_from_server.strftime(FMT)}..{date_to_server.strftime(FMT)} "
                f"({window_days:.2f} дн.) raw_points={raw_count} non_sentinel={non_sentinel_count} "
                f"points/day={non_sentinel_count / window_days:.1f}"
            )

            intervals = build_value_intervals(vals, date_from_server, date_to_server)

            # --- ДИАГНОСТИКА (временно): для подозрительно длинных down-
            # интервалов печатаем все сырые точки, из которых они собраны -
            # чтобы увидеть, не "тянется" ли интервал от одной-двух
            # залипших точек. Убрать после диагностики.
            long_downs = [iv for iv in intervals
                          if iv["state"] == "down"
                          and (iv["end"] - iv["start"]).total_seconds() > DIAG_LONG_DOWN_THRESHOLD_SECONDS]
            if long_downs:
                dedup_pts = []
                for v in vals:
                    if v["val"] == SENTINEL:
                        continue
                    tm = datetime.strptime(v["tm"], FMT)
                    if dedup_pts and dedup_pts[-1][0] == tm:
                        dedup_pts[-1] = (tm, v["val"])
                    else:
                        dedup_pts.append((tm, v["val"]))
                dedup_pts.sort(key=lambda p: p[0])

                points_str = ", ".join(
                    f"{tm.strftime('%H:%M:%S')}={val}" for tm, val in dedup_pts[:DIAG_MAX_LOGGED_POINTS]
                )
                more = "" if len(dedup_pts) <= DIAG_MAX_LOGGED_POINTS else f" ...+{len(dedup_pts) - DIAG_MAX_LOGGED_POINTS} ещё"

                for iv in long_downs:
                    dur_h = (iv["end"] - iv["start"]).total_seconds() / 3600
                    logger.warning(
                        f"zsk_downtime DIAG LONG-DOWN: oid={oid} ({name!r}) sensor={sensor_name!r} "
                        f"интервал {to_local(iv['start']).strftime(FMT)}..{to_local(iv['end']).strftime(FMT)} "
                        f"({dur_h:.1f} ч, val={iv['val']}) — всего точек за окно: {len(dedup_pts)}"
                    )
                logger.warning(f"zsk_downtime DIAG LONG-DOWN points: oid={oid} [{points_str}{more}]")

            for iv in intervals:
                if iv["state"] != "down":
                    continue
                start_local = to_local(iv["start"])
                end_local = to_local(iv["end"])
                dur = iv["end"] - iv["start"]
                rows.append({
                    "zsk_id":            oid,
                    "zsk_name":          name,
                    "sensor_name":       sensor_name,
                    "sensor_sid":        sid,
                    "start_local":       start_local.strftime(FMT),
                    "end_local":         end_local.strftime(FMT),
                    "start_local_ts":    start_local.strftime(FMT),  # для ключа статуса "Отработано"
                    "duration_seconds":  int(dur.total_seconds()),
                    "duration_hms":      fmt_td(dur),
                })

            time.sleep(PER_OBJECT_DELAY_SECONDS)

    rows.sort(key=lambda r: (r["zsk_name"], r["start_local"]))

    meta = {
        "resolved": resolved_count,
        "total": len(ZSK_OBJECT_NAMES),
        "skipped_no_sensor": skipped_no_sensor,
        "skipped_fetch_error": skipped_fetch_error,
    }
    logger.info(
        f"zsk_downtime: собрано {len(rows)} простоев, объектов резолвлено "
        f"{resolved_count}/{len(ZSK_OBJECT_NAMES)}, окно "
        f"{to_local(date_from_server).strftime(FMT)} — {to_local(date_to_server).strftime(FMT)}"
    )
    return rows, meta
