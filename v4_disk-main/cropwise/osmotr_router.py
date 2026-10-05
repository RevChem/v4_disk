"""
cropwise/osmotr_router.py
==========================
Эндпоинт: «Осмотр полей» — сводка осмотров полей агрономами за период.
POST /api/osmotr-polej — возвращает Excel-файл.

Тело запроса (JSON):
  {
    "date_from":    "2026-06-08",
    "date_to":      "2026-06-14",
    "season_start": "2026-01-01",
    "season_end":   "2026-06-14"
  }

Логика (адаптировано из автономного скрипта osmotr.py):
  — date_from / date_to — короткий «период», за который считаются
    отдельные «недельные» показатели отчётов осмотра.
  — season_start / season_end — границы «сезона», кумулятивного
    показателя; задаются явно в модальном окне (раньше вычислялись
    автоматически как 1 января текущего года).
  — Активные поля считаются на дату as_of = max(date_to, season_end) —
    самую позднюю из выбранных границ — через inventory_history_items.
  — FieldScoutReports загружаются один раз за весь сезон, затем делятся
    на «период» и «сезон» по времени отчёта.
  — Привязка полей к агрофирмам — через group_folders/field_groups,
    с принудительными правилами по имени поля (FIELD_NAME_FORCE_RULES).
  — Результат — Excel: один лист «Свод по осмотрам полей».
"""
from __future__ import annotations

import asyncio
import io
from collections import defaultdict
from datetime import date, datetime, timedelta

import aiohttp
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.auth.dependencies import get_current_admin_user
from app.cropwise.client import (
    CROPWISE_HEADERS,
    FIO_NOT_FOUND,
    _fetch_all_pages,
    fetch_local_fio_map,
)
from app.cropwise.plans_1 import (
    FIELD_NAME_FORCE_RULES,
    FIRM_ORDER,
    FOLDER_TO_FIRM,
    TZ_MSK,
)
from app.logging_config import get_logger
from app.users.models import User

logger = get_logger("cropwise.osmotr_router")

router = APIRouter(prefix="/api/osmotr-polej", tags=["Осмотр полей"])

SEMAPHORE_LIMIT = 5


# =============================================================================
# Схема запроса
# =============================================================================

class OsmotrRequest(BaseModel):
    date_from:    str   # "YYYY-MM-DD" — начало периода (короткое окно)
    date_to:      str   # "YYYY-MM-DD" — конец периода
    season_start: str   # "YYYY-MM-DD" — начало сезона (кумулятивное окно)
    season_end:   str   # "YYYY-MM-DD" — конец сезона / дата, на которую считаются активные поля


# =============================================================================
# Привязка полей к агрофирмам (как в osmotr.py / 2_main_ideal_compact)
# =============================================================================

def _norm(name: str | None) -> str:
    return (name or "").strip().casefold().replace(" ", "").replace("\u00a0", "")


def _folder_parent_map(gfs_all: list[dict]) -> dict[int, int | None]:
    parent: dict[int, int | None] = {}
    for gf in gfs_all:
        fid = gf.get("id")
        if fid is None:
            continue
        pid = (
            gf.get("parent_id")
            or gf.get("parent_group_folder_id")
            or gf.get("group_folder_parent_id")
        )
        parent[int(fid)] = int(pid) if pid is not None else None
    return parent


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


def _lpf_hints(gfs_all: list[dict]) -> dict[int, str]:
    hints: dict[int, str] = {}
    for gf in gfs_all:
        fid = gf.get("id")
        if fid is None:
            continue
        blob = _norm(f"{gf.get('name') or ''} {gf.get('title') or ''}")
        if any(x in blob for x in ("лениногор", "птицефабрик", "лпф", "lpf")):
            hints[int(fid)] = "Лениногорская Птицефабрика"
    return hints


def _firm_from_field_name(field_rec: dict) -> str | None:
    blob = _norm(f"{field_rec.get('name') or ''} {field_rec.get('title') or ''}")
    if not blob:
        return None
    for needle, firm_key in FIELD_NAME_FORCE_RULES:
        if needle in blob:
            return firm_key
    return None


