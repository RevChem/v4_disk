"""
Отчёт «Сводка по операторам и машинам» (на дату смены)
=======================================================
Для каждой агрофирмы — один лист Excel. Каждая строка = одна машина/оператор в разрезе
одного поля за смену (дату). Колонки:

  Оператор | Машина | Агрофирма | Вид работ | Поле | Культура |
  Площадь поля, га | Обработано за смену, га | Обработано всего, га |
  % поля | Статус | Простой | Топливо, % | Начало смены | Конец смены

Простой = суммарный gap между mtfmi-записями одной задачи > IDLE_THRESHOLD_MIN.
Топливо берётся из поля fuel_level задачи (если есть в API).

Зависимости: openpyxl, requests, httpx
Константы агрофирм / токен берём из того же plans.py.
"""
from __future__ import annotations

import asyncio
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import httpx
import openpyxl
import requests
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# =============================================================================
# КОНФИГУРАЦИЯ  (подстрой под свой plans.py если нужно)
# =============================================================================

try:
    from plans import (
        API_TOKEN,
        BASE_URL,
        TZ_MSK,
        FIRM_ORDER,
        FIRM_TO_FOLDER,
        FOLDER_TO_FIRM,
        WORK_GROUPS,
        SKIP_CROPS,
        HIRED_GROUP_ID,
        FIELD_NAME_FORCE_RULES,
    )
except ImportError:
    # Fallback — заполни вручную если plans.py недоступен
    API_TOKEN      = "6DayNcBDr9BWEZgst1Dy"
    BASE_URL       = "https://operations.cropwise.com/api/v3"
    TZ_MSK         = timezone(timedelta(hours=3))
    HIRED_GROUP_ID = 7          # id группы «наёмная техника» — уточни
    FIRM_ORDER     = [
        "АФ Чистопольская", "АгроРаздолье", "Аксу-Агро",
        "НурлатАгро", "Дрожжаное Рассвет Агро", "Актай",
        "Лениногорская Птицефабрика",
    ]
    FIRM_TO_FOLDER = {
        "АФ Чистопольская": 3, "АгроРаздолье": 4, "Аксу-Агро": 5,
        "НурлатАгро": 11, "Дрожжаное Рассвет Агро": 6,
        "Актай": 7, "Лениногорская Птицефабрика": 9,
    }
    FOLDER_TO_FIRM = {v: k for k, v in FIRM_TO_FOLDER.items()}
    WORK_GROUPS    = []          # список [(название, [wt_id, ...])]
    SKIP_CROPS     = {"Залежь, лес", "Залежь", "Введение в оборот 2026 год",
                      "Луговые травы", "Обмен", "В пользовании у фермера",
                      "Введение в оборот 2027 год", "Введение в оборот",
                      "Передано в аренду", "Дороги/лесополосы", "Овраги/неудобия"}
    FIELD_NAME_FORCE_RULES = []

# Дата смены — по умолчанию вчера (завершённая смена)
REPORT_DATE: date = date.today() 
if os.environ.get("REPORT_DATE"):
    REPORT_DATE = date.fromisoformat(os.environ["REPORT_DATE"])

# Начало и конец смены: 06:00 МСК → 06:00 МСК следующего дня
DAY_START_HOUR  = 6
IDLE_THRESHOLD_MIN = 20    # минут простоя, чтобы считать простоем
SEASON_YEAR     = REPORT_DATE.year

OUTPUT_FILE = f"operator_report_{REPORT_DATE}.xlsx"

# =============================================================================
# HTTP
# =============================================================================

session = requests.Session()
session.headers.update({"X-User-Api-Token": API_TOKEN})
SEMAPHORE_LIMIT = 5


def get_all(endpoint: str, params: dict | None = None) -> list[dict]:
    p       = dict(params or {})
    results = []
    from_id = 0
    for _ in range(500):
        r = session.get(f"{BASE_URL}/{endpoint}",
                        params={**p, "from_id": from_id, "limit": 1000},
                        timeout=120)
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


