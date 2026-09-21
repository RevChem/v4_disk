"""
diag_today_diff.py
==================
Диагностика расхождения «Факт за сутки» между 5_main и 1_main_dop.

Известно:
  5_main    → 3 798.18 га  (верный)
  1_main_dop → ~3 758 га   (на ~40 га меньше)

Запуск:
  python diag_today_diff.py [YYYY-MM-DD]

Зависимости: те же, что у обоих отчётов (plans, plans_1, httpx, requests).
"""

from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import httpx
import requests

from plans import *
from plans_1 import (
    API_TOKEN, BASE_URL, TZ_MSK,
    FIRM_TO_FOLDER, FOLDER_TO_FIRM, FIELD_NAME_FORCE_RULES,
)
try:
    from plans_1 import SKIP_CROPS
except ImportError:
    SKIP_CROPS: set[str] = set()

# ──────────────────────────────────────────────────────────────────────────────
REPORT_DATE = date.today()
if len(sys.argv) > 1:
    REPORT_DATE = date.fromisoformat(sys.argv[1])

SEASON_YEAR    = 2026
DAY_START_HOUR = 6
DAY_END_HOUR   = 6
SEMAPHORE_LIMIT = 40

_SEED_KEY = "4.0 Посев"
ALL_WT_IDS: set[int] = {
    wt_id
    for group_name, wt_ids in WORK_GROUPS
    if group_name == _SEED_KEY
    for wt_id in wt_ids
}

session = requests.Session()
session.headers.update({"X-User-Api-Token": API_TOKEN})

SEP = "─" * 80


def sep(title=""):
    print(f"\n{SEP}")
    if title:
        print(f"  {title}")
    print(SEP)


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ──────────────────────────────────────────────────────────────────────────────

def get_all_from_id(endpoint: str, extra_params: dict | None = None) -> list[dict]:
    params, results, from_id = dict(extra_params or {}), [], 0
    while True:
        r = session.get(f"{BASE_URL}/{endpoint}",
                        params={**params, "from_id": from_id, "limit": 1000})
        r.raise_for_status()
        body  = r.json()
        chunk = body.get("data", [])
        if not chunk:
            break
        results.extend(chunk)
        meta    = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        if not last_id or meta.get("obtained_records", 0) < 100:
            break
        from_id = last_id + 1
    return results


async def fetch_pages(client, url, params, semaphore):
    items, from_id = [], 0
    while True:
        async with semaphore:
            r     = await client.get(url, params={**params, "from_id": from_id, "limit": 1000})
            body  = r.json()
            chunk = body.get("data", [])
        if not chunk:
            break
        items.extend(chunk)
        meta    = body.get("meta", {}).get("response", {})
        last_id = meta.get("last_record_id")
        if not last_id or meta.get("obtained_records", 0) < 100:
            break
        from_id = last_id + 1
    return items


async def fetch_mtfmi_for_tasks(task_ids, semaphore, client):
    url = f"{BASE_URL}/machine_task_field_mapping_items"

    async def _one(tid):
        items, from_id = [], 0
        while True:
            async with semaphore:
                r     = await client.get(url, params={"machine_task_id": tid,
                                                       "from_id": from_id, "limit": 1000})
                body  = r.json()
                chunk = body.get("data", [])
            if not chunk:
                break
            items.extend(chunk)
            meta    = body.get("meta", {}).get("response", {})
            last_id = meta.get("last_record_id")
            if not last_id or meta.get("obtained_records", 0) < 100:
                break
            from_id = last_id + 1
        return tid, items

    results = await asyncio.gather(*[_one(tid) for tid in task_ids])
    return {tid: items for tid, items in results}


# ──────────────────────────────────────────────────────────────────────────────
# Time helpers
# ──────────────────────────────────────────────────────────────────────────────

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


def shift_cutoff_for_task(end_dt: datetime) -> datetime:
    """Возвращает shift_cutoff (верхнюю границу смены) по end_time задания."""
    cal_date = end_dt.date()
    anchor   = datetime(cal_date.year, cal_date.month, cal_date.day,
                        DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK)
    if end_dt < anchor:
        return datetime((cal_date - timedelta(days=1)).year,
                        (cal_date - timedelta(days=1)).month,
                        (cal_date - timedelta(days=1)).day,
                        DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK) + timedelta(days=1)
    return anchor + timedelta(days=1)


