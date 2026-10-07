"""Cropwise UI: POST /machines/reports/work.json + парсинг regionRows."""
from __future__ import annotations

import html
import os
import re
import threading
import time
from collections import defaultdict
from datetime import date
from typing import Any

try:
    from season_calendar import season_year_for_date
except ImportError:  # pragma: no cover
    def season_year_for_date(d: date) -> int:
        return d.year + (1 if d.month >= 7 else 0)

import requests

try:
    from firm_sheet_names import FIRM_DISPLAY_NAMES
except ImportError:
    FIRM_DISPLAY_NAMES = {}

# Группы regionRows, не участвующие в сверке (как в UI Cropwise).
SKIP_REGION_GROUPS = frozenset({
    "зу под зданиями",
})

_SUBDIVISION_RE = re.compile(r"агрофирма\s*\d", re.IGNORECASE)
# Имя группы полей (ООО «Агрофирма 4», ЧО Полив, …) → одна из 7 агрофирм.
# Пустой кэш не кэшируем как «нет данных»: None значит «ещё не загружали».
_SUBGROUP_TO_FIRM: dict[str, str] | None = None
# Любые юникод-пробелы (в т.ч. U+00A0, U+202F, U+2009) как разделители разрядов.
_WS_RE = re.compile("[\\s\u00a0\u202f\u2009\u2007\u2007\u2060]+")
_TOTAL_LABELS = frozenset({"всего", "итого", "total", "итог"})
_TASK_ID_RE = re.compile(r"#(\d+)")
_CROPWISE_COOKIE_LOCK = threading.RLock()

HOST = "https://operations.cropwise.com"
WORK_JSON_URL = f"{HOST}/machines/reports/work.json"
WORK_PAGE_URL = f"{HOST}/machines/reports/work"


def normalize_cookie_string(raw: str) -> str:
    raw = raw.strip().strip('"').strip("'")
    if not raw:
        return ""
    if "=" not in raw and len(raw) >= 16:
        return f"_cropio_ses={raw}"
    return raw


def parse_cookie_string(raw: str) -> dict[str, str]:
    raw = normalize_cookie_string(raw)
    out: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if part and "=" in part:
            k, _, v = part.partition("=")
            out[k.strip()] = v.strip()
    return out


def make_web_session(cookie_raw: str) -> requests.Session:
    cookies = parse_cookie_string(cookie_raw)
    if not cookies:
        raise ValueError(
            "Cookie не распознана. Формат: _cropio_ses=...; crossdomain_auth=..."
        )
    s = requests.Session()
    normalized = normalize_cookie_string(cookie_raw)
    s.headers.update({"Accept": "application/json, */*", "Cookie": normalized})
    for name, value in cookies.items():
        s.cookies.set(name, value, domain=".cropwise.com")
        s.cookies.set(name, value, domain="operations.cropwise.com")
    return s


def _fetch_csrf_token(web: requests.Session) -> str:
    r = web.get(WORK_PAGE_URL, timeout=60)
    r.raise_for_status()
    m = re.search(r'name="csrf-token"\s+content="([^"]+)"', r.text)
    return m.group(1) if m else ""


def detect_ui_session_season(page_html: str) -> int | None:
    """seasonValue из HTML страницы work (сессия Cropwise)."""
    m = re.search(r'seasonValue":(\d{4})', html.unescape(page_html))
    return int(m.group(1)) if m else None


def detect_ui_season_years(page_html: str, report_date_year: int | None = None) -> list[int]:
    """
    Годы сезона из HTML страницы work (если UI их отдаёт).
    Cropwise может хранить выбор сезона в сессии — не в теле work.json.
    """
    years: set[int] = set()
    y0 = report_date_year or 0
    for m in re.finditer(r"\b(20[2-3][0-9])\b", page_html):
        y = int(m.group(1))
        if y0 and abs(y - y0) <= 2:
            years.add(y)
    # типичные data-атрибуты / JSON на странице
    for pat in (
        r'"season_years?"\s*:\s*\[([^\]]+)\]',
        r'"selected_years?"\s*:\s*\[([^\]]+)\]',
        r'data-season-year="(\d{4})"',
    ):
        for m in re.finditer(pat, page_html, re.I):
            chunk = m.group(1)
            for ym in re.finditer(r"20\d{2}", chunk):
                years.add(int(ym.group(0)))
    return sorted(years)


def _csrf_from_html(page_html: str) -> str:
    csrf_m = re.search(r'name="csrf-token"\s+content="([^"]+)"', page_html)
    return csrf_m.group(1) if csrf_m else ""


