# -*- coding: utf-8 -*-
"""Аналитика поверх хранилища заданий (store.sqlite) — только чтение.

Источник цифр — те же таблицы, что у сводок: ``mtfmi_daily`` (площадь по сменным суткам
[D 06:00, D+1 06:00)) и ``mtfmi_fact`` (строка площади), плюс площадь строк без почасовых данных
(``hourly_n = 0``), которая идёт по дате окончания задания — как ``flat_facts`` в хранилище.
Поэтому суммы совпадают со сводками.

Названия машин, полей и культур берутся из ``refs.sqlite`` (кэш справочников), группы работ — из
``plans.WORK_GROUPS``. Без справочников показываются идентификаторы.

Ежедневные снимки итогов лежат в отдельном файле ``analytics.sqlite`` рядом со хранилищем:
он не стирается при полном пересборе store.sqlite и нужен, чтобы видеть, как цифры за прошлые
даты менялись задним числом (``tools/analytics_snapshot.py``, раз в ночь после синхронизации).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import zlib
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

SHIFT_START_HOUR = 6


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _tz_hours() -> int:
    return int(_env_float("SVODKI_STORE_TZ_HOURS", 3))


def _shift_offset_sec() -> int:
    """Сдвиг epoch → сменная дата: МСК-время минус начало смены (06:00)."""
    return (_tz_hours() - SHIFT_START_HOUR) * 3600


def store_dir() -> Path | None:
    raw = (os.environ.get("SVODKI_TASK_STORE_DIR") or "").strip()
    return Path(raw) if raw else None


# ----------------------------------------------------------------- подключение
def _connect_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


@contextmanager
def store_conn(d: Path | None = None) -> Iterator[sqlite3.Connection]:
    d = d or store_dir()
    if d is None or not (d / "store.sqlite").exists():
        raise FileNotFoundError("store.sqlite не найден (SVODKI_TASK_STORE_DIR)")
    con = _connect_ro(d / "store.sqlite")
    try:
        yield con
    finally:
        con.close()


# ----------------------------------------------------------------- справочники
def _ref_rows(d: Path, endpoint: str) -> list[dict]:
    """Самая полная выгрузка справочника из refs.sqlite (без проверки TTL)."""
    p = d / "refs.sqlite"
    if not p.exists():
        return []
    con = _connect_ro(p)
    try:
        row = con.execute(
            "SELECT rows FROM ref WHERE endpoint=? ORDER BY n DESC, fetched_at DESC LIMIT 1", (endpoint,)
        ).fetchone()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    if not row:
        return []
    try:
        return json.loads(zlib.decompress(row[0]).decode("utf-8"))
    except Exception:
        return []


def _names(d: Path, endpoint: str, *keys: str) -> dict[int, str]:
    out: dict[int, str] = {}
    for r in _ref_rows(d, endpoint):
        try:
            i = int(r["id"])
        except (KeyError, TypeError, ValueError):
            continue
        for k in keys:
            v = str(r.get(k) or "").strip()
            if v:
                out[i] = v
                break
    return out


def work_groups() -> list[tuple[str, list[int]]]:
    try:
        from plans import WORK_GROUPS

        return [(str(n), [int(x) for x in ids]) for n, ids in WORK_GROUPS]
    except Exception:
        return []


def _wts_for(group: str | None) -> list[int]:
    groups = work_groups()
    if group:
        for name, ids in groups:
            if name == group:
                return ids
        raise ValueError(f"неизвестная группа работ: {group}")
    return sorted({w for _, ids in groups for w in ids})


def _group_of_wt() -> dict[int, str]:
    m: dict[int, str] = {}
    for name, ids in work_groups():
        for w in ids:
            m.setdefault(w, name)
    return m


# ----------------------------------------------------------------- общий SQL
def _facts_sql() -> str:
    """Факты площади: (wt, machine_id, field_id, day, ha, task_id, rid) — как сводки."""
    off = _shift_offset_sec()
    return (
        "SELECT t.wt AS wt, t.machine_id AS machine_id, f.field_id AS field_id, d.shift_date AS day, "
        "d.ha AS ha, f.task_id AS task_id, f.rid AS rid "
        "FROM mtfmi_daily d JOIN mtfmi_fact f ON f.rid=d.rid JOIN tasks t ON t.id=f.task_id "
        "UNION ALL "
        f"SELECT t.wt, t.machine_id, f.field_id, date(t.end_ts + ({off}), 'unixepoch'), "
        "f.covered_area, f.task_id, f.rid "
        "FROM mtfmi_fact f JOIN tasks t ON t.id=f.task_id WHERE f.hourly_n=0 AND f.covered_area>0"
    )


def _in(n: int) -> str:
    return ",".join("?" * n)


def _check_dates(date_from: str, date_to: str) -> tuple[str, str]:
    # [date_from, date_to) в сменных датах ISO
    date.fromisoformat(date_from)
    date.fromisoformat(date_to)
    return date_from, date_to


def default_range(con: sqlite3.Connection, days: int = 60) -> tuple[str, str]:
    row = con.execute("SELECT MAX(end_ts) FROM tasks").fetchone()
    hi = datetime.fromtimestamp(row[0], tz=timezone(timedelta(hours=_tz_hours()))).date() if row and row[0] else date.today()
    return (hi - timedelta(days=days)).isoformat(), (hi + timedelta(days=1)).isoformat()


# ----------------------------------------------------------------- обзор
def overview(d: Path | None = None) -> dict[str, Any]:
    d = d or store_dir()
    with store_conn(d) as con:
        n_tasks, lo, hi = con.execute("SELECT COUNT(*), MIN(end_ts), MAX(end_ts) FROM tasks").fetchone()
        n_mt = con.execute("SELECT COUNT(*) FROM mtfmi_tasks").fetchone()[0]
        dlo, dhi = default_range(con, 60)
        meta = dict(con.execute("SELECT k, v FROM meta").fetchall()) if _has_table(con, "meta") else {}
    tz = timezone(timedelta(hours=_tz_hours()))
    iso = lambda t: datetime.fromtimestamp(t, tz=tz).strftime("%Y-%m-%d") if t else None  # noqa: E731
    st = (d / "store.sqlite").stat()
    return {
        "tasks": n_tasks,
        "tasks_with_area_rows": n_mt,
        "first_task_date": iso(lo),
        "last_task_date": iso(hi),
        "store_updated_at": datetime.fromtimestamp(st.st_mtime, tz=tz).strftime("%Y-%m-%d %H:%M"),
        "store_size_mb": round(st.st_size / 1e6, 1),
        "default_date_from": dlo,
        "default_date_to": dhi,
        "groups": [n for n, _ in work_groups()],
        "snapshots": list_snapshots(d),
        "meta_keys": sorted(meta)[:20],
    }


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


# ----------------------------------------------------------------- 1. темп работ
def pace(date_from: str, date_to: str, groups: list[str] | None = None, d: Path | None = None) -> dict[str, Any]:
    """Гектары по сменным дням для каждой группы работ + накопительно + среднее за 7 дней."""
    date_from, date_to = _check_dates(date_from, date_to)
    by_wt = _group_of_wt()
    wanted = set(groups or [n for n, _ in work_groups()])
    wts = sorted(w for w, g in by_wt.items() if g in wanted)
    if not wts:
        return {"days": [], "series": []}
    sql = (f"SELECT wt, day, SUM(ha) FROM ({_facts_sql()}) WHERE wt IN ({_in(len(wts))}) "
           "AND day>=? AND day<? GROUP BY wt, day")
    with store_conn(d) as con:
        rows = con.execute(sql, [*wts, date_from, date_to]).fetchall()
    days: list[str] = []
    cur = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    while cur < end:
        days.append(cur.isoformat())
        cur += timedelta(days=1)
    idx = {x: i for i, x in enumerate(days)}
    acc: dict[str, list[float]] = {}
    for wt, day, ha in rows:
        g = by_wt.get(int(wt), f"wt {wt}")
        acc.setdefault(g, [0.0] * len(days))[idx[day]] += float(ha or 0)
    series = []
    for g, vals in sorted(acc.items()):
        cum, s = [], 0.0
        for v in vals:
            s += v
            cum.append(round(s, 2))
        avg7 = []
        for i in range(len(vals)):
            w = vals[max(0, i - 6): i + 1]
            avg7.append(round(sum(w) / len(w), 2))
        series.append({"group": g, "daily": [round(v, 2) for v in vals], "cumulative": cum,
                       "avg7": avg7, "total": round(s, 2),
                       "last7_avg": avg7[-1] if avg7 else 0.0})
    series.sort(key=lambda x: -x["total"])
    return {"days": days, "series": series}


# ----------------------------------------------------------------- 2. техника
def machines(group: str, date_from: str, date_to: str, limit: int = 300, d: Path | None = None) -> dict[str, Any]:
    date_from, date_to = _check_dates(date_from, date_to)
    wts = _wts_for(group)
    d = d or store_dir()
    sql = (f"SELECT machine_id, day, SUM(ha) FROM ({_facts_sql()}) WHERE wt IN ({_in(len(wts))}) "
           "AND day>=? AND day<? GROUP BY machine_id, day")
    with store_conn(d) as con:
        rows = con.execute(sql, [*wts, date_from, date_to]).fetchall()
    names = _names(d, "machines", "name", "title", "number", "registration_number")
    per: dict[Any, dict[str, float]] = {}
    for mid, day, ha in rows:
        per.setdefault(mid, {})[day] = per.get(mid, {}).get(day, 0.0) + float(ha or 0)
    out = []
    for mid, dd in per.items():
        total = sum(dd.values())
        worked = {k: v for k, v in dd.items() if v > 0}
        if not worked:
            continue
        first, last = min(worked), max(worked)
        n_span = (date.fromisoformat(last) - date.fromisoformat(first)).days + 1  # простой — между первой и последней сменой
        out.append({
            "machine_id": mid,
            "machine": names.get(int(mid), f"машина {mid}") if mid is not None else "без машины",
            "ha": round(total, 2),
            "days_worked": len(worked),
            "avg_ha_per_day": round(total / len(worked), 2),
            "best_day_ha": round(max(worked.values()), 2),
            "first_day": first,
            "last_day": last,
            "idle_days": max(0, n_span - len(worked)),
            "low_days": sum(1 for v in worked.values() if v < 10.0),  # смены с выработкой < 10 га
        })
    out.sort(key=lambda r: -r["ha"])
    avg_all = round(sum(r["ha"] for r in out) / max(1, sum(r["days_worked"] for r in out)), 2)
    return {"group": group, "date_from": date_from, "date_to": date_to, "avg_ha_per_machine_day": avg_all,
            "n_machines": len(out), "rows": out[:limit]}


# ----------------------------------------------------------------- 3. поля × операции
def coverage(date_from: str, date_to: str, d: Path | None = None) -> dict[str, Any]:
    """Для каждой группы работ: сколько полей обработано в окне и какая доля от всех полей."""
    date_from, date_to = _check_dates(date_from, date_to)
    d = d or store_dir()
    by_wt = _group_of_wt()
    wts = sorted(by_wt)
    sql = (f"SELECT wt, COUNT(DISTINCT field_id), SUM(ha) FROM ({_facts_sql()}) WHERE wt IN ({_in(len(wts))}) "
           "AND day>=? AND day<? AND field_id IS NOT NULL GROUP BY wt")
    with store_conn(d) as con:
        rows = con.execute(sql, [*wts, date_from, date_to]).fetchall()
        fields_sql = (f"SELECT wt, field_id FROM ({_facts_sql()}) WHERE wt IN ({_in(len(wts))}) "
                      "AND day>=? AND day<? AND field_id IS NOT NULL GROUP BY wt, field_id")
        pairs = con.execute(fields_sql, [*wts, date_from, date_to]).fetchall()
    total_fields = len(_ref_rows(d, "fields")) or None
    done: dict[str, set[int]] = {}
    for wt, fid in pairs:
        done.setdefault(by_wt.get(int(wt), f"wt {wt}"), set()).add(int(fid))
    ha_by_group: dict[str, float] = {}
    for wt, _n, ha in rows:
        g = by_wt.get(int(wt), f"wt {wt}")
        ha_by_group[g] = ha_by_group.get(g, 0.0) + float(ha or 0)
    out = []
    for name, _ in work_groups():
        n = len(done.get(name, ()))
        out.append({"group": name, "fields_done": n,
                    "pct_of_fields": round(100.0 * n / total_fields, 1) if total_fields else None,
                    "ha": round(ha_by_group.get(name, 0.0), 2)})
    return {"total_fields": total_fields, "date_from": date_from, "date_to": date_to, "rows": out}


def fields_missing(group: str, date_from: str, date_to: str, done_in: str | None = None,
                   limit: int = 500, d: Path | None = None) -> dict[str, Any]:
    """Поля, где операция ``done_in`` выполнена, а ``group`` — нет (например: посев есть, культивации нет)."""
    date_from, date_to = _check_dates(date_from, date_to)
    d = d or store_dir()
    w_group = _wts_for(group)
    w_done = _wts_for(done_in) if done_in else _wts_for(None)

    def _fields(con, wts):
        sql = (f"SELECT field_id, MAX(day), SUM(ha) FROM ({_facts_sql()}) WHERE wt IN ({_in(len(wts))}) "
               "AND day>=? AND day<? AND field_id IS NOT NULL GROUP BY field_id")
        return {int(f): (last, float(ha or 0)) for f, last, ha in con.execute(sql, [*wts, date_from, date_to])}

    with store_conn(d) as con:
        a = _fields(con, w_group)
        b = _fields(con, w_done)
    names = _names(d, "fields", "name", "title")
    rows = [{"field_id": f, "field": names.get(f, f"поле {f}"), "last_day_other": last, "ha_other": round(ha, 2)}
            for f, (last, ha) in b.items() if f not in a]
    rows.sort(key=lambda r: r["last_day_other"] or "")
    return {"group": group, "done_in": done_in or "любая операция", "count": len(rows), "rows": rows[:limit]}


# ----------------------------------------------------------------- 4. культуры
def crops(group: str | None, date_from: str, date_to: str, d: Path | None = None) -> dict[str, Any]:
    """Гектары по культурам (history_item_id строки площади → history_crop)."""
    date_from, date_to = _check_dates(date_from, date_to)
    d = d or store_dir()
    wts = _wts_for(group)
    sql = (
        f"SELECT COALESCE(h.crop, 'без культуры'), SUM(x.ha) FROM ({_facts_sql()}) x "
        "JOIN mtfmi m ON m.rid=x.rid "
        "LEFT JOIN history_crop h ON h.hid = CAST(json_extract(m.data, '$.history_item_id') AS INTEGER) "
        f"WHERE x.wt IN ({_in(len(wts))}) AND x.day>=? AND x.day<? GROUP BY 1 ORDER BY 2 DESC"
    )
    with store_conn(d) as con:
        try:
            rows = con.execute(sql, [*wts, date_from, date_to]).fetchall()
        except sqlite3.OperationalError as exc:  # нет JSON1 / поля history_item_id
            return {"group": group or "все", "rows": [], "error": f"культуры недоступны: {exc}"}
    total = sum(float(r[1] or 0) for r in rows) or 1.0
    return {"group": group or "все группы", "date_from": date_from, "date_to": date_to,
            "rows": [{"crop": c, "ha": round(float(ha or 0), 2), "pct": round(100 * float(ha or 0) / total, 1)}
                     for c, ha in rows]}


# ----------------------------------------------------------------- 5. качество данных
def quality(d: Path | None = None) -> dict[str, Any]:
    with store_conn(d) as con:
        one = lambda sql, *a: con.execute(sql, a).fetchone()[0]  # noqa: E731
        n_tasks = one("SELECT COUNT(*) FROM tasks")
        no_mtfmi = one("SELECT COUNT(*) FROM tasks t WHERE NOT EXISTS (SELECT 1 FROM mtfmi_tasks m WHERE m.task_id=t.id)")
        no_rows = one("SELECT COUNT(*) FROM tasks t WHERE EXISTS (SELECT 1 FROM mtfmi_tasks m WHERE m.task_id=t.id) "
                      "AND NOT EXISTS (SELECT 1 FROM mtfmi_fact f WHERE f.task_id=t.id)")
        no_machine = one("SELECT COUNT(*) FROM tasks WHERE machine_id IS NULL")
        facts, ha_all = con.execute("SELECT COUNT(*), COALESCE(SUM(covered_area),0) FROM mtfmi_fact").fetchone()
        no_hourly, ha_no_hourly = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(covered_area),0) FROM mtfmi_fact WHERE hourly_n=0").fetchone()
        zero = one("SELECT COUNT(*) FROM mtfmi_fact WHERE covered_area<=0")
        no_field = one("SELECT COUNT(*) FROM mtfmi_fact WHERE field_id IS NULL")
        mism = con.execute(
            "SELECT COUNT(*) FROM (SELECT f.rid FROM mtfmi_fact f JOIN mtfmi_daily d ON d.rid=f.rid "
            "WHERE f.hourly_n>0 GROUP BY f.rid, f.covered_area HAVING ABS(SUM(d.ha) - f.covered_area) > 0.5)"
        ).fetchone()[0]
        by_wt = con.execute(
            "SELECT t.wt, COUNT(*) FROM tasks t WHERE NOT EXISTS (SELECT 1 FROM mtfmi_tasks m WHERE m.task_id=t.id) "
            "GROUP BY t.wt ORDER BY 2 DESC LIMIT 10").fetchall()
    g = _group_of_wt()
    pct = lambda a, b: round(100.0 * a / b, 2) if b else 0.0  # noqa: E731
    items = [
        ("Задания без загруженных строк площади (mtfmi)", no_mtfmi, pct(no_mtfmi, n_tasks)),
        ("Задания, у которых строк площади нет вовсе", no_rows, pct(no_rows, n_tasks)),
        ("Задания без машины", no_machine, pct(no_machine, n_tasks)),
        ("Строки площади без почасовых данных (площадь идёт по дате задания)", no_hourly, pct(no_hourly, facts)),
        ("Строки площади с нулевой площадью", zero, pct(zero, facts)),
        ("Строки площади без поля", no_field, pct(no_field, facts)),
        ("Строки, где сумма по суткам ≠ covered_area (> 0,5 га)", mism, pct(mism, facts)),
    ]
    return {
        "tasks": n_tasks, "area_rows": facts, "area_ha": round(float(ha_all), 1),
        "ha_without_hourly": round(float(ha_no_hourly), 1),
        "ha_without_hourly_pct": pct(ha_no_hourly, ha_all),
        "items": [{"title": t, "count": c, "pct": p} for t, c, p in items],
        "tasks_without_mtfmi_by_wt": [{"wt": w, "group": g.get(int(w), ""), "count": c} for w, c in by_wt],
    }


# ----------------------------------------------------------------- 6–7. снимки и «задним числом»
def _snap_db(d: Path, create: bool = False) -> sqlite3.Connection | None:
    p = d / "analytics.sqlite"
    if not p.exists() and not create:
        return None
    if create:
        con = sqlite3.connect(str(p), timeout=60)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE IF NOT EXISTS snap (snap_date TEXT NOT NULL, day TEXT NOT NULL, "
                    "wt INTEGER NOT NULL, ha REAL NOT NULL, PRIMARY KEY (snap_date, day, wt)) WITHOUT ROWID")
        con.execute("CREATE TABLE IF NOT EXISTS snap_meta (snap_date TEXT PRIMARY KEY, taken_at REAL, n_tasks INTEGER)")
        return con
    return _connect_ro(p)


def take_snapshot(d: Path | None = None, days_back: int = 120, snap_date: str | None = None,
                  keep_days: int = 400) -> dict[str, Any]:
    """Записать итоги (сменный день × тип работ) за последние ``days_back`` дней под датой снимка."""
    d = d or store_dir()
    if d is None:
        raise FileNotFoundError("SVODKI_TASK_STORE_DIR не задан")
    snap_date = snap_date or date.today().isoformat()
    dfrom = (date.fromisoformat(snap_date) - timedelta(days=days_back)).isoformat()
    dto = (date.fromisoformat(snap_date) + timedelta(days=2)).isoformat()
    wts = _wts_for(None) or None
    with store_conn(d) as src:
        if wts:
            sql = (f"SELECT day, wt, SUM(ha) FROM ({_facts_sql()}) WHERE wt IN ({_in(len(wts))}) "
                   "AND day>=? AND day<? GROUP BY day, wt")
            rows = src.execute(sql, [*wts, dfrom, dto]).fetchall()
        else:
            sql = f"SELECT day, wt, SUM(ha) FROM ({_facts_sql()}) WHERE day>=? AND day<? GROUP BY day, wt"
            rows = src.execute(sql, [dfrom, dto]).fetchall()
        n_tasks = src.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    out = _snap_db(d, create=True)
    try:
        with out:
            out.execute("DELETE FROM snap WHERE snap_date=?", (snap_date,))
            out.executemany("INSERT INTO snap VALUES (?,?,?,?)",
                            [(snap_date, day, int(wt), round(float(ha or 0), 3)) for day, wt, ha in rows if ha])
            out.execute("INSERT OR REPLACE INTO snap_meta VALUES (?,?,?)", (snap_date, time.time(), n_tasks))
            cutoff = (date.fromisoformat(snap_date) - timedelta(days=keep_days)).isoformat()
            out.execute("DELETE FROM snap WHERE snap_date<?", (cutoff,))
            out.execute("DELETE FROM snap_meta WHERE snap_date<?", (cutoff,))
    finally:
        out.close()
    return {"snap_date": snap_date, "rows": len(rows), "from": dfrom}


def list_snapshots(d: Path | None = None) -> list[str]:
    d = d or store_dir()
    con = _snap_db(d) if d else None
    if con is None:
        return []
    try:
        return [r[0] for r in con.execute("SELECT snap_date FROM snap_meta ORDER BY snap_date DESC")]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def restatements(day_from: str, day_to: str, d: Path | None = None) -> dict[str, Any]:
    """Как менялась площадь за [day_from, day_to) от снимка к снимку (по всем типам работ)."""
    d = d or store_dir()
    con = _snap_db(d) if d else None
    if con is None:
        return {"snapshots": [], "series": [], "note": "снимков пока нет — запустите tools/analytics_snapshot.py"}
    try:
        rows = con.execute(
            "SELECT snap_date, day, SUM(ha) FROM snap WHERE day>=? AND day<? GROUP BY snap_date, day "
            "ORDER BY snap_date, day", (day_from, day_to)).fetchall()
    finally:
        con.close()
    snaps = sorted({r[0] for r in rows})
    days = sorted({r[1] for r in rows})
    table: dict[tuple[str, str], float] = {(s, dy): float(ha) for s, dy, ha in rows}
    series = []
    for dy in days:
        vals = [table.get((s, dy)) for s in snaps]
        known = [v for v in vals if v is not None]
        series.append({"day": dy, "values": [None if v is None else round(v, 2) for v in vals],
                       "first": round(known[0], 2) if known else None,
                       "last": round(known[-1], 2) if known else None,
                       "delta": round(known[-1] - known[0], 2) if len(known) > 1 else 0.0})
    return {"snapshots": snaps, "days": days, "series": series}


def changes_between(snap_a: str, snap_b: str, min_abs_ha: float = 0.5, limit: int = 300,
                    d: Path | None = None) -> dict[str, Any]:
    """Что изменилось между двумя снимками: (сменный день × группа работ), разница в га."""
    d = d or store_dir()
    con = _snap_db(d) if d else None
    if con is None:
        return {"rows": [], "note": "снимков пока нет"}
    try:
        a = {(day, wt): ha for day, wt, ha in con.execute("SELECT day, wt, ha FROM snap WHERE snap_date=?", (snap_a,))}
        b = {(day, wt): ha for day, wt, ha in con.execute("SELECT day, wt, ha FROM snap WHERE snap_date=?", (snap_b,))}
    finally:
        con.close()
    g = _group_of_wt()
    agg: dict[tuple[str, str], list[float]] = {}
    for key in set(a) | set(b):
        day, wt = key
        diff = b.get(key, 0.0) - a.get(key, 0.0)
        cell = agg.setdefault((day, g.get(wt, f"wt {wt}")), [0.0, 0.0, 0.0])
        cell[0] += a.get(key, 0.0)
        cell[1] += b.get(key, 0.0)
        cell[2] += diff
    rows = [{"day": day, "group": grp, "was": round(x[0], 2), "now": round(x[1], 2), "delta": round(x[2], 2)}
            for (day, grp), x in agg.items() if abs(x[2]) >= min_abs_ha]
    rows.sort(key=lambda r: -abs(r["delta"]))
    return {"snap_a": snap_a, "snap_b": snap_b, "count": len(rows),
            "total_delta": round(sum(r["delta"] for r in rows), 2), "rows": rows[:limit]}


# ----------------------------------------------------------------- 8. сезон к сезону
def season_compare(group: str | None, year_a: int, year_b: int, d: Path | None = None) -> dict[str, Any]:
    """Площадь по неделям от 1 января: год A против года B (если в хранилище есть оба)."""
    wts = _wts_for(group)
    res: dict[str, Any] = {"group": group or "все группы", "years": [year_a, year_b], "weeks": list(range(1, 54)), "series": []}
    with store_conn(d) as con:
        for y in (year_a, year_b):
            sql = (f"SELECT day, SUM(ha) FROM ({_facts_sql()}) WHERE wt IN ({_in(len(wts))}) "
                   "AND day>=? AND day<? GROUP BY day")
            rows = con.execute(sql, [*wts, f"{y}-01-01", f"{y + 1}-01-01"]).fetchall()
            weeks = [0.0] * 53
            for day, ha in rows:
                doy = date.fromisoformat(day).timetuple().tm_yday
                weeks[min(52, (doy - 1) // 7)] += float(ha or 0)
            cum, s = [], 0.0
            for v in weeks:
                s += v
                cum.append(round(s, 1))
            res["series"].append({"year": y, "weekly": [round(v, 1) for v in weeks], "cumulative": cum,
                                  "total": round(s, 1)})
    return res