def _build_field_to_firm(
    fields_all: list[dict],
    fgs_all: list[dict],
    gfs_all: list[dict],
) -> dict[int, str]:
    fgs_map = {fg["id"]: fg for fg in fgs_all if fg.get("id") is not None}
    parent_map = _folder_parent_map(gfs_all)
    root_ids = set(FOLDER_TO_FIRM.keys())
    gf_hints = _lpf_hints(gfs_all)

    field_to_firm: dict[int, str] = {}

    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        fgid = f.get("field_group_id")
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
            cur = rint
            seen: set[int] = set()
            for _ in range(64):
                if cur is None:
                    break
                if cur in root_ids:
                    firm_key = FOLDER_TO_FIRM.get(cur)
                    break
                if cur in seen:
                    break
                seen.add(cur)
                cur = parent_map.get(cur)

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


# =============================================================================
# Время
# =============================================================================

def _parse_report_dt(raw) -> datetime | None:
    """Парсит время отчёта и возвращает datetime в TZ_MSK."""
    if raw is None:
        return None
    s = str(raw).strip()
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


def _day_start(d: date) -> datetime:
    """00:00 заданного дня — начало календарных суток."""
    return datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=TZ_MSK)


def _day_end(d: date) -> datetime:
    """00:00 следующего дня — конец календарных суток (не включительно)."""
    return _day_start(d) + timedelta(days=1)


# =============================================================================
# Загрузка данных
# =============================================================================

async def _load_data(
    session: aiohttp.ClientSession,
    as_of: date,
    fetch_from: date,
    fetch_to: date,
    sem: asyncio.Semaphore,
) -> tuple[
    dict[int, str],          # user_id -> full_name
    dict[int, str],          # field_id -> firm_name
    dict[int, float],        # field_id -> area_ha
    dict[str, set[int]],     # firm -> set of field_ids (активные на `as_of`)
    list[dict],              # отчёты осмотра за весь запрошенный диапазон
]:
    users_all, fields_all, fgs_all, gfs_all = await asyncio.gather(
        _fetch_all_pages(session, "users", sem=sem),
        _fetch_all_pages(session, "fields", sem=sem),
        _fetch_all_pages(session, "field_groups", sem=sem),
        _fetch_all_pages(session, "group_folders", sem=sem),
    )

    local_fio_by_id = await fetch_local_fio_map()
    user_name: dict[int, str] = {}
    for u in users_all:
        uid = u.get("id")
        if uid is None:
            continue
        uid = int(uid)
        local_fio = local_fio_by_id.get(uid)
        if local_fio:
            user_name[uid] = local_fio
            continue
        parts = [u.get("last_name") or "", u.get("first_name") or "", u.get("middle_name") or ""]
        full = " ".join(p for p in parts if p).strip()
        if not full:
            full = u.get("username") or u.get("email") or FIO_NOT_FOUND
        user_name[int(uid)] = full

    field_to_firm = _build_field_to_firm(fields_all, fgs_all, gfs_all)

    field_area: dict[int, float] = {}
    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        field_area[int(fid)] = float(f.get("tillable_area") or f.get("calculated_area") or 0.0)

    # Активные поля на `as_of` через inventory_history_items
    ihi_all = await _fetch_all_pages(
        session, "inventory_history_items", {"historyable_type": "Field"}, sem=sem,
    )
    active_as_of: set[int] = set()
    for r in ihi_all:
        if not r.get("available"):
            continue
        fid = r.get("historyable_id")
        if fid is None:
            continue
        try:
            ev_start = date.fromisoformat(str(r.get("event_start_at", ""))[:10])
        except ValueError:
            continue
        raw_end = r.get("event_end_at")
        try:
            ev_end = date.fromisoformat(str(raw_end)[:10]) if raw_end else None
        except ValueError:
            ev_end = None
        if ev_start <= as_of and (ev_end is None or ev_end >= as_of):
            active_as_of.add(int(fid))

    firm_active_fields: dict[str, set[int]] = defaultdict(set)
    for fid in active_as_of:
        firm = field_to_firm.get(fid)
        if firm:
            firm_active_fields[firm].add(fid)

    # FieldScoutReports за весь запрошенный диапазон (период + сезон вместе);
    # деление на period/season происходит позже, в aggregate()
    fetch_from_dt = _day_start(fetch_from)
    fetch_to_dt = _day_end(fetch_to)
    reports_all = await _fetch_all_pages(
        session,
        "field_scout_reports",
        {
            "created_at_gteq": fetch_from_dt.isoformat(),
            "created_at_lteq": fetch_to_dt.isoformat(),
        },
        sem=sem,
    )

    return user_name, field_to_firm, field_area, firm_active_fields, reports_all


