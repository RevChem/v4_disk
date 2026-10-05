"""
cropwise/custom_ops_router.py
==============================
Эндпоинт: произвольный запрос операций по культурам за диапазон дат.
POST /api/custom-ops-query  — возвращает Excel-файл.

Тело запроса (JSON):
  {
    "crops":     ["Соя", "Пшеница озимая"],   # культуры через запятую
    "operation": "2.12 Прикатывание",          # вид работ (подтип)
    "date_from": "2025-04-15",                 # включительно
    "date_to":   "2025-05-15"                  # включительно
  }

Алгоритм идентичен _sync_forbidden_ops_async в tasks.py, но:
  — window дат берётся из запроса (не «двое суток»)
  — фильтрация: crop IN crops AND work_type == operation
  — результат — Excel (BytesIO) с теми же колонками, что в
    таблице «Нестандартное окончание задачи», плюс последняя
    колонка «Ссылка на задание» с гиперссылкой в Excel.
"""
from __future__ import annotations

import asyncio
import io
from collections import defaultdict
from datetime import datetime, date, timedelta, timezone
from typing import List

import aiohttp
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.auth.dependencies import get_current_admin_user
from app.cropwise.client import (
    CROPWISE_HEADERS,
    cropwise_season_years_for_range,
    FIO_NOT_FOUND,
    _fetch_all_pages,
    _fetch_machine_tasks_by_wt,
    _fetch_mtfmi_by_task_ids,
    fetch_local_fio_map,
)
from app.logging_config import get_logger
from app.users.models import User

logger = get_logger("cropwise.custom_ops_router")

router = APIRouter(prefix="/api/custom-ops-query", tags=["Кастомный запрос операций"])

TZ_MSK = timezone(timedelta(hours=3))

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

SEMAPHORE_LIMIT = 5

CROPWISE_TASK_BASE = "https://operations.cropwise.com/machines"

# ── Культуры, которые не должны попадать в отчёт ────────────────────────────
SKIP_CROPS = {
    "Залежь, лес", "В пользовании у фермера",
    "Передано в аренду", "Дороги/лесополосы",
    "Овраги/неудобия", "crop_None",
}


# ---------------------------------------------------------------------------
# Схема запроса
# ---------------------------------------------------------------------------

class CustomOpsRequest(BaseModel):
    crops: List[str]          # список культур
    operations: List[str] = []  # список видов работ (подтипов)
    operation: str = ""         # устаревшее поле для обратной совместимости
    date_from: str            # "YYYY-MM-DD"
    date_to: str              # "YYYY-MM-DD"

    def get_operations(self) -> List[str]:
        ops = self.operations or ([self.operation] if self.operation else [])
        return [o for o in ops if o]


# ---------------------------------------------------------------------------
# Вспомогательные функции (аналог tasks.py)
# ---------------------------------------------------------------------------

def _folder_parent_map(gfs):
    out = {}
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


def parse_dt(s):
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


# ---------------------------------------------------------------------------
# Основная async-функция сбора данных
# ---------------------------------------------------------------------------

