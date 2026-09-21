"""
Генератор аналитического отчёта «Информация по ВПР за сутки».

Источники данных:
  1. CropWise API (https://operations.cropwise.com/api/v3) — данные о работах,
     полях, машинах (логика из 2_main_ideal.py)
  2. Downtime API (https://mech.chistai.ru/v1/downtime) — данные о простоях

Результат: файл Word (.docx) с отчётом за указанную дату.

Использование:
    python ranis_svodka.py [YYYY-MM-DD]
    Аргумент и REPORT_DATE в окружении — штамп отчёта (день запуска): сутки в отчёте —
    предыдущий день (завершённые смены), сравнение — с ещё одним днём назад.
    Явно задать день в отчёте: SVODKI_ANALYTICS_DATE=YYYY-MM-DD.
    Альтернатива штампу: SVODKI_REPORT_STAMP=YYYY-MM-DD.

Зависимости:
    pip install requests httpx python-docx openpyxl
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import httpx
import requests
from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH


def _r23_fertilizer_transport_machine_count(fallback: int) -> int:
    """runpy init_globals `_SVODKI_R23_MACHINES`, файл в cwd, SIDECAR, env; иначе fallback из Cropwise."""
    g = globals()
    gv = g.get("_SVODKI_R23_MACHINES")
    if isinstance(gv, int) and 0 <= gv <= 999_999:
        return gv
    try:
        p = Path.cwd() / ".r23_fertilizer_transport_machines"
        if p.is_file():
            s = p.read_text(encoding="utf-8").strip()
            if s.isdigit():
                n = int(s)
                if 0 <= n <= 999_999:
                    return n
    except OSError:
        pass
    side = (os.environ.get("SVODKI_R23_MACHINES_SIDECAR") or "").strip()
    if side:
        try:
            p = Path(side)
            if p.is_file():
                s = p.read_text(encoding="utf-8").strip()
                if s.isdigit():
                    n = int(s)
                    if 0 <= n <= 999_999:
                        return n
        except OSError:
            pass
    raw = os.environ.get("SVODKI_R23_FERTILIZER_TRANSPORT_MACHINES")
    if raw is None:
        return fallback
    s = str(raw).strip()
    if not s:
        return fallback
    try:
        n = int(s)
    except ValueError:
        return fallback
    if n < 0:
        return fallback
    return n


# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

CROPWISE_TOKEN = "6DayNcBDr9BWEZgst1Dy"
CROPWISE_URL   = "https://operations.cropwise.com/api/v3"

DOWNTIME_API_KEY = "sk_74220500d6ee8ea412496a62fecdfc5f09f8f922"   # <-- вставьте полный ключ
DOWNTIME_URL     = "https://mech.chistai.ru/v1/downtime"

def _first_iso_date_in_argv() -> date | None:
    """Первый аргумент вида YYYY-MM-DD (игнорируя --diag и прочие флаги)."""
    for a in sys.argv[1:]:
        if a.startswith("-"):
            continue
        try:
            return date.fromisoformat(a.strip())
        except ValueError:
            continue
    return None


def _resolve_analytics_dates() -> tuple[date, date, date]:
    """
    (REPORT_DATE, PREV_DATE, REPORT_STAMP).
    REPORT_STAMP — день запуска; фактические сутки в отчёте = STAMP − 1 день.
    """
    if os.environ.get("SVODKI_ANALYTICS_DATE"):
        rd = date.fromisoformat(os.environ["SVODKI_ANALYTICS_DATE"].strip())
        return rd, rd - timedelta(days=1), rd + timedelta(days=1)

    cli = _first_iso_date_in_argv()
    if cli is not None:
        stamp = cli
    elif os.environ.get("SVODKI_REPORT_STAMP"):
        stamp = date.fromisoformat(os.environ["SVODKI_REPORT_STAMP"].strip())
    elif os.environ.get("REPORT_DATE"):
        stamp = date.fromisoformat(os.environ["REPORT_DATE"].strip())
    else:
        stamp = date.today()

    report = stamp - timedelta(days=1)
    prev = report - timedelta(days=1)
    return report, prev, stamp


REPORT_DATE, PREV_DATE, REPORT_STAMP = _resolve_analytics_dates()
START_DATE  = date(2026, 3, 15)   # начало сезона

TZ_MSK = timezone(timedelta(hours=3))

HIRED_GROUP_ID = 7   # machine_group_id == 7 → НАЁМНЫЕ

FIRM_TO_FOLDER: dict[str, int] = {
    "АФ Чистопольская":           3,
    "АгроРаздолье":               4,
    "Аксу-Агро":                  5,
    "НурлатАгро":                 11,
    "Дрожжаное Рассвет Агро":     6,
    "Актай":                      7,
    "Лениногорская Птицефабрика": 9,
}
FOLDER_TO_FIRM = {v: k for k, v in FIRM_TO_FOLDER.items()}
FIRM_ORDER     = list(FIRM_TO_FOLDER.keys())

# Короткие аббревиатуры для отчёта
FIRM_SHORT: dict[str, str] = {
    "АФ Чистопольская":           "АФЧ",
    "АгроРаздолье":               "АР",
    "Аксу-Агро":                  "АА",
    "НурлатАгро":                 "НА",
    "Дрожжаное Рассвет Агро":     "ДРА",
    "Актай":                      "Актай",
    "Лениногорская Птицефабрика": "ЛПФ",
}

FIELD_NAME_FORCE_RULES: list[tuple[str, str]] = [
    ("лп0115", "Лениногорская Птицефабрика"),
    ("lp0115", "Лениногорская Птицефабрика"),
    ("пф0115", "Лениногорская Птицефабрика"),
]

WORK_GROUPS: list[tuple[str, list[int]]] = [
    ("1.1 Внесение минеральных удобрений", [55]),
    ("2.1 Боронование",                    [103, 81, 97, 12]),
    ("2.9 Дискование",                     [60]),
    ("2.4 Вспашка",                        [171]),
    ("2.11 Культивация",                   [96]),
    ("4.0 Посев",                          [69, 70, 71, 72, 119, 120]),
    ("4.1 Посев (овощи)",                  [68, 172]),
    ("3.1 Опрыскивание",                   [64, 1]),
    ("2.12 Прикатывание",                  [13]),
    ("2.7 Гребнеобразование",              [62]),
    ("2.8 Глубокорыхление",                [11]),
    ("5.0 Уборка",                         [6, 7, 75, 76, 77, 111, 118, 125, 126, 145]),
]

SKIP_CROPS = {
    "Дороги/лесополосы", "Овраги/неудобия", "Залежь, лес", 
    "В пользовании у фермера", "crop_None",
}

OZ_RAPE_CROP_NAMES = {"рапс озимый"}
YR_RAPE_CROP_NAMES = {"рапс яровой"}
RAPE_CROP_NAMES    = OZ_RAPE_CROP_NAMES | YR_RAPE_CROP_NAMES  # оба вида рапса
WORK_LABEL: dict[str, str] = {
    "1.1 Внесение минеральных удобрений": "разбрасывание минеральных удобрений",
    "2.1 Боронование":                    "боронование",
    "2.9 Дискование":                     "дискование",
    "2.4 Вспашка":                        "вспашка",
    "2.11 Культивация":                   "культивация",
    "4.0 Посев":                          "посев",
    "4.1 Посев (овощи)":                  "посев (овощи)",
    "3.1 Опрыскивание":                   "опрыскивание",
    "2.12 Прикатывание":                  "прикатывание",
    "2.7 Гребнеобразование":              "гребнеобразование",
    "2.8 Глубокорыхление":                "глубокорыхление",
    "5.0 Уборка":                         "уборка",
}
# Предложный падеж фразы после «на …» в блоке про технику (нельзя просто label + «е»).
WORK_LABEL_AFTER_NA: dict[str, str] = {
    "1.1 Внесение минеральных удобрений": "разбрасывании минеральных удобрений",
    "2.1 Боронование":                    "бороновании",
    "2.9 Дискование":                     "дисковании",
    "2.4 Вспашка":                        "вспашке",
    "2.11 Культивация":                   "культивации",
    "4.0 Посев":                          "посеве",
    "4.1 Посев (овощи)":                  "посеве (овощи)",
    "3.1 Опрыскивание":                   "опрыскивании",
    "2.12 Прикатывание":                  "прикатывании",
    "2.7 Гребнеобразование":              "гребнеобразовании",
    "2.8 Глубокорыхление":                "глубокорыхлении",
    "5.0 Уборка":                         "уборке",
}

SEMAPHORE_LIMIT = 5

# Подтипы агрегатов для Варианта Б (machine_subtype из /machines)
SELF_PROPELLED_SUBTYPES = {
    "spreader", "self_propelled_spreader", "self_propelled_sprayer",
    "sprayer", "combine_harvester", "forage_harvester",
    "self_propelled_mower", "self_propelled_baler",
}
TRACTOR_SUBTYPES = {
    "tractor", "powerful_tractor", "energy_saturated_tractor",
}

# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =============================================================================

def is_skip_crop(crop_name: str) -> bool:
    if not crop_name:
        return True
    if crop_name in SKIP_CROPS:
        return True
    return False


def _norm_field_label(name: str) -> str:
    return (name or "").strip().casefold().replace(" ", "").replace("\u00a0", "")


def _firm_from_field_name(field_rec: dict) -> str | None:
    blob = _norm_field_label(
        f"{field_rec.get('name') or ''} {field_rec.get('title') or ''}"
    )
    if not blob:
        return None
    for needle, firm_key in FIELD_NAME_FORCE_RULES:
        if needle in blob:
            return firm_key
    return None


def _folder_parent_map(group_folders: list[dict]) -> dict[int, int | None]:
    parent: dict[int, int | None] = {}
    for gf in group_folders:
        fid = gf.get("id")
        if fid is None:
            continue
        pid = (gf.get("parent_id")
               or gf.get("parent_group_folder_id")
               or gf.get("group_folder_parent_id"))
        parent[int(fid)] = int(pid) if pid is not None else None
    return parent


def _group_folder_name_firm_hints(group_folders: list[dict]) -> dict[int, str]:
    hints: dict[int, str] = {}
    for gf in group_folders:
        fid = gf.get("id")
        if fid is None:
            continue
        blob = _norm_field_label(f"{gf.get('name') or ''} {gf.get('title') or ''}")
        if not blob:
            continue
        if any(x in blob for x in ("лениногор", "птицефабрик", "лпф", "lpf")):
            hints[int(fid)] = "Лениногорская Птицефабрика"
    return hints


def _resolve_to_holding_root(
    folder_id:  int | None,
    parent_map: dict[int, int | None],
    root_ids:   set[int],
) -> int | None:
    if folder_id is None:
        return None
    cur = int(folder_id)
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


def _folder_id_chain(folder_id: int | None, parent_map: dict[int, int | None]) -> list[int]:
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
        nxt = parent_map.get(cur)
        if nxt is None:
            break
        cur = int(nxt)
    return out


def parse_dt(s) -> datetime | None:
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


def seconds_to_hhmm(sec: int) -> str:
    h = sec // 3600
    m = (sec % 3600) // 60
    return f"{h} ч {m:02d} мин."


def round_hours(sec: int) -> str:
    """Округляет секунды до часов с точностью 0,5."""
    h = sec / 3600
    rounded = round(h * 2) / 2
    if rounded == int(rounded):
        return f"{int(rounded)} ч."
    return f"{rounded} ч."


# =============================================================================
# HTTP-СЕССИЯ CropWise
# =============================================================================

cw_session = requests.Session()
cw_session.headers.update({"X-User-Api-Token": CROPWISE_TOKEN})


def get_all_from_id(endpoint: str, extra_params: dict | None = None) -> list[dict]:
    params  = dict(extra_params or {})
    results = []
    from_id = 0
    for _ in range(500):
        r = cw_session.get(
            f"{CROPWISE_URL}/{endpoint}",
            params={**params, "from_id": from_id, "limit": 1000},
            timeout=120,
        )
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


# =============================================================================
# ЗАГРУЗКА СПРАВОЧНИКОВ CropWise (из 2_main_ideal.py)
# =============================================================================

def load_field_to_firm(fields_all, fgs_all, gfs_all) -> dict[int, str]:
    fgs_map    = {fg["id"]: fg for fg in fgs_all}
    parent_map = _folder_parent_map(gfs_all)
    root_ids   = set(FIRM_TO_FOLDER.values())
    gf_hints   = _group_folder_name_firm_hints(gfs_all)

    field_to_firm: dict[int, str] = {}
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
        for cid in _folder_id_chain(rint, parent_map):
            if cid in gf_hints:
                firm_key = gf_hints[cid]
                break
        if firm_key is None:
            root = _resolve_to_holding_root(rint, parent_map, root_ids)
            if root is not None:
                firm_key = FOLDER_TO_FIRM.get(root)
        if firm_key:
            field_to_firm[int(fid)] = firm_key

    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        fk = _firm_from_field_name(f)
        if fk:
            field_to_firm[int(fid)] = fk

    return field_to_firm


def _load_available_field_ids_in_period(
    period_start: date,
    period_end:   date,
    ihi_all:      list[dict],
) -> set[int]:
    """Возвращает field_id у которых available=True хотя бы в один день периода."""
    available_ids: set[int] = set()
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
            available_ids.add(int(fid))
    print(f"    Полей available=True в периоде {period_start}\u2013{period_end}: {len(available_ids)}")
    return available_ids


def load_reference_data():
    print("  Загружаем crops...")
    crops_map = {c["id"]: c.get("name") or c.get("title", "")
                 for c in get_all_from_id("crops")}

    _crop_year = REPORT_DATE.year
    print(f"  Загружаем history_items year={_crop_year}...")
    his_all = get_all_from_id("history_items", {"year": _crop_year})
    field_to_crop: dict[int, str] = {}
    for hi in his_all:
        fid     = hi.get("field_id")
        crop_id = hi.get("crop_id")
        active  = hi.get("active")
        if fid and crop_id and crop_id in crops_map and active:
            field_to_crop[fid] = crops_map[crop_id]

    crop_field_ids = set(field_to_crop.keys())
    print(f"    {len(crop_field_ids)} полей с культурой в сезоне {_crop_year}")

    print("  Загружаем fields / field_groups / group_folders...")
    fields_all = get_all_from_id("fields")
    fgs_all    = get_all_from_id("field_groups")
    gfs_all    = get_all_from_id("group_folders")

    field_area: dict[int, float] = {}
    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        field_area[fid] = (f.get("tillable_area") or
                           f.get("calculated_area") or 0.0)

    field_to_firm_all = load_field_to_firm(fields_all, fgs_all, gfs_all)

    # Фильтруем по активности полей в периоде — как в эталоне
    print("  Загружаем inventory_history_items (архивный статус полей)...")
    ihi_all = get_all_from_id("inventory_history_items", {"historyable_type": "Field"})
    print(f"    Всего записей inventory_history_items: {len(ihi_all)}")
    period_available_ids = _load_available_field_ids_in_period(START_DATE, REPORT_DATE, ihi_all)
    field_to_firm = {
        fid: firm for fid, firm in field_to_firm_all.items()
        if fid in crop_field_ids and fid in period_available_ids
    }
    print(f"    {len(field_to_firm)} полей для факта (активны в периоде, с культурой)")

    print("  Загружаем machines...")
    machines_all = get_all_from_id("machines")
    machine_group: dict[int, int] = {
        m["id"]: (m.get("machine_group_id") or 0)
        for m in machines_all
    }
    # Полный словарь машин — нужен для Варианта Б (machine_subtype)
    machines_map: dict[int, dict] = {m["id"]: m for m in machines_all}

    # Машины МТК (транспортные средства, не учитываются в блоке простоев)
    machine_is_mtk: dict[int, bool] = {}
    for m in machines_all:
        name = (m.get("name") or m.get("title") or "").upper()
        machine_is_mtk[m["id"]] = "МТК" in name

    return field_to_crop, field_to_firm, field_area, machine_group, machines_map, machine_is_mtk, crops_map


# =============================================================================
# АСИНХРОННАЯ ЗАГРУЗКА — один клиент и один семафор на весь процесс
# =============================================================================

async def fetch_all_pages_async(
    client:    httpx.AsyncClient,
    url:       str,
    params:    dict,
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


def _shift_window(end_time_raw) -> tuple | None:
    """
    Возвращает (shift_date, shift_start, shift_cutoff) для задания.
    Смена дня D = [D 06:00, D+1 06:00).
    Если end_time < D 06:00 → задание относится к смене D-1.
    """
    end_dt = parse_dt(end_time_raw)
    if end_dt is None:
        return None
    cal_date = end_dt.date()
    anchor   = datetime(cal_date.year, cal_date.month, cal_date.day,
                        6, 0, 0, tzinfo=TZ_MSK)
    if end_dt < anchor:
        cal_date = cal_date - timedelta(days=1)
        anchor   = datetime(cal_date.year, cal_date.month, cal_date.day,
                            6, 0, 0, tzinfo=TZ_MSK)
    return cal_date, anchor, anchor + timedelta(days=1)


async def _collect_one_day_async(
    report_date:   date,
    field_to_crop: dict[int, str],
    field_to_firm: dict[int, str],
    field_area:    dict[int, float],
    machine_group: dict[int, int],
    machines_map:  dict[int, dict],
    machine_is_mtk: dict[int, bool] | None,
    crops_map:     dict[int, str] | None,
) -> dict:
    DAY_START_HOUR = 6
    day_start = datetime(report_date.year, report_date.month, report_date.day,
                         DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK)
    cutoff    = day_start + timedelta(days=1)

    # Сезон с начала года — как в fetch_day_stats_for_work_types
    season_start_str = datetime(report_date.year, 1, 1, tzinfo=TZ_MSK).strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_str       = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    headers   = {"X-User-Api-Token": CROPWISE_TOKEN}
    semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
    pool      = httpx.Limits(max_connections=60, max_keepalive_connections=30)

    url_tasks = f"{CROPWISE_URL}/machine_tasks"
    url_mtfmi = f"{CROPWISE_URL}/machine_task_field_mapping_items"

    result: dict[str, dict] = {}

    def _shift_window(end_time_raw):
        """(shift_date, shift_day_start, shift_cutoff) или None."""
        end_dt = parse_dt(end_time_raw)
        if end_dt is None:
            return None
        cal_date = end_dt.date()
        anchor   = datetime(cal_date.year, cal_date.month, cal_date.day,
                            DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK)
        if end_dt < anchor:
            cal_date = cal_date - timedelta(days=1)
            anchor   = datetime(cal_date.year, cal_date.month, cal_date.day,
                                DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK)
        return cal_date, anchor, anchor + timedelta(days=1)

    async with httpx.AsyncClient(headers=headers, timeout=120, limits=pool) as client:

        # ------------------------------------------------------------------
        # 1. Загружаем все задания за СЕЗОН по всем группам параллельно
        #    (как в fetch_day_stats_for_work_types: season_start → cutoff)
        # ------------------------------------------------------------------
        all_wt_ids = [wt_id for _, wt_ids in WORK_GROUPS for wt_id in wt_ids]

        season_results = await asyncio.gather(*[
            fetch_all_pages_async(client, url_tasks, {
                "work_type_id":   wt_id,
                "end_time_gt_eq": season_start_str,
                "end_time_lt":    cutoff_str,
            }, semaphore)
            for wt_id in all_wt_ids
        ])

        # Раскладываем по группам, дедублируем
        group_tasks_raw: dict[str, list[dict]] = {}
        wt_idx = 0
        for group_name, wt_ids in WORK_GROUPS:
            seen_ids: set[int] = set()
            tasks_raw: list[dict] = []
            for _ in wt_ids:
                for t in season_results[wt_idx]:
                    if t["id"] not in seen_ids:
                        seen_ids.add(t["id"])
                        tasks_raw.append(t)
                wt_idx += 1
            group_tasks_raw[group_name] = tasks_raw

        # ------------------------------------------------------------------
        # 2. Определяем today_task_ids и task_shift для каждой группы
        #    Логика как в fetch_day_stats_for_work_types:
        #      today = shift_ds >= day_start and shift_co <= cutoff
        # ------------------------------------------------------------------
        group_tasks:       dict[str, list[dict]]         = {}
        group_today_ids:   dict[str, set[int]]           = {}
        task_shift_info:   dict[int, tuple]              = {}   # tid → (shift_date, shift_ds, shift_co)
        all_task_ids:      list[int]                     = []
        seen_task_ids:     set[int]                      = set()

        for group_name, tasks_raw in group_tasks_raw.items():
            # Не фильтруем по covered_area: площадь считается из covered_area_hourly,
            # которая может быть > 0 при covered_area == 0 (как в 2_main_ideal_compact).
            today_ids: set[int] = set()
            for t in tasks_raw:
                sw = _shift_window(t.get("end_time"))
                if sw is None:
                    continue
                shift_date, shift_ds, shift_co = sw
                task_shift_info[t["id"]] = sw
                if shift_ds >= day_start and shift_co <= cutoff:
                    today_ids.add(t["id"])
                if t["id"] not in seen_task_ids:
                    seen_task_ids.add(t["id"])
                    all_task_ids.append(t["id"])
            group_tasks[group_name]     = tasks_raw
            group_today_ids[group_name] = today_ids

        # ------------------------------------------------------------------
        # 3. Загружаем mtfmi для всех заданий одним параллельным gather
        # ------------------------------------------------------------------
        if all_task_ids:
            mtfmi_results = await asyncio.gather(*[
                fetch_all_pages_async(client, url_mtfmi,
                                      {"machine_task_id": tid}, semaphore)
                for tid in all_task_ids
            ])
            mtfmi_map: dict[int, list[dict]] = {
                tid: items for tid, items in zip(all_task_ids, mtfmi_results)
            }
        else:
            mtfmi_map = {}

    # ------------------------------------------------------------------
    # 4. Считаем площадь по каждой группе — полностью по аналогии с
    #    fetch_day_stats_for_work_types: сезон + сутки раздельно.
    # ------------------------------------------------------------------
    for group_name, all_tasks in group_tasks.items():
        if not all_tasks:
            continue

        today_task_ids = group_today_ids[group_name]

        # Суточная площадь по полям: covered_area_hourly h < cutoff, нет нижней границы.
        # Fallback на covered_area если hourly пустой (для посева — в отличие от report3).
        today_area_by_field: dict[int, float] = defaultdict(float)
        for tid in today_task_ids:
            sw = task_shift_info.get(tid)
            shift_co = sw[2] if sw else cutoff   # shift_cutoff задания или глобальный как fallback
            for item in mtfmi_map.get(tid, []):
                fid    = item.get("field_id")
                hourly = item.get("covered_area_hourly") or []
                cov    = 0.0
                for entry in hourly:
                    h = parse_dt(entry[0])
                    if h and h < shift_co:
                        cov += entry[1] or 0
                if not hourly:
                    cov = float(item.get("covered_area") or 0.0)
                if fid and cov > 0.5:
                    today_area_by_field[int(fid)] += cov

        # Сезонная и суточная агрегация по фирмам
        season_daily_area:     dict[str, dict[str, float]]    = {f: defaultdict(float) for f in FIRM_ORDER}
        season_daily_machines: dict[str, dict[str, set[int]]] = {f: defaultdict(set)   for f in FIRM_ORDER}
        season_field_cov:      dict[str, dict[int, float]]    = {f: defaultdict(float) for f in FIRM_ORDER}
        machines_own:    defaultdict = defaultdict(set)
        machines_hired:  defaultdict = defaultdict(set)
        machines_sp:     defaultdict = defaultdict(set)   # Вариант Б: SELF_PROPELLED_SUBTYPES
        machines_tr:     defaultdict = defaultdict(set)   # Вариант Б: TRACTOR_SUBTYPES
        machines_no_sub: defaultdict = defaultdict(set)   # Вариант Б: subtype не определён

        for task in all_tasks:
            tid = task["id"]
            mid = task.get("machine_id")
            sw  = task_shift_info.get(tid)
            if sw is None:
                continue
            shift_date, shift_ds, shift_co = sw

            items = mtfmi_map.get(tid, [])

            # Площадь по смене через hourly < shift_co, без нижней границы.
            # Fallback на covered_area если hourly пустой.
            firm_items_map: dict[str, list[tuple]] = defaultdict(list)
            for item in items:
                fid = item.get("field_id")
                if not fid:
                    continue
                fid_int = int(fid)
                firm = field_to_firm.get(fid_int)
                if not firm:
                    continue
                crop = field_to_crop.get(fid_int, "Не определена")
                if is_skip_crop(crop):
                    continue
                hourly = item.get("covered_area_hourly") or []
                cov = 0.0
                for entry in hourly:
                    h = parse_dt(entry[0])
                    if h and h < shift_co:
                        cov += entry[1] or 0
                if not hourly:
                    cov = float(item.get("covered_area") or 0.0)
                if cov > 0.5:
                    firm_items_map[firm].append((item, cov, crop))

            # Вариант Б: subtype берётся один раз на задание — как в delete.py
            m_rec   = machines_map.get(mid, {}) if mid else {}
            m_sub   = m_rec.get("machine_subtype") or "—"
            grp     = machine_group.get(mid, 0) if mid else 0

            for firm, fitems in firm_items_map.items():
                # Сезонный факт — только дни строго до даты отчёта
                if shift_date < report_date:
                    cov_firm = sum(c for _, c, _ in fitems)
                    if cov_firm > 0:
                        day_key = shift_date.isoformat()
                        season_daily_area[firm][day_key] += cov_firm
                        if mid is not None:
                            season_daily_machines[firm][day_key].add(mid)
                    for item, cov_item, _ in fitems:
                        fid_int = int(item.get("field_id"))
                        if cov_item > 0:
                            season_field_cov[firm][fid_int] += cov_item

                # Агрегаты учитываем ТОЛЬКО за день отчёта — как в delete.py (Вариант Б)
                if mid and shift_date == report_date:
                    # Собственные / наёмные (для итога «работало техники всего»)
                    if grp == HIRED_GROUP_ID:
                        machines_hired[firm].add(mid)
                    else:
                        machines_own[firm].add(mid)
                    # Самоходные / тракторы / без подтипа (Вариант Б)
                    if m_sub in SELF_PROPELLED_SUBTYPES:
                        machines_sp[firm].add(mid)
                    elif m_sub in TRACTOR_SUBTYPES:
                        machines_tr[firm].add(mid)
                    else:
                        machines_no_sub[firm].add(mid)

        # Собираем итоговый gd по группе
        gd: dict[str, Any] = {
            "area_by_firm":   defaultdict(float),
            "area_by_crop":   defaultdict(lambda: defaultdict(float)),
            "machines_own":   machines_own,
            "machines_hired": machines_hired,
            "machines_sp":    machines_sp,
            "machines_tr":    machines_tr,
            "machines_no_sub": machines_no_sub,
            # Доп. поля для возможного использования в build_docx
            "season_daily_area":     season_daily_area,
            "season_daily_machines": season_daily_machines,
        }

        # area_by_firm и area_by_crop — из today_area_by_field (суточные)
        for fid, fid_cov in today_area_by_field.items():
            if fid_cov <= 0:
                continue
            firm = field_to_firm.get(fid)
            if not firm:
                continue
            crop = field_to_crop.get(fid, "Не определена")
            if is_skip_crop(crop):
                continue
            gd["area_by_firm"][firm]       += fid_cov
            gd["area_by_crop"][firm][crop] += fid_cov

        # Округляем
        for firm in list(gd["area_by_firm"].keys()):
            gd["area_by_firm"][firm] = round(gd["area_by_firm"][firm])
        for firm in list(gd["area_by_crop"].keys()):
            for crop in list(gd["area_by_crop"][firm].keys()):
                gd["area_by_crop"][firm][crop] = round(gd["area_by_crop"][firm][crop])

        if any(gd["area_by_firm"].values()):
            result[group_name] = gd

    return result


def collect_one_day(
    report_date:    date,
    field_to_crop:  dict[int, str],
    field_to_firm:  dict[int, str],
    field_area:     dict[int, float],
    machine_group:  dict[int, int],
    machines_map:   dict[int, dict],
    machine_is_mtk: dict[int, bool] | None = None,
    crops_map:      dict[int, str] | None = None,
) -> dict:
    return asyncio.run(_collect_one_day_async(
        report_date, field_to_crop, field_to_firm, field_area,
        machine_group, machines_map, machine_is_mtk, crops_map,
    ))

def load_downtime(report_date: date) -> list[dict]:
    """Загружает все простои за report_date из Downtime API.

    Дедуплицируем по id (или композитному ключу), потому что API изредка
    возвращает одну и ту же запись на нескольких страницах подряд — без
    этого защиты при перегенерации сводки итог простоев удваивается.
    """

    def _dedup_key(rec: dict) -> tuple:
        rid = rec.get("id")
        if rid is not None:
            return ("id", rid)
        return (
            "composite",
            rec.get("vehicle_id") or rec.get("vehicle") or "",
            rec.get("fio") or "",
            rec.get("started_at") or rec.get("date") or "",
            rec.get("duration") or 0,
            rec.get("reason") or "",
        )

    dt_str = report_date.isoformat()
    headers = {"X-API-Key": DOWNTIME_API_KEY}
    all_records: list[dict] = []
    seen_keys: set[tuple] = set()
    page = 1
    max_pages = 500
    while page <= max_pages:
        r = requests.get(
            DOWNTIME_URL,
            headers=headers,
            params={"date_from": dt_str, "date_to": dt_str, "page": page, "limit": 200},
            timeout=30,
        )
        r.raise_for_status()
        body = r.json()
        data = body.get("data", []) or []
        if not data:
            break
        new_added = 0
        for rec in data:
            key = _dedup_key(rec)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            all_records.append(rec)
            new_added += 1
        total = int(body.get("total") or 0)
        if total and len(all_records) >= total:
            break
        if new_added == 0:
            break
        page += 1
    return all_records

def aggregate_downtime(records: list[dict]) -> dict:
    """
    Группирует простои по причинам и по агрофирмам.
    Агрофирма берётся напрямую из поля agrofirm, которое теперь возвращает
    Downtime API (заполняется из Vehicle.agrofirm через Celery-задачу
    sync_vehicle_agrofirms). Записи без agrofirm относим к «Техника МТК».

    Возвращает:
    {
      "total_units":   int,
      "total_seconds": int,
      "by_reason":     {reason: {"units": int, "seconds": int}},
      "by_firm":       {firm_name: {
                          "units": int, "seconds": int,
                          "avg_seconds": int,
                          "fio_set": set,
                          "reasons": [str],
                       }},
    }
    """
    # Учитываем только технику агрофирм холдинга — МТК и прочие исключаются.
    records = [r for r in records if r.get("agrofirm") in FIRM_TO_FOLDER]

    total_units   = len(records)
    total_seconds = sum(r.get("duration", 0) for r in records)

    by_reason: dict[str, dict] = defaultdict(lambda: {"units": 0, "seconds": 0})
    for r in records:
        reason = r.get("reason") or "Не указана"
        by_reason[reason]["units"]   += 1
        by_reason[reason]["seconds"] += r.get("duration", 0)

    by_firm: dict[str, dict] = defaultdict(lambda: {
        "units": 0, "seconds": 0,
        "avg_seconds_list": [],
        "fio_set": set(),
        "reasons": [],
    })

    for r in records:
        # После фильтра по белому списку agrofirm гарантированно непустой.
        firm_key = r.get("agrofirm")
        if not firm_key:
            continue
        by_firm[firm_key]["units"]   += 1
        by_firm[firm_key]["seconds"] += r.get("duration", 0)
        by_firm[firm_key]["avg_seconds_list"].append(r.get("duration", 0))
        fio = r.get("fio") or ""
        if fio:
            by_firm[firm_key]["fio_set"].add(fio)
        reason = r.get("reason") or ""
        if reason and reason not in by_firm[firm_key]["reasons"]:
            by_firm[firm_key]["reasons"].append(reason)

    # Вычисляем средние
    for v in by_firm.values():
        lst = v["avg_seconds_list"]
        v["avg_seconds"] = int(sum(lst) / len(lst)) if lst else 0

    return {
        "total_units":   total_units,
        "total_seconds": total_seconds,
        "by_reason":     dict(by_reason),
        "by_firm":       dict(by_firm),
    }


# =============================================================================
# ФОРМИРОВАНИЕ DOCX
# =============================================================================

def fmt_date_ru(d: date) -> str:
    """16 апреля"""
    months = ["января","февраля","марта","апреля","мая","июня",
              "июля","августа","сентября","октября","ноября","декабря"]
    return f"{d.day} {months[d.month - 1]}"


def firms_summary(area_by_firm: dict[str, float]) -> str:
    """АФЧ (1270 га.), АгроРаздолье (2743 га.), ..."""
    parts = []
    for firm in FIRM_ORDER:
        ha = area_by_firm.get(firm, 0)
        if ha > 0:
            parts.append(f"{FIRM_SHORT[firm]} ({int(ha)} га.)")
    return ", ".join(parts)


def add_bold_run(para, text: str):
    run = para.add_run(text)
    run.bold = True
    return run


def add_run(para, text: str):
    return para.add_run(text)


def style_doc(doc: Document) -> None:
    style = doc.styles["Normal"]
    style.font.name = "Arial"
    style.font.size = Pt(10.5)


def style_header_paragraph(para) -> None:
    para.alignment = WD_ALIGN_PARAGRAPH.LEFT
    para.paragraph_format.space_before = Pt(6)
    para.paragraph_format.space_after = Pt(4)
    para.paragraph_format.line_spacing = 1.15


def style_body_paragraph(para) -> None:
    para.alignment = WD_ALIGN_PARAGRAPH.LEFT
    para.paragraph_format.space_before = Pt(0)
    para.paragraph_format.space_after = Pt(2)
    para.paragraph_format.line_spacing = 1.08


# Плановая выработка на агрегат (га/сутки).
PLAN_HA_PER_MACHINE = 250


def _output_color(actual_ha: float, plan_ha: float) -> RGBColor:
    """
    Цвет выработки относительно плана:
      < 80%  — красный
      80–98% — жёлтый
      ≥ 98%  — зелёный
    """
    if plan_ha <= 0:
        return RGBColor(0, 0, 0)
    ratio = actual_ha / plan_ha
    if ratio >= 0.98:
        return RGBColor(0, 150, 0)    # зелёный
    if ratio >= 0.80:
        return RGBColor(200, 150, 0)  # жёлтый
    return RGBColor(200, 0, 0)        # красный


def add_colored_run(para, text: str, color: RGBColor):
    run = para.add_run(text)
    run.font.color.rgb = color
    return run


def build_docx(
    report_date:  date,
    prev_date:    date,
    work_data:    dict,
    work_data_prev: dict,
    downtime_agg: dict,
) -> Document:
    doc = Document()
    style_doc(doc)

    work_order = [g for g, _ in WORK_GROUPS if g in work_data]
    for group_name in work_order:
        gd = work_data[group_name]
        gd_prev = work_data_prev.get(group_name, {})
        label = WORK_LABEL.get(group_name, group_name)
        area_by_firm = gd["area_by_firm"]
        total_ha = sum(area_by_firm.values())
        prev_total = sum(gd_prev.get("area_by_firm", {}).values())
        delta = int(total_ha) - int(prev_total)
        delta_text = f"увеличение на {abs(delta)} га." if delta >= 0 else f"уменьшение на {abs(delta)} га."

        p = doc.add_paragraph()
        style_body_paragraph(p)
        add_run(
            p,
            f"Проводилось {label}, обработано {int(total_ha)} га., а именно:\n"
            f"{firms_summary(area_by_firm)}.",
        )

        p_delta = doc.add_paragraph()
        style_body_paragraph(p_delta)
        add_run(p_delta, f"По сравнению с {fmt_date_ru(prev_date)} {delta_text}")

        if "удобрени" in label.lower():
            area_by_crop_all: dict[str, float] = defaultdict(float)
            for firm in FIRM_ORDER:
                for crop, ha in gd["area_by_crop"].get(firm, {}).items():
                    area_by_crop_all[crop] += ha

            oz_rape_ha = sum(
                v for k, v in area_by_crop_all.items()
                if k.strip().lower() in OZ_RAPE_CROP_NAMES
            )
            if oz_rape_ha > 0:
                p2 = doc.add_paragraph()
                style_body_paragraph(p2)
                add_bold_run(p2, "По рапсу озимому всего обработано:")
                add_run(p2, f" {int(oz_rape_ha)} га.")
                oz_rape_by_firm = {
                    firm: sum(
                        ha for crop, ha in gd["area_by_crop"].get(firm, {}).items()
                        if crop.strip().lower() in OZ_RAPE_CROP_NAMES
                    )
                    for firm in FIRM_ORDER
                }
                p3 = doc.add_paragraph()
                style_body_paragraph(p3)
                add_bold_run(p3, "Рапс озимый по агрофирмам")
                add_run(p3, ": " + firms_summary(oz_rape_by_firm) + ".")

            yr_rape_ha = sum(
                v for k, v in area_by_crop_all.items()
                if k.strip().lower() in YR_RAPE_CROP_NAMES
            )
            if yr_rape_ha > 0:
                p2b = doc.add_paragraph()
                style_body_paragraph(p2b)
                add_bold_run(p2b, "По рапсу яровому всего обработано:")
                add_run(p2b, f" {int(yr_rape_ha)} га.")
                yr_rape_by_firm = {
                    firm: sum(
                        ha for crop, ha in gd["area_by_crop"].get(firm, {}).items()
                        if crop.strip().lower() in YR_RAPE_CROP_NAMES
                    )
                    for firm in FIRM_ORDER
                }
                p3b = doc.add_paragraph()
                style_body_paragraph(p3b)
                add_bold_run(p3b, "Рапс яровой по агрофирмам")
                add_run(p3b, ": " + firms_summary(yr_rape_by_firm) + ".")

            wheat_ha = sum(
                v for k, v in area_by_crop_all.items()
                if "пшениц" in k.lower() and "озим" in k.lower()
            )
            if wheat_ha > 0:
                wheat_by_firm = {
                    firm: sum(
                        ha for crop, ha in gd["area_by_crop"].get(firm, {}).items()
                        if "пшениц" in crop.lower() and "озим" in crop.lower()
                    )
                    for firm in FIRM_ORDER
                }
                p4 = doc.add_paragraph()
                style_body_paragraph(p4)
                add_bold_run(p4, "По пшенице озимой")
                add_run(p4, ": " + firms_summary(wheat_by_firm) + ".")

            grass_ha = sum(
                v for k, v in area_by_crop_all.items()
                if "многолетн" in k.lower() and "трав" in k.lower()
            )
            if grass_ha > 0:
                grass_by_firm = {
                    firm: sum(
                        ha for crop, ha in gd["area_by_crop"].get(firm, {}).items()
                        if "многолетн" in crop.lower() and "трав" in crop.lower()
                    )
                    for firm in FIRM_ORDER
                }
                p5 = doc.add_paragraph()
                style_body_paragraph(p5)
                add_bold_run(p5, "По многолетним травам")
                add_run(p5, ": " + firms_summary(grass_by_firm) + ".")

        own_all = set()
        hired_all = set()
        total_machines = 0
        for firm in FIRM_ORDER:
            own_f = gd["machines_own"].get(firm, set())
            hired_f = gd["machines_hired"].get(firm, set())
            own_all |= own_f
            hired_all |= hired_f
            # Сумма по фирмам совпадает со строками ниже (одна машина может учитываться у нескольких фирм).
            total_machines += len(own_f) + len(hired_f)
        # own_all / hired_all — уникальные id для блока «наёмная техника».

        label_na = WORK_LABEL_AFTER_NA.get(group_name, f"{label}е")
        p_mach = doc.add_paragraph()
        style_header_paragraph(p_mach)
        run_mach = add_bold_run(
            p_mach,
            f"Работало техники всего на {label_na} {total_machines} ед. техники, а именно:",
        )
        run_mach.font.color.rgb = RGBColor(31, 56, 100)

        for firm in FIRM_ORDER:
            own_f   = gd["machines_own"].get(firm, set())
            hired_f = gd["machines_hired"].get(firm, set())
            total_f = len(own_f) + len(hired_f)
            if total_f <= 0:
                continue

            # Выработка на агрегат = суточная площадь / число агрегатов за день
            # (логика «СУТОЧНАЯ ВЫРАБОТКА НА 1 АГРЕГАТ» из 2_main_ideal_compact)
            firm_ha = gd["area_by_firm"].get(firm, 0)
            avg_ha  = round(firm_ha / total_f, 1) if total_f else 0.0
            color   = _output_color(avg_ha, PLAN_HA_PER_MACHINE)

            p_f = doc.add_paragraph()
            style_body_paragraph(p_f)
            add_run(p_f, f"{FIRM_SHORT[firm]} – {total_f} ед., выработка на агрегат: ")
            add_colored_run(p_f, f"{avg_ha} га.", color)

        if hired_all:
            hired_firm_parts = []
            for firm in FIRM_ORDER:
                hf = len(gd["machines_hired"].get(firm, set()))
                if hf > 0:
                    hired_firm_parts.append(f"{FIRM_SHORT[firm]} – {hf}")
            p_hired = doc.add_paragraph()
            style_body_paragraph(p_hired)
            add_run(p_hired, f"В том числе {len(hired_all)} наемников: {', '.join(hired_firm_parts)}.")
        else:
            p_hired = doc.add_paragraph()
            style_body_paragraph(p_hired)
            add_run(p_hired, "Наемная техника: отсутствует.")

        if "удобрени" in label.lower():
            tr_all_group: set = set()
            for firm in FIRM_ORDER:
                tr_all_group |= gd["machines_tr"].get(firm, set())
            tr_count = _r23_fertilizer_transport_machine_count(len(tr_all_group))
            if tr_count > 0:
                p_tr = doc.add_paragraph()
                style_body_paragraph(p_tr)
                add_run(p_tr, f"Данную технику обслуживало {tr_count} машин для подвоза/загрузки удобрений.")

    p = doc.add_paragraph()
    style_header_paragraph(p)
    run_dt = add_bold_run(p, "Информация по агрофирмам и по простоям техники:")
    run_dt.font.color.rgb = RGBColor(31, 56, 100)

    total_u = downtime_agg["total_units"]
    total_s = downtime_agg["total_seconds"]
    p = doc.add_paragraph()
    style_body_paragraph(p)
    add_run(p, f"Суммарно простоев: {total_u} ед. на {seconds_to_hhmm(total_s)}")

    # По причинам
    by_reason = sorted(
        downtime_agg["by_reason"].items(),
        key=lambda x: x[1]["units"],
        reverse=True,
    )
    if by_reason:
        p = doc.add_paragraph()
        style_body_paragraph(p)
        add_run(p, "Причины простоя:")
        for i, (reason, stats) in enumerate(by_reason, 1):
            pr = doc.add_paragraph()
            style_body_paragraph(pr)
            add_run(pr,
                f"{i}) причина «{reason}» количество {stats['units']} ед., "
                f"общее время простоя {round_hours(stats['seconds'])}"
            )

    # По агрофирмам
    by_firm = downtime_agg["by_firm"]
    for firm in FIRM_ORDER:
        firm_short = FIRM_SHORT[firm]
        # Ключи в by_firm — полные названия агрофирм (из Vehicle.agrofirm)
        fd = by_firm.get(firm) or {}
        if not fd:
            continue
        p_firm = doc.add_paragraph()
        style_header_paragraph(p_firm)
        add_bold_run(p_firm, f"{firm_short} -")

        # По каждой группе работ — отдельная строка самоходных/прицепных (Вариант Б)
        for group_name, _ in WORK_GROUPS:
            _gd = work_data.get(group_name)
            if not _gd:
                continue
            sp_set_g = _gd["machines_sp"].get(firm, set())
            tr_set_g = _gd["machines_tr"].get(firm, set())
            total_g  = len(sp_set_g) + len(tr_set_g)
            if total_g == 0:
                continue
            group_label = WORK_LABEL.get(group_name, group_name)
            p_work_g = doc.add_paragraph(
                f"По работам на {group_label}: "
                f"работало – {len(sp_set_g)} агрегатов самоходных и {len(tr_set_g)} прицепных"
            )
            style_body_paragraph(p_work_g)

        p_units = doc.add_paragraph(f"Всего простоев {fd['units']},")
        style_body_paragraph(p_units)
        p_sum = doc.add_paragraph(f"суммарное время простоя - {round_hours(fd['seconds'])},")
        style_body_paragraph(p_sum)
        p_avg = doc.add_paragraph(f"среднее время простоя - {seconds_to_hhmm(fd.get('avg_seconds', 0))},")
        style_body_paragraph(p_avg)
        reasons_str = (", ".join(fd["reasons"]) if fd.get("reasons") else "отсутствует")
        p_reasons = doc.add_paragraph(f"топ причин простоя – {reasons_str}")
        style_body_paragraph(p_reasons)

    return doc


# =============================================================================
# MAIN
# =============================================================================

def main() -> None:
    print(
        f"Штамп отчёта: {REPORT_STAMP} | "
        f"сутки в отчёте: {REPORT_DATE} | день сравнения: {PREV_DATE}"
    )

    from r23_from_report2 import try_load_r23_bundle_for_analytics

    bundled = try_load_r23_bundle_for_analytics(REPORT_DATE, PREV_DATE)
    if bundled is not None:
        work_data, work_data_prev = bundled
        print(
            f"\nДанные по работам из сводки 2 (снимок): сутки {REPORT_DATE}, "
            f"сравнение {PREV_DATE}, групп {len(work_data)}"
        )
    else:
        # 1. Справочники CropWise
        print("\nЗагружаем справочники CropWise...")
        (field_to_crop, field_to_firm,
         field_area, machine_group, machines_map, machine_is_mtk, crops_map) = load_reference_data()

        # 2. Данные о работах за REPORT_DATE
        print(f"\nСобираем данные о работах за {REPORT_DATE}...")
        work_data = collect_one_day(
            REPORT_DATE, field_to_crop, field_to_firm,
            field_area, machine_group, machines_map,
            machine_is_mtk,
            crops_map=crops_map,
        )

        # 3. Данные о работах за PREV_DATE (для дельты)
        print(f"\nСобираем данные о работах за {PREV_DATE} (для сравнения)...")
        work_data_prev = collect_one_day(
            PREV_DATE, field_to_crop, field_to_firm,
            field_area, machine_group, machines_map,
            machine_is_mtk,
            crops_map=crops_map,
        )

    # 4. Простои из Downtime API
    print(f"\nЗагружаем данные о простоях за {REPORT_DATE}...")
    downtime_records = load_downtime(REPORT_DATE)
    print(f"  Получено записей о простоях: {len(downtime_records)}")
    downtime_agg = aggregate_downtime(downtime_records)

    # 5. Формируем docx
    print("\nФормируем отчёт...")
    doc = build_docx(REPORT_DATE, PREV_DATE, work_data, work_data_prev, downtime_agg)

    out_file = f"analytics_{REPORT_DATE}.docx"
    doc.save(out_file)
    print(f"Сохранено: {out_file}")
    print(f"\nГотово: {out_file}")


if __name__ == "__main__":
    main()

# =============================================================================
# ДИАГНОСТИКА — ВАРИАНТ А: через implement_id + /implements
# (орудие = отдельная запись в /implements, наёмность по legal_company)
# =============================================================================

def _diag_impl_load_implements():
    """Загружает справочник /implements и строит вспомогательные словари."""
    OWN_LEGAL_KEYWORDS = [
        "чистопольская", "агрораздолье", "аксу", "нурлат", "дрожжаное",
        "рассвет", "актай", "лениногорск", "птицефабрик",
    ]
    LEGAL_TO_FIRM = [
        ("чистопольская",  "АФ Чистопольская"),
        ("агрораздолье",   "АгроРаздолье"),
        ("аксу",           "Аксу-Агро"),
        ("нурлат",         "НурлатАгро"),
        ("дрожжаное",      "Дрожжаное Рассвет Агро"),
        ("рассвет",        "Дрожжаное Рассвет Агро"),
        ("актай",          "Актай"),
        ("лениногор",      "Лениногорская Птицефабрика"),
        ("птицефабрик",    "Лениногорская Птицефабрика"),
    ]
    IMPL_NAME_TO_FIRM = [
        ("1афч", "АФ Чистопольская"), ("афч", "АФ Чистопольская"),
        ("1дра", "Дрожжаное Рассвет Агро"), (" дра", "Дрожжаное Рассвет Агро"), ("дра", "Дрожжаное Рассвет Агро"),
        ("1ар",  "АгроРаздолье"), (" ар ", "АгроРаздолье"), ("агрораздол", "АгроРаздолье"),
        ("1аа",  "Аксу-Агро"), (" аа", "Аксу-Агро"), ("аксу", "Аксу-Агро"),
        ("1на",  "НурлатАгро"), (" на", "НурлатАгро"), ("нурлат", "НурлатАгро"),
        ("актай", "Актай"),
        ("лениногор", "Лениногорская Птицефабрика"), ("птицефабрик", "Лениногорская Птицефабрика"), ("лпф", "Лениногорская Птицефабрика"),
    ]

    cw_session_local = requests.Session()
    cw_session_local.headers.update({"X-User-Api-Token": CROPWISE_TOKEN})
    implements_all = get_all_from_id("implements")

    implement_name: dict[int, str] = {
        impl["id"]: (impl.get("name") or impl.get("custom_name") or f"impl={impl['id']}")
        for impl in implements_all
    }

    def _is_hired(impl):
        legal = (impl.get("legal_company") or "").lower().strip()
        if not legal:
            return False
        return not any(kw in legal for kw in OWN_LEGAL_KEYWORDS)

    def _impl_firm(impl):
        legal = (impl.get("legal_company") or "").lower().strip()
        for kw, firm in LEGAL_TO_FIRM:
            if kw in legal:
                return firm
        nm = (impl.get("name") or "").lower()
        for kw, firm in IMPL_NAME_TO_FIRM:
            if kw in nm:
                return firm
        addl = (impl.get("additional_info") or "").lower()
        for kw, firm in IMPL_NAME_TO_FIRM:
            if kw in addl:
                return firm
        return None

    implement_is_hired: dict[int, bool]      = {impl["id"]: _is_hired(impl)  for impl in implements_all}
    implement_firm:     dict[int, str | None] = {impl["id"]: _impl_firm(impl) for impl in implements_all}
    return implement_name, implement_is_hired, implement_firm


async def _diag_variant_a(report_date: date, field_to_crop, field_to_firm,
                           machine_group, machine_name_map,
                           implement_name, implement_is_hired):
    """
    ВАРИАНТ А — машина считается как:
      machine_id  → самоходная  (тягач/разбрасыватель/опрыскиватель)
      implement_id → прицепная  (орудие из /implements)
    Наёмность орудия — по legal_company из /implements.
    """
    DAY_START_HOUR = 6
    TZ = TZ_MSK
    day_start = datetime(report_date.year, report_date.month, report_date.day,
                         DAY_START_HOUR, 0, 0, tzinfo=TZ)
    cutoff    = day_start + timedelta(days=1)
    season_start_str = datetime(report_date.year, 1, 1, tzinfo=TZ).strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_str       = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
    pool      = httpx.Limits(max_connections=60, max_keepalive_connections=30)
    headers   = {"X-User-Api-Token": CROPWISE_TOKEN}

    # {firm: {"own_sp": set, "own_tr": set, "hired_sp": set, "hired_tr": set, "hired_no_subtype": set}}
    firm_machines: dict[str, dict[str, set]] = {
        firm: {"own_sp": set(), "own_tr": set(), "hired_sp": set(),
               "hired_tr": set(), "hired_no_subtype": set()}
        for firm in FIRM_ORDER
    }

    async with httpx.AsyncClient(headers=headers, timeout=120, limits=pool) as client:
        all_wt_ids = [wt_id for _, wt_ids in WORK_GROUPS for wt_id in wt_ids]
        season_results = await asyncio.gather(*[
            fetch_all_pages_async(client, f"{CROPWISE_URL}/machine_tasks",
                                  {"work_type_id": wt_id,
                                   "end_time_gt_eq": season_start_str,
                                   "end_time_lt": cutoff_str}, semaphore)
            for wt_id in all_wt_ids
        ])

        seen_ids: set[int] = set()
        today_tasks: list[dict] = []
        for chunk in season_results:
            for t in chunk:
                if t["id"] in seen_ids:
                    continue
                seen_ids.add(t["id"])
                if (t.get("covered_area") or 0) <= 0:
                    continue
                sw = _shift_window(t.get("end_time"), day_start, cutoff)
                if sw and sw[0] == report_date:
                    today_tasks.append(t)

        if not today_tasks:
            print("  [Вариант А] Заданий за дату не найдено.")
            return

        mtfmi_results = await asyncio.gather(*[
            fetch_all_pages_async(client, f"{CROPWISE_URL}/machine_task_field_mapping_items",
                                  {"machine_task_id": t["id"]}, semaphore)
            for t in today_tasks
        ])
        mtfmi_map = {t["id"]: items for t, items in zip(today_tasks, mtfmi_results)}

    def _get_cov(items, shift_cutoff):
        cov = 0.0
        hourly = items.get("covered_area_hourly") or []
        for entry in hourly:
            h = parse_dt(entry[0])
            if h and h < shift_cutoff:
                cov += entry[1] or 0
        if not hourly:
            cov = float(items.get("covered_area") or 0.0)
        return cov

    for task in today_tasks:
        tid     = task["id"]
        mid     = task.get("machine_id")
        impl_id = task.get("implement_id")
        sw      = _shift_window(task.get("end_time"), day_start, cutoff)
        if not sw:
            continue
        _, _, shift_co = sw

        for item in mtfmi_map.get(tid, []):
            fid = item.get("field_id")
            if not fid:
                continue
            fid_int = int(fid)
            firm = field_to_firm.get(fid_int)
            if not firm:
                continue
            crop = field_to_crop.get(fid_int, "")
            if is_skip_crop(crop):
                continue
            if _get_cov(item, shift_co) <= 0.5:
                continue

            # Самоходная (machine_id)
            if mid:
                grp = machine_group.get(mid, 0)
                key = "hired_sp" if grp == HIRED_GROUP_ID else "own_sp"
                firm_machines[firm][key].add(mid)

            # Прицепная (implement_id из /implements)
            if impl_id:
                if implement_is_hired.get(impl_id, False):
                    firm_machines[firm]["hired_tr"].add(impl_id)
                else:
                    firm_machines[firm]["own_tr"].add(impl_id)

    # ── Вывод ─────────────────────────────────────────────────────────────────
    print(f"\n{'='*65}")
    print(f"ВАРИАНТ А — implement_id из /implements  |  дата: {report_date}")
    print(f"{'='*65}")
    print(f"{'Фирма':<8} {'св.сам':>6} {'св.пр':>6} {'нм.сам':>7} {'нм.пр':>6} {'итого':>6}")
    print(f"{'─'*45}")
    grand = [0, 0, 0, 0]
    for firm in FIRM_ORDER:
        fm   = firm_machines[firm]
        own_sp  = len(fm["own_sp"])
        own_tr  = len(fm["own_tr"])
        hir_sp  = len(fm["hired_sp"])
        hir_tr  = len(fm["hired_tr"])
        total   = own_sp + own_tr + hir_sp + hir_tr
        if total == 0:
            continue
        short = FIRM_SHORT[firm]
        print(f"  {short:<6} {own_sp:>6} {own_tr:>6} {hir_sp:>7} {hir_tr:>6} {total:>6}")
        grand[0] += own_sp; grand[1] += own_tr; grand[2] += hir_sp; grand[3] += hir_tr
        # Детали
        for mid in sorted(fm["own_sp"]):
            print(f"    [св.сам]  mid={mid}  '{machine_name_map.get(mid, '?')}'")
        for iid in sorted(fm["own_tr"]):
            print(f"    [св.пр]   impl={iid}  '{implement_name.get(iid, '?')}'")
        for mid in sorted(fm["hired_sp"]):
            print(f"    [нм.сам]  mid={mid}  '{machine_name_map.get(mid, '?')}'")
        for iid in sorted(fm["hired_tr"]):
            print(f"    [нм.пр]   impl={iid}  '{implement_name.get(iid, '?')}'")
    print(f"{'─'*45}")
    print(f"  {'ИТОГО':<6} {grand[0]:>6} {grand[1]:>6} {grand[2]:>7} {grand[3]:>6} {sum(grand):>6}")


# =============================================================================
# ДИАГНОСТИКА — ВАРИАНТ Б: через machine_subtype (самоходные) и machine_id/implement_id
# Самоходные: subtype в SELF_PROPELLED_SUBTYPES
# Прицепные:  subtype в TRACTOR_SUBTYPES  (тягачи из machine_tasks)
# Наёмники без subtype — отдельная категория
# =============================================================================


async def _diag_variant_b(report_date: date, field_to_crop, field_to_firm,
                           machine_group, machine_name_map, machines_map: dict):
    """
    ВАРИАНТ Б — тип агрегата определяется по machine_subtype:
      SELF_PROPELLED_SUBTYPES → самоходный
      TRACTOR_SUBTYPES        → трактор (прицепной агрегат = тягач + орудие)
      None / другой           → «без подтипа» (наёмники часто без подтипа)
    """
    DAY_START_HOUR = 6
    TZ = TZ_MSK
    day_start = datetime(report_date.year, report_date.month, report_date.day,
                         DAY_START_HOUR, 0, 0, tzinfo=TZ)
    cutoff    = day_start + timedelta(days=1)
    season_start_str = datetime(report_date.year, 1, 1, tzinfo=TZ).strftime("%Y-%m-%dT%H:%M:%S")
    cutoff_str       = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    semaphore = asyncio.Semaphore(SEMAPHORE_LIMIT)
    pool      = httpx.Limits(max_connections=60, max_keepalive_connections=30)
    headers   = {"X-User-Api-Token": CROPWISE_TOKEN}

    # {firm: {"self_propelled": set, "tractor": set, "no_subtype": set}}
    firm_machines: dict[str, dict[str, set]] = {
        firm: {"self_propelled": set(), "tractor": set(), "no_subtype": set()}
        for firm in FIRM_ORDER
    }

    async with httpx.AsyncClient(headers=headers, timeout=120, limits=pool) as client:
        all_wt_ids = [wt_id for _, wt_ids in WORK_GROUPS for wt_id in wt_ids]
        season_results = await asyncio.gather(*[
            fetch_all_pages_async(client, f"{CROPWISE_URL}/machine_tasks",
                                  {"work_type_id": wt_id,
                                   "end_time_gt_eq": season_start_str,
                                   "end_time_lt": cutoff_str}, semaphore)
            for wt_id in all_wt_ids
        ])

        seen_ids: set[int] = set()
        today_tasks: list[dict] = []
        for chunk in season_results:
            for t in chunk:
                if t["id"] in seen_ids:
                    continue
                seen_ids.add(t["id"])
                if (t.get("covered_area") or 0) <= 0:
                    continue
                sw = _shift_window(t.get("end_time"), day_start, cutoff)
                if sw and sw[0] == report_date:
                    today_tasks.append(t)

        if not today_tasks:
            print("  [Вариант Б] Заданий за дату не найдено.")
            return

        mtfmi_results = await asyncio.gather(*[
            fetch_all_pages_async(client, f"{CROPWISE_URL}/machine_task_field_mapping_items",
                                  {"machine_task_id": t["id"]}, semaphore)
            for t in today_tasks
        ])
        mtfmi_map = {t["id"]: items for t, items in zip(today_tasks, mtfmi_results)}

    def _get_cov(items, shift_cutoff):
        cov = 0.0
        hourly = items.get("covered_area_hourly") or []
        for entry in hourly:
            h = parse_dt(entry[0])
            if h and h < shift_cutoff:
                cov += entry[1] or 0
        if not hourly:
            cov = float(items.get("covered_area") or 0.0)
        return cov

    for task in today_tasks:
        tid = task["id"]
        mid = task.get("machine_id")
        if not mid:
            continue
        sw = _shift_window(task.get("end_time"), day_start, cutoff)
        if not sw:
            continue
        _, _, shift_co = sw

        m       = machines_map.get(mid, {})
        subtype = m.get("machine_subtype") or None

        for item in mtfmi_map.get(tid, []):
            fid = item.get("field_id")
            if not fid:
                continue
            fid_int = int(fid)
            firm = field_to_firm.get(fid_int)
            if not firm:
                continue
            crop = field_to_crop.get(fid_int, "")
            if is_skip_crop(crop):
                continue
            if _get_cov(item, shift_co) <= 0.5:
                continue

            if subtype in SELF_PROPELLED_SUBTYPES:
                firm_machines[firm]["self_propelled"].add(mid)
            elif subtype in TRACTOR_SUBTYPES:
                firm_machines[firm]["tractor"].add(mid)
            else:
                firm_machines[firm]["no_subtype"].add(mid)

    # ── Вывод ─────────────────────────────────────────────────────────────────
    print(f"\n{'='*65}")
    print(f"ВАРИАНТ Б — по machine_subtype  |  дата: {report_date}")
    print(f"Самоходные: {sorted(SELF_PROPELLED_SUBTYPES)}")
    print(f"Тракторы:   {sorted(TRACTOR_SUBTYPES)}")
    print(f"{'='*65}")
    print(f"{'Фирма':<8} {'самоход':>8} {'трактор':>8} {'без_типа':>9} {'итого':>6}")
    print(f"{'─'*45}")
    grand = [0, 0, 0]
    for firm in FIRM_ORDER:
        fm   = firm_machines[firm]
        sp   = len(fm["self_propelled"])
        tr   = len(fm["tractor"])
        no   = len(fm["no_subtype"])
        total = sp + tr + no
        if total == 0:
            continue
        short = FIRM_SHORT[firm]
        print(f"  {short:<6} {sp:>8} {tr:>8} {no:>9} {total:>6}")
        grand[0] += sp; grand[1] += tr; grand[2] += no
        # Детали
        for mid in sorted(fm["self_propelled"]):
            st = machines_map.get(mid, {}).get("machine_subtype", "?")
            print(f"    [самоход]   mid={mid}  subtype={st}  '{machine_name_map.get(mid, '?')}'")
        for mid in sorted(fm["tractor"]):
            st = machines_map.get(mid, {}).get("machine_subtype", "?")
            print(f"    [трактор]   mid={mid}  subtype={st}  '{machine_name_map.get(mid, '?')}'")
        for mid in sorted(fm["no_subtype"]):
            st = machines_map.get(mid, {}).get("machine_subtype") or "—"
            print(f"    [без_типа]  mid={mid}  subtype={st}  '{machine_name_map.get(mid, '?')}'")
    print(f"{'─'*45}")
    print(f"  {'ИТОГО':<6} {grand[0]:>8} {grand[1]:>8} {grand[2]:>9} {sum(grand):>6}")


# =============================================================================
# ТОЧКА ВХОДА ДЛЯ ДИАГНОСТИКИ
# Запуск: python ranis_svodka_diag.py --diag [YYYY-MM-DD]
# =============================================================================

def _shift_window(end_time_raw, day_start, cutoff):
    dt = parse_dt(end_time_raw)
    if dt is None:
        return None
    cal_date = dt.date()
    anchor   = datetime(cal_date.year, cal_date.month, cal_date.day,
                        6, 0, 0, tzinfo=TZ_MSK)
    if dt < anchor:
        cal_date = cal_date - timedelta(days=1)
        anchor   = datetime(cal_date.year, cal_date.month, cal_date.day,
                            6, 0, 0, tzinfo=TZ_MSK)
    shift_co = anchor + timedelta(days=1)
    return cal_date, anchor, shift_co


def run_diagnostics():
    report_date, prev_d, stamp = _resolve_analytics_dates()
    print(
        f"=== ДИАГНОСТИКА АГРЕГАТОВ (штамп {stamp}, сутки {report_date}, сравн. {prev_d}) ===\n"
    )
    print("Загружаем справочники...")
    field_to_crop, field_to_firm, field_area, machine_group, machines_map, machine_is_mtk, crops_map = load_reference_data()

    # Дополнительно: машины уже загружены в machines_map
    machine_name_map = {mid: (m.get("name") or m.get("title") or f"id={mid}")
                        for mid, m in machines_map.items()}

    # Вариант А: implements
    print("\nЗагружаем /implements для варианта А...")
    implement_name, implement_is_hired, implement_firm = _diag_impl_load_implements()

    # Запускаем оба варианта
    async def _run_both():
        await _diag_variant_a(report_date, field_to_crop, field_to_firm,
                               machine_group, machine_name_map,
                               implement_name, implement_is_hired)
        await _diag_variant_b(report_date, field_to_crop, field_to_firm,
                               machine_group, machine_name_map, machines_map)

    asyncio.run(_run_both())
    print("\nДиагностика завершена.")


if __name__ == "__main__":
    if "--diag" in sys.argv:
        run_diagnostics()
    else:
        main()