def get_all_page(endpoint: str, params: dict | None = None) -> list[dict]:
    p = dict(params or {})
    p.setdefault("per_page", 1000)
    results, page = [], 1
    while True:
        r = session.get(f"{BASE_URL}/{endpoint}", params={**p, "page": page},
                        timeout=120)
        r.raise_for_status()
        body  = r.json()
        chunk = body if isinstance(body, list) else body.get("data", [])
        results.extend(chunk)
        if len(chunk) < p["per_page"]:
            break
        page += 1
    return results


async def _fetch_pages_async(
    client: httpx.AsyncClient,
    url: str,
    params: dict,
    semaphore: asyncio.Semaphore,
) -> list[dict]:
    items, from_id = [], 0
    while True:
        async with semaphore:
            r = await client.get(url, params={**params, "from_id": from_id, "limit": 1000})
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


async def fetch_mtfmi_async(task_ids: list[int]) -> dict[int, list[dict]]:
    sem = asyncio.Semaphore(SEMAPHORE_LIMIT)
    url = f"{BASE_URL}/machine_task_field_mapping_items"
    async with httpx.AsyncClient(
        headers={"X-User-Api-Token": API_TOKEN}, timeout=120
    ) as client:
        results = await asyncio.gather(*[
            _fetch_pages_async(client, url, {"machine_task_id": tid}, sem)
            for tid in task_ids
        ])
    return {tid: items for tid, items in zip(task_ids, results)}

# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ: ДАТЫ / ВРЕМЯ
# =============================================================================

def parse_dt(s) -> Optional[datetime]:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        ts = float(s)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ_MSK)
        except (OSError, ValueError, OverflowError):
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
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_MSK)
    else:
        dt = dt.astimezone(TZ_MSK)
    return dt


def shift_window(report_date: date):
    """Возвращает (day_start, cutoff) — окно смены дня report_date."""
    day_start = datetime(report_date.year, report_date.month, report_date.day,
                         DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK)
    cutoff    = day_start + timedelta(days=1)
    return day_start, cutoff


def fmt_duration(minutes: float) -> str:
    if minutes <= 0:
        return "—"
    h  = int(minutes) // 60
    m  = int(minutes) % 60
    if h:
        return f"{h}ч {m:02d}мин"
    return f"{m}мин"


def fmt_dt(dt: Optional[datetime]) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%d.%m %H:%M")

# =============================================================================
# ИЕРАРХИЯ ПАПОК → АГРОФИРМА
# =============================================================================

def _norm(s: str) -> str:
    return (s or "").strip().casefold().replace(" ", "").replace("\u00a0", "")


def _folder_parent_map(gfs: list[dict]) -> dict[int, int | None]:
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


def _resolve_firm(folder_id: int | None,
                  parent_map: dict[int, int | None],
                  root_ids: set[int]) -> int | None:
    if folder_id is None:
        return None
    cur  = int(folder_id)
    seen: set[int] = set()
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


def build_field_to_firm(fields_all, fgs_all, gfs_all) -> dict[int, str]:
    fgs_map    = {fg["id"]: fg for fg in fgs_all}
    parent_map = _folder_parent_map(gfs_all)
    root_ids   = set(FOLDER_TO_FIRM.keys())

    out: dict[int, str] = {}
    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        raw_folder = f.get("group_folder_id")
        fgid       = f.get("field_group_id")
        if raw_folder is None and fgid is not None:
            raw_folder = fgs_map.get(fgid, {}).get("group_folder_id")
        rint = int(raw_folder) if raw_folder is not None else None
        root = _resolve_firm(rint, parent_map, root_ids)
        if root is not None:
            firm = FOLDER_TO_FIRM.get(root)
            if firm:
                out[int(fid)] = firm

    # Принудительные правила по имени поля
    for f in fields_all:
        fid  = f.get("id")
        blob = _norm(f"{f.get('name') or ''} {f.get('title') or ''}")
        for needle, firm_key in FIELD_NAME_FORCE_RULES:
            if needle in blob:
                out[int(fid)] = firm_key
                break
    return out

# =============================================================================
# РАСЧЁТ ПРОСТОЯ ПО mtfmi
# =============================================================================