# =============================================================================
# Агрегация
# =============================================================================

def _aggregate(
    user_name: dict[int, str],
    field_to_firm: dict[int, str],
    reports_all: list[dict],
    period_start: date,
    period_end: date,
    season_start: date,
    season_end: date,
) -> dict[str, dict[str, dict]]:
    """
    Возвращает структуру:
      firm -> agronomist_name -> {
        period_reports, period_fields, season_reports, season_fields
      }
    """
    period_start_dt = _day_start(period_start)
    period_end_dt = _day_end(period_end)
    season_start_dt = _day_start(season_start)
    season_end_dt = _day_end(season_end)

    result: dict[str, dict[str, dict]] = defaultdict(
        lambda: defaultdict(lambda: {
            "period_reports": 0,
            "period_fields": set(),
            "season_reports": 0,
            "season_fields": set(),
        })
    )

    for rep in reports_all:
        uid = rep.get("user_id")
        fid = rep.get("field_id")
        if uid is None or fid is None:
            continue

        firm = field_to_firm.get(int(fid))
        if not firm:
            continue

        agro_name = user_name.get(int(uid), "ФИО не обнаружено")

        raw_dt = rep.get("report_time") or rep.get("created_at")
        rep_dt = _parse_report_dt(raw_dt)
        if rep_dt is None:
            continue

        agg = result[firm][agro_name]

        if season_start_dt <= rep_dt < season_end_dt:
            agg["season_reports"] += 1
            agg["season_fields"].add(int(fid))

        if period_start_dt <= rep_dt < period_end_dt:
            agg["period_reports"] += 1
            agg["period_fields"].add(int(fid))

    return result


# =============================================================================
# Excel
# =============================================================================

def _side(style="thin") -> Side:
    return Side(border_style=style, color="000000")


def _border_thin() -> Border:
    s = _side()
    return Border(left=s, right=s, top=s, bottom=s)


def _fill(hex_color: str) -> PatternFill:
    return PatternFill("solid", start_color=hex_color, fgColor=hex_color)


def _font(bold=False, size=9, color="000000") -> Font:
    return Font(name="Arial", bold=bold, size=size, color=color)


def _align(horizontal="center", vertical="center", wrap=True) -> Alignment:
    return Alignment(horizontal=horizontal, vertical=vertical, wrap_text=wrap)


def _sc(cell, fill=None, font=None, align=None, border=None):
    if fill:
        cell.fill = fill
    if font:
        cell.font = font
    if align:
        cell.alignment = align
    if border:
        cell.border = border


_FILL_TITLE = _fill("1F497D")
_FILL_FIRM  = _fill("4472C4")
_FILL_HDR   = _fill("BDD7EE")
_FILL_AGRO  = _fill("FFFFFF")
_FILL_TOTAL = _fill("D9E1F2")
_FILL_ITOGO = _fill("9DC3E6")

_FONT_TITLE = _font(bold=True, size=11, color="FFFFFF")
_FONT_FIRM  = _font(bold=True, size=10, color="FFFFFF")
_FONT_HDR   = _font(bold=True, size=9)
_FONT_DATA  = _font(bold=False, size=9)
_FONT_TOTAL = _font(bold=True, size=9)

_ALIGN_C  = _align("center")
_ALIGN_L  = _align("left")
_ALIGN_CH = _align("center", wrap=True)

_BORDER = _border_thin()


