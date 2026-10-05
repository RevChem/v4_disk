"""
Снимок данных по работам для сводки 23 из уже посчитанной сводки 2 (2_main_ideal_compact).

Формат JSON: work_data / work_data_prev в структуре ranis (area_by_firm, machines_*).
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

BUNDLE_OUT_ENV = "SVODKI_R23_WORK_DATA_BUNDLE_OUT"
BUNDLE_IN_ENV = "SVODKI_R23_WORK_DATA_BUNDLE_IN"

SELF_PROPELLED_SUBTYPES = {
    "spreader",
    "self_propelled_spreader",
    "self_propelled_sprayer",
    "sprayer",
    "combine_harvester",
    "forage_harvester",
    "self_propelled_mower",
    "self_propelled_baler",
}
TRACTOR_SUBTYPES = {
    "tractor",
    "powerful_tractor",
    "energy_saturated_tractor",
}


def r23_reporting_day_from_stamp(stamp: date) -> date:
    """
    Сутки в отчёте 23 = как последняя колонка сводки 2.
    У сводки 2: TODAY = stamp (REPORT_DATE), данные в колонках до TODAY − 1 включительно.
    """
    return stamp - timedelta(days=1)


def _last_activity_day_in_report2(report2_work_data: dict, stamp: date) -> date | None:
    """Последний день с данными в work_data сводки 2 (как compute_display_dates[-1])."""
    active: set[date] = set()
    for gd in report2_work_data.values():
        for firm_map in (gd.get("area_by_firm_date") or {}).values():
            for dt, v in firm_map.items():
                kd = _coerce_date_key(dt)
                if kd and float(v or 0) > 0:
                    active.add(kd)
        for firm_crops in (gd.get("crops") or {}).values():
            for crop_rec in firm_crops.values():
                for dt, v in (crop_rec.get("by_date") or {}).items():
                    kd = _coerce_date_key(dt)
                    if kd and float(v or 0) > 0:
                        active.add(kd)
        for firm_m in (gd.get("machines") or {}).values():
            for dt, bag in firm_m.items():
                kd = _coerce_date_key(dt)
                if kd and ((bag or {}).get("own") or (bag or {}).get("hired")):
                    active.add(kd)
    upper = stamp - timedelta(days=1)
    cand = sorted(d for d in active if d <= upper)
    return cand[-1] if cand else None


def analytics_dates_from_report_stamp(
    stamp: date,
    report2_work_data: dict | None = None,
) -> tuple[date, date]:
    """
    (сутки в отчёте 23, день сравнения).
    stamp = REPORT_DATE / TODAY сводки 2 (= дата платформы).
    """
    if report2_work_data:
        report = _last_activity_day_in_report2(report2_work_data, stamp)
    else:
        report = None
    if report is None:
        report = r23_reporting_day_from_stamp(stamp)
    prev = report - timedelta(days=1)
    return report, prev


def _coerce_date_key(key: Any) -> date | None:
    if isinstance(key, date):
        return key
    if isinstance(key, str):
        raw = key.strip()[:10]
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None
    return None


def _lookup_day_value(by_date: dict, target_date: date) -> Any:
    """Значение по дате: ключи в dict могут быть date или ISO-строка (после JSON)."""
    if not by_date:
        return None
    hit = by_date.get(target_date)
    if hit is not None:
        return hit
    iso = target_date.isoformat()
    hit = by_date.get(iso)
    if hit is not None:
        return hit
    for key, val in by_date.items():
        kd = _coerce_date_key(key)
        if kd == target_date:
            return val
    return None


def _lookup_day_mapping(by_firm_date: dict, firm: str, target_date: date) -> dict:
    firm_map = (by_firm_date.get(firm) or {})
    raw = _lookup_day_value(firm_map, target_date)
    return raw if isinstance(raw, dict) else {}


def _classify_machine_sets(
    mids: set[int],
    subtype_by_id: dict[int, str],
) -> tuple[set[int], set[int], set[int]]:
    sp: set[int] = set()
    tr: set[int] = set()
    no_sub: set[int] = set()
    for mid in mids:
        m_sub = subtype_by_id.get(mid, "—")
        if m_sub in SELF_PROPELLED_SUBTYPES:
            sp.add(mid)
        elif m_sub in TRACTOR_SUBTYPES:
            tr.add(mid)
        else:
            no_sub.add(mid)
    return sp, tr, no_sub


def build_r23_group_for_day(
    gd2: dict,
    target_date: date,
    firm_order: list[str],
    subtype_by_id: dict[int, str],
) -> dict[str, Any] | None:
    """Одна группа работ: суточные показатели из сводки 2 за target_date."""
    area_by_firm_date = gd2.get("area_by_firm_date") or {}
    crops_block = gd2.get("crops") or {}
    machines_block = gd2.get("machines") or {}

    area_by_firm: dict[str, float] = defaultdict(float)
    area_by_crop: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    machines_own: dict[str, set[int]] = defaultdict(set)
    machines_hired: dict[str, set[int]] = defaultdict(set)
    machines_sp: dict[str, set[int]] = defaultdict(set)
    machines_tr: dict[str, set[int]] = defaultdict(set)
    machines_no_sub: dict[str, set[int]] = defaultdict(set)

    has_any = False
    for firm in firm_order:
        ha = float(_lookup_day_value(area_by_firm_date.get(firm) or {}, target_date) or 0)
        if ha > 0:
            area_by_firm[firm] = round(ha)
            has_any = True
        for crop, crop_rec in (crops_block.get(firm) or {}).items():
            raw = _lookup_day_value(crop_rec.get("by_date") or {}, target_date)
            ch = float(raw or 0)
            if ch > 0:
                area_by_crop[firm][crop] = round(ch)
                has_any = True
        mday = _lookup_day_mapping(machines_block, firm, target_date)
        own_m = set(mday.get("own") or [])
        hired_m = set(mday.get("hired") or [])
        if own_m or hired_m:
            has_any = True
        machines_own[firm] = own_m
        machines_hired[firm] = hired_m
        sp, tr, ns = _classify_machine_sets(own_m | hired_m, subtype_by_id)
        machines_sp[firm] = sp
        machines_tr[firm] = tr
        machines_no_sub[firm] = ns

    if not has_any:
        return None

    return {
        "area_by_firm": area_by_firm,
        "area_by_crop": area_by_crop,
        "machines_own": machines_own,
        "machines_hired": machines_hired,
        "machines_sp": machines_sp,
        "machines_tr": machines_tr,
        "machines_no_sub": machines_no_sub,
    }


def build_r23_work_data_from_report2(
    report2_work_data: dict,
    target_date: date,
    firm_order: list[str],
    subtype_by_id: dict[int, str],
) -> dict:
    out: dict[str, dict] = {}
    for group_name, gd2 in report2_work_data.items():
        gd = build_r23_group_for_day(gd2, target_date, firm_order, subtype_by_id)
        if gd is not None:
            out[group_name] = gd
    return out


def _serialize_work_data(work_data: dict) -> dict:
    blob: dict[str, dict] = {}
    for group_name, gd in work_data.items():
        sg: dict[str, Any] = {
            "area_by_firm": dict(gd.get("area_by_firm") or {}),
            "area_by_crop": {
                firm: dict(by_crop)
                for firm, by_crop in (gd.get("area_by_crop") or {}).items()
            },
        }
        for key in (
            "machines_own",
            "machines_hired",
            "machines_sp",
            "machines_tr",
            "machines_no_sub",
            "machines_transport",
        ):
            raw = gd.get(key) or {}
            sg[key] = {firm: sorted(int(x) for x in mids) for firm, mids in raw.items()}
        blob[group_name] = sg
    return blob


def _deserialize_work_data(blob: dict) -> dict:
    out: dict[str, dict] = {}
    for group_name, gd in blob.items():
        rec: dict[str, Any] = {
            "area_by_firm": dict(gd.get("area_by_firm") or {}),
            "area_by_crop": defaultdict(
                lambda: defaultdict(float),
                {
                    firm: defaultdict(float, dict(by_crop))
                    for firm, by_crop in (gd.get("area_by_crop") or {}).items()
                },
            ),
        }
        for key in (
            "machines_own",
            "machines_hired",
            "machines_sp",
            "machines_tr",
            "machines_no_sub",
            "machines_transport",
        ):
            raw = gd.get(key) or {}
            rec[key] = defaultdict(set, {firm: set(mids) for firm, mids in raw.items()})
        out[group_name] = rec
    return out


def _json_default(obj: Any) -> Any:
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, set):
        return sorted(int(x) for x in obj)
    if isinstance(obj, defaultdict):
        return dict(obj)
    raise TypeError(f"Не сериализуется: {type(obj)!r}")


def _serialize_report2_raw(report2_work_data: dict) -> dict:
    """Сырые данные сводки 2: ключи date, set, defaultdict — приводим к JSON."""
    def _walk(obj: Any) -> Any:
        if obj is None or isinstance(obj, (bool, int, float, str)):
            return obj
        if isinstance(obj, date):
            return obj.isoformat()
        if isinstance(obj, set):
            return sorted(int(x) for x in obj)
        if isinstance(obj, defaultdict):
            return _walk(dict(obj))
        if isinstance(obj, dict):
            return {
                (k.isoformat() if isinstance(k, date) else k): _walk(v)
                for k, v in obj.items()
            }
        if isinstance(obj, (list, tuple)):
            return [_walk(x) for x in obj]
        raise TypeError(f"Не сериализуется: {type(obj)!r}")

    walked = _walk(report2_work_data)
    if not isinstance(walked, dict):
        raise TypeError("report2_work_data должен быть dict")
    return walked


def write_r23_bundle(
    path: Path,
    *,
    report_date: date,
    prev_date: date,
    work_data: dict,
    work_data_prev: dict,
    report_stamp: date | None = None,
    source: str = "report2",
    report2_raw: dict | None = None,
    firm_order: list[str] | None = None,
    subtype_by_id: dict[int, str] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "version": 2,
        "source": source,
        "report_stamp": (report_stamp or report_date + timedelta(days=1)).isoformat(),
        "report_date": report_date.isoformat(),
        "prev_date": prev_date.isoformat(),
        "work_data": _serialize_work_data(work_data),
        "work_data_prev": _serialize_work_data(work_data_prev),
    }
    if report2_raw is not None:
        payload["report2_raw"] = _serialize_report2_raw(report2_raw)
    if firm_order is not None:
        payload["firm_order"] = list(firm_order)
    if subtype_by_id is not None:
        payload["subtype_by_id"] = {
            str(mid): sub for mid, sub in subtype_by_id.items()
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=0), encoding="utf-8")


def load_r23_bundle(path: Path) -> tuple[date, date, dict, dict] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if int(payload.get("version") or 0) not in {1, 2}:
        return None
    rd = date.fromisoformat(str(payload["report_date"]))
    pd = date.fromisoformat(str(payload["prev_date"]))
    subtype_by_id = {
        int(k): str(v) for k, v in (payload.get("subtype_by_id") or {}).items()
    }
    firm_order = [str(x) for x in (payload.get("firm_order") or [])]
    raw2 = payload.get("report2_raw")
    if isinstance(raw2, dict) and firm_order and subtype_by_id:
        wd = build_r23_work_data_from_report2(raw2, rd, firm_order, subtype_by_id)
        wp = build_r23_work_data_from_report2(raw2, pd, firm_order, subtype_by_id)
    else:
        wd = _deserialize_work_data(payload.get("work_data") or {})
        wp = _deserialize_work_data(payload.get("work_data_prev") or {})
    return rd, pd, wd, wp


def try_load_r23_bundle_for_analytics(
    report_date: date,
    prev_date: date,
) -> tuple[dict, dict] | None:
    """Читает bundle из env; None если файла нет или даты не совпали с запрошенными сутками."""
    raw = (os.environ.get(BUNDLE_IN_ENV) or "").strip()
    if not raw:
        return None
    loaded = load_r23_bundle(Path(raw))
    if loaded is None:
        return None
    rd, pd, work_data, work_data_prev = loaded
    if not work_data and not work_data_prev:
        return None
    if rd != report_date or pd != prev_date:
        platform_raw = (os.environ.get("REPORT_DATE") or "").strip()
        bstamp = ""
        try:
            payload = json.loads(Path(raw).read_text(encoding="utf-8"))
            bstamp = str(payload.get("report_stamp") or "").strip()
        except (OSError, ValueError, TypeError):
            pass
        if platform_raw and bstamp and platform_raw == bstamp:
            return work_data, work_data_prev
        print(
            f"[r23] Даты bundle ({rd}/{pd}) ≠ запрошенные ({report_date}/{prev_date}) — "
            f"снимок отклонён, пересоберите сводку 2 за {report_date}.",
            flush=True,
        )
        return None
    return work_data, work_data_prev


def peek_bundle_reporting_dates(
    path: Path,
    *,
    stamp: date | None = None,
) -> tuple[date, date] | None:
    """Даты суток/сравнения из bundle, если штамп совпадает с REPORT_DATE платформы."""
    loaded = load_r23_bundle(path)
    if loaded is None:
        return None
    rd, pd, _, _ = loaded
    if stamp is not None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            bstamp = str(payload.get("report_stamp") or "").strip()
            if bstamp and bstamp != stamp.isoformat():
                return None
        except (OSError, ValueError, TypeError):
            return None
    return rd, pd


def export_r23_bundle_after_report2(
    report2_work_data: dict,
    *,
    report_stamp: date,
    firm_order: list[str],
    subtype_by_id: dict[int, str],
    reporting_date: date | None = None,
) -> bool:
    """Пишет bundle, если задан SVODKI_R23_WORK_DATA_BUNDLE_OUT."""
    out_raw = (os.environ.get(BUNDLE_OUT_ENV) or "").strip()
    if not out_raw:
        return False
    if reporting_date is not None:
        report_date = reporting_date
        prev_date = reporting_date - timedelta(days=1)
    else:
        report_date, prev_date = analytics_dates_from_report_stamp(
            report_stamp, report2_work_data
        )
    work_data = build_r23_work_data_from_report2(
        report2_work_data, report_date, firm_order, subtype_by_id
    )
    work_data_prev = build_r23_work_data_from_report2(
        report2_work_data, prev_date, firm_order, subtype_by_id
    )
    write_r23_bundle(
        Path(out_raw),
        report_date=report_date,
        prev_date=prev_date,
        work_data=work_data,
        work_data_prev=work_data_prev,
        report_stamp=report_stamp,
        firm_order=firm_order,
        subtype_by_id=subtype_by_id,
    )
    vsp = work_data.get("2.4 Вспашка") or {}
    spr = work_data.get("3.1 Опрыскивание") or {}
    vsp_m = sum(
        len(vsp.get("machines_own", {}).get(f, set()))
        + len(vsp.get("machines_hired", {}).get(f, set()))
        for f in firm_order
    )
    spr_ha = sum((spr.get("area_by_firm") or {}).values())
    print(
        f"  [r23←2] Снимок для сводки 23: {out_raw} "
        f"(сутки {report_date}, сравнение {prev_date}, групп {len(work_data)}, "
        f"вспашка {vsp_m} ед., опрыскивание {spr_ha} га)",
        flush=True,
    )
    return True