def calc_idle_minutes(mtfmi_items: list[dict],
                      day_start: datetime,
                      cutoff: datetime) -> float:
    """
    Суммарный простой внутри смены.
    Простой = gap между концом одной записи и началом следующей > IDLE_THRESHOLD_MIN.
    Если у записей нет временных меток — возвращает 0.
    """
    times: list[tuple[datetime, datetime]] = []
    for item in mtfmi_items:
        s = parse_dt(item.get("start_time") or item.get("started_at"))
        e = parse_dt(item.get("end_time")   or item.get("finished_at"))
        if s and e and s < e:
            # Обрезаем по окну смены
            s = max(s, day_start)
            e = min(e, cutoff)
            if s < e:
                times.append((s, e))
    if not times:
        return 0.0
    times.sort(key=lambda x: x[0])

    idle = 0.0
    for i in range(1, len(times)):
        gap_sec = (times[i][0] - times[i - 1][1]).total_seconds()
        gap_min = gap_sec / 60
        if gap_min > IDLE_THRESHOLD_MIN:
            idle += gap_min
    return round(idle, 1)

# =============================================================================
# ЗАГРУЗКА ДАННЫХ
# =============================================================================

def load_reference_data():
    print("  Поля...")
    fields_all = get_all("fields")
    fgs_all    = get_all("field_groups")
    gfs_all    = get_all("group_folders")

    field_name:  dict[int, str]   = {}
    field_area:  dict[int, float] = {}
    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        field_name[fid]  = f.get("name") or f.get("title") or f"field_{fid}"
        field_area[fid]  = float(f.get("tillable_area") or f.get("calculated_area") or 0.0)

    field_to_firm = build_field_to_firm(fields_all, fgs_all, gfs_all)

    print("  Культуры...")
    crops_raw     = get_all_page("crops")
    crop_id_name  = {c["id"]: (c.get("name") or c.get("title", "")) for c in crops_raw}
    his_all       = get_all("history_items", {"year": str(SEASON_YEAR)})
    field_to_crop: dict[int, str] = {}
    for hi in his_all:
        fid = hi.get("field_id"); cid = hi.get("crop_id")
        if not (fid and cid):
            continue
        cname = crop_id_name.get(cid, "")
        if not cname or cname in SKIP_CROPS:
            continue
        if hi.get("active"):
            field_to_crop[fid] = cname

    print("  Машины и операторы...")
    machines_all   = get_all("machines")
    machine_name:  dict[int, str] = {}
    machine_group: dict[int, int] = {}
    for m in machines_all:
        mid = m.get("id")
        if mid is None:
            continue
        machine_name[mid]  = (m.get("name") or m.get("title") or
                              m.get("model") or f"machine_{mid}")
        machine_group[mid] = m.get("machine_group_id") or 0

    # Операторы: пробуем эндпоинт workers или operators
    operators_map: dict[int, str] = {}
    for ep in ("workers", "operators", "users"):
        try:
            ops = get_all(ep) or get_all_page(ep)
            if ops:
                for o in ops:
                    oid = o.get("id")
                    if oid is None:
                        continue
                    name = (o.get("full_name") or o.get("name") or
                            f"{o.get('first_name','')} {o.get('last_name','')}".strip()
                            or f"{ep}_{oid}")
                    operators_map[oid] = name
                print(f"    Операторов из /{ep}: {len(operators_map)}")
                break
        except Exception:
            continue

    print("  Виды работ...")
    wt_raw = get_all("work_types") or get_all_page("work_types")
    work_type_map: dict[int, str] = {
        w["id"]: (w.get("name") or w.get("title") or f"wt_{w['id']}")
        for w in wt_raw
    }

    print("  Орудия (implements)...")
    impl_raw = get_all("implements") or get_all_page("implements")
    implements_map: dict[int, dict] = {
        impl["id"]: impl for impl in impl_raw if impl.get("id") is not None
    }
    print(f"    Орудий: {len(implements_map)}")

    return (field_name, field_area, field_to_firm, field_to_crop,
            machine_name, machine_group, operators_map, work_type_map,
            implements_map)