def _session_cookie_for_year(preferred: int) -> str:
    """Опциональная cookie с нужным seasonValue (CROPWISE_SESSION_COOKIE_2027 и т.д.)."""
    for key in (f"CROPWISE_SESSION_COOKIE_{preferred}", "CROPWISE_SESSION_COOKIE_2027"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            return raw
    return ""


def ensure_ui_session_season(
    cookie_raw: str,
    preferred: int,
) -> tuple[requests.Session, str, list[int], int | None, str]:
    """
    Cropwise work.json отдаёт данные только за seasonValue сессии.

    Сезон переключается тем же запросом, что и выбор в верхнем меню:
    GET /session_variables/set_current_year/<год>. Параметр ?seasonValue=
    на это не влияет. Если переключение не удалось, остальные сезоны
    сводки по-прежнему берутся из API v3.

    Возвращает (web, csrf, ui_years, ui_session, cookie_used).
    """
    web, csrf, ui_years, ui_session = warm_web_session(cookie_raw)
    cookie_used = cookie_raw
    if ui_session == preferred:
        return web, csrf, ui_years, ui_session, cookie_used

    try:
        switched = set_ui_session_year(web, csrf, preferred)
    except requests.HTTPError as exc:
        print(
            f"  Cropwise: сезон {preferred} не переключился ({exc.response.status_code}), "
            "факт этого сезона возьмём из API v3",
            flush=True,
        )
        switched = None
    if switched == preferred:
        return web, csrf, ui_years, switched, cookie_used

    alt = _session_cookie_for_year(preferred)
    if alt and alt != cookie_raw:
        try:
            web2, csrf2, ui_years2, ui2 = warm_web_session(alt)
            if ui2 == preferred:
                return web2, csrf2, ui_years2, ui2, alt
        except (requests.RequestException, ValueError):
            pass

    return web, csrf, ui_years, ui_session, cookie_used


def set_ui_session_year(web: requests.Session, csrf: str, year: int) -> int | None:
    """Сезон открытой сессии Cropwise, как пункт «Сезон» в верхнем меню."""
    url = f"{HOST}/session_variables/set_current_year/{int(year)}"
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "text/html,application/json",
        "Referer": WORK_PAGE_URL,
    }
    if csrf:
        headers["X-CSRF-Token"] = csrf
    response = web.get(url, headers=headers, timeout=60, allow_redirects=True)
    response.raise_for_status()
    season = detect_ui_session_season(response.text)
    if season == int(year):
        return season
    page = web.get(WORK_PAGE_URL, timeout=60)
    page.raise_for_status()
    return detect_ui_session_season(page.text)


def warm_web_session(cookie_raw: str) -> tuple[requests.Session, str, list[int], int | None]:
    """Страница work + CSRF; вернуть (session, csrf, ui_season_years, ui_session_seasonValue)."""
    web = make_web_session(cookie_raw)
    r = web.get(WORK_PAGE_URL, timeout=60)
    r.raise_for_status()
    csrf = _csrf_from_html(r.text)
    ui_years = detect_ui_season_years(r.text)
    ui_session = detect_ui_session_season(r.text)
    return web, csrf, ui_years, ui_session


def fetch_work_report(
    web: requests.Session,
    time_from: str,
    time_to: str,
    work_type_ids: list[int],
    *,
    csrf: str = "",
    ui_season_years: list[int] | None = None,
    ui_session_season: int | None = None,
) -> dict[str, Any]:
    payload = {
        "from_time": time_from,
        "to_time": time_to,
        "work_type_ids": ",".join(str(x) for x in work_type_ids),
    }
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if not csrf:
        csrf = _fetch_csrf_token(web)
    if csrf:
        headers["X-CSRF-Token"] = csrf
    r = web.post(WORK_JSON_URL, json=payload, headers=headers, timeout=120)
    if r.status_code != 200:
        return {
            "ok": False,
            "status": r.status_code,
            "error": r.text[:500],
            "payload": payload,
        }
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text[:3000]}
    out: dict[str, Any] = {"ok": True, "status": 200, "payload": payload, "body": body}
    if ui_season_years:
        out["ui_season_years"] = ui_season_years
    if ui_session_season is not None:
        out["ui_session_seasonValue"] = ui_session_season
    return out


def cropwise_total_ha(work: dict[str, Any], fact_hints: tuple[str, ...] | list[str]) -> float:
    """Итого «Всего» из work.json (0 если blank/ошибка)."""
    if not work.get("ok"):
        return 0.0
    body = work.get("body")
    if not isinstance(body, dict) or body.get("reportBlank"):
        return 0.0
    parsed = parse_work_report_body(body, [], list(fact_hints))
    return float(parsed.get("fact_total_ha") or 0)


def _fetch_work_report_get(
    cookie: str,
    time_from: str,
    time_to: str,
    work_type_ids: list[int],
) -> dict[str, Any]:
    """GET /machines/reports/work.json — иногда стабильнее POST после нагрузки API v3."""
    try:
        from cropwise_web_session import fetch_work_report_json, make_web_session as make_get_session
    except ImportError:
        return {"ok": False, "status": 0, "error": "cropwise_web_session unavailable"}
    prev = os.environ.get("CROPWISE_SESSION_COOKIE")
    normalized = normalize_cookie_string(cookie)
    with _CROPWISE_COOKIE_LOCK:
        os.environ["CROPWISE_SESSION_COOKIE"] = normalized
        try:
            session, source = make_get_session()
            if source == "none":
                return {"ok": False, "status": 0, "error": "no cookie for GET work.json"}
            raw = fetch_work_report_json(
                session, time_from=time_from, time_to=time_to, work_type_ids=work_type_ids,
            )
        finally:
            if prev is None:
                os.environ.pop("CROPWISE_SESSION_COOKIE", None)
            else:
                os.environ["CROPWISE_SESSION_COOKIE"] = prev
    if raw.get("status") != 200:
        return {
            "ok": False,
            "status": raw.get("status", 0),
            "error": raw.get("error", "GET failed"),
        }
    return {
        "ok": True,
        "status": 200,
        "payload": {
            "from_time": time_from,
            "to_time": time_to,
            "work_type_ids": ",".join(str(x) for x in work_type_ids),
            "method": "GET",
        },
        "body": raw.get("body"),
    }


def _work_response_score(work: dict[str, Any], fact_hints: tuple[str, ...] | list[str]) -> tuple[int, float]:
    """Приоритет: больше #task_id строк, затем выше «Всего»."""
    if not work.get("ok"):
        return (0, 0.0)
    body = work.get("body")
    if not isinstance(body, dict) or body.get("reportBlank"):
        return (0, 0.0)
    parsed = parse_work_report_body(body, [], list(fact_hints))
    task_rows = sum(
        1 for row in (parsed.get("detail_rows") or [])
        if task_id_from_label(str(row.get("label") or ""))
    )
    total = float(parsed.get("fact_total_ha") or 0)
    return (task_rows, total)


