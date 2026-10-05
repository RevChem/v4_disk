"""
cropwise/field_overlaps_router.py
==================================
Эндпоинт: «Пересечения операций на полях».
POST /api/field-overlaps  — возвращает Excel-файл.

Тело запроса (JSON):
  {
    "overlap_sets": [
      ["2.11 Культивация", "4.0 Посев"],
      ["3.1 Опрыскивание", "4.0 Посев"]
    ],
    "date_from": "2025-08-01",
    "date_to":   "2025-08-15"
  }

Логика:
  — Загружаем agro_operations за диапазон дат по всем work_type_id,
    которые входят хотя бы в один запрошенный паттерн.
  — Группируем по (field_id, date) и ищем поля, где одновременно
    присутствуют все категории из паттерна.
  — Для каждой операции подгружаем machine_task (через mtaomi).
  — Длительность = completed_at − started_at (фактические поля machine_task).
  — Ссылка: /machines/{machine_id}/tasks/{task_id}.
  — Результат — Excel: отдельный лист на каждый паттерн.
"""
from __future__ import annotations

import asyncio
import io
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import List

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
    cropwise_season_years_for_range,
    _fetch_agro_operations_by_wt,
    _fetch_all_pages,
    _fetch_mass_request,
    _fetch_mtaomi_by_agro_op_ids,
)
from app.logging_config import get_logger
from app.users.models import User

logger = get_logger("cropwise.field_overlaps_router")

router = APIRouter(prefix="/api/field-overlaps", tags=["Пересечения операций на полях"])

TZ_MSK          = timezone(timedelta(hours=3))
SEMAPHORE_LIMIT = 5
CROPWISE_TASK_BASE = "https://operations.cropwise.com/machines"

FIRM_TO_FOLDER = {
    "АФ Чистопольская": 3, "АгроРаздолье": 4, "Аксу-Агро": 5,
    "НурлатАгро": 11, "Дрожжаное Рассвет Агро": 6,
    "Актай": 7, "Лениногорская Птицефабрика": 9,
}
FOLDER_TO_FIRM = {v: k for k, v in FIRM_TO_FOLDER.items()}
FIRM_ORDER = [
    "АФ Чистопольская", "АгроРаздолье", "Аксу-Агро",
    "НурлатАгро", "Дрожжаное Рассвет Агро", "Актай",
    "Лениногорская Птицефабрика",
]

# ── Справочник операций (name → [work_type_ids]) ─────────────────────────────
WORK_GROUPS: list[tuple[str, list[int]]] = [
    ("1.1 Внесение минеральных удобрений", [55]),
    ("2.1 Боронование",                    [103, 81, 97, 12]),
    ("2.9 Дискование",                     [60]),
    ("2.4 Вспашка",                        [49, 57, 171]),
    ("2.11 Культивация",                   [20, 50, 52, 63, 96]),
    ("4.0 Посев",                          [3, 4, 69, 70, 71, 72, 119, 120, 159, 160, 161, 163]),
    ("4.1 Посев (овощи)",                  [68, 148, 172]),
    ("3.1 Опрыскивание",                   [1, 64, 65, 66, 153]),
    ("3.2 Опрыскивание (овощи)",           [151]),
    ("2.12 Прикатывание",                  [13, 67, 142]),
    ("2.7 Гребнеобразование",              [62]),
    ("2.6 Фрезерование",                   [73]),
    ("2.8 Глубокорыхление",                [11, 106, 121, 150]),
    ("5.0 Уборка",                         [6, 7, 75, 76, 77, 111, 118, 125, 126, 145]),
    ("5.4 Уборка картофеля, лука",         [76]),
]

_WT_TO_GROUP: dict[int, str] = {
    wt_id: name
    for name, ids in WORK_GROUPS
    for wt_id in ids
}

_GROUP_NAMES: list[str] = [name for name, _ in WORK_GROUPS]

# =============================================================================
# Схема запроса
# =============================================================================

