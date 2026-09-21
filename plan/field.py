"""
Cropwise — отчёт "Поля с начала сезона"
========================================
Для каждой агрофирмы выводит лист Excel:
  Номер поля | Площадь поля, га | Культура | Агрооперация |
  Материал (удобрение / СЗР / семена) | Обработано, га |
  Факт внесения (кг/га или л/га) | Дата начала | Дата окончания

Источник данных: agro_operations (season=SEASON_YEAR), application_mix_items.
"""
from __future__ import annotations

import os, sys, requests
from collections import defaultdict
from datetime import date, datetime

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

CROPWISE_API_TOKEN   = "6DayNcBDr9BWEZgst1Dy"
BASE_URL    = "https://operations.cropwise.com/api/v3"
SEASON_YEAR = 2026

REPORT_DATE = date.today()
if os.environ.get("REPORT_DATE"):
    REPORT_DATE = date.fromisoformat(os.environ["REPORT_DATE"])

# folder_id → название агрофирмы
AGROFIRMS: dict[int, str] = {
    3:  "АФ Чистопольская",
    4:  "АгроРаздолье",
    5:  "Аксу-Агро",
    11: "НурлатАгро",
    6:  "Дрожжаное Рассвет Агро",
    7:  "Актай",
    9:  "Лениногорская Птицефабрика",
}

# Культуры, которые не выводим в отчёт
SKIP_CROPS: set[str] = {
    "Залежь, лес", "Залежь", "Введение в оборот 2026 год",
    "Луговые травы", "Обмен", "В пользовании у фермера",
    "Введение в оборот 2027 год", "Введение в оборот",
    "Передано в аренду", "Дороги/лесополосы", "Овраги/неудобия",
}

# =============================================================================
# HTTP
# =============================================================================

session = requests.Session()
session.headers.update({"X-User-Api-Token": CROPWISE_API_TOKEN})


def get_all(endpoint: str, params: dict | None = None) -> list[dict]:
    p = dict(params or {})
    results, from_id = [], 0
    while True:
        r = session.get(f"{BASE_URL}/{endpoint}",
                        params={**p, "from_id": from_id, "limit": 1000})
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
        r = session.get(f"{BASE_URL}/{endpoint}", params={**p, "page": page})
        r.raise_for_status()
        body  = r.json()
        chunk = body if isinstance(body, list) else body.get("data", [])
        results.extend(chunk)
        if len(chunk) < p["per_page"]:
            break
        page += 1
    return results

# =============================================================================
# ФОРМАТИРОВАНИЕ ДАТ
# =============================================================================

def _fmt_date(raw) -> str:
    if not raw:
        return ""
    s = str(raw)[:10]   # берём только YYYY-MM-DD
    try:
        d = date.fromisoformat(s)
        return d.strftime("%d.%m.%Y")
    except ValueError:
        return s

# =============================================================================
# СТИЛИ
# =============================================================================

_C_HDR   = "4472C4"   # синий заголовок
_C_TITLE = "D9E1F2"   # светло-синий заголовок агрофирмы
_C_TOTAL = "BDD7EE"   # итоговая строка
_C_ODD   = "FFFFFF"   # нечётная строка
_C_EVEN  = "F2F7FC"   # чётная строка
_C_FERT  = "EBF3E8"   # строка с материалом (подстрока)


def _side() -> Border:
    t = Side(style="thin", color="AAAAAA")
    return Border(left=t, right=t, top=t, bottom=t)


def _c(ws, row: int, col: int, value=None,
       bold=False, bg: str | None = None, color="000000",
       halign="center", wrap=True, fmt: str | None = None,
       size=10) -> None:
    cell = ws.cell(row=row, column=col, value=value)
    cell.font      = Font(name="Calibri", size=size, bold=bold, color=color)
    cell.alignment = Alignment(horizontal=halign, vertical="center", wrap_text=wrap)
    cell.border    = _side()
    if bg:
        cell.fill = PatternFill("solid", fgColor=bg)
    if fmt:
        cell.number_format = fmt

# =============================================================================
# ЗАГОЛОВКИ КОЛОНОК
# =============================================================================