class WorkLoadJob:
    """Один work.json: операция или сводка, окно смены и виды работ."""

    def __init__(
        self,
        key: str,
        time_from: str,
        time_to: str,
        work_type_ids: list[int],
        fact_hints: tuple[str, ...],
    ) -> None:
        self.key = key
        self.time_from = time_from
        self.time_to = time_to
        self.work_type_ids = list(work_type_ids)
        self.fact_hints = tuple(fact_hints)


def ui_check_years(report_date: date | int) -> list[int]:
    """Сезоны проверки: текущий сезон и предыдущий. Не из констант и не из редактора.

    Сезон Y идёт с 01.07.(Y-1) по 01.07.Y: 07.10.2026 -> [2026, 2027],
    15.03.2027 -> [2026, 2027]. Число (старый вызов с календарным годом)
    трактуется как осень этого года.
    """
    if isinstance(report_date, date):
        current = season_year_for_date(report_date)
    else:
        current = int(report_date) + 1
    return [current - 1, current]


def read_ui_session_season(web: requests.Session) -> int | None:
    """Перечитать seasonValue со страницы work после переключения или пачки."""
    page = web.get(WORK_PAGE_URL, timeout=60)
    page.raise_for_status()
    return detect_ui_session_season(page.text)


def _not_loaded(year: int, reason: str) -> dict[str, Any]:
    return {
        "ok": False,
        "load_status": "not_loaded",
        "year": int(year),
        "error": reason,
        "body": None,
        "ui_session_seasonValue": None,
    }


def _unstable(year: int, reason: str) -> dict[str, Any]:
    return {
        "ok": False,
        "load_status": "unstable",
        "year": int(year),
        "error": reason,
        "body": None,
        "ui_session_seasonValue": int(year),
    }


def _response_kind(work: dict[str, Any]) -> str:
    if not work.get("ok"):
        return "error"
    body = work.get("body")
    if not isinstance(body, dict):
        return "error"
    if body.get("reportBlank"):
        return "empty"
    return "data"


def _fetch_job_once_or_retry(
    web: requests.Session,
    csrf: str,
    job: WorkLoadJob,
    year: int,
) -> dict[str, Any]:
    """Один запрос. Пустой или ошибочный — один повтор. Расхождение — «UI нестабилен»."""
    first = fetch_work_report(
        web, job.time_from, job.time_to, job.work_type_ids,
        csrf=csrf, ui_session_season=year,
    )
    if _response_kind(first) == "data":
        first["load_status"] = "loaded"
        first["ui_session_seasonValue"] = int(year)
        return first
    second = fetch_work_report(
        web, job.time_from, job.time_to, job.work_type_ids,
        csrf=csrf, ui_session_season=year,
    )
    k1, k2 = _response_kind(first), _response_kind(second)
    if k1 == "data" and k2 == "data":
        t1 = cropwise_total_ha(first, job.fact_hints)
        t2 = cropwise_total_ha(second, job.fact_hints)
        if abs(t1 - t2) <= 0.05:
            first["load_status"] = "loaded"
            first["ui_session_seasonValue"] = int(year)
            return first
        return _unstable(year, f"UI нестабилен: {t1:.2f} и {t2:.2f} га")
    if k1 == k2 == "empty":
        first["ok"] = True
        first["load_status"] = "empty"
        first["ui_session_seasonValue"] = int(year)
        return first
    if k1 != k2:
        return _unstable(year, "UI нестабилен: повтор work.json не совпал с первым ответом")
    reason = str(second.get("error") or first.get("error") or "work.json не загрузился")
    return _not_loaded(year, reason)


def load_works_for_seasons(
    cookie: str,
    jobs: list[WorkLoadJob],
    *,
    years: list[int] | None,
) -> dict[str, dict[int, dict[str, Any]]]:
    """
    Единственная загрузка work.json на прогон.

    Сезоны переключаются строго по очереди на одной сессии. Чужой ответ под
    нужным годом не записывается. Исходный сезон возвращается в finally.
    years=None — не переключать сезон, снять ответ текущей сессии.
    """
    stored: dict[str, dict[int, dict[str, Any]]] = {job.key: {} for job in jobs}
    if not jobs:
        return stored
    web, csrf, _ui_years, original = warm_web_session(cookie)
    switched_any = False
    try:
        if years is None:
            if original is None:
                for job in jobs:
                    stored[job.key][0] = _not_loaded(0, "сезон сессии не прочитан")
                return stored
            for job in jobs:
                stored[job.key][int(original)] = _fetch_job_once_or_retry(
                    web, csrf, job, int(original),
                )
            return stored
        current = original
        for year in years:
            year = int(year)
            switched: int | None
            if current == year:
                switched = current
            else:
                switched_any = True
                try:
                    switched = set_ui_session_year(web, csrf, year)
                except requests.RequestException as exc:
                    switched = None
                    reason = f"сезон {year} не переключился ({exc})"
                    for job in jobs:
                        stored[job.key][year] = _not_loaded(year, reason)
                    continue
            try:
                confirmed = read_ui_session_season(web)
            except requests.RequestException as exc:
                confirmed = None
                for job in jobs:
                    stored[job.key][year] = _not_loaded(year, f"сезон {year} не прочитан ({exc})")
                continue
            if switched != year or confirmed != year:
                for job in jobs:
                    stored[job.key][year] = _not_loaded(
                        year,
                        f"сезон сессии {confirmed}, нужен {year}",
                    )
                current = confirmed if confirmed is not None else current
                continue
            current = year
            batch = {
                job.key: _fetch_job_once_or_retry(web, csrf, job, year)
                for job in jobs
            }
            try:
                after = read_ui_session_season(web)
            except requests.RequestException:
                after = None
            if after != year:
                for job in jobs:
                    stored[job.key][year] = _not_loaded(
                        year, "сезон сессии изменился во время загрузки",
                    )
                current = after
                continue
            for job in jobs:
                stored[job.key][year] = batch[job.key]
        return stored
    finally:
        if switched_any and original is not None:
            try:
                set_ui_session_year(web, csrf, int(original))
            except requests.RequestException:
                pass