def collect_shift_data(
    field_to_firm:  dict[int, str],
    field_to_crop:  dict[int, str],
    field_area:     dict[int, float],
    field_name:     dict[int, str],
    machine_name:   dict[int, str],
    machine_group:  dict[int, int],
    operators_map:  dict[int, str],
    work_type_map:  dict[int, str],
    implements_map: dict[int, dict],
) -> list[dict]:
    """
    Собирает все строки отчёта: одна строка = машина × поле за смену.
    """
    day_start, cutoff = shift_window(REPORT_DATE)
    day_start_str     = day_start.strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_str        = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    print(f"\n  Загружаем machine_tasks за смену {REPORT_DATE} "
          f"({day_start_str} — {cutoff_str})...")

    # Собираем задачи всех видов работ из WORK_GROUPS
    all_tasks: list[dict] = []
    wt_ids_seen: set[int] = set()
    for _group_name, wt_ids in WORK_GROUPS:
        for wt_id in wt_ids:
            if wt_id in wt_ids_seen:
                continue
            wt_ids_seen.add(wt_id)
            tasks = get_all("machine_tasks", {
                "work_type_id":   wt_id,
                "end_time_gt_eq": day_start_str,
                "end_time_lt":    cutoff_str,
            })
            all_tasks.extend(tasks)

    # Если WORK_GROUPS пустой — берём все задачи за смену
    if not all_tasks:
        print("  WORK_GROUPS пустой, загружаем все задачи за смену...")
        all_tasks = get_all("machine_tasks", {
            "end_time_gt_eq": day_start_str,
            "end_time_lt":    cutoff_str,
        })

    # Дедупликация по id
    seen_ids: set[int] = set()
    unique_tasks: list[dict] = []
    for t in all_tasks:
        tid = t.get("id")
        if tid not in seen_ids:
            seen_ids.add(tid)
            unique_tasks.append(t)
    all_tasks = unique_tasks

    print(f"    Задач в смене: {len(all_tasks)}")
    if not all_tasks:
        return []

    # Загружаем mtfmi асинхронно
    task_ids  = [t["id"] for t in all_tasks]
    mtfmi_map = asyncio.run(fetch_mtfmi_async(task_ids))

    # Накапливаем площадь за весь сезон по полю (для % поля)
    # season_area[fid] = сколько га обработано с начала сезона по всем задачам
    # Для простоты используем covered_area_hourly из mtfmi (уже накоплено)
    # — это делается ниже при обходе записей

    # Сезонный итог по полю (нужен для столбца «Обработано всего»)
    print("  Загружаем all_tasks за сезон для сезонного итога по полям...")
    season_start_str = f"{SEASON_YEAR}-01-01T00:00:00"
    season_area_by_field: dict[int, float] = defaultdict(float)
    for _group_name, wt_ids in WORK_GROUPS:
        for wt_id in wt_ids:
            s_tasks = get_all("machine_tasks", {
                "work_type_id":   wt_id,
                "end_time_gt_eq": season_start_str,
                "end_time_lt":    cutoff_str,
            })
            for st in s_tasks:
                for item in mtfmi_map.get(st["id"], []):
                    fid = item.get("field_id")
                    if fid:
                        cov = float(item.get("covered_area") or 0)
                        season_area_by_field[fid] += cov

    # Если WORK_GROUPS пустой — сезонный итог из уже загруженных mtfmi
    if not WORK_GROUPS:
        for tid, items in mtfmi_map.items():
            for item in items:
                fid = item.get("field_id")
                if fid:
                    cov = float(item.get("covered_area") or 0)
                    season_area_by_field[fid] += cov

    # Строим строки отчёта
    rows: list[dict] = []

    for task in all_tasks:
        tid  = task["id"]
        mid  = task.get("machine_id")
        # Оператор — ищем в нескольких возможных полях
        op_id = (task.get("operator_id") or task.get("worker_id")
                 or task.get("user_id"))
        operator_name = operators_map.get(op_id, "—") if op_id else "—"
        mach_name     = machine_name.get(mid, f"machine_{mid}") if mid else "—"
        wt_id         = task.get("work_type_id")
        work_type     = work_type_map.get(wt_id, "—") if wt_id else "—"

        t_start = parse_dt(task.get("start_time") or task.get("started_at"))
        t_end   = parse_dt(task.get("end_time")   or task.get("finished_at"))

        # Орудие
        impl_id  = task.get("implement_id")
        impl_rec = implements_map.get(impl_id) if impl_id else None
        implement_name = ""
        if impl_rec:
            implement_name = str(
                impl_rec.get("custom_name") or impl_rec.get("name") or ""
            ).strip()

        # Статус задачи
        raw_status = str(task.get("status") or "").strip().lower()
        if raw_status in ("in_progress", "active", "started"):
            status = "✅ Работает"
        elif raw_status in ("completed", "done", "finished"):
            status = "☑ Завершено"
        elif raw_status in ("paused", "idle"):
            status = "⏸ Простой"
        else:
            status = raw_status or "—"

        # Топливо (если API возвращает)
        fuel_raw = task.get("fuel_level") or task.get("fuel_remaining")
        fuel_str = f"{round(float(fuel_raw))}%" if fuel_raw is not None else "—"

        items_for_task = mtfmi_map.get(tid, [])

        # Считаем простой по всем mtfmi задачи
        idle_min   = calc_idle_minutes(items_for_task, day_start, cutoff)
        idle_str   = fmt_duration(idle_min)

        # Группируем mtfmi по полю
        field_cov: dict[int, float] = defaultdict(float)
        for item in items_for_task:
            fid  = item.get("field_id")
            if not fid:
                continue
            # covered_area_hourly — обрезаем по смене
            hourly = item.get("covered_area_hourly") or []
            if hourly:
                cov = 0.0
                for entry in hourly:
                    h = parse_dt(entry[0])
                    if h and day_start <= h < cutoff:
                        cov += entry[1] or 0
            else:
                cov = float(item.get("covered_area") or 0)
            if cov > 0:
                field_cov[fid] += cov

        if not field_cov:
            # Задача без площади — добавляем одну строку без поля
            rows.append(_make_row(
                operator=operator_name,
                machine=mach_name,
                implement=implement_name or "—",
                firm="—",
                work_type=work_type,
                field_name="—",
                crop="—",
                field_area_ha=None,
                covered_shift=0.0,
                covered_season=0.0,
                status=status,
                idle_str=idle_str,
                fuel_str=fuel_str,
                t_start=fmt_dt(t_start),
                t_end=fmt_dt(t_end),
            ))
            continue

        for fid, cov_shift in field_cov.items():
            firm       = field_to_firm.get(fid, "—")
            crop       = field_to_crop.get(fid, "—")
            if crop in SKIP_CROPS:
                continue
            fname      = field_name.get(fid, f"field_{fid}")
            f_area     = field_area.get(fid)
            cov_season = round(season_area_by_field.get(fid, 0.0), 1)

            rows.append(_make_row(
                operator=operator_name,
                machine=mach_name,
                implement=implement_name or "—",
                firm=firm,
                work_type=work_type,
                field_name=fname,
                crop=crop,
                field_area_ha=f_area,
                covered_shift=round(cov_shift, 1),
                covered_season=cov_season,
                status=status,
                idle_str=idle_str,
                fuel_str=fuel_str,
                t_start=fmt_dt(t_start),
                t_end=fmt_dt(t_end),
            ))

    return rows