HEADERS = [
    "Номер поля",
    "Площадь\nполя, га",
    "Культура",
    "Урожайность,\nц/га",
    "Название\nагрооперации",
    "Название ТМЦ\n(удобрение / СЗР / семена)",
    "Количество обраб.\nгектар",
    "Норма\nвнесения",
    "Факт\nвнесения",
    "Дата начала\nагрооперации",
    "Дата окончания\nагрооперации",
]

COL_W = [14, 10, 18, 10, 30, 30, 12, 12, 12, 12, 12]

# =============================================================================
# СТРОИМ СТРОКИ ДЛЯ ОДНОЙ АГРОФИРМЫ
# =============================================================================

def _calc_yield(op: dict) -> float | None:
    """Урожайность ц/га = harvested_weight (кг) / completed_area (га) / 100."""
    hw   = op.get("harvested_weight") or 0.0
    area = op.get("completed_area")   or 0.0
    if hw > 0 and area > 0:
        return round(hw / area / 100, 2)
    return None


def _build_rows(
    firm_field_ids: set[int],
    field_name_map: dict[int, str],
    field_area_map: dict[int, float],
    field_crop_map: dict[int, str],
    operations: list[dict],
    work_type_map: dict[int, str],
    material_map: dict[int, str],
) -> list[dict]:
    by_field: dict[int, list[dict]] = defaultdict(list)
    for op in operations:
        fid = op.get("field_id")
        if fid not in firm_field_ids:
            continue
        if str(op.get("status") or "").strip().lower() == "canceled":
            continue
        crop = field_crop_map.get(fid, "")
        if not crop or crop in SKIP_CROPS:
            continue
        by_field[fid].append(op)

    rows = []
    for fid in sorted(by_field.keys(),
                      key=lambda f: field_name_map.get(f, f"zzz_{f}")):
        ops   = sorted(by_field[fid], key=lambda o: o.get("planned_start_date") or "")
        fname = field_name_map.get(fid, f"field_{fid}")
        farea = field_area_map.get(fid, 0.0)
        crop  = field_crop_map.get(fid, "")

        field_yield: float | None = None
        for op in ops:
            y = _calc_yield(op)
            if y is not None:
                field_yield = y
                break

        field_first = True
        for op in ops:
            wt_id   = op.get("work_type_id")
            wt_name = work_type_map.get(wt_id, f"wt_{wt_id}")
            comp    = op.get("completed_area") or 0.0
            d_start = _fmt_date(op.get("actual_start_datetime") or op.get("planned_start_date"))
            d_end   = _fmt_date(op.get("completed_date") or op.get("planned_end_date"))
            mix     = op.get("application_mix_items") or []

            if mix:
                # Одна строка на каждый материал — все поля заполнены полностью
                for item in mix:
                    mat_id = item.get("applicable_id")
                    mat_nm = material_map.get(mat_id, f"id_{mat_id}") if mat_id is not None else ""
                    p_rate = item.get("planned_rate") or item.get("planned_value") or ""
                    f_rate = item.get("fact_rate")    or item.get("value")         or ""
                    if isinstance(p_rate, (int, float)):
                        p_rate = round(float(p_rate), 3)
                    if isinstance(f_rate, (int, float)):
                        f_rate = round(float(f_rate), 3)
                    mat_area = item.get("custom_fact_area") or comp or None
                    rows.append({
                        "field_name":      fname if field_first else "",
                        "field_area":      farea if field_first else None,
                        "crop":            crop  if field_first else "",
                        "field_yield":     field_yield if field_first else None,
                        "work_type":       wt_name,
                        "material":        mat_nm,
                        "completed_area":  mat_area,
                        "planned_rate":    p_rate,
                        "fact_rate":       f_rate,
                        "date_start":      d_start,
                        "date_end":        d_end,
                        "is_material_row": False,
                        "is_field_first":  field_first,
                    })
                    field_first = False
            else:
                # Материалов нет — одна строка без материала
                rows.append({
                    "field_name":      fname if field_first else "",
                    "field_area":      farea if field_first else None,
                    "crop":            crop  if field_first else "",
                    "field_yield":     field_yield if field_first else None,
                    "work_type":       wt_name,
                    "material":        "",
                    "completed_area":  comp if comp else None,
                    "planned_rate":    "",
                    "fact_rate":       "",
                    "date_start":      d_start,
                    "date_end":        d_end,
                    "is_material_row": False,
                    "is_field_first":  field_first,
                })
                field_first = False

    return rows

