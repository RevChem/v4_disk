"""
Аналитика урожайности комбайнов по сменам.

Методика сводки 14 (площадь Cropwise, масса из 1С «Намолот», урожайность т/га),
раскладка по дням: одна колонка на смену. По умолчанию 4 смены — то же окно,
что у сводки 14.1, но без суммирования в одну цифру.

Окно каждой колонки — одна смена [D-1 06:00 .. D 06:00), как в сводке 14.
Колонка подписана календарной датой начала смены. REPORT_DATE — утро отсечки
последней смены. Число смен: SVODKI_YIELD_DAYS (по умолчанию 4).

Смены запрашиваются по отдельности. У _shift_datetimes ширина окна зашита
в аргумент по умолчанию на момент импорта 14.main (обычно 1 день), поэтому
один вызов с REPORT_DAYS=4 не расширяет выборку и оставляет только последнюю смену.

0 — за смену была выработка, валовый сбор в 1С не найден (без вала).
—  — выработки по этой культуре в эту смену нет.
Серый — урожайность выше порога аномалии сводки 14.
Зелёный / жёлтый / розовый — верхняя / средняя / нижняя треть
урожайности внутри культуры и смены на листе фирмы.
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import importlib.util

_HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("report14_main", _HERE / "14.main.py")
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("Не найден 14.main.py")
r14 = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = r14
_SPEC.loader.exec_module(r14)

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.page import PageMargins


def _int_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


N_DAYS = _int_env("SVODKI_YIELD_DAYS", 4)

# Как в excel_style / сводке 14: шапка таблиц 8093B3, секции F3F4F6
HEADER_FILL = "8093B3"
SECTION_FILL = "F3F4F6"
FILL_TOP = r14.YIELD_TOP_TERTILE_COLOR
FILL_MID = r14.YIELD_MID_TERTILE_COLOR
FILL_BOTTOM = r14.YIELD_BOTTOM_TERTILE_COLOR
FILL_ANOMALY = r14.YIELD_ANOMALY_COLOR
FILL_ZERO = r14.YIELD_ZERO_COLOR

THIN = Border(
    left=Side(style="thin", color="000000"),
    right=Side(style="thin", color="000000"),
    top=Side(style="thin", color="000000"),
    bottom=Side(style="thin", color="000000"),
)
FONT_TITLE = Font(bold=True, size=12, color="000000")
FONT_SECTION = Font(bold=True, size=11, color="000000")
FONT_HEADER = Font(bold=True, color="FFFFFF")
FONT_DATA = Font(size=9, color="000000")
FONT_NOTE = Font(size=9, italic=True, color="000000")
FONT_NOTE_BOLD = Font(size=9, italic=True, bold=True, color="000000")
ALIGN_C = Alignment(horizontal="center", vertical="center", wrap_text=True)
ALIGN_DATA_C = Alignment(horizontal="center", vertical="center", wrap_text=False)
ALIGN_L = Alignment(horizontal="left", vertical="center", wrap_text=False)
ALIGN_TITLE = Alignment(horizontal="left", vertical="center", wrap_text=False)


@dataclass
class DayCell:
    area: float
    mass: float
    yld: float


def _step(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _legal_name(firm: str) -> str:
    raw = r14.FIRM_DISPLAY_NAMES.get(firm, firm).strip()
    if raw.count('"') >= 2:
        left, mid, right = raw.split('"', 2)
        raw = f"{left}«{mid}»{right}"
    return raw


def _machine_label(name: str, reg: str) -> str:
    label = " ".join(str(name or "").split())
    plate = " ".join(str(reg or "").split())
    if plate in {"", "—", "-"}:
        return label or "—"
    compact = label.replace(" ", "").casefold()
    if plate.replace(" ", "").casefold() in compact:
        return label or plate
    return f"{label} {plate}".strip()


def _row_key(crop: str, reg: str, label: str) -> tuple[str, str]:
    sig = r14._reg_signature(reg) or ""
    return (r14._normalize_crop_name(crop), sig or label.casefold())


def _display_yield(cell: DayCell | None):
    if cell is None or cell.area <= 0:
        return "—"
    if cell.mass <= 0:
        return 0
    return round(cell.yld, 2)


def _absorb(bucket: dict, df, day_idx: int, n_days: int) -> None:
    if df is None or df.empty:
        return
    for rec in df.to_dict("records"):
        crop = str(rec.get("Культура") or "—").strip() or "—"
        name = str(rec.get("Комбайн") or "")
        reg = str(rec.get("Гос. номер") or "")
        label = _machine_label(name, reg)
        key = _row_key(crop, reg, label)
        row = bucket.get(key)
        if row is None:
            row = {"crop": crop, "label": label, "days": [None] * n_days}
            bucket[key] = row
        elif len(label) > len(row["label"]):
            row["label"] = label
        area = float(rec.get("Обработанная площадь, га") or 0)
        mass = float(rec.get("Масса собранного урожая, т") or 0)
        yld = float(rec.get("Урожайность, т/га") or 0)
        prev = row["days"][day_idx]
        if prev is not None and prev.area >= area:
            continue
        row["days"][day_idx] = DayCell(area, mass, yld)


def _tasks_for_shift(tasks: list[dict], report_date: date) -> list[dict]:
    """Те же границы end_time, что fetch_machine_tasks сводки 14 за одну смену."""
    day_start, cutoff = r14._shift_datetimes(report_date)
    cutoff_ext = cutoff + timedelta(hours=r14.CUTOFF_BUFFER_HOURS)
    out = []
    for task in tasks:
        end_dt = r14._parse_dt(task.get("end_time"))
        if end_dt is None or end_dt < day_start or end_dt >= cutoff_ext:
            continue
        out.append(task)
    return out


def _fmi_for_tasks(raw_fmi: list[dict], task_ids: set[int]) -> list[dict]:
    if not task_ids:
        return []
    return [row for row in raw_fmi if row.get("machine_task_id") in task_ids]


def _style_range(ws, r1, c1, r2, c2, *, fill: str | None, font, align) -> None:
    pattern = PatternFill("solid", fgColor=fill) if fill else None
    for r in range(r1, r2 + 1):
        for c in range(c1, c2 + 1):
            cell = ws.cell(row=r, column=c)
            cell.border = THIN
            cell.font = font
            cell.alignment = align
            if pattern is not None:
                cell.fill = pattern


def _paint_yields(ws, first_row: int, n_rows: int, n_days: int) -> None:
    fill_top = PatternFill("solid", fgColor=FILL_TOP)
    fill_mid = PatternFill("solid", fgColor=FILL_MID)
    fill_bottom = PatternFill("solid", fgColor=FILL_BOTTOM)
    fill_anomaly = PatternFill("solid", fgColor=FILL_ANOMALY)
    fill_zero = PatternFill("solid", fgColor=FILL_ZERO)
    groups: dict[tuple[str, int], list[tuple[int, float]]] = defaultdict(list)
    for i in range(n_rows):
        r = first_row + i
        crop = str(ws.cell(row=r, column=2).value or "")
        for d in range(n_days):
            c = 4 + d
            val = ws.cell(row=r, column=c).value
            if val == 0 or val == 0.0:
                ws.cell(row=r, column=c).fill = fill_zero
                continue
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                continue
            yld = float(val)
            if r14._is_yield_anomaly(crop, yld):
                ws.cell(row=r, column=c).fill = fill_anomaly
                continue
            groups[(crop, c)].append((r, yld))
    for (_crop, col), items in groups.items():
        yields = [y for _, y in items]
        min_y, max_y = min(yields), max(yields)
        for r, yld in items:
            ws.cell(row=r, column=col).fill = r14._yield_tertile_fill_by_value(
                yld, min_y, max_y,
                fill_top=fill_top, fill_mid=fill_mid, fill_bottom=fill_bottom,
            )


def _write_section(
    ws, row: int, title: str, bucket: dict, labels: list[date],
) -> int:
    n_days = len(labels)
    n_cols = 3 + n_days
    rows = sorted(
        bucket.values(),
        key=lambda item: (r14._crop_sort_key(item["crop"]), item["label"].casefold()),
    )
    if not rows:
        return row

    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=n_cols)
    ws.cell(row=row, column=1, value=title)
    _style_range(ws, row, 1, row, n_cols, fill=SECTION_FILL, font=FONT_SECTION, align=ALIGN_C)
    ws.row_dimensions[row].height = 18
    row += 1

    header = row
    sub = row + 1
    ws.merge_cells(start_row=header, start_column=1, end_row=sub, end_column=1)
    ws.merge_cells(start_row=header, start_column=2, end_row=sub, end_column=2)
    ws.merge_cells(start_row=header, start_column=3, end_row=sub, end_column=3)
    ws.cell(row=header, column=1, value="№ п/п")
    ws.cell(row=header, column=2, value="Культура")
    ws.merge_cells(start_row=header, start_column=4, end_row=header, end_column=n_cols)
    ws.cell(row=header, column=4, value="Урожайность, т/га")
    for i, label in enumerate(labels):
        ws.cell(row=sub, column=4 + i, value=label.strftime("%d.%m.%Y"))
    _style_range(ws, header, 1, sub, n_cols, fill=HEADER_FILL, font=FONT_HEADER, align=ALIGN_C)
    ws.row_dimensions[header].height = 18
    ws.row_dimensions[sub].height = 18
    row = sub + 1

    first_data = row
    for i, item in enumerate(rows, start=1):
        ws.cell(row=row, column=1, value=i)
        ws.cell(row=row, column=2, value=item["crop"])
        ws.cell(row=row, column=3, value=item["label"])
        for d, cell in enumerate(item["days"]):
            value = _display_yield(cell)
            dest = ws.cell(row=row, column=4 + d, value=value)
            if isinstance(value, float):
                dest.number_format = "0.00"
        _style_range(ws, row, 1, row, n_cols, fill=None, font=FONT_DATA, align=ALIGN_DATA_C)
        ws.cell(row=row, column=2).alignment = ALIGN_L
        ws.cell(row=row, column=3).alignment = ALIGN_L
        ws.row_dimensions[row].height = 15
        row += 1
    _paint_yields(ws, first_data, len(rows), n_days)
    return row + 1


def _write_notes(ws, row: int, n_cols: int) -> None:
    notes = [
        (FONT_NOTE_BOLD, "Примечание к отчету:"),
        (FONT_NOTE, "— — выработка по данной культуре в указанный день отсутствует."),
    ]
    for font, text in notes:
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=n_cols)
        cell = ws.cell(row=row, column=1, value=text)
        cell.font = font
        cell.alignment = ALIGN_L
        row += 1
    swatch = ws.cell(row=row, column=1, value="")
    swatch.fill = PatternFill("solid", fgColor=FILL_ANOMALY)
    swatch.border = THIN
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=n_cols)
    note = ws.cell(row=row, column=2, value="цветом выделены показатели выше нормы")
    note.font = FONT_NOTE
    note.alignment = ALIGN_L


def _page_setup(ws, n_cols: int, last_row: int) -> None:
    ws.page_setup.orientation = "portrait"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins = PageMargins(left=0.4, right=0.4, top=0.45, bottom=0.4, header=0.2, footer=0.2)
    ws.print_options.horizontalCentered = True
    ws.sheet_view.showGridLines = False
    ws.page_setup.scale = 100
    ws.print_area = f"A1:{get_column_letter(n_cols)}{last_row}"
    ws.sheet_view.zoomScale = 110
    widths = [7, 16, 48] + [14] * (n_cols - 3)
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width


def _build_day_tables(
    *,
    report_date: date,
    tasks: list[dict],
    raw_fmi: list[dict],
    task_crop: dict,
    field_to_crop: dict,
    field_to_group: dict,
    harvesters, harvester_ids,
    hired_machines, hired_ids,
    field_to_fg, fg_to_folder,
    task_to_implement, implements_dict,
    field_to_firm_folder, field_name_to_firm_folder,
    allowed_crops=None,
    excluded_crops=None,
    own_machines=None,
    own_ids=None,
) -> dict[tuple[str, str], dict]:
    """firm, kind ('own'|'hired') -> bucket строк за одну смену."""
    r14.REPORT_DAYS = 1
    r14.REPORT_DATE = report_date
    day_tasks = _tasks_for_shift(tasks, report_date)
    task_ids = {t["id"] for t in day_tasks}
    task_to_machine = {t["id"]: t["machine_id"] for t in day_tasks}
    task_to_wt = {
        int(t["id"]): int(t["work_type_id"])
        for t in day_tasks
        if t.get("id") is not None and t.get("work_type_id") is not None
    }
    fmi = r14._normalize_fmi_for_shift(
        _fmi_for_tasks(raw_fmi, task_ids),
        report_date,
        task_wt=task_to_wt or None,
    )
    own_src = own_machines if own_machines is not None else harvesters
    own_id_src = own_ids if own_ids is not None else harvester_ids
    out: dict[tuple[str, str], dict] = {}
    for firm_name, (folder_id, _) in r14.AGROFIRMS.items():
        common = dict(
            field_to_fg=field_to_fg,
            fg_to_folder=fg_to_folder,
            task_to_implement=task_to_implement,
            implements_dict=implements_dict,
            task_crop_via_operation=task_crop,
            field_to_crop_general=field_to_crop,
            field_to_group=field_to_group,
            field_to_firm_folder=field_to_firm_folder,
            field_name_to_firm_folder=field_name_to_firm_folder,
            allowed_crops=allowed_crops,
            excluded_crops=excluded_crops,
        )
        own_df, _ = r14.build_own_table(
            folder_id, report_date, own_src, own_id_src,
            task_to_machine, fmi, **common,
        )
        hired_df, _ = r14.build_own_table(
            folder_id, report_date, hired_machines, hired_ids,
            task_to_machine, fmi, **common,
        )
        out[(firm_name, "own")] = own_df
        out[(firm_name, "hired")] = hired_df
    return out


def _fetch_shift(
    report_date: date,
    machine_ids: set[int] | None,
    work_type_ids: tuple[int, ...] | None = None,
) -> dict:
    """Одна смена теми же вызовами, что сводка 14 при REPORT_DAYS=1."""
    r14.REPORT_DAYS = 1
    r14.REPORT_DATE = report_date
    if work_type_ids is None:
        tasks = r14.fetch_machine_tasks(machine_ids, report_date)
    else:
        tasks = r14.fetch_machine_tasks(
            machine_ids, report_date, work_type_ids=work_type_ids,
        )
    task_ids = {t["id"] for t in tasks if t.get("id") is not None}
    raw_fmi = r14.fetch_fmi(task_ids, report_date) if task_ids else []
    task_to_implement = {
        t["id"]: t.get("implement_id") for t in tasks if t.get("id") is not None
    }
    return {
        "tasks": tasks,
        "raw_fmi": raw_fmi,
        "task_to_implement": task_to_implement,
    }


def _crops_for_tasks(task_lists: list[list[dict]], history_item_to_crop: dict) -> dict:
    task_ids = [
        t["id"] for tasks in task_lists for t in tasks if t.get("id") is not None
    ]
    if not task_ids:
        return {}
    return __import__("asyncio").run(
        r14.fetch_task_crop_via_agro_operation(task_ids, history_item_to_crop)
    )


def _load_reference(last_cutoff: date) -> dict:
    r14.REPORT_DAYS = 1
    r14.REPORT_DATE = last_cutoff
    _step("справочники")
    all_machines = r14.load_machines()
    harvesters, hired_machines, harvester_ids, hired_ids, _ = r14.split_machines(all_machines)
    print(f"  комбайнов {len(harvesters)}, наёмных {len(hired_machines)}", flush=True)
    (
        field_to_fg, fg_to_folder, _folder_to_name,
        field_to_firm_folder, field_name_to_firm_folder,
    ) = r14.load_geo_reference()
    implements_dict = {imp["id"]: imp for imp in r14.get_all_from_id("implements")}
    history_item_to_crop, field_to_crop = r14.load_crop_reference(last_cutoff)
    veg_own = [m for m in all_machines if m.get("machine_group_id") != r14.HIRED_GROUP_ID]
    veg_hired = [m for m in all_machines if m.get("machine_group_id") == r14.HIRED_GROUP_ID]
    return dict(
        harvesters=harvesters,
        harvester_ids=harvester_ids,
        hired_machines=hired_machines,
        hired_ids=hired_ids,
        field_to_fg=field_to_fg,
        fg_to_folder=fg_to_folder,
        field_to_firm_folder=field_to_firm_folder,
        field_name_to_firm_folder=field_name_to_firm_folder,
        implements_dict=implements_dict,
        field_to_crop=field_to_crop,
        history_item_to_crop=history_item_to_crop,
        veg_own=veg_own,
        veg_own_ids={m["id"] for m in veg_own},
        veg_hired=veg_hired,
        veg_hired_ids={m["id"] for m in veg_hired},
    )


def build_workbook(last_cutoff: date, n_days: int) -> tuple[Workbook, str, date, date]:
    cutoffs = [last_cutoff - timedelta(days=n_days - 1 - i) for i in range(n_days)]
    labels = [d - timedelta(days=1) for d in cutoffs]
    ctx = _load_reference(last_cutoff)
    grain_machine_ids = ctx["harvester_ids"] | ctx["hired_ids"]

    grain_shifts: list[dict] = []
    veg_shifts: list[dict] = []
    for report_date, label in zip(cutoffs, labels):
        _step(f"загрузка смены {label.strftime('%d.%m.%Y')} (отсечка {report_date.isoformat()})")
        grain = _fetch_shift(report_date, grain_machine_ids)
        grain_shifts.append(grain)
        print(f"  зерно: заданий {len(grain['tasks'])}, FMI {len(grain['raw_fmi'])}", flush=True)
        if r14.VEG_HARVEST_WORK_TYPE_IDS:
            veg = _fetch_shift(
                report_date, None, work_type_ids=r14.VEG_HARVEST_WORK_TYPE_IDS,
            )
            print(f"  овощи: заданий {len(veg['tasks'])}, FMI {len(veg['raw_fmi'])}", flush=True)
        else:
            veg = {"tasks": [], "raw_fmi": [], "task_to_implement": {}}
        veg_shifts.append(veg)

    _step("культуры заданий")
    task_crop = _crops_for_tasks(
        [shift["tasks"] for shift in grain_shifts], ctx["history_item_to_crop"],
    )
    veg_task_crop = _crops_for_tasks(
        [shift["tasks"] for shift in veg_shifts], ctx["history_item_to_crop"],
    )
    field_to_group = r14.build_global_field_group_map(
        *(shift["raw_fmi"] for shift in grain_shifts),
        *(shift["raw_fmi"] for shift in veg_shifts),
    )

    sections: list[tuple[str, dict]] = []
    grain_buckets: dict[tuple[str, str], dict] = {
        (firm, kind): {}
        for firm in r14.AGROFIRMS
        for kind in ("own", "hired")
    }
    veg_buckets: dict[str, dict] = {"own": {}, "hired": {}}

    for day_idx, report_date in enumerate(cutoffs):
        _step(f"расчёт смены {labels[day_idx].strftime('%d.%m.%Y')}")
        r14._namolot_signature_cache.pop(report_date, None)
        grain = grain_shifts[day_idx]
        day = _build_day_tables(
            report_date=report_date,
            tasks=grain["tasks"],
            raw_fmi=grain["raw_fmi"],
            task_crop=task_crop,
            field_to_crop=ctx["field_to_crop"],
            field_to_group=field_to_group,
            harvesters=ctx["harvesters"],
            harvester_ids=ctx["harvester_ids"],
            hired_machines=ctx["hired_machines"],
            hired_ids=ctx["hired_ids"],
            field_to_fg=ctx["field_to_fg"],
            fg_to_folder=ctx["fg_to_folder"],
            task_to_implement=grain["task_to_implement"],
            implements_dict=ctx["implements_dict"],
            field_to_firm_folder=ctx["field_to_firm_folder"],
            field_name_to_firm_folder=ctx["field_name_to_firm_folder"],
            excluded_crops=r14.FORAGE_CROPS,
        )
        for key, df in day.items():
            _absorb(grain_buckets[key], df, day_idx, n_days)
            n = 0 if df is None or df.empty else len(df)
            if n:
                print(f"    {key[0]} / {key[1]}: {n}", flush=True)

        veg = veg_shifts[day_idx]
        if veg["tasks"]:
            veg_day = _build_day_tables(
                report_date=report_date,
                tasks=veg["tasks"],
                raw_fmi=veg["raw_fmi"],
                task_crop=veg_task_crop,
                field_to_crop=ctx["field_to_crop"],
                field_to_group=field_to_group,
                harvesters=ctx["veg_own"],
                harvester_ids=ctx["veg_own_ids"],
                hired_machines=ctx["veg_hired"],
                hired_ids=ctx["veg_hired_ids"],
                field_to_fg=ctx["field_to_fg"],
                fg_to_folder=ctx["fg_to_folder"],
                task_to_implement=veg["task_to_implement"],
                implements_dict=ctx["implements_dict"],
                field_to_firm_folder=ctx["field_to_firm_folder"],
                field_name_to_firm_folder=ctx["field_name_to_firm_folder"],
                allowed_crops=r14.VEG_CROPS_ALLOWED,
                own_machines=ctx["veg_own"],
                own_ids=ctx["veg_own_ids"],
            )
            firm = r14.VEG_HARVEST_FIRM
            for kind in ("own", "hired"):
                _absorb(veg_buckets[kind], veg_day.get((firm, kind)), day_idx, n_days)

    kind_title = {"own": "СОБСТВЕННЫЕ", "hired": "НАЕМНЫЕ"}
    for firm in r14.AGROFIRMS:
        legal = _legal_name(firm)
        for kind in ("own", "hired"):
            bucket = grain_buckets[(firm, kind)]
            if bucket:
                sections.append((f"{legal} ({kind_title[kind]})", bucket))
    legal_veg = _legal_name(r14.VEG_HARVEST_FIRM)
    for kind in ("own", "hired"):
        if veg_buckets[kind]:
            sections.append((
                f"УБОРКА ОВОЩЕЙ ({legal_veg}) ({kind_title[kind]})",
                veg_buckets[kind],
            ))

    wb = Workbook()
    ws = wb.active
    ws.title = "Аналитика"
    n_cols = 3 + n_days
    title = (
        f"Аналитика по {n_days} дневной работе комбайнов "
        f"{labels[0].strftime('%d.%m.%Y')} - {labels[-1].strftime('%d.%m.%Y')}"
    )
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=n_cols)
    cell = ws.cell(row=1, column=1, value=title)
    cell.font = FONT_TITLE
    cell.alignment = ALIGN_TITLE
    ws.row_dimensions[1].height = 20

    row = 3
    for section_title, bucket in sections:
        row = _write_section(ws, row, section_title, bucket, labels)
    if row == 3:
        ws.cell(row=3, column=1, value="Нет выработки комбайнов за выбранные смены").font = FONT_DATA
        row = 4
    _write_notes(ws, row, n_cols)
    last_row = row + 3
    _page_setup(ws, n_cols, last_row)
    return wb, title, labels[0], labels[-1]


def _to_pdf(xlsx: Path) -> Path | None:
    import shutil
    import subprocess

    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return None
    env = os.environ.copy()
    env.setdefault("LC_ALL", "ru_RU.UTF-8")
    subprocess.run(
        [soffice, "--headless", "--norestore", "--convert-to", "pdf", "--outdir", str(xlsx.parent), str(xlsx)],
        check=False,
        env=env,
        timeout=180,
    )
    pdf = xlsx.with_suffix(".pdf")
    return pdf if pdf.is_file() else None


def main() -> None:
    raw = (os.environ.get("REPORT_DATE") or "").strip()
    last_cutoff = date.fromisoformat(raw) if raw else date.today()
    n_days = N_DAYS
    print(
        f"Аналитика урожайности комбайнов: {n_days} смен, отсечка {last_cutoff.isoformat()}",
        flush=True,
    )
    wb, title, d0, d1 = build_workbook(last_cutoff, n_days)
    out_name = f"{title}.xlsx"
    out = Path.cwd() / out_name
    wb.save(out)
    print(f"Сохранено: {out}", flush=True)
    pdf = _to_pdf(out)
    if pdf:
        print(f"PDF: {pdf}", flush=True)
    else:
        print("PDF не собран: нет LibreOffice (soffice).", flush=True)


if __name__ == "__main__":
    main()
