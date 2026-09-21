from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock, Thread

import httpx
import requests

from app.config import MISSING_CROPWISE_TOKEN_DETAIL, get_cropwise_token, settings

# Как в svodki/plan/plans.py и desktop works (2).py — фиксированное смещение МСК для смен и срезов API.
TZ_MSK = timezone(timedelta(hours=3))

logger = logging.getLogger(__name__)

DAY_START_HOUR = 6
MTFMI_TASK_BATCH_SIZE = 200
MTFMI_SEMAPHORE_LIMIT = 40  # как в works (2).py

WORK_GROUPS: tuple[tuple[str, tuple[int, ...]], ...] = (
    # Синхронизировано с рабочими скриптами в svodki/plan/plans.py.
    ("1.1 Внесение минеральных удобрений", (55,)),
    ("2.1 Боронование", (103, 81, 97, 12)),
    ("2.9 Дискование", (60,)),
    ("2.4 Вспашка", (49, 57, 171)),
    ("2.11 Культивация", (96,)),
    ("4.0 Посев", (3, 4, 69, 70, 71, 72, 119, 120)),
    ("4.1 Посев (овощи)", (68, 148, 172)),
    ("3.1 Опрыскивание", (1, 64, 65, 66, 153)),
    ("2.12 Прикатывание", (13, 67, 142)),
    ("2.7 Гребнеобразование", (62,)),
    ("2.6 Фрезерование", (73,)),
    ("2.8 Глубокорыхление", (11, 106, 121, 150)),
    ("5.0 Уборка", (6, 7, 75, 76, 77, 111, 118, 125, 126, 145)),
    ("5.4 Уборка картофеля, лука", (76,)),
)

SKIP_CROPS: set[str] = {
    "Залежь, лес", "Залежь", "Введение в оборот 2026 год",
    "Луговые травы", "Обмен", "В пользовании у фермера",
    "Введение в оборот 2027 год", "Введение в оборот",
    "Передано в аренду", "Дороги/лесополосы", "Овраги/неудобия", "crop_None",
}

FIRM_TO_FOLDER: dict[str, int] = {
    "АФ Чистопольская": 3,
    "АгроРаздолье": 4,
    "Аксу-Агро": 5,
    "НурлатАгро": 11,
    "Дрожжаное Рассвет Агро": 6,
    "Актай": 7,
    "Лениногорская Птицефабрика": 9,
}
FOLDER_TO_FIRM: dict[int, str] = {v: k for k, v in FIRM_TO_FOLDER.items()}
# Порядок строк как в desktop plans.py / works (2).py
FIRM_ORDER: tuple[str, ...] = tuple(FIRM_TO_FOLDER.keys())

FIELD_NAME_FORCE_RULES: tuple[tuple[str, str], ...] = (
    ("птицефабрик", "Лениногорская Птицефабрика"),
    ("лпф", "Лениногорская Птицефабрика"),
    ("лп0115", "Лениногорская Птицефабрика"),
    ("lp0115", "Лениногорская Птицефабрика"),
    ("пф0115", "Лениногорская Птицефабрика"),
)

_state_lock = Lock()
_is_running = False
_last_error = ""


