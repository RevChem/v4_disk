from __future__ import annotations
from plans import (
    API_TOKEN,
    BASE_URL,
    TZ_MSK,
    HIRED_GROUP_ID,
    AGROFIRMS_META,
    WORK_GROUPS,
    AVAILABLE_OWN,
    FIELD_NAME_FORCE_RULES,
    FIRM_TO_FOLDER,
    FOLDER_TO_FIRM,
    SKIP_CROPS,
)

"""
Cropwise — отчёт "Сводка + Остановки на полях"
===============================================
Лист 1 "Сводка"      — по агрофирмам: техника, площадь, выработка
Лист 2 "Остановки"   — машины с остановкой на поле > STOP_THRESHOLD_MIN минут

Логика:
- Агрофирма определяется по ПОЛЮ из mtfmi через иерархию папок (как в эталоне).
- Отчёт строится за "смену": с 06:00 текущего дня до 06:00 следующего дня.
- Площадь до среза report_dt: covered_area_hourly с h < cutoff (как сводки 1–3),
  fallback на covered_area; порог 0.5 га на mtfmi-запись — для всех групп работ,
  КРОМЕ групп из NO_MIN_AREA_GROUPS (Вспашка, Гребнеобразование, Глубокорыхление,
  Посев (овощи), Уборка, Уборка картофеля/лука), где порога нет — считается
  любая площадь > 0.
- Виды работ объединены в группы WORK_GROUPS; каждая группа — отдельный блок
  в сводке.
"""


import asyncio
import json
import math
import os
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
import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.properties import PageSetupProperties
from openpyxl.worksheet.pagebreak import Break
import requests


REPORT_DATE = date(2026, 5, 25)
if os.environ.get("REPORT_DATE"):
    REPORT_DATE = date.fromisoformat(os.environ["REPORT_DATE"])

REPORT_HOUR   = int(os.environ.get("REPORT_HOUR", 18))
REPORT_MINUTE = int(os.environ.get("REPORT_MINUTE", 0))

# Агрофирмы: (folder_id, region_id) — берём из AGROFIRMS_META
AGROFIRMS: dict[str, tuple[int, int]] = {
    firm: (ids[0], ids[1]) for firm, ids in AGROFIRMS_META.items()
}

DISPLAY_FIRM_NAMES: dict[str, str] = {
    "АФ Чистопольская":           "АФ Чистопольская",
    "АгроРаздолье":               "Агро Раздолье",
    "Аксу-Агро":                  "Аксу Агро",
    "НурлатАгро":                 "Нурлат Агро",
    "Дрожжаное Рассвет Агро":     "Дрожжаное Рассвет Агро",
    "Актай":                      "Актай",
    "Лениногорская Птицефабрика": "Лениногорская птицефабрика",
}

# Плоский словарь wt_id → название группы (для быстрого поиска)
WT_TO_GROUP: dict[int, str] = {
    wt_id: group_name
    for group_name, wt_ids in WORK_GROUPS
    for wt_id in wt_ids
}

# Все уникальные wt_id (все группы работ)
ALL_WT_IDS: list[int] = list({wt_id for _, wt_ids in WORK_GROUPS for wt_id in wt_ids})

STOP_THRESHOLD_MIN = int(os.environ.get("STOP_THRESHOLD_MIN", 30))

DAY_START_HOUR  = int(os.environ.get("DAY_START_HOUR", "6"))
DAY_START_MINUTE = int(os.environ.get("DAY_START_MINUTE", "0"))
REPORT_WINDOW_START_RAW = os.environ.get("REPORT_WINDOW_START", "").strip()
REPORT_WINDOW_END_RAW = os.environ.get("REPORT_WINDOW_END", "").strip()
SEMAPHORE_LIMIT = int(os.environ.get("SVODKI_REPORT22_HTTP_CONCURRENCY", "15"))

_PREFETCH_ENDPOINTS: dict[str, list[dict]] | None = None
_PREFETCH_LOAD_DONE = False

# Количество собственных агрегатов — из plans (AVAILABLE_OWN импортирован выше)
# Принудительные правила по имени поля — из plans (FIELD_NAME_FORCE_RULES импортирован выше)


session = requests.Session()
session.headers.update({"X-User-Api-Token": API_TOKEN})


MIN_ITEM_AREA_HA = 0.5

# Группы работ, на которые порог MIN_ITEM_AREA_HA НЕ распространяется —
# учитывается любая площадь > 0, без отсева мелких записей.
NO_MIN_AREA_GROUPS: set[str] = {
    "2.4 Вспашка",
    "2.7 Гребнеобразование",
    "2.8 Глубокорыхление",
    "4.1 Посев (овощи)",
    "5.0 Уборка",
    "5.4 Уборка картофеля, лука",
}
SVODKA22_SCRIPT_REV = "text_svodka_2 (10)+window_fix2"

# Фильтр полей по группам — как сводки 1–2.
VEG_CROPS = frozenset({"Картофель", "Морковь", "Свекла столовая"})
SPRAY_GROUP = "3.1 Опрыскивание"
VEG_SPRAY_GROUP = "3.2 Опрыскивание (овощи)"
VEG_PLANT_GROUP = "4.1 Посев (овощи)"
VEG_HARVEST_GROUP = "5.4 Уборка картофеля, лука"

