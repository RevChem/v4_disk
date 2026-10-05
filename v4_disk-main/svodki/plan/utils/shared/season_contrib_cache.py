# -*- coding: utf-8 -*-
"""Сезонные суммы сводки как сумма кэшируемых вкладов заданий (пересчёт только изменившихся).

Сводка 2 считает сезон циклом по всем заданиям: для каждого — фирма, культура, площадь по сменным дням и
машинам. Вклад одного задания не зависит от остальных, поэтому его можно посчитать один раз, положить в
sqlite-хранилище (``svodki_calc_cache``) и на следующий день пересчитывать только те задания, у которых
изменилась версия (``task_rev``: само задание, строки площади, связи/привязка агроопераций) или значения
справочников, на которые они смотрят (``stamp``).

Режим ``SVODKI_SEASON_CACHE``:
  off     (по умолчанию) — прежний полный пересчёт, кэш не читается и не пишется;
  shadow  — полный пересчёт (его результат идёт в отчёт) + расчёт через кэш; расхождения пишутся в лог.
            Кэш при этом наполняется. Включить на неделю на реальных данных, затем перейти на on;
  on      — вклады берутся из кэша; считаются только новые/изменённые и «горячие» задания.

«Горячие» задания (end_time позже ``SVODKI_SEASON_CACHE_HOT_DAYS`` (3) суток до начала отчётной смены, и задания без
смены) никогда не кэшируются: их вклад зависит от даты отчёта (окно смены, отчётные сутки, захват фактов).

``SVODKI_SEASON_CACHE_AUDIT`` (0.02 в on): доля попаданий в кэш, которую выборочно пересчитывают и сверяют —
страховка от забытой зависимости. Расхождение: в лог, используется свежий пересчёт, запись кэша исправляется.

Любая ошибка кэша → полный пересчёт, как раньше.
"""
from __future__ import annotations

import hashlib
import os
import random
import time
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Callable

TOL = 1e-6


class Unserializable(Exception):
    """Ключ/значение аккумулятора нельзя сохранить в JSON без потери типа — группу считаем без кэша."""


def cache_mode() -> str:
    m = (os.environ.get("SVODKI_SEASON_CACHE") or "off").strip().lower()
    return m if m in {"off", "shadow", "on"} else "off"


def hot_days() -> float:
    try:
        return float((os.environ.get("SVODKI_SEASON_CACHE_HOT_DAYS") or "").strip() or 3)
    except ValueError:
        return 3.0


def audit_fraction(mode: str) -> float:
    raw = (os.environ.get("SVODKI_SEASON_CACHE_AUDIT") or "").strip()
    try:
        return max(0.0, min(1.0, float(raw))) if raw else (0.02 if mode == "on" else 0.0)
    except ValueError:
        return 0.02


def code_hash(*paths: str | Path) -> str:
    """Отпечаток кода расчёта: правка файла сводки/общих модулей сбрасывает кэш."""
    h = hashlib.sha1()
    for p in paths:
        try:
            h.update(Path(p).read_bytes())
        except OSError:
            h.update(str(p).encode())
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------- аккумуляторы
def new_acc(firm_order) -> dict:
    """Те же структуры, что заводит сводка 2 для группы работ."""
    return {
        "mda": {f: defaultdict(lambda: defaultdict(float)) for f in firm_order},
        "mdg": {f: defaultdict(dict) for f in firm_order},
        "fda": {f: defaultdict(lambda: defaultdict(float)) for f in firm_order},
        "mdi": {f: defaultdict(lambda: defaultdict(list)) for f in firm_order},
        "agg": defaultdict(lambda: defaultdict(lambda: defaultdict(float))),
        "fcf": defaultdict(lambda: defaultdict(set)),
    }


def _d(day: Any) -> str:
    if type(day) is not date:  # datetime и прочее — не сериализуем молча
        raise Unserializable(f"ключ даты {type(day).__name__}")
    return day.isoformat()


def _k(v: Any) -> Any:
    if v is not None and type(v) not in (int, str):
        raise Unserializable(f"ключ {type(v).__name__}")
    return v