def _cache_path() -> Path:
    root = Path(settings.upload_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root / "works_summary_cache.json"


def _excel_dir() -> Path:
    root = Path(settings.upload_dir) / "works-excel"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _norm(s: str) -> str:
    return (s or "").strip().casefold().replace(" ", "").replace("\u00a0", "")


def _parse_dt_msk(value) -> datetime | None:
    """Как parse_dt() в works (2).py: все метки времени приводим к TZ_MSK для смены и hourly."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ_MSK)
        except Exception:
            return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.replace(tzinfo=TZ_MSK) if dt.tzinfo is None else dt.astimezone(TZ_MSK)


def _get_shift(end_time_raw, d_from: date, d_to: date) -> tuple[date, datetime] | None:
    """Как get_shift() в works (2).py — календарный день смены по московскому времени."""
    end_dt = _parse_dt_msk(end_time_raw)
    if end_dt is None:
        return None
    cal = end_dt.date()
    anchor = datetime(cal.year, cal.month, cal.day, DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK)
    if end_dt < anchor:
        cal = cal - timedelta(days=1)
        cutoff = anchor
    else:
        cutoff = anchor + timedelta(days=1)
    if not (d_from <= cal <= d_to):
        return None
    return cal, cutoff


def _get_all(session: requests.Session, base_url: str, endpoint: str, params: dict | None = None) -> list[dict]:
    p = dict(params or {})
    results, from_id = [], 0
    for _ in range(500):
        r = session.get(
            f"{base_url}/{endpoint}",
            params={**p, "from_id": from_id, "limit": 1000},
            timeout=120,
        )
        r.raise_for_status()
        body = r.json()
        chunk = body.get("data", [])
        if not chunk:
            break
        results.extend(chunk)
        meta = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        if not last_id or meta.get("obtained_records", 0) < 100:
            break
        from_id = int(last_id) + 1
    return results


def _get_all_page(session: requests.Session, base_url: str, endpoint: str, params: dict | None = None) -> list[dict]:
    """Cropwise: часть справочников отдаётся постранично через `page`, а не `from_id` (как в field_season_summary)."""
    p = dict(params or {})
    p.setdefault("per_page", 1000)
    results, page = [], 1
    while True:
        r = session.get(f"{base_url}/{endpoint}", params={**p, "page": page}, timeout=120)
        r.raise_for_status()
        body = r.json()
        chunk = body if isinstance(body, list) else body.get("data", [])
        results.extend(chunk)
        if len(chunk) < p["per_page"]:
            break
        page += 1
    return results


def _parent_map(gfs: list[dict]) -> dict[int, int | None]:
    out: dict[int, int | None] = {}
    for gf in gfs:
        fid = gf.get("id")
        if fid is None:
            continue
        pid = gf.get("parent_id") or gf.get("parent_group_folder_id") or gf.get("group_folder_parent_id")
        out[int(fid)] = int(pid) if pid is not None else None
    return out


def _chain(folder_id: int | None, pm: dict[int, int | None]) -> list[int]:
    if folder_id is None:
        return []
    out: list[int] = []
    cur = int(folder_id)
    seen: set[int] = set()
    for _ in range(64):
        if cur in seen:
            break
        seen.add(cur)
        out.append(cur)
        nxt = pm.get(cur)
        if nxt is None:
            break
        cur = int(nxt)
    return out


def _to_root(folder_id: int | None, pm: dict[int, int | None], roots: set[int]) -> int | None:
    if folder_id is None:
        return None
    cur = int(folder_id)
    seen: set[int] = set()
    for _ in range(64):
        if cur in roots:
            return cur
        if cur in seen:
            return None
        seen.add(cur)
        nxt = pm.get(cur)
        if nxt is None:
            return None
        cur = int(nxt)
    return None


def _build_field_firm(fields: list[dict], fgs: list[dict], gfs: list[dict]) -> dict[int, str]:
    fgs_map = {int(fg["id"]): fg for fg in fgs if fg.get("id") is not None}
    pm = _parent_map(gfs)
    roots = set(FIRM_TO_FOLDER.values())

    hints: dict[int, str] = {}
    for gf in gfs:
        fid = gf.get("id")
        if fid is None:
            continue
        blob = _norm(f"{gf.get('name') or ''} {gf.get('title') or ''}")
        if any(x in blob for x in ("лениногор", "птицефабрик", "лпф", "lpf")):
            hints[int(fid)] = "Лениногорская Птицефабрика"

    result: dict[int, str] = {}
    for field in fields:
        fid = field.get("id")
        if fid is None:
            continue
        fgid = field.get("field_group_id")
        folder_id = field.get("group_folder_id")
        if folder_id is None and fgid is not None:
            folder_id = fgs_map.get(int(fgid), {}).get("group_folder_id")
        rint = int(folder_id) if folder_id is not None else None
        firm = None
        for cid in _chain(rint, pm):
            if cid in hints:
                firm = hints[cid]
                break
        if firm is None:
            root = _to_root(rint, pm, roots)
            if root is not None:
                firm = FOLDER_TO_FIRM.get(root)
        if firm:
            result[int(fid)] = firm

    for field in fields:
        fid = field.get("id")
        if fid is None:
            continue
        blob = _norm(f"{field.get('name') or ''} {field.get('title') or ''}")
        for needle, firm_key in FIELD_NAME_FORCE_RULES:
            if needle in blob:
                result[int(fid)] = firm_key
                break
    return result


async def _pages(client: httpx.AsyncClient, url: str, params: dict, sem: asyncio.Semaphore) -> list[dict]:
    items: list[dict] = []
    from_id = 0
    while True:
        async with sem:
            r = await client.get(url, params={**params, "from_id": from_id, "limit": 1000})
        r.raise_for_status()
        body = r.json()
        chunk = body.get("data", [])
        if not chunk:
            break
        items.extend(chunk)
        meta = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        if not last_id or meta.get("obtained_records", 0) < 100:
            break
        from_id = int(last_id) + 1
    return items


async def _mtfmi_async(base_url: str, token: str, task_ids: list[int]) -> dict[int, list[dict]]:
    sem = asyncio.Semaphore(MTFMI_SEMAPHORE_LIMIT)
    url = f"{base_url}/machine_task_field_mapping_items"
    async with httpx.AsyncClient(headers={"X-User-Api-Token": token}, timeout=120) as client:
        results = await asyncio.gather(*[_pages(client, url, {"machine_task_id": tid}, sem) for tid in task_ids])
    return {tid: items for tid, items in zip(task_ids, results)}


def _load_field_crop(
    session: requests.Session,
    base_url: str,
    season_year: int,
    crop_map: dict[int, str],
) -> dict[int, str]:
    his = _get_all(session, base_url, "history_items", {"year": str(season_year)})
    out: dict[int, str] = {}
    for hi in his:
        fid = hi.get("field_id")
        cid = hi.get("crop_id")
        if not (fid and cid and hi.get("active")):
            continue
        crop = crop_map.get(int(cid), "")
        if crop and crop not in SKIP_CROPS:
            out[int(fid)] = crop
    return out


def _collect_for_year(
    session: requests.Session,
    base_url: str,
    token: str,
    season_year: int,
    d_from: date,
    d_to: date,
    field_crop: dict[int, str],
    field_firm: dict[int, str],
    wt_to_group: dict[int, str],
    all_wt_ids: set[int],
) -> dict[str, dict[str, dict[str, dict[date, float]]]]:
    season_start = datetime(season_year, 1, 1, DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK)
    cutoff_end = datetime(d_to.year, d_to.month, d_to.day, DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK) + timedelta(days=1)

    all_tasks: list[dict] = []
    for wt_id in sorted(all_wt_ids):
        all_tasks.extend(
            _get_all(
                session,
                base_url,
                "machine_tasks",
                {
                    "work_type_id": wt_id,
                    "end_time_gt_eq": season_start.strftime("%Y-%m-%dT%H:%M:%S"),
                    "end_time_lt": cutoff_end.strftime("%Y-%m-%dT%H:%M:%S"),
                },
            )
        )
    all_tasks = [t for t in all_tasks if str(t.get("status") or "").strip().lower() != "canceled"]

    task_shift: dict[int, tuple[date, datetime]] = {}
    for t in all_tasks:
        info = _get_shift(t.get("end_time"), d_from, d_to)
        if info:
            task_shift[int(t["id"])] = info
    in_period = [t for t in all_tasks if int(t["id"]) in task_shift]
    if not in_period:
        return {}

    task_ids = [int(t["id"]) for t in in_period]
    total_task_ids = len(task_ids)
    logger.info(
        "works_summary: year=%s in_period_tasks=%s mtfmi_batch_size=%s",
        season_year,
        total_task_ids,
        MTFMI_TASK_BATCH_SIZE,
    )
    mtfmi: dict[int, list[dict]] = {}
    for start in range(0, total_task_ids, MTFMI_TASK_BATCH_SIZE):
        chunk = task_ids[start : start + MTFMI_TASK_BATCH_SIZE]
        mtfmi.update(asyncio.run(_mtfmi_async(base_url, token, chunk)))
        logger.info(
            "works_summary: year=%s mtfmi_progress=%s/%s",
            season_year,
            min(start + len(chunk), total_task_ids),
            total_task_ids,
        )
    data: dict[str, dict[str, dict[str, dict[date, float]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    )
    for task in in_period:
        tid = int(task["id"])
        t_date, cutoff = task_shift[tid]
        wt_id = task.get("work_type_id")
        group = wt_to_group.get(int(wt_id)) if wt_id is not None else None
        if not group:
            continue
        for item in mtfmi.get(tid, []):
            fid = item.get("field_id")
            if not fid:
                continue
            fid_i = int(fid)
            firm = field_firm.get(fid_i)
            if not firm:
                continue
            crop = field_crop.get(fid_i, "")
            if not crop or crop in SKIP_CROPS:
                continue
            hourly = item.get("covered_area_hourly") or []
            cov = 0.0
            for e in hourly:
                dt_h = _parse_dt_msk(e[0])
                if dt_h and dt_h < cutoff:
                    cov += float(e[1] or 0)
            if not hourly:
                cov = float(item.get("covered_area") or 0)
            if cov < 0.5:
                continue
            data[group][crop][firm][t_date] += cov
    return data


def _works_bounds(season_year: int) -> tuple[date, date, date, date]:
    """Как build_report() в works (2).py: WORKS_DATE_FROM/TO или 1 янв — сегодня; прошлый период — год − 1."""
    today = date.today()
    df_s = os.environ.get("WORKS_DATE_FROM", "").strip()
    dt_s = os.environ.get("WORKS_DATE_TO", "").strip()
    try:
        d_from = date.fromisoformat(df_s) if df_s else date(season_year, 1, 1)
    except ValueError:
        d_from = date(season_year, 1, 1)
    try:
        d_to = date.fromisoformat(dt_s) if dt_s else today
    except ValueError:
        d_to = today
    if d_to < d_from:
        d_to = d_from
    d_from_prev = d_from.replace(year=d_from.year - 1)
    d_to_prev = d_to.replace(year=d_to.year - 1)
    return d_from, d_to, d_from_prev, d_to_prev


def _build_rows() -> dict:
    token = get_cropwise_token()
    if not token:
        raise ValueError(MISSING_CROPWISE_TOKEN_DETAIL)
    base_url = (settings.cropwise_base_url or "").strip() or "https://operations.cropwise.com/api/v3"
    season_year = int(settings.cropwise_season_year or date.today().year)
    prev_year = season_year - 1

    d_from, d_to, d_from_prev, d_to_prev = _works_bounds(season_year)
    logger.info(
        "works_summary: period current=%s..%s prev=%s..%s (env WORKS_DATE_FROM/WORKS_DATE_TO как в works.py)",
        d_from,
        d_to,
        d_from_prev,
        d_to_prev,
    )

    session = requests.Session()
    session.headers.update({"X-User-Api-Token": token})

    logger.info("works_summary: loading reference data from cropwise")
    fields = _get_all(session, base_url, "fields")
    # Как desktop works (2).py — field_groups через from_id, не page (иначе справочник может отличаться от скрипта).
    fgs = _get_all(session, base_url, "field_groups")
    gfs = _get_all(session, base_url, "group_folders") or _get_all_page(session, base_url, "group_folders")
    field_firm = _build_field_firm(fields, fgs, gfs)

    crops_raw = _get_all(session, base_url, "crops")
    crop_map = {
        int(c["id"]): str(c.get("name") or c.get("title") or "")
        for c in crops_raw
        if c.get("id") is not None
    }
    field_crop_curr = _load_field_crop(session, base_url, season_year, crop_map)
    field_crop_prev = _load_field_crop(session, base_url, prev_year, crop_map)

    wt_to_group: dict[int, str] = {wt_id: name for name, ids in WORK_GROUPS for wt_id in ids}
    all_wt_ids: set[int] = {wt_id for _, ids in WORK_GROUPS for wt_id in ids}

    logger.info("works_summary: collect current season=%s", season_year)
    data_curr = _collect_for_year(
        session, base_url, token, season_year, d_from, d_to, field_crop_curr, field_firm, wt_to_group, all_wt_ids
    )
    logger.info("works_summary: collect previous season=%s", prev_year)
    data_prev = _collect_for_year(
        session, base_url, token, prev_year, d_from_prev, d_to_prev, field_crop_prev, field_firm, wt_to_group, all_wt_ids
    )

    rows: list[dict] = []
    groups_set: set[str] = set()
    crops_set: set[str] = set()
    firms_set: set[str] = set()

    for year, src in ((season_year, data_curr), (prev_year, data_prev)):
        for group, by_crop in src.items():
            groups_set.add(group)
            for crop, by_firm in by_crop.items():
                crops_set.add(crop)
                for firm, by_date in by_firm.items():
                    firms_set.add(firm)
                    for d, area in by_date.items():
                        rows.append(
                            {
                                "year": int(year),
                                "date": d.isoformat(),
                                "group": group,
                                "crop": crop,
                                "firm": firm,
                                "area": round(float(area), 2),
                            }
                        )

    rows.sort(key=lambda x: (int(x["year"]), str(x["date"]), str(x["group"]), str(x["crop"]), str(x["firm"])))
    logger.info(
        "works_summary: rows_ready=%s groups=%s crops=%s firms=%s",
        len(rows),
        len(groups_set),
        len(crops_set),
        len(firms_set),
    )
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "season_year": season_year,
        "prev_year": prev_year,
        "rows": rows,
        "groups": sorted(groups_set),
        "crops": sorted(crops_set),
        "firms": sorted(firms_set),
    }


def load_cache() -> dict | None:
    p = _cache_path()
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def generate_cache() -> dict:
    payload = _build_rows()
    _cache_path().write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


def run_generate_cache_blocking() -> dict:
    """Синхронная генерация кеша с теми же флагами running/error, что и фоновый поток (для очереди API)."""
    global _is_running, _last_error
    with _state_lock:
        if _is_running:
            raise RuntimeError("Сводка по работам уже формируется. Дождитесь завершения.")
        _is_running = True
        _last_error = ""
    try:
        return generate_cache()
    except Exception as exc:
        logger.exception("works_summary.generate_cache failed")
        with _state_lock:
            _last_error = str(exc)
        raise
    finally:
        with _state_lock:
            _is_running = False


def works_generation_status() -> dict:
    with _state_lock:
        return {"running": bool(_is_running), "error": str(_last_error or "")}


def generate_cache_async() -> None:
    """Фоновый запуск без блокировки HTTP (устаревший путь; предпочтительна очередь POST generate)."""
    with _state_lock:
        if _is_running:
            return

    def _worker() -> None:
        try:
            run_generate_cache_blocking()
        except RuntimeError:
            pass

    Thread(target=_worker, daemon=True, name="works-summary-generator").start()


def _parse_csv(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {part.strip() for part in str(raw).split(",") if part.strip()}


def filter_rows(
    payload: dict,
    *,
    years: list[int] | None = None,
    groups: str | None = None,
    crops: str | None = None,
    firms: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict]:
    rows = list(payload.get("rows") or [])
    years_set = {int(y) for y in (years or []) if int(y) > 0}
    groups_set = _parse_csv(groups)
    crops_set = _parse_csv(crops)
    firms_set = _parse_csv(firms)
    out: list[dict] = []
    for row in rows:
        row_year = int(row.get("year") or 0)
        if years_set and row_year not in years_set:
            continue
        if groups_set and str(row.get("group") or "") not in groups_set:
            continue
        if crops_set and str(row.get("crop") or "") not in crops_set:
            continue
        if firms_set and str(row.get("firm") or "") not in firms_set:
            continue
        row_date_raw = str(row.get("date") or "")
        if date_from or date_to:
            try:
                row_date = date.fromisoformat(row_date_raw)
            except ValueError:
                continue
            if date_from and row_date < date_from:
                continue
            if date_to and row_date > date_to:
                continue
        out.append(row)
    out.sort(key=lambda x: (str(x.get("date") or ""), str(x.get("group") or ""), str(x.get("crop") or ""), str(x.get("firm") or "")))
    return out


def _works_date_range(d0: date, d1: date) -> list[date]:
    out: list[date] = []
    d = d0
    while d <= d1:
        out.append(d)
        d += timedelta(days=1)
    return out


def _works_ru_day_month(d: date) -> str:
    mon = (
        "",
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    )
    return f"{d.day} {mon[d.month]}"


def aggregate_works_for_excel_layout(
    rows: list[dict],
    season_year: int,
    prev_year: int,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> tuple[
    list[date],
    list[date],
    list[tuple[str, str]],
    dict[tuple[str, str], dict[str, dict[date, float]]],
    dict[tuple[str, str], dict[str, dict[date, float]]],
    date,
    date,
]:
    """Та же геометрия данных, что и в desktop works.py / build_works_excel: даты колонок, пары (группа, культура), матрицы по фирмам."""
    wf, wt, _wfp, _wtp = _works_bounds(season_year)
    eff_from, eff_to = wf, wt
    if date_from is not None:
        eff_from = max(eff_from, date_from)
    if date_to is not None:
        eff_to = min(eff_to, date_to)
    if eff_to < eff_from:
        eff_to = eff_from

    eff_from_prev = eff_from.replace(year=eff_from.year - 1)
    eff_to_prev = eff_to.replace(year=eff_to.year - 1)

    dates_curr = _works_date_range(eff_from, eff_to)
    dates_prev = _works_date_range(eff_from_prev, eff_to_prev)
    if len(dates_curr) != len(dates_prev):
        nmin = min(len(dates_curr), len(dates_prev))
        dates_curr = dates_curr[:nmin]
        dates_prev = dates_prev[:nmin]

    curr: dict[tuple[str, str], dict[str, dict[date, float]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    prev: dict[tuple[str, str], dict[str, dict[date, float]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))

    for r in rows:
        y = int(r.get("year") or 0)
        try:
            d = date.fromisoformat(str(r.get("date") or ""))
        except ValueError:
            continue
        g = str(r.get("group") or "").strip()
        c = str(r.get("crop") or "").strip()
        f = str(r.get("firm") or "").strip()
        if not g or not c or not f:
            continue
        a = float(r.get("area") or 0)
        key = (g, c)
        if y == season_year:
            curr[key][f][d] += a
        elif y == prev_year:
            prev[key][f][d] += a

    pairs: set[tuple[str, str]] = set(curr.keys()) | set(prev.keys())
    if not pairs:
        raise ValueError("Для выбранных фильтров нет данных для Excel.")

    group_order = {name: i for i, (name, _) in enumerate(WORK_GROUPS)}
    sorted_pairs = sorted(pairs, key=lambda gc: (group_order.get(gc[0], 999), gc[1]))
    return dates_curr, dates_prev, sorted_pairs, curr, prev, eff_from, eff_to


def build_works_grid_view(
    rows: list[dict],
    season_year: int,
    prev_year: int,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict:
    """JSON-сетка как в Excel: листы → блоки года → строки FIRM_ORDER × колонки дней."""
    dates_curr, dates_prev, sorted_pairs, curr, prev, eff_from, eff_to = aggregate_works_for_excel_layout(
        rows, season_year, prev_year, date_from=date_from, date_to=date_to
    )
    sheets_out: list[dict] = []
    for group, crop in sorted_pairs:
        sheet_title = f"{group} {crop}"[:31]
        blocks: list[dict] = []

        for dates, header_year, src in (
            (dates_curr, season_year, curr),
            (dates_prev, prev_year, prev),
        ):
            by_all = src.get((group, crop), {})
            firms_out: list[dict] = []
            grand_total = 0.0
            for firm in FIRM_ORDER:
                fd = dict(by_all.get(firm, {}))
                daily = [round(float(fd.get(dd, 0.0)), 1) for dd in dates]
                total = round(sum(fd.get(dd, 0.0) for dd in dates), 1)
                grand_total += total
                firms_out.append({"firm": firm, "daily": daily, "total": total})
            grand_total = round(grand_total, 1)
            blocks.append(
                {
                    "header_year": header_year,
                    "is_current_season": header_year == season_year,
                    "title": f"{group.upper()} | {crop} | сезон {header_year} год",
                    "date_headers": [{"date": d.isoformat(), "label": _works_ru_day_month(d)} for d in dates],
                    "firms": firms_out,
                    "grand_total": grand_total,
                }
            )

        sheets_out.append(
            {
                "group": group,
                "crop": crop,
                "sheet_title": sheet_title,
                "blocks": blocks,
            }
        )

    return {
        "season_year": season_year,
        "prev_year": prev_year,
        "effective_date_from": eff_from.isoformat(),
        "effective_date_to": eff_to.isoformat(),
        "sheet_count": len(sheets_out),
        "sheets": sheets_out,
    }


def build_works_excel(
    rows: list[dict],
    season_year: int,
    prev_year: int,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> tuple[Path, str]:
    """Excel как desktop works (2).py: лист на каждую пару (вид работ, культура), колонки — дни периода, два блока по годам."""
    if not rows:
        raise ValueError("Для выбранных фильтров нет данных для Excel.")

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    _C_TITLE_CURR = "FFFF00"
    _C_TITLE_PREV = "E2EFDA"
    _C_HEADER = "FF0000"
    _C_TOTAL = "BDD7EE"
    _C_SUMMA = "D9E1F2"
    _C_SEP = "F2F2F2"  # как в Загрузки/works.py — разделитель между блоками годов

    def _b() -> Border:
        s = Side(style="thin", color="000000")
        return Border(left=s, right=s, top=s, bottom=s)

    def _cell(
        ws,
        row: int,
        col: int,
        val=None,
        *,
        bold: bool = False,
        bg: str | None = None,
        fg: str = "000000",
        ha: str = "center",
        sz: int = 9,
        italic: bool = False,
    ) -> None:
        c = ws.cell(row=row, column=col, value=val)
        c.font = Font(name="Calibri", size=sz, bold=bold, color=fg, italic=italic)
        c.alignment = Alignment(horizontal=ha, vertical="center", wrap_text=False)
        c.border = _b()
        if bg:
            c.fill = PatternFill(fill_type="solid", fgColor=bg)

    def _fill_row(ws, row: int, cols: range, bg: str) -> None:
        for ci in cols:
            x = ws.cell(row=row, column=ci)
            x.fill = PatternFill(fill_type="solid", fgColor=bg)
            x.border = _b()

    def _write_block(
        ws,
        start_row: int,
        group: str,
        crop: str,
        dates: list[date],
        by_firm: dict[str, dict[date, float]],
        header_year: int,
        n_cols: int,
    ) -> int:
        row = start_row
        title_bg = _C_TITLE_CURR if header_year == season_year else _C_TITLE_PREV

        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=n_cols)
        _cell(ws, row, 1, f"{group.upper()} | {crop} | сезон {header_year} год", bold=True, bg=title_bg, ha="left", sz=12)
        _fill_row(ws, row, range(2, n_cols + 1), title_bg)
        ws.row_dimensions[row].height = 22
        row += 1

        for ci in range(1, n_cols + 1):
            ws.cell(row=row, column=ci).border = _b()
        ws.row_dimensions[row].height = 6
        row += 1

        _cell(ws, row, 1, "Агрофирма", bold=True, italic=True, bg=_C_HEADER, fg="FFFFFF", ha="center", sz=10)
        _cell(ws, row, 2, "Культура", bold=True, italic=True, bg=_C_HEADER, fg="FFFFFF", ha="center", sz=10)
        _cell(ws, row, 3, "Вид работ", bold=True, italic=True, bg=_C_HEADER, fg="FFFFFF", ha="center", sz=10)
        for di, dd in enumerate(dates, start=4):
            _cell(ws, row, di, _works_ru_day_month(dd), bold=True, italic=True, bg=_C_HEADER, fg="FFFFFF", ha="center", sz=10)
        _cell(ws, row, n_cols, "ИТОГО", bold=True, italic=True, bg=_C_HEADER, fg="FFFFFF", ha="center", sz=10)
        ws.row_dimensions[row].height = 22
        row += 1

        grand_total = 0.0

        # Как Загрузки/works.py: всегда 7 строк по FIRM_ORDER (нет строк для фирм вне списка).
        for firm in FIRM_ORDER:
            fd = by_firm.get(firm, {})
            firm_total = round(sum(fd.values()), 1)
            grand_total += firm_total

            _cell(ws, row, 1, firm, ha="left", sz=9)
            _cell(ws, row, 2, crop, ha="left", sz=9)
            _cell(ws, row, 3, group, ha="left", sz=9)
            for di, dd in enumerate(dates):
                val = round(fd.get(dd, 0.0), 1)
                _cell(ws, row, 4 + di, val, ha="center", sz=9)
            _cell(ws, row, n_cols, firm_total, bold=True, ha="center", sz=9, bg=_C_TOTAL)
            ws.row_dimensions[row].height = 16
            row += 1

        for ci in range(1, n_cols + 1):
            ws.cell(row=row, column=ci).border = _b()
        ws.row_dimensions[row].height = 6
        row += 1

        for ci in range(1, n_cols + 1):
            _cell(ws, row, ci, None, bg=_C_SUMMA)
        _cell(ws, row, n_cols - 1, "СУММА", bold=True, bg=_C_SUMMA, ha="right", sz=9)
        _cell(ws, row, n_cols, round(grand_total, 1), bold=True, bg=_C_SUMMA, ha="center", sz=9)
        ws.row_dimensions[row].height = 16
        row += 1

        return row

    dates_curr, dates_prev, sorted_pairs, curr, prev, _eff_from, eff_to = aggregate_works_for_excel_layout(
        rows, season_year, prev_year, date_from=date_from, date_to=date_to
    )

    wb = Workbook()
    default_ws = wb.active
    wb.remove(default_ws)

    n = len(dates_curr)
    n_cols = 3 + n + 1

    for group, crop in sorted_pairs:
        sheet_title = f"{group} {crop}"[:31]
        base = sheet_title
        suf = 2
        while sheet_title in wb.sheetnames:
            sheet_title = f"{base[:28]}_{suf}"
            suf += 1

        ws = wb.create_sheet(title=sheet_title)
        ws.column_dimensions["A"].width = 26
        ws.column_dimensions["B"].width = 18
        ws.column_dimensions["C"].width = 24
        for ci in range(4, n_cols):
            ws.column_dimensions[get_column_letter(ci)].width = 10
        ws.column_dimensions[get_column_letter(n_cols)].width = 10

        by_curr = {firm: dict(dd) for firm, dd in curr.get((group, crop), {}).items()}
        by_prev = {firm: dict(dd) for firm, dd in prev.get((group, crop), {}).items()}

        row = 1
        row = _write_block(ws, row, group, crop, dates_curr, by_curr, season_year, n_cols)

        for ci in range(1, n_cols + 1):
            c = ws.cell(row=row, column=ci)
            c.fill = PatternFill(fill_type="solid", fgColor=_C_SEP)
            c.border = _b()
        ws.row_dimensions[row].height = 8
        row += 1

        _write_block(ws, row, group, crop, dates_prev, by_prev, prev_year, n_cols)
        ws.freeze_panes = "D4"

    label = eff_to.strftime("%d_%m_%Y")
    stem = f"works_{label}"
    # Уникальное имя файла на диске — параллельные выгрузки не перезаписывают друг друга.
    out_path = _excel_dir() / f"{stem}_{uuid.uuid4().hex}.xlsx"
    wb.save(out_path)
    return out_path, stem


def _cleanup_old_script_runs(root: Path, max_age_sec: int = 24 * 3600) -> None:
    """Удаляет папки run_* старше суток (результаты запусков works.py)."""
    import shutil
    import time

    now = time.time()
    for d in root.glob("run_*"):
        try:
            if d.is_dir() and now - d.stat().st_mtime > max_age_sec:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            continue


def _works_script_candidates() -> list[Path]:
    """Где искать works.py (по приоритету).

    1) env WORKS_EXCEL_SCRIPT_PATH — явный путь;
    2) <svodki_root>/plan/works.py — штатное место (входит в образ);
    3) <svodki_root>/new/works.py — прежнее расположение.
    """
    root = Path(settings.svodki_root).expanduser()
    if not root.is_absolute():
        root = (Path.cwd() / root).resolve()
    explicit = str(os.getenv("WORKS_EXCEL_SCRIPT_PATH", "")).strip()
    candidates = [Path(explicit).expanduser()] if explicit else []
    candidates += [root / "plan" / "works.py", root / "new" / "works.py"]
    return candidates


def build_works_excel_via_script(
    *,
    groups: str = "",
    crops: str = "",
    date_from: date | None = None,
    date_to: date | None = None,
) -> tuple[Path, str]:
    """Excel «Сводка работ» через скрипт works.py (запасной путь, если нет кэша).

    Скрипт запускается в отдельном процессе; каталог со скриптом может быть
    read-only (в Docker svodki смонтирован :ro), поэтому результат пишется
    в отдельную временную папку под uploads/works-excel/.
    """
    candidates = _works_script_candidates()
    script_path = next((p for p in candidates if p.is_file()), None)
    if script_path is None:
        raise ValueError(
            "Не найден скрипт works.py для выгрузки Excel. Искали: "
            + "; ".join(str(p) for p in candidates)
            + ". Положите works.py в svodki/plan/ или задайте WORKS_EXCEL_SCRIPT_PATH."
        )
    script_path = script_path.resolve()
    if not (get_cropwise_token() or "").strip():
        raise ValueError(MISSING_CROPWISE_TOKEN_DETAIL)

    _cleanup_old_script_runs(_excel_dir())
    out_dir = _excel_dir() / f"run_{uuid.uuid4().hex}"
    out_dir.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    # Скрипту нужны plan/ и utils/* в PYTHONPATH (plans.py, _apply_plans_edits, …).
    plan_dir = script_path.parent
    py_path = [str(plan_dir)]
    utils_root = plan_dir / "utils"
    if utils_root.is_dir():
        py_path += [str(p) for p in sorted(utils_root.iterdir()) if p.is_dir() and p.name != "__pycache__"]
    if env.get("PYTHONPATH"):
        py_path.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(py_path)
    env["PYTHONIOENCODING"] = "utf-8"
    env["WORKS_OUTPUT_DIR"] = str(out_dir)
    token = (get_cropwise_token() or "").strip()
    if token:
        env["CROPWISE_API_TOKEN"] = token
    # Правки «Редактора планов» должны быть видны и скрипту (WORK_GROUPS и т.д.).
    try:
        from app.legacy.plans_editor import inject_plans_edits_env

        inject_plans_edits_env(env, out_dir)
    except Exception:
        logger.warning("works script: не удалось передать правки планов", exc_info=True)
    for key in ("DATE_FROM", "DATE_TO", "FILTER_GROUPS", "FILTER_CROPS"):
        env.pop(key, None)  # не наследуем случайные значения из окружения сервера
    if date_from:
        env["DATE_FROM"] = date_from.isoformat()
    if date_to:
        env["DATE_TO"] = date_to.isoformat()
    if groups:
        env["FILTER_GROUPS"] = str(groups)
    if crops:
        env["FILTER_CROPS"] = str(crops)

    try:
        proc = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(out_dir),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3600,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Скрипт works.py не уложился в отведённое время.") from exc
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout or "").splitlines()[-20:])
        raise ValueError(f"Ошибка скрипта works: {tail or 'unknown error'}")

    result_files = sorted(out_dir.glob("works_*.xlsx"), key=lambda p: p.stat().st_mtime)
    if not result_files:
        raise ValueError("Скрипт works завершился, но Excel-файл не найден.")
    latest = result_files[-1]
    return latest, latest.stem