# Отображаемое имя в тексте сводки 22 (ключ WORK_GROUPS / plans не меняем).
GROUP_DISPLAY_NAMES: dict[str, str] = {
    "5.4 Уборка картофеля, лука": "Уборка овощей",
}


def _group_display_name(group_name: str) -> str:
    mapped = GROUP_DISPLAY_NAMES.get(group_name)
    if mapped:
        return mapped
    return group_name.split(" ", 1)[1] if " " in group_name else group_name

GRAIN_HARVEST_GROUP = "5.0 Уборка"
TILLAGE_GROUPS = frozenset({
    "2.4 Вспашка",
    "2.7 Гребнеобразование",
    "2.8 Глубокорыхление",
})


def _shift_window(end_time_raw) -> tuple[date, datetime, datetime] | None:
    """Смена дня D = [D 06:00, D+1 06:00) — как в 1_main_ammonium / 2_main_ideal_compact."""
    end_dt = parse_dt(end_time_raw)
    if end_dt is None:
        return None
    cal_date = end_dt.date()
    anchor = datetime(
        cal_date.year, cal_date.month, cal_date.day,
        DAY_START_HOUR, DAY_START_MINUTE, 0, tzinfo=TZ_MSK,
    )
    if end_dt < anchor:
        cal_date = cal_date - timedelta(days=1)
        anchor = datetime(
            cal_date.year, cal_date.month, cal_date.day,
            DAY_START_HOUR, DAY_START_MINUTE, 0, tzinfo=TZ_MSK,
        )
    return cal_date, anchor, anchor + timedelta(days=1)


def _report_window_bounds() -> tuple[datetime | None, datetime | None]:
    return _parse_report_window(REPORT_WINDOW_START_RAW), _parse_report_window(REPORT_WINDOW_END_RAW)


def _report_shift_date_span() -> tuple[date, date]:
    """Календарные даты смен в окне отчёта (или одна REPORT_DATE)."""
    ws, we = _report_window_bounds()
    if ws is not None and we is not None and we > ws:
        d0 = ws.date()
        end_inclusive = we - timedelta(seconds=1)
        d1 = max(d0, end_inclusive.date())
        return d0, d1
    return REPORT_DATE, REPORT_DATE


def _task_fetch_end(day_start: datetime, report_dt: datetime) -> datetime:
    """Конец смены для загрузки machine_tasks (площадь режется отдельно по report_dt)."""
    _, d1 = _report_shift_date_span()
    shift_open = datetime(
        d1.year, d1.month, d1.day,
        DAY_START_HOUR, DAY_START_MINUTE, 0, tzinfo=TZ_MSK,
    )
    return shift_open + timedelta(days=1)


def _task_belongs_to_report_shift(task: dict) -> bool:
    sw = _shift_window(task.get("end_time"))
    d0, d1 = _report_shift_date_span()
    if sw is not None:
        return d0 <= sw[0] <= d1
    if str(task.get("status") or "").lower() == "in_progress":
        start = parse_dt(task.get("start_time"))
        ws, we = _report_window_bounds()
        if ws is not None and we is not None:
            return start is not None and ws <= start < we
        return start is not None and start.date() >= d0
    return False


def _cov_item_before_cutoff(item: dict, cutoff: datetime) -> float:
    """
    Площадь mtfmi-записи до cutoff — как fetch_day_stats в 1_main_ammonium:
    covered_area_hourly: h < cutoff; если hourly пустой — covered_area.
    """
    hourly = item.get("covered_area_hourly") or []
    cov = 0.0
    if hourly:
        for entry in hourly:
            h = parse_dt(entry[0])
            if h and h < cutoff:
                cov += float(entry[1] or 0.0)
    else:
        cov = float(item.get("covered_area") or 0.0)
    return cov


def calc_area_hourly(
    items: list[dict],
    _start: datetime,
    end: datetime,
    min_item_area: float = MIN_ITEM_AREA_HA,
) -> float:
    """
    Сумма по items с порогом min_item_area на запись (как fetch_day_stats в сводке 1).
    Для групп работ из NO_MIN_AREA_GROUPS вызывающий код передаёт min_item_area=0.0 —
    тогда учитывается любая площадь > 0, без отсева мелких записей.
    """
    total = 0.0
    for item in items:
        cov = _cov_item_before_cutoff(item, end)
        if cov > min_item_area:
            total += cov
    return total


