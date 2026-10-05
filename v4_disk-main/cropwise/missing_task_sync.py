"""
cropwise/missing_task_sync.py
==============================
Логика вкладки «Возможное отсутствие задачи» панели «Нарушения»:
машина БЫЛА НА ПОЛЕ (machine_object_intersections, object_type=Field), а
задания на это время нет.

Две причины (reason):

  no_task            — на часть визита (не менее MIN_UNCOVERED_MINUTES) у машины
                       нет НИ ОДНОГО задания (machine_tasks), пересекающего это время.
                       В таблицу попадает одна строка на машину: самый длинный
                       такой отрезок;
  field_not_in_task  — задание есть, но этого поля в нём нет (нет ни
                       machine_task_field_mapping_items с этим field_id, ни
                       task.field_id), и машина была на поле не меньше
                       MIN_OTHER_FIELD_MINUTES. Короче — проезд, не работа.

Одно и то же задание в таблице не повторяется: если машина «проехала» по нескольким
полям, а задание у неё одно, остаётся строка с самым долгим пребыванием, а остальные
поля уходят в also_fields этой строки (dedupe_rows_by_task).

Визит = склеенные (пауза ≤ VISIT_MERGE_GAP_MIN) пересечения MOI машина × поле.
hour_start у записи — час начала заезда, поэтому выборка начинается на 12 ч
раньше окна: иначе заезд, начатый до границы, не попадает в окно.
GPS-треки и контуры полей не используются: заезд берётся из MOI как есть.
Короткие «касания» границы поля отсекаются порогами (MIN_*), поля с культурами
из SKIP_CROPS_CHECK9 (залежь, дороги и т.п.) не проверяются. Проверяются только
агро-машины (machine_type != transport, не скрытые).

Окно — как у «Отсутствующих задач» (check6): «вчера 06:00» … min(+2 суток, now).
Кэш — Redis (missing_task_router.py), Celery missing_task_refresh раз в час.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp

from app.cropwise.client import (
    CROPWISE_HEADERS,
    _CROPWISE_TIMEOUT,
    _fetch_all_pages,
    _fetch_history_items_for_date_range,
    _fetch_machine_object_intersections,
    _fetch_mtfmi_by_task_ids,
    _fetch_task_agro_op_links_by_task_ids,
    moi_row_to_interval,
    parse_moi_dt,
)
from app.logging_config import get_logger

logger = get_logger("cropwise.missing_task")

TZ_MSK = timezone(timedelta(hours=3))
DAY_START_HOUR = 6
CROPWISE_TASK_BASE = "https://operations.cropwise.com/machines"

# ── Пороги (подбираются по факту, все в одном месте) ─────────────────────────
VISIT_MERGE_GAP_MIN = 30          # склейка визитов: геометрия «моргает» на границе поля
MOI_HOUR_LOOKBACK = timedelta(hours=12)  # hour_start — час начала заезда
MIN_UNCOVERED_MINUTES = 10        # no_task: минимум минут без задания
MIN_OTHER_FIELD_MINUTES = 30      # field_not_in_task: короче — проезд мимо поля, не работа
TASK_PAD_MINUTES = 5              # допуск вокруг [start_time, end_time] задания
INCLUDE_NO_OPERATION = False      # причина no_operation выключена

MOI_MACHINE_TYPES_EXCLUDED = frozenset({"transport"})   # только агро-техника

FIRM_ORDER: list[str] = [
    "АФ Чистопольская", "АгроРаздолье", "Аксу-Агро",
    "НурлатАгро", "Дрожжаное Рассвет Агро", "Актай",
    "Лениногорская Птицефабрика",
]

REASON_LABELS = {
    "no_task": "Нет задания",
    "field_not_in_task": "Задание есть, но поля в нём нет",
    "no_operation": "Нет агрооперации по полю",
}

Interval = tuple[datetime, datetime]


# =============================================================================
# Интервальная арифметика
# =============================================================================

def merge_intervals(ivs: list[Interval], gap: timedelta = timedelta(0)) -> list[Interval]:
    """Склеивает пересекающиеся/соседние интервалы (пауза ≤ gap)."""
    if not ivs:
        return []
    ordered = sorted(ivs, key=lambda x: x[0])
    out: list[list[datetime]] = [[ordered[0][0], ordered[0][1]]]
    for a, b in ordered[1:]:
        if a <= out[-1][1] + gap:
            if b > out[-1][1]:
                out[-1][1] = b
        else:
            out.append([a, b])
    return [(x[0], x[1]) for x in out]


def intersect_intervals(base: Interval, cuts: list[Interval]) -> list[Interval]:
    """Части base, лежащие внутри объединения cuts (cuts уже склеены)."""
    out: list[Interval] = []
    for a, b in cuts:
        lo, hi = max(a, base[0]), min(b, base[1])
        if hi > lo:
            out.append((lo, hi))
    return out


def subtract_intervals(base: Interval, cuts: list[Interval]) -> list[Interval]:
    """Части base, НЕ покрытые cuts (cuts уже склеены и отсортированы)."""
    out: list[Interval] = []
    cur = base[0]
    for a, b in cuts:
        if b <= cur:
            continue
        if a >= base[1]:
            break
        if a > cur:
            out.append((cur, min(a, base[1])))
        cur = max(cur, b)
        if cur >= base[1]:
            break
    if cur < base[1]:
        out.append((cur, base[1]))
    return out


def _minutes(iv: Interval) -> float:
    return (iv[1] - iv[0]).total_seconds() / 60.0


def _total_minutes(ivs: list[Interval]) -> float:
    return sum(_minutes(i) for i in ivs)


def _fmt_duration(minutes: float) -> str:
    m = max(0, int(round(minutes)))
    return f"{m // 60}:{m % 60:02d}"


def _window(now: datetime) -> tuple[datetime, datetime]:
    """Как violations_sync._shift_window: вчера 06:00 … min(+2 суток, now)."""
    day_start = datetime(now.year, now.month, now.day, DAY_START_HOUR, tzinfo=TZ_MSK) - timedelta(days=1)
    return day_start, min(day_start + timedelta(days=2), now)


# =============================================================================
# Чистая логика (без сети — тестируется отдельно)
# =============================================================================

def build_visits(
    moi_rows: list[dict],
    machine_ids: set[int],
    win_lo: datetime,
    win_hi: datetime,
    *,
    merge_gap_min: int = VISIT_MERGE_GAP_MIN,
) -> dict[tuple[int, int], list[Interval]]:
    """{(machine_id, field_id): [визит, ...]} — склеенные и обрезанные по окну."""
    raw: dict[tuple[int, int], list[Interval]] = defaultdict(list)
    for row in moi_rows:
        iv = moi_row_to_interval(row, open_end=win_hi)
        if iv is None:
            continue
        mid, fid, a, b = iv
        if mid not in machine_ids:
            continue
        a, b = max(a, win_lo), min(b, win_hi)
        if b > a:
            raw[(mid, fid)].append((a, b))
    gap = timedelta(minutes=merge_gap_min)
    return {k: merge_intervals(v, gap) for k, v in raw.items()}


def task_interval(task: dict, win_hi: datetime, pad_min: int = TASK_PAD_MINUTES) -> Optional[Interval]:
    """[start_time − pad, end_time + pad]; задание без end_time — открыто до win_hi."""
    a = parse_moi_dt(task.get("start_time"))
    if a is None:
        return None
    b = parse_moi_dt(task.get("end_time")) or win_hi
    pad = timedelta(minutes=pad_min)
    return a - pad, max(b, a) + pad


def classify_visit(
    visit: Interval,
    machine_tasks: list[tuple[Interval, dict]],
) -> tuple[list[Interval], list[Interval], list[dict]]:
    """→ (без задания, под заданиями, задания, пересёкшие визит)."""
    overlapping = [(iv, t) for iv, t in machine_tasks if iv[1] > visit[0] and iv[0] < visit[1]]
    union = merge_intervals([iv for iv, _ in overlapping])
    uncovered = subtract_intervals(visit, union)
    covered = intersect_intervals(visit, union)
    return uncovered, covered, [t for _, t in overlapping]


def dedupe_rows_by_task(rows: list[dict]) -> list[dict]:
    """Одно задание машины — одна строка.

    Строки со ссылками на задания (field_not_in_task / no_operation) группируются по
    машине. Идём от самого долгого пребывания на поле к самому короткому: строка
    «забирает» свои задания, а следующая, где все задания уже заняты, не выводится —
    её поле записывается в also_fields строки-владельца. Если у следующей строки есть
    ещё и новые задания, остаются только они (ни одно задание не показывается дважды).
    Строки без заданий (no_task) не затрагиваются."""
    plain = [r for r in rows if not r["tasks"]]
    by_machine: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        if r["tasks"]:
            by_machine[r["machine_id"]].append(r)

    kept: list[dict] = []
    for lst in by_machine.values():
        lst.sort(key=lambda r: (-r["duration_min"], r["start_ts"]))
        owner_of: dict[int, dict] = {}
        for r in lst:
            fresh = [t for t in r["tasks"] if t["task_id"] not in owner_of]
            if not fresh:
                owner = owner_of[r["tasks"][0]["task_id"]]
                also = owner.setdefault("also_fields", [])
                if r["field_id"] != owner["field_id"] and all(a["field_id"] != r["field_id"] for a in also):
                    also.append({
                        "field": r["field"], "field_id": r["field_id"],
                        "field_link": r["field_link"], "duration": r["duration"],
                    })
                continue
            r["tasks"] = fresh
            for t in fresh:
                owner_of[t["task_id"]] = r
            kept.append(r)
    return plain + kept


# =============================================================================
# Основной прогон
# =============================================================================

async def refresh_missing_task() -> dict:
    # Ленивые импорты: violations_sync тяжёлый и сам импортирует client.
    from app.cropwise.missing_task_router import update_cache
    from app.cropwise.violations_sync import (
        CHECK1_MIN_FIELD_AREA_HA,
        SKIP_CROPS_CHECK9,
        _build_field_to_firm,
        _fetch_op_field_map,
        _is_transport_work_type,
        _item_cov_before_cutoff,
    )

    now = datetime.now(tz=TZ_MSK)
    win_lo, win_hi = _window(now)
    lo_str = win_lo.strftime("%Y-%m-%dT%H:%M:%S")
    hi_str = win_hi.strftime("%Y-%m-%dT%H:%M:%S")
    now_str = now.strftime("%Y-%m-%dT%H:%M:%S")
    logger.info(f"missing_task refresh start: {lo_str} — {hi_str}")
    _t0 = time.monotonic()

    def _lap(stage: str) -> None:
        logger.info(f"missing_task [{time.monotonic() - _t0:6.1f}s] {stage}")

    async with aiohttp.ClientSession(headers=CROPWISE_HEADERS, timeout=_CROPWISE_TIMEOUT) as session:
        (
            tasks_by_end, tasks_by_start, machines_raw, work_types_raw,
            fields_raw, fgs_raw, gfs_raw, crops_raw, history_raw, hidden_raw,
        ) = await asyncio.gather(
            _fetch_all_pages(session, "machine_tasks", {"end_time_gt_eq": lo_str, "start_time_lt": hi_str}),
            _fetch_all_pages(session, "machine_tasks", {"start_time_gt_eq": lo_str, "start_time_lt": hi_str}),
            _fetch_all_pages(session, "machines"),
            _fetch_all_pages(session, "work_types"),
            _fetch_all_pages(session, "fields"),
            _fetch_all_pages(session, "field_groups"),
            _fetch_all_pages(session, "group_folders"),
            _fetch_all_pages(session, "crops"),
            _fetch_history_items_for_date_range(session, now.date(), now.date()),
            _fetch_all_pages(session, "inventory_history_items", {
                "historyable_type": "Machine", "hidden": "true",
                "event_start_at": now_str, "event_end_at": now_str,
            }),
        )

        hidden_ids = {int(i["historyable_id"]) for i in hidden_raw if i.get("historyable_id") is not None}
        machine_name: dict[int, str] = {}
        machine_plate: dict[int, str] = {}
        agro_ids: set[int] = set()
        for m in machines_raw:
            if m.get("id") is None:
                continue
            mid = int(m["id"])
            machine_name[mid] = m.get("name") or m.get("title") or m.get("model") or f"machine_{mid}"
            machine_plate[mid] = (m.get("registration_number") or "").strip()
            mtype = (m.get("machine_type") or "").lower()
            if mtype not in MOI_MACHINE_TYPES_EXCLUDED and mid not in hidden_ids:
                agro_ids.add(mid)

        work_type_name = {
            w["id"]: (w.get("name") or w.get("title") or f"wt_{w['id']}") for w in work_types_raw
        }
        field_name = {
            int(f["id"]): (f.get("name") or f.get("title") or f"field_{f['id']}")
            for f in fields_raw if f.get("id") is not None
        }
        field_to_firm = _build_field_to_firm(fields_raw, fgs_raw, gfs_raw)

        crop_id_name = {c["id"]: (c.get("name") or c.get("title") or "") for c in crops_raw}
        field_to_crop: dict[int, str] = {}
        for hi in history_raw:
            fid, cid = hi.get("field_id"), hi.get("crop_id")
            if fid and cid and hi.get("active"):
                cname = crop_id_name.get(cid, "")
                if cname:
                    field_to_crop[int(fid)] = cname

        # ── Задания по машинам ──────────────────────────────────────────────
        tasks_by_id: dict[int, dict] = {}
        for t in tasks_by_end + tasks_by_start:
            if t.get("id") is not None and t.get("machine_id") is not None:
                tasks_by_id[int(t["id"])] = t
        tasks_by_machine: dict[int, list[tuple[Interval, dict]]] = defaultdict(list)
        for t in tasks_by_id.values():
            iv = task_interval(t, win_hi)
            if iv is not None:
                tasks_by_machine[int(t["machine_id"])].append((iv, t))
        logger.info(f"missing_task: агро-машин={len(agro_ids)}, заданий за окно={len(tasks_by_id)}")
        _lap("справочники и задания загружены")

        # ── MOI (Field) по агро-машинам ─────────────────────────────────────
        moi_rows = await _fetch_machine_object_intersections(
            session, object_type="Field",
            hour_from=win_lo - MOI_HOUR_LOOKBACK, hour_to=win_hi,
            machine_ids=sorted(agro_ids),
        )
        visits = build_visits(moi_rows, agro_ids, win_lo, win_hi)
        logger.info(f"missing_task MOI: записей={len(moi_rows)}, пар машина–поле={len(visits)}")
        _lap("MOI загружены")

        # ── Классификация визитов ───────────────────────────────────────────
        candidates: list[dict] = []          # предварительные строки
        for (mid, fid), vlist in visits.items():
            crop = field_to_crop.get(fid, "")
            if crop in SKIP_CROPS_CHECK9:
                continue
            for v in vlist:
                uncovered, covered, overlapping = classify_visit(v, tasks_by_machine.get(mid, []))
                for seg in uncovered:
                    if _minutes(seg) >= MIN_UNCOVERED_MINUTES:
                        candidates.append({
                            "reason": "no_task", "mid": mid, "fid": fid, "seg": seg,
                            "task_ids": [],
                        })
                if _total_minutes(covered) >= MIN_OTHER_FIELD_MINUTES and overlapping:
                    span = (min(a for a, _ in covered), max(b for _, b in covered))
                    tids = [int(t["id"]) for t in overlapping]
                    candidates.append({
                        "reason": "_covered", "mid": mid, "fid": fid, "seg": span,
                        "task_ids": tids,
                    })

        _lap(f"кандидатов={len(candidates)}")
        need_mtfmi = {
            tid for c in candidates if c["reason"] == "_covered" for tid in c["task_ids"]
        }

        # ── Поле есть в задании? Агрооперация привязана? ────────────────────
        mtfmi_map = await _fetch_mtfmi_by_task_ids(session, sorted(need_mtfmi)) if need_mtfmi else {}

        def _task_has_field(t: dict, fid: int) -> bool:
            if t.get("field_id") and int(t["field_id"]) == fid:
                return True
            return any(
                it.get("field_id") and int(it["field_id"]) == fid
                for it in mtfmi_map.get(int(t["id"]), [])
            )

        op_links: dict[int, list[int]] = {}
        op_field_map: dict[int, Optional[int]] = {}
        if INCLUDE_NO_OPERATION:
            link_task_ids = sorted({
                tid for c in candidates if c["reason"] == "_covered" for tid in c["task_ids"]
                if any(
                    it.get("field_id") and int(it["field_id"]) == c["fid"]
                    for it in mtfmi_map.get(tid, [])
                )
            })
            if link_task_ids:
                op_links = await _fetch_task_agro_op_links_by_task_ids(session, link_task_ids)
                op_ids = {oid for ids in op_links.values() for oid in ids}
                op_field_map = await _fetch_op_field_map(op_ids) if op_ids else {}

    # ── Финальные строки ────────────────────────────────────────────────────
    rows: list[dict] = []
    for c in candidates:
        mid, fid, seg = c["mid"], c["fid"], c["seg"]
        reason = c["reason"]
        task_ids: list[int] = c["task_ids"]
        if reason == "_covered":
            tasks = [tasks_by_id[t] for t in task_ids if t in tasks_by_id]
            with_field = [t for t in tasks if _task_has_field(t, fid)]
            if not with_field:
                reason = "field_not_in_task"
                shown = tasks
            elif INCLUDE_NO_OPERATION:
                # Как check1: задание без транзита, не транспортный вид работ,
                # выработка по полю ≥ порога, нет привязанной операции по полю.
                shown = []
                eligible = False
                linked_here = False
                for t in with_field:
                    tid = int(t["id"])
                    wt = work_type_name.get(t.get("work_type_id"), "")
                    if _is_transport_work_type(wt) or float(t.get("transit_distance") or 0) > 0:
                        continue
                    items = [
                        it for it in mtfmi_map.get(tid, [])
                        if it.get("field_id") and int(it["field_id"]) == fid
                        and it.get("in_transit") is not True
                        and _item_cov_before_cutoff(it, win_hi) >= CHECK1_MIN_FIELD_AREA_HA
                    ]
                    if not items:
                        continue
                    eligible = True
                    shown.append(t)
                    if any(op_field_map.get(oid) == fid for oid in op_links.get(tid, [])):
                        linked_here = True
                if not eligible or linked_here:
                    continue
                reason = "no_operation"
            else:
                continue
        else:
            shown = []

        a, b = seg
        firm = field_to_firm.get(fid, "—")
        task_links = [
            {"task_id": int(t["id"]),
             "work_type": work_type_name.get(t.get("work_type_id"), "—"),
             "link": f"{CROPWISE_TASK_BASE}/{mid}/tasks/{int(t['id'])}"}
            for t in shown
        ]
        rows.append({
            "reason": reason,
            "reason_text": REASON_LABELS[reason],
            "firm": firm,
            "machine": machine_name.get(mid, f"machine_{mid}"),
            "machine_id": mid,
            "license_plate": machine_plate.get(mid) or "—",
            "field": field_name.get(fid, f"field_{fid}"),
            "field_id": fid,
            "crop": field_to_crop.get(fid, "—"),
            "start": a.strftime("%d.%m %H:%M"),
            "end": b.strftime("%d.%m %H:%M"),
            "start_ts": int(a.timestamp()),
            "end_ts": int(b.timestamp()),
            "duration": _fmt_duration(_minutes(seg)),
            "duration_min": round(_minutes(seg), 1),
            "tasks": task_links,
            "link": f"{CROPWISE_TASK_BASE}/{mid}",
            "field_link": f"https://operations.cropwise.com/fields/{fid}",
        })

    # Одно задание — одна строка (машина «проехала» по нескольким полям в рамках одного задания).
    n_before = len(rows)
    rows = dedupe_rows_by_task(rows)
    if len(rows) != n_before:
        logger.info(f"missing_task: убрано дублей по заданию={n_before - len(rows)}")

    # «Нет задания»: одна строка на машину — самый длинный отрезок без задания.
    best_no_task: dict[int, dict] = {}
    kept: list[dict] = []
    for r in rows:
        if r["reason"] != "no_task":
            kept.append(r)
            continue
        prev = best_no_task.get(r["machine_id"])
        rank = (r["duration_min"], -r["start_ts"])
        if prev is None or rank > (prev["duration_min"], -prev["start_ts"]):
            best_no_task[r["machine_id"]] = r
    rows = kept + list(best_no_task.values())

    rows.sort(key=lambda r: (
        FIRM_ORDER.index(r["firm"]) if r["firm"] in FIRM_ORDER else 999,
        r["machine"], r["start_ts"],
    ))
    by_reason = {k: sum(1 for r in rows if r["reason"] == k) for k in REASON_LABELS}
    update_cache(rows, now, {
        "machines": len(agro_ids), "tasks": len(tasks_by_id),
        "moi_records": len(moi_rows), "visits": sum(len(v) for v in visits.values()),
        "by_reason": by_reason,
    })
    logger.info(f"missing_task: строк={len(rows)} {by_reason}")
    return {"rows": len(rows), "by_reason": by_reason, "moi_records": len(moi_rows)}