class FieldOverlapsRequest(BaseModel):
    overlap_sets: List[List[str]]   # список паттернов, каждый — список категорий
    date_from: str                   # "YYYY-MM-DD"
    date_to:   str                   # "YYYY-MM-DD"


# =============================================================================
# Вспомогательные функции
# =============================================================================

def _parse_dt(s) -> datetime | None:
    if not s:
        return None
    s = str(s).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.replace(tzinfo=TZ_MSK) if dt.tzinfo is None else dt.astimezone(TZ_MSK)


def _calc_duration(started_str, completed_str) -> str:
    """
    Длительность = completed_at − started_at (фактические поля machine_task).
    Возвращает строку 'H:MM:SS' или '—'.
    """
    s = _parse_dt(started_str)
    e = _parse_dt(completed_str)
    if s is None or e is None:
        return "—"
    diff = int((e - s).total_seconds())
    if diff < 0:
        return "—"
    h, rem = divmod(diff, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}"


def _folder_parent_map(gfs: list[dict]) -> dict[int, int | None]:
    out: dict[int, int | None] = {}
    for gf in gfs:
        fid = gf.get("id")
        if fid is None:
            continue
        pid = (gf.get("parent_id") or gf.get("parent_group_folder_id")
               or gf.get("group_folder_parent_id"))
        out[int(fid)] = int(pid) if pid is not None else None
    return out


def _resolve_firm(folder_id, parent_map, root_ids):
    if folder_id is None:
        return None
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


def _build_field_to_firm(fields_all, fgs_all, gfs_all) -> dict[int, str]:
    fgs_map    = {int(fg["id"]): fg for fg in fgs_all if fg.get("id") is not None}
    parent_map = _folder_parent_map(gfs_all)
    root_ids   = set(FOLDER_TO_FIRM.keys())
    out: dict[int, str] = {}
    for f in fields_all:
        fid = f.get("id")
        if fid is None:
            continue
        raw_folder = f.get("group_folder_id")
        fgid = f.get("field_group_id")
        if raw_folder is None and fgid is not None:
            raw_folder = fgs_map.get(int(fgid), {}).get("group_folder_id")
        rint = int(raw_folder) if raw_folder is not None else None
        root = _resolve_firm(rint, parent_map, root_ids)
        if root is not None:
            firm = FOLDER_TO_FIRM.get(root)
            if firm:
                out[int(fid)] = firm
    return out


# =============================================================================
# Async: загрузка agro_operations
# =============================================================================

async def _fetch_agro_ops_for_pattern(
    session: aiohttp.ClientSession,
    wt_ids: set[int],
    date_from: date,
    date_to: date,
    semaphore: asyncio.Semaphore,
) -> list[dict]:
    """agro_operations batch по work_type_id; фильтр по дате — в Python."""
    results: list[dict] = []
    seen: set[int] = set()
    season_years = cropwise_season_years_for_range(date_from, date_to)

    for status in ("done", "in_progress"):
        for season_yr in season_years:
            chunk = await _fetch_agro_operations_by_wt(
                session, wt_ids,
                {"status": status, "season": season_yr},
                sem=semaphore,
                log_prefix="field_overlaps ops ",
            )
            for op in chunk:
                raw = (op.get("completed_date") or op.get("completed_datetime")
                       or op.get("started_date"))
                if raw:
                    try:
                        op_date = date.fromisoformat(str(raw)[:10])
                        if not (date_from <= op_date <= date_to):
                            continue
                    except ValueError:
                        pass

                oid = op.get("id")
                if oid is not None and oid not in seen:
                    seen.add(oid)
                    results.append(op)

    return results


# =============================================================================
# Основная логика
# =============================================================================