def _write_excel(
    result: dict[str, dict[str, dict]],
    firm_active_fields: dict[str, set[int]],
    field_area: dict[int, float],
    period_start: date,
    period_end: date,
    season_start: date,
    season_end: date,
    as_of: date,
) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Свод по осмотрам полей"

    # --- Заголовок ---
    title = (
        f"Отчёт осмотров полей пользователями за период "
        f"с {period_start.strftime('%d.%m.%Y')} по {period_end.strftime('%d.%m.%Y')} "
        f"(сезон: с {season_start.strftime('%d.%m.%Y')} по {season_end.strftime('%d.%m.%Y')})"
    )
    c = ws["A1"]
    c.value = title
    _sc(c, fill=_FILL_TITLE, font=_FONT_TITLE, align=_ALIGN_C)
    for ci in range(2, 11):
        _sc(ws.cell(row=1, column=ci), fill=_FILL_TITLE)
    ws.merge_cells("A1:J1")
    ws.row_dimensions[1].height = 22

    # --- Шапка ---
    headers = [
        "№",
        "Агроном / Агрофирма",
        f"Кол-во полей\n(активных на {as_of.strftime('%d.%m.%Y')})",
        f"Площадь полей, га\n(активных на {as_of.strftime('%d.%m.%Y')})",
        f"Отчётов\nс {period_start.strftime('%d.%m')} по {period_end.strftime('%d.%m.%Y')}",
        f"Осмотрено полей\nс {period_start.strftime('%d.%m')} по {period_end.strftime('%d.%m.%Y')}",
        "% от кол-ва полей\n(период)",
        f"Отчётов\nс {season_start.strftime('%d.%m')} по {season_end.strftime('%d.%m.%Y')}",
        "Осмотрено полей\n(сезон)",
        "% от кол-ва полей\n(сезон)",
    ]
    hdr_row = 2
    for ci, h in enumerate(headers, 1):
        c = ws.cell(row=hdr_row, column=ci, value=h)
        _sc(c, fill=_FILL_HDR, font=_FONT_HDR, align=_ALIGN_CH, border=_BORDER)
    ws.row_dimensions[hdr_row].height = 42

    row = 3
    num = 0

    grand_fields_total = 0
    grand_area_total = 0.0
    grand_period_rep = 0
    grand_period_fields: set[int] = set()
    grand_season_rep = 0
    grand_season_fields: set[int] = set()

    for firm in FIRM_ORDER:
        agros = result.get(firm, {})
        active_fields = firm_active_fields.get(firm, set())
        n_fields = len(active_fields)
        area_ha = sum(field_area.get(fid, 0.0) for fid in active_fields)

        c_a = ws.cell(row=row, column=1, value=None)
        _sc(c_a, fill=_FILL_FIRM, font=_FONT_FIRM, align=_ALIGN_C, border=_BORDER)
        c_b = ws.cell(row=row, column=2, value=firm)
        _sc(c_b, fill=_FILL_FIRM, font=_FONT_FIRM, align=_ALIGN_L, border=_BORDER)
        for ci in range(3, 11):
            c = ws.cell(row=row, column=ci, value=None)
            _sc(c, fill=_FILL_FIRM, font=_FONT_FIRM, align=_ALIGN_C, border=_BORDER)
        ws.merge_cells(f"B{row}:J{row}")
        ws.row_dimensions[row].height = 16
        row += 1

        firm_period_rep = 0
        firm_period_fields: set[int] = set()
        firm_season_rep = 0
        firm_season_fields: set[int] = set()

        for agro_name in sorted(agros.keys()):
            agg = agros[agro_name]
            p_fields = len(agg["period_fields"])
            s_fields = len(agg["season_fields"])

            if agg["period_reports"] == 0 and p_fields == 0 and agg["season_reports"] == 0 and s_fields == 0:
                continue

            num += 1
            pct_period = round(p_fields / n_fields * 100, 2) if n_fields else 0.0
            pct_season = round(s_fields / n_fields * 100, 2) if n_fields else 0.0

            row_vals = [
                num,
                agro_name,
                "",
                "",
                agg["period_reports"],
                p_fields,
                pct_period,
                agg["season_reports"],
                s_fields,
                pct_season,
            ]
            for ci, v in enumerate(row_vals, 1):
                c = ws.cell(row=row, column=ci, value=v)
                al = _ALIGN_L if ci == 2 else _ALIGN_C
                _sc(c, fill=_FILL_AGRO, font=_FONT_DATA, align=al, border=_BORDER)
            ws.row_dimensions[row].height = 15
            row += 1

            firm_period_rep += agg["period_reports"]
            firm_period_fields |= agg["period_fields"]
            firm_season_rep += agg["season_reports"]
            firm_season_fields |= agg["season_fields"]

        fp = len(firm_period_fields)
        fs = len(firm_season_fields)
        pct_p = round(fp / n_fields * 100, 2) if n_fields else 0.0
        pct_s = round(fs / n_fields * 100, 2) if n_fields else 0.0
        itog_vals = [
            "", f"Итого {firm}",
            n_fields, round(area_ha, 1),
            firm_period_rep, fp, pct_p,
            firm_season_rep, fs, pct_s,
        ]
        for ci, v in enumerate(itog_vals, 1):
            c = ws.cell(row=row, column=ci, value=v)
            al = _ALIGN_L if ci == 2 else _ALIGN_C
            _sc(c, fill=_FILL_ITOGO, font=_FONT_TOTAL, align=al, border=_BORDER)
        ws.row_dimensions[row].height = 15
        row += 1

        grand_fields_total += n_fields
        grand_area_total += area_ha
        grand_period_rep += firm_period_rep
        grand_period_fields |= firm_period_fields
        grand_season_rep += firm_season_rep
        grand_season_fields |= firm_season_fields

    gfp = len(grand_period_fields)
    gfs = len(grand_season_fields)
    pct_gp = round(gfp / grand_fields_total * 100, 2) if grand_fields_total else 0.0
    pct_gs = round(gfs / grand_fields_total * 100, 2) if grand_fields_total else 0.0
    grand_vals = [
        "", "Итого:",
        grand_fields_total, round(grand_area_total, 1),
        grand_period_rep, gfp, pct_gp,
        grand_season_rep, gfs, pct_gs,
    ]
    for ci, v in enumerate(grand_vals, 1):
        c = ws.cell(row=row, column=ci, value=v)
        al = _ALIGN_L if ci == 2 else _ALIGN_C
        _sc(c, fill=_FILL_TOTAL, font=_FONT_TOTAL, align=al, border=_BORDER)
    ws.row_dimensions[row].height = 16

    col_widths = [5, 36, 14, 16, 14, 16, 14, 14, 16, 14]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    ws.freeze_panes = "A3"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# =============================================================================
