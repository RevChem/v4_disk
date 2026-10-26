# -*- coding: utf-8 -*-
"""Задания всех операций из локального хранилища: удобрения, посев, уборка и остальные.

Окно без фильтров — с 1 января года сводки до 06:00 даты сводки. Агрофирма, культура,
даты, госномер и фамилия применяются снаружи, теми же правилами, что у кнопки Namolot.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import driver_work as dw

_TZ = timezone(timedelta(hours=3))
_TASK_URL = "https://operations.cropwise.com/machines/{machine_id}/tasks/{task_id}"


def shift_cutoff(report_date: date) -> datetime:
    """06:00 даты сводки: смена, которая в неё закрывается, заканчивается в 05:59."""
    return datetime(report_date.year, report_date.month, report_date.day, 6, 0, tzinfo=_TZ)


def season_start(report_date: date) -> datetime:
    """Начало окна без фильтров: 1 января, чтобы весенние операции тоже попали в срез."""
    return datetime(report_date.year, 1, 1, tzinfo=_TZ)


def _wall(moment: datetime) -> str:
    return moment.astimezone(_TZ).strftime("%Y-%m-%dT%H:%M:%S")


def windows(report_date: date) -> dict[str, dict[str, str]]:
    cutoff = shift_cutoff(report_date)
    shown = cutoff - timedelta(minutes=1)
    day0 = cutoff - timedelta(days=1)
    start = season_start(report_date)
    return {
        "day": {"from": _wall(day0), "to": _wall(shown), "until": _wall(cutoff)},
        "season": {"from": _wall(start), "to": _wall(shown), "until": _wall(cutoff)},
    }


def area_rows(con: sqlite3.Connection, start_ts: float, end_ts: float) -> list[dict[str, Any]]:
    """Строка = задание × поле с площадью больше нуля. Культура — из history_crop."""
    found: list[dict[str, Any]] = []
    crop_join = ""
    crop_col = "'—'"
    if dw._has_table(con, "history_crop"):
        crop_col = "h.crop"
        crop_join = (
            "LEFT JOIN history_crop h "
            "ON h.hid = CAST(json_extract(m.data, '$.history_item_id') AS INTEGER)"
        )
    query = f"""
        SELECT t.id, t.machine_id, t.wt, t.end_ts,
               CAST(json_extract(t.data, '$.driver_id') AS INTEGER),
               CAST(json_extract(m.data, '$.field_id') AS INTEGER),
               {crop_col},
               CAST(json_extract(m.data, '$.covered_area') AS REAL)
        FROM mtfmi m
        JOIN tasks t ON t.id = m.task_id
        {crop_join}
        WHERE t.end_ts >= ? AND t.end_ts < ?
          AND CAST(json_extract(m.data, '$.covered_area') AS REAL) > 0
    """
    for task_id, machine_id, wt, end_ts, driver_id, field_id, crop, ha in con.execute(
        query, (start_ts, end_ts)
    ):
        moment = datetime.fromtimestamp(float(end_ts), tz=_TZ)
        found.append(
            {
                "at": moment.strftime("%Y-%m-%dT%H:%M:%S"),
                "task_id": int(task_id),
                "machine_id": int(machine_id) if machine_id is not None else None,
                "work_type_id": int(wt) if wt is not None else None,
                "driver_id": int(driver_id) if driver_id else None,
                "field_id": int(field_id) if field_id else None,
                "crop": str(crop or "").strip() or "—",
                "ha": round(float(ha or 0), 2),
            }
        )
    return found


def _plates(refs_path: Path) -> dict[int, str]:
    out: dict[int, str] = {}
    for row in dw._load_ref(refs_path, "machines"):
        mid = dw._int_or_none(row.get("id"))
        if mid is None:
            continue
        reg = str(row.get("registration_number") or row.get("reg_number") or "").strip()
        name = str(row.get("name") or "").strip()
        out[mid] = reg or name
    return out


def label_rows(
    raw: list[dict[str, Any]],
    *,
    firms: dict[int, str],
    plates: dict[int, str],
    operations: dict[int, str],
    field_names: dict[int, str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rec in raw:
        fid = rec.get("field_id")
        mid = rec.get("machine_id")
        wt = rec.get("work_type_id")
        tid = rec.get("task_id")
        rows.append(
            {
                "at": rec.get("at"),
                "firm": (firms.get(fid) or "") if isinstance(fid, int) else "",
                "crop": rec.get("crop") or "—",
                "operation": operations.get(wt) or (f"вид работ {wt}" if wt else "—"),
                "plate": (plates.get(mid) or "") if isinstance(mid, int) else "",
                "operator": "",
                "surname": "",
                "field": field_names.get(fid) or (f"поле {fid}" if fid else ""),
                "ha": rec.get("ha") or 0,
                "task_id": tid,
                "machine_id": mid,
                "driver_id": rec.get("driver_id"),
                "task_url": _TASK_URL.format(machine_id=mid, task_id=tid) if mid and tid else "",
            }
        )
    return rows


def load_rows(store_dir: Path, report_date: date) -> dict[str, Any]:
    """Все операции за год сводки до 06:00. ФИО подставляет вызывающий код."""
    frame = windows(report_date)
    store = Path(store_dir) / "store.sqlite"
    if not store.is_file():
        return {
            "available": False,
            "detail": "Хранилище заданий не найдено. Сначала синхронизируйте сводки.",
            "rows": [],
            **frame,
        }
    start = season_start(report_date).timestamp()
    end = shift_cutoff(report_date).timestamp()
    try:
        con = dw._connect(store)
    except sqlite3.Error as exc:
        return {"available": False, "detail": f"Не удалось открыть хранилище: {exc}", "rows": [], **frame}
    try:
        if not (dw._has_table(con, "tasks") and dw._has_table(con, "mtfmi")):
            return {
                "available": False,
                "detail": "В хранилище нет заданий.",
                "rows": [],
                **frame,
            }
        raw = area_rows(con, start, end)
    except sqlite3.Error as exc:
        return {"available": False, "detail": f"Хранилище заданий не прочиталось: {exc}", "rows": [], **frame}
    finally:
        con.close()

    refs = Path(store_dir) / "refs.sqlite"
    fields = dw._load_ref(refs, "fields")
    firms = dw.field_firms(fields, dw._load_ref(refs, "field_groups"), dw._load_ref(refs, "group_folders"))
    rows = label_rows(
        raw,
        firms=firms,
        plates=_plates(refs),
        operations=dw._operation_labels(),
        field_names=dw._field_names(fields),
    )
    return {"available": True, "detail": "", "rows": rows, "records": len(rows), **frame}