async def _run_overlaps(
    overlap_sets: list[list[str]],
    date_from: date,
    date_to: date,
) -> dict[tuple, list[dict]]:
    """
    Возвращает dict: паттерн (tuple) → список строк для Excel.
    """
    # Нужные work_type_id — только те, что фигурируют в паттернах
    needed_groups: set[str] = set()
    for pat in overlap_sets:
        needed_groups.update(pat)

    needed_wt_ids: set[int] = set()
    for name, ids in WORK_GROUPS:
        if name in needed_groups:
            needed_wt_ids.update(ids)

    sem = asyncio.Semaphore(SEMAPHORE_LIMIT)

    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS) as session:
        # 1. Справочники + agro_operations параллельно
        ref_task = asyncio.gather(
            _fetch_all_pages(session, "fields"),
            _fetch_all_pages(session, "field_groups"),
            _fetch_all_pages(session, "group_folders"),
            _fetch_all_pages(session, "machines"),
            _fetch_all_pages(session, "work_types"),
        )
        ops_task = _fetch_agro_ops_for_pattern(
            session, needed_wt_ids, date_from, date_to, sem,
        )

        (
            (fields_all, fgs_all, gfs_all, machines_raw, wt_raw),
            all_ops,
        ) = await asyncio.gather(ref_task, ops_task)

        logger.info(f"field_overlaps: загружено {len(all_ops)} agro_operations")

        # Справочники
        field_to_firm   = _build_field_to_firm(fields_all, fgs_all, gfs_all)
        fgs_name_map    = {
            int(fg["id"]): (fg.get("name") or fg.get("title") or "").strip()
            for fg in fgs_all if fg.get("id") is not None
        }
        field_name_map: dict[int, str] = {}
        field_group_map: dict[int, str] = {}
        for f in fields_all:
            fid = f.get("id")
            if fid is None:
                continue
            field_name_map[int(fid)]  = (f.get("name") or f.get("title") or f"Поле#{fid}").strip()
            fgid = f.get("field_group_id")
            if fgid is not None and int(fgid) in fgs_name_map:
                field_group_map[int(fid)] = fgs_name_map[int(fgid)]

        machine_name: dict[int, str] = {
            int(m["id"]): (m.get("name") or m.get("title") or m.get("model") or f"machine_{m['id']}")
            for m in machines_raw if m.get("id") is not None
        }
        work_type_map: dict[int, str] = {
            w["id"]: (w.get("name") or w.get("title") or f"wt_{w['id']}")
            for w in wt_raw if w.get("id") is not None
        }

        # 2. Поиск пересечений по (field_id, date)
        by_field_date: dict[tuple[int, str], dict[str, list[dict]]] = \
            defaultdict(lambda: defaultdict(list))

        for op in all_ops:
            fid = op.get("field_id")
            if not fid:
                continue
            wt_id = op.get("work_type_id")
            group = _WT_TO_GROUP.get(wt_id) if wt_id else None
            if group is None:
                continue
            raw_date = (op.get("completed_date") or op.get("completed_datetime")
                        or op.get("started_date") or "unknown")
            op_date = str(raw_date)[:10]
            by_field_date[(int(fid), op_date)][group].append(op)

        # 3. Выбираем операции, попавшие в паттерны
        pattern_op_ids: set[int] = set()
        pattern_matches: dict[tuple, list[dict]] = defaultdict(list)

        for pattern in overlap_sets:
            pat_tuple  = tuple(pattern)
            pat_frozen = frozenset(pattern)
            for (fid, op_date), groups_on_field in by_field_date.items():
                present = frozenset(groups_on_field.keys())
                if not pat_frozen.issubset(present):
                    continue
                ops_for_pat: list[dict] = []
                for g in pattern:
                    ops_for_pat.extend(groups_on_field[g])
                for op in ops_for_pat:
                    oid = op.get("id")
                    if oid:
                        pattern_op_ids.add(int(oid))
                pattern_matches[pat_tuple].append({
                    "field_id":   fid,
                    "op_date":    op_date,
                    "pattern":    pat_tuple,
                    "ops":        ops_for_pat,
                    "all_groups": dict(groups_on_field),
                })

        logger.info(f"field_overlaps: операций с пересечениями: {len(pattern_op_ids)}")

        if not pattern_op_ids:
            return {}

        # 4. mtaomi batch по agro_operation_id
        op_to_task_ids = await _fetch_mtaomi_by_agro_op_ids(
            session, list(pattern_op_ids), sem=sem, log_prefix="field_overlaps ",
        )
        mtaomi_map: dict[int, list[dict]] = defaultdict(list)
        for op_id, tids in op_to_task_ids.items():
            for tid in tids:
                mtaomi_map[op_id].append({
                    "agro_operation_id": op_id,
                    "machine_task_id": tid,
                })

        # Собираем task_ids
        op_to_task_ids: dict[int, list[int]] = defaultdict(list)
        all_task_ids: set[int] = set()
        for op_id, records in mtaomi_map.items():
            for rec in records:
                tid = rec.get("machine_task_id")
                if tid:
                    op_to_task_ids[op_id].append(int(tid))
                    all_task_ids.add(int(tid))

        logger.info(f"field_overlaps: загружаем {len(all_task_ids)} machine_tasks...")

        # 5. Загружаем machine_tasks — task_id является собственным id
        #    ресурса machine_tasks, поэтому вместо N отдельных GET-запросов
        #    используем POST .../machine_tasks/mass_request (тот же приём,
        #    что уже применяется в unclosed_ops_router.py).
        task_records = await _fetch_mass_request(
            session, "machine_tasks", sorted(all_task_ids), sem=sem,
        )
        task_map: dict[int, dict] = {
            int(t["id"]): t for t in task_records if t.get("id") is not None
        }

    # 6. Строим плоские строки для Excel
    result_rows: dict[tuple, list[dict]] = {}

    for pattern, matches in pattern_matches.items():
        rows: list[dict] = []

        for m in matches:
            fid    = m["field_id"]
            firm   = field_to_firm.get(fid, "—")
            fname  = field_name_map.get(fid, f"Поле#{fid}")
            fgroup = field_group_map.get(fid, "—")
            op_date = m["op_date"]

            for op in m["ops"]:
                wt_id    = op.get("work_type_id")
                wt_name  = work_type_map.get(wt_id, f"wt#{wt_id}") if wt_id else "—"
                covered  = op.get("covered_area")
                cov_str  = f"{float(covered):.2f}" if covered is not None else "—"
                oid      = op.get("id")
                task_ids = op_to_task_ids.get(int(oid), []) if oid else []

                if task_ids:
                    for tid in task_ids:
                        t = task_map.get(tid)
                        if not t:
                            continue
                        mid   = t.get("machine_id")
                        mname = machine_name.get(int(mid), f"machine_{mid}") if mid else "—"

                        # ✓ Длительность: completed_at − started_at (фактические поля)
                        dur  = _calc_duration(t.get("started_at"), t.get("completed_at"))

                        # ✓ Ссылка: /machines/{machine_id}/tasks/{task_id}
                        link = (f"{CROPWISE_TASK_BASE}/{mid}/tasks/{tid}"
                                if mid else None)

                        rows.append({
                            "firm":        firm,
                            "field_group": fgroup,
                            "field_name":  fname,
                            "op_date":     op_date,
                            "work_type":   wt_name,
                            "machine":     mname,
                            "covered_ha":  cov_str,
                            "duration":    dur,
                            "task_id":     tid,
                            "machine_id":  mid,
                            "link":        link,
                            "op_id":       oid,
                        })
                else:
                    rows.append({
                        "firm":        firm,
                        "field_group": fgroup,
                        "field_name":  fname,
                        "op_date":     op_date,
                        "work_type":   wt_name,
                        "machine":     "—",
                        "covered_ha":  cov_str,
                        "duration":    "—",
                        "task_id":     None,
                        "machine_id":  None,
                        "link":        None,
                        "op_id":       oid,
                    })

        def _sort_key(row):
            fi = FIRM_ORDER.index(row["firm"]) if row["firm"] in FIRM_ORDER else 999
            return (fi, row["field_group"], row["field_name"], row["op_date"], row["work_type"])

        rows.sort(key=_sort_key)
        result_rows[pattern] = rows

    return result_rows


