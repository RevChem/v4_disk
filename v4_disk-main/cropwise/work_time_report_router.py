"""
cropwise/work_time_report_router.py
====================================
Отчёт «Информация по времени работы» в разрезе агрофирм за период —
воспроизводит формат ручного Excel-отчёта, который раньше собирали руками:
один лист на агрофирму, внутри — блоки по агрономическим суткам
(6:00 текущего дня — 5:59:59 следующего, МСК), в каждом блоке по одной
строке на пару «машина + поле»:

    Техника | Поле | Время начала работ | Время окончания работ |
    Время переезда, общее | Время простоя согласно кропвайс |
    Причина простоя с чат-бот | Обработанная площадь, га

Источники данных:
  - Cropwise machine_tasks + machine_task_field_mapping_items (MTFMI) —
    даёт точное время въезда/выезда по каждому полю (из covered_area_hourly)
    и обработанную площадь. Логика идентична violations_sync.py (check4/check5).
  - «Время простоя согласно кропвайс» — берётся напрямую из готового поля
    stops_timetable каждого MTFMI-элемента (список интервалов простоя,
    которые Кропвайс уже сам вычислил и показывает в карточке задания UI).
    Ничего не пересчитываем, только суммируем интервалы этого поля.
    Сверено вручную по task_id=398063 (27.06.2026) — совпадает с UI 1:1.
  - «Причина простоя с чат-бот» — сначала смотрим в таблицу downtime
    (актуальные данные: водитель фиксирует причину сразу через «Начало
    простоя»/«Завершить простой», без задержки), сопоставляя по госномеру
    машины (через vehicles) и пересечению интервала с временем визита на
    поле; затем ДОПОЛНЯЕМ причинами из cropwise_stops (панель «Простои» —
    туда причина может лечь позже, либо быть проставлена только там, если
    водитель отметил её на странице «Мои простои из Cropwise», а не через
    «Начало простоя»). Раньше смотрели только в cropwise_stops, из-за чего
    часть реально имеющихся причин из downtime не попадала в отчёт — если
    запись в cropwise_stops к моменту генерации отчёта ещё не дошла (сама
    затягивается тем, что подхватывает downtime только когда водитель
    открывает свою страницу — см. _autofill_my_stops_from_downtime в
    driver_stops/router.py). Сейчас же попадают из обоих источников.
    Сейчас туда попадают только водители АФ Чистопольской — поэтому
    для остальных агрофирм колонка будет пустой, как и в ручном отчёте.
  - «Время переезда, общее» — сумма готового поля movements_on_road_duration
    (в секундах) каждого machine_task этой машины за агрономические сутки —
    Кропвайс уже сам считает время «на дорогах» на уровне задания, поэтому
    вычислять разрывы между полями вручную не нужно. Раньше здесь считали
    сумму промежутков между выездом с одного поля и въездом на следующее —
    это давало неверные цифры; сверено вручную по task_id=398063
    (27.06.2026, эталон из UI «На дорогах» 01:18:34) — совпадает 1:1.
    Один и тот же итог проставляется в каждую строку машины за этот день
    (как и в исходном отчёте).
    NB: в документации Cropwise API это поле называется
    engine_work_duration_on_road, но фактически API отдаёт его под именем
    movements_on_road_duration — заведено на реальном ответе API, не на доке.

POST /api/work-time-report/excel
"""
from __future__ import annotations

import asyncio
import io
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

import aiohttp
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from sqlalchemy import select

from app.auth.dependencies import get_current_admin_user
from app.cropwise.client import (
    CROPWISE_HEADERS,
    cropwise_season_years_for_range,
    _fetch_all_pages,
    _fetch_machine_tasks_by_wt,
    _fetch_mtfmi_by_task_ids,
    _CROPWISE_TIMEOUT,
)
from app.cropwise.plans_1 import WORK_GROUPS, FIRM_TO_FOLDER, FOLDER_TO_FIRM, FIRM_ORDER
from app.logging_config import get_logger
from app.users.models import User
from app.database import async_session_maker
from app.driver_stops.models import CropwiseStop
from app.driver_stops.dao import CropwiseStopDAO
from app.venchies.models import Vehicle
from app.downtime.schemas import normalize_license_plate

