"""Независимый запрос work.json Cropwise и вопрос DeepSeek по заданиям.

Сверка со сводкой здесь не выполняется. Cookie нужен только на время запроса
и в кэш не записывается: ключ кэша — хеш cookie плюс окно и операции.
"""
from __future__ import annotations

import hashlib
import os
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

_MAX_DAYS = 62
_PREVIEW_ROWS = 80
_CACHE_TTL_SEC = 600
_CACHE_MAX = 8
_cache: dict[str, tuple[float, dict[str, Any]]] = {}

SYSTEM_PROMPT = (
    "Ты помощник агронома. Отвечай только по таблице заданий Cropwise. "
    "Строка — одно задание. ha — гектары из отчёта. driver — ФИО исполнителя, "
    "machine — техника, implement — орудие, field — поля из карточки задания "
    "(площадь в скобках — по карточке, она может отличаться от ha). "
    "fuel — расход топлива в литрах; «остаток N%» — уровень бака, а не расход. "
    "label — подпись строки отчёта. Пустая ячейка значит, что в карточке этого нет: "
    "так и скажи, не выдумывай. Если в шапке есть cards_error, пустые driver/machine/"
    "field/fuel могли не загрузиться, а не отсутствовать в Cropwise. "
    "Если таблица усечена, суммы и строки «По …» и «Топливо» "
    "посчитаны по всем загруженным заданиям, а не только по видимым строкам. "
    "Отвечай кратко, по-русски."
)

_TSV_COLUMNS = (
    "firm", "crop", "task_id", "ha", "driver", "machine", "implement", "field", "fuel", "label",
)


def env_session_cookie() -> str:
    """Cookie из .env (CROPWISE_SESSION_COOKIE), без записи в интерфейс на диск."""
    from proverka.reconcile import load_env, session_cookie

    load_env()
    return session_cookie()


def list_groups() -> list[dict[str, Any]]:
    from proverka.work_groups import work_group_wt_map

    return [{"name": name, "work_type_ids": ids} for name, ids in work_group_wt_map().items()]


def hints_for_groups(groups: list[str]) -> list[str]:
    from proverka.task_names import tasks_for_group

    hints: list[str] = []
    for name in groups:
        for task in tasks_for_group(name):
            if task not in hints:
                hints.append(task)
    return hints


def shift_window(date_from: date, date_to: date) -> tuple[str, str]:
    """Окно 06:00–06:00. date_to — последний включённый день смены."""
    if date_to < date_from:
        raise ValueError("Дата «по» раньше даты «с»")
    if (date_to - date_from).days > _MAX_DAYS:
        raise ValueError(f"Период не длиннее {_MAX_DAYS} дней")
    from plans import TZ_MSK

    start = datetime(date_from.year, date_from.month, date_from.day, 6, 0, tzinfo=TZ_MSK)
    end = datetime(date_to.year, date_to.month, date_to.day, 6, 0, tzinfo=TZ_MSK) + timedelta(days=1)
    return start.isoformat()[:19], end.isoformat()[:19]


def _crop_name(raw: str) -> str:
    try:
        from proverka.crop_compare import _crop_label

        return _crop_label(raw)
    except Exception:  # noqa: BLE001
        return str(raw or "").strip()


