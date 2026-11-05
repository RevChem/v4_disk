"""Cropwise — Отчёт по видам работ
===================================
Один лист = один вид работ + культура
Строки    = агрофирмы
Колонки   = дни выбранного диапазона + ИТОГО

Запуск (параметры — только через переменные окружения, как их передаёт бэкенд):
    DATE_FROM=2026-04-14 DATE_TO=2026-04-28 \
    FILTER_GROUPS="4.0 Посев" FILTER_CROPS="Пар,Соя" \
    WORKS_OUTPUT_DIR=/tmp/out python works.py

DATE_FROM / DATE_TO   — ГГГГ-ММ-ДД (по умолчанию: 1 января сезона .. сегодня);
FILTER_GROUPS         — группы работ через запятую (пусто = все);
FILTER_CROPS          — культуры через запятую (пусто = все);
WORKS_SEASON_YEAR     — год сезона (по умолчанию год DATE_TO / текущий);
WORKS_OUTPUT_DIR      — куда писать works_*.xlsx (по умолчанию текущая папка);
CROPWISE_API_TOKEN / CROPWISE_BASE_URL — переопределяют значения из plans.py.
"""
from __future__ import annotations


import asyncio
import os
import sys
import requests
import httpx
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import plans  # noqa: F401  # bootstrap utils/* (plan_paths via plans.py)
from report_fetch_orchestration import report_async_client, run_report_async
from cropwise_paging import CROPWISE_PAGE_LIMIT, from_id_should_continue
import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from plans import (
    API_TOKEN,
    BASE_URL,
    TZ_MSK,
    FIRM_TO_FOLDER,
    FOLDER_TO_FIRM,
    FIRM_ORDER,
    FIELD_NAME_FORCE_RULES,
    SKIP_CROPS,
    WORK_GROUPS,
)

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

SEMAPHORE_LIMIT = 40
DAY_START_HOUR  = 6


def _env_date(name: str) -> date | None:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.split("T")[0])
    except ValueError:
        raise SystemExit(f"{name}: ожидается дата ГГГГ-ММ-ДД, получено {raw!r}")