# ──────────────────────────────────────────────────────────────────────────────
# Reference loaders
# ──────────────────────────────────────────────────────────────────────────────

def load_field_to_firm(fields_all, fgs_all, gfs_all) -> dict[int, str]:
    def _norm(name):
        return (name or "").strip().casefold().replace(" ", "").replace("\u00a0", "")

    parent_map: dict[int, int | None] = {}
    for gf in gfs_all:
        fid = gf.get("id")
        if fid is None:
            continue
        pid = (gf.get("parent_id") or gf.get("parent_group_folder_id")
               or gf.get("group_folder_parent_id"))
        parent_map[int(fid)] = int(pid) if pid is not None else None

    hints: dict[int, str] = {}
    for gf in gfs_all:
        fid  = gf.get("id")
        blob = _norm(f"{gf.get('name') or ''} {gf.get('title') or ''}")
        if fid and any(x in blob for x in ("лениногор", "птицефабрик", "лпф", "lpf")):
            hints[int(fid)] = "Лениногорская Птицефабрика"

    def _chain(folder_id):
        if folder_id is None:
            return []
        out, cur, seen = [], int(folder_id), set()
        for _ in range(64):
            if cur in seen:
                break
            seen.add(cur); out.append(cur)
            nxt = parent_map.get(cur)
            if nxt is None:
                break
            cur = int(nxt)
        return out

    def _resolve_root(folder_id):
        if folder_id is None:
            return None
        root_ids = set(FIRM_TO_FOLDER.values())
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

    fgs_map = {fg["id"]: fg for fg in fgs_all}
    f2f: dict[int, str] = {}
    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        fgid       = f.get("field_group_id")
        raw_folder = f.get("group_folder_id")
        if raw_folder is None and fgid is not None:
            raw_folder = fgs_map.get(fgid, {}).get("group_folder_id")
        rint = int(raw_folder) if raw_folder is not None else None

        firm_key = None
        for cid in _chain(rint):
            if cid in hints:
                firm_key = hints[cid]
                break
        if firm_key is None:
            root = _resolve_root(rint)
            if root is not None:
                firm_key = FOLDER_TO_FIRM.get(root)
        if firm_key:
            f2f[int(fid)] = firm_key

    for f in fields_all:
        fid  = f.get("id")
        if fid is None:
            continue
        blob = _norm(f"{f.get('name') or ''} {f.get('title') or ''}")
        for needle, fk in FIELD_NAME_FORCE_RULES:
            if needle in blob:
                f2f[int(fid)] = fk
                break
    return f2f


def load_available_field_ids(period_start: date, period_end: date) -> set[int]:
    ihi_all = get_all_from_id("inventory_history_items", {"historyable_type": "Field"})
    ids: set[int] = set()
    for r in ihi_all:
        if not r.get("available"):
            continue
        fid = r.get("historyable_id")
        if fid is None:
            continue
        raw_start = r.get("event_start_at")
        raw_end   = r.get("event_end_at")
        try:
            ev_start = date.fromisoformat(str(raw_start)[:10]) if raw_start else None
        except ValueError:
            ev_start = None
        try:
            ev_end = date.fromisoformat(str(raw_end)[:10]) if raw_end else None
        except ValueError:
            ev_end = None
        if ev_start is None:
            continue
        if ev_start <= period_end and (ev_end is None or ev_end >= period_start):
            ids.add(int(fid))
    return ids


def load_crop_field_ids(year: int) -> set[int]:
    """Поля с активной культурой (не SKIP_CROPS) — фильтр из 5_main."""
    crops_map = {c["id"]: c.get("name") or c.get("title", "")
                 for c in get_all_from_id("crops")}
    his_all = get_all_from_id("history_items", {"year": year})
    ids: set[int] = set()
    for hi in his_all:
        fid     = hi.get("field_id")
        crop_id = hi.get("crop_id")
        active  = hi.get("active")
        if not (fid and crop_id and crop_id in crops_map and active):
            continue
        crop_name = crops_map[crop_id]
        if not crop_name or crop_name in SKIP_CROPS:
            continue
        ids.add(int(fid))
    return ids