def tasks_from_body(body: dict[str, Any], firm_names: list[str], hints: list[str]) -> list[dict[str, Any]]:
    """Задания из regionRows. Культура подписывается по cropRows того же #task_id."""
    from proverka.cropwise_ui import parse_work_report_body, parse_work_report_detail_rows, task_id_from_label

    parsed = parse_work_report_body(body, firm_names, hints)
    by_task: dict[int, dict[str, Any]] = {}
    for row in parsed.get("detail_rows") or []:
        if row.get("aggregate"):
            continue
        tid = task_id_from_label(str(row.get("label") or ""))
        if tid is None:
            continue
        rec = by_task.setdefault(
            tid,
            {"task_id": tid, "firm": "", "ha": 0.0, "label": str(row.get("label") or ""), "paths": set()},
        )
        path = str(row.get("path") or "")
        if path and path in rec["paths"]:
            continue
        if path:
            rec["paths"].add(path)
        rec["ha"] += float(row.get("area_ha") or 0)
        firm = str(row.get("firm") or "").strip()
        if firm and not rec["firm"]:
            rec["firm"] = firm
        label = str(row.get("label") or "")
        if len(label) > len(rec["label"]):
            rec["label"] = label

    crop_names: dict[int, set[str]] = defaultdict(set)
    crop_ha: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    crop_rows = parse_work_report_detail_rows(
        body.get("cropRows") or [],
        body.get("cropColumns") or [],
        firm_names,
        hints,
    )
    for row in crop_rows:
        if row.get("aggregate"):
            continue
        tid = task_id_from_label(str(row.get("label") or ""))
        if tid is None:
            continue
        parts = [p.strip() for p in str(row.get("path") or "").split("→") if p.strip()]
        crop = _crop_name(parts[0]) if parts else ""
        if not crop:
            continue
        crop_names[tid].add(crop)
        crop_ha[tid][crop] += float(row.get("area_ha") or 0)

    out: list[dict[str, Any]] = []
    for tid, rec in by_task.items():
        crops = sorted(crop_names.get(tid) or [])
        out.append({
            "task_id": tid,
            "firm": rec["firm"],
            "crop": ", ".join(crops),
            "crops": crops,
            "crop_ha": {k: round(v, 2) for k, v in crop_ha.get(tid, {}).items()},
            "ha": round(rec["ha"], 2),
            "label": rec["label"],
        })
    out.sort(key=lambda r: (-r["ha"], r["firm"], r["task_id"]))
    return out


def filter_tasks(
    rows: list[dict[str, Any]],
    *,
    firm: str | None = None,
    crop: str | None = None,
) -> list[dict[str, Any]]:
    firm_q = (firm or "").strip().casefold()
    crop_q = (crop or "").strip().casefold()
    picked: list[dict[str, Any]] = []
    for row in rows:
        if firm_q and firm_q not in str(row.get("firm") or "").casefold():
            continue
        crops = [str(c) for c in (row.get("crops") or [])]
        if crop_q:
            matched = [c for c in crops if crop_q in c.casefold()]
            if not matched:
                continue
            specific = row.get("crop_ha") or {}
            ha = sum(float(specific.get(c) or 0) for c in matched)
            if ha <= 0:
                ha = float(row.get("ha") or 0)
            item = {**row, "crop": ", ".join(matched), "ha": round(ha, 2)}
        else:
            item = dict(row)
        item.pop("crops", None)
        item.pop("crop_ha", None)
        picked.append(item)
    picked.sort(key=lambda r: (-float(r["ha"]), r["firm"], r["task_id"]))
    return picked


def _tsv_cell(value: Any) -> str:
    return str(value or "").replace("\t", " ").replace("\r", " ").replace("\n", " ")


def _ha_rollup(rows: list[dict[str, Any]], key: str, title: str, *, limit: int = 12) -> str | None:
    bucket: dict[str, float] = defaultdict(float)
    missing = 0.0
    for row in rows:
        ha = float(row.get("ha") or 0)
        name = str(row.get(key) or "").strip()
        if not name:
            missing += ha
            continue
        bucket[name] += ha
    if not bucket and missing <= 0:
        return None
    parts = [f"{name}={ha:.2f}" for name, ha in sorted(bucket.items(), key=lambda kv: -kv[1])[:limit]]
    if len(bucket) > limit:
        parts.append(f"ещё {len(bucket) - limit}")
    if missing > 0.009:
        parts.append(f"без данных={missing:.2f}")
    return f"{title}: " + "; ".join(parts)