def get_all_from_id(endpoint: str, extra_params: dict | None = None) -> list[dict]:
    params  = dict(extra_params or {})
    results = []
    from_id = 0
    while True:
        r = session.get(f"{BASE_URL}/{endpoint}",
                        params={**params, "from_id": from_id, "limit": 1000},
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


# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ
# =============================================================================

def seconds_to_hms(seconds: float) -> str:
    total = int(seconds)
    h     = total // 3600
    m     = (total % 3600) // 60
    s     = total % 60
    return f"{h}:{m:02d}:{s:02d}"


def _border() -> Border:
    t = Side(style="thin")
    return Border(left=t, right=t, top=t, bottom=t)


def _cell(ws, row: int, col: int, value=None,
          bold=False, color="000000", bg=None,
          halign="center", wrap=True, border=True):
    c = ws.cell(row=row, column=col, value=value)
    c.font      = Font(name="Times New Roman", bold=bold, color=color, size=11)
    c.alignment = Alignment(horizontal=halign, vertical="center", wrap_text=wrap)
    if bg:
        c.fill = PatternFill("solid", fgColor=bg)
    if border:
        c.border = _border()
    return c


def _fit_table_row_height(ws, row: int, first_col: int, last_col: int, *, min_height: float = 16.0) -> None:
    """Raise table row height when wrapped text needs more than one visual line."""
    required = float(min_height)
    for col in range(first_col, last_col + 1):
        cell = ws.cell(row=row, column=col)
        value = cell.value
        if value in (None, ""):
            continue
        text = str(value)
        if "\n" not in text and not bool(getattr(cell.alignment, "wrap_text", False)):
            continue

        col_letter = get_column_letter(col)
        width = float(ws.column_dimensions[col_letter].width or 10)
        chars_per_line = max(1, int(width * 1.08))
        lines = 0
        for part in text.splitlines() or [text]:
            lines += max(1, math.ceil(len(part.strip()) / chars_per_line))
        if lines > 1:
            font_size = float(cell.font.sz or 11)
            required = max(required, min(60.0, lines * font_size * 1.25 + 5))
    ws.row_dimensions[row].height = required


def _fmt_plain_num(v: float | int) -> str:
    """Формат числа для текстовой (не табличной) печати."""
    try:
        fv = float(v)
    except Exception:
        return str(v)
    if abs(fv - round(fv)) < 1e-9:
        return str(int(round(fv)))
    return f"{fv:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def _plain_line(ws, row: int, text: str, *, bold: bool = False, size: int = 10) -> int:
    """Одна строка текстового макета (как в эталонном PDF)."""
    c = ws.cell(row=row, column=1, value=text)
    c.font = Font(name="Times New Roman", bold=bold, size=size)
    c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    ws.row_dimensions[row].height = 12
    return row + 1


def _plain_metric_row(
    ws,
    row: int,
    label: str,
    value: str,
    hired_label: str = "",
    hired_value: str = "",
) -> int:
    """Строка показателя в текстовом стиле из референсного PDF."""
    c_label = ws.cell(row=row, column=1, value=label)
    c_label.font = Font(name="Times New Roman", size=10, italic=True)
    c_label.alignment = Alignment(horizontal="left", vertical="center")

    c_val = ws.cell(row=row, column=2, value=value)
    c_val.font = Font(name="Times New Roman", size=10, bold=True)
    c_val.alignment = Alignment(horizontal="left", vertical="center")

    if hired_label:
        c_hl = ws.cell(row=row, column=3, value=hired_label)
        c_hl.font = Font(name="Times New Roman", size=10, italic=True)
        c_hl.alignment = Alignment(horizontal="left", vertical="center")
    if hired_value:
        c_hv = ws.cell(row=row, column=4, value=hired_value)
        c_hv.font = Font(name="Times New Roman", size=10, bold=True)
        c_hv.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[row].height = 12
    return row + 1


def _try_insert_logo(ws, row: int = 1, col: str = "A") -> None:
    """Добавляет логотип в верхней части листа, если файл найден."""
    try:
        from openpyxl.drawing.image import Image as XLImage
    except Exception:
        return

    candidates = [
        Path(__file__).resolve().parents[2] / "frontend/src/assets/logo.png",
        Path(__file__).resolve().parents[2] / "dop/frontend/logo.png",
    ]
    logo_path = next((p for p in candidates if p.exists()), None)
    if logo_path is None:
        return
    try:
        img = XLImage(str(logo_path))
        img.height = 36
        img.width = 36
        ws.add_image(img, f"{col}{row}")
    except Exception:
        return


# =============================================================================
# ИЕРАРХИЯ ПОЛЕЙ → АГРОФИРМА
# =============================================================================

machines_map:   dict[int, dict] = {}
_field_to_firm: dict[int, str]  = {}
field_to_crop: dict[int, str] = {}
available_field_ids: set[int] = set()
crop_map: dict[int, str] = {}


def _norm_field_label(name: str) -> str:
    return (
        (name or "")
        .strip()
        .casefold()
        .replace(" ", "")
        .replace("\u00a0", "")
    )


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


def _prefetch_endpoints() -> dict[str, list[dict]] | None:
    global _PREFETCH_ENDPOINTS, _PREFETCH_LOAD_DONE
    if _PREFETCH_LOAD_DONE:
        return _PREFETCH_ENDPOINTS
    _PREFETCH_LOAD_DONE = True
    raw = os.environ.get("SVODKI_PREFETCH_DATA_PATH", "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        endpoints = data.get("endpoints")
        if isinstance(endpoints, dict):
            _PREFETCH_ENDPOINTS = endpoints
            profile = str(data.get("prefetch_profile") or "").strip()
            print(
                f"  Prefetch JSON: {path.name}"
                + (f" ({profile})" if profile else ""),
                flush=True,
            )
    except Exception as exc:
        print(f"  Prefetch JSON не прочитан ({exc}), живой API", flush=True)
    return _PREFETCH_ENDPOINTS


def _load_field_area_filters() -> None:
    """Поля с культурой и available=True — как fetch_day_stats в 1_main_ammonium."""
    global field_to_crop, available_field_ids, crop_map

    field_to_crop = {}
    available_field_ids = set()
    crop_map = {
        c["id"]: (c.get("name") or c.get("title") or "")
        for c in get_all_from_id("crops")
    }

    season_year = REPORT_DATE.year
    for hi in get_all_from_id("history_items", {"year": season_year}):
        fid = hi.get("field_id")
        crop_id = hi.get("crop_id")
        if not (fid and crop_id and crop_id in crop_map and hi.get("active")):
            continue
        crop_name = crop_map[crop_id]
        if crop_name in SKIP_CROPS:
            continue
        field_to_crop[int(fid)] = crop_name

    period_start = date(REPORT_DATE.year, 3, 1)
    period_end = REPORT_DATE
    for row in get_all_from_id("inventory_history_items", {"historyable_type": "Field"}):
        if not row.get("available"):
            continue
        fid_r = row.get("historyable_id")
        if fid_r is None:
            continue
        try:
            ev_start = date.fromisoformat(str(row.get("event_start_at"))[:10]) if row.get("event_start_at") else None
        except ValueError:
            ev_start = None
        try:
            ev_end = date.fromisoformat(str(row.get("event_end_at"))[:10]) if row.get("event_end_at") else None
        except ValueError:
            ev_end = None
        if ev_start is not None and ev_start > period_end:
            continue
        if ev_end is not None and ev_end < period_start:
            continue
        available_field_ids.add(int(fid_r))

    print(
        f"    Фильтр полей: {len(field_to_crop)} с культурой, "
        f"{len(available_field_ids)} available в периоде",
        flush=True,
    )


def _item_eligible_for_area(item: dict, group_name: str) -> bool:
    """Допуск mtfmi-записи в площадь — с учётом группы работ (как 1_main / 2_main)."""
    fid = item.get("field_id")
    if not fid:
        return False
    fid_i = int(fid)
    crop = field_to_crop.get(fid_i)

    if group_name in TILLAGE_GROUPS:
        return bool(field_to_firm_fn(fid_i))

    if group_name in {VEG_PLANT_GROUP, VEG_HARVEST_GROUP, VEG_SPRAY_GROUP}:
        return bool(crop and crop in VEG_CROPS)

    if fid_i not in available_field_ids:
        return False
    if crop is None:
        return False
    if group_name == SPRAY_GROUP and crop in VEG_CROPS:
        return False
    if group_name == GRAIN_HARVEST_GROUP and crop in VEG_CROPS:
        return False
    return True


def load_reference_data() -> None:
    global machines_map, _field_to_firm

    endpoints = _prefetch_endpoints()
    if endpoints is not None:
        print("  Справочники из prefetch JSON...")
        machines_map = {m["id"]: m for m in (endpoints.get("machines") or [])}
        fields_all = endpoints.get("fields") or []
        fgs_all = endpoints.get("field_groups") or []
        gfs_all = endpoints.get("group_folders") or []
        print(f"    {len(machines_map)} машин")
    else:
        print("  Загружаем machines...")
        machines_map = {m["id"]: m for m in get_all_from_id("machines")}
        print(f"    {len(machines_map)} машин")

        print("  Загружаем fields / field_groups / group_folders...")
        fields_all = get_all_from_id("fields")
        fgs_all    = get_all_from_id("field_groups")
        gfs_all    = get_all_from_id("group_folders")

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

    forced = 0
    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        fk = _firm_from_field_name(f)
        if fk:
            field_to_firm[int(fid)] = fk
            forced += 1

    _field_to_firm = field_to_firm
    print(
        f"    {len(fields_all)} полей, {len(fgs_all)} групп, "
        f"{len(field_to_firm)} с привязкой (принудительно: {forced})"
    )
    _load_field_area_filters()


def field_to_firm_fn(fid: int) -> str:
    return _field_to_firm.get(fid, "")


# =============================================================================
# АСИНХРОННАЯ ЗАГРУЗКА ДАННЫХ
# =============================================================================

async def fetch_pages(client: httpx.AsyncClient, url: str,
                      params: dict, sem: asyncio.Semaphore) -> list[dict]:
    items, from_id = [], 0
    while True:
        async with sem:
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


async def fetch_mtfmi(client: httpx.AsyncClient, task_id: int,
                      sem: asyncio.Semaphore) -> tuple[int, list[dict]]:
    items = await fetch_pages(client, f"{BASE_URL}/machine_task_field_mapping_items",
                              {"machine_task_id": task_id}, sem)
    return task_id, items


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


def _parse_report_window(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=TZ_MSK)
        except ValueError:
            continue
    return parse_dt(text)


def _collect_shift_machine_tasks(
    rows: list[dict],
    day_start: datetime,
    shift_end: datetime,
) -> list[dict]:
    wt_ok = set(ALL_WT_IDS)
    seen: set[int] = set()
    out: list[dict] = []
    for t in rows:
        try:
            tid = int(t["id"])
        except Exception:
            continue
        if tid in seen:
            continue
        try:
            wt_i = int(t.get("work_type_id"))
        except Exception:
            continue
        if wt_i not in wt_ok:
            continue
        if not _task_belongs_to_report_shift(t):
            continue
        end = parse_dt(t.get("end_time") or t.get("finished_at") or t.get("updated_at"))
        if end is not None and day_start <= end < shift_end:
            seen.add(tid)
            out.append(t)
            continue
        if str(t.get("status") or "").lower() == "in_progress":
            start = parse_dt(t.get("start_time"))
            if start is not None and start >= day_start:
                seen.add(tid)
                out.append(t)
    return out


def _mtfmi_map_for_tasks(rows: list[dict], task_ids: set[int]) -> dict[int, list[dict]]:
    mtfmi_map: dict[int, list[dict]] = {}
    for row in rows:
        try:
            tid = int(row.get("machine_task_id"))
        except Exception:
            continue
        if tid not in task_ids:
            continue
        mtfmi_map.setdefault(tid, []).append(row)
    return mtfmi_map


async def fetch_all_data(
    client:    httpx.AsyncClient,
    sem:       asyncio.Semaphore,
    day_start: datetime,
    report_dt: datetime,
) -> tuple[dict, dict]:

    # Задачи — по окну REPORT_WINDOW или одна смена; срез report_dt режет hourly/остановки.
    shift_end = _task_fetch_end(day_start, report_dt)
    endpoints = _prefetch_endpoints()
    if endpoints is not None:
        print("    machine_tasks + mtfmi из prefetch JSON (без повторных HTTP)...", flush=True)
        all_tasks = _collect_shift_machine_tasks(
            endpoints.get("machine_tasks") or [],
            day_start,
            shift_end,
        )
        task_ids = {int(t["id"]) for t in all_tasks}
        mtfmi_map = _mtfmi_map_for_tasks(
            endpoints.get("machine_task_field_mapping_items") or [],
            task_ids,
        )
    else:
        url           = f"{BASE_URL}/machine_tasks"
        day_start_str = day_start.strftime("%Y-%m-%dT%H:%M:%S")
        shift_end_str = shift_end.strftime("%Y-%m-%dT%H:%M:%S")

        print("    Загружаем machine_tasks за смену (все группы работ)...")

        done_chunks = await asyncio.gather(*[
            fetch_pages(client, url, {
                "work_type_id":   wt_id,
                "end_time_gt_eq": day_start_str,
                "end_time_lt":    shift_end_str,
            }, sem)
            for wt_id in ALL_WT_IDS
        ])

        open_chunks = await asyncio.gather(*[
            fetch_pages(client, url, {
                "work_type_id":     wt_id,
                "start_time_gt_eq": day_start_str,
                "status":           "in_progress",
            }, sem)
            for wt_id in ALL_WT_IDS
        ])

        seen: set[int] = set()
        all_tasks = []
        for chunk in list(done_chunks) + list(open_chunks):
            for t in chunk:
                if t["id"] in seen:
                    continue
                if not _task_belongs_to_report_shift(t):
                    continue
                seen.add(t["id"])
                all_tasks.append(t)

        print("    Загружаем mtfmi...")
        mtfmi_results = await asyncio.gather(*[
            fetch_mtfmi(client, t["id"], sem) for t in all_tasks
        ])
        mtfmi_map = {tid: items for tid, items in mtfmi_results}

    print(f"    Всего заданий (без дублей): {len(all_tasks)}")

    # firm → machine_id → данные
    # Данные по машине хранятся отдельно для каждой группы работ
    # firm_machines[firm][mid] = { group_name → {area, n_working, n_hired}, ... }
    firm_machines: dict[str, dict[int, dict]] = {f: {} for f in AGROFIRMS}

    STOP_THRESHOLD_SEC = STOP_THRESHOLD_MIN * 60

    for t in all_tasks:
        tid   = t["id"]
        mid   = t.get("machine_id")
        if not mid:
            continue
        wt_id = t.get("work_type_id")
        group_name = WT_TO_GROUP.get(wt_id)
        if group_name is None:
            continue

        items = mtfmi_map.get(tid, [])

        # Группируем items по агрофирме
        firm_groups: dict[str, list[dict]] = defaultdict(list)
        for item in items:
            if not _item_eligible_for_area(item, group_name):
                continue
            fid = item.get("field_id")
            firm = field_to_firm_fn(int(fid))
            if firm:
                firm_groups[firm].append(item)

        if not firm_groups:
            continue

        work_dur = t.get("work_duration") or 0
        road_dur = t.get("movements_on_road_duration") or 0
        work_net = max(work_dur - road_dur, 0)

        machine_group = (machines_map.get(mid) or {}).get("machine_group_id")
        machine_name  = (machines_map.get(mid) or {}).get("name", f"machine_{mid}")

        item_min_area = 0.0 if group_name in NO_MIN_AREA_GROUPS else MIN_ITEM_AREA_HA
        task_area_in_window = calc_area_hourly(items, day_start, report_dt, item_min_area)

        for firm, firm_items in firm_groups.items():
            firm_area = calc_area_hourly(firm_items, day_start, report_dt, item_min_area)
            if firm_area <= 0:
                continue

            # Остановки
            stop_secs = 0.0
            for item in firm_items:
                hourly_stops = item.get("stops_duration_hourly") or []
                if hourly_stops:
                    for entry in hourly_stops:
                        h = parse_dt(entry[0])
                        if h and h < report_dt:
                            stop_secs += entry[1] or 0
                else:
                    stop_secs += item.get("stops_duration") or 0

            pm = firm_machines[firm]
            if mid not in pm:
                pm[mid] = {
                    "name":        machine_name,
                    "group":       machine_group,
                    "work_secs":   0.0,
                    "stop_secs":   0.0,
                    "has_stop":    False,
                    "area_total":  0.0,
                    # group_name → площадь по этой группе работ
                    "by_group":    defaultdict(float),
                }

            pm[mid]["area_total"]          += firm_area
            pm[mid]["by_group"][group_name] += firm_area
            pm[mid]["stop_secs"]           += stop_secs
            if stop_secs > STOP_THRESHOLD_SEC:
                pm[mid]["has_stop"] = True

            if task_area_in_window > 0:
                pm[mid]["work_secs"] += work_net * (firm_area / task_area_in_window)
            else:
                pm[mid]["work_secs"] += work_net

    # --- Собираем итог ---
    summary_data: dict[str, dict] = {}
    stops_data:   dict[str, list] = {}

    # Порог MIN_ITEM_AREA_HA уже применён на уровне отдельной mtfmi-записи
    # (calc_area_hourly, с учётом NO_MIN_AREA_GROUPS) при накоплении area_total
    # и by_group — доп. отсев по суммарной площади машины здесь не нужен и,
    # более того, некорректно резал бы мелкие площади по группам-исключениям.
    # Оставляем только защиту от пустых записей.
    for firm in AGROFIRMS:
        pm = firm_machines[firm]
        pm = {mid: d for mid, d in pm.items() if d["area_total"] > 0}

        # Для каждой группы работ считаем: суммарную площадь, кол-во машин, нанятых
        by_group_summary: dict[str, dict] = {}
        for group_name, _ in WORK_GROUPS:
            machines_for_area = [
                d for d in pm.values()
                if d["by_group"].get(group_name, 0.0) > 0
            ]
            area    = round(sum(d["by_group"][group_name] for d in machines_for_area), 1)
            n_work  = len(machines_for_area)
            n_hired = sum(1 for d in machines_for_area if d["group"] == HIRED_GROUP_ID)
            n_own = AVAILABLE_OWN.get(group_name, {}).get(firm, 0)
            by_group_summary[group_name] = {
                "area":     area,
                "n_work":   n_work,
                "n_hired":  n_hired,
                "n_own":    n_own,
                "avg":      round(area / n_work, 1) if n_work else 0.0,
            }

        summary_data[firm] = {
            "by_group": by_group_summary,
        }

        stops_data[firm] = [
            {
                "name":      d["name"],
                "work_secs": d["work_secs"],
                "stop_secs": d["stop_secs"],
                "area":      d["area_total"],
                # Для листа остановок — перечисляем группы, где машина работала
                "groups":    [g for g, a in d["by_group"].items() if a > 0],
            }
            for d in pm.values()
            if d["has_stop"]
        ]

    return summary_data, stops_data


# =============================================================================
# ЛИСТ 1: СВОДКА
# =============================================================================

def _write_group_table(ws, group_name: str, summary_data: dict,
                       start_row: int, dt_str: str) -> int:
    """Рисует один блок таблицы для группы работ. Возвращает следующую строку."""

    ws.merge_cells(start_row=start_row, start_column=1, end_row=start_row, end_column=5)
    _cell(ws, start_row, 1,
          f"Вид работы «{_group_display_name(group_name)}»",
          bold=True, color="FFFFFF", bg="1F3864")
    ws.row_dimensions[start_row].height = 26
    start_row += 1

    headers = [
        "Агрофирма",
        "Всего собственных\nагрегатов\n(в т.ч. прицепных)",
        "В работе\nзадействовано,\nед. техники",
        "Обработанная\nплощадь, га",
        "Средняя выработка\nна агрегат, га",
    ]
    for ci, h in enumerate(headers, start=1):
        _cell(ws, start_row, ci, h, bold=True, color="FFFFFF", bg="2E75B6")
    ws.row_dimensions[start_row].height = 55
    start_row += 1

    t_own = t_work = t_hired = 0
    t_area = 0.0

    for firm in AGROFIRMS:
        s   = summary_data.get(firm, {})
        gs  = s.get("by_group", {}).get(group_name, {})
        n_own   = gs.get("n_own", 0)
        area    = gs.get("area", 0.0)
        n_work  = gs.get("n_work", 0)
        n_hired = gs.get("n_hired", 0)
        avg     = gs.get("avg", 0.0)

        working_str = f"{n_work} в т.ч. наемников: {n_hired}"

        _cell(ws, start_row, 1, firm,                            halign="left")
        _cell(ws, start_row, 2, n_own)
        _cell(ws, start_row, 3, working_str)
        _cell(ws, start_row, 4, area    if area    else "")
        _cell(ws, start_row, 5, avg     if avg     else "")

        t_own   += n_own
        t_work  += n_work
        t_hired += n_hired
        t_area  += area
        start_row += 1

    t_avg = round(t_area / t_work, 1) if t_work else 0.0
    _cell(ws, start_row, 1, "Итого",
          bold=True, bg="BDD7EE", halign="left")
    _cell(ws, start_row, 2, t_own,
          bold=True, bg="BDD7EE")
    _cell(ws, start_row, 3, f"{t_work} в т.ч. наемников: {t_hired}",
          bold=True, bg="BDD7EE")
    _cell(ws, start_row, 4, round(t_area, 1) if t_area else "",
          bold=True, bg="BDD7EE")
    _cell(ws, start_row, 5, t_avg if t_avg else "",
          bold=True, bg="BDD7EE")
    start_row += 1

    start_row += 1  # пустая строка-разделитель
    return start_row


def write_sheet_summary(wb: openpyxl.Workbook,
                        summary_data: dict[str, dict],
                        report_dt:    datetime) -> None:
    ws     = wb.create_sheet("Сводка", 0)
    row = 1
    for group_idx, (group_name, _) in enumerate(WORK_GROUPS):
        if group_idx > 0:
            ws.row_breaks.append(Break(id=max(1, row - 1), min=0, max=16383, man=True))
        human_group = _group_display_name(group_name)
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
        c_group = ws.cell(row=row, column=1, value=f"Вид работы «{human_group}»")
        c_group.font = Font(name="Times New Roman", size=10, bold=True)
        c_group.alignment = Alignment(horizontal="left", vertical="center")
        ws.row_dimensions[row].height = 13
        row += 1
        for firm_idx, firm in enumerate(AGROFIRMS):
            s   = summary_data.get(firm, {})
            gs  = s.get("by_group", {}).get(group_name, {})
            n_own   = gs.get("n_own", 0)
            area    = gs.get("area", 0.0)
            n_work  = gs.get("n_work", 0)
            n_hired = gs.get("n_hired", 0)
            avg     = gs.get("avg", 0.0)

            if firm_idx > 0:
                row += 0
            row = _plain_line(ws, row, DISPLAY_FIRM_NAMES.get(firm, firm), bold=True)
            row = _plain_metric_row(
                ws, row,
                "Всего собственных агрегатов (в т.ч прицепных):",
                _fmt_plain_num(n_own),
            )
            row = _plain_metric_row(
                ws, row,
                "В работе задействовано, единиц техники:",
                _fmt_plain_num(n_work),
                "в т.ч. наемников:",
                _fmt_plain_num(n_hired),
            )
            row = _plain_metric_row(
                ws, row,
                "Обработанная площадь, гектар:",
                _fmt_plain_num(area),
            )
            row = _plain_metric_row(
                ws, row,
                "Средняя выработка на агрегат, гектар:",
                _fmt_plain_num(avg),
            )
        row += 1

    ws.column_dimensions["A"].width = 46
    ws.column_dimensions["B"].width = 10
    ws.column_dimensions["C"].width = 18
    ws.column_dimensions["D"].width = 8
    _apply_print_layout(ws, "D")


# =============================================================================
# ЛИСТ 2: ОСТАНОВКИ НА ПОЛЯХ
# =============================================================================

def write_sheet_stops(wb: openpyxl.Workbook,
                      stops_data: dict[str, list],
                      report_dt:  datetime) -> None:
    ws     = wb.create_sheet("Остановки на полях", 1)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=6)
    _cell(
        ws, 1, 1,
        f"Остановка на полях более {STOP_THRESHOLD_MIN} минут до {report_dt.hour} часов",
        bold=True, color="000000", bg=None, halign="left", wrap=False, border=False
    )

    headers = [
        "Регион",
        "Тип работ",
        "Машина",
        "Выработка,\nга за сутки",
        "Время работы на\nполях, чч:мм:сс",
        "Остановки на\nполях, чч:мм:сс",
    ]
    for ci, h in enumerate(headers, start=1):
        _cell(ws, 2, ci, h, bold=True, color="FFFFFF", bg="7F95B8")
    _fit_table_row_height(ws, 2, 1, 6, min_height=38.0)

    row = 3
    has_data = False

    for firm_name in AGROFIRMS:
        machines = stops_data.get(firm_name, [])
        if not machines:
            continue
        has_data = True

        firm_work_total = 0.0
        firm_stop_total = 0.0

        for idx, d in enumerate(sorted(machines, key=lambda x: -x["stop_secs"])):
            groups_str = ", ".join(d.get("groups", []))
            area_val   = d.get("area", 0.0)
            _cell(ws, row, 1, DISPLAY_FIRM_NAMES.get(firm_name, firm_name) if idx == 0 else "", halign="left", bg="DDE3EE")
            _cell(ws, row, 2, groups_str, halign="left", bg="7F95B8", color="FFFFFF")
            _cell(ws, row, 3, d["name"], halign="left")
            _cell(ws, row, 4, round(area_val, 2) if area_val else "")
            _cell(ws, row, 5, seconds_to_hms(d["work_secs"]))
            _cell(ws, row, 6, seconds_to_hms(d["stop_secs"]))
            _fit_table_row_height(ws, row, 1, 6, min_height=16.0)
            firm_work_total += d["work_secs"]
            firm_stop_total += d["stop_secs"]
            row += 1

        _cell(ws, row, 1, f"Всего ({DISPLAY_FIRM_NAMES.get(firm_name, firm_name)})", bold=True, halign="left", bg="DDE3EE")
        _cell(ws, row, 2, "", bold=True, bg="DDE3EE")
        _cell(ws, row, 3, "", bold=True, bg="DDE3EE")
        _cell(ws, row, 4, "", bold=True, bg="DDE3EE")
        _cell(ws, row, 5, seconds_to_hms(firm_work_total), bold=True, bg="DDE3EE")
        _cell(ws, row, 6, seconds_to_hms(firm_stop_total), bold=True, bg="DDE3EE")
        _fit_table_row_height(ws, row, 1, 6, min_height=16.0)
        row += 1

    if not has_data:
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
        _cell(ws, row, 1, f"Остановок более {STOP_THRESHOLD_MIN} минут не обнаружено", halign="left")
        _fit_table_row_height(ws, row, 1, 6, min_height=16.0)
        row += 1

    all_machines = [d for machines in stops_data.values() for d in machines]
    if all_machines:
        total_work = sum(d["work_secs"] for d in all_machines)
        total_stop = sum(d["stop_secs"] for d in all_machines)
        _cell(ws, row, 1, "Итого", bold=True, bg="D1D9E8", halign="left")
        _cell(ws, row, 2, "", bold=True, bg="D1D9E8")
        _cell(ws, row, 3, "", bold=True, bg="D1D9E8")
        _cell(ws, row, 4, "", bold=True, bg="D1D9E8")
        _cell(ws, row, 5, seconds_to_hms(total_work), bold=True, bg="D1D9E8")
        _cell(ws, row, 6, seconds_to_hms(total_stop), bold=True, bg="D1D9E8")
        _fit_table_row_height(ws, row, 1, 6, min_height=16.0)

    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 38
    ws.column_dimensions["C"].width = 36
    ws.column_dimensions["D"].width = 16
    ws.column_dimensions["E"].width = 16
    ws.column_dimensions["F"].width = 16
    _apply_print_layout(ws, "F")


def _apply_print_layout(ws, last_col: str) -> None:
    """Keep report 22 pages from splitting values onto separate PDF pages."""
    ws.page_setup.orientation = ws.ORIENTATION_PORTRAIT
    try:
        ws.page_setup.paperSize = ws.PAPERSIZE_A4
    except Exception:
        pass
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True, autoPageBreaks=True)
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.page_setup.scale = None
    ws.page_margins.left = 0.25
    ws.page_margins.right = 0.25
    ws.page_margins.top = 1.05
    ws.page_margins.bottom = 0.25
    ws.print_options.horizontalCentered = True
    ws.print_area = f"A1:{last_col}{ws.max_row}"