def _make_row(**kw) -> dict:
    f_area = kw.get("field_area_ha")
    cov_s  = kw.get("covered_season", 0.0)
    pct    = None
    if f_area and f_area > 0:
        pct = round(cov_s / f_area * 100, 1)
    return {
        "operator":       kw["operator"],
        "machine":        kw["machine"],
        "implement":      kw.get("implement", "—"),
        "firm":           kw["firm"],
        "work_type":      kw["work_type"],
        "field_name":     kw["field_name"],
        "crop":           kw["crop"],
        "field_area_ha":  f_area,
        "covered_shift":  kw.get("covered_shift", 0.0),
        "covered_season": kw.get("covered_season", 0.0),
        "pct_field":      pct,
        "status":         kw["status"],
        "idle":           kw["idle_str"],
        "fuel":           kw["fuel_str"],
        "t_start":        kw["t_start"],
        "t_end":          kw["t_end"],
    }

# =============================================================================
# СТИЛИ
# =============================================================================

_C_HDR   = "2E4DA0"   # тёмно-синий заголовок
_C_TITLE = "D9E1F2"   # светло-синий шапка листа
_C_TOTAL = "BDD7EE"   # итоговая строка
_C_ODD   = "FFFFFF"
_C_EVEN  = "EEF4FB"
_C_IDLE  = "FFE0B2"   # подсветка: есть простой
_C_FUEL  = "FFCDD2"   # подсветка: топливо низкое
_C_WORK  = "E8F5E9"   # подсветка: работает