def _field_rollup(rows: list[dict[str, Any]], *, limit: int = 20) -> str | None:
    bucket: dict[str, float] = defaultdict(float)
    missing = 0.0
    for row in rows:
        items = row.get("field_items") or []
        if items:
            named = False
            for item in items:
                name = str(item.get("name") or "").strip()
                ha = float(item.get("ha") or 0)
                if not name:
                    missing += ha
                    continue
                named = True
                bucket[name] += ha
            if not named:
                missing += float(row.get("ha") or 0)
            continue
        name = str(row.get("field") or "").strip()
        if name:
            bucket[name] += float(row.get("ha") or 0)
        else:
            missing += float(row.get("ha") or 0)
    if not bucket and missing <= 0:
        return None
    parts = [f"{name}={ha:.2f}" for name, ha in sorted(bucket.items(), key=lambda kv: -kv[1])[:limit]]
    if len(bucket) > limit:
        parts.append(f"ещё {len(bucket) - limit}")
    if missing > 0.009:
        parts.append(f"без данных={missing:.2f}")
    return "По полям, га по карточке: " + "; ".join(parts)


def _fuel_lines(rows: list[dict[str, Any]]) -> list[str]:
    enriched = bool(rows) and "fuel" in rows[0]
    if not enriched:
        return []
    by_driver: dict[str, float] = defaultdict(float)
    total = 0.0
    with_liters = 0
    remainders = 0
    for row in rows:
        if str(row.get("fuel") or "").startswith("остаток"):
            remainders += 1
        liters = row.get("fuel_l")
        if liters is None:
            continue
        with_liters += 1
        amount = float(liters)
        total += amount
        name = str(row.get("driver") or "").strip() or "без ФИО"
        by_driver[name] += amount
    lines: list[str] = []
    if with_liters:
        lines.append(f"Топливо: {total:.1f} л по {with_liters} заданиям из {len(rows)}")
        parts = [
            f"{name}={amount:.1f}"
            for name, amount in sorted(by_driver.items(), key=lambda kv: -kv[1])[:12]
        ]
        if len(by_driver) > 12:
            parts.append(f"ещё {len(by_driver) - 12}")
        lines.append("Топливо по исполнителям, л: " + "; ".join(parts))
        if remainders:
            lines.append(
                f"Ещё у {remainders} заданий в карточке только остаток бака, не расход."
            )
    elif remainders:
        lines.append(
            "Расход топлива в литрах в карточках не найден. "
            f"Остаток бака (не расход) есть у {remainders} заданий."
        )
    else:
        lines.append("Расход топлива: в карточках заданий нет.")
    return lines


def format_tsv(rows: list[dict[str, Any]], *, header: str, max_rows: int) -> tuple[str, dict[str, Any]]:
    shown = rows[:max_rows]
    lines = [header, "\t".join(_TSV_COLUMNS)]
    for row in shown:
        lines.append(
            "\t".join([
                _tsv_cell(row.get("firm")),
                _tsv_cell(row.get("crop")),
                str(row.get("task_id") or ""),
                f"{float(row.get('ha') or 0):.2f}",
                _tsv_cell(row.get("driver")),
                _tsv_cell(row.get("machine")),
                _tsv_cell(row.get("implement")),
                _tsv_cell(row.get("field")),
                _tsv_cell(row.get("fuel")),
                _tsv_cell(row.get("label")),
            ])
        )
    total = round(sum(float(r.get("ha") or 0) for r in rows), 2)
    if len(rows) > len(shown):
        rest = round(sum(float(r.get("ha") or 0) for r in rows[len(shown):]), 2)
        lines.append(f"… не показано {len(rows) - len(shown)} заданий на {rest:.2f} га")
    lines.append(f"Итого: {len(rows)} заданий, {total:.2f} га")
    by_firm: dict[str, float] = defaultdict(float)
    for row in rows:
        by_firm[str(row.get("firm") or "—")] += float(row.get("ha") or 0)
    if len(by_firm) > 1:
        parts = [f"{name}={ha:.2f}" for name, ha in sorted(by_firm.items(), key=lambda kv: -kv[1])[:12]]
        lines.append("По фирмам: " + "; ".join(parts))
    if rows and "driver" in rows[0]:
        for extra in (
            _ha_rollup(rows, "driver", "По исполнителям, га отчёта"),
            _ha_rollup(rows, "machine", "По технике, га отчёта"),
            _field_rollup(rows),
        ):
            if extra:
                lines.append(extra)
        lines.extend(_fuel_lines(rows))
    return "\n".join(lines), {
        "rows_total": len(rows),
        "rows_sent": len(shown),
        "ha_total": total,
        "truncated": len(rows) > len(shown),
    }