# =============================================================================
# Стили Excel
# =============================================================================

_C_HDR        = "2E4DA0"
_C_TITLE      = "1F3864"
_C_FIRM       = "D9E1F2"
_C_FIELD_ODD  = "FFFFFF"
_C_FIELD_EVEN = "FFF2CC"
_C_TOTAL      = "BDD7EE"

HEADERS = [
    "Группа полей",
    "Поле",
    "Вид работ",
    "Машина",
    "Обработанная площадь, га",
    "Продолжительность работы",
    "№ задания",
]
COL_W = [22, 26, 42, 36, 20, 20, 14]


def _side() -> Border:
    t = Side(style="thin", color="BBBBBB")
    return Border(left=t, right=t, top=t, bottom=t)


def _cell(ws, row: int, col: int, value=None,
          bold=False, bg: str | None = None, color="000000",
          halign="left", wrap=False, size=9, hyperlink: str | None = None):
    c = ws.cell(row=row, column=col, value=value)
    c.font      = Font(name="Calibri", size=size, bold=bold,
                       color="1A6ABF" if hyperlink else color,
                       underline="single" if hyperlink else None)
    c.alignment = Alignment(horizontal=halign, vertical="center", wrap_text=wrap)
    c.border    = _side()
    if bg:
        c.fill = PatternFill("solid", fgColor=bg)
    if hyperlink:
        c.hyperlink = hyperlink


