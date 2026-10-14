# -*- coding: utf-8 -*-
"""28. Сводка по топливу: выработка водителей и машин за период.

Лист 1 — водители (свои и наёмные).
Лист 2 — машины в разрезе полей (все, свои, наёмные).
Лист 3 — машины без полей.

Расход в поле берётся из карточки machine_task:
литры в поле = fuel_consumption − fuel_consumption_on_road.
Это совпадает с fuel_consumption_per_ha × covered_area.
л/га = литры в поле / площадь, л/км = литры в поле / work_distance.
Коэффициент ширины захвата — как в сводках 6 и 14:
covered_area_by_track / путь_км / ширина_орудия × 10 × 100.
Наёмные — machine_group_id == 7. Окно смены — с 06:00.

Запуск:
    python 28_fuel_summary.py --from 2025-11-01 --to 2025-12-01 \\
        --reference "/path/Сводка по топливу.xlsx"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=3))
BASE_URL = "https://operations.cropwise.com/api/v3"
HIRED_GROUP_ID = 7
SHIFT_HOUR = 6

FIRM_FOLDERS: dict[int, str] = {
    3: "АФ Чистопольская",
    4: "АгроРаздолье",
    5: "Аксу-Агро",
    11: "НурлатАгро",
    6: "Дрожжаное Рассвет Агро",
    7: "Актай",
    9: "Лениногорская Птицефабрика",
}

# Виды работ образца «Сводка по топливу» (ноябрь 2025). --all-agri берёт все,
# кроме перевозки (7.*) и хозяйственных (8.*).
SAMPLE_WORK_TYPE_IDS = (55, 171, 121, 60, 126, 118)


def _load_env() -> None:
    here = Path(__file__).resolve()
    for folder in (here.parent, *here.parents):
        env_path = folder / ".env"
        if not env_path.is_file():
            continue
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        if os.environ.get("CROPWISE_API_TOKEN") or os.environ.get("API_TOKEN"):
            return


def _token() -> str:
    token = (os.environ.get("CROPWISE_API_TOKEN") or os.environ.get("API_TOKEN") or "").strip()
    if not token:
        raise SystemExit("Нет CROPWISE_API_TOKEN в окружении или .env")
    return token


def _parse_api_dt(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ)


def _parse_hour(value) -> datetime | None:
    """Метка часа в covered_area_hourly: наивный UTC."""
    if not value:
        return None
    text = str(value).strip()
    if "T" in text or "+" in text or text.endswith("Z"):
        return _parse_api_dt(text)
    try:
        dt = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(TZ)


def _num(value) -> float:
    try:
        if value is None or value is False:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _series_sum(series, win_lo: datetime, win_hi: datetime) -> float | None:
    if not series:
        return None
    total = 0.0
    seen = False
    for item in series:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        ts = _parse_hour(item[0])
        if ts is None:
            continue
        seen = True
        if win_lo <= ts < win_hi:
            total += _num(item[1])
    return total if seen else None


def get_pages(session, endpoint: str, params: dict | None = None) -> list[dict]:
    import requests

    url = endpoint if endpoint.startswith("http") else f"{BASE_URL}/{endpoint}"
    out: list[dict] = []
    from_id = 0
    base = dict(params or {})
    while True:
        query = {**base, "from_id": from_id, "limit": 1000}
        response = session.get(url, params=query, timeout=180)
        response.raise_for_status()
        body = response.json()
        chunk = body.get("data") or []
        if not chunk:
            break
        out.extend(chunk)
        meta = (body.get("meta") or {}).get("response") or {}
        last = meta.get("last_record_id")
        obtained = int(meta.get("obtained_records") or len(chunk))
        print(f"    {endpoint}: +{len(chunk)} (всего {len(out) + 0})", flush=True)
        if not last or obtained < 100:
            break
        from_id = int(last) + 1
    return out


def _cache_path(cache_dir: Path, name: str) -> Path:
    return cache_dir / f"{name}.json"


def cached(cache_dir: Path, name: str, loader):
    path = _cache_path(cache_dir, name)
    if path.is_file() and os.environ.get("FUEL_REFRESH") != "1":
        print(f"  кэш {name}", flush=True)
        return json.loads(path.read_text(encoding="utf-8"))
    data = loader()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def _slim_task(task: dict) -> dict:
    keys = (
        "id", "machine_id", "driver_id", "implement_id", "work_type_id",
        "start_time", "end_time", "covered_area", "covered_area_hourly",
        "work_distance", "work_distance_hourly", "work_duration", "work_duration_hourly",
        "fuel_consumption", "fuel_consumption_on_road", "fuel_consumption_per_ha",
        "real_implement_width",
    )
    return {key: task.get(key) for key in keys}


def _slim_mtfmi(item: dict) -> dict:
    keys = (
        "id", "machine_task_id", "field_id", "covered_area",
        "work_distance", "work_duration", "covered_area_by_track",
        "fuel_consumption", "fuel_consumption_per_ha",
    )
    return {key: item.get(key) for key in keys}


def fetch_work_types(session, all_agri: bool) -> dict[int, str]:
    rows = get_pages(session, "work_types")
    names: dict[int, str] = {}
    for row in rows:
        try:
            wid = int(row["id"])
        except (KeyError, TypeError, ValueError):
            continue
        name = str(row.get("name") or "").strip()
        if not all_agri:
            if wid in SAMPLE_WORK_TYPE_IDS:
                names[wid] = name
            continue
        if name[:2] in {"7.", "8."} or name.startswith("7.") or name.startswith("8."):
            continue
        if name[:1].isdigit():
            names[wid] = name
    return names


def fetch_tasks(session, work_ids: list[int], win_lo: datetime, win_hi: datetime) -> list[dict]:
    # Захват с запасом в сутки: площадь режется по часам внутри окна.
    lo = (win_lo - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S+03:00")
    hi = (win_hi + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S+03:00")
    by_id: dict[int, dict] = {}
    batch = 15
    for start in range(0, len(work_ids), batch):
        chunk = work_ids[start:start + batch]
        rows = get_pages(session, "machine_tasks", {
            "work_type_id": ",".join(str(i) for i in chunk),
            "start_time_gt_eq": lo,
            "start_time_lt": hi,
        })
        for row in rows:
            if row.get("id") is not None:
                by_id[int(row["id"])] = _slim_task(row)
    return list(by_id.values())


def fetch_mtfmi(session, task_ids: list[int]) -> list[dict]:
    out: list[dict] = []
    size = 200
    for start in range(0, len(task_ids), size):
        chunk = task_ids[start:start + size]
        rows = get_pages(session, "machine_task_field_mapping_items", {
            "machine_task_id": ",".join(str(i) for i in chunk),
        })
        out.extend(_slim_mtfmi(row) for row in rows)
        print(f"    mtfmi {min(start + size, len(task_ids))}/{len(task_ids)}", flush=True)
    return out


def _norm(text: object) -> str:
    return " ".join(str(text or "").split())


def _person_name(user: dict) -> str:
    username = _norm(user.get("username"))
    if username:
        return username
    parts = [user.get("last_name"), user.get("first_name"), user.get("middle_name")]
    return _norm(" ".join(str(p) for p in parts if p and str(p).strip()))


def _mass(session, endpoint: str, ids: list[int]) -> list[dict]:
    import requests

    uniq = list(dict.fromkeys(int(i) for i in ids if i))
    out: list[dict] = []
    for start in range(0, len(uniq), 400):
        chunk = uniq[start:start + 400]
        response = session.post(
            f"{BASE_URL}/{endpoint}/mass_request",
            json={"data": chunk},
            timeout=180,
        )
        if response.status_code >= 400:
            break
        out.extend(response.json().get("data") or [])
    return out


def build_firms(fields: list[dict], field_groups: list[dict], folders: list[dict]) -> dict[int, str]:
    parent: dict[int, int | None] = {}
    for folder in folders:
        if folder.get("id") is None:
            continue
        pid = (
            folder.get("parent_id")
            or folder.get("parent_group_folder_id")
            or folder.get("group_folder_parent_id")
        )
        parent[int(folder["id"])] = int(pid) if pid is not None else None
    roots = set(FIRM_FOLDERS)

    def root_of(folder_id: int | None) -> int | None:
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
            nxt = parent.get(cur)
            if nxt is None:
                return None
            cur = int(nxt)
        return None

    fg_folder = {}
    for group in field_groups:
        if group.get("id") is None:
            continue
        gid = group.get("group_folder_id") or group.get("folder_id")
        if gid is not None:
            fg_folder[int(group["id"])] = int(gid)

    firms: dict[int, str] = {}
    for field_row in fields:
        if field_row.get("id") is None:
            continue
        fg = field_row.get("field_group_id")
        folder_id = fg_folder.get(int(fg)) if fg is not None else None
        if folder_id is None:
            raw = field_row.get("group_folder_id") or field_row.get("folder_id")
            folder_id = int(raw) if raw is not None else None
        root = root_of(folder_id)
        if root is not None:
            firms[int(field_row["id"])] = FIRM_FOLDERS[root]
    return firms


@dataclass
class Unit:
    """Одна строка площади (mtfmi) с выработкой. В образце это «задание»."""
    firm: str
    work: str
    driver: str
    machine: str
    field: str
    hired: bool
    area: float
    l_ha: float
    l_km: float | None
    width: float | None
    duration_s: float


def _coef(track_ha: float, distance_m: float, width_m: float) -> float | None:
    if track_ha <= 0 or distance_m <= 0 or width_m <= 0:
        return None
    return track_ha / (distance_m / 1000.0) / width_m * 10 * 100


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def build_units(
    raw_tasks: list[dict],
    mtfmi: list[dict],
    *,
    win_lo: datetime,
    win_hi: datetime,
    work_names: dict[int, str],
    machines: dict[int, dict],
    users: dict[int, dict],
    implements: dict[int, dict],
    field_names: dict[int, str],
    field_firms: dict[int, str],
) -> list[Unit]:
    by_task: dict[int, list[dict]] = defaultdict(list)
    for item in mtfmi:
        if item.get("machine_task_id") is not None:
            by_task[int(item["machine_task_id"])].append(item)

    units: list[Unit] = []
    for task in raw_tasks:
        start = _parse_api_dt(task.get("start_time"))
        if start is None or not (win_lo <= start < win_hi):
            continue
        try:
            work = work_names[int(task.get("work_type_id"))]
        except (KeyError, TypeError, ValueError):
            continue
        machine = machines.get(int(task.get("machine_id") or 0), {})
        hired = int(machine.get("machine_group_id") or 0) == HIRED_GROUP_ID
        machine_name = _norm(machine.get("name") or f"машина {task.get('machine_id')}")
        driver_name = _person_name(users.get(int(task.get("driver_id") or 0), {}))
        implement = implements.get(int(task.get("implement_id") or 0))
        width_m = 0.0
        if implement:
            width_m = _num(implement.get("width") or implement.get("official_width"))
        if width_m <= 0:
            width_m = _num(task.get("real_implement_width"))
        for item in by_task.get(int(task["id"]), []):
            area = _num(item.get("covered_area"))
            if area <= 0:
                continue
            try:
                fid = int(item.get("field_id"))
            except (TypeError, ValueError):
                continue
            firm = field_firms.get(fid, "")
            if not firm:
                continue
            dist = _num(item.get("work_distance"))
            fuel = _num(item.get("fuel_consumption"))
            track = _num(item.get("covered_area_by_track"))
            l_km = (fuel / (dist / 1000.0)) if dist > 0 else None
            units.append(Unit(
                firm=firm,
                work=work,
                driver=driver_name,
                machine=machine_name,
                field=field_names.get(fid, str(fid)),
                hired=hired,
                area=area,
                l_ha=_num(item.get("fuel_consumption_per_ha")),
                l_km=l_km,
                width=_coef(track, dist, width_m),
                duration_s=_num(item.get("work_duration")),
            ))
    return units


@dataclass
class AggRow:
    firm: str
    work: str
    name: str
    field: str
    tasks: int
    area: float
    l_ha: float | None
    l_km: float | None
    width: float | None
    dur_min: float
    dur_max: float
    dur_sum: float
    output: float | None
    kind: str = "data"


def _row_from_units(units: list[Unit], *, firm: str, work: str, name: str, field_name: str, kind: str = "data") -> AggRow:
    durations = [u.duration_s for u in units]
    area = sum(u.area for u in units)
    return AggRow(
        firm=firm,
        work=work,
        name=name,
        field=field_name,
        tasks=len(units),
        area=area,
        l_ha=_mean([u.l_ha for u in units]),
        l_km=_mean([u.l_km for u in units if u.l_km is not None]),
        width=_mean([u.width for u in units if u.width is not None]),
        dur_min=min(durations) if durations else 0.0,
        dur_max=max(durations) if durations else 0.0,
        dur_sum=sum(durations),
        output=(area / len(units)) if units else None,
        kind=kind,
    )


def _accumulate(units: list[Unit], *, with_field: bool, attr: str) -> list[AggRow]:
    groups: dict[tuple, list[Unit]] = defaultdict(list)
    for unit in units:
        name = getattr(unit, attr)
        if attr == "driver" and not name:
            continue
        key = (unit.firm, unit.work, name, unit.field if with_field else "")
        groups[key].append(unit)
    rows = [
        _row_from_units(group, firm=key[0], work=key[1], name=key[2], field_name=key[3])
        for key, group in groups.items()
    ]
    return rows


def _with_totals(rows: list[AggRow], units: list[Unit], *, attr: str) -> list[AggRow]:
    """Итог — среднее по строкам площади, каждая строка один вес."""
    usable = [u for u in units if attr != "driver" or u.driver]
    data = sorted(rows, key=lambda r: (r.firm, r.work, r.name, r.field))
    by_firm_work: dict[tuple[str, str], list[AggRow]] = defaultdict(list)
    for row in data:
        by_firm_work[(row.firm, row.work)].append(row)

    def pool(firm: str, work: str | None) -> list[Unit]:
        return [
            u for u in usable
            if u.firm == firm and (work is None or u.work == work)
        ]

    firms = sorted({r.firm for r in data})
    out: list[AggRow] = []
    for firm in firms:
        works = sorted({w for f, w in by_firm_work if f == firm})
        for work in works:
            out.extend(by_firm_work[(firm, work)])
            out.append(_row_from_units(
                pool(firm, work), firm="", work=f"Всего ({work})", name="", field_name="", kind="work",
            ))
        out.append(_row_from_units(
            pool(firm, None), firm=f"Всего ({firm})", work="", name="", field_name="", kind="firm",
        ))
    out.append(_row_from_units(
        usable, firm="Итого", work="", name="", field_name="", kind="grand",
    ))
    return out


def _seconds_to_day(seconds: float) -> float:
    return seconds / 86400.0


def _write_block(ws, start_col: int, title: str, headers: list[str], rows: list[AggRow], *, with_field: bool, with_lkm: bool, with_output: bool):
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    thin = Border(
        left=Side(style="thin", color="D0D0D0"),
        right=Side(style="thin", color="D0D0D0"),
        top=Side(style="thin", color="D0D0D0"),
        bottom=Side(style="thin", color="D0D0D0"),
    )
    header_fill = PatternFill("solid", fgColor="1F4E79")
    header_font = Font(bold=True, color="FFFFFF", name="Calibri", size=9)
    title_font = Font(bold=True, name="Calibri", size=12, color="1F4E79")
    data_font = Font(name="Calibri", size=9)
    total_fill = PatternFill("solid", fgColor="D6E3F0")
    firm_fill = PatternFill("solid", fgColor="FFF2CC")
    grand_fill = PatternFill("solid", fgColor="F4B183")
    total_font = Font(bold=True, name="Calibri", size=9)

    ws.cell(2, start_col, title).font = title_font
    for offset, header in enumerate(headers):
        cell = ws.cell(3, start_col + offset, header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
        cell.border = thin
    ws.row_dimensions[3].height = 32
    ws.auto_filter.ref = None
    ws.freeze_panes = "A4"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToPage = True
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.oddHeader.left.text = title
    ws.print_title_rows = "1:3"
    ws.page_setup.horizontalCentered = True
    ws.sheet_view.showGridLines = False
    ws.sheet_view.zoomScale = 90
    ws.page_margins.left = 0.4
    ws.page_margins.right = 0.4
    ws.page_margins.top = 0.6
    ws.page_margins.bottom = 0.4

    for index, row in enumerate(rows):
        excel_row = 4 + index
        values: list = [row.firm, row.work]
        if with_field:
            values.extend([row.name, row.field])
        else:
            values.append(row.name)
        values.append(row.tasks)
        values.append(round(row.area, 2))
        values.append(None if row.l_ha is None else round(row.l_ha, 2))
        if with_lkm:
            values.append(None if row.l_km is None else round(row.l_km, 2))
        values.append(None if row.width is None else round(row.width, 2))
        values.append(_seconds_to_day(row.dur_min))
        values.append(_seconds_to_day(row.dur_max))
        values.append(_seconds_to_day(row.dur_sum))
        if with_output:
            values.append(None if row.output is None else round(row.output, 2))
        fill = None
        font = data_font
        if row.kind == "work":
            fill, font = total_fill, total_font
        elif row.kind == "firm":
            fill, font = firm_fill, total_font
        elif row.kind == "grand":
            fill, font = grand_fill, total_font
        for offset, value in enumerate(values):
            cell = ws.cell(excel_row, start_col + offset, value)
            cell.font = font
            cell.border = thin
            cell.alignment = Alignment(vertical="center")
            if fill is not None:
                cell.fill = fill
        # числовые форматы: площадь, расход, ширина, время, выработка
        base = start_col
        name_cols = 4 if with_field else 3
        area_col = base + name_cols + 1
        ws.cell(excel_row, area_col).number_format = "0.00"
        cursor = area_col + 1
        ws.cell(excel_row, cursor).number_format = "0.00"
        cursor += 1
        if with_lkm:
            ws.cell(excel_row, cursor).number_format = "0.00"
            cursor += 1
        ws.cell(excel_row, cursor).number_format = "0.00"
        cursor += 1
        for _ in range(3):
            ws.cell(excel_row, cursor).number_format = "[h]:mm:ss"
            cursor += 1
        if with_output:
            ws.cell(excel_row, cursor).number_format = "0.00"

    widths = {
        0: 28, 1: 42, 2: 38,
    }
    if with_field:
        widths[3] = 16
    for offset in range(len(headers)):
        letter = get_column_letter(start_col + offset)
        ws.column_dimensions[letter].width = widths.get(offset, 16)


DRIVER_HEADERS_OWN = [
    "Агрофирма", "Подтип работ", "Водитель", "Кол-во заданий",
    "Обработанная площадь, га", "Расход топлива в поле, л/га",
    "Средний расход внутри поля, л/км", "Коэф. ширины захвата",
    "Продолжительность работы в полях, часов (минимум)",
    "Продолжительность работы в полях, часов (максимум)",
    "Продолжительность работы в полях, часов (суммарно)",
]
DRIVER_HEADERS_HIRED = [
    "Агрофирма", "Подтип работ", "Водитель", "Кол-во заданий",
    "Обработанная площадь, га", "Расход топлива в поле, л/га",
    "Коэф. ширины захвата",
    "Продолжительность работы в полях, часов (минимум)",
    "Продолжительность работы в полях, часов (максимум)",
    "Продолжительность работы в полях, часов (суммарно)",
]
MACHINE_FIELD_HEADERS = [
    "Агрофирма", "Подтип работ", "Машина", "Поле", "Кол-во заданий",
    "Обработанная площадь, га", "Расход топлива в поле, л/га",
    "Средний расход внутри поля, л/км", "Коэффициент ширины захвата в %",
    "Продолжительность работы в полях, часов (Минимум)",
    "Продолжительность работы в полях, часов (Максимум)",
    "Продолжительность работы в полях, часов (Суммарно)",
    "Выработка за смену",
]
MACHINE_FIELD_HEADERS_HIRED = [
    h for h in MACHINE_FIELD_HEADERS if h != "Средний расход внутри поля, л/км"
]
MACHINE_HEADERS = [
    "Агрофирма", "Подтип работ", "Машина", "Кол-во заданий",
    "Обработанная площадь, га", "Расход топлива в поле, л/га",
    "Средний расход внутри поля, л/км", "Коэффициент ширины захвата в %",
    "Продолжительность работы в полях, часов (Минимум)",
    "Продолжительность работы в полях, часов (Максимум)",
    "Продолжительность работы в полях, часов (Суммарно)",
    "Выработка за смену",
]
MACHINE_HEADERS_HIRED = [h for h in MACHINE_HEADERS if "л/км" not in h]


def write_workbook(path: Path, units: list[Unit], win_lo: datetime, win_hi: datetime) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    own = [t for t in units if not t.hired]
    hired = [t for t in units if t.hired]

    def pack(subset: list[Unit], *, with_field: bool, attr: str) -> list[AggRow]:
        rows = _accumulate(subset, with_field=with_field, attr=attr)
        return _with_totals(rows, subset, attr=attr)

    wb = Workbook()
    intro = wb.active
    intro.title = "Введение"
    intro["A1"] = "Выработка техники и водителей"
    intro["A1"].font = Font(bold=True, size=14)
    intro["A2"] = "Диапазон выгрузки:"
    intro["B2"] = (
        f"{win_lo.strftime('%d.%m.%Y %H:%M')} - "
        f"{(win_hi - timedelta(minutes=1)).strftime('%d.%m.%Y %-H:%M')}"
    )
    intro["A4"] = (
        "Одна строка площади machine_task_field_mapping_items — одно «задание». "
        "л/га — fuel_consumption_per_ha этой строки, л/км — её fuel_consumption / work_distance. "
        "Итог группы — среднее по строкам. Выработка за смену — площадь / число строк. "
        "Наёмные: machine_group_id = 7, колонки л/км нет."
    )
    intro.column_dimensions["A"].width = 28
    intro.column_dimensions["B"].width = 80

    ws1 = wb.create_sheet("1")
    _write_block(ws1, 2, "Собственные машины", DRIVER_HEADERS_OWN, pack(own, with_field=False, attr="driver"), with_field=False, with_lkm=True, with_output=False)
    _write_block(ws1, 14, "Наёмные машины", DRIVER_HEADERS_HIRED, pack(hired, with_field=False, attr="driver"), with_field=False, with_lkm=False, with_output=False)

    ws2 = wb.create_sheet("2")
    _write_block(ws2, 2, "Машины всего в разрезе полей", MACHINE_FIELD_HEADERS, pack(units, with_field=True, attr="machine"), with_field=True, with_lkm=True, with_output=True)
    _write_block(ws2, 16, "Собственные машины в разрезе полей", MACHINE_FIELD_HEADERS, pack(own, with_field=True, attr="machine"), with_field=True, with_lkm=True, with_output=True)
    _write_block(ws2, 30, "Наёмные машины в разрезе полей", MACHINE_FIELD_HEADERS_HIRED, pack(hired, with_field=True, attr="machine"), with_field=True, with_lkm=False, with_output=True)

    ws3 = wb.create_sheet("3")
    _write_block(ws3, 2, "Машины всего без полей", MACHINE_HEADERS, pack(units, with_field=False, attr="machine"), with_field=False, with_lkm=True, with_output=True)
    _write_block(ws3, 15, "Собственные машины без полей", MACHINE_HEADERS, pack(own, with_field=False, attr="machine"), with_field=False, with_lkm=True, with_output=True)
    _write_block(ws3, 28, "Наёмные машины без полей", MACHINE_HEADERS_HIRED, pack(hired, with_field=False, attr="machine"), with_field=False, with_lkm=False, with_output=True)

    for ws in (ws1, ws2, ws3):
        ws.auto_filter.ref = None
        ws.freeze_panes = "B4"
        ws.page_setup.orientation = "landscape"
        ws.page_setup.paperSize = ws.PAPERSIZE_A4
        ws.page_setup.fitToPage = True
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.print_title_rows = "1:3"
        ws.page_setup.horizontalCentered = True
        ws.sheet_view.showGridLines = False
        ws.page_margins.left = 0.4
        ws.page_margins.right = 0.4
        ws.page_margins.top = 0.55
        ws.page_margins.bottom = 0.4
        ws.oddFooter.right.text = "Стр. &P из &N"
        ws.oddFooter.left.text = "ХК «Чистополье»"

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    print(f"записано {path}", flush=True)


def _ref_rows(path: Path, sheet: str, start_col: int, *, with_field: bool, with_lkm: bool) -> dict[tuple, dict]:
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True, read_only=True)
    ws = wb[sheet]
    out = {}
    for row in ws.iter_rows(min_row=4, min_col=start_col, max_col=start_col + 14, values_only=True):
        firm = str(row[0] or "").strip()
        work = str(row[1] or "").strip()
        if not firm and not work:
            continue
        if firm.startswith("Всего") or firm == "Итого" or work.startswith("Всего"):
            continue
        if with_field:
            name = str(row[2] or "").strip()
            field_name = str(row[3] or "").strip()
            tasks, area, l_ha = row[4], row[5], row[6]
            cursor = 7
        else:
            name = str(row[2] or "").strip()
            field_name = ""
            tasks, area, l_ha = row[3], row[4], row[5]
            cursor = 6
        l_km = None
        if with_lkm:
            l_km = row[cursor]
            cursor += 1
        out[(firm, work, name, field_name)] = {
            "tasks": tasks, "area": area, "l_ha": l_ha, "l_km": l_km,
        }
    wb.close()
    return out


def _our_rows(path: Path, sheet: str, start_col: int, *, with_field: bool, with_lkm: bool) -> dict[tuple, dict]:
    return _ref_rows(path, sheet, start_col, with_field=with_field, with_lkm=with_lkm)


def _as_float(value) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compare(reference: Path, produced: Path) -> int:
    specs = [
        ("1", 2, False, True, "водители свои"),
        ("1", 14, False, False, "водители наёмные"),
        ("2", 2, True, True, "машины+поля все"),
        ("3", 2, False, True, "машины все"),
    ]
    mismatches = 0
    for sheet, col, with_field, with_lkm, title in specs:
        ref = _ref_rows(reference, sheet, col, with_field=with_field, with_lkm=with_lkm)
        ours = _our_rows(produced, sheet, col, with_field=with_field, with_lkm=with_lkm)
        only_ref = sorted(set(ref) - set(ours))
        only_ours = sorted(set(ours) - set(ref))
        area_bad = []
        rate_bad = []
        count_bad = []
        for key in sorted(set(ref) & set(ours)):
            a, b = ref[key], ours[key]
            if _as_float(a["tasks"]) != _as_float(b["tasks"]):
                count_bad.append((key, a["tasks"], b["tasks"]))
            area_a, area_b = _as_float(a["area"]), _as_float(b["area"])
            if area_a is not None and area_b is not None and abs(area_a - area_b) > 0.2:
                area_bad.append((key, a["area"], b["area"]))
            rate_a, rate_b = _as_float(a["l_ha"]), _as_float(b["l_ha"])
            if rate_a is not None and rate_b is not None and abs(rate_a - rate_b) > 0.05:
                rate_bad.append((key, a["l_ha"], b["l_ha"]))
        print(
            f"\n{title}: эталон {len(ref)}, наша {len(ours)}, "
            f"только в эталоне {len(only_ref)}, только у нас {len(only_ours)}, "
            f"задания {len(count_bad)}, площадь {len(area_bad)}, л/га {len(rate_bad)}",
            flush=True,
        )
        for key, left, right in count_bad[:8]:
            print(f"  задания {key}: эталон {left} наша {right}", flush=True)
        for key, left, right in area_bad[:8]:
            print(f"  площадь {key}: эталон {left} наша {right}", flush=True)
        for key, left, right in rate_bad[:8]:
            print(f"  л/га {key}: эталон {left} наша {right}", flush=True)
        for key in only_ref[:5]:
            print(f"  нет у нас: {key} площадь {ref[key]['area']}", flush=True)
        for key in only_ours[:5]:
            print(f"  лишняя: {key} площадь {ours[key]['area']}", flush=True)
        mismatches += len(only_ref) + len(only_ours) + len(count_bad) + len(area_bad) + len(rate_bad)
    return mismatches


def main() -> None:
    _load_env()
    parser = argparse.ArgumentParser(description="Сводка по топливу")
    parser.add_argument("--from", dest="date_from", default="2025-11-01")
    parser.add_argument("--to", dest="date_to", default="2025-12-01")
    parser.add_argument("--output", default="")
    parser.add_argument("--reference", default="/home/ren/Загрузки/Сводка по топливу.xlsx")
    parser.add_argument("--all-agri", action="store_true")
    parser.add_argument("--no-compare", action="store_true")
    args = parser.parse_args()

    import requests

    win_lo = datetime.fromisoformat(args.date_from).replace(hour=SHIFT_HOUR, tzinfo=TZ)
    win_hi = datetime.fromisoformat(args.date_to).replace(hour=SHIFT_HOUR, tzinfo=TZ)
    if win_hi <= win_lo:
        raise SystemExit("Дата «по» должна быть позже даты «с»")

    cache_dir = Path(os.environ.get("FUEL_CACHE_DIR") or f"/tmp/fuel_svodka_{args.date_from}_{args.date_to}")
    cache_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["X-User-Api-Token"] = _token()

    work_names = cached(cache_dir, "work_types" + ("_all" if args.all_agri else ""), lambda: [
        {"id": k, "name": v} for k, v in fetch_work_types(session, args.all_agri).items()
    ])
    work_map = {int(row["id"]): row["name"] for row in work_names}
    print(f"видов работ: {len(work_map)}", flush=True)

    tasks_raw = cached(
        cache_dir,
        "tasks",
        lambda: fetch_tasks(session, sorted(work_map), win_lo, win_hi),
    )
    print(f"заданий в выборке: {len(tasks_raw)}", flush=True)
    task_ids = [int(t["id"]) for t in tasks_raw]
    mtfmi = cached(cache_dir, "mtfmi", lambda: fetch_mtfmi(session, task_ids)) if task_ids else []
    print(f"строк площади: {len(mtfmi)}", flush=True)

    machine_ids = [int(t["machine_id"]) for t in tasks_raw if t.get("machine_id")]
    driver_ids = [int(t["driver_id"]) for t in tasks_raw if t.get("driver_id")]
    implement_ids = [int(t["implement_id"]) for t in tasks_raw if t.get("implement_id")]
    machines = {int(m["id"]): m for m in cached(cache_dir, "machines", lambda: _mass(session, "machines", machine_ids) or get_pages(session, "machines"))}
    users = {int(u["id"]): u for u in cached(cache_dir, "users", lambda: _mass(session, "users", driver_ids))}
    if not users and driver_ids:
        users = {int(u["id"]): u for u in get_pages(session, "users")}
    implements = {int(i["id"]): i for i in cached(cache_dir, "implements", lambda: _mass(session, "implements", implement_ids) or get_pages(session, "implements"))}
    fields = cached(cache_dir, "fields", lambda: get_pages(session, "fields"))
    field_groups = cached(cache_dir, "field_groups", lambda: get_pages(session, "field_groups"))
    folders = cached(cache_dir, "group_folders", lambda: get_pages(session, "group_folders"))
    field_names = {int(f["id"]): str(f.get("name") or f.get("title") or f["id"]) for f in fields if f.get("id") is not None}
    field_firms = build_firms(fields, field_groups, folders)
    print(f"полей с агрофирмой: {len(field_firms)} из {len(field_names)}", flush=True)

    records = build_units(
        tasks_raw, mtfmi,
        win_lo=win_lo, win_hi=win_hi, work_names=work_map,
        machines=machines, users=users, implements=implements,
        field_names=field_names, field_firms=field_firms,
    )
    print(
        f"строк площади в сводке: {len(records)}, своих {sum(not t.hired for t in records)}, "
        f"наёмных {sum(t.hired for t in records)}",
        flush=True,
    )
    output = Path(args.output) if args.output else Path(__file__).resolve().parent / f"report_fuel_{args.date_from}_{args.date_to}.xlsx"
    write_workbook(output, records, win_lo, win_hi)
    reference = Path(args.reference)
    if not args.no_compare and reference.is_file():
        compare(reference, output)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