def _side() -> Border:
    t = Side(style="thin", color="BBBBBB")
    return Border(left=t, right=t, top=t, bottom=t)


def _cell(ws, row: int, col: int, value=None,
          bold=False, bg: str | None = None, color="000000",
          halign="center", wrap=False, fmt: str | None = None,
          size=9) -> None:
    c = ws.cell(row=row, column=col, value=value)
    c.font      = Font(name="Calibri", size=size, bold=bold, color=color)
    c.alignment = Alignment(horizontal=halign, vertical="center", wrap_text=wrap)
    c.border    = _side()
    if bg:
        c.fill = PatternFill("solid", fgColor=bg)
    if fmt:
        c.number_format = fmt

# =============================================================================
# ЗАПИСЬ ЛИСТА
# =============================================================================

HEADERS = [
    "Оператор",
    "Машина",
    "Орудие",
    "Агрофирма",
    "Вид работ",
    "Поле",
    "Культура",
    "Площадь\nполя, га",
    "Обработано\nза смену, га",
    "Обработано\nза сезон, га",
    "% поля\n(сезон)",
    "Статус",
    "Простой",
    "Топливо",
    "Начало\nсмены",
    "Конец\nсмены",
]

COL_W = [20, 20, 20, 20, 24, 18, 18, 10, 12, 12, 9, 14, 11, 9, 13, 13]


def write_firm_sheet(wb, firm_name: str, rows: list[dict]) -> None:
    ws = wb.create_sheet(title=firm_name[:31])

    for ci, w in enumerate(COL_W, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = w

    r = 1
    # Заголовок листа
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(HEADERS))
    c = ws.cell(row=r, column=1,
                value=f"Сводка по операторам и машинам — {firm_name}  |  Смена {REPORT_DATE.strftime('%d.%m.%Y')}")
    c.font      = Font(name="Calibri", size=12, bold=True)
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.fill      = PatternFill("solid", fgColor=_C_TITLE)
    c.border    = _side()
    ws.row_dimensions[r].height = 22
    r += 1

    # Шапка
    for ci, h in enumerate(HEADERS, start=1):
        _cell(ws, r, ci, h, bold=True, bg=_C_HDR, color="FFFFFF",
              halign="center", wrap=True, size=9)
    ws.row_dimensions[r].height = 36
    ws.freeze_panes = f"A{r+1}"
    r += 1

    # Данные
    for i, row in enumerate(rows):
        bg = _C_ODD if i % 2 == 0 else _C_EVEN

        # Цветовые сигналы
        status_bg = bg
        idle_bg   = bg
        fuel_bg   = bg
        if "Работает" in (row["status"] or ""):
            status_bg = _C_WORK
        if row["idle"] != "—":
            idle_bg = _C_IDLE
        fuel_val = row["fuel"]
        if fuel_val != "—":
            try:
                fnum = float(fuel_val.replace("%", "").strip())
                if fnum < 20:
                    fuel_bg = _C_FUEL
            except ValueError:
                pass

        vals_bg = [
            (row["operator"],        bg,        "left"),    # 1  Оператор
            (row["machine"],         bg,        "left"),    # 2  Машина
            (row.get("implement", "—"), bg,     "left"),    # 3  Орудие
            (row["firm"],            bg,        "left"),    # 4  Агрофирма
            (row["work_type"],       bg,        "left"),    # 5  Вид работ
            (row["field_name"],      bg,        "left"),    # 6  Поле
            (row["crop"],            bg,        "left"),    # 7  Культура
            (row["field_area_ha"],   bg,        "right"),   # 8  Площадь поля
            (row["covered_shift"],   bg,        "right"),   # 9  Обработано за смену
            (row["covered_season"],  bg,        "right"),   # 10 Обработано за сезон
            (row["pct_field"],       bg,        "center"),  # 11 % поля
            (row["status"],          status_bg, "center"),  # 12 Статус
            (row["idle"],            idle_bg,   "center"),  # 13 Простой
            (row["fuel"],            fuel_bg,   "center"),  # 14 Топливо
            (row["t_start"],         bg,        "center"),  # 15 Начало смены
            (row["t_end"],           bg,        "center"),  # 16 Конец смены
        ]

        for ci, (val, cell_bg, align) in enumerate(vals_bg, start=1):
            fmt = None
            if ci in (8, 9, 10) and isinstance(val, (int, float)):
                fmt = "# ##0.0"
            elif ci == 11 and isinstance(val, (int, float)):
                fmt = "# ##0.0"
            _cell(ws, r, ci, val, bg=cell_bg, halign=align, fmt=fmt)
        ws.row_dimensions[r].height = 15
        r += 1

    # Итоговая строка
    total_shift  = sum(row["covered_shift"]  for row in rows if isinstance(row["covered_shift"], (int, float)))
    total_season = sum(row["covered_season"] for row in rows if isinstance(row["covered_season"], (int, float)))
    n_machines   = len({row["machine"] for row in rows if row["machine"] != "—"})
    n_operators  = len({row["operator"] for row in rows if row["operator"] != "—"})

    summary_vals = [
        f"Операторов: {n_operators}",   # 1  Оператор
        f"Машин: {n_machines}",          # 2  Машина
        "",                              # 3  Орудие
        "",                              # 4  Агрофирма
        "",                              # 5  Вид работ
        f"Полей: {len(rows)}",           # 6  Поле
        "",                              # 7  Культура
        "",                              # 8  Площадь поля
        round(total_shift, 1),           # 9  Обработано за смену
        round(total_season, 1),          # 10 Обработано за сезон
        "",                              # 11–16
        "", "", "", "", "",
    ]
    for ci, val in enumerate(summary_vals, start=1):
        fmt = "# ##0.0" if ci in (9, 10) and isinstance(val, float) else None
        _cell(ws, r, ci, val, bold=True, bg=_C_TOTAL,
              halign="right" if ci in (9, 10) else "left", fmt=fmt)
    ws.row_dimensions[r].height = 16