def _write_sheet(wb, sheet_name: str, pattern: tuple, rows: list[dict],
                 date_from: date, date_to: date, generated_at: datetime) -> None:
    ws = wb.create_sheet(title=sheet_name[:31])

    for ci, w in enumerate(COL_W, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = w

    r = 1

    # Заголовок листа
    ts_str   = generated_at.strftime("%d.%m.%Y %H:%M")
    df_str   = date_from.strftime("%d.%m.%Y")
    dt_str   = date_to.strftime("%d.%m.%Y")
    pat_str  = " + ".join(pattern)
    title_text = f"Пересечения: {pat_str}  |  {df_str} — {dt_str}  |  сформировано {ts_str}"

    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(HEADERS))
    c = ws.cell(row=r, column=1, value=title_text)
    c.font      = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.fill      = PatternFill("solid", fgColor=_C_TITLE)
    c.border    = _side()
    ws.row_dimensions[r].height = 28
    r += 1

    # Шапка колонок
    for ci, h in enumerate(HEADERS, start=1):
        _cell(ws, r, ci, h, bold=True, bg=_C_HDR, color="FFFFFF",
              halign="center", wrap=True, size=9)
    ws.row_dimensions[r].height = 36
    ws.freeze_panes = f"A{r + 1}"
    r += 1

    if not rows:
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(HEADERS))
        c = ws.cell(row=r, column=1, value="Пересечений не найдено")
        c.font      = Font(name="Calibri", size=10, bold=True, color="375623")
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.fill      = PatternFill("solid", fgColor="E2EFDA")
        c.border    = _side()
        ws.row_dimensions[r].height = 18
        return

    current_firm  = None
    current_field = None
    field_alt     = False
    firm_cnt      = 0
    total_cnt     = 0

    for row in rows:
        firm  = row["firm"]
        field = row["field_name"]

        if firm != current_firm:
            if current_firm is not None:
                _cell(ws, r, 1, f"Итого {current_firm}: {firm_cnt} строк",
                      bold=True, bg=_C_TOTAL, halign="right", size=9)
                for ci in range(2, len(HEADERS) + 1):
                    _cell(ws, r, ci, "", bg=_C_TOTAL)
                ws.row_dimensions[r].height = 14
                r += 1
            current_firm  = firm
            current_field = None
            firm_cnt      = 0
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(HEADERS))
            _cell(ws, r, 1, firm, bold=True, bg=_C_FIRM, size=10)
            ws.row_dimensions[r].height = 16
            r += 1

        if field != current_field:
            if current_field is not None:
                field_alt = not field_alt
            current_field = field

        bg = _C_FIELD_ODD if not field_alt else _C_FIELD_EVEN

        _cell(ws, r, 1, row["field_group"], bg=bg)
        _cell(ws, r, 2, row["field_name"],  bg=bg)
        _cell(ws, r, 3, row["work_type"],   bg=bg)
        _cell(ws, r, 4, row["machine"],     bg=bg, size=8)
        _cell(ws, r, 5, row["covered_ha"],  bg=bg, halign="right")
        _cell(ws, r, 6, row["duration"],    bg=bg, halign="center")

        # ✓ Ссылка по аналогии с панелью «Нарушения»: task_id как текст + hyperlink
        tid  = row.get("task_id")
        link = row.get("link")
        if tid and link:
            _cell(ws, r, 7, str(tid), bg=bg, halign="center", hyperlink=link)
        else:
            _cell(ws, r, 7, str(tid) if tid else "—", bg=bg, halign="center")

        ws.row_dimensions[r].height = 14
        r += 1
        firm_cnt  += 1
        total_cnt += 1

    if current_firm is not None:
        _cell(ws, r, 1, f"Итого {current_firm}: {firm_cnt} строк",
              bold=True, bg=_C_TOTAL, halign="right", size=9)
        for ci in range(2, len(HEADERS) + 1):
            _cell(ws, r, ci, "", bg=_C_TOTAL)
        ws.row_dimensions[r].height = 14
        r += 1

    _cell(ws, r, 1, f"ИТОГО: {total_cnt} строк", bold=True, bg=_C_HDR,
          color="FFFFFF", halign="right", size=10)
    for ci in range(2, len(HEADERS) + 1):
        _cell(ws, r, ci, "", bg=_C_HDR)
    ws.row_dimensions[r].height = 16

    logger.info(f"field_overlaps: лист «{sheet_name}»: {total_cnt} строк")


