# -*- coding: utf-8 -*-
"""Топливо за период из локального хранилища заданий + поиск аномалий расхода.

Читает store.sqlite (задания, площади) и refs.sqlite (поля, группы, папки, машины).
ФИО и названия видов работ в этих файлах нет: подставляет вызывающий код по id.

Что считается (одна строка = одно задание, сменный день по end_ts, как в «Работе исполнителя»):
  * fuel_total  = fuel_consumption                      — весь расход, л
  * fuel_road   = fuel_consumption_on_road              — расход на дороге, л
  * fuel_field  = fuel_total − fuel_road                — расход в поле, л (как в сводке 28)
  * л/га        = fuel_field / covered_area
  * л/км        = fuel_field / work_distance           (если у машины есть датчик пути)
  * дорога, л/ч = fuel_road / movements_on_road_duration

Датчики есть не у всей техники. Нет топлива (или 0) — задание считается «без данных», в расчёт
л/га и аномалий не входит, но попадает в покрытие (coverage): по нему видно, какая техника
вообще не отдаёт топливо и у какой датчик пропадает периодами.

Аномалии (поиск перерасхода / возможного слива) — только сравнение с аналогами, без LLM:
  over_field        л/га задания выше нормы группы (вид работ) — медиана + робастный порог (MAD)
  over_distance     л/км задания выше нормы группы
  over_road         дорожный расход на час в пути выше нормы
  idle_burn         расход топлива, а выработки (га и км) нет — только для видов работ,
                    где обычно есть площадь
  road_no_motion    расход на дороге при нулевом времени в пути
  machine_systemic  у машины в виде работ средний л/га стабильно выше парка за период
  sensor_gaps       датчик топлива работает не всегда: у части заданий с площадью топлива нет

Пороги — THRESHOLDS; их можно переопределить параметром thresholds.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import driver_work as dw

THRESHOLDS: dict[str, float] = {
    "min_ha": 2.0,              # короче — л/га шумит, в сравнение не берём
    "min_peers": 8,             # аналогов в группе, чтобы считать норму
    "k_mad": 3.5,               # множитель робастного разброса
    "rel_field": 0.35,          # минимум превышения нормы по л/га (доля от медианы)
    "rel_road": 0.60,           # то же для л/ч на дороге
    "min_excess_l": 10.0,       # меньше литров лишнего — не показываем
    "critical_ratio": 2.0,      # во сколько раз выше нормы — critical
    "critical_excess_l": 100.0,  # литров лишнего — critical
    "min_road_h": 0.25,         # час в пути минимум для л/ч на дороге
    "min_km": 1.0,              # минимум пути для л/км
    "idle_min_l": 15.0,         # расход без выработки от, л
    "idle_critical_l": 50.0,
    "road_no_motion_l": 10.0,
    "area_wt_share": 0.7,       # вид работ «площадной», если у стольких заданий есть площадь
    "machine_min_ha": 20.0,     # машина-вид работ: минимум га для сравнения
    "machine_min_peers": 5,
    "machine_rel": 0.20,
    "gaps_min_tasks": 5,        # заданий с площадью у машины
    "gaps_share": 0.30,         # доля заданий без топлива, при которой есть «пропуски»
}

_MAX_TASK_ROWS = 3000
_MAX_ANOMALIES = 500
_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


# ----------------------------------------------------------------------------- числа
def _num(value: Any) -> float | None:
    if value is None or value is False or value == "" or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # NaN


def _median(values: list[float]) -> float:
    xs = sorted(values)
    n = len(xs)
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2


def _norm(values: list[float]) -> tuple[float, float]:
    """(медиана, MAD) — устойчиво к самим выбросам."""
    med = _median(values)
    return med, _median([abs(v - med) for v in values])


def _limit(med: float, mad: float, k: float, rel: float) -> float:
    return med + max(k * 1.4826 * mad, rel * med)


def _r(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def _div(a: float | None, b: float | None, min_b: float = 0.0) -> float | None:
    if a is None or b is None or b <= min_b:
        return None
    return a / b


# ----------------------------------------------------------------------------- чтение
def _machine_labels(refs_path: Path) -> dict[int, str]:
    out: dict[int, str] = {}
    for row in dw._load_ref(refs_path, "machines"):
        mid = dw._int_or_none(row.get("id"))
        if mid is None:
            continue
        reg = str(row.get("registration_number") or row.get("reg_number") or "").strip()
        name = str(row.get("name") or "").strip()
        label = f"{name} · {reg}" if reg and name and reg not in name else (reg or name)
        if label:
            out[mid] = label
    return out


def _task_fields(con: sqlite3.Connection, ts_from: float, ts_to: float) -> dict[int, tuple[int, float | None]]:
    """task_id → (главное поле по площади, сумма covered_area строк)."""
    best: dict[int, tuple[float, int | None]] = {}
    total: dict[int, float] = defaultdict(float)
    if not (dw._has_table(con, "mtfmi_fact") and dw._has_table(con, "tasks")):
        return {}
    for row in con.execute(
        "SELECT f.task_id, f.field_id, f.covered_area FROM mtfmi_fact f "
        "JOIN tasks t ON t.id=f.task_id WHERE t.end_ts>=? AND t.end_ts<?",
        (ts_from, ts_to),
    ):
        tid = int(row["task_id"])
        ha = float(row["covered_area"] or 0)
        total[tid] += ha
        cur = best.get(tid)
        if cur is None or ha > cur[0]:
            best[tid] = (ha, dw._int_or_none(row["field_id"]))
    return {tid: (best[tid][1], round(total[tid], 4)) for tid in best}


def _scan_tasks(con: sqlite3.Connection, ts_from: float, ts_to: float,
                firms: dict[int, str], fields_by_task: dict[int, tuple[int | None, float | None]],
                machines: dict[int, str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in con.execute(
        "SELECT id, machine_id, wt, end_ts, data FROM tasks WHERE end_ts>=? AND end_ts<?",
        (ts_from, ts_to),
    ):
        data = dw._loads(row["data"])
        tid = int(row["id"])
        mid = dw._int_or_none(row["machine_id"])
        field_id, fact_ha = fields_by_task.get(tid, (None, None))
        ha = _num(data.get("covered_area"))
        if ha is None:
            ha = fact_ha
        total = _num(data.get("fuel_consumption"))
        road_raw = _num(data.get("fuel_consumption_on_road"))
        has_fuel = total is not None and total > 0
        fuel_total = total if has_fuel else None
        fuel_road = None
        fuel_field = None
        if has_fuel:
            if road_raw is not None and road_raw >= 0:
                fuel_road = min(road_raw, total)
                fuel_field = total - fuel_road
            else:
                fuel_field = total  # граница поле/дорога неизвестна — считаем всё полевым
        dist_m = _num(data.get("work_distance"))
        dur_s = _num(data.get("work_duration"))
        road_s = _num(data.get("movements_on_road_duration"))
        rows.append({
            "task_id": tid,
            "machine_id": mid,
            "machine": machines.get(mid or 0, f"id {mid}" if mid else ""),
            "driver_id": dw._driver_id(data),
            "wt": dw._int_or_none(row["wt"]),
            "date": dw._shift_date(float(row["end_ts"])),
            "field_id": field_id,
            "firm": firms.get(field_id or 0, ""),
            "ha": ha if ha is not None else 0.0,
            "fuel_total": fuel_total,
            "fuel_field": fuel_field,
            "fuel_road": fuel_road,
            "road_known": road_raw is not None,
            "dist_km": None if dist_m is None else dist_m / 1000.0,
            "work_h": None if dur_s is None else dur_s / 3600.0,
            "road_h": None if road_s is None else road_s / 3600.0,
            "task_url": dw._TASK_URL.format(machine_id=mid, task_id=tid) if mid else "",
        })
    for r in rows:
        r["l_ha"] = _div(r["fuel_field"], r["ha"], 0.0)
        r["l_km"] = _div(r["fuel_field"], r["dist_km"], 0.0)
        r["road_l_h"] = _div(r["fuel_road"], r["road_h"], 0.0)
    return rows


# ----------------------------------------------------------------------------- сводки
def _group_stats(items: list[dict[str, Any]]) -> dict[str, Any]:
    ha = sum(t["ha"] for t in items)
    fueled = [t for t in items if t["fuel_total"] is not None]
    with_area = [t for t in fueled if t["fuel_field"] is not None and t["ha"] > 0]
    km = [t for t in with_area if (t["dist_km"] or 0) > 0]
    road = [t for t in fueled if t["fuel_road"] is not None]
    road_h = sum((t["road_h"] or 0) for t in road if (t["road_h"] or 0) > 0)
    road_fuel_timed = sum(t["fuel_road"] for t in road if (t["road_h"] or 0) > 0)
    f_field = sum(t["fuel_field"] for t in fueled if t["fuel_field"] is not None)
    f_road = sum(t["fuel_road"] for t in road)
    return {
        "tasks": len(items),
        "ha": round(ha, 2),
        "fuel_tasks": len(fueled),
        "fuel_share": round(len(fueled) / len(items), 3) if items else 0.0,
        "fuel_total": round(sum(t["fuel_total"] for t in fueled), 1),
        "fuel_field": round(f_field, 1),
        "fuel_road": round(f_road, 1),
        "road_share": _r(f_road / (f_field + f_road), 3) if (f_field + f_road) > 0 else None,
        "l_ha": _r(_div(sum(t["fuel_field"] for t in with_area), sum(t["ha"] for t in with_area), 0.0)),
        "l_km": _r(_div(sum(t["fuel_field"] for t in km), sum(t["dist_km"] for t in km), 0.0)),
        "road_h": round(road_h, 1),
        "road_l_h": _r(_div(road_fuel_timed, road_h, 0.0)),
    }


def _breakdown(rows: list[dict[str, Any]], key: str, extra: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    buckets: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for t in rows:
        buckets[t[key]].append(t)
    out = []
    for k, items in buckets.items():
        row = {key: k, **_group_stats(items)}
        for e in extra:
            row[e] = next((t[e] for t in items if t.get(e)), "")
        out.append(row)
    out.sort(key=lambda r: (-r["fuel_total"], -r["ha"]))
    return out


def _coverage(rows: list[dict[str, Any]], th: dict[str, float]) -> dict[str, Any]:
    by_machine: dict[int | None, list[dict[str, Any]]] = defaultdict(list)
    for t in rows:
        by_machine[t["machine_id"]].append(t)
    no_fuel, partial = [], []
    for mid, items in by_machine.items():
        with_fuel = sum(1 for t in items if t["fuel_total"] is not None)
        area_tasks = [t for t in items if t["ha"] >= 1.0]
        base = {"machine_id": mid, "machine": items[0]["machine"], "tasks": len(items),
                "ha": round(sum(t["ha"] for t in items), 1)}
        if with_fuel == 0:
            no_fuel.append(base)
        elif len(area_tasks) >= th["gaps_min_tasks"]:
            miss = [t for t in area_tasks if t["fuel_total"] is None]
            share = len(miss) / len(area_tasks)
            if share >= th["gaps_share"]:
                partial.append({**base, "area_tasks": len(area_tasks), "missing": len(miss),
                                "missing_share": round(share, 3),
                                "missing_ha": round(sum(t["ha"] for t in miss), 1)})
    no_fuel.sort(key=lambda r: -r["ha"])
    partial.sort(key=lambda r: -r["missing_ha"])
    n = len(rows)
    return {
        "tasks": n,
        "with_fuel": sum(1 for t in rows if t["fuel_total"] is not None),
        "with_road_split": sum(1 for t in rows if t["fuel_total"] is not None and t["road_known"]),
        "with_distance": sum(1 for t in rows if (t["dist_km"] or 0) > 0),
        "with_driver": sum(1 for t in rows if t["driver_id"]),
        "machines": len(by_machine),
        "machines_no_fuel": no_fuel[:100],
        "machines_no_fuel_count": len(no_fuel),
        "machines_partial": partial[:100],
        "machines_partial_count": len(partial),
    }


# ----------------------------------------------------------------------------- аномалии
def _severity(ratio: float, excess_l: float, th: dict[str, float]) -> str:
    if ratio >= th["critical_ratio"] or excess_l >= th["critical_excess_l"]:
        return "critical"
    return "warning"


def _anomaly(kind: str, severity: str, t: dict[str, Any], text: str, **values: Any) -> dict[str, Any]:
    return {
        "kind": kind, "severity": severity, "text": text,
        "task_id": t["task_id"], "task_url": t["task_url"], "date": t["date"],
        "machine_id": t["machine_id"], "machine": t["machine"], "driver_id": t["driver_id"],
        "firm": t["firm"], "wt": t["wt"], "ha": _r(t["ha"]),
        **{k: _r(v) if isinstance(v, float) else v for k, v in values.items()},
    }


def _area_work_types(rows: list[dict[str, Any]], th: dict[str, float]) -> set[int | None]:
    by_wt: dict[int | None, list[dict[str, Any]]] = defaultdict(list)
    for t in rows:
        by_wt[t["wt"]].append(t)
    return {wt for wt, items in by_wt.items()
            if len(items) >= 5 and sum(1 for t in items if t["ha"] > 0.5) / len(items) >= th["area_wt_share"]}


def find_anomalies(rows: list[dict[str, Any]], th: dict[str, float] | None = None) -> list[dict[str, Any]]:
    th = {**THRESHOLDS, **(th or {})}
    found: list[dict[str, Any]] = []

    by_wt: dict[int | None, list[dict[str, Any]]] = defaultdict(list)
    for t in rows:
        by_wt[t["wt"]].append(t)

    for wt, items in by_wt.items():
        # --- л/га по заданиям
        peers = [t for t in items if t["l_ha"] is not None and t["ha"] >= th["min_ha"]]
        if len(peers) >= th["min_peers"]:
            med, mad = _norm([t["l_ha"] for t in peers])
            lim = _limit(med, mad, th["k_mad"], th["rel_field"])
            for t in peers:
                if t["l_ha"] > lim:
                    excess = (t["l_ha"] - med) * t["ha"]
                    if excess < th["min_excess_l"]:
                        continue
                    found.append(_anomaly(
                        "over_field", _severity(t["l_ha"] / med if med else 99, excess, th), t,
                        f"В поле {t['l_ha']:.1f} л/га при норме {med:.1f} (лишних ≈ {excess:.0f} л).",
                        l_ha=t["l_ha"], norm=med, excess_l=excess, fuel_field=t["fuel_field"],
                    ))
        # --- л/км
        km_peers = [t for t in items if t["l_km"] is not None and (t["dist_km"] or 0) >= th["min_km"]]
        if len(km_peers) >= th["min_peers"]:
            med, mad = _norm([t["l_km"] for t in km_peers])
            lim = _limit(med, mad, th["k_mad"], th["rel_field"])
            for t in km_peers:
                if t["l_km"] > lim:
                    excess = (t["l_km"] - med) * t["dist_km"]
                    if excess < th["min_excess_l"]:
                        continue
                    found.append(_anomaly(
                        "over_distance", _severity(t["l_km"] / med if med else 99, excess, th), t,
                        f"В поле {t['l_km']:.2f} л/км при норме {med:.2f} (лишних ≈ {excess:.0f} л).",
                        l_km=t["l_km"], norm=med, excess_l=excess, fuel_field=t["fuel_field"],
                    ))
        # --- дорога, л/ч
        road_peers = [t for t in items if t["road_l_h"] is not None and (t["road_h"] or 0) >= th["min_road_h"]]
        if len(road_peers) >= th["min_peers"]:
            med, mad = _norm([t["road_l_h"] for t in road_peers])
            lim = _limit(med, mad, th["k_mad"], th["rel_road"])
            for t in road_peers:
                if t["road_l_h"] > lim:
                    excess = (t["road_l_h"] - med) * t["road_h"]
                    if excess < th["min_excess_l"]:
                        continue
                    found.append(_anomaly(
                        "over_road", _severity(t["road_l_h"] / med if med else 99, excess, th), t,
                        f"На дороге {t['road_l_h']:.1f} л/ч при норме {med:.1f} (лишних ≈ {excess:.0f} л).",
                        road_l_h=t["road_l_h"], norm=med, excess_l=excess, fuel_road=t["fuel_road"],
                    ))

    # --- расход без выработки и расход на дороге без движения
    area_wts = _area_work_types(rows, th)
    for t in rows:
        if t["fuel_field"] is not None and t["wt"] in area_wts and t["ha"] < 0.1 \
                and (t["dist_km"] is None or t["dist_km"] < 0.1) and t["fuel_field"] >= th["idle_min_l"]:
            found.append(_anomaly(
                "idle_burn", "critical" if t["fuel_field"] >= th["idle_critical_l"] else "warning", t,
                f"Израсходовано {t['fuel_field']:.0f} л, а площади и пути нет.",
                excess_l=t["fuel_field"], fuel_field=t["fuel_field"],
            ))
        if t["fuel_road"] is not None and t["road_h"] is not None and t["road_h"] < 0.02 \
                and t["fuel_road"] >= th["road_no_motion_l"]:
            found.append(_anomaly(
                "road_no_motion", "warning", t,
                f"На дороге {t['fuel_road']:.0f} л, а времени в пути почти нет.",
                excess_l=t["fuel_road"], fuel_road=t["fuel_road"],
            ))

    # --- машина × вид работ: систематически выше парка
    cells: dict[tuple[int | None, int | None], list[dict[str, Any]]] = defaultdict(list)
    for t in rows:
        if t["l_ha"] is not None and t["machine_id"] is not None and t["ha"] >= th["min_ha"]:
            cells[(t["machine_id"], t["wt"])].append(t)
    by_wt_cells: dict[int | None, list[tuple[int | None, float, float, list[dict[str, Any]]]]] = defaultdict(list)
    for (mid, wt), items in cells.items():
        ha = sum(t["ha"] for t in items)
        if ha >= th["machine_min_ha"]:
            by_wt_cells[wt].append((mid, sum(t["fuel_field"] for t in items) / ha, ha, items))
    for wt, cell_list in by_wt_cells.items():
        if len(cell_list) < th["machine_min_peers"]:
            continue
        med, mad = _norm([c[1] for c in cell_list])
        lim = _limit(med, mad, th["k_mad"], th["machine_rel"])
        for mid, l_ha, ha, items in cell_list:
            if l_ha <= lim:
                continue
            excess = (l_ha - med) * ha
            if excess < th["min_excess_l"]:
                continue
            first = max(items, key=lambda t: t["ha"])
            found.append(_anomaly(
                "machine_systemic", _severity(l_ha / med if med else 99, excess, th), first,
                f"За период {l_ha:.1f} л/га при норме парка {med:.1f}: {len(items)} заданий, "
                f"{ha:.0f} га, лишних ≈ {excess:.0f} л.",
                l_ha=l_ha, norm=med, excess_l=excess, tasks=len(items), ha_total=ha,
            ))
    return _finish(found)


def _finish(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found.sort(key=lambda a: (_SEVERITY_ORDER.get(a["severity"], 9), -(a.get("excess_l") or 0)))
    return found


# ----------------------------------------------------------------------------- публичный вход
def _empty(date_from: date, date_to: date, detail: str) -> dict[str, Any]:
    return {
        "available": False, "detail": detail,
        "date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
        "coverage": {}, "totals": {}, "by_machine": [], "by_driver": [], "by_operation": [],
        "by_firm": [], "by_day": [], "anomalies": [], "anomaly_counts": {}, "tasks": [],
        "tasks_truncated": False, "thresholds": dict(THRESHOLDS),
    }


def query_fuel(store_dir: Path, date_from: date, date_to: date, *,
               thresholds: dict[str, float] | None = None) -> dict[str, Any]:
    d_from, d_to, ts_from, ts_to = dw._window(date_from, date_to)
    store = Path(store_dir) / "store.sqlite"
    if not store.is_file():
        return _empty(date_from, date_to, "Хранилище заданий не найдено. Сначала синхронизируйте сводки.")
    refs = Path(store_dir) / "refs.sqlite"
    try:
        con = dw._connect(store)
    except sqlite3.Error as exc:
        return _empty(date_from, date_to, f"Не удалось открыть хранилище: {exc}")
    th = {**THRESHOLDS, **(thresholds or {})}
    try:
        if not dw._has_table(con, "tasks"):
            return _empty(date_from, date_to, "В хранилище нет таблицы заданий.")
        firms = dw.field_firms(
            dw._load_ref(refs, "fields"), dw._load_ref(refs, "field_groups"), dw._load_ref(refs, "group_folders"),
        )
        machines = _machine_labels(refs)
        rows = _scan_tasks(con, ts_from, ts_to, firms, _task_fields(con, ts_from, ts_to), machines)
    except sqlite3.Error as exc:
        return _empty(date_from, date_to, f"Хранилище заданий без нужных таблиц: {exc}")
    finally:
        con.close()

    anomalies = find_anomalies(rows, th)
    counts: dict[str, int] = defaultdict(int)
    for a in anomalies:
        counts[a["kind"]] += 1
    coverage = _coverage(rows, th)
    counts["sensor_gaps"] = coverage["machines_partial_count"]

    listed = sorted((t for t in rows if t["fuel_total"] is not None),
                    key=lambda t: -t["fuel_total"])
    truncated = len(listed) > _MAX_TASK_ROWS
    return {
        "available": True, "detail": "",
        "date_from": d_from, "date_to": d_to,
        "coverage": coverage,
        "totals": _group_stats(rows),
        "by_machine": _breakdown(rows, "machine_id", ("machine",)),
        "by_driver": _breakdown(rows, "driver_id"),
        "by_operation": _breakdown(rows, "wt"),
        "by_firm": _breakdown(rows, "firm"),
        "by_day": sorted(_breakdown(rows, "date"), key=lambda r: r["date"]),
        "anomalies": anomalies[:_MAX_ANOMALIES],
        "anomalies_total": len(anomalies),
        "anomaly_counts": dict(counts),
        "tasks": [_public_task(t) for t in listed[:_MAX_TASK_ROWS]],
        "tasks_truncated": truncated,
        "thresholds": th,
    }


def _public_task(t: dict[str, Any]) -> dict[str, Any]:
    keys = ("task_id", "task_url", "date", "machine_id", "machine", "driver_id", "wt", "firm",
            "ha", "fuel_total", "fuel_field", "fuel_road", "l_ha", "l_km", "road_l_h", "road_h", "dist_km")
    return {k: (_r(t[k], 3) if isinstance(t[k], float) else t[k]) for k in keys}


def apply_names(payload: dict[str, Any], drivers: dict[int, str], work_types: dict[int, str]) -> None:
    """Подставить ФИО и названия видов работ по id (после запроса к Cropwise)."""
    def fio(did: Any) -> str:
        return drivers.get(int(did), "") if did else ""

    def op(wt: Any) -> str:
        return work_types.get(int(wt), "") if wt else ""

    for r in payload.get("by_driver") or []:
        r["driver"] = fio(r.get("driver_id")) or (f"id {r['driver_id']}" if r.get("driver_id") else "Без исполнителя")
    for r in payload.get("by_operation") or []:
        r["operation"] = op(r.get("wt")) or (f"вид работ {r['wt']}" if r.get("wt") else "Без вида работ")
    for key in ("anomalies", "tasks"):
        for r in payload.get(key) or []:
            r["driver"] = fio(r.get("driver_id"))
            r["operation"] = op(r.get("wt"))
    payload["names_loaded"] = bool(drivers)