def pack_loaded_work(by_year: dict[int, dict[str, Any]], years: list[int]) -> dict[str, Any]:
    """Словарь сезонов для сверки. Один сезон в body не подставляется."""
    usable = [
        year for year in years
        if (by_year.get(year) or {}).get("load_status") in ("loaded", "empty")
    ]
    return {
        "ok": bool(usable),
        "by_year": by_year,
        "years": list(years),
        "body": None,
        "ui_session_seasonValue": None,
    }


def fetch_work_report_resilient(
    cookie: str,
    time_from: str,
    time_to: str,
    work_type_ids: list[int],
    fact_hints: tuple[str, ...] | list[str],
    *,
    attempts: int = 1,
    delay_sec: float = 0.0,
    preferred_ui_session: int | None = None,
) -> dict[str, Any]:
    """
    Совместимый вход: один сезон, без выбора наибольшего ответа.

    Проверка сводок ходит через load_works_for_seasons. Здесь остаётся
    одиночный запрос (вопрос к Cropwise), тоже с возвратом сессии.
    """
    del attempts, delay_sec
    job = WorkLoadJob(
        "one", time_from, time_to, list(work_type_ids), tuple(fact_hints),
    )
    years = [int(preferred_ui_session)] if preferred_ui_session is not None else None
    loaded = load_works_for_seasons(cookie, [job], years=years)
    slots = loaded.get("one") or {}
    if not slots:
        return _not_loaded(int(preferred_ui_session or 0), "work.json не загружен")
    return next(iter(slots.values()))