async def _fetch_custom_ops(
    crops: list[str],
    operations: list[str],
    date_from: date,
    date_to: date,
) -> list[dict]:
    """
    Возвращает строки: агрофирма, культура, поле, оператор, машина,
    агрегат, вид работ, площадь, task_id, machine_id.

    Принимает список операций — справочники загружаются один раз для всех.
    """
    # обратная совместимость: если передана пустая строка — игнорируем
    operations = [o for o in operations if o]
    season_years = cropwise_season_years_for_range(date_from, date_to)

    # Окно: от 06:00 date_from до 06:00 date_to+1
    window_start = datetime(date_from.year, date_from.month, date_from.day,
                            6, 0, 0, tzinfo=TZ_MSK)
    window_end   = datetime(date_to.year, date_to.month, date_to.day,
                            6, 0, 0, tzinfo=TZ_MSK) + timedelta(days=1)
    window_start_str = window_start.strftime("%Y-%m-%dT%H:%M:%S")
    window_end_str   = window_end.strftime("%Y-%m-%dT%H:%M:%S")

    logger.info(
        f"_fetch_custom_ops: crops={crops}, ops={operations!r}, "
        f"window={window_start_str}–{window_end_str}"
    )

    crops_set = set(crops)

    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS) as session:
        (
            fields_all, fgs_all, gfs_all,
            crops_raw,
            machines_all, users_raw,
            impl_raw, wt_raw,
        ) = await asyncio.gather(
            _fetch_all_pages(session, "fields"),
            _fetch_all_pages(session, "field_groups"),
            _fetch_all_pages(session, "group_folders"),
            _fetch_all_pages(session, "crops"),
            _fetch_all_pages(session, "machines"),
            _fetch_all_pages(session, "users"),
            _fetch_all_pages(session, "implements"),
            _fetch_all_pages(session, "work_types"),
        )
        his_pages = await asyncio.gather(*[
            _fetch_all_pages(session, "history_items", {"year": str(yr)})
            for yr in season_years
        ])
        his_all = [hi for page in his_pages for hi in page]

        wt_name_to_id: dict[str, int] = {}
        for wt in wt_raw:
            wid = wt.get("id")
            if wid is None:
                continue
            name = (wt.get("name") or wt.get("title") or "").strip()
            if name:
                wt_name_to_id[name] = int(wid)

        wt_ids_filter: set[int] = set()
        for op_name in operations:
            wid = wt_name_to_id.get(op_name)
            if wid is not None:
                wt_ids_filter.add(wid)

        mt_params = {
            "end_time_gt_eq": window_start_str,
            "end_time_lt":    window_end_str,
        }
        if wt_ids_filter:
            all_tasks = await _fetch_machine_tasks_by_wt(
                session, wt_ids_filter, mt_params,
                log_prefix="custom_ops ",
            )
        else:
            all_tasks = await _fetch_all_pages(session, "machine_tasks", mt_params)

    logger.info(
        f"Loaded: fields={len(fields_all)}, tasks={len(all_tasks)}, "
        f"crops={len(crops_raw)}"
    )

    # ── Справочники ──────────────────────────────────────────────────────────

    field_name: dict[int, str] = {
        int(f["id"]): (f.get("name") or f.get("title") or f"field_{f['id']}")
        for f in fields_all if f.get("id") is not None
    }

    fgs_map    = {int(fg["id"]): fg for fg in fgs_all if fg.get("id") is not None}
    parent_map = _folder_parent_map(gfs_all)
    root_ids   = set(FOLDER_TO_FIRM.keys())

    field_to_firm: dict[int, str] = {}
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
                field_to_firm[int(fid)] = firm

    crop_id_name = {c["id"]: (c.get("name") or c.get("title", "")) for c in crops_raw}
    field_to_crop: dict[int, str] = {}
    for hi in his_all:
        fid = hi.get("field_id"); cid = hi.get("crop_id")
        if fid and cid and hi.get("active"):
            cname = crop_id_name.get(cid, "")
            if cname:
                field_to_crop[int(fid)] = cname

    machine_name: dict[int, str] = {
        int(m["id"]): (m.get("name") or m.get("title") or m.get("model") or f"machine_{m['id']}")
        for m in machines_all if m.get("id") is not None
    }

    local_fio_by_id = await fetch_local_fio_map()
    operators_map: dict[int, str] = {
        int(u["id"]): (
            local_fio_by_id.get(int(u["id"]))
            or u.get("username") or u.get("email")
            or FIO_NOT_FOUND
        )
        for u in users_raw if u.get("id") is not None
    }

    implements_map: dict[int, dict] = {
        impl["id"]: impl for impl in impl_raw if impl.get("id") is not None
    }

    work_type_map: dict[int, str] = {
        w["id"]: (w.get("name") or w.get("title") or f"wt_{w['id']}") for w in wt_raw
    }

    # ── Фильтруем задачи по operations ───────────────────────────────────────

    # Дедупликация
    seen_ids: set[int] = set()
    unique_tasks = []
    for t in all_tasks:
        tid = t.get("id")
        if tid not in seen_ids:
            seen_ids.add(tid)
            unique_tasks.append(t)

    # Множество допустимых имён операций для быстрого поиска
    ops_set: set[str] = set(operations)

    # Отбор только по нужным операциям (если список пуст — берём все)
    filtered_tasks = [
        t for t in unique_tasks
        if not ops_set or work_type_map.get(t.get("work_type_id"), "") in ops_set
    ]

    logger.info(
        f"Tasks after op-filter {operations!r}: "
        f"{len(filtered_tasks)}/{len(unique_tasks)}"
    )

    if not filtered_tasks:
        return []

    # ── MTFMI ────────────────────────────────────────────────────────────────

    task_ids = [t["id"] for t in filtered_tasks]
    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS) as session:
        mtfmi_map = await _fetch_mtfmi_by_task_ids(session, task_ids)

    # ── Сборка строк ──────────────────────────────────────────────────────────

    rows: list[dict] = []

    for task in filtered_tasks:
        tid     = task["id"]
        mid     = task.get("machine_id")
        wt_id   = task.get("work_type_id")
        wt_name = work_type_map.get(wt_id, "—") if wt_id else "—"
        mach    = machine_name.get(int(mid), f"machine_{mid}") if mid else "—"

        try:
            op_id = int(task["driver_id"]) if task.get("driver_id") is not None else None
        except (TypeError, ValueError):
            op_id = None
        operator_name = operators_map.get(op_id, "—") if op_id else "—"

        impl_id  = task.get("implement_id")
        impl_rec = implements_map.get(impl_id) if impl_id else None
        implement_name = "—"
        if impl_rec:
            implement_name = str(
                impl_rec.get("custom_name") or impl_rec.get("name") or "—"
            ).strip() or "—"

        field_cov: dict[int, float] = defaultdict(float)
        for item in mtfmi_map.get(tid, []):
            fid_raw = item.get("field_id")
            if not fid_raw:
                continue
            fid = int(fid_raw)
            hourly = item.get("covered_area_hourly") or []
            cov = 0.0
            for e in hourly:
                dt = parse_dt(e[0])
                if dt and dt < window_end:
                    cov += float(e[1] or 0)
            if not hourly:
                cov = float(item.get("covered_area") or 0)
            if cov < 0.5:
                continue
            field_cov[fid] += cov

        if not field_cov:
            direct_fid = task.get("field_id")
            if direct_fid:
                field_cov[int(direct_fid)] = float(
                    task.get("completed_area") or task.get("covered_area") or 0
                )

        for fid, area in field_cov.items():
            crop = field_to_crop.get(fid, "—")

            # Исключаем служебные культуры
            if crop in SKIP_CROPS:
                continue

            # Фильтр по культурам (если список непуст — строгий фильтр)
            if crops_set and crop not in crops_set:
                continue

            rows.append({
                "task_id":   tid,
                "machine_id": mid,
                "firm":      field_to_firm.get(fid, "—"),
                "crop":      crop,
                "field":     field_name.get(fid, f"field_{fid}"),
                "operator":  operator_name,
                "machine":   mach,
                "implement": implement_name,
                "work_type": wt_name,
                "area":      round(area, 2),
            })

    logger.info(f"_fetch_custom_ops: {len(rows)} rows")
    return rows