# =============================================================================
# ЗАПИСЬ ЛИСТА
# =============================================================================

def write_sheet(wb: openpyxl.Workbook, firm_name: str, rows: list[dict]) -> None:
    ws = wb.create_sheet(title=firm_name[:31])

    # Ширины колонок
    for ci, w in enumerate(COL_W, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = w

    row = 1

    # Заголовок агрофирмы
    ws.merge_cells(start_row=row, start_column=1,
                   end_row=row, end_column=len(HEADERS))
    c = ws.cell(row=row, column=1,
                value=f"Поля с начала сезона {SEASON_YEAR} — {firm_name}")
    c.font      = Font(name="Calibri", size=13, bold=True)
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.fill      = PatternFill("solid", fgColor=_C_TITLE)
    c.border    = _side()
    ws.row_dimensions[row].height = 22
    row += 1

    # Шапка
    for ci, h in enumerate(HEADERS, start=1):
        _c(ws, row, ci, h, bold=True, bg=_C_HDR, color="FFFFFF",
           halign="center", wrap=True, size=10)
    ws.row_dimensions[row].height = 38
    row += 1

    ws.freeze_panes = f"A{row}"

    # Данные
    field_parity: dict[str, int] = {}   # чередование цвета по полям
    parity_counter = 0

    for r in rows:
        fname = r["field_name"]
        if r["is_field_first"]:
            if fname not in field_parity:
                field_parity[fname] = parity_counter % 2
                parity_counter += 1
        bg_idx = field_parity.get(fname or list(field_parity.keys())[-1] if field_parity else "", 0)
        bg = _C_ODD if bg_idx == 0 else _C_EVEN

        vals = [
            r["field_name"],                                          # 1
            r["field_area"] if r["field_area"] != "" else None,       # 2
            r["crop"],                                                 # 3
            r["field_yield"] if r["field_yield"] is not None else None, # 4
            r["work_type"],                                            # 5
            r["material"],                                             # 6
            r["completed_area"] if r["completed_area"] else None,     # 7
            r["planned_rate"] if r["planned_rate"] != "" else None,   # 8
            r["fact_rate"] if r["fact_rate"] != "" else None,         # 9
            r["date_start"],                                           # 10
            r["date_end"],                                             # 11
        ]

        for ci, v in enumerate(vals, start=1):
            fmt = None
            if ci == 2 and isinstance(v, float):
                fmt = "# ##0.00"
            elif ci == 4 and isinstance(v, float):
                fmt = "# ##0.00"
            elif ci == 7 and isinstance(v, (int, float)):
                fmt = "# ##0.00"
            elif ci in (8, 9) and isinstance(v, (int, float)):
                fmt = "# ##0.000"
            _c(ws, row, ci, v,
               bg=bg,
               halign="left" if ci in (1, 3, 5, 6) else "center",
               wrap=False, fmt=fmt)

        ws.row_dimensions[row].height = 16
        row += 1

    # Итоговая строка — количество полей
    n_fields = len({r["field_name"] for r in rows if r["field_name"]})
    n_ops    = sum(1 for r in rows if r["work_type"] and not r["is_material_row"])
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=len(HEADERS))
    _c(ws, row, 1, f"Полей: {n_fields}   Операций: {n_ops}",
       bold=True, bg=_C_TOTAL, halign="left")
    for ci in range(2, len(HEADERS) + 1):
        _c(ws, row, ci, None, bg=_C_TOTAL)

# =============================================================================
# ГЛАВНЫЙ ПРОЦЕСС
# =============================================================================