# Эндпоинт
# =============================================================================

@router.post("")
async def osmotr_polej_query(
    body: OsmotrRequest,
    _: User = Depends(get_current_admin_user),
):
    try:
        period_start = date.fromisoformat(body.date_from)
        period_end = date.fromisoformat(body.date_to)
        season_start = date.fromisoformat(body.season_start)
        season_end = date.fromisoformat(body.season_end)
    except ValueError:
        raise HTTPException(status_code=400, detail="Неверный формат даты, ожидается YYYY-MM-DD")

    if period_start > period_end:
        raise HTTPException(status_code=400, detail="«Дата с» не может быть позже «Дата по»")
    if season_start > season_end:
        raise HTTPException(status_code=400, detail="«Начало сезона» не может быть позже «Конца сезона»")

    # Дата, на которую считаются активные поля — самая поздняя из заданных границ
    as_of = max(period_end, season_end)
    # Диапазон, за который нужно подтянуть отчёты осмотра (период + сезон вместе)
    fetch_from = min(period_start, season_start)
    fetch_to = max(period_end, season_end)

    logger.info(
        f"osmotr_polej: период {period_start}–{period_end}, "
        f"сезон {season_start}–{season_end}, активные поля на {as_of}"
    )

    sem = asyncio.Semaphore(SEMAPHORE_LIMIT)
    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS) as session:
        user_name, field_to_firm, field_area, firm_active_fields, reports_all = await _load_data(
            session, as_of, fetch_from, fetch_to, sem,
        )

    result = _aggregate(
        user_name, field_to_firm, reports_all,
        period_start, period_end, season_start, season_end,
    )
    xlsx_bytes = _write_excel(
        result, firm_active_fields, field_area,
        period_start, period_end, season_start, season_end, as_of,
    )

    filename = f"osmotr_polej_{period_start}_{period_end}.xlsx"
    return StreamingResponse(
        io.BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