def _parse_ha_number(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = _WS_RE.sub("", str(value).strip())
    if not s or s in ("—", "-", "–", "−"):
        return 0.0
    # 1.282,35 → 1282.35; 1,282.35 → 1282.35
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _cell_ha(value: Any) -> float:
    if value is None:
        return 0.0
    # Колонка Cropwise — это группа видов работ («Обработка СЗР, га»), а ячейка —
    # список значений по work_type_names этой колонки. Сумма списка верна только
    # когда в запросе work.json были все виды работ группы (так делают
    # тематические сводки); сверка одной операции запрашивает свои work_type_ids,
    # и тогда в списке остаётся ровно её значение.
    if isinstance(value, list):
        return sum(_parse_ha_number(x) for x in value)
    return _parse_ha_number(value)


def column_work_types(col: dict | None) -> list[str]:
    """Виды работ Cropwise, попавшие в колонку-группу (work_type_names)."""
    if not col:
        return []
    return [str(x) for x in (col.get("work_type_names") or [])]


def matching_work_type_indexes(names: list[str], hints: list[str] | None) -> list[int] | None:
    """
    Индексы work_type_names, чьё имя точно совпало с запрошенной задачей.

    None — в колонке одна задача и она совпала: ячейка целиком её факт.
    [] — совпадений нет, сумму колонки брать нельзя.
    Непустой список — только эти позиции, остальные задачи колонки не входят.
    """
    from proverka.task_names import names_match

    if not names or not hints:
        return [] if names else None
    hits = [i for i, name in enumerate(names) if any(names_match(name, hint) for hint in hints)]
    if len(names) == 1 and len(hits) == 1:
        return None
    return hits


def selected_work_type_names(col: dict | None, hints: list[str] | None) -> list[str]:
    names = column_work_types(col)
    indexes = matching_work_type_indexes(names, hints)
    if indexes is None:
        return names
    return [names[i] for i in indexes]


def _normalize_name(name: str) -> str:
    s = name.replace('"', "").replace("«", "").replace("»", "").strip().lower()
    for prefix in ("ооо ", "аф ", "чо "):
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
    return s


def _is_skip_region(name: str) -> bool:
    rn = _normalize_name(name)
    if not rn:
        return False
    if rn in SKIP_REGION_GROUPS:
        return True
    return "под зданиями" in rn


def _display_name_aliases(firm_names: list[str]) -> dict[str, str]:
    """Нормализованное отображаемое имя (ООО …) → ключ агрофирмы."""
    out: dict[str, str] = {}
    for firm in firm_names:
        display = FIRM_DISPLAY_NAMES.get(firm)
        if display:
            out[_normalize_name(display)] = firm
    return out


def clear_subgroup_firm_cache() -> None:
    global _SUBGROUP_TO_FIRM
    _SUBGROUP_TO_FIRM = None


def subgroup_name_to_firm() -> dict[str, str]:
    """
    Подагрофирма Cropwise → основная агрофирма.

    В дереве «Культура / группа полей» на втором уровне стоят ООО «Агрофирма N»,
    ЧО Полив и другие группы. В смене их может не быть — это нормально.
    Если площадь есть, она относится к одной из семи агрофирм-родителей.
    """
    global _SUBGROUP_TO_FIRM
    if _SUBGROUP_TO_FIRM is not None:
        return _SUBGROUP_TO_FIRM
    try:
        from plans import FOLDER_TO_FIRM
        from proverka.season_cropwise import _get_all
    except ImportError:
        return {}
    try:
        folders = _get_all("group_folders")
        groups = _get_all("field_groups")
    except Exception:
        return {}
    parent = {g.get("id"): g.get("parent_id") for g in folders if g.get("id") is not None}
    roots = {g.get("id") for g in folders if g.get("id") is not None and not g.get("parent_id")}

    def root_of(folder_id: Any) -> Any:
        seen: set[Any] = set()
        while folder_id and folder_id not in seen:
            seen.add(folder_id)
            if folder_id in roots:
                return folder_id
            folder_id = parent.get(folder_id)
        return None

    out: dict[str, str] = {}
    for group in groups:
        name = str(group.get("name") or "").strip()
        firm = FOLDER_TO_FIRM.get(root_of(group.get("group_folder_id")))
        if not name or not firm:
            continue
        key = _normalize_name(name)
        prev = out.get(key)
        if prev and prev != firm:
            continue
        out[key] = firm
    _SUBGROUP_TO_FIRM = out
    return out


def _match_firm(
    row_name: str,
    firm_names: list[str],
    *,
    firm_ctx: str | None = None,
    display_aliases: dict[str, str] | None = None,
) -> str | None:
    """
    Точное сопоставление строки UI с агрофирмой.

    Подстрочное сравнение применяется только если кандидат ровно один:
    иначе фирма, чьё имя является подстрокой другой, уводила строки не туда.
    """
    rn = _normalize_name(row_name)
    if not rn or rn in _TOTAL_LABELS:
        return None
    if _is_skip_region(row_name):
        return None
    aliases = display_aliases or _display_name_aliases(firm_names)
    if rn in aliases:
        return aliases[rn]
    for firm in firm_names:
        if rn == _normalize_name(firm):
            return firm
    partial = [
        firm for firm in firm_names
        if (fn := _normalize_name(firm)) and (fn in rn or rn in fn)
    ]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        # Неоднозначность: выбираем самое длинное совпадение, но только если
        # оно строго длиннее остальных — иначе отказываемся сопоставлять.
        ranked = sorted(partial, key=lambda f: -len(_normalize_name(f)))
        if len(_normalize_name(ranked[0])) > len(_normalize_name(ranked[1])):
            return ranked[0]
        return None
    # Группа полей внутри агрофирмы (не каждый день с площадью).
    mapped = subgroup_name_to_firm().get(rn)
    if mapped and (not firm_names or mapped in firm_names):
        return mapped
    # ООО «Агрофирма N» — юрлицо подразделения; привязываем к родительской агрофирме.
    if firm_ctx and _SUBDIVISION_RE.search(rn):
        return firm_ctx
    return None


def _is_label_column(col: dict) -> bool:
    """Колонка-заголовок строк (не метрика в га)."""
    name = (col.get("name") or "").strip()
    low = name.lower()
    if col.get("key") == "name":
        return True
    if "площадь" in low and low.startswith("площадь"):
        return True
    # «Культура / Поле / …» — не «Обработка почвы, га»
    if "/" in name and "га" not in low:
        return True
    return False


def _column_matches_tasks(col: dict, hints: list[str]) -> bool:
    names = column_work_types(col)
    if not names or not hints:
        return False
    indexes = matching_work_type_indexes(names, hints)
    return indexes is None or bool(indexes)


def pick_fact_column(columns: list[dict], hints: list[str]) -> tuple[dict | None, str]:
    """
    Первая колонка, в которой есть точно запрошенная задача.

    confidence: "exact" — имя задачи совпало; "none" — совпадения нет.
    Колонку по слову «га» или по единственной «Площади» не выбираем.
    """
    for col in columns:
        if _is_label_column(col):
            continue
        if _column_matches_tasks(col, hints):
            return col, "exact"
    return None, "none"


def matching_fact_columns(columns: list[dict], hints: list[str]) -> list[dict]:
    """Все колонки, где есть хотя бы одна точно запрошенная задача.

    «Уборка» и «Кормозаготовка» суммируются, только если в каждой лежит
    своя задача из запроса, а не потому что в заголовке есть «га».
    """
    matched: list[dict] = []
    seen: set[Any] = set()
    for col in columns:
        if _is_label_column(col):
            continue
        key = col.get("key")
        if key in seen or not _column_matches_tasks(col, hints):
            continue
        matched.append(col)
        seen.add(key)
    return matched


def fact_column_mismatch(columns: list[dict], hints: list[str]) -> str | None:
    """Точная причина, почему колонку факта нельзя использовать. None — всё совпало."""
    from proverka.task_names import is_known_task, names_match

    requested = [str(hint).strip() for hint in hints if str(hint or "").strip()]
    if not requested:
        return "не задано точное имя задачи"
    present: list[str] = []
    unknown: list[str] = []
    bare: list[str] = []
    for col in columns:
        if _is_label_column(col):
            continue
        title = (col.get("name") or col.get("key") or "").strip()
        names = column_work_types(col)
        if not names:
            if title:
                bare.append(title)
            continue
        for name in names:
            present.append(name)
            if not is_known_task(name):
                unknown.append(name)
    matched = matching_fact_columns(columns, requested)
    skipped = [
        name for name in present
        if is_known_task(name) and not any(names_match(name, hint) for hint in requested)
    ]
    parts: list[str] = []
    if unknown:
        parts.append(
            "неизвестная задача: "
            + ", ".join(f"«{name}»" for name in unknown)
            + ". В списке допустимых её нет"
        )
    if not matched:
        if present:
            parts.append(
                "нет точного совпадения. Ожидали: "
                + ", ".join(f"«{name}»" for name in requested)
                + ". В ответе: "
                + ", ".join(f"«{name}»" for name in present)
            )
        elif bare:
            parts.append(
                "нет точного совпадения. Ожидали: "
                + ", ".join(f"«{name}»" for name in requested)
                + ". Колонки без списка задач: "
                + ", ".join(f"«{name}»" for name in bare)
            )
        else:
            parts.append(
                "нет точного совпадения. Ожидали: "
                + ", ".join(f"«{name}»" for name in requested)
                + ". В ответе колонок с задачами нет"
            )
    elif bare:
        parts.append(
            "колонки без списка задач, гектары не взяты: "
            + ", ".join(f"«{name}»" for name in bare)
        )
    if matched and skipped:
        parts.append(
            "не взяты, имя не совпало с запросом: "
            + ", ".join(f"«{name}»" for name in skipped)
        )
    if not parts:
        return None
    return "; ".join(parts)


def cell_shape_error(rows: list, columns: list[dict], hints: list[str]) -> str | None:
    """Ячейка-список должна быть той же длины, что work_type_names. Иначе это не «ноль гектаров»."""
    fact_cols = matching_fact_columns(columns, hints)

    def walk(nodes: list) -> str | None:
        for row in nodes or []:
            if not isinstance(row, dict):
                continue
            for col in fact_cols:
                names = column_work_types(col)
                indexes = matching_work_type_indexes(names, hints)
                if not indexes:
                    continue
                value = row.get(col.get("key"))
                if value is None or value == "":
                    continue
                if isinstance(value, list) and len(value) == len(names):
                    continue
                got = len(value) if isinstance(value, list) else type(value).__name__
                title = col.get("name") or col.get("key")
                return (
                    f"колонка «{title}»: в ячейке {got} значений, "
                    f"задач {len(names)} ({', '.join(names)})"
                )
            found = walk(row.get("children") or [])
            if found:
                return found
        return None

    return walk(rows)


def _pick_fact_column(columns: list[dict], hints: list[str]) -> dict | None:
    return pick_fact_column(columns, hints)[0]


def _row_metric_ha(row: dict, col: dict | None, hints: list[str] | None = None) -> float:
    if not col:
        return 0.0
    value = row.get(col.get("key"))
    names = column_work_types(col)
    indexes = matching_work_type_indexes(names, hints)
    if indexes is not None:
        if not indexes or not isinstance(value, list) or len(value) != len(names):
            return 0.0
        return sum(_parse_ha_number(value[i]) for i in indexes if 0 <= i < len(value))
    return _cell_ha(value)


def _parse_tab(
    rows: list,
    columns: list,
    firm_names: list[str],
    fact_hints: list[str],
    *,
    region_groups: bool = False,
) -> dict[str, Any]:
    fact_cols = matching_fact_columns(columns, fact_hints)
    fact_col = fact_cols[0] if fact_cols else None
    _picked, fact_confidence = pick_fact_column(columns, fact_hints)
    fact_column_error = fact_column_mismatch(columns, fact_hints)
    shape_error = cell_shape_error(rows, columns, fact_hints)
    if shape_error:
        fact_column_error = (
            f"{fact_column_error}; {shape_error}" if fact_column_error else shape_error
        )
    if fact_cols and not fact_column_error:
        fact_confidence = "exact"

    def _fact_ha(row: dict) -> float:
        return sum(_row_metric_ha(row, col, fact_hints) for col in fact_cols)
    area_col = next((c for c in columns if "Площадь" in (c.get("name") or "")), None)
    display_aliases = _display_name_aliases(firm_names)

    fact_by_firm: dict[str, float] = {f: 0.0 for f in firm_names}

    def _add_firm(name: str, ha: float) -> None:
        # Оценка ответа work.json вызывает разбор с пустым списком фирм.
        # Сопоставление подагрофирмы тогда даёт имя родителя, которого ещё нет в словаре.
        fact_by_firm[name] = fact_by_firm.get(name, 0.0) + ha
    # Гектары строк верхнего уровня, которые не удалось привязать к агрофирме.
    # Раньше они молча исчезали; теперь видны и входят в итог.
    unmatched_ha = 0.0
    unmatched_rows: list[str] = []
    root_total = 0.0
    nested_totals: list[float] = []

    def _sum_subdivisions(children: list) -> float:
        total = 0.0
        for child in children:
            sub_name = (child.get("name") or "").strip()
            if _is_skip_region(sub_name):
                continue
            total += _fact_ha(child)
        return total

    def walk(nodes: list, depth: int = 0, firm_ctx: str | None = None) -> None:
        nonlocal root_total, unmatched_ha
        for row in nodes:
            name = (row.get("name") or "").strip()
            low = _normalize_name(name)
            children = row.get("children") or []
            if low in _TOTAL_LABELS:
                # «Всего» берём ТОЛЬКО с корня: вложенные итоги групп раньше
                # затирали общий итог (побеждала последняя встреченная строка).
                if depth == 0:
                    root_total = _fact_ha(row)
                else:
                    nested_totals.append(_fact_ha(row))
                continue
            if _is_skip_region(name):
                continue

            matched = _match_firm(
                name, firm_names, firm_ctx=firm_ctx, display_aliases=display_aliases,
            )
            firm = matched or firm_ctx

            # regionRows: верхний уровень = агрофирма, дети = подразделения (ООО «Агрофирма N»).
            if region_groups and depth == 0 and matched:
                if children:
                    _add_firm(matched, _sum_subdivisions(children))
                else:
                    _add_firm(matched, _fact_ha(row))
                continue

            if region_groups and depth == 0 and not matched:
                ha = _fact_ha(row)
                if ha > 0:
                    unmatched_ha += ha
                    unmatched_rows.append(name)
                continue

            if firm:
                nested = [
                    c for c in children
                    if _match_firm(
                        (c.get("name") or ""),
                        firm_names,
                        firm_ctx=firm,
                        display_aliases=display_aliases,
                    )
                ]
                if not nested:
                    _add_firm(firm, _fact_ha(row))
            walk(children, depth + 1, firm or firm_ctx)

    walk(rows or [])
    sum_by_firm = sum(fact_by_firm.values()) + unmatched_ha
    fact_total = root_total if root_total > 0 else sum_by_firm

    return {
        "fact_by_firm": {k: round(v, 2) for k, v in fact_by_firm.items() if v > 0},
        "fact_total_ha": round(fact_total, 2),
        "fact_total_ha_root_row": round(root_total, 2),
        "fact_total_ha_sum_rows": round(sum_by_firm, 2),
        # Невязка «строка Всего» − «сумма строк»: если не 0, дерево разобрано не полностью.
        "fact_total_residual_ha": round(fact_total - sum_by_firm, 2),
        "unmatched_firm_ha": round(unmatched_ha, 2),
        "unmatched_firm_rows": unmatched_rows[:20],
        "fact_column": " + ".join(
            str(c.get("name") or "") for c in fact_cols if c.get("name")
        ) or (fact_col or {}).get("name"),
        "fact_column_confidence": fact_confidence,
        "fact_column_error": fact_column_error,
        "fact_work_types": [
            name
            for col in fact_cols
            for name in selected_work_type_names(col, fact_hints)
        ],
        "area_column": (area_col or {}).get("name"),
        "column_titles": [c.get("name") for c in columns],
    }


def _empty_parse_result(error: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "fact_by_firm": {},
        "fact_total_ha": 0.0,
        "fact_column": None,
        "detail_rows": [],
    }
    if error:
        out["error"] = error
    return out


def task_id_from_label(label: str) -> int | None:
    m = _TASK_ID_RE.search(label or "")
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def aggregate_ui_task_rows(
    detail_rows: list[dict[str, Any]],
    valid_task_ids: set[int] | None,
    *,
    min_task_level: int = 0,
    drop_orphans: bool = False,
) -> dict[str, Any]:
    """
    Сумма по UI по строкам-заданиям (#task_id).

    Раньше здесь было два молчаливых источника недосчёта:
      * min_task_level=5 — жёсткая глубина дерева; ветки без уровня
        подразделения выпадали целиком;
      * задания, которых нет в valid_task_ids, просто исчезали из итога.

    Теперь глубина не фильтруется (достаточно признака листа + «#id»), а
    «сироты» по умолчанию входят в итог и одновременно учитываются отдельно,
    чтобы расхождение с API было видно, а не растворялось в цифре.
    """
    by_firm: dict[str, float] = defaultdict(float)
    by_subdivision: dict[str, float] = defaultdict(float)
    orphan_tasks: dict[int, float] = defaultdict(float)
    used_rows: list[dict[str, Any]] = []
    orphan_ha = 0.0
    no_firm_ha = 0.0
    below_level_ha = 0.0
    seen_paths: set[str] = set()

    for row in detail_rows:
        # Агрегатные узлы дерева не суммируем — иначе двойной счёт с их листьями.
        if row.get("aggregate"):
            continue
        tid = task_id_from_label(str(row.get("label") or ""))
        if tid is None:
            continue
        ha = float(row.get("area_ha") or 0)
        if ha <= 0:
            continue
        path = str(row.get("path") or "")
        if path and path in seen_paths:
            continue
        if path:
            seen_paths.add(path)
        if min_task_level and int(row.get("level") or 0) < min_task_level:
            below_level_ha += ha
            continue

        is_orphan = valid_task_ids is not None and tid not in valid_task_ids
        if is_orphan:
            orphan_tasks[tid] += ha
            orphan_ha += ha
            if drop_orphans:
                continue

        firm = str(row.get("firm") or "")
        if firm:
            by_firm[firm] += ha
        else:
            no_firm_ha += ha
        parts = [p.strip() for p in path.split("→")]
        if len(parts) >= 2:
            sub_key = f"{parts[0]} → {parts[1]}"
            by_subdivision[sub_key] += ha
        row_copy = dict(row)
        row_copy["task_id"] = tid
        row_copy["orphan"] = is_orphan
        used_rows.append(row_copy)

    # Итог = гектары всех учтённых строк, включая те, у которых не определилась
    # агрофирма (раньше total = sum(by_firm) и такие строки пропадали).
    total = sum(by_firm.values()) + no_firm_ha
    return {
        "fact_by_firm": {k: round(v, 2) for k, v in sorted(by_firm.items(), key=lambda x: -x[1]) if v > 0},
        "fact_total_ha": round(total, 2),
        "by_subdivision": {k: round(v, 2) for k, v in sorted(by_subdivision.items()) if v > 0},
        "orphan_task_ids": {int(k): round(v, 2) for k, v in sorted(orphan_tasks.items())},
        "orphan_ha": round(orphan_ha, 2),
        "no_firm_ha": round(no_firm_ha, 2),
        "below_level_ha": round(below_level_ha, 2),
        "task_rows": used_rows,
    }


def parse_work_report_detail_rows(
    rows: list,
    columns: list,
    firm_names: list[str],
    fact_hints: list[str],
) -> list[dict[str, Any]]:
    """Листья дерева regionRows: путь, агрофирма, метка (поле/задание), га."""
    fact_cols = matching_fact_columns(columns, fact_hints)
    if not fact_cols:
        return []

    def _detail_ha(row: dict) -> float:
        return sum(_row_metric_ha(row, col, fact_hints) for col in fact_cols)

    display_aliases = _display_name_aliases(firm_names)
    out: list[dict[str, Any]] = []

    def walk(nodes: list, path: list[str], firm_ctx: str | None, depth: int = 0) -> None:
        for row in nodes:
            name = (row.get("name") or "").strip()
            if not name:
                continue
            low = name.lower()
            if low in ("всего", "итого", "total"):
                continue
            if _is_skip_region(name):
                continue
            children = row.get("children") or []
            matched = _match_firm(
                name, firm_names, firm_ctx=firm_ctx, display_aliases=display_aliases,
            )
            firm = matched or firm_ctx
            path_new = path + [name]
            fact = _detail_ha(row)

            if depth == 0 and matched and children:
                for sub in children:
                    sub_name = (sub.get("name") or "").strip()
                    if _is_skip_region(sub_name):
                        continue
                    # Бездетное подразделение обойдёт walk ниже и выдаст его как
                    # лист — не выводим его здесь, иначе строка задваивается.
                    if not (sub.get("children") or []):
                        continue
                    sub_ha = _detail_ha(sub)
                    if sub_ha > 0:
                        out.append({
                            "firm": matched,
                            "path": " → ".join(path_new + [sub_name]),
                            "label": sub_name,
                            "area_ha": round(sub_ha, 2),
                            "level": len(path_new) + 1,
                            "aggregate": True,
                            "source": "cropwise_ui",
                        })
                walk(children, path_new, matched, depth + 1)
                continue

            if not children:
                if fact > 0:
                    out.append({
                        "firm": firm or "",
                        "path": " → ".join(path_new),
                        "label": name,
                        "area_ha": round(fact, 2),
                        "level": len(path_new),
                        "aggregate": False,
                        "source": "cropwise_ui",
                    })
            else:
                walk(children, path_new, firm, depth + 1)

    walk(rows or [], [], None)
    out.sort(key=lambda x: (-x["area_ha"], x.get("firm") or "", x["label"]))
    return out


def parse_work_report_body(
    body: Any,
    firm_names: list[str],
    fact_column_hints: list[str],
    *,
    valid_task_ids: set[int] | None = None,
) -> dict[str, Any]:
    if not isinstance(body, dict):
        return _empty_parse_result("body is not an object")
    if body.get("reportBlank"):
        return _empty_parse_result("reportBlank=true (нет данных за период/фильтр)")

    by_regions = _parse_tab(
        body.get("regionRows") or [],
        body.get("regionColumns") or [],
        firm_names,
        fact_column_hints,
        region_groups=True,
    )
    by_crops = _parse_tab(
        body.get("cropRows") or [],
        body.get("cropColumns") or [],
        firm_names,
        fact_column_hints,
        region_groups=False,
    )
    primary = by_regions if by_regions["fact_total_ha"] or by_regions["fact_by_firm"] else by_crops
    region_rows = body.get("regionRows") or []
    region_cols = body.get("regionColumns") or []
    detail_rows = parse_work_report_detail_rows(
        region_rows, region_cols, firm_names, fact_column_hints,
    )
    if not detail_rows:
        detail_rows = parse_work_report_detail_rows(
            body.get("cropRows") or [],
            body.get("cropColumns") or [],
            firm_names,
            fact_column_hints,
        )
    out = {
        "by_regions": by_regions,
        "by_crops": by_crops,
        "fact_by_firm": primary["fact_by_firm"],
        "fact_total_ha": primary["fact_total_ha"],
        "fact_column": primary.get("fact_column"),
        "detail_rows": detail_rows,
    }
    out["fact_column_confidence"] = primary.get("fact_column_confidence")
    column_error = primary.get("fact_column_error")
    if not (primary.get("fact_total_ha") or primary.get("fact_by_firm")):
        column_error = (
            by_regions.get("fact_column_error") or by_crops.get("fact_column_error")
        )
    out["fact_column_error"] = column_error
    out["fact_work_types"] = primary.get("fact_work_types") or []
    out["unmatched_firm_ha"] = primary.get("unmatched_firm_ha", 0.0)
    out["fact_total_residual_ha"] = primary.get("fact_total_residual_ha", 0.0)
    if valid_task_ids is not None and detail_rows:
        verified = aggregate_ui_task_rows(detail_rows, valid_task_ids)
        out["fact_by_firm_verified"] = verified["fact_by_firm"]
        out["fact_total_ha_verified"] = verified["fact_total_ha"]
        out["ui_orphan_tasks"] = verified["orphan_task_ids"]
        out["ui_orphan_ha"] = verified["orphan_ha"]
        out["ui_no_firm_ha"] = verified["no_firm_ha"]
        out["by_subdivision_verified"] = verified["by_subdivision"]
        out["ui_balance"] = ui_balance(
            float(out.get("fact_total_ha") or 0), verified,
        )
    return out


def ui_balance(raw_total: float, verified: dict[str, Any]) -> dict[str, Any]:
    """
    Сходимость итога UI: «Всего» = учтённые задания + сироты + строки без фирмы
    + отброшенные по глубине. Если residual != 0, часть дерева не разобрана —
    именно это раньше давало необъяснимые расхождения вида 1766 vs 1545.
    """
    accounted = float(verified.get("fact_total_ha") or 0)
    dropped = float(verified.get("below_level_ha") or 0)
    residual = raw_total - accounted - dropped
    return {
        "raw_total_ha": round(raw_total, 2),
        "accounted_ha": round(accounted, 2),
        "orphan_ha": round(float(verified.get("orphan_ha") or 0), 2),
        "no_firm_ha": round(float(verified.get("no_firm_ha") or 0), 2),
        "below_level_ha": round(dropped, 2),
        "residual_ha": round(residual, 2),
        "balanced": abs(residual) <= max(1.0, raw_total * 0.005),
    }