# =============================================================================
# Эндпоинт
# =============================================================================

@router.post("")
async def field_overlaps_query(
    body: FieldOverlapsRequest,
    _: User = Depends(get_current_admin_user),
):
    date_from = date.fromisoformat(body.date_from)
    date_to   = date.fromisoformat(body.date_to)

    if date_from > date_to:
        raise HTTPException(status_code=400, detail="date_from не может быть позже date_to")

    # Валидируем паттерны
    valid_names = {name for name, _ in WORK_GROUPS}
    for pat in body.overlap_sets:
        for g in pat:
            if g not in valid_names:
                raise HTTPException(status_code=400, detail=f"Неизвестная категория: {g!r}")
        if len(pat) < 2:
            raise HTTPException(status_code=400, detail="Каждый паттерн должен содержать минимум 2 категории")

    logger.info(f"field_overlaps: {body.overlap_sets}, {date_from}–{date_to}")

    rows_by_pattern = await _run_overlaps(body.overlap_sets, date_from, date_to)

    now = datetime.now(tz=TZ_MSK)
    wb  = Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    for pat_list in body.overlap_sets:
        pat_tuple  = tuple(pat_list)
        rows       = rows_by_pattern.get(pat_tuple, [])
        sheet_name = " + ".join(
            p.split(" ", 1)[1] if " " in p else p for p in pat_list
        )
        _write_sheet(wb, sheet_name, pat_tuple, rows, date_from, date_to, now)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    filename = f"field_overlaps_{date_from}_{date_to}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# Отдаём список доступных категорий (для фронтенда)
@router.get("/groups")
async def get_overlap_groups(_: User = Depends(get_current_admin_user)):
    return {"groups": _GROUP_NAMES}