def load_field_to_crop(year: int) -> dict[int, str]:
    """field_id → crop_name — для 1_main_dop (без crop_field_ids фильтра)."""
    crops_map = {c["id"]: c.get("name") or c.get("title", "")
                 for c in get_all_from_id("crops")}
    his_all = get_all_from_id("history_items", {"year": year})
    f2c: dict[int, str] = {}
    for hi in his_all:
        if not hi.get("active"):
            continue
        fid     = hi.get("field_id")
        crop_id = hi.get("crop_id")
        if not fid or not crop_id:
            continue
        cname = crops_map.get(crop_id, "")
        if not cname or cname in SKIP_CROPS:
            continue
        f2c[int(fid)] = cname
    return f2c


# ──────────────────────────────────────────────────────────────────────────────
# Core area calculators
# ──────────────────────────────────────────────────────────────────────────────

def calc_area_5main(
    task:            dict,
    mtfmi:           list[dict],
    available_ids:   set[int],
    crop_field_ids:  set[int],
    field_to_firm:   dict[int, str],
) -> dict[str, float]:
    """
    Площадь по логике 5_main:
      - фильтр available_field_ids  ✓
      - фильтр crop_field_ids       ✓  ← этого нет в 1_main_dop
      - covered_area_hourly < shift_cutoff
      - fallback covered_area если hourly пуст
      - порог > 0.5 га
    """
    end_dt = parse_dt(task.get("end_time"))
    if end_dt is None:
        sc = datetime(REPORT_DATE.year, REPORT_DATE.month, REPORT_DATE.day,
                      DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK)
    else:
        sc = shift_cutoff_for_task(end_dt)

    firm_cov: dict[str, float] = defaultdict(float)
    for item in mtfmi:
        fid_raw = item.get("field_id")
        if fid_raw is None:
            continue
        fid = int(fid_raw)
        if fid not in available_ids:
            continue
        if fid not in crop_field_ids:
            continue
        firm = field_to_firm.get(fid)
        if not firm:
            continue
        hourly = item.get("covered_area_hourly") or []
        cov = sum(float(e[1] or 0) for e in hourly if parse_dt(e[0]) and parse_dt(e[0]) < sc)
        if not hourly:
            cov = float(item.get("covered_area") or 0)
        if cov > 0.5:
            firm_cov[firm] += cov
    return dict(firm_cov)


def calc_area_1main(
    task:          dict,
    mtfmi:         list[dict],
    available_ids: set[int],
    field_to_firm: dict[int, str],
) -> dict[str, float]:
    """
    Площадь по логике 1_main_dop:
      - фильтр available_field_ids  ✓
      - crop_field_ids НЕТ          ← ключевое отличие
      - covered_area_hourly < shift_cutoff
      - fallback covered_area если hourly пуст
      - порог > 0.5 га
    """
    end_dt = parse_dt(task.get("end_time"))
    if end_dt is None:
        sc = datetime(REPORT_DATE.year, REPORT_DATE.month, REPORT_DATE.day,
                      DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK)
    else:
        sc = shift_cutoff_for_task(end_dt)

    firm_cov: dict[str, float] = defaultdict(float)
    for item in mtfmi:
        fid_raw = item.get("field_id")
        if fid_raw is None:
            continue
        fid = int(fid_raw)
        if fid not in available_ids:
            continue
        firm = field_to_firm.get(fid)
        if not firm:
            continue
        hourly = item.get("covered_area_hourly") or []
        cov = sum(float(e[1] or 0) for e in hourly if parse_dt(e[0]) and parse_dt(e[0]) < sc)
        if not hourly:
            cov = float(item.get("covered_area") or 0)
        if cov > 0.5:
            firm_cov[firm] += cov
    return dict(firm_cov)