def write_summary_sheet(wb, all_rows: list[dict]) -> None:
    """Сводный лист — все агрофирмы в одной таблице, отсортировано по агрофирме."""
    ws = wb.create_sheet(title="Все агрофирмы")
    wb.move_sheet("Все агрофирмы", offset=-len(wb.sheetnames) + 1)

    for ci, w in enumerate(COL_W, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = w

    r = 1
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(HEADERS))
    c = ws.cell(row=r, column=1,
                value=f"СВОДКА ПО ВСЕМ АГРОФИРМАМ  |  Смена {REPORT_DATE.strftime('%d.%m.%Y')}")
    c.font      = Font(name="Calibri", size=13, bold=True)
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.fill      = PatternFill("solid", fgColor="1E3A6E")
    c.font      = Font(name="Calibri", size=13, bold=True, color="FFFFFF")
    c.border    = _side()
    ws.row_dimensions[r].height = 24
    r += 1

    for ci, h in enumerate(HEADERS, start=1):
        _cell(ws, r, ci, h, bold=True, bg=_C_HDR, color="FFFFFF",
              halign="center", wrap=True, size=9)
    ws.row_dimensions[r].height = 36
    ws.freeze_panes = f"A{r+1}"
    r += 1

    sorted_rows = sorted(all_rows, key=lambda x: (
        FIRM_ORDER.index(x["firm"]) if x["firm"] in FIRM_ORDER else 999,
        x["operator"],
        x["machine"],
    ))

    for i, row in enumerate(sorted_rows):
        bg        = _C_ODD if i % 2 == 0 else _C_EVEN
        status_bg = _C_WORK if "Работает" in (row["status"] or "") else bg
        idle_bg   = _C_IDLE if row["idle"] != "—" else bg
        fuel_bg   = bg
        fuel_val  = row["fuel"]
        if fuel_val != "—":
            try:
                if float(fuel_val.replace("%", "").strip()) < 20:
                    fuel_bg = _C_FUEL
            except ValueError:
                pass

        vals_bg = [
            (row["operator"],          bg,        "left"),    # 1  Оператор
            (row["machine"],           bg,        "left"),    # 2  Машина
            (row.get("implement", "—"), bg,        "left"),   # 3  Орудие
            (row["firm"],              bg,        "left"),    # 4  Агрофирма
            (row["work_type"],         bg,        "left"),    # 5  Вид работ
            (row["field_name"],        bg,        "left"),    # 6  Поле
            (row["crop"],              bg,        "left"),    # 7  Культура
            (row["field_area_ha"],     bg,        "right"),   # 8  Площадь поля
            (row["covered_shift"],     bg,        "right"),   # 9  Обработано за смену
            (row["covered_season"],    bg,        "right"),   # 10 Обработано за сезон
            (row["pct_field"],         bg,        "center"),  # 11 % поля
            (row["status"],            status_bg, "center"),  # 12 Статус
            (row["idle"],              idle_bg,   "center"),  # 13 Простой
            (row["fuel"],              fuel_bg,   "center"),  # 14 Топливо
            (row["t_start"],           bg,        "center"),  # 15 Начало смены
            (row["t_end"],             bg,        "center"),  # 16 Конец смены
        ]

        for ci, (val, cell_bg, align) in enumerate(vals_bg, start=1):
            fmt = "# ##0.0" if ci in (8, 9, 10, 11) and isinstance(val, (int, float)) else None
            _cell(ws, r, ci, val, bg=cell_bg, halign=align, fmt=fmt)
        ws.row_dimensions[r].height = 15
        r += 1

    # Итог по всем
    t_shift  = sum(x["covered_shift"]  for x in all_rows if isinstance(x["covered_shift"],  (int, float)))
    t_season = sum(x["covered_season"] for x in all_rows if isinstance(x["covered_season"], (int, float)))
    n_m = len({x["machine"]  for x in all_rows if x["machine"]  != "—"})
    n_o = len({x["operator"] for x in all_rows if x["operator"] != "—"})

    summary = [f"Операторов: {n_o}", f"Машин: {n_m}", "", "",
               f"Строк: {len(all_rows)}", "", "", "",
               round(t_shift, 1), round(t_season, 1),
               "", "", "", "", "", ""]
    for ci, val in enumerate(summary, start=1):
        fmt = "# ##0.0" if ci in (9, 10) and isinstance(val, float) else None
        _cell(ws, r, ci, val, bold=True, bg=_C_TOTAL,
              halign="right" if ci in (9, 10) else "left", fmt=fmt)
    ws.row_dimensions[r].height = 16