# =============================================================================
# ГЛАВНАЯ ФУНКЦИЯ
# =============================================================================

async def build_report_async() -> None:
    window_start = _parse_report_window(REPORT_WINDOW_START_RAW)
    window_end = _parse_report_window(REPORT_WINDOW_END_RAW)
    if window_start is not None and window_end is not None:
        if window_start >= window_end:
            raise ValueError(
                f"Неверное окно сводки 22: {REPORT_WINDOW_START_RAW} — {REPORT_WINDOW_END_RAW}"
            )
        day_start = window_start
        report_dt = window_end
    else:
        day_start = datetime(
            REPORT_DATE.year, REPORT_DATE.month, REPORT_DATE.day,
            DAY_START_HOUR, DAY_START_MINUTE, 0, tzinfo=TZ_MSK
        )
        report_dt = datetime(
            REPORT_DATE.year, REPORT_DATE.month, REPORT_DATE.day,
            REPORT_HOUR, REPORT_MINUTE, 0, tzinfo=TZ_MSK
        )

    print(f"Отчёт: Сводка по всем видам работ")
    print(f"Срез:  {report_dt.strftime('%H:%M %d.%m.%Y')}")
    print(f"Площадь считается: с {day_start.strftime('%d.%m %H:%M')} до {report_dt.strftime('%d.%m %H:%M')}\n")

    print("Загружаем справочники...")
    load_reference_data()

    headers = {"X-User-Api-Token": API_TOKEN}
    sem     = asyncio.Semaphore(SEMAPHORE_LIMIT)

    async with httpx.AsyncClient(headers=headers, timeout=120) as client:
        print("\nЗагружаем данные...")
        summary_data, stops_data = await fetch_all_data(client, sem, day_start, report_dt)

    print("\nФормируем Excel...")
    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    write_sheet_summary(wb, summary_data, report_dt)
    write_sheet_stops(wb, stops_data, report_dt)

    if window_start is not None and window_end is not None:
        output_file = (
            f"daily_summary_{day_start.strftime('%Y-%m-%d')}_"
            f"{report_dt.strftime('%Y%m%d_%H%M')}.xlsx"
        )
    else:
        output_file = f"daily_summary_{REPORT_DATE}_{REPORT_HOUR:02d}{REPORT_MINUTE:02d}.xlsx"
    wb.save(output_file)
    print(f"\nСохранено: {output_file}")


def build_report() -> None:
    asyncio.run(build_report_async())


if __name__ == "__main__":
    build_report()