def _firm_names() -> list[str]:
    from plans import FIRM_ORDER

    return list(FIRM_ORDER)


def _cache_key(cookie: str, t0: str, t1: str, groups: list[str], season: int | None) -> str:
    raw = "|".join([cookie, t0, t1, ",".join(groups), str(season or "")])
    return hashlib.sha256(raw.encode()).hexdigest()


def _remember(key: str, payload: dict[str, Any]) -> None:
    now = time.time()
    stale = [k for k, (ts, _) in _cache.items() if now - ts > _CACHE_TTL_SEC]
    for k in stale:
        _cache.pop(k, None)
    if key not in _cache and len(_cache) >= _CACHE_MAX:
        oldest = min(_cache.items(), key=lambda kv: kv[1][0])[0]
        _cache.pop(oldest, None)
    _cache[key] = (now, payload)


def load_snapshot(
    cookie: str,
    date_from: date,
    date_to: date,
    groups: list[str],
    season: int | None,
) -> dict[str, Any]:
    cookie = (cookie or "").strip() or env_session_cookie()
    if not cookie:
        raise ValueError("Введите cookie Cropwise или задайте CROPWISE_SESSION_COOKIE в .env")
    names = []
    seen: set[str] = set()
    for name in groups or []:
        text = str(name or "").strip()
        if text and text not in seen:
            seen.add(text)
            names.append(text)
    if not names:
        raise ValueError("Выберите хотя бы одну агрооперацию")
    known = {item["name"] for item in list_groups()}
    unknown = [name for name in names if name not in known]
    if unknown:
        raise ValueError("Неизвестная агрооперация: " + ", ".join(unknown))
    t0, t1 = shift_window(date_from, date_to)
    key = _cache_key(cookie, t0, t1, names, season)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] <= _CACHE_TTL_SEC:
        return hit[1]

    from proverka.cropwise_ui import fetch_work_report_resilient
    from proverka.work_groups import work_type_ids_for_group

    wt: list[int] = []
    for name in names:
        wt.extend(work_type_ids_for_group(name))
    wt = sorted(set(wt))
    if not wt:
        raise ValueError("Для выбранных операций не заданы work_type_id")
    hints = hints_for_groups(names)
    work = fetch_work_report_resilient(
        cookie,
        t0,
        t1,
        wt,
        hints,
        attempts=1,
        delay_sec=0.5,
        preferred_ui_session=season,
    )
    if not work.get("ok"):
        status = work.get("status")
        raise ValueError(f"Cropwise не ответил (HTTP {status}). Проверьте cookie и сезон.")
    body = work.get("body")
    if not isinstance(body, dict) or body.get("reportBlank"):
        rows: list[dict[str, Any]] = []
        work_types: list[str] = []
    else:
        rows = tasks_from_body(body, _firm_names(), hints)
        from proverka.cropwise_ui import parse_work_report_body

        work_types = list(parse_work_report_body(body, _firm_names(), hints).get("fact_work_types") or [])
    payload = {
        "from_time": t0,
        "to_time": t1,
        "session": work.get("ui_session_seasonValue"),
        "groups": names,
        "work_types": work_types,
        "rows": rows,
    }
    _remember(key, payload)
    return payload