def build_report() -> None:
    print(f"Отчёт 'Поля с начала сезона {SEASON_YEAR}'  |  {REPORT_DATE}")

    # ── Справочники ─────────────────────────────────────────────────────────
    print("  Загружаем поля...")
    fields_all = get_all("fields")
    fgs_all    = get_all_page("field_groups")
    print(f"    {len(fields_all)} полей, {len(fgs_all)} групп")

    field_name_map: dict[int, str]   = {}
    field_area_map: dict[int, float] = {}
    field_to_fg:    dict[int, int]   = {}
    for f in fields_all:
        fid = f["id"]
        field_name_map[fid] = f.get("name") or f.get("title") or f"field_{fid}"
        field_area_map[fid] = float(f.get("tillable_area") or f.get("calculated_area") or 0.0)
        field_to_fg[fid]    = f.get("field_group_id")

    fg_to_folder: dict[int, int] = {
        fg["id"]: fg.get("group_folder_id") for fg in fgs_all
    }

    # field_id → folder_id
    def get_folder(fid: int) -> int | None:
        return fg_to_folder.get(field_to_fg.get(fid))

    # folder_id → set of field_ids
    folder_fields: dict[int, set[int]] = defaultdict(set)
    for fid in field_name_map:
        fol = get_folder(fid)
        if fol is not None:
            folder_fields[fol].add(fid)

    print("  Загружаем культуры (history_items)...")
    crops_raw  = get_all_page("crops")
    crop_map   = {c["id"]: (c.get("name") or c.get("title", "")) for c in crops_raw}
    his_all    = get_all("history_items", {"year": str(SEASON_YEAR)})
    field_crop_map: dict[int, str] = {}
    for hi in his_all:
        fid = hi.get("field_id"); cid = hi.get("crop_id")
        if not (fid and cid):
            continue
        cname = crop_map.get(cid, "")
        if cname in SKIP_CROPS or not cname:
            continue
        if hi.get("active"):
            field_crop_map[fid] = cname
    print(f"    {len(field_crop_map)} полей с культурой")

    print("  Загружаем work_types...")
    wt_raw = get_all("work_types")
    if not wt_raw:
        wt_raw = get_all_page("work_types")
    work_type_map: dict[int, str] = {
        w["id"]: (w.get("name") or w.get("title") or f"wt_{w['id']}")
        for w in wt_raw
    }
    print(f"    {len(work_type_map)} видов работ")

    print("  Загружаем материалы (fertilizers, seeds, chemicals)...")
    material_map: dict[int, str] = {}
    for ep in ["fertilizers", "seeds", "chemicals"]:
        try:
            items = get_all(ep)
            for item in items:
                material_map[item["id"]] = (
                    item.get("name") or item.get("title") or f"{ep}_{item['id']}"
                )
        except Exception:
            pass
    print(f"    {len(material_map)} материалов")

    # ── Операции за сезон ───────────────────────────────────────────────────
    print(f"  Загружаем agro_operations (season={SEASON_YEAR})...")
    all_ops_raw = get_all("agro_operations", {"season": str(SEASON_YEAR)})
    # Фильтруем только отменённые — дату не режем:
    # сезон в Cropwise начинается осенью предыдущего года,
    # поэтому операции июль-декабрь 2025 относятся к сезону 2026 и должны попасть.
    all_ops = [
        op for op in all_ops_raw
        if str(op.get("status") or "").strip().lower() != "canceled"
    ]
    print(f"    {len(all_ops_raw)} операций загружено, {len(all_ops)} после фильтрации")

    # ── Excel ────────────────────────────────────────────────────────────────
    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    for folder_id, firm_name in AGROFIRMS.items():
        print(f"  → {firm_name}...", end=" ")
        firm_fids = folder_fields.get(folder_id, set())

        # Фильтруем операции этой агрофирмы
        firm_ops = [op for op in all_ops if op.get("field_id") in firm_fids]
        print(f"{len(firm_ops)} операций, {len(firm_fids)} полей")

        rows = _build_rows(
            firm_fids, field_name_map, field_area_map, field_crop_map,
            firm_ops, work_type_map, material_map,
        )
        write_sheet(wb, firm_name, rows)

    output = f"fields_season_{SEASON_YEAR}_{REPORT_DATE}.xlsx"
    wb.save(output)
    print(f"\nСохранено: {output}")


if __name__ == "__main__":
    build_report()