# ──────────────────────────────────────────────────────────────────────────────
# Main diagnostic
# ──────────────────────────────────────────────────────────────────────────────

async def run_diag():
    day_start = datetime(REPORT_DATE.year, REPORT_DATE.month, REPORT_DATE.day,
                         DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK) - timedelta(days=1)
    cutoff    = datetime(REPORT_DATE.year, REPORT_DATE.month, REPORT_DATE.day,
                         DAY_END_HOUR, 0, 0, tzinfo=TZ_MSK)

    sep(f"ДИАГНОСТИКА расхождения «Факт за сутки»  |  Дата: {REPORT_DATE}")
    print(f"  Окно суток: {day_start}  —  {cutoff}")

    # ── 1. Справочники ────────────────────────────────────────────────────────
    sep("1. Загрузка справочников")
    print("  fields / field_groups / group_folders...")
    fields_all = get_all_from_id("fields")
    fgs_all    = get_all_from_id("field_groups")
    gfs_all    = get_all_from_id("group_folders")

    print("  field_to_firm...")
    field_to_firm = load_field_to_firm(fields_all, fgs_all, gfs_all)
    print(f"    Полей с привязкой к фирме: {len(field_to_firm)}")

    print("  available_field_ids...")
    available_ids = load_available_field_ids(date(SEASON_YEAR, 1, 1), REPORT_DATE)
    print(f"    available_field_ids: {len(available_ids)}")

    print("  crop_field_ids (только 5_main использует)...")
    crop_field_ids = load_crop_field_ids(SEASON_YEAR)
    print(f"    crop_field_ids: {len(crop_field_ids)}")

    in_available_not_in_crop = available_ids - crop_field_ids
    print(f"\n  [КЛЮЧ] Полей в available, но НЕ в crop_field_ids: {len(in_available_not_in_crop)}")
    print("  → Именно по этим полям 1_main_dop считает площадь, а 5_main — нет (если такие")
    print("    поля попадают в суточные задания, разница будет отрицательной для 5_main).")
    print("  ВНИМАНИЕ: у нас обратная картина — 5_main БОЛЬШЕ. Значит, есть поля,")
    print("  которые crop_field_ids содержит, но которых НЕТ в available_ids,")
    print("  ИЛИ другой источник разницы (логика hourly/fallback, порог 0.5).")

    in_crop_not_in_available = crop_field_ids - available_ids
    print(f"\n  Полей в crop_field_ids, но НЕ в available_ids: {len(in_crop_not_in_available)}")
    print("  → По этим полям 5_main будет отфильтровывать их через available, а 1_main_dop —")
    print("    через crop_field_ids. Взаимный охват важен.")

    # ── 2. Суточные задания ───────────────────────────────────────────────────
    sep("2. Загрузка суточных machine_tasks")
    day_start_str = day_start.strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_str    = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    headers   = {"X-User-Api-Token": API_TOKEN}
    semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
    pool      = httpx.Limits(max_connections=60, max_keepalive_connections=30)

    async with httpx.AsyncClient(headers=headers, timeout=120, limits=pool) as client:
        chunks = await asyncio.gather(*[
            fetch_pages(client, f"{BASE_URL}/machine_tasks", {
                "work_type_id":   wt,
                "end_time_gt_eq": day_start_str,
                "end_time_lt":    cutoff_str,
            }, semaphore)
            for wt in sorted(ALL_WT_IDS)
        ])
        seen: set[int] = set()
        tasks_today: list[dict] = []
        for chunk in chunks:
            for t in chunk:
                if t["id"] not in seen:
                    seen.add(t["id"])
                    tasks_today.append(t)
        print(f"  Уникальных суточных заданий: {len(tasks_today)}")

        task_ids = [t["id"] for t in tasks_today]
        print(f"  Загружаем MTFMI для {len(task_ids)} заданий...")
        mtfmi_map = await fetch_mtfmi_for_tasks(task_ids, semaphore, client)

    # ── 3. Попарное сравнение ─────────────────────────────────────────────────
    sep("3. Сравнение площади по каждому заданию")

    total_5   : dict[str, float] = defaultdict(float)
    total_1   : dict[str, float] = defaultdict(float)
    diff_tasks: list[dict] = []

    for t in tasks_today:
        tid   = t["id"]
        mtfmi = mtfmi_map.get(tid, [])

        a5 = calc_area_5main(t, mtfmi, available_ids, crop_field_ids, field_to_firm)
        a1 = calc_area_1main(t, mtfmi, available_ids, field_to_firm)

        sum5 = sum(a5.values())
        sum1 = sum(a1.values())

        for firm, v in a5.items():
            total_5[firm] += v
        for firm, v in a1.items():
            total_1[firm] += v

        delta = sum5 - sum1
        if abs(delta) > 0.01:
            diff_tasks.append({
                "task_id": tid,
                "wt_id":   t.get("work_type_id"),
                "end_time": t.get("end_time"),
                "area_5main":  round(sum5, 2),
                "area_1main":  round(sum1, 2),
                "delta":       round(delta, 2),
                "by_firm_5":   {k: round(v, 2) for k, v in a5.items()},
                "by_firm_1":   {k: round(v, 2) for k, v in a1.items()},
            })

    diff_tasks.sort(key=lambda x: -abs(x["delta"]))

    # ── 4. Итоги по фирмам ────────────────────────────────────────────────────
    sep("4. Итого «Факт за сутки» по агрофирмам")
    all_firms = sorted(set(list(total_5.keys()) + list(total_1.keys())))
    grand_5 = grand_1 = 0.0
    print(f"  {'Агрофирма':<35} {'5_main':>10} {'1_main':>10} {'Δ (5−1)':>10}")
    print("  " + "-" * 67)
    for firm in all_firms:
        v5 = round(total_5.get(firm, 0.0), 2)
        v1 = round(total_1.get(firm, 0.0), 2)
        d  = round(v5 - v1, 2)
        grand_5 += v5
        grand_1 += v1
        marker = " ◄" if abs(d) > 0.5 else ""
        print(f"  {firm:<35} {v5:>10.2f} {v1:>10.2f} {d:>+10.2f}{marker}")
    print("  " + "-" * 67)
    print(f"  {'ИТОГО':<35} {grand_5:>10.2f} {grand_1:>10.2f} {grand_5 - grand_1:>+10.2f}")

    # ── 5. Задания с расхождением ─────────────────────────────────────────────
    sep("5. Задания с расхождением (топ-30 по |Δ|)")
    if not diff_tasks:
        print("  Расхождений не найдено (все задания дают одинаковую площадь).")
    else:
        print(f"  Всего заданий с расхождением: {len(diff_tasks)}")
        print(f"  {'task_id':>10} {'wt_id':>6} {'5_main':>9} {'1_main':>9} {'Δ':>9}  end_time")
        print("  " + "-" * 80)
        for row in diff_tasks[:30]:
            print(f"  {row['task_id']:>10} {row['wt_id']:>6} "
                  f"{row['area_5main']:>9.2f} {row['area_1main']:>9.2f} "
                  f"{row['delta']:>+9.2f}  {row['end_time']}")

    # ── 6. Детальный анализ по полям первого расходящегося задания ────────────
    if diff_tasks:
        sep("6. Детальный разбор первого расходящегося задания")
        t_ex = next((t for t in tasks_today if t["id"] == diff_tasks[0]["task_id"]), None)
        if t_ex:
            tid   = t_ex["id"]
            mtfmi = mtfmi_map.get(tid, [])
            end_dt = parse_dt(t_ex.get("end_time"))
            sc     = shift_cutoff_for_task(end_dt) if end_dt else None

            print(f"  task_id={tid}  wt_id={t_ex.get('work_type_id')}  "
                  f"end_time={t_ex.get('end_time')}")
            print(f"  shift_cutoff = {sc}")
            print(f"  Итого mtfmi-записей: {len(mtfmi)}")
            print()
            print(f"  {'field_id':>10} {'avail':>6} {'crop_fi':>7} {'firm':<30} "
                  f"{'hourly':>7} {'cov_h':>8} {'cov_raw':>8} {'5_pass':>7} {'1_pass':>7}")
            print("  " + "-" * 100)

            for item in mtfmi:
                fid_raw = item.get("field_id")
                if fid_raw is None:
                    continue
                fid    = int(fid_raw)
                avail  = fid in available_ids
                cropfi = fid in crop_field_ids
                firm   = field_to_firm.get(fid, "—")
                hourly = item.get("covered_area_hourly") or []
                cov_h  = 0.0
                if sc:
                    cov_h = sum(float(e[1] or 0) for e in hourly
                                if parse_dt(e[0]) and parse_dt(e[0]) < sc)
                cov_raw = float(item.get("covered_area") or 0)
                cov_used = cov_h if hourly else cov_raw

                pass5 = avail and cropfi and bool(firm != "—") and cov_used > 0.5
                pass1 = avail and bool(firm != "—") and cov_used > 0.5

                differs = pass5 != pass1
                marker  = " ◄◄◄" if differs else ""
                print(f"  {fid:>10} {str(avail):>6} {str(cropfi):>7} {firm:<30} "
                      f"{len(hourly):>7} {cov_h:>8.2f} {cov_raw:>8.2f} "
                      f"{str(pass5):>7} {str(pass1):>7}{marker}")

    # ── 7. Гипотезы ───────────────────────────────────────────────────────────
    sep("7. Вероятные причины расхождения")

    only_in_crop = sum(1 for t in diff_tasks if t["delta"] > 0)
    only_in_avail = sum(1 for t in diff_tasks if t["delta"] < 0)

    print(f"""
  Δ итого: {grand_5 - grand_1:+.2f} га (5_main − 1_main_dop)

  Гипотеза A — crop_field_ids vs отсутствие этого фильтра
  ─────────────────────────────────────────────────────────
  5_main применяет ДВА фильтра к полям: available_field_ids И crop_field_ids.
  1_main_dop применяет только available_field_ids.

  Если поле есть в crop_field_ids но НЕ в available_ids:
    → 5_main его пропустит (нет в available)
    → 1_main_dop тоже пропустит (нет в available)
    → разницы нет

  Если поле есть в available_ids но НЕ в crop_field_ids:
    → 1_main_dop ЗАСЧИТАЕТ площадь
    → 5_main ПРОПУСТИТ (нет в crop_field_ids)
    → 1_main_dop будет БОЛЬШЕ на эту площадь

  Но у нас 5_main БОЛЬШЕ → основная причина, скорее всего, другая:

  Гипотеза B — разная логика фильтрации заданий по end_time
  ───────────────────────────────────────────────────────────
  5_main: задания за сутки фильтруются по end_time в [day_start, cutoff)
          при загрузке (серверный фильтр end_time_gt_eq / end_time_lt),
          затем площадь берётся по covered_area_hourly < shift_cutoff задания.

  1_main_dop: суточный диапазон задаётся через today_task_ids по end_time,
          НО в _write_crop_rows используется today_by_crop, который накапливается
          отдельно — проверьте, не попадают ли туда задания с end_time ровно
          на границе cutoff (включительно vs исключительно).

  Гипотеза C — covered_area_hourly vs covered_area fallback
  ──────────────────────────────────────────────────────────
  В 5_main: если hourly пуст → fallback на covered_area.
  В 1_main_dop: если hourly пуст → fallback на covered_area.
  Логика одинакова — эта гипотеза менее вероятна.

  Гипотеза D — порог 0.5 га и накопление
  ─────────────────────────────────────────
  В 5_main порог > 0.5 применяется к каждому mtfmi-item отдельно.
  В 1_main_dop — та же логика. Вряд ли даёт разницу.

  Рекомендация: смотри раздел 5 (задания с Δ > 0, т.е. 5_main > 1_main_dop)
  и раздел 6 (поля с pass5=True, pass1=False).
  Поля со статусом avail=True, cropfi=True, firm≠— и pass1=False
  будут точно указывать на источник расхождения.
""")

    sep("ДИАГНОСТИКА ЗАВЕРШЕНА")


if __name__ == "__main__":
    asyncio.run(run_diag())