logger = get_logger("cropwise.work_time_report")

router = APIRouter(prefix="/api/work-time-report", tags=["Отчёт по времени работы"])

TZ_MSK = timezone(timedelta(hours=3))
DAY_START_HOUR = 6
SEM = asyncio.Semaphore(20)
MTFMI_SEM = asyncio.Semaphore(15)

# Группы видов работ → числовые work_type_id в Cropwise. Это НЕ то же самое,
# что каталог /work_types (там "name" — общая категория техники/операции,
# а не то, что реально фильтрует machine_tasks). Список id для каждой группы
# выверен вручную и уже используется во всех остальных Cropwise-отчётах
# проекта (см. plans_1.py WORK_GROUPS) — переиспользуем его 1:1, чтобы не
# разъезжаться с остальными отчётами и не гадать по live-каталогу заново.
WORK_GROUPS_MAP: dict[str, list[int]] = dict(WORK_GROUPS)

FIRM_SHEET_NAME = {
    "АФ Чистопольская":           "АФЧ",
    "АгроРаздолье":                "АР",
    "Аксу-Агро":                   "Аксу-Агро",
    "НурлатАгро":                  "НурлатАгро",
    "Дрожжаное Рассвет Агро":      "Дрожжаное Рассвет",
    "Актай":                       "Актай",
    "Лениногорская Птицефабрика":  "Лен.Птицефабрика",
}


class WorkTimeReportRequest(BaseModel):
    date_from: str   # "YYYY-MM-DD" — первые агрономические сутки периода
    date_to:   str   # "YYYY-MM-DD" — последние агрономические сутки периода (включительно)
    work_type: str   # ключ = название группы работ из WORK_GROUPS, например "3.1 Опрыскивание"


@router.get("/work-types", summary="Список видов работ для выбора в отчёте")
async def list_work_types(current_user: User = Depends(get_current_admin_user)):
    return [{"key": name, "label": name} for name, _ in WORK_GROUPS]


# ---------------------------------------------------------------------------
# Вспомогательные функции (перенесены 1:1 из violations_sync.py / field_area_router.py)
# ---------------------------------------------------------------------------

def _parse_dt(s) -> Optional[datetime]:
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
    # Cropwise отдаёт datetime-строки БЕЗ смещения (naive) как UTC, а не как
    # уже московское время — сверено на covered_area_hourly и stops_timetable
    # (наивные "07:00"/"08:00" на деле оказались 10:00/11:00 МСК, что совпало
    # с реальным окном работы по полю из UI Кропвайс). Раньше naive строка
    # ошибочно считалась уже МСК (dt.replace(tzinfo=TZ_MSK)) — это давало
    # неверные абсолютные времена (хотя суммы/длительности не менялись,
    # т.к. сдвиг на константу не влияет на разницу двух времён).
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ_MSK)


def _day_window(d: date) -> tuple[datetime, datetime]:
    """Агрономические сутки: 06:00 текущего дня — 05:59:59.999999 следующего (МСК)."""
    start = datetime(d.year, d.month, d.day, DAY_START_HOUR, 0, 0, tzinfo=TZ_MSK)
    end   = start + timedelta(days=1) - timedelta(microseconds=1)
    return start, end


def _shift_day_of(dt_msk: datetime) -> date:
    """Каким агрономическим суткам принадлежит момент времени (уже в МСК)."""
    return dt_msk.date() if dt_msk.hour >= DAY_START_HOUR else (dt_msk.date() - timedelta(days=1))


def _folder_parent_map(gfs: list[dict]) -> dict[int, Optional[int]]:
    out: dict[int, Optional[int]] = {}
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


# ---------------------------------------------------------------------------
# Сбор данных
# ---------------------------------------------------------------------------

