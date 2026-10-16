# -*- coding: utf-8 -*-
"""Хаб аналитики: готовые срезы по хранилищу заданий (store.sqlite) — только чтение.

Каждый срез — функция ``fn(ctx, con, p) -> dict`` и запись в ``QUERIES``. Все срезы отдают один
формат, поэтому интерфейс рисует их одним и тем же компонентом:

    {columns: [{key, label, type, ...}], rows: [...], summary: [{label, value, tone}],
     note: str, chart: {...} | None, tree: bool, truncated: bool}

Гектары считаются теми же фактами, что и сводки (``mtfmi_daily`` по сменным суткам, плюс строки без
почасовых данных по дате окончания задания), поэтому суммы совпадают с ``svodki_analytics``.

Поля заданий вроде топлива, нормы, остановок и скорости читаются из JSON ``tasks.data`` по списку
вероятных имён. Если в хранилище поля нет, срез честно пишет об этом в ``note`` и не выдумывает цифры.
"""
from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import svodki_analytics as sa

TASK_URL = "https://operations.cropwise.com/machines/{m}/tasks/{t}"
MAX_ROWS = 3000
MAX_DAYS = 800
NO_CROP = "Культура не указана"
NO_FIRM = "Агрофирма не определена"

DRIVER_KEYS = ("driver_id", "operator_id", "worker_id", "user_id")
FUEL_KEYS = ("fuel_consumption", "consumed_fuel", "total_fuel_consumption", "fuel_used", "fuel_l", "fuel")
NORM_KEYS = ("norm_ha_per_hour", "norm_ha_h", "planned_rate")
SPEED_KEYS = ("avg_speed", "average_speed")
STOP_KEYS = ("stops_duration",)
STOP_FIELD_KEYS = ("idle_field_secs",)
STOP_ROAD_KEYS = ("stops_road_secs",)
AREA_KEYS = ("area", "legal_area", "calculated_area", "square", "gis_area")

_DRIVER_SQL = "COALESCE(" + ",".join(
    f"NULLIF(CAST(json_extract(t.data,'$.{k}') AS INTEGER),0)" for k in DRIVER_KEYS
) + ")"


# ----------------------------------------------------------------------------- формат ответа
def col(key: str, label: str, type: str = "text", **kw: Any) -> dict[str, Any]:
    """type: text | int | num | pct | progress | date | link | tag.

    drill: "driver" | "machine" | "field" — значение кликабельно, строка обязана нести ``<drill>_id``
    (интерфейс по нему открывает «Работу исполнителя», «Работу машины» или «Историю поля»).
    """
    return {"key": key, "label": label, "type": type, **kw}


def stat(label: str, value: Any, tone: str | None = None, hint: str | None = None) -> dict[str, Any]:
    return {"label": label, "value": value, "tone": tone, "hint": hint}


def result(title: str, columns: list[dict], rows: list[dict], *, summary: list[dict] | None = None,
           note: str = "", chart: dict | None = None, tree: bool = False) -> dict[str, Any]:
    truncated = len(rows) > MAX_ROWS
    return {
        "title": title, "columns": columns, "rows": rows[:MAX_ROWS], "total_rows": len(rows),
        "summary": summary or [], "note": note, "chart": chart, "tree": tree, "truncated": truncated,
    }


@dataclass
class P:
    date_from: str
    date_to: str  # исключительно, как во всей svodki_analytics
    group: str | None = None
    driver_id: int | None = None
    machine_id: int | None = None
    field_id: int | None = None
    threshold: float | None = None
    firm: str | None = None
    plan: dict = field(default_factory=dict)  # HARDCODED_PLAN с правками редактора