# =============================================================================
# MAIN
# =============================================================================

def build_report() -> None:
    print(f"=== Отчёт по операторам и машинам  |  Смена: {REPORT_DATE} ===\n")

    (field_name, field_area, field_to_firm, field_to_crop,
     machine_name, machine_group, operators_map,
     work_type_map, implements_map) = load_reference_data()

    all_rows = collect_shift_data(
        field_to_firm, field_to_crop, field_area, field_name,
        machine_name, machine_group, operators_map, work_type_map,
        implements_map,
    )

    print(f"\n  Итого строк: {len(all_rows)}")

    # Группируем по агрофирмам
    by_firm: dict[str, list[dict]] = defaultdict(list)
    for row in all_rows:
        by_firm[row["firm"]].append(row)

    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    # Сводный лист первым
    write_summary_sheet(wb, all_rows)

    # Листы по агрофирмам
    for firm in FIRM_ORDER:
        rows_firm = by_firm.get(firm, [])
        print(f"  → {firm}: {len(rows_firm)} строк")
        write_firm_sheet(wb, firm, rows_firm)

    # Фирмы, которых нет в FIRM_ORDER (на всякий случай)
    for firm, rows_firm in by_firm.items():
        if firm not in FIRM_ORDER:
            write_firm_sheet(wb, firm or "Неизвестно", rows_firm)

    wb.save(OUTPUT_FILE)
    print(f"\n✓ Сохранено: {OUTPUT_FILE}")


if __name__ == "__main__":
    build_report()