async def _build_report_rows(
    date_from: date, date_to: date, work_type_key: str,
) -> dict[str, dict[date, list[dict]]]:
    """Возвращает { firm: { day: [row, ...] } }."""
    window_start, _            = _day_window(date_from)
    _, window_end               = _day_window(date_to)
    seasons = cropwise_season_years_for_range(date_from, date_to)

    wt_ids = WORK_GROUPS_MAP.get(work_type_key)
    if not wt_ids:
        raise HTTPException(400, f"Вид работ {work_type_key!r} не найден")

    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS, timeout=_CROPWISE_TIMEOUT) as session:
        machines_raw, fields_raw, fgs_raw, gfs_raw = await asyncio.gather(
            _fetch_all_pages(session, "machines",      sem=SEM),
            _fetch_all_pages(session, "fields",        sem=SEM),
            _fetch_all_pages(session, "field_groups",  sem=SEM),
            _fetch_all_pages(session, "group_folders", sem=SEM),
        )

        mt_chunks = await asyncio.gather(*[
            _fetch_machine_tasks_by_wt(
                session, wt_ids, {"season": season}, sem=SEM,
                log_prefix="work_time_report ",
            )
            for season in seasons
        ])
        seen_tids: set[int] = set()
        tasks: list[dict] = []
        for chunk in mt_chunks:
            for t in chunk:
                tid = t.get("id")
                if tid is None or int(tid) in seen_tids:
                    continue
                s = _parse_dt(t.get("start_time"))
                e = _parse_dt(t.get("end_time"))
                if (e and e < window_start) or (s and s > window_end):
                    continue
                if not s and not e:
                    continue
                seen_tids.add(int(tid))
                tasks.append(t)

        logger.info(f"work_time_report: {len(tasks)} задач wt_ids={wt_ids} за {date_from}..{date_to}")

        task_ids  = [int(t["id"]) for t in tasks]
        mtfmi_map = await _fetch_mtfmi_by_task_ids(session, task_ids, sem=MTFMI_SEM)

    field_to_firm = _build_field_to_firm(fields_raw, fgs_raw, gfs_raw)
    field_name: dict[int, str] = {
        int(f["id"]): (f.get("name") or f.get("title") or f"Поле #{f['id']}").strip()
        for f in fields_raw if f.get("id") is not None
    }
    machine_name: dict[int, str] = {}
    for m in machines_raw:
        mid = m.get("id")
        if mid is None:
            continue
        machine_name[int(mid)] = m.get("name") or m.get("title") or m.get("model") or f"machine_{mid}"

    # ── Строим строки «машина × поле × визит» ────────────────────────────────
    visits: list[dict] = []
    for task in tasks:
        mid_raw = task.get("machine_id")
        if mid_raw is None:
            continue
        mid   = int(mid_raw)
        t_start = _parse_dt(task.get("start_time"))
        t_end   = _parse_dt(task.get("end_time"))
        items = mtfmi_map.get(int(task["id"]), [])
        for item in items:
            fid_raw = item.get("field_id")
            if fid_raw is None:
                continue
            fid = int(fid_raw)

            hourly = item.get("covered_area_hourly") or []
            points = []
            for e in hourly:
                if not e or len(e) < 2:
                    continue
                ts = _parse_dt(e[0])
                if ts:
                    points.append((ts, float(e[1] or 0)))
            points.sort(key=lambda p: p[0])

            if points:
                entry, exit_ = points[0][0], points[-1][0]
                area = sum(v for _, v in points)
            else:
                entry = t_start
                exit_ = t_end
                area  = float(item.get("covered_area") or 0)
            if not entry or not exit_:
                continue
            if area <= 0:
                area = float(item.get("covered_area") or 0)

            # covered_area_hourly иногда содержит точки за пределами времени
            # самого задания (шум телеметрии) — из-за этого въезд мог
            # оказаться раньше start_time задания, а выезд позже end_time.
            # Задание — источник истины по временным границам, обрезаем по нему.
            if t_start and entry < t_start:
                entry = t_start
            if t_end and exit_ > t_end:
                exit_ = t_end
            if entry > exit_:
                continue  # после обрезки интервал вырожден — строку пропускаем

            # Строки без обработанной площади не несут информации — как и в
            # ручном отчёте, их не показываем.
            if area <= 0:
                continue

            day = _shift_day_of(entry)
            if not (date_from <= day <= date_to):
                continue

            # Простой по Кропвайс — берём напрямую из stops_timetable этого
            # MTFMI-элемента (готовые интервалы, которые показывает и UI
            # Кропвайс в карточке задания), а не вычисляем сами.
            downtime_sec = 0.0
            for interval in (item.get("stops_timetable") or []):
                if not interval or len(interval) < 2:
                    continue
                st_start = _parse_dt(interval[0])
                st_end   = _parse_dt(interval[1])
                if st_start is None or st_end is None:
                    continue
                downtime_sec += max((st_end - st_start).total_seconds(), 0.0)

            firm = field_to_firm.get(fid, "—")
            visits.append({
                "firm": firm, "day": day,
                "task_id": int(task["id"]),
                "machine_id": mid, "machine": machine_name.get(mid, f"machine_{mid}"),
                "field": field_name.get(fid, f"Поле #{fid}"),
                "entry": entry, "exit": exit_, "area": round(area, 2),
                "downtime_sec": downtime_sec,
            })

    if not visits:
        return {}

    # ── «Время переезда, общее» — по машине за сутки: сумма готового поля
    #    movements_on_road_duration (сек) каждого machine_task этой машины,
    #    отнесённого к тем же агрономическим суткам, что и его строки-визиты.
    #    Раньше считали как сумму разрывов между выездом с одного поля и
    #    въездом на следующее — это давало неверные числа (сверено с UI
    #    Кропвайс на карточке задания «На дорогах»), а Кропвайс уже отдаёт
    #    готовое значение прямо в объекте machine_tasks.
    #    День задания считаем НАПРЯМУЮ по task.start_time (а не по дню его
    #    визитов) — иначе задание с нулевой обработанной площадью, чьи
    #    строки отфильтрованы выше, потеряло бы вклад в переезд.
    transit_by_machine_day: dict[tuple[int, date], float] = defaultdict(float)
    for task in tasks:
        mid_raw = task.get("machine_id")
        if mid_raw is None:
            continue
        t_start = _parse_dt(task.get("start_time"))
        if t_start is None:
            continue
        day = _shift_day_of(t_start)
        if not (date_from <= day <= date_to):
            continue
        road_sec = task.get("movements_on_road_duration")
        if road_sec is None:
            continue
        transit_by_machine_day[(int(mid_raw), day)] += float(road_sec)

    # ── Причина простоя — сначала downtime (актуальные данные, без задержки
    #    подхвата), потом дополняем cropwise_stops (панель «Простои») ────────
    machine_ids = sorted({v["machine_id"] for v in visits})
    shift_dates = sorted({v["day"].isoformat() for v in visits})

    machine_to_plate: dict[int, str] = {}
    if machine_ids:
        async with async_session_maker() as db_session:
            res = await db_session.execute(
                select(Vehicle.id, Vehicle.registration_number)
                .where(Vehicle.id.in_(machine_ids))
            )
            for vid, plate in res.all():
                if plate:
                    machine_to_plate[vid] = normalize_license_plate(plate)

    downtime_by_plate: dict[str, list[dict]] = defaultdict(list)
    if machine_to_plate:
        downtime_list = await CropwiseStopDAO.get_downtimes_for_stops(
            shift_dates=shift_dates,
            license_plates=sorted(set(machine_to_plate.values())),
        )
        for dt in downtime_list:
            if dt.get("license_plate"):
                downtime_by_plate[dt["license_plate"]].append(dt)

    stops_by_machine: dict[int, list[CropwiseStop]] = defaultdict(list)
    if machine_ids:
        async with async_session_maker() as db_session:
            res = await db_session.execute(
                select(CropwiseStop).where(
                    CropwiseStop.machine_id.in_(machine_ids),
                    CropwiseStop.shift_date.in_(shift_dates),
                )
            )
            for stop in res.scalars().all():
                stops_by_machine[stop.machine_id].append(stop)

    def _match_reason(machine_id: int, entry: datetime, exit_: datetime) -> str:
        """Причина простоя от водителя: сначала downtime (основной, актуальный
        источник — сопоставляем по госномеру машины и пересечению интервала),
        затем дополняем тем, что есть только в cropwise_stops (панель
        «Простои» — может отставать или содержать причины, вписанные только
        там). Длительность простоя в эту функцию не входит — она берётся из
        downtime_sec визита (источник — stops_timetable из Cropwise)."""
        reasons: list[str] = []

        plate = machine_to_plate.get(machine_id)
        if plate:
            for dt in downtime_by_plate.get(plate, []):
                if dt["start_dt"] < exit_.replace(tzinfo=None) and entry.replace(tzinfo=None) < dt["end_dt"]:
                    if dt.get("reason") and dt["reason"] not in reasons:
                        reasons.append(dt["reason"])

        for stop in stops_by_machine.get(machine_id, []):
            if stop.stop_start < exit_ and entry < stop.stop_end:
                if stop.reason and stop.reason not in reasons:
                    reasons.append(stop.reason)

        return ", ".join(reasons)

    # ── Собираем финальные строки, группируем по (firm, day) ────────────────
    out: dict[str, dict[date, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for v in visits:
        transit_sec = transit_by_machine_day.get((v["machine_id"], v["day"]), 0.0)
        reason_str = _match_reason(v["machine_id"], v["entry"], v["exit"])
        out[v["firm"]][v["day"]].append({
            "machine":     v["machine"],
            "field":       v["field"],
            "entry":       v["entry"],
            "exit":        v["exit"],
            "transit_sec": transit_sec,
            "downtime_sec": v["downtime_sec"],
            "reason":      reason_str,
            "area":        v["area"],
        })

    for firm, days in out.items():
        for day, rows in days.items():
            rows.sort(key=lambda r: (r["machine"], r["entry"]))

    return out


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------

_HDR_FILL   = PatternFill("solid", start_color="2E4DA0", end_color="2E4DA0")
_TITLE_FILL = PatternFill("solid", start_color="D9E1F2", end_color="D9E1F2")
_HDR_FONT   = Font(bold=True, color="FFFFFF", size=10)
_TITLE_FONT = Font(bold=True, color="1F2937", size=11)
_THIN       = Side(style="thin", color="B7C0CE")
_BORDER     = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)

HEADERS = [
    "Техника", "Поле", "Время начала работ", "Время окончания работ",
    "Время переезда, общее", "Время простоя согласно кропвайс",
    "Причина простоя с чат-бот", "Обработанная площадь, га",
]
COL_W = [40, 16, 16, 16, 16, 18, 40, 16]


def _sec_to_excel_frac(sec: float) -> float:
    return max(sec, 0) / 86400.0


def _build_workbook(data: dict[str, dict[date, list[dict]]], work_type_label: str) -> Workbook:
    wb = Workbook()
    wb.remove(wb.active)

    for firm in FIRM_ORDER:
        days = data.get(firm)
        if not days:
            continue
        sheet_name = FIRM_SHEET_NAME.get(firm, firm)[:31]
        ws = wb.create_sheet(sheet_name)
        for i, w in enumerate(COL_W, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w

        r = 1
        for day in sorted(days.keys()):
            rows = days[day]
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(HEADERS))
            title_cell = ws.cell(
                row=r, column=1,
                value=f"Информация по времени работы на {work_type_label} в {firm} за {day.strftime('%d.%m.%Y')}",
            )
            title_cell.font = _TITLE_FONT
            title_cell.fill = _TITLE_FILL
            title_cell.alignment = Alignment(horizontal="left", vertical="center")
            ws.row_dimensions[r].height = 20
            r += 1

            for c, htext in enumerate(HEADERS, start=1):
                cell = ws.cell(row=r, column=c, value=htext)
                cell.font = _HDR_FONT
                cell.fill = _HDR_FILL
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                cell.border = _BORDER
            r += 1

            first_data_row = r
            prev_machine = None
            machine_start_row = r
            for row in rows:
                ws.cell(row=r, column=1, value=row["machine"]).border = _BORDER
                ws.cell(row=r, column=2, value=row["field"]).border = _BORDER

                t_start = ws.cell(row=r, column=3, value=row["entry"].time())
                t_start.number_format = "HH:MM"
                t_start.border = _BORDER
                t_end = ws.cell(row=r, column=4, value=row["exit"].time())
                t_end.number_format = "HH:MM"
                t_end.border = _BORDER

                c_transit = ws.cell(row=r, column=5, value=_sec_to_excel_frac(row["transit_sec"]))
                c_transit.number_format = "[h]:mm"
                c_transit.border = _BORDER
                c_transit.alignment = Alignment(horizontal="center", vertical="center")

                c_down = ws.cell(row=r, column=6, value=_sec_to_excel_frac(row["downtime_sec"]))
                c_down.number_format = "[h]:mm"
                c_down.border = _BORDER

                ws.cell(row=r, column=7, value=row["reason"] or "").border = _BORDER
                ws.cell(row=r, column=7).alignment = Alignment(wrap_text=True, vertical="top")

                c_area = ws.cell(row=r, column=8, value=row["area"])
                c_area.number_format = "#,##0.00"
                c_area.border = _BORDER

                # Схлопываем колонки «Техника» и «Время переезда, общее» для
                # последовательных строк одной машины — оба значения одни и
                # те же для всех строк машины за этот день.
                if row["machine"] != prev_machine:
                    if prev_machine is not None and r - 1 > machine_start_row:
                        ws.merge_cells(start_row=machine_start_row, start_column=1,
                                        end_row=r - 1, end_column=1)
                        ws.merge_cells(start_row=machine_start_row, start_column=5,
                                        end_row=r - 1, end_column=5)
                    machine_start_row = r
                    prev_machine = row["machine"]
                r += 1
            if rows and r - 1 > machine_start_row:
                ws.merge_cells(start_row=machine_start_row, start_column=1,
                                end_row=r - 1, end_column=1)
                ws.merge_cells(start_row=machine_start_row, start_column=5,
                                end_row=r - 1, end_column=5)

            r += 1  # пустая строка между днями

        ws.freeze_panes = "A2"

    if not wb.sheetnames:
        ws = wb.create_sheet("Нет данных")
        ws.cell(row=1, column=1, value="За выбранный период данных не найдено")

    return wb


@router.post("/excel", summary="Отчёт по времени работы в разрезе агрофирм — скачать Excel (админ)")
async def work_time_report_excel(
    data: WorkTimeReportRequest,
    current_user: User = Depends(get_current_admin_user),
):
    try:
        date_from = date.fromisoformat(data.date_from)
        date_to   = date.fromisoformat(data.date_to)
    except ValueError:
        raise HTTPException(400, "Некорректный формат даты, ожидается YYYY-MM-DD")
    if date_to < date_from:
        raise HTTPException(400, "date_to не может быть раньше date_from")

    work_type_label = data.work_type

    report = await _build_report_rows(date_from, date_to, data.work_type)
    wb = _build_workbook(report, work_type_label)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    safe_wt = "".join(c if c.isalnum() else "_" for c in data.work_type).strip("_")
    fname = f"Время_работы_{safe_wt}_{date_from.isoformat()}_{date_to.isoformat()}.xlsx"

    # Content-Disposition разрешает только latin-1 в headers — кириллица туда
    # не помещается напрямую (latin-1 её вообще не покрывает). Отдаём
    # ASCII-only fallback в filename= (для старых клиентов) и настоящее имя
    # с кириллицей в filename*=UTF-8''... (RFC 5987/6266) — современные
    # браузеры используют именно filename*.
    ascii_fallback = f"work_time_report_{date_from.isoformat()}_{date_to.isoformat()}.xlsx"
    fname_quoted = quote(fname, safe="")
    content_disposition = (
        f'attachment; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{fname_quoted}"
    )

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition},
    )
