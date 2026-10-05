"""
cropwise/low_productivity_router.py
=====================================
Роутер: водители с низкой производительностью.

Текущая смена («live»):
  Данные хранятся в Redis (ключ: low_productivity_cache) и обновляются задачей
  sync_user_assignments каждые 15 минут. В кэше — ВСЕ водители с day_area > 0.1 га.
  Фильтрация по порогу делается здесь из кэша (без запросов к Cropwise).

История смен («prev_day» / «prev_night»):
  Отдельный кэш (ключ: low_productivity_shift_history) с двумя готовыми
  наборами строк за прошлые сутки:
    prev_day   — вчера 06:00–18:00 МСК
    prev_night — вчера 18:00 – сегодня 06:00 МСК
  Обновляется той же задачей sync_user_assignments (раз в 15 минут — но
  фактический пересчёт исторических данных кэшируется по дате, см.
  sync_assignments._update_shift_history_cache).

Параметры GET /api/low-productivity:
  shift        — какие данные отдавать:
                 "current"     (по умолчанию) — текущая смена (live, из low_productivity_cache)
                 "prev_day"    — вчера 06:00–18:00
                 "prev_night"  — вчера 18:00 – сегодня 06:00
  threshold    — порог площади (float, по умолчанию 5.0)
  window_hours — окно в часах для расчёта метрики (только для shift="current"):
                 1.0  → prev_hour_area + curr_hour_area (исторический режим)
                 2.0  → h2_area + prev_hour_area + curr_hour_area
                 0    → day_area (площадь за всю смену с 06:00)
                 Для shift="prev_day"/"prev_night" метрика — всегда shift_area
                 (площадь за весь период смены).
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timezone, timedelta
from typing import Optional

import redis
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.auth.dependencies import get_current_admin_user
from app.users.models import User
from app.logging_config import get_logger
from app.config import get_redis_url

logger = get_logger("cropwise.low_productivity")

router = APIRouter(
    prefix="/api/low-productivity",
    tags=["Низкая производительность"],
)

REDIS_KEY         = "low_productivity_cache"
REDIS_KEY_HISTORY = "low_productivity_shift_history"
TZ_MSK            = timezone(timedelta(hours=3))

_redis = redis.from_url(get_redis_url(), decode_responses=True)


def update_cache(rows: list[dict], generated_at) -> None:
    """Вызывается из sync_user_assignments после каждой синхронизации (текущая смена)."""
    data = {
        "rows":         rows,
        "generated_at": generated_at.strftime("%d.%m.%Y %H:%M"),
    }
    _redis.set(REDIS_KEY, json.dumps(data, ensure_ascii=False, default=str))
    logger.info(f"low_productivity cache updated: {len(rows)} rows at {data['generated_at']}")


def update_shift_history_cache(
    prev_day_rows:   list[dict],
    prev_night_rows: list[dict],
    today_day_rows:  list[dict],
    generated_at,
    for_date: str,
    prev_day_label: str,
    prev_night_label: str,
    today_day_label: str,
) -> None:
    """
    Вызывается из sync_user_assignments._update_shift_history_cache.

    for_date — дата (YYYY-MM-DD), за которую посчитаны окна prev_day/prev_night
    (используется для кэширования: пересчёт исторических смен не нужен после
    того, как они посчитаны для актуального "вчера").
    """
    data = {
        "prev_day_rows":    prev_day_rows,
        "prev_night_rows":  prev_night_rows,
        "today_day_rows":   today_day_rows,
        "generated_at":     generated_at.strftime("%d.%m.%Y %H:%M"),
        "for_date":         for_date,
        "prev_day_label":   prev_day_label,
        "prev_night_label": prev_night_label,
        "today_day_label":  today_day_label,
    }
    _redis.set(REDIS_KEY_HISTORY, json.dumps(data, ensure_ascii=False, default=str))
    logger.info(
        f"low_productivity shift history cache updated: "
        f"prev_day={len(prev_day_rows)}, prev_night={len(prev_night_rows)}, for_date={for_date}"
    )


def get_shift_history_for_date() -> Optional[str]:
    """Возвращает for_date из кэша истории смен (или None, если кэш пуст)."""
    raw = _redis.get(REDIS_KEY_HISTORY)
    if not raw:
        return None
    try:
        return json.loads(raw).get("for_date")
    except Exception:
        return None


def _compute_metric(row: dict, window_hours: float) -> float:
    """
    Вычисляет метрику производительности для строки из кэша текущей смены.
      window_hours == 0   → day_area (площадь с 06:00)
      window_hours == 1   → prev_hour + curr_hour
      window_hours == 2   → h2 + prev_hour + curr_hour
      window_hours == 3   → h3 + h2 + prev_hour + curr_hour
    """
    if window_hours == 0:
        return float(row.get("day_area") or 0)
    n = max(1, int(window_hours))  # окно не меньше 1
    parts = [
        float(row.get("curr_hour_area") or 0),
        float(row.get("prev_hour_area") or 0),
        float(row.get("h2_area") or 0),
        float(row.get("h3_area") or 0),
    ]
    return sum(parts[:n])


@router.get("", summary="Водители с низкой производительностью (фильтр из кэша)")
async def get_low_productivity(
    shift:        str   = Query("current", description="current | prev_day | prev_night | today_day"),
    threshold:    float = Query(5.0,  description="Порог площади (га). Строки НИЖЕ порога"),
    window_hours: float = Query(1.0,  description="Окно в часах (только для shift=current): 1=прошлый+текущий час, 2=+ещё час, 0=за всю смену"),
    current_user: User  = Depends(get_current_admin_user),
):
    """
    Возвращает водителей, чья площадь за выбранный период < threshold.
    Данные берутся из Redis-кэша без обращения к Cropwise API.
    """
    if shift in ("prev_day", "prev_night", "today_day"):
        raw = _redis.get(REDIS_KEY_HISTORY)
        if not raw:
            return {
                "generated_at": None, "rows": [], "threshold": threshold,
                "shift": shift, "shift_label": None,
            }

        data = json.loads(raw)
        if shift == "today_day":
            key = "today_day_rows"
            label_key = "today_day_label"
        else:
            key = "prev_day_rows" if shift == "prev_day" else "prev_night_rows"
            label_key = "prev_day_label" if shift == "prev_day" else "prev_night_label"
        all_rows: list[dict] = data.get(key, [])

        filtered = []
        for row in all_rows:
            metric = float(row.get("shift_area") or 0)
            if metric < threshold:
                filtered.append({**row, "_metric": round(metric, 2)})

        filtered.sort(key=lambda r: (r["has_downtime"], r["_metric"]))

        return {
            "generated_at":   data.get("generated_at"),
            "rows":           filtered,
            "threshold":      threshold,
            "shift":          shift,
            "shift_label":    data.get(label_key),
            "total_in_cache": len(all_rows),
        }

    # shift == "current" — поведение без изменений
    raw = _redis.get(REDIS_KEY)
    if not raw:
        return {"generated_at": None, "rows": [], "threshold": threshold, "window_hours": window_hours, "shift": "current"}

    data = json.loads(raw)
    all_rows: list[dict] = data.get("rows", [])

    filtered = []
    for row in all_rows:
        metric = _compute_metric(row, window_hours)
        if metric < threshold:
            filtered.append({**row, "_metric": round(metric, 2)})

    # Пересортировка с учётом новой метрики
    filtered.sort(key=lambda r: (r["has_downtime"], r["_metric"]))

    return {
        "generated_at": data.get("generated_at"),
        "rows":         filtered,
        "threshold":    threshold,
        "window_hours": window_hours,
        "shift":        "current",
        "total_in_cache": len(all_rows),
    }


@router.post("/refresh", summary="Запустить sync_user_assignments немедленно (админ)")
async def trigger_refresh(
    current_user: User = Depends(get_current_admin_user),
):
    """Немедленно запускает синхронизацию через Celery."""
    try:
        from app.cropwise.tasks import sync_user_assignments_task
        task = sync_user_assignments_task.delay()
        logger.info(
            f"low_productivity refresh triggered: task_id={task.id}, by={current_user.fio}"
        )
        return {"status": "queued", "task_id": task.id}
    except Exception as e:
        logger.error(f"trigger_refresh low_productivity: {e}", exc_info=True)
        from fastapi import HTTPException
        raise HTTPException(status_code=502, detail=f"Ошибка постановки задачи: {e}")


@router.post("/refresh-downtimes", summary="Обновить только данные о простоях из БД (без Cropwise)")
async def refresh_downtimes_only(
    current_user: User = Depends(get_current_admin_user),
):
    """
    Читает актуальные простои из PostgreSQL и патчит оба кэша
    (текущая смена + история смен) — без обращения к Cropwise API.
    Работает ~1–2 секунды. Полезно после ручного редактирования простоев.
    """
    from sqlalchemy import select, or_
    from app.database import async_session_maker
    from app.downtime.models import ActiveDowntime, Downtime

    now_msk = datetime.now(TZ_MSK)
    today   = now_msk.date()
    yesterday = (now_msk - timedelta(days=1)).date()

    # Для ночной смены (до 06:00) простои могут быть и за вчера, и за сегодня
    shift_dates_current = [today] if now_msk.hour >= 6 else [yesterday, today]

    # История смен охватывает позавчера–сегодня (prev_day + prev_night + today_day)
    today_start   = now_msk.replace(hour=6, minute=0, second=0, microsecond=0)
    if now_msk.hour < 6:
        today_start -= timedelta(days=1)
    prev_day_start = today_start - timedelta(days=1)

    history_dates = list({prev_day_start.date(), today_start.date(), today})

    def _fmt_duration(seconds: int) -> str:
        if seconds <= 0:
            return "0м"
        h = seconds // 3600
        m = (seconds % 3600) // 60
        if h and m:
            return f"{h}ч {m}м"
        return f"{h}ч" if h else f"{m}м"

    patched_current  = 0
    patched_history  = 0

    # ── 1. Патч кэша текущей смены ───────────────────────────────────────────
    raw_current = _redis.get(REDIS_KEY)
    if raw_current:
        data_current = json.loads(raw_current)
        rows_current: list[dict] = data_current.get("rows", [])
        driver_ids_current = [r["user_id"] for r in rows_current if r.get("user_id")]

        if driver_ids_current:
            async with async_session_maker() as session:
                act_rows = await session.execute(
                    select(ActiveDowntime).where(
                        ActiveDowntime.user_id.in_(driver_ids_current)
                    )
                )
                active_map: dict[int, ActiveDowntime] = {
                    r.user_id: r for r in act_rows.scalars().all()
                }

                fin_rows = await session.execute(
                    select(Downtime).where(
                        Downtime.user_id.in_(driver_ids_current),
                        or_(*[Downtime.start_date == d for d in shift_dates_current]),
                    )
                )
                fin_map: dict[int, list[Downtime]] = {}
                for r in fin_rows.scalars().all():
                    fin_map.setdefault(r.user_id, []).append(r)

            def _build_current(uid: int) -> list[dict]:
                info: list[dict] = []
                act = active_map.get(uid)
                if act:
                    info.append({
                        "reason":   act.reason,
                        "start":    f"{act.start_date.strftime('%d.%m')} {act.start_time.strftime('%H:%M')}",
                        "end":      None,
                        "duration": None,
                        "active":   True,
                    })
                for dt in sorted(fin_map.get(uid, []), key=lambda x: (x.start_date, x.start_time)):
                    info.append({
                        "reason":   dt.reason,
                        "start":    f"{dt.start_date.strftime('%d.%m')} {dt.start_time.strftime('%H:%M')}",
                        "end":      f"{dt.end_date.strftime('%d.%m')} {dt.end_time.strftime('%H:%M')}",
                        "duration": _fmt_duration(dt.duration),
                        "active":   False,
                    })
                return info

            for row in rows_current:
                uid = row.get("user_id")
                if uid:
                    info = _build_current(uid)
                    row["downtime"]    = info
                    row["has_downtime"] = bool(info)
                    patched_current += 1

            data_current["rows"] = rows_current
            _redis.set(REDIS_KEY, json.dumps(data_current, ensure_ascii=False, default=str))
            logger.info(f"refresh_downtimes: current cache patched ({patched_current} rows), by={current_user.fio}")

    # ── 2. Патч кэша истории смен ────────────────────────────────────────────
    raw_history = _redis.get(REDIS_KEY_HISTORY)
    if raw_history:
        data_history = json.loads(raw_history)

        # Собираем все user_id из всех вкладок
        all_history_rows: list[dict] = (
            data_history.get("prev_day_rows",   []) +
            data_history.get("prev_night_rows", []) +
            data_history.get("today_day_rows",  [])
        )
        driver_ids_history = list({r["user_id"] for r in all_history_rows if r.get("user_id")})

        if driver_ids_history:
            async with async_session_maker() as session:
                act_rows2 = await session.execute(
                    select(ActiveDowntime).where(
                        ActiveDowntime.user_id.in_(driver_ids_history)
                    )
                )
                active_map2: dict[int, ActiveDowntime] = {
                    r.user_id: r for r in act_rows2.scalars().all()
                }

                fin_rows2 = await session.execute(
                    select(Downtime).where(
                        Downtime.user_id.in_(driver_ids_history),
                        or_(*[Downtime.start_date == d for d in history_dates]),
                    )
                )
                fin_map2: dict[int, list[Downtime]] = {}
                for r in fin_rows2.scalars().all():
                    fin_map2.setdefault(r.user_id, []).append(r)

            def _dt_to_aware(dt_obj, t_obj) -> datetime:
                from datetime import datetime as _dt
                return _dt.combine(dt_obj, t_obj, tzinfo=TZ_MSK)

            def _overlaps(dt_model, win_start: datetime, win_end: datetime) -> bool:
                s = _dt_to_aware(dt_model.start_date, dt_model.start_time)
                if getattr(dt_model, "end_date", None) and getattr(dt_model, "end_time", None):
                    e = _dt_to_aware(dt_model.end_date, dt_model.end_time)
                else:
                    e = win_end
                return s < win_end and e > win_start

            # Границы окон (те же, что в _update_shift_history_cache)
            prev_night_start = prev_day_start + timedelta(hours=12)   # вчера 18:00
            prev_night_end   = today_start                             # сегодня 06:00
            prev_day_end     = prev_night_start                        # вчера 18:00
            today_day_end    = today_start + timedelta(hours=12)       # сегодня 18:00

            windows = {
                "prev_day_rows":   (prev_day_start,   prev_day_end),
                "prev_night_rows": (prev_night_start, prev_night_end),
                "today_day_rows":  (today_start,      today_day_end),
            }

            def _build_history(uid: int, win_start: datetime, win_end: datetime) -> list[dict]:
                info: list[dict] = []
                act = active_map2.get(uid)
                if act and _overlaps(act, win_start, win_end):
                    info.append({
                        "reason":   act.reason,
                        "start":    f"{act.start_date.strftime('%d.%m')} {act.start_time.strftime('%H:%M')}",
                        "end":      None,
                        "duration": None,
                        "active":   True,
                    })
                for dt in sorted(fin_map2.get(uid, []), key=lambda x: (x.start_date, x.start_time)):
                    if not _overlaps(dt, win_start, win_end):
                        continue
                    info.append({
                        "reason":   dt.reason,
                        "start":    f"{dt.start_date.strftime('%d.%m')} {dt.start_time.strftime('%H:%M')}",
                        "end":      f"{dt.end_date.strftime('%d.%m')} {dt.end_time.strftime('%H:%M')}",
                        "duration": _fmt_duration(dt.duration),
                        "active":   False,
                    })
                return info

            for key, (win_start, win_end) in windows.items():
                for row in data_history.get(key, []):
                    uid = row.get("user_id")
                    if uid:
                        info = _build_history(uid, win_start, win_end)
                        row["downtime"]    = info
                        row["has_downtime"] = bool(info)
                        patched_history += 1

            _redis.set(REDIS_KEY_HISTORY, json.dumps(data_history, ensure_ascii=False, default=str))
            logger.info(f"refresh_downtimes: history cache patched ({patched_history} rows), by={current_user.fio}")

    return {
        "status":           "ok",
        "patched_current":  patched_current,
        "patched_history":  patched_history,
    }


@router.get("/chatbot-docx", summary="Отчёт по чат-боту (DOCX) для любой смены")
async def export_chatbot_docx(
    shift:        str   = Query("prev_night", description="current | prev_day | prev_night | today_day"),
    threshold:    float = Query(500.0, description="Порог площади (га) — как отображается в панели"),
    window_hours: float = Query(0.0,   description="Окно в часах (только для shift=current): 0=за всю смену, 1=прошлый+тек., ..."),
    current_user: User = Depends(get_current_admin_user),
):
    """
    Генерирует DOCX «Информация по фиксации в чат-бот за смену».

    «Не использовали чат-бот» — механизаторы, у которых has_downtime=False
    и метрика < threshold (т.е. они отработали мало и простой не фиксировали).

    «Использовали чат-бот» — механизаторы с has_downtime=True
    (вне зависимости от площади, если они в кэше).

    Для shift=current метрика = _compute_metric(row, window_hours).
    Для shift=prev_day/prev_night метрика = shift_area.
    """
    if shift not in ("current", "prev_day", "prev_night", "today_day"):
        raise HTTPException(status_code=400, detail="shift должен быть current, prev_day, prev_night или today_day")

    try:
        from docx import Document
        from docx.shared import Pt, Cm
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.enum.table import WD_ALIGN_VERTICAL
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"python-docx не установлен: {e}")

    now_msk = datetime.now(TZ_MSK)
    report_time = now_msk.strftime("%d.%m.%Y %H:%M")

    # ── Загружаем нужные строки из кэша ──────────────────────────────────────
    if shift == "current":
        raw = _redis.get(REDIS_KEY)
        if not raw:
            raise HTTPException(status_code=404, detail="Кэш текущей смены пуст — дождитесь синхронизации")
        data = json.loads(raw)
        all_rows: list[dict] = data.get("rows", [])

        # Метрика для текущей смены
        def _metric(row: dict) -> float:
            return _compute_metric(row, window_hours)

        shift_title_date = now_msk.strftime("%d.%m.%Y")
        # Подпись периода
        if window_hours == 0:
            shift_time = f"с 06:00 (вся смена)"
            shift_name = "текущую смену"
        else:
            hrs = int(window_hours)
            shift_time = f"последние {hrs} ч"
            shift_name = "текущую смену"
        shift_abbr = "текущая"

    else:
        raw = _redis.get(REDIS_KEY_HISTORY)
        if not raw:
            raise HTTPException(status_code=404, detail="Кэш истории смен пуст — дождитесь синхронизации")
        data = json.loads(raw)
        if shift == "today_day":
            key = "today_day_rows"
            label_key = "today_day_label"
        else:
            key       = "prev_day_rows"  if shift == "prev_day"  else "prev_night_rows"
            label_key = "prev_day_label" if shift == "prev_day"  else "prev_night_label"
        all_rows  = data.get(key, [])
        shift_label: str = data.get(label_key, "")

        def _metric(row: dict) -> float:
            return float(row.get("shift_area") or 0)

        if shift == "prev_night":
            shift_title_date = now_msk.strftime("%d.%m.%Y")
            shift_name = "вечернюю смену"
            shift_time = "с 18:00 по 6:00"
            shift_abbr = "вечерняя"
        elif shift == "today_day":
            shift_title_date = now_msk.strftime("%d.%m.%Y")
            shift_name = "дневную смену"
            shift_time = "с 06:00 по 18:00"
            shift_abbr = "сегодняшняя"
        else:
            yesterday = now_msk - timedelta(days=1)
            shift_title_date = yesterday.strftime("%d.%m.%Y")
            shift_name = "дневную смену"
            shift_time = "с 06:00 по 18:00"
            shift_abbr = "дневная"

    # ── Только agri-машины (как во фронтенде) ────────────────────────────────
    all_rows = [r for r in all_rows if (r.get("machine_type") or "").lower() == "agri"]

    # ── Разбивка ─────────────────────────────────────────────────────────────
    in_report = [r for r in all_rows if _metric(r) < threshold]
    used_bot  = [r for r in in_report if r.get("has_downtime")]
    not_used  = [r for r in in_report if not r.get("has_downtime")]

    # ── Статистика по агрофирмам ──────────────────────────────────────────────
    firms: dict[str, dict] = {}
    for r in in_report:
        firm = r.get("agrofirm") or "—"
        if firm not in firms:
            firms[firm] = {"total": 0, "not_used": 0}
        firms[firm]["total"] += 1
        if not r.get("has_downtime"):
            firms[firm]["not_used"] += 1

    # ── Создаём документ ─────────────────────────────────────────────────────
    doc = Document()

    section = doc.sections[0]
    section.top_margin    = Cm(1.5)
    section.bottom_margin = Cm(1.5)
    section.left_margin   = Cm(2.0)
    section.right_margin  = Cm(1.5)

    # Время формирования
    p_time = doc.add_paragraph()
    p_time.alignment = WD_ALIGN_PARAGRAPH.LEFT
    run_time = p_time.add_run(f"Время формирования отчёта: {report_time}")
    run_time.italic = True
    run_time.font.size = Pt(10)

    # Заголовок
    p_title = doc.add_paragraph()
    p_title.alignment = WD_ALIGN_PARAGRAPH.LEFT
    run_title = p_title.add_run(
        f"Информация за {shift_name} по фиксации простоев в чат-бот "
        f"({shift_time}) за {shift_title_date}"
    )
    run_title.bold = True
    run_title.font.size = Pt(12)

    doc.add_paragraph()

    # Общая статистика
    p_stat_hdr = doc.add_paragraph()
    p_stat_hdr.add_run("Общая статистика").bold = True

    stat_table = doc.add_table(rows=4, cols=2)
    stat_table.style = "Table Grid"

    def _set_cell(cell, text: str, bold: bool = False, font_size: int = 10):
        cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        p = cell.paragraphs[0]
        run = p.add_run(text)
        run.bold = bold
        run.font.size = Pt(font_size)

    stat_rows = stat_table.rows
    _set_cell(stat_rows[0].cells[0], "Показатель",                                              bold=True)
    _set_cell(stat_rows[0].cells[1], "Значение",                                                bold=True)
    _set_cell(stat_rows[1].cells[0], "Всего механизаторов в отчёте")
    _set_cell(stat_rows[1].cells[1], str(len(in_report)))
    _set_cell(stat_rows[2].cells[0], "Использовали (информация внесена в чат-бот механизатором)")
    _set_cell(stat_rows[2].cells[1], str(len(used_bot)))
    _set_cell(stat_rows[3].cells[0], "Не использовал (информация не вносилась в чат-бот)")
    _set_cell(stat_rows[3].cells[1], str(len(not_used)))

    for row in stat_table.rows:
        row.cells[0].width = Cm(12)
        row.cells[1].width = Cm(4)

    doc.add_paragraph()

    # Статистика по агрофирмам
    p_firm_hdr = doc.add_paragraph()
    p_firm_hdr.add_run("Статистика пользованием чат-бот по агрофирмам").bold = True

    firm_table = doc.add_table(rows=1, cols=3)
    firm_table.style = "Table Grid"

    hdr_cells = firm_table.rows[0].cells
    for cell, text in zip(hdr_cells, ["Наименование агрофирмы", "Кол-во агроработ за смену", "Не проводили фиксацию"]):
        cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        run = cell.paragraphs[0].add_run(text)
        run.bold = True
        run.font.size = Pt(10)

    for firm_name in sorted(firms.keys()):
        stat = firms[firm_name]
        row_cells = firm_table.add_row().cells
        _set_cell(row_cells[0], firm_name)
        _set_cell(row_cells[1], str(stat["total"]))
        _set_cell(row_cells[2], str(stat["not_used"]))

    for row in firm_table.rows:
        row.cells[0].width = Cm(7)
        row.cells[1].width = Cm(5)
        row.cells[2].width = Cm(4)

    # ── Сохраняем в буфер ────────────────────────────────────────────────────
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)

    from urllib.parse import quote
    filename = f"Информация_по_фиксации_в_чат-бот_за_{shift_abbr}_смену_на_{shift_title_date.replace('.', '_')}.docx"
    filename_encoded = quote(filename, safe="")

    logger.info(f"chatbot_docx: shift={shift}, window_hours={window_hours}, threshold={threshold}, rows={len(in_report)}, by={current_user.fio}")

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename_encoded}"},
    )