def to_payload(acc: dict, has_data: bool) -> dict:
    out: dict[str, Any] = {"h": bool(has_data), "mda": [], "mdg": [], "fda": [], "mdi": [], "agg": [], "fcf": []}
    for firm, by_day in acc["mda"].items():
        for day, by_mid in by_day.items():
            for mid, v in by_mid.items():
                out["mda"].append([firm, _d(day), _k(mid), float(v)])
    for firm, by_day in acc["mdg"].items():
        for day, by_mid in by_day.items():
            for mid, g in by_mid.items():
                out["mdg"].append([firm, _d(day), _k(mid), g])
    for firm, by_day in acc["fda"].items():
        for day, by_fid in by_day.items():
            for fid, v in by_fid.items():
                out["fda"].append([firm, _d(day), _k(fid), float(v)])
    for firm, by_day in acc["mdi"].items():
        for day, by_mid in by_day.items():
            for mid, items in by_mid.items():
                if items:
                    out["mdi"].append([firm, _d(day), _k(mid),
                                       [[_k(f), c, float(h)] for f, c, h in items]])
    for firm, by_crop in acc["agg"].items():
        for crop, by_day in by_crop.items():
            for day, v in by_day.items():
                out["agg"].append([firm, crop, _d(day), float(v)])
    for firm, by_crop in acc["fcf"].items():
        for crop, fids in by_crop.items():
            if fids:
                out["fcf"].append([firm, crop, sorted((_k(x) for x in fids), key=repr)])
    return out


def merge_payload(acc: dict, p: dict) -> bool:
    """Влить вклад в аккумуляторы группы (в порядке заданий: суммы складываются, группа машины — последняя)."""
    for firm, day, mid, v in p["mda"]:
        acc["mda"][firm][date.fromisoformat(day)][mid] += v
    for firm, day, mid, g in p["mdg"]:
        acc["mdg"][firm][date.fromisoformat(day)][mid] = g
    for firm, day, fid, v in p["fda"]:
        acc["fda"][firm][date.fromisoformat(day)][fid] += v
    for firm, day, mid, items in p["mdi"]:
        acc["mdi"][firm][date.fromisoformat(day)][mid].extend((f, c, h) for f, c, h in items)
    for firm, crop, day, v in p["agg"]:
        acc["agg"][firm][crop][date.fromisoformat(day)] += v
    for firm, crop, fids in p["fcf"]:
        acc["fcf"][firm][crop].update(fids)
    return bool(p["h"])


def flatten(acc: dict) -> dict:
    """Плоское представление для сравнения: пустые контейнеры отбрасываются, списки сортируются."""
    p = to_payload(acc, False)
    flat: dict[tuple, Any] = {}
    for firm, day, mid, v in p["mda"]:
        flat[("mda", firm, day, mid)] = v
    for firm, day, mid, g in p["mdg"]:
        flat[("mdg", firm, day, mid)] = g
    for firm, day, fid, v in p["fda"]:
        flat[("fda", firm, day, fid)] = v
    for firm, day, mid, items in p["mdi"]:
        flat[("mdi", firm, day, mid)] = sorted((repr(f), c, round(h, 6)) for f, c, h in items)
    for firm, crop, day, v in p["agg"]:
        flat[("agg", firm, crop, day)] = v
    for firm, crop, fids in p["fcf"]:
        flat[("fcf", firm, crop)] = [repr(x) for x in fids]
    return flat


def diff_flat(a: dict, b: dict, limit: int = 8) -> tuple[int, list[str]]:
    """Число расхождений и несколько примеров (a — эталон полного пересчёта, b — через кэш)."""
    bad: list[str] = []
    n = 0
    for k in sorted(set(a) | set(b), key=repr):
        va, vb = a.get(k), b.get(k)
        same = (abs(float(va) - float(vb)) <= TOL) if isinstance(va, float) and isinstance(vb, float) else va == vb
        if va is None or vb is None:
            # ноль в одной стороне и отсутствие в другой — не расхождение
            other = vb if va is None else va
            same = isinstance(other, float) and abs(other) <= TOL
        if not same:
            n += 1
            if len(bad) < limit:
                bad.append(f"{k}: полный={va} кэш={vb}")
    return n, bad