def _max_rows() -> int:
    try:
        return max(1, int(os.environ.get("PROVERKA_FACTS_MAX_ROWS", "360")))
    except ValueError:
        return 120


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": row.get("task_id"),
        "firm": row.get("firm") or "",
        "crop": row.get("crop") or "",
        "ha": row.get("ha"),
        "driver": row.get("driver") or "",
        "machine": row.get("machine") or "",
        "implement": row.get("implement") or "",
        "field": row.get("field") or "",
        "fuel": row.get("fuel") or "",
        "label": row.get("label") or "",
    }


def _with_cards(rows: list[dict[str, Any]]) -> dict[str, Any]:
    from proverka.cropwise_task_cards import enrich_rows

    if not rows:
        return {"details_error": None, "details_note": "", "details_loaded": 0}
    return enrich_rows(rows)


def _select(
    cookie: str,
    date_from: date,
    date_to: date,
    groups: list[str],
    *,
    firm: str | None,
    crop: str | None,
    season: int | None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    snap = load_snapshot(cookie, date_from, date_to, groups, season)
    all_rows = snap["rows"]
    firms = sorted({str(r.get("firm") or "") for r in all_rows if r.get("firm")})
    crops = sorted({c for r in all_rows for c in (r.get("crops") or []) if c})
    picked = filter_tasks(all_rows, firm=firm, crop=crop)
    details = _with_cards(picked)
    shown = [_public_row(r) for r in picked[:_PREVIEW_ROWS]]
    public = {
        "ok": True,
        "available": True,
        "from_time": snap["from_time"],
        "to_time": snap["to_time"],
        "session": snap["session"],
        "groups": snap["groups"],
        "work_types": snap["work_types"],
        "rows_total": len(picked),
        "ha_total": round(sum(float(r.get("ha") or 0) for r in picked), 2),
        "firms": firms,
        "crops": crops,
        "rows": shown,
        "truncated": len(picked) > len(shown),
        "details_error": details.get("details_error"),
        "details_note": details.get("details_note") or "",
        "details_loaded": details.get("details_loaded") or 0,
    }
    return snap, picked, public


def query_tasks(
    cookie: str,
    date_from: date,
    date_to: date,
    groups: list[str],
    *,
    firm: str | None = None,
    crop: str | None = None,
    season: int | None = None,
) -> dict[str, Any]:
    _, _, public = _select(
        cookie, date_from, date_to, groups, firm=firm, crop=crop, season=season,
    )
    return public


def ask_tasks(
    cookie: str,
    date_from: date,
    date_to: date,
    groups: list[str],
    question: str,
    *,
    firm: str | None = None,
    crop: str | None = None,
    season: int | None = None,
    use_llm: bool = True,
) -> dict[str, Any]:
    snap, picked, base = _select(
        cookie, date_from, date_to, groups, firm=firm, crop=crop, season=season,
    )
    header = (
        f"# period={snap['from_time']}..{snap['to_time']}; "
        f"session={snap['session']}; groups={', '.join(snap['groups'])}"
    )
    if base.get("details_error"):
        header += f"; cards_error={base['details_error']}"
    if base.get("details_note"):
        header += f"; {base['details_note']}"
    table, meta = format_tsv(picked, header=header, max_rows=_max_rows())
    if not use_llm or not (question or "").strip():
        answer = {
            "ok": True,
            "llm_used": False,
            "question": (question or "").strip(),
            "answer": table if picked else "По заданному фильтру данных нет.",
            "usage": None,
            "model": None,
            "error": None,
        }
    else:
        from proverka.ai.facts_qa import answer_tsv

        answer = answer_tsv(question, table if picked else "", system=SYSTEM_PROMPT)
    base.update(answer)
    base.update(rows_sent=meta["rows_sent"], llm_truncated=meta["truncated"])
    return base