# ---------------------------------------------------------------------------
# Excel builder
# ---------------------------------------------------------------------------

def _build_excel(
    rows: list[dict],
    crops: list[str],
    operation: str,
    date_from: date,
    date_to: date,
) -> bytes:
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except ImportError as e:
        raise RuntimeError(f"openpyxl not installed: {e}")

    _C_HDR   = "2E4DA0"
    _C_TITLE = "1F4E79"
    _C_TOTAL = "BDD7EE"
    _C_ODD   = "FFFFFF"
    _C_EVEN  = "EBF3FB"
    _C_FIRM  = "D9E1F2"

    def _side():
        t = Side(style="thin", color="BBBBBB")
        return Border(left=t, right=t, top=t, bottom=t)

    def _cell(ws, row, col, value=None, bold=False, bg=None, color="000000",
              halign="left", wrap=False, fmt=None, size=9, hyperlink=None):
        c = ws.cell(row=row, column=col, value=value)
        c.font = Font(name="Calibri", size=size, bold=bold, color=color)
        c.alignment = Alignment(horizontal=halign, vertical="center", wrap_text=wrap)
        c.border = _side()
        if bg:
            c.fill = PatternFill("solid", fgColor=bg)
        if fmt:
            c.number_format = fmt
        if hyperlink:
            c.hyperlink = hyperlink
            c.font = Font(name="Calibri", size=size, bold=bold,
                          color="1A6ABF", underline="single")
        return c

    HEADERS = [
        "Агрофирма", "Культура", "Поле", "ФИО водителя",
        "Машина", "Наименование агрегата", "Подтип работ",
        "Обработанная\nплощадь, га", "Ссылка на задание",
    ]
    COL_W = [22, 20, 18, 24, 28, 26, 34, 14, 18]

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Кастомный запрос"

    for ci, w in enumerate(COL_W, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = w

    r = 1
    crops_str = ", ".join(crops) if crops else "все"
    title_text = (
        f"Операция: {operation}  |  Культуры: {crops_str}  |  "
        f"{date_from.strftime('%d.%m.%Y')} – {date_to.strftime('%d.%m.%Y')}"
    )
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(HEADERS))
    c = ws.cell(row=r, column=1, value=title_text)
    c.font = Font(name="Calibri", size=12, bold=True, color="FFFFFF")
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.fill = PatternFill("solid", fgColor=_C_TITLE)
    c.border = _side()
    ws.row_dimensions[r].height = 26
    r += 1

    for ci, h in enumerate(HEADERS, start=1):
        _cell(ws, r, ci, h, bold=True, bg=_C_HDR, color="FFFFFF",
              halign="center", wrap=True, size=9)
    ws.row_dimensions[r].height = 36
    ws.freeze_panes = f"A{r + 1}"
    r += 1

    if not rows:
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(HEADERS))
        c = ws.cell(row=r, column=1, value="Нарушений не обнаружено")
        c.font = Font(name="Calibri", size=10, bold=True, color="375623")
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.fill = PatternFill("solid", fgColor="E2EFDA")
        c.border = _side()
        ws.row_dimensions[r].height = 18
    else:
        def sort_key(row):
            fi = FIRM_ORDER.index(row["firm"]) if row["firm"] in FIRM_ORDER else 999
            return (fi, row["crop"], row["field"], row["machine"])

        rows_sorted = sorted(rows, key=sort_key)

        current_firm  = None
        firm_area_sum = 0.0
        totals_area   = 0.0

        def write_firm_total(end_r, firm, area):
            _cell(ws, end_r, 1, f"Итого {firm}", bold=True, bg=_C_FIRM,
                  halign="right", size=9)
            for ci in range(2, len(HEADERS)):
                _cell(ws, end_r, ci, "", bg=_C_FIRM)
            _cell(ws, end_r, len(HEADERS) - 1, round(area, 2), bold=True,
                  bg=_C_FIRM, halign="right", fmt="# ##0.00", size=9)
            _cell(ws, end_r, len(HEADERS), "", bg=_C_FIRM)
            ws.row_dimensions[end_r].height = 14

        for i, row in enumerate(rows_sorted):
            firm = row["firm"]
            if firm != current_firm:
                if current_firm is not None:
                    write_firm_total(r, current_firm, firm_area_sum)
                    r += 1
                current_firm  = firm
                firm_area_sum = 0.0

            bg = _C_ODD if i % 2 == 0 else _C_EVEN
            _cell(ws, r, 1, row["firm"],      bg=bg)
            _cell(ws, r, 2, row["crop"],      bg=bg)
            _cell(ws, r, 3, row["field"],     bg=bg)
            _cell(ws, r, 4, row["operator"],  bg=bg)
            _cell(ws, r, 5, row["machine"],   bg=bg, size=8)
            _cell(ws, r, 6, row["implement"], bg=bg, size=8)
            _cell(ws, r, 7, row["work_type"], bg=bg)
            _cell(ws, r, 8, row["area"],      bg=bg, halign="right", fmt="# ##0.00")

            # Ссылка на задание
            tid = row.get("task_id")
            mid = row.get("machine_id")
            if tid and mid:
                link = f"https://operations.cropwise.com/machines/{mid}/tasks/{tid}"
                _cell(ws, r, 9, str(tid), bg=bg, hyperlink=link)
            else:
                _cell(ws, r, 9, "—", bg=bg)

            ws.row_dimensions[r].height = 14
            firm_area_sum += row["area"]
            totals_area   += row["area"]
            r += 1

        if current_firm is not None:
            write_firm_total(r, current_firm, firm_area_sum)
            r += 1

        _cell(ws, r, 1, "ИТОГО", bold=True, bg=_C_TOTAL, halign="right", size=10)
        for ci in range(2, len(HEADERS)):
            _cell(ws, r, ci, "", bg=_C_TOTAL)
        _cell(ws, r, len(HEADERS) - 1, round(totals_area, 2), bold=True,
              bg=_C_TOTAL, halign="right", fmt="# ##0.00", size=10)
        _cell(ws, r, len(HEADERS), "", bg=_C_TOTAL)
        ws.row_dimensions[r].height = 16

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# Эндпоинт
# ---------------------------------------------------------------------------