# --------------------------------------------------------------------------- драйвер
def _stamp(parts: Any) -> str:
    import json

    return hashlib.sha1(json.dumps(parts, ensure_ascii=False, default=str, separators=(",", ":")).encode()).hexdigest()[:20]


def cached_pass(
    *,
    store,
    snapshot: dict[int, int],
    ckey: str,
    label: str,
    tasks: list[dict],
    no_shift: list[dict],
    target: dict,
    new_acc_fn: Callable[[], dict],
    accumulate: Callable[[list, list, dict], bool],
    stamp_parts: Callable[[dict], Any],
    is_hot: Callable[[dict], bool],
    audit: float = 0.0,
    log: str = "",
) -> tuple[bool, dict]:
    """Один проход по заданиям группы с кэшем вкладов. Результат вливается в ``target``.

    accumulate(tasks_a, tasks_b, acc) — исходные циклы сводки над переданными списками (один и тот же код,
    что и в полном пересчёте), поэтому вклад задания = то, что дал бы полный пересчёт для него одного.
    Возвращает (group_has_data, статистика). Исключение — вызывающий откатывается на полный пересчёт.
    """
    t0 = time.monotonic()
    st = {"tasks": len(tasks), "cold": 0, "hit": 0, "calc": 0, "hot": 0, "written": 0, "audit": 0, "audit_bad": 0,
          "no_shift": len(no_shift)}
    cold: dict[int, str] = {}
    for t in tasks:
        tid = int(t["id"])
        if tid in snapshot and not is_hot(t):  # задания не из хранилища и горячие не кэшируем
            cold[tid] = _stamp(stamp_parts(t))
    st["cold"] = len(cold)
    cached = store.calc_get(ckey, cold, cold) if cold else {}
    tmp = new_acc_fn()
    has = False
    to_put: list[tuple[int, int, str, dict]] = []
    rnd = random.Random(1)
    for t in tasks:
        tid = int(t["id"])
        p = cached.get(tid)
        if p is not None and audit > 0 and rnd.random() < audit:
            st["audit"] += 1
            fresh_acc = new_acc_fn()
            fresh = to_payload(fresh_acc, accumulate([t], [], fresh_acc))
            n, bad = diff_flat(flatten(_acc_from(p, new_acc_fn)), flatten(fresh_acc))
            if n or fresh["h"] != p["h"]:
                st["audit_bad"] += 1
                print(f"  {log}[season-cache] АУДИТ: задание {tid} расходится с пересчётом ({n}): {bad[:3]}", flush=True)
                p = None
                cached.pop(tid, None)
        if p is not None:
            st["hit"] += 1
            has = merge_payload(tmp, p) or has
            continue
        acc_t = new_acc_fn()
        h = accumulate([t], [], acc_t)
        payload = to_payload(acc_t, h)
        has = merge_payload(tmp, payload) or has
        if tid in cold:
            st["calc"] += 1
            to_put.append((tid, snapshot[tid], cold[tid], payload))
        else:
            st["hot"] += 1
    for t in no_shift:  # зависят от даты отчёта — всегда считаем заново
        acc_t = new_acc_fn()
        h = accumulate([], [t], acc_t)
        has = merge_payload(tmp, to_payload(acc_t, h)) or has
    if to_put:
        st["written"] = store.calc_put(ckey, to_put, label=label)
    else:
        store.calc_touch(ckey, label)
    merge_payload(target, to_payload(tmp, False))
    st["sec"] = round(time.monotonic() - t0, 1)
    print(f"  {log}[season-cache] {label}: заданий {st['tasks']} (кэшируемых {st['cold']}): из кэша {st['hit']}, "
          f"пересчитано {st['calc']}, горячих {st['hot']}, без смены {st['no_shift']}, записано {st['written']}"
          f"{', аудит ' + str(st['audit']) + ' (расхождений ' + str(st['audit_bad']) + ')' if st['audit'] else ''}: "
          f"{st['sec']} с", flush=True)
    return has, st


def _acc_from(payload: dict, new_acc_fn: Callable[[], dict]) -> dict:
    acc = new_acc_fn()
    merge_payload(acc, payload)
    return acc