# ----------------------------------------------------------------------------- контекст
class Ctx:
    """Справочники из refs.sqlite, считаются лениво и один раз на запрос."""

    def __init__(self, d: Path | None = None) -> None:
        self.d = d or sa.store_dir()
        if self.d is None or not (self.d / "store.sqlite").exists():
            raise FileNotFoundError("store.sqlite не найден (SVODKI_TASK_STORE_DIR)")
        self._memo: dict[str, Any] = {}

    def _get(self, name: str, fn: Callable[[], Any]) -> Any:
        if name not in self._memo:
            self._memo[name] = fn()
        return self._memo[name]

    @property
    def mtime(self) -> float:
        return (self.d / "store.sqlite").stat().st_mtime

    @property
    def machines(self) -> dict[int, str]:
        return self._get("machines", lambda: sa._names(self.d, "machines", "name", "title", "number", "registration_number"))

    @property
    def field_rows(self) -> list[dict]:
        return self._get("field_rows", lambda: sa._ref_rows(self.d, "fields"))

    @property
    def field_names(self) -> dict[int, str]:
        return self._get("field_names", lambda: sa._names(self.d, "fields", "name", "title"))

    @property
    def work_names(self) -> dict[int, str]:
        return self._get("work_names", lambda: sa._names(self.d, "work_types", "name", "title"))

    @property
    def group_of_wt(self) -> dict[int, str]:
        return self._get("group_of_wt", sa._group_of_wt)

    @property
    def firm_of_field(self) -> dict[int, str]:
        def build() -> dict[int, str]:
            try:
                import driver_work

                return driver_work.field_firms(
                    self.field_rows, sa._ref_rows(self.d, "field_groups"), sa._ref_rows(self.d, "group_folders"))
            except Exception:  # noqa: BLE001 — без справочников фирма просто не определяется
                return {}
        return self._get("firm_of_field", build)

    @property
    def field_area(self) -> dict[int, float]:
        def build() -> dict[int, float]:
            out: dict[int, float] = {}
            for r in self.field_rows:
                try:
                    fid = int(r["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                for k in AREA_KEYS:
                    try:
                        v = float(r.get(k) or 0)
                    except (TypeError, ValueError):
                        continue
                    if v > 0:
                        out[fid] = v
                        break
            return out
        return self._get("field_area", build)

    def group_label(self, wt: Any) -> str:
        try:
            w = int(wt)
        except (TypeError, ValueError):
            return "Вид работ не указан"
        return self.group_of_wt.get(w) or self.work_names.get(w) or f"вид работ {w}"

    def machine(self, mid: Any) -> str:
        if mid is None:
            return "без машины"
        return self.machines.get(int(mid), f"машина {mid}")

    def field(self, fid: Any) -> str:
        if fid is None:
            return "без поля"
        return self.field_names.get(int(fid), f"поле {fid}")

    def firm(self, fid: Any) -> str:
        if fid is None:
            return NO_FIRM
        return self.firm_of_field.get(int(fid)) or NO_FIRM

    def ts(self, day_iso: str) -> float:
        tz = timezone(timedelta(hours=sa._tz_hours()))
        d = date.fromisoformat(day_iso)
        return datetime(d.year, d.month, d.day, sa.SHIFT_START_HOUR, tzinfo=tz).timestamp()


_KEY_CACHE: dict[tuple, bool] = {}


def pick_key(ctx: Ctx, con: sqlite3.Connection, candidates: tuple[str, ...]) -> str | None:
    """Первое имя поля, которое реально есть хотя бы в одном задании (ключи — константы модуля)."""
    for k in candidates:
        ck = (str(ctx.d), ctx.mtime, k)
        if ck not in _KEY_CACHE:
            _KEY_CACHE[ck] = con.execute(
                f"SELECT 1 FROM tasks WHERE json_extract(data,'$.{k}') IS NOT NULL LIMIT 1").fetchone() is not None
        if _KEY_CACHE[ck]:
            return k
    return None


# ----------------------------------------------------------------------------- факты
def _facts_sql() -> str:
    """wt, machine_id, field_id, day, ha, task_id, rid, nh (1 — строка без почасовых данных)."""
    off = sa._shift_offset_sec()
    return (
        "SELECT t.wt AS wt, t.machine_id AS machine_id, f.field_id AS field_id, d.shift_date AS day, "
        "d.ha AS ha, f.task_id AS task_id, f.rid AS rid, 0 AS nh "
        "FROM mtfmi_daily d JOIN mtfmi_fact f ON f.rid=d.rid JOIN tasks t ON t.id=f.task_id "
        "UNION ALL "
        f"SELECT t.wt, t.machine_id, f.field_id, date(t.end_ts + ({off}), 'unixepoch'), "
        "f.covered_area, f.task_id, f.rid, 1 "
        "FROM mtfmi_fact f JOIN tasks t ON t.id=f.task_id WHERE f.hourly_n=0 AND f.covered_area>0"
    )


def _rows(con: sqlite3.Connection, sql: str, args: list) -> list[dict]:
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(sql, args)]


def load_facts(con: sqlite3.Connection, p: P, *, wts: list[int] | None = None, crop: bool = False,
               driver: bool = False, machine_id: int | None = None, field_id: int | None = None) -> list[dict]:
    sel = ["x.wt", "x.machine_id", "x.field_id", "x.day", "x.ha", "x.task_id", "x.rid", "x.nh"]
    joins = ""
    if driver:
        sel.append(f"{_DRIVER_SQL} AS driver")
        joins += " JOIN tasks t ON t.id=x.task_id"
    where = "x.day>=? AND x.day<? AND x.ha>0"
    args: list = [p.date_from, p.date_to]
    if wts:
        where += f" AND x.wt IN ({sa._in(len(wts))})"
        args += wts
    if machine_id is not None:
        where += " AND x.machine_id=?"
        args.append(machine_id)
    if field_id is not None:
        where += " AND x.field_id=?"
        args.append(field_id)
    base = f"FROM ({_facts_sql()}) x{joins}"
    if crop:
        crop_joins = (" LEFT JOIN mtfmi m ON m.rid=x.rid LEFT JOIN history_crop h "
                      "ON h.hid=CAST(json_extract(m.data,'$.history_item_id') AS INTEGER)")
        try:
            return _rows(con, f"SELECT {','.join(sel)}, h.crop AS crop {base}{crop_joins} WHERE {where}", args)
        except sqlite3.OperationalError:
            pass  # нет history_crop / JSON1 — культуры недоступны
    rows = _rows(con, f"SELECT {','.join(sel)} {base} WHERE {where}", args)
    if crop:
        for r in rows:
            r["crop"] = None
    return rows


def _wts(p: P) -> list[int] | None:
    return sa._wts_for(p.group) if p.group else None


def _wt_clause(wts: list[int] | None, alias: str = "t") -> tuple[str, list]:
    if not wts:
        return "", []
    return f" AND {alias}.wt IN ({sa._in(len(wts))})", list(wts)


def _url(mid: Any, tid: Any) -> str:
    return TASK_URL.format(m=mid, t=tid) if mid and tid else ""


def _r2(v: float) -> float:
    return round(float(v), 2)


def _med(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def _pct(a: float, b: float) -> float:
    return round(100.0 * a / b, 1) if b else 0.0


def _no_data(title: str, columns: list[dict], note: str) -> dict[str, Any]:
    return result(title, columns, [], note=note)


# ============================================================================ ЛЮДИ
def people_rating(ctx: Ctx, con: sqlite3.Connection, p: P) -> dict:
    facts = load_facts(con, p, wts=_wts(p), driver=True)
    per: dict[int, dict] = {}
    total = no_driver = 0.0
    for r in facts:
        total += r["ha"]
        did = r["driver"]
        if not did:
            no_driver += r["ha"]
            continue
        rec = per.setdefault(did, {"days": defaultdict(float), "tasks": set(), "machines": set(),
                                   "groups": defaultdict(float), "ha": 0.0})
        rec["ha"] += r["ha"]
        rec["days"][r["day"]] += r["ha"]
        rec["tasks"].add(r["task_id"])
        rec["machines"].add(r["machine_id"])
        rec["groups"][ctx.group_label(r["wt"])] += r["ha"]
    rows = []
    for did, rec in per.items():
        n = len(rec["days"])
        rows.append({
            "driver_id": did, "driver": f"id {did}", "ha": _r2(rec["ha"]), "shifts": n,
            "avg": _r2(rec["ha"] / n), "best": _r2(max(rec["days"].values())), "tasks": len(rec["tasks"]),
            "machines": len(rec["machines"]), "group": max(rec["groups"].items(), key=lambda kv: kv[1])[0],
        })
    rows.sort(key=lambda r: -r["ha"])
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    cols = [col("rank", "№", "int"), col("driver", "Исполнитель", drill="driver"), col("ha", "Га", "num"),
            col("shifts", "Смен", "int"), col("avg", "Га за смену", "num"), col("best", "Лучшая смена, га", "num"),
            col("tasks", "Заданий", "int"), col("machines", "Машин", "int"), col("group", "Основная операция")]
    nd_pct = _pct(no_driver, total)
    return result(
        "Рейтинг исполнителей", cols, rows,
        summary=[stat("Исполнителей", len(rows)), stat("Всего, га", _r2(total - no_driver)),
                 stat("Медиана га за смену", _r2(_med([r["avg"] for r in rows]))),
                 stat("Без исполнителя, га", _r2(no_driver), "warn" if nd_pct > 5 else None, f"{nd_pct}% от всех")],
        chart={"type": "bar", "x": "driver", "series": [{"key": "ha", "label": "Га"}], "limit": 15, "horizontal": True},
        note="Сравнивайте людей внутри одной операции: выберите группу работ, иначе уборка и опрыскивание несопоставимы.")


def people_low(ctx: Ctx, con: sqlite3.Connection, p: P) -> dict:
    thr = p.threshold or 10.0
    per: dict[tuple, dict] = {}
    for r in load_facts(con, p, wts=_wts(p), driver=True):
        rec = per.setdefault((r["driver"], r["machine_id"], r["day"]),
                             {"ha": 0.0, "fields": set(), "tasks": set(), "groups": set(), "first": r["task_id"]})
        rec["ha"] += r["ha"]
        rec["fields"].add(r["field_id"])
        rec["tasks"].add(r["task_id"])
        rec["groups"].add(ctx.group_label(r["wt"]))
    rows = []
    for (did, mid, day), rec in per.items():
        if rec["ha"] >= thr:
            continue
        rows.append({"day": day, "driver_id": did, "driver": f"id {did}" if did else "не указан",
                     "machine_id": mid, "machine": ctx.machine(mid), "group": ", ".join(sorted(rec["groups"])),
                     "ha": _r2(rec["ha"]), "fields": len(rec["fields"]), "tasks": len(rec["tasks"]),
                     "task_id": rec["first"], "task_url": _url(mid, rec["first"])})
    rows.sort(key=lambda r: r["ha"])
    rows.sort(key=lambda r: r["day"], reverse=True)
    cols = [col("day", "Смена", "date"), col("driver", "Исполнитель", drill="driver"), col("machine", "Машина", drill="machine"),
            col("group", "Операция"), col("ha", "Га", "num"), col("fields", "Полей", "int"),
            col("task_id", "Задание", "link", href="task_url")]
    low_ha = [r["ha"] for r in rows]
    return result(
        f"Малая выработка (< {thr:g} га за смену)", cols, rows,
        summary=[stat("Смен ниже порога", len(rows), "warn" if rows else None),
                 stat("Доля от всех смен", f"{_pct(len(rows), len(per))}%"),
                 stat("Исполнителей", len({r['driver_id'] for r in rows})),
                 stat("Средняя в таких сменах, га", _r2(sum(low_ha) / len(low_ha)) if low_ha else 0)],
        note="Смена = исполнитель × машина × сменные сутки (с 06:00). Короткая смена бывает законной: "
             "довыполнение поля, переезд, непогода. Порог меняется в фильтрах.")


def people_load(ctx: Ctx, con: sqlite3.Connection, p: P) -> dict:
    per: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in load_facts(con, p, wts=_wts(p), driver=True):
        if r["driver"]:
            per[r["driver"]][r["day"]] += r["ha"]
    rows = []
    for did, days in per.items():
        ds = sorted(date.fromisoformat(d) for d in days)
        span = (ds[-1] - ds[0]).days + 1
        streak = best = 1
        for a, b in zip(ds, ds[1:]):
            streak = streak + 1 if (b - a).days == 1 else 1
            best = max(best, streak)
        vals = list(days.values())
        low = sum(1 for v in vals if v < 10.0)
        flag = "без выходных" if best >= 14 else ("много пропусков" if span >= 14 and len(ds) / span < 0.4 else "")
        rows.append({"driver_id": did, "driver": f"id {did}", "shifts": len(ds), "first": ds[0].isoformat(),
                     "last": ds[-1].isoformat(), "gaps": span - len(ds), "streak": best,
                     "low_pct": _pct(low, len(vals)), "avg": _r2(sum(vals) / len(vals)),
                     "best": _r2(max(vals)), "stdev": _r2(statistics.pstdev(vals)) if len(vals) > 1 else 0.0,
                     "flag": flag})
    rows.sort(key=lambda r: (-r["streak"], -r["gaps"]))
    cols = [col("driver", "Исполнитель", drill="driver"), col("shifts", "Смен", "int"), col("first", "Первая", "date"),
            col("last", "Последняя", "date"), col("gaps", "Дней без работы внутри периода", "int"),
            col("streak", "Макс. смен подряд", "int"), col("low_pct", "Смен < 10 га, %", "pct"),
            col("avg", "Га за смену", "num"), col("stdev", "Разброс (σ), га", "num"), col("flag", "Метка", "tag")]
    return result(
        "Нагрузка и «пустые» смены", cols, rows,
        summary=[stat("Исполнителей", len(rows)),
                 stat("14+ смен подряд", sum(1 for r in rows if r["streak"] >= 14), "warn"),
                 stat("Со многими пропусками", sum(1 for r in rows if r["flag"] == "много пропусков"))],
        note="«Дни без работы» считаются между первой и последней сменой человека. Большой разброс (σ) "
             "значит нестабильную выработку: полезно сверить с телеметрией.")


# ============================================================================ ТЕХНИКА
def machine_work(ctx: Ctx, con: sqlite3.Connection, p: P) -> dict:
    cols = [col("day", "Смена", "date"), col("firm", "Агрофирма"), col("field", "Поле", drill="field"), col("crop", "Культура"),
            col("group", "Операция"), col("driver", "Исполнитель", drill="driver"), col("ha", "Га", "num"),
            col("task_id", "Задание", "link", href="task_url")]
    if not p.machine_id:
        return _no_data("Работа машины", cols, "Выберите машину в фильтрах.")
    facts = load_facts(con, p, wts=_wts(p), crop=True, driver=True, machine_id=p.machine_id)
    agg: dict[tuple, dict] = {}
    for r in facts:
        k = (r["day"], r["field_id"], r["task_id"])
        rec = agg.setdefault(k, {"day": r["day"], "firm": ctx.firm(r["field_id"]), "field": ctx.field(r["field_id"]),
                                 "crop": r["crop"] or NO_CROP, "group": ctx.group_label(r["wt"]),
                                 "driver_id": r["driver"], "driver": f"id {r['driver']}" if r["driver"] else "не указан",
                                 "ha": 0.0, "task_id": r["task_id"], "task_url": _url(p.machine_id, r["task_id"]),
                                 "field_id": r["field_id"]})
        rec["ha"] += r["ha"]
    rows = sorted(agg.values(), key=lambda r: (r["day"], r["field"]))
    for r in rows:
        r["ha"] = _r2(r["ha"])
    by_day: dict[str, float] = defaultdict(float)
    for r in rows:
        by_day[r["day"]] += r["ha"]
    total = sum(by_day.values())
    return result(
        f"Работа машины: {ctx.machine(p.machine_id)}", cols, rows,
        summary=[stat("Всего, га", _r2(total)), stat("Смен", len(by_day)),
                 stat("Га за смену", _r2(total / len(by_day)) if by_day else 0),
                 stat("Полей", len({r["field_id"] for r in rows})),
                 stat("Исполнителей", len({r["driver_id"] for r in rows if r["driver_id"]}))],
        chart={"type": "line", "x": "day", "series": [{"key": "ha", "label": "Га"}],
               "data": [{"day": d, "ha": _r2(v)} for d, v in sorted(by_day.items())]})


def machine_idle(ctx: Ctx, con: sqlite3.Connection, p: P) -> dict:
    wts = _wts(p)
    per: dict[Any, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in load_facts(con, p, wts=wts):
        per[r["machine_id"]][r["day"]] += r["ha"]
    clause, cargs = _wt_clause(wts)
    keys = {"stop": pick_key(ctx, con, STOP_KEYS), "field": pick_key(ctx, con, STOP_FIELD_KEYS),
            "road": pick_key(ctx, con, STOP_ROAD_KEYS)}
    tele: dict[Any, dict[str, float]] = defaultdict(dict)
    for name, k in keys.items():
        if not k:
            continue
        sql = (f"SELECT t.machine_id AS m, SUM(CAST(json_extract(t.data,'$.{k}') AS REAL)) AS v FROM tasks t "
               f"WHERE t.end_ts>=? AND t.end_ts<?{clause} AND json_extract(t.data,'$.{k}') IS NOT NULL GROUP BY t.machine_id")
        for r in _rows(con, sql, [ctx.ts(p.date_from), ctx.ts(p.date_to), *cargs]):
            tele[r["m"]][name] = float(r["v"] or 0) / 3600.0
    rows = []
    for mid, days in per.items():
        ds = sorted(date.fromisoformat(d) for d in days)
        span = (ds[-1] - ds[0]).days + 1
        longest = 0
        for a, b in zip(ds, ds[1:]):
            gap = (b - a).days - 1
            longest = max(longest, gap)
        row = {"machine_id": mid, "machine": ctx.machine(mid), "shifts": len(ds), "ha": _r2(sum(days.values())),
               "idle_days": span - len(ds), "longest_gap": longest, "last_day": ds[-1].isoformat(),
               "stop_h": None, "field_h": None, "road_h": None}
        t = tele.get(mid, {})
        for name, key in (("stop", "stop_h"), ("field", "field_h"), ("road", "road_h")):
            if name in t:
                row[key] = round(t[name], 1)
        rows.append(row)
    rows.sort(key=lambda r: (-r["idle_days"], -r["longest_gap"]))
    cols = [col("machine", "Машина", drill="machine"), col("shifts", "Смен", "int"), col("ha", "Га", "num"),
            col("idle_days", "Дней без работы", "int"), col("longest_gap", "Самый долгий перерыв, дн.", "int"),
            col("last_day", "Последняя смена", "date")]
    used = []
    if keys["stop"]:
        cols.append(col("stop_h", "Остановки всего, ч", "num")); used.append(keys["stop"])
    if keys["field"]:
        cols.append(col("field_h", "Простой в поле, ч", "num")); used.append(keys["field"])
    if keys["road"]:
        cols.append(col("road_h", "Остановки на дороге, ч", "num")); used.append(keys["road"])
    note = ("Дни без работы считаются между первой и последней сменой машины. "
            + (f"Остановки по телеметрии (поля {', '.join(used)}, секунды → часы) считаются по заданиям, завершённым в периоде."
               if used else "Телеметрии остановок в хранилище не нашлось, поэтому показаны только дни без работы."))
    tot_h = sum((r["stop_h"] or 0) for r in rows)
    summary = [stat("Машин", len(rows)),
               stat("Дней простоя, ср. на машину", _r2(sum(r["idle_days"] for r in rows) / len(rows)) if rows else 0)]
    if keys["stop"]:
        summary.append(stat("Остановок, ч", round(tot_h, 1)))
    return result("Простои техники", cols, rows, summary=summary, note=note)


def _task_window_sql(ctx: Ctx, p: P, wts: list[int] | None, extra_sel: str, extra_where: str, group_by: str) -> tuple[str, list]:
    clause, cargs = _wt_clause(wts)
    sql = (f"SELECT {extra_sel} FROM tasks t "
           "JOIN (SELECT task_id, SUM(covered_area) AS ha FROM mtfmi_fact GROUP BY task_id) fa ON fa.task_id=t.id "
           f"WHERE t.end_ts>=? AND t.end_ts<?{clause}{extra_where} GROUP BY {group_by}")
    return sql, [ctx.ts(p.date_from), ctx.ts(p.date_to), *cargs]


def fuel(ctx: Ctx, con: sqlite3.Connection, p: P) -> dict:
    cols = [col("machine", "Машина", drill="machine"), col("group", "Операция"), col("fuel", "Топливо, л", "num"),
            col("ha", "Га", "num"), col("lha", "Л/га", "num"), col("tasks", "Заданий", "int"), col("flag", "Метка", "tag")]
    k = pick_key(ctx, con, FUEL_KEYS)
    if not k:
        return _no_data("Расход топлива", cols,
                        "В хранилище заданий нет поля расхода топлива (искал: " + ", ".join(FUEL_KEYS) + "). "
                        "Пока оно не сохраняется в store.sqlite, срез недоступен.")
    v = f"CAST(json_extract(t.data,'$.{k}') AS REAL)"
    sql, args = _task_window_sql(ctx, p, _wts(p), f"t.machine_id AS m, t.wt AS wt, SUM({v}) AS fuel, SUM(fa.ha) AS ha, COUNT(*) AS n",
                                 f" AND {v}>0", "t.machine_id, t.wt")
    agg: dict[tuple, dict] = {}
    for r in _rows(con, sql, args):
        rec = agg.setdefault((r["m"], ctx.group_label(r["wt"])), {"fuel": 0.0, "ha": 0.0, "n": 0})
        rec["fuel"] += r["fuel"] or 0
        rec["ha"] += r["ha"] or 0
        rec["n"] += r["n"]
    by_group: dict[str, list[float]] = defaultdict(list)
    for (m, g), rec in agg.items():
        if rec["ha"] >= 1:
            by_group[g].append(rec["fuel"] / rec["ha"])
    med = {g: _med(v) for g, v in by_group.items()}
    rows = []
    for (m, g), rec in agg.items():
        if rec["ha"] < 1:
            continue
        lha = rec["fuel"] / rec["ha"]
        base = med.get(g) or 0
        flag = ""
        if base and len(by_group[g]) >= 3:
            flag = "расход выше нормы" if lha > 1.5 * base else ("подозрительно мало" if lha < 0.5 * base else "")
        rows.append({"machine_id": m, "machine": ctx.machine(m), "group": g, "fuel": round(rec["fuel"], 1),
                     "ha": _r2(rec["ha"]), "lha": _r2(lha), "tasks": rec["n"], "flag": flag})
    rows.sort(key=lambda r: -r["lha"])
    tf, th = sum(r["fuel"] for r in rows), sum(r["ha"] for r in rows)
    return result(
        "Расход топлива на гектар", cols, rows,
        summary=[stat("Топливо, л", round(tf)), stat("Га", _r2(th)), stat("Л/га в среднем", _r2(tf / th) if th else 0),
                 stat("С выбросами", sum(1 for r in rows if r["flag"]), "warn")],
        chart={"type": "bar", "x": "label", "series": [{"key": "v", "label": "Л/га (медиана по машинам)"}],
               "data": [{"label": g, "v": _r2(m)} for g, m in sorted(med.items(), key=lambda kv: -kv[1])][:12]},
        note=f"Поле топлива: {k} (считаем литрами). Берутся задания, завершённые в периоде, с полной площадью задания. "
             "Метка ставится, если расход отличается от медианы группы работ более чем в 1,5 раза.")


def norm_vs_fact(ctx: Ctx, con: sqlite3.Connection, p: P) -> dict:
    nk = pick_key(ctx, con, NORM_KEYS)
    clause, cargs = _wt_clause(_wts(p))
    norm_sel = f", CAST(json_extract(t.data,'$.{nk}') AS REAL) AS norm" if nk else ", NULL AS norm"
    sql = ("SELECT t.machine_id AS m, t.wt AS wt, f.covered_area AS ha, f.hourly_n AS h" + norm_sel +
           " FROM mtfmi_fact f JOIN tasks t ON t.id=f.task_id "
           f"WHERE t.end_ts>=? AND t.end_ts<? AND f.hourly_n>0 AND f.covered_area>0{clause}")
    agg: dict[tuple, dict] = {}
    for r in _rows(con, sql, [ctx.ts(p.date_from), ctx.ts(p.date_to), *cargs]):
        rec = agg.setdefault((r["m"], ctx.group_label(r["wt"])), {"ha": 0.0, "h": 0.0, "nha": 0.0, "nw": 0.0})
        rec["ha"] += r["ha"]
        rec["h"] += r["h"]
        if r["norm"] and r["norm"] > 0:
            rec["nha"] += r["ha"]
            rec["nw"] += r["ha"] * r["norm"]
    by_group: dict[str, list[float]] = defaultdict(list)
    for (m, g), rec in agg.items():
        if rec["ha"] >= 5 and rec["h"] > 0:
            by_group[g].append(rec["ha"] / rec["h"])
    med = {g: _med(v) for g, v in by_group.items()}
    rows = []
    for (m, g), rec in agg.items():
        if rec["ha"] < 5 or rec["h"] <= 0:
            continue
        rate = rec["ha"] / rec["h"]
        norm = rec["nw"] / rec["nha"] if rec["nha"] else None
        vs_norm = _pct(rate, norm) if norm else None
        vs_med = _pct(rate, med[g]) if med.get(g) else None
        ref = vs_norm if vs_norm is not None else vs_med
        flag = "ниже нормы" if (ref is not None and ref < 70) else ("выше нормы" if ref is not None and ref > 130 else "")
        rows.append({"machine_id": m, "machine": ctx.machine(m), "group": g, "ha": _r2(rec["ha"]), "hours": _r2(rec["h"]),
                     "rate": _r2(rate), "norm": _r2(norm) if norm else None, "vs_norm": vs_norm,
                     "vs_med": vs_med, "flag": flag})
    rows.sort(key=lambda r: (r["vs_norm"] if r["vs_norm"] is not None else (r["vs_med"] if r["vs_med"] is not None else 999)))
    cols = [col("machine", "Машина", drill="machine"), col("group", "Операция"), col("ha", "Га", "num"), col("hours", "Часов", "num"),
            col("rate", "Факт, га/ч", "num")]
    if nk:
        cols += [col("norm", "Норма, га/ч", "num"), col("vs_norm", "Факт к норме, %", "pct")]
    cols += [col("vs_med", "К медиане группы, %", "pct"), col("flag", "Метка", "tag")]
    note = ("Факт = га ÷ число почасовых отметок телеметрии (одна отметка = час). " +
            (f"Норма берётся из поля заданий {nk}, усреднённого по площади." if nk else
             "Нормы выработки в хранилище нет, поэтому сравнение идёт с медианой своей группы работ.") +
            " Метка: меньше 70% или больше 130% от опорного значения. Машины до 5 га не берутся.")
    return result("Факт против нормы выработки", cols, rows,
                  summary=[stat("Пар машина × операция", len(rows)),
                           stat("Ниже нормы", sum(1 for r in rows if r["flag"] == "ниже нормы"), "warn"),
                           stat("Выше нормы", sum(1 for r in rows if r["flag"] == "выше нормы"))],
                  note=note)


def fleet_load(ctx: Ctx, con: sqlite3.Connection, p: P) -> dict:
    days: dict[str, dict] = defaultdict(lambda: {"m": set(), "d": set(), "ha": 0.0})
    for r in load_facts(con, p, wts=_wts(p), driver=True):
        rec = days[r["day"]]
        rec["m"].add(r["machine_id"])
        rec["ha"] += r["ha"]
        if r["driver"]:
            rec["d"].add(r["driver"])
    fleet = len(ctx.machines)
    rows = [{"day": d, "machines": len(v["m"]), "drivers": len(v["d"]), "ha": _r2(v["ha"]),
             "per_machine": _r2(v["ha"] / len(v["m"])) if v["m"] else 0,
             "load": _pct(len(v["m"]), fleet) if fleet else None} for d, v in sorted(days.items())]
    cols = [col("day", "Смена", "date"), col("machines", "Машин в работе", "int"), col("drivers", "Исполнителей", "int"),
            col("ha", "Га", "num"), col("per_machine", "Га на машину", "num")]
    if fleet:
        cols.append(col("load", "Загрузка парка, %", "progress"))
    peak = max(rows, key=lambda r: r["machines"]) if rows else None
    return result(
        "Загрузка парка", cols, rows,
        summary=[stat("Машин в парке (справочник)", fleet or "—"),
                 stat("В среднем в день", _r2(sum(r["machines"] for r in rows) / len(rows)) if rows else 0),
                 stat("Пик", f"{peak['machines']} ({peak['day']})" if peak else "—"),
                 stat("Га на машину в среднем", _r2(sum(r["per_machine"] for r in rows) / len(rows)) if rows else 0)],
        chart={"type": "line", "x": "day", "series": [{"key": "machines", "label": "Машин в работе"}]},
        note="Парк берётся из справочника машин Cropwise целиком (включая технику не на этих операциях), "
             "поэтому для узкой группы работ процент загрузки занижен.")


def speed(ctx: Ctx, con: sqlite3.Connection, p: P) -> dict:
    cols = [col("machine", "Машина", drill="machine"), col("group", "Операция"), col("tasks", "Заданий", "int"),
            col("avg", "Скорость, км/ч", "num"), col("med", "Медиана группы", "num"),
            col("dev", "Отклонение, %", "pct"), col("flag", "Метка", "tag")]
    k = pick_key(ctx, con, SPEED_KEYS)
    if not k:
        return _no_data("Средняя скорость", cols,
                        "В хранилище заданий нет поля скорости (искал: " + ", ".join(SPEED_KEYS) + ").")
    clause, cargs = _wt_clause(_wts(p))
    v = f"CAST(json_extract(t.data,'$.{k}') AS REAL)"
    sql = (f"SELECT t.machine_id AS m, t.wt AS wt, {v} AS s FROM tasks t "
           f"WHERE t.end_ts>=? AND t.end_ts<? AND {v}>0{clause}")
    agg: dict[tuple, list[float]] = defaultdict(list)
    for r in _rows(con, sql, [ctx.ts(p.date_from), ctx.ts(p.date_to), *cargs]):
        agg[(r["m"], ctx.group_label(r["wt"]))].append(r["s"])
    by_group: dict[str, list[float]] = defaultdict(list)
    for (m, g), vals in agg.items():
        by_group[g].extend(vals)
    med = {g: _med(v) for g, v in by_group.items()}
    rows = []
    for (m, g), vals in agg.items():
        avg = sum(vals) / len(vals)
        dev = round((avg / med[g] - 1) * 100, 1) if med.get(g) else 0.0
        rows.append({"machine_id": m, "machine": ctx.machine(m), "group": g, "tasks": len(vals), "avg": _r2(avg),
                     "med": _r2(med[g]), "dev": dev,
                     "flag": "быстрее обычного" if dev > 40 else ("медленнее обычного" if dev < -40 else "")})
    rows.sort(key=lambda r: -abs(r["dev"]))
    return result("Средняя скорость", cols, rows,
                  summary=[stat("Пар машина × операция", len(rows)), stat("С отклонением > 40%", sum(1 for r in rows if r["flag"]), "warn")],
                  note=f"Поле скорости: {k}. Скорость слишком выше или ниже типичной для операции может говорить "
                       "о нарушении технологии или о неверной разметке задания.")


# ============================================================================ ПОЛЯ И КУЛЬТУРЫ
def field_history(ctx: Ctx, con: sqlite3.Connection, p: P) -> dict:
    cols = [col("day", "Смена", "date"), col("group", "Операция"), col("crop", "Культура"), col("machine", "Машина", drill="machine"),
            col("driver", "Исполнитель", drill="driver"), col("ha", "Га", "num"), col("task_id", "Задание", "link", href="task_url")]
    if not p.field_id:
        return _no_data("История поля", cols, "Выберите поле в фильтрах.")
    agg: dict[tuple, dict] = {}
    for r in load_facts(con, p, wts=_wts(p), crop=True, driver=True, field_id=p.field_id):
        k = (r["day"], r["task_id"])
        rec = agg.setdefault(k, {"day": r["day"], "group": ctx.group_label(r["wt"]), "crop": r["crop"] or NO_CROP,
                                 "machine_id": r["machine_id"], "machine": ctx.machine(r["machine_id"]), "driver_id": r["driver"],
                                 "driver": f"id {r['driver']}" if r["driver"] else "не указан", "ha": 0.0,
                                 "task_id": r["task_id"], "task_url": _url(r["machine_id"], r["task_id"])})
        rec["ha"] += r["ha"]
    rows = sorted(agg.values(), key=lambda r: r["day"])
    for r in rows:
        r["ha"] = _r2(r["ha"])
    by_group: dict[str, float] = defaultdict(float)
    for r in rows:
        by_group[r["group"]] += r["ha"]
    area = ctx.field_area.get(p.field_id)
    summary = [stat("Агрофирма", ctx.firm(p.field_id)), stat("Площадь поля, га", _r2(area) if area else "—"),
               stat("Операций", len(by_group)), stat("Смен с работой", len({r["day"] for r in rows}))]
    return result(f"История поля: {ctx.field(p.field_id)}", cols, rows, summary=summary,
                  chart={"type": "bar", "x": "label", "series": [{"key": "v", "label": "Га"}],
                         "data": [{"label": g, "v": _r2(v)} for g, v in sorted(by_group.items(), key=lambda kv: -kv[1])],
                         "horizontal": True},
                  note="Все операции на поле за выбранный период по порядку. Расширьте период на весь сезон.")


def plan_progress(ctx: Ctx, con: sqlite3.Connection, p: P) -> dict:
    cols = [col("group", "Операция"), col("firm", "Агрофирма"), col("crop", "Культура"), col("plan", "План, га", "num"),
            col("fact", "Факт, га", "num"), col("pct", "Выполнено", "progress"), col("left", "Осталось, га", "num"),
            col("pace", "Темп 7 дн., га/сут", "num"), col("eta", "Закончим при таком темпе", "date")]
    plan = p.plan or {}
    if not plan:
        return _no_data("Прогресс против плана", cols, "План (HARDCODED_PLAN) не загружен.")
    fact: dict[tuple, float] = defaultdict(float)
    recent: dict[tuple, float] = defaultdict(float)
    last = date.fromisoformat(p.date_to) - timedelta(days=1)
    cut = (last - timedelta(days=6)).isoformat()
    for r in load_facts(con, p, wts=_wts(p), crop=True):
        k = (ctx.group_label(r["wt"]), ctx.firm(r["field_id"]), r["crop"] or NO_CROP)
        fact[k] += r["ha"]
        if r["day"] >= cut:
            recent[k] += r["ha"]
    keys: set[tuple] = set()
    for g, firms in plan.items():
        if p.group and g != p.group:
            continue
        for f, crops in (firms or {}).items():
            for c, v in (crops or {}).items():
                try:
                    if float(v) > 0:
                        keys.add((g, f, c))
                except (TypeError, ValueError):
                    pass
    keys |= {k for k, v in fact.items() if v >= 0.5}
    rows = []
    for k in keys:
        g, f, c = k
        try:
            pl = float((plan.get(g) or {}).get(f, {}).get(c, 0) or 0)
        except (TypeError, ValueError):
            pl = 0.0
        fa = fact.get(k, 0.0)
        if pl <= 0 and fa < 0.5:
            continue
        pace = recent.get(k, 0.0) / 7.0
        left = max(pl - fa, 0.0)
        eta = ""
        if pl > 0 and left > 0 and pace > 0:
            eta = (last + timedelta(days=int(left / pace) + 1)).isoformat()
        rows.append({"group": g, "firm": f, "crop": c, "plan": _r2(pl), "fact": _r2(fa),
                     "pct": _pct(fa, pl) if pl > 0 else None, "left": _r2(left) if pl > 0 else None,
                     "pace": _r2(pace), "eta": eta})
    rows.sort(key=lambda r: (r["group"], r["firm"], -r["plan"]))
    tp, tf = sum(r["plan"] for r in rows), sum(min(r["fact"], r["plan"]) for r in rows if r["plan"] > 0)
    return result(
        "Прогресс против плана", cols, rows,
        summary=[stat("План, га", _r2(tp)), stat("Выполнено, га", _r2(tf)), stat("Доля", f"{_pct(tf, tp)}%"),
                 stat("Строк без плана", sum(1 for r in rows if not r["plan"]), "warn")],
        note="План — эффективный HARDCODED_PLAN из редактора планов. Факт — по культуре строки площади и агрофирме поля. "
             "Для сезонных операций ставьте начало периода на начало сезона. Дата окончания — оценка по темпу последних 7 дней.")


def fields_idle(ctx: Ctx, con: sqlite3.Connection, p: P) -> dict:
    cols = [col("field", "Поле", drill="field"), col("firm", "Агрофирма"), col("area", "Площадь, га", "num"),
            col("last_day", "Последняя работа (любая)", "date"), col("days_since", "Дней назад", "int")]
    names = ctx.field_names
    if not names:
        return _no_data("Поля без работ", cols, "В кэше справочников нет списка полей (refs.sqlite).")
    worked = {r["field_id"] for r in load_facts(con, p, wts=_wts(p)) if r["field_id"] is not None}
    last_any = {r["f"]: r["d"] for r in _rows(
        con, f"SELECT x.field_id AS f, MAX(x.day) AS d FROM ({_facts_sql()}) x WHERE x.ha>0 AND x.field_id IS NOT NULL GROUP BY x.field_id", [])}
    end = date.fromisoformat(p.date_to) - timedelta(days=1)
    rows = []
    for fid, name in names.items():
        if fid in worked:
            continue
        ld = last_any.get(fid)
        rows.append({"field_id": fid, "field": name, "firm": ctx.firm(fid), "area": _r2(ctx.field_area.get(fid, 0)) or None,
                     "last_day": ld, "days_since": (end - date.fromisoformat(ld)).days if ld else None})
    rows.sort(key=lambda r: -(r["area"] or 0))
    idle_area = sum(r["area"] or 0 for r in rows)
    what = f"операции «{p.group}»" if p.group else "любой операции"
    return result(
        "Поля без работ", cols, rows,
        summary=[stat("Полей в справочнике", len(names)), stat(f"Без {what}", len(rows), "warn" if rows else None),
                 stat("Их площадь, га", _r2(idle_area)), stat("Доля полей", f"{_pct(len(rows), len(names))}%")],
        note="В справочнике есть и поля вне оборота (залежь, чужие), поэтому часть строк ожидаема. Выберите группу работ, "
             "чтобы увидеть, где конкретно операции не было.")


def repeat_passes(ctx: Ctx, con: sqlite3.Connection, p: P) -> dict:
    thr = p.threshold or 1.15
    agg: dict[tuple, dict] = {}
    for r in load_facts(con, p, wts=_wts(p)):
        if r["field_id"] is None:
            continue
        rec = agg.setdefault((r["field_id"], ctx.group_label(r["wt"])), {"ha": 0.0, "days": set(), "tasks": set()})
        rec["ha"] += r["ha"]
        rec["days"].add(r["day"])
        rec["tasks"].add(r["task_id"])
    rows = []
    for (fid, g), rec in agg.items():
        area = ctx.field_area.get(fid)
        if not area:
            continue
        ratio = rec["ha"] / area
        if ratio >= thr:
            rows.append({"field_id": fid, "field": ctx.field(fid), "firm": ctx.firm(fid), "group": g, "area": _r2(area),
                         "ha": _r2(rec["ha"]), "ratio": round(ratio * 100, 1), "days": len(rec["days"]), "tasks": len(rec["tasks"])})
    rows.sort(key=lambda r: -r["ratio"])
    cols = [col("field", "Поле", drill="field"), col("firm", "Агрофирма"), col("group", "Операция"), col("area", "Площадь поля, га", "num"),
            col("ha", "Обработано, га", "num"), col("ratio", "К площади, %", "pct"), col("days", "Смен", "int"),
            col("tasks", "Заданий", "int")]
    no_area = not ctx.field_area
    return result(
        f"Повторные проходы (≥ {thr * 100:.0f}% площади)", cols, rows,
        summary=[stat("Случаев", len(rows), "warn" if rows else None), stat("Полей", len({r["field_id"] for r in rows})),
                 stat("Лишних га ≈", _r2(sum(r["ha"] - r["area"] for r in rows)))],
        note=("В справочнике полей нет площади, срез недоступен. " if no_area else "") +
             "Для посева, вспашки и уборки значение заметно выше 100% подозрительно (дубль задания или перекрытие). "
             "Для опрыскивания, подкормок и боронования повторы бывают плановыми. Порог меняется в фильтрах: 1,15 = 115%.")


# ============================================================================ АГРОФИРМЫ
def firm_compare(ctx: Ctx, con: sqlite3.Connection, p: P) -> dict:
    per: dict[str, dict] = defaultdict(lambda: {"ha": 0.0, "fields": set(), "mdays": set(), "drivers": set(), "machines": set()})
    for r in load_facts(con, p, wts=_wts(p), driver=True):
        rec = per[ctx.firm(r["field_id"])]
        rec["ha"] += r["ha"]
        rec["fields"].add(r["field_id"])
        rec["mdays"].add((r["machine_id"], r["day"]))
        rec["machines"].add(r["machine_id"])
        if r["driver"]:
            rec["drivers"].add(r["driver"])
    total = sum(v["ha"] for v in per.values())
    rows = [{"firm": f, "ha": _r2(v["ha"]), "share": _pct(v["ha"], total), "fields": len(v["fields"]),
             "machines": len(v["machines"]), "drivers": len(v["drivers"]), "shifts": len(v["mdays"]),
             "per_shift": _r2(v["ha"] / len(v["mdays"])) if v["mdays"] else 0}
            for f, v in per.items()]
    rows.sort(key=lambda r: -r["ha"])
    cols = [col("firm", "Агрофирма"), col("ha", "Га", "num"), col("share", "Доля холдинга", "progress"),
            col("fields", "Полей", "int"), col("machines", "Машин", "int"), col("drivers", "Исполнителей", "int"),
            col("shifts", "Машино-смен", "int"), col("per_shift", "Га за машино-смену", "num")]
    return result(f"Сравнение агрофирм{': ' + p.group if p.group else ''}", cols, rows,
                  summary=[stat("Агрофирм", len(rows)), stat("Всего, га", _r2(total)),
                           stat("Лидер", rows[0]["firm"] if rows else "—")],
                  chart={"type": "bar", "x": "firm", "series": [{"key": "ha", "label": "Га"}], "horizontal": True},
                  note="Выберите одну группу работ, чтобы сравнение было честным. Агрофирма поля определяется так же, как в сводках.")


def holding_totals(ctx: Ctx, con: sqlite3.Connection, p: P) -> dict:
    tree: dict[str, dict[str, dict[str, dict]]] = {}
    for r in load_facts(con, p, wts=_wts(p), crop=True):
        f, c, g = ctx.firm(r["field_id"]), r["crop"] or NO_CROP, ctx.group_label(r["wt"])
        node = tree.setdefault(f, {}).setdefault(c, {}).setdefault(g, {"ha": 0.0, "fields": set()})
        node["ha"] += r["ha"]
        node["fields"].add(r["field_id"])

    def total(d: Any) -> float:
        return d["ha"] if "ha" in d and isinstance(d["ha"], float) else sum(total(v) for v in d.values())

    def fields_of(d: Any) -> set:
        return d["fields"] if "fields" in d and isinstance(d.get("fields"), set) else set().union(*[fields_of(v) for v in d.values()]) if d else set()

    rows: list[dict] = []
    grand = sum(total(v) for v in tree.values())

    def emit(d: dict, depth: int, parent: str | None, path: str, parent_ha: float) -> None:
        for name, child in sorted(d.items(), key=lambda kv: -total(kv[1])):
            ha = total(child)
            nid = f"{path}/{name}"
            rows.append({"_id": nid, "_parent": parent, "_depth": depth, "name": name, "ha": _r2(ha),
                         "share": _pct(ha, parent_ha), "fields": len(fields_of(child) - {None})})
            if depth < 2:
                emit(child, depth + 1, nid, nid, ha)

    emit(tree, 0, None, "", grand)
    cols = [col("name", "Холдинг → фирма → культура → операция", "text"), col("ha", "Га", "num"),
            col("share", "Доля от родителя", "progress"), col("fields", "Полей", "int")]
    return result("Итоги по холдингу", cols, rows, tree=True,
                  summary=[stat("Всего, га", _r2(grand)), stat("Агрофирм", len(tree))],
                  note="Раскрывайте строки: фирма → культура → операция. Гектары разных операций суммируются как есть "
                       "(площадь, обработанная несколько раз, считается каждый раз).")


# ============================================================================ КАЧЕСТВО ДАННЫХ
def q_no_driver(ctx: Ctx, con: sqlite3.Connection, p: P) -> dict:
    agg: dict[tuple, dict] = {}
    total = 0.0
    for r in load_facts(con, p, wts=_wts(p), driver=True):
        total += r["ha"]
        if r["driver"]:
            continue
        rec = agg.setdefault((r["task_id"], r["day"]), {"day": r["day"], "firm": ctx.firm(r["field_id"]),
                             "machine_id": r["machine_id"], "machine": ctx.machine(r["machine_id"]), "group": ctx.group_label(r["wt"]), "ha": 0.0,
                             "task_id": r["task_id"], "task_url": _url(r["machine_id"], r["task_id"])})
        rec["ha"] += r["ha"]
    rows = sorted(agg.values(), key=lambda r: -r["ha"])
    for r in rows:
        r["ha"] = _r2(r["ha"])
    lost = sum(r["ha"] for r in rows)
    cols = [col("day", "Смена", "date"), col("firm", "Агрофирма"), col("machine", "Машина", drill="machine"), col("group", "Операция"),
            col("ha", "Га", "num"), col("task_id", "Задание", "link", href="task_url")]
    return result("Задания без исполнителя", cols, rows,
                  summary=[stat("Заданий", len({r["task_id"] for r in rows}), "warn" if rows else None),
                           stat("Гектаров", _r2(lost)), stat("Доля от всех га", f"{_pct(lost, total)}%")],
                  note="Эти гектары есть в сводках, но не попадают ни в рейтинг, ни в «Работу исполнителя»: в задании не указан человек.")


def q_no_crop_agro(ctx: Ctx, con: sqlite3.Connection, p: P) -> dict:
    try:
        no_agro = {r["task_id"] for r in _rows(
            con, "SELECT mt.task_id FROM mtaomi_tasks mt WHERE NOT EXISTS "
                 "(SELECT 1 FROM mtaomi a WHERE a.task_id=mt.task_id AND a.agro_operation_id IS NOT NULL)", [])}
        links_known = True
    except sqlite3.OperationalError:
        no_agro, links_known = set(), False
    agg: dict[tuple, dict] = {}
    total = 0.0
    for r in load_facts(con, p, wts=_wts(p), crop=True):
        total += r["ha"]
        problems = []
        if not r["crop"]:
            problems.append("Нет культуры")
        if r["task_id"] in no_agro:
            problems.append("Нет агрооперации")
        for pr in problems:
            rec = agg.setdefault((pr, r["task_id"], r["field_id"]), {
                "problem": pr, "day": r["day"], "firm": ctx.firm(r["field_id"]), "field": ctx.field(r["field_id"]),
                "machine_id": r["machine_id"], "field_id": r["field_id"], "machine": ctx.machine(r["machine_id"]), "group": ctx.group_label(r["wt"]), "ha": 0.0,
                "task_id": r["task_id"], "task_url": _url(r["machine_id"], r["task_id"])})
            rec["ha"] += r["ha"]
    rows = sorted(agg.values(), key=lambda r: -r["ha"])
    for r in rows:
        r["ha"] = _r2(r["ha"])
    nc = sum(r["ha"] for r in rows if r["problem"] == "Нет культуры")
    na = sum(r["ha"] for r in rows if r["problem"] == "Нет агрооперации")
    cols = [col("problem", "Проблема", "tag"), col("day", "Смена", "date"), col("firm", "Агрофирма"), col("field", "Поле", drill="field"),
            col("machine", "Машина", drill="machine"), col("group", "Операция"), col("ha", "Га", "num"),
            col("task_id", "Задание", "link", href="task_url")]
    return result("Задания без культуры или агрооперации", cols, rows,
                  summary=[stat("Га без культуры", _r2(nc), "warn" if nc else None, f"{_pct(nc, total)}% от всех"),
                           stat("Га без агрооперации", _r2(na), "warn" if na else None),
                           stat("Строк", len(rows))],
                  note="Нет культуры: у строки площади не нашлась запись истории поля. Нет агрооперации: связи задания "
                       "загружены, но ни одной агрооперации к нему не привязано." +
                       ("" if links_known else " Таблица связей в хранилище недоступна, второй признак не проверялся."))


def q_no_hourly(ctx: Ctx, con: sqlite3.Connection, p: P) -> dict:
    per: dict[Any, dict] = defaultdict(lambda: {"ha": 0.0, "nh": 0.0, "tasks": set()})
    for r in load_facts(con, p, wts=_wts(p)):
        rec = per[r["machine_id"]]
        rec["ha"] += r["ha"]
        if r["nh"]:
            rec["nh"] += r["ha"]
            rec["tasks"].add(r["task_id"])
    rows = [{"machine_id": m, "machine": ctx.machine(m), "ha": _r2(v["ha"]), "nh": _r2(v["nh"]),
             "pct": _pct(v["nh"], v["ha"]), "tasks": len(v["tasks"])} for m, v in per.items() if v["nh"] > 0]
    rows.sort(key=lambda r: -r["nh"])
    th, tn = sum(v["ha"] for v in per.values()), sum(v["nh"] for v in per.values())
    cols = [col("machine", "Машина"), col("ha", "Всего га", "num"), col("nh", "Без почасовой разбивки, га", "num"),
            col("pct", "Доля, %", "progress"), col("tasks", "Заданий", "int")]
    return result("Га без почасовой разбивки", cols, rows,
                  summary=[stat("Доля по всем машинам", f"{_pct(tn, th)}%", "warn" if _pct(tn, th) > 5 else None),
                           stat("Таких гектаров", _r2(tn)), stat("Машин с проблемой", len(rows))],
                  note="Для этих гектаров нет почасовой телеметрии, поэтому площадь относится ко дню окончания задания, "
                       "а не к реальным суткам работы. Это главный источник расхождений по дням.")


def q_suspicious(ctx: Ctx, con: sqlite3.Connection, p: P) -> dict:
    mult = p.threshold or 3.0
    facts = load_facts(con, p, wts=_wts(p))
    day_tot: dict[tuple, dict] = {}
    for r in facts:
        g = ctx.group_label(r["wt"])
        rec = day_tot.setdefault((r["machine_id"], r["day"], g), {"ha": 0.0, "first": r["task_id"]})
        rec["ha"] += r["ha"]
    by_group: dict[str, list[float]] = defaultdict(list)
    for (m, d, g), rec in day_tot.items():
        by_group[g].append(rec["ha"])
    med = {g: _med(v) for g, v in by_group.items()}
    p90 = {g: sorted(v)[int(0.9 * (len(v) - 1))] for g, v in by_group.items() if v}
    rows = []
    for (m, d, g), rec in day_tot.items():
        base = med.get(g) or 0
        # выше множителя к медиане И заметно выше обычного верхнего края группы (90-й процентиль)
        if base and len(by_group[g]) >= 5 and rec["ha"] >= 5 and rec["ha"] > mult * base and rec["ha"] > 1.3 * p90[g]:
            rows.append({"kind": "Нереальная выработка", "day": d, "machine_id": m, "field_id": None, "machine": ctx.machine(m), "field": "", "group": g,
                         "ha": _r2(rec["ha"]), "detail": f"в {rec['ha'] / base:.1f} раза выше медианы группы ({base:.1f} га)",
                         "task_id": rec["first"], "task_url": _url(m, rec["first"])})
    dup: dict[tuple, dict] = {}
    for r in facts:
        rec = dup.setdefault((r["machine_id"], r["field_id"], r["day"], round(r["ha"], 1)), {"tasks": set(), "ha": r["ha"], "g": ctx.group_label(r["wt"])})
        rec["tasks"].add(r["task_id"])
    for (m, f, d, _), rec in dup.items():
        if len(rec["tasks"]) >= 2 and rec["ha"] >= 1:
            ids = sorted(rec["tasks"])
            rows.append({"kind": "Возможный дубль", "day": d, "machine_id": m, "field_id": f, "machine": ctx.machine(m), "field": ctx.field(f), "group": rec["g"],
                         "ha": _r2(rec["ha"]), "detail": f"{len(ids)} задания с одинаковой площадью: " + ", ".join(map(str, ids[:4])),
                         "task_id": ids[0], "task_url": _url(m, ids[0])})
    rows.sort(key=lambda r: -r["ha"])
    cols = [col("kind", "Признак", "tag"), col("day", "Смена", "date"), col("machine", "Машина", drill="machine"), col("field", "Поле", drill="field"),
            col("group", "Операция"), col("ha", "Га", "num"), col("detail", "Что не так"),
            col("task_id", "Задание", "link", href="task_url")]
    return result("Подозрительные задания", cols, rows,
                  summary=[stat("Нереальная выработка", sum(1 for r in rows if r["kind"] == "Нереальная выработка"), "warn"),
                           stat("Возможные дубли", sum(1 for r in rows if r["kind"] == "Возможный дубль"), "warn")],
                  note=f"Выработка: смена машины выше {mult:g}× медианы своей группы и выше 1,3× её 90-го процентиля. Дубль: на одной машине, "
                       "поле и смене два и более задания с одной площадью. Множитель меняется в фильтрах.")


# ----------------------------------------------------------------------------- справочники для выбора
def options(kind: str, date_from: str, date_to: str, d: Path | None = None) -> list[dict]:
    ctx = Ctx(d)
    if kind == "machines":
        return [{"id": i, "label": n, "hint": ""} for i, n in sorted(ctx.machines.items(), key=lambda kv: kv[1])]
    if kind == "fields":
        return [{"id": i, "label": n, "hint": ctx.firm(i) if ctx.firm_of_field else ""}
                for i, n in sorted(ctx.field_names.items(), key=lambda kv: kv[1])]
    if kind == "drivers":
        p = P(date_from, date_to)
        with sa.store_conn(ctx.d) as con:
            agg: dict[int, float] = defaultdict(float)
            for r in load_facts(con, p, driver=True):
                if r["driver"]:
                    agg[r["driver"]] += r["ha"]
        return [{"id": i, "label": f"id {i}", "hint": f"{ha:.1f} га"} for i, ha in sorted(agg.items(), key=lambda kv: -kv[1])]
    raise ValueError(f"неизвестный справочник: {kind}")


QUERIES: dict[str, Callable[[Ctx, sqlite3.Connection, P], dict]] = {
    "people_rating": people_rating, "people_low": people_low, "people_load": people_load,
    "machine_work": machine_work, "machine_idle": machine_idle, "fuel": fuel, "norm_vs_fact": norm_vs_fact,
    "fleet_load": fleet_load, "speed": speed,
    "field_history": field_history, "plan_progress": plan_progress, "fields_idle": fields_idle,
    "repeat_passes": repeat_passes,
    "firm_compare": firm_compare, "holding_totals": holding_totals,
    "q_no_driver": q_no_driver, "q_no_crop_agro": q_no_crop_agro, "q_no_hourly": q_no_hourly,
    "q_suspicious": q_suspicious,
}


def run(query_id: str, *, date_from: str, date_to: str, group: str | None = None, driver_id: int | None = None,
        machine_id: int | None = None, field_id: int | None = None, threshold: float | None = None,
        firm: str | None = None, plan: dict | None = None, d: Path | None = None) -> dict[str, Any]:
    fn = QUERIES.get(query_id)
    if fn is None:
        raise ValueError(f"неизвестный срез: {query_id}")
    sa._check_dates(date_from, date_to)
    if (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days > MAX_DAYS:
        raise ValueError(f"Период не длиннее {MAX_DAYS} дней")
    if date.fromisoformat(date_to) <= date.fromisoformat(date_from):
        raise ValueError("Дата «по» раньше даты «с»")
    ctx = Ctx(d)
    p = P(date_from, date_to, group or None, driver_id, machine_id, field_id, threshold, firm or None, plan or {})
    with sa.store_conn(ctx.d) as con:
        out = fn(ctx, con, p)
    out.update({"id": query_id, "date_from": date_from, "date_to": date_to, "available": True})
    return out