def _env_list(name: str, known: list[str] | None = None) -> list[str] | None:
    """Список из переменной окружения через запятую; пусто -> None (= всё).

    Названия групп могут содержать запятую («5.4 Уборка картофеля, лука»):
    соседние части склеиваются, если вместе дают известное название.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if known:
        known_set = set(known)
        merged: list[str] = []
        i = 0
        while i < len(parts):
            hit = None
            for j in range(len(parts), i, -1):
                cand = ", ".join(parts[i:j])
                if cand in known_set:
                    hit = (cand, j)
                    break
            if hit:
                merged.append(hit[0])
                i = hit[1]
            else:
                merged.append(parts[i])
                i += 1
        parts = merged
    return parts or None


# ── Диапазон дат ─────────────────────────────────────────────────────────────
DATE_FROM: date | None = _env_date("DATE_FROM")
DATE_TO:   date | None = _env_date("DATE_TO")

_season_raw = (os.environ.get("WORKS_SEASON_YEAR") or "").strip()
SEASON_YEAR = int(_season_raw) if _season_raw.isdigit() else (DATE_TO or date.today()).year
PREV_YEAR   = SEASON_YEAR - 1

# ── Фильтры (None = брать всё; список = только указанные) ────────────────────
# Названия групп — как в plans.WORK_GROUPS ("1.1 Внесение минеральных удобрений",
# "2.1 Боронование", "4.0 Посев", "5.4 Уборка картофеля, лука", ...).
# Культуры — любые строки как в справочнике Cropwise.
FILTER_GROUPS: list[str] | None = _env_list("FILTER_GROUPS", [name for name, _ in WORK_GROUPS])
FILTER_CROPS:  list[str] | None = _env_list("FILTER_CROPS")

_TOKEN = (os.environ.get("CROPWISE_API_TOKEN") or "").strip() or API_TOKEN
_BASE_URL = (os.environ.get("CROPWISE_BASE_URL") or "").strip() or BASE_URL


_active_groups = (
    {g for g in FILTER_GROUPS if any(g == name for name, _ in WORK_GROUPS)}
    if FILTER_GROUPS is not None
    else {name for name, _ in WORK_GROUPS}
)
ALL_WT_IDS: set[int] = {
    wt_id for name, ids in WORK_GROUPS
    if name in _active_groups
    for wt_id in ids
}

WT_TO_GROUPS: dict[int, list[str]] = {}
for _name, _ids in WORK_GROUPS:
    for _wt_id in _ids:
        _bucket = WT_TO_GROUPS.setdefault(int(_wt_id), [])
        if _name not in _bucket:
            _bucket.append(_name)

WT_TO_GROUP: dict[int, str] = {
    wt_id: names[0] for wt_id, names in WT_TO_GROUPS.items() if names
}

session = requests.Session()
session.headers.update({"X-User-Api-Token": _TOKEN})


def get_all(endpoint: str, params: dict | None = None) -> list[dict]:
    p = dict(params or {})
    results, from_id = [], 0
    for _ in range(500):
        r = session.get(f"{_BASE_URL}/{endpoint}",
                        params={**p, "from_id": from_id, "limit": CROPWISE_PAGE_LIMIT},
                        timeout=120)
        r.raise_for_status()
        body  = r.json()
        chunk = body.get("data", [])
        if not chunk:
            break
        results.extend(chunk)
        meta    = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        if not from_id_should_continue(meta, chunk):
            break
        from_id = last_id + 1
    return results

# =============================================================================
# РАЗБОР ДАТ / СМЕНА
# =============================================================================

def parse_dt(s) -> datetime | None:
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


def get_shift(end_time_raw, d_from: date, d_to: date) -> tuple[date, datetime] | None:
    """Возвращает (дата_смены, cutoff) или None если вне периода."""
    end_dt = parse_dt(end_time_raw)
    if end_dt is None:
        return None
    cal    = end_dt.date()
    anchor = datetime(cal.year, cal.month, cal.day,
                      DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK)
    if end_dt < anchor:
        cal    = cal - timedelta(days=1)
        cutoff = anchor
    else:
        cutoff = anchor + timedelta(days=1)
    if not (d_from <= cal <= d_to):
        return None
    return cal, cutoff

# =============================================================================
# ИЕРАРХИЯ ПАПОК → АГРОФИРМА
# =============================================================================

def _norm(s: str) -> str:
    return (s or "").strip().casefold().replace(" ", "").replace("\u00a0", "")


def _parent_map(gfs: list[dict]) -> dict[int, int | None]:
    out: dict[int, int | None] = {}
    for gf in gfs:
        fid = gf.get("id")
        if fid is None:
            continue
        pid = (gf.get("parent_id")
               or gf.get("parent_group_folder_id")
               or gf.get("group_folder_parent_id"))
        out[int(fid)] = int(pid) if pid is not None else None
    return out


def _chain(folder_id: int | None, pm: dict[int, int | None]) -> list[int]:
    if folder_id is None:
        return []
    out, cur, seen = [], int(folder_id), set()
    for _ in range(64):
        if cur in seen:
            break
        seen.add(cur); out.append(cur)
        nxt = pm.get(cur)
        if nxt is None:
            break
        cur = int(nxt)
    return out


def _to_root(folder_id: int | None,
             pm: dict[int, int | None],
             roots: set[int]) -> int | None:
    if folder_id is None:
        return None
    cur, seen = int(folder_id), set()
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


def build_field_firm(fields: list[dict],
                     fgs: list[dict],
                     gfs: list[dict]) -> dict[int, str]:
    fgs_map = {fg["id"]: fg for fg in fgs}
    pm      = _parent_map(gfs)
    roots   = set(FIRM_TO_FOLDER.values())

    hints: dict[int, str] = {}
    for gf in gfs:
        fid = gf.get("id")
        if fid is None:
            continue
        blob = _norm(f"{gf.get('name') or ''} {gf.get('title') or ''}")
        if any(x in blob for x in ("лениногор", "птицефабрик", "лпф", "lpf")):
            hints[int(fid)] = "Лениногорская Птицефабрика"

    result: dict[int, str] = {}
    for f in fields:
        fid = f.get("id")
        if fid is None:
            continue
        fgid = f.get("field_group_id")
        rf   = f.get("group_folder_id")
        if rf is None and fgid is not None:
            rf = fgs_map.get(fgid, {}).get("group_folder_id")
        rint = int(rf) if rf is not None else None

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

    for f in fields:
        fid = f.get("id")
        if fid is None:
            continue
        blob = _norm(f"{f.get('name') or ''} {f.get('title') or ''}")
        for needle, firm_key in FIELD_NAME_FORCE_RULES:
            if needle in blob:
                result[int(fid)] = firm_key
                break

    print(f"  Полей с привязкой к фирме: {len(result)}")
    return result

# =============================================================================
# ASYNC MTFMI
# =============================================================================

async def _pages(client: httpx.AsyncClient, url: str,
                 params: dict, sem: asyncio.Semaphore) -> list[dict]:
    items, from_id = [], 0
    while True:
        async with sem:
            r = await client.get(url, params={**params, "from_id": from_id, "limit": CROPWISE_PAGE_LIMIT})
        body  = r.json()
        chunk = body.get("data", [])
        if not chunk:
            break
        items.extend(chunk)
        meta = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        if not from_id_should_continue(meta, chunk):
            break
        from_id = last_id + 1
    return items


async def _mtfmi_async(task_ids: list[int]) -> dict[int, list[dict]]:
    from mtfmi_batch_fetch import fetch_mtfmi_map_for_tasks

    if not task_ids:
        return {}
    async with report_async_client(_TOKEN, semaphore_limit=SEMAPHORE_LIMIT, timeout=120) as (client, sem):
        return await fetch_mtfmi_map_for_tasks(
            client, sem, _BASE_URL, task_ids, log_prefix="[works] ",
        )


def fetch_mtfmi(task_ids: list[int]) -> dict[int, list[dict]]:
    return run_report_async(_mtfmi_async(task_ids))

# =============================================================================
# СБОР ДАННЫХ ЗА СЕЗОН
# Возвращает: {group_name: {crop: {firm: {date: ha}}}}
# =============================================================================

def collect(
    field_crop: dict[int, str],
    field_firm: dict[int, str],
    d_from: date,
    d_to: date,
    season_year: int,
    filter_groups: list[str] | None,
    filter_crops: list[str] | None,
    all_wt_ids: set[int],
) -> dict[str, dict[str, dict[str, dict[date, float]]]]:
    """
    Загружает machine_tasks по всем work_type_id из WORK_GROUPS за заданный
    сезон и диапазон дат.

    Возвращает структуру:
      group_name -> crop -> firm -> date -> ha
    """
    season_start = datetime(season_year, 1, 1, DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK)
    cutoff_end   = datetime(d_to.year, d_to.month, d_to.day,
                            DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK) + timedelta(days=1)

    from svodki_prefetch_io import sync_machine_tasks_prefetch_or_batch

    _wt_label = f"{len(all_wt_ids)} id" + (f" ({', '.join(filter_groups)})" if filter_groups else "")
    print(f"  [{season_year}] Загружаем machine_tasks по {_wt_label}...")
    mt_params = {
        "end_time_gt_eq": season_start.strftime("%Y-%m-%dT%H:%M:%S"),
        "end_time_lt": cutoff_end.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    all_tasks = sync_machine_tasks_prefetch_or_batch(
        get_all, all_wt_ids, mt_params, log_prefix=f"[works-{season_year}] ",
    )

    all_tasks = [t for t in all_tasks
                 if str(t.get("status") or "").strip().lower() != "canceled"]
    print(f"    [{season_year}] Всего заданий: {len(all_tasks)}")

    # Диагностика: показываем диапазон end_time реальных задач
    if all_tasks:
        sample_times = [parse_dt(t.get("end_time")) for t in all_tasks[:5]]
        print(f"    [{season_year}] Примеры end_time: {[str(dt) if dt else None for dt in sample_times]}")
        print(f"    [{season_year}] Ищем смены в диапазоне {d_from} — {d_to}")

    task_shift: dict[int, tuple[date, datetime]] = {}
    for t in all_tasks:
        info = get_shift(t.get("end_time"), d_from, d_to)
        if info:
            task_shift[t["id"]] = info

    in_period = [t for t in all_tasks if t["id"] in task_shift]
    print(f"    [{season_year}] В периоде {d_from}–{d_to}: {len(in_period)} заданий")
    if not in_period:
        return {}

    print(f"  [{season_year}] Загружаем mtfmi (асинхронно)...")
    mtfmi = fetch_mtfmi([t["id"] for t in in_period])

    # group -> crop -> firm -> date -> ha
    data: dict[str, dict[str, dict[str, dict[date, float]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    )
    from mtfmi_batch_fetch import UNDEFINED_CROP, crop_of_item

    for task in in_period:
        tid            = task["id"]
        t_date, cutoff = task_shift[tid]
        wt_id          = task.get("work_type_id")
        try:
            groups = WT_TO_GROUPS.get(int(wt_id), [])
        except (TypeError, ValueError):
            groups = []
        if not groups:
            continue
        try:
            official = float(task.get("covered_area") or 0)
        except (TypeError, ValueError):
            official = 0.0
        if official <= 0:
            continue
        buckets: dict[tuple[str, str], float] = defaultdict(float)
        for item in mtfmi.get(tid, []):
            fid = item.get("field_id")
            if not fid:
                continue
            firm = field_firm.get(int(fid))
            if not firm:
                continue
            crop = crop_of_item(item, "") or UNDEFINED_CROP
            if crop in SKIP_CROPS:
                continue
            if filter_crops is not None and crop not in filter_crops:
                continue
            hourly = item.get("covered_area_hourly") or []
            if hourly:
                cov = sum(
                    float(e[1] or 0) for e in hourly
                    if parse_dt(e[0]) and parse_dt(e[0]) < cutoff
                )
            else:
                cov = float(item.get("covered_area") or 0)
            if cov <= 0:
                continue
            buckets[(firm, crop)] += cov
        if not buckets:
            continue
        scale = official / sum(buckets.values())
        for (firm, crop), raw in buckets.items():
            area = raw * scale
            if area <= 0:
                continue
            for group in groups:
                data[group][crop][firm][t_date] += area

    return data

# =============================================================================
# СТИЛИ
# =============================================================================

_C_TITLE_26 = "FFFF00"   # жёлтый — заголовок 2026
_C_TITLE_25 = "E2EFDA"   # светло-зелёный — заголовок 2025
_C_HEADER   = "FF0000"   # красный — шапка колонок
_C_TOTAL    = "BDD7EE"   # синий — итого по строке
_C_SUMMA    = "D9E1F2"   # строка СУММА
_C_SEP      = "F2F2F2"   # серый — разделитель между блоками


def _b() -> Border:
    s = Side(style="thin", color="000000")
    return Border(left=s, right=s, top=s, bottom=s)


def _c(ws, row: int, col: int, val=None, *,
       bold=False, bg: str | None = None, fg="000000",
       ha="center", wrap=False, sz=9, italic=False) -> None:
    cell = ws.cell(row=row, column=col, value=val)
    cell.font      = Font(name="Calibri", size=sz, bold=bold,
                          color=fg, italic=italic)
    cell.alignment = Alignment(horizontal=ha, vertical="center", wrap_text=wrap)
    cell.border    = _b()
    if bg:
        cell.fill = PatternFill("solid", fgColor=bg)


def _fill(ws, row: int, cols: range, bg: str) -> None:
    for ci in cols:
        c = ws.cell(row=row, column=ci)
        c.fill   = PatternFill("solid", fgColor=bg)
        c.border = _b()


def _ru_month(d: date) -> str:
    MON = ("", "января", "февраля", "марта", "апреля", "мая", "июня",
           "июля", "августа", "сентября", "октября", "ноября", "декабря")
    return f"{d.day} {MON[d.month]}"

# =============================================================================
# ЗАПИСЬ ОДНОГО БЛОКА (один год) В ЛИСТ
# =============================================================================

def _write_block(
    ws,
    start_row: int,
    group: str,
    crop: str,
    dates: list[date],
    by_firm: dict[str, dict[date, float]],
    season_year: int,
    n_cols: int,
) -> int:
    """
    Записывает блок (заголовок + шапка + строки + СУММА) начиная с start_row.
    Возвращает номер следующей свободной строки.
    """
    n = len(dates)
    row = start_row
    title_bg = _C_TITLE_26 if season_year == SEASON_YEAR else _C_TITLE_25

    # ── Заголовок блока ──────────────────────────────────────────────────────
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=n_cols)
    _c(ws, row, 1,
       f"{group.upper()} | {crop} | сезон {season_year} год",
       bold=True, bg=title_bg, ha="left", sz=12)
    _fill(ws, row, range(2, n_cols + 1), title_bg)
    ws.row_dimensions[row].height = 22
    row += 1

    # Пустая строка
    for ci in range(1, n_cols + 1):
        ws.cell(row=row, column=ci).border = _b()
    ws.row_dimensions[row].height = 6
    row += 1

    # ── Шапка (красный фон, белый курсив) ───────────────────────────────────
    _c(ws, row, 1, "Агрофирма", bold=True, italic=True, bg=_C_HEADER, fg="FFFFFF", ha="center", sz=10)
    _c(ws, row, 2, "Культура",  bold=True, italic=True, bg=_C_HEADER, fg="FFFFFF", ha="center", sz=10)
    _c(ws, row, 3, "Вид работ", bold=True, italic=True, bg=_C_HEADER, fg="FFFFFF", ha="center", sz=10)
    for di, d in enumerate(dates, start=4):
        _c(ws, row, di, _ru_month(d), bold=True, italic=True,
           bg=_C_HEADER, fg="FFFFFF", ha="center", sz=10)
    _c(ws, row, n_cols, "ИТОГО", bold=True, italic=True,
       bg=_C_HEADER, fg="FFFFFF", ha="center", sz=10)
    ws.row_dimensions[row].height = 22
    row += 1

    # ── Данные ───────────────────────────────────────────────────────────────
    col_totals  = [0.0] * n
    grand_total = 0.0

    for firm in FIRM_ORDER:
        fd         = by_firm.get(firm, {})
        firm_total = round(sum(fd.values()), 1)
        grand_total += firm_total

        _c(ws, row, 1, firm,  ha="left", sz=9)
        _c(ws, row, 2, crop,  ha="left", sz=9)
        _c(ws, row, 3, group, ha="left", sz=9)
        for di, d in enumerate(dates):
            val = round(fd.get(d, 0.0), 1)
            _c(ws, row, 4 + di, val, ha="center", sz=9)
            col_totals[di] += val
        _c(ws, row, n_cols, firm_total, bold=True, ha="center", sz=9, bg=_C_TOTAL)
        ws.row_dimensions[row].height = 16
        row += 1

    # Пустая строка
    for ci in range(1, n_cols + 1):
        ws.cell(row=row, column=ci).border = _b()
    ws.row_dimensions[row].height = 6
    row += 1

    # ── СУММА ────────────────────────────────────────────────────────────────
    for ci in range(1, n_cols + 1):
        _c(ws, row, ci, None, bg=_C_SUMMA)
    _c(ws, row, n_cols - 1, "СУММА", bold=True, bg=_C_SUMMA, ha="right", sz=9)
    _c(ws, row, n_cols, round(grand_total, 1), bold=True, bg=_C_SUMMA, ha="center", sz=9)
    ws.row_dimensions[row].height = 16
    row += 1

    return row

# =============================================================================
# ЗАПИСЬ ЛИСТА (группа работ + культура): 2026 + разделитель + 2025
# =============================================================================

def write_sheet(
    wb,
    group: str,
    crop: str,
    dates: list[date],
    dates_25: list[date],
    by_firm_26: dict[str, dict[date, float]],
    by_firm_25: dict[str, dict[date, float]],
) -> None:
    # Название листа: обрезаем до 31 символа, при дубликате добавляем суффикс
    sheet_title = f"{group} {crop}"[:31]
    base   = sheet_title
    suffix = 2
    while sheet_title in wb.sheetnames:
        sheet_title = f"{base[:28]}_{suffix}"
        suffix += 1

    ws = wb.create_sheet(title=sheet_title)

    n = len(dates)
    # Колонки: Агрофирма | Культура | Вид работ | day1..dayN | ИТОГО
    n_cols = 3 + n + 1

    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 18
    ws.column_dimensions["C"].width = 24
    for ci in range(4, n_cols):
        ws.column_dimensions[get_column_letter(ci)].width = 10
    ws.column_dimensions[get_column_letter(n_cols)].width = 10

    row = 1

    # ── Блок 2026 ─────────────────────────────────────────────────────────────
    row = _write_block(ws, row, group, crop, dates, by_firm_26, SEASON_YEAR, n_cols)

    # ── Разделитель между годами ──────────────────────────────────────────────
    for ci in range(1, n_cols + 1):
        c = ws.cell(row=row, column=ci)
        c.fill   = PatternFill("solid", fgColor=_C_SEP)
        c.border = _b()
    ws.row_dimensions[row].height = 8
    row += 1

    # ── Блок 2025 — используем даты 2025 года, чтобы ключи совпали с данными ──
    _write_block(ws, row, group, crop, dates_25, by_firm_25, PREV_YEAR, n_cols)

    # Закрепить панели после шапки первого блока (строка 4 = первая шапка)
    ws.freeze_panes = "D4"

# =============================================================================
# ЗАГРУЗКА КУЛЬТУР ЗА СЕЗОН
# =============================================================================

def _field_crop_from_history(
    history_items: list[dict],
    crop_map: dict[int, str],
) -> dict[int, str]:
    field_crop: dict[int, str] = {}
    for hi in history_items:
        fid = hi.get("field_id")
        cid = hi.get("crop_id")
        if not (fid and cid and hi.get("active")):
            continue
        cname = crop_map.get(cid, "")
        if cname and cname not in SKIP_CROPS:
            field_crop[fid] = cname
    return field_crop


def load_field_crop(season_year: int, crop_map: dict[int, str]) -> dict[int, str]:
    his = get_all("history_items", {"year": str(season_year)})
    return _field_crop_from_history(his, crop_map)

# =============================================================================
# ГЛАВНЫЙ ПРОЦЕСС
# =============================================================================

def _date_range(d_from: date, d_to: date) -> list[date]:
    out, d = [], d_from
    while d <= d_to:
        out.append(d)
        d += timedelta(days=1)
    return out


def _range_label(d_from: date, d_to: date) -> str:
    return f"{d_from.strftime('%d.%m.%Y')} — {d_to.strftime('%d.%m.%Y')}"


def _same_period_prev_year(d_from: date, d_to: date) -> tuple[date, date]:
    """Тот же календарный диапазон, но на год раньше."""
    return (
        d_from.replace(year=d_from.year - 1),
        d_to.replace(year=d_to.year - 1),
    )


def _output_dir() -> str:
    out = (os.environ.get("WORKS_OUTPUT_DIR") or "").strip() or "."
    os.makedirs(out, exist_ok=True)
    return out


def build_report() -> None:
    d_from = DATE_FROM or date(SEASON_YEAR, 1, 1)
    d_to   = DATE_TO   or date.today()
    dates  = _date_range(d_from, d_to)

    d_from_25, d_to_25 = _same_period_prev_year(d_from, d_to)
    dates_25 = _date_range(d_from_25, d_to_25)

    # Вычисляем фильтры локально, чтобы исключить влияние глобальных переменных
    # из plans.py или других модулей, которые могут их переопределить
    filter_groups: list[str] | None = FILTER_GROUPS
    filter_crops:  list[str] | None = FILTER_CROPS

    active_groups = (
        {g for g in filter_groups if any(g == name for name, _ in WORK_GROUPS)}
        if filter_groups is not None
        else {name for name, _ in WORK_GROUPS}
    )
    all_wt_ids: set[int] = {
        wt_id for name, ids in WORK_GROUPS
        if name in active_groups
        for wt_id in ids
    }

    print(f"Отчёт по работам  |  {_range_label(d_from, d_to)}  ({len(dates)} дней)")
    print(f"Сравнение с 2025  |  {_range_label(d_from_25, d_to_25)}")
    print(f"Фильтр групп      |  {', '.join(filter_groups) if filter_groups else 'все'}")
    print(f"Фильтр культур    |  {', '.join(filter_crops)  if filter_crops  else 'все'}")
    print(f"Активных work_type_id: {len(all_wt_ids)} → {sorted(all_wt_ids)}")

    # ── Справочники (параллельно, как _fetch_reference_raw в сводке 2) ─────
    from svodki_pipeline import run_reference_jobs

    print("  Загружаем поля / группы / папки / культуры / history_items...")
    ref = run_reference_jobs(
        {
            "fields": lambda: get_all("fields"),
            "field_groups": lambda: get_all("field_groups"),
            "group_folders": lambda: get_all("group_folders"),
            "crops": lambda: get_all("crops"),
            f"history_{SEASON_YEAR}": lambda: get_all(
                "history_items", {"year": str(SEASON_YEAR)},
            ),
            f"history_{PREV_YEAR}": lambda: get_all(
                "history_items", {"year": str(PREV_YEAR)},
            ),
        },
        thread_prefix="works-ref",
    )
    fields = ref["fields"]
    fgs = ref["field_groups"]
    gfs = ref["group_folders"]
    print(f"    {len(fields)} полей, {len(fgs)} групп, {len(gfs)} папок")

    field_firm = build_field_firm(fields, fgs, gfs)

    crops_raw = ref["crops"]
    crop_map = {c["id"]: (c.get("name") or c.get("title", "")) for c in crops_raw}
    print(f"    {len(crop_map)} культур в справочнике")

    field_crop_26 = _field_crop_from_history(ref[f"history_{SEASON_YEAR}"], crop_map)
    print(f"    {len(field_crop_26)} полей с культурой ({SEASON_YEAR})")

    field_crop_25 = _field_crop_from_history(ref[f"history_{PREV_YEAR}"], crop_map)
    print(f"    {len(field_crop_25)} полей с культурой ({PREV_YEAR})")

    # ── Сбор данных ───────────────────────────────────────────────────────────
    print(f"\nСбор данных за {SEASON_YEAR}...")
    data_26 = collect(field_crop_26, field_firm, d_from,    d_to,    SEASON_YEAR,
                      filter_groups, filter_crops, all_wt_ids)

    print(f"\nСбор данных за {PREV_YEAR}...")
    data_25 = collect(field_crop_25, field_firm, d_from_25, d_to_25, PREV_YEAR,
                      filter_groups, filter_crops, all_wt_ids)

    # ── Определяем все пары (group, crop) из обоих лет ────────────────────────
    pairs: set[tuple[str, str]] = set()
    for group, crop_dict in data_26.items():
        for crop in crop_dict:
            pairs.add((group, crop))
    for group, crop_dict in data_25.items():
        for crop in crop_dict:
            pairs.add((group, crop))

    # Сортируем: по порядку WORK_GROUPS, внутри группы — по названию культуры
    group_order = {name: i for i, (name, _) in enumerate(WORK_GROUPS)}
    sorted_pairs = sorted(pairs, key=lambda gc: (group_order.get(gc[0], 999), gc[1]))
    print(f"\n  Пар (группа, культура) с данными: {len(sorted_pairs)}")

    # ── Excel ─────────────────────────────────────────────────────────────────
    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    if not sorted_pairs:
        # Нет данных — создаём информационный лист вместо краша
        ws = wb.create_sheet("Нет данных")
        ws.cell(row=1, column=1, value="Данные не найдены для указанных фильтров.")
        ws.cell(row=2, column=1, value=f"Группы: {', '.join(filter_groups) if filter_groups else 'все'}")
        ws.cell(row=3, column=1, value=f"Культуры: {', '.join(filter_crops) if filter_crops else 'все'}")
        ws.cell(row=4, column=1, value=f"Период: {_range_label(d_from, d_to)}")
        ws.column_dimensions["A"].width = 60
        label  = d_to.strftime("%d_%m_%Y")
        output = os.path.join(_output_dir(), f"works_{label}.xlsx")
        wb.save(output)
        print(f"\nДанные не найдены. Сохранён пустой отчёт: {output}")
        return

    for group, crop in sorted_pairs:
        print(f"  -> лист: {group} | {crop}")
        by_firm_26 = data_26.get(group, {}).get(crop, {})
        by_firm_25 = data_25.get(group, {}).get(crop, {})
        write_sheet(wb, group, crop, dates, dates_25, by_firm_26, by_firm_25)

    label  = d_to.strftime("%d_%m_%Y")
    output = os.path.join(_output_dir(), f"works_{label}.xlsx")
    wb.save(output)
    print(f"\nСохранено: {output}")


if __name__ == "__main__":
    build_report()