@router.post(
    "",
    summary="Кастомный запрос операций по культурам — Excel",
    description=(
        "Принимает список культур, вид работ и диапазон дат. "
        "Возвращает Excel-файл с операциями за указанный период."
    ),
)
async def post_custom_ops_query(
    body: CustomOpsRequest,
    current_user: User = Depends(get_current_admin_user),
):
    try:
        date_from = date.fromisoformat(body.date_from)
        date_to   = date.fromisoformat(body.date_to)
    except ValueError as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail=f"Неверный формат даты: {e}")

    if date_from > date_to:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="date_from не может быть позже date_to")

    ops = body.get_operations()
    if not ops:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="Укажите хотя бы одну операцию")

    try:
        # Все операции передаются одним вызовом — справочники загружаются один раз
        rows = await _fetch_custom_ops(
            crops=body.crops,
            operations=ops,
            date_from=date_from,
            date_to=date_to,
        )
    except Exception as e:
        logger.error(f"custom_ops_query error: {e}", exc_info=True)
        from fastapi import HTTPException
        raise HTTPException(status_code=502, detail=f"Ошибка получения данных из Cropwise: {e}")

    ops_label = ops[0] if len(ops) == 1 else f"{len(ops)}_операций"
    try:
        xlsx_bytes = _build_excel(rows, body.crops, ops_label, date_from, date_to)
    except Exception as e:
        logger.error(f"excel build error: {e}", exc_info=True)
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=f"Ошибка формирования Excel: {e}")

    from urllib.parse import quote
    fname = (
        f"custom_ops_{ops_label[:20].replace(' ', '_')}_"
        f"{body.date_from}_{body.date_to}.xlsx"
    )
    return StreamingResponse(
        io.BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )