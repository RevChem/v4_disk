# -*- coding: utf-8 -*-
"""Сверка суточных фактов хранилища с прямым расчётом из raw (task_area_shared.area_ha_by_shift_date).

Запуск из svodki/plan на реальном хранилище (только чтение):
    SVODKI_TASK_STORE_DIR=/путь python tools/verify_daily_facts.py [--limit 0]
Код возврата 0 — по каждой строке площади факты совпали с прямым расчётом (допуск 1e-9 га)."""
from __future__ import annotations

import argparse, json, os, sqlite3, sys, zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import plans  # noqa: F401,E402
from task_area_shared import area_ha_by_shift_date  # noqa: E402

TZ = timezone(timedelta(hours=float(os.environ.get("SVODKI_STORE_TZ_HOURS", "3") or 3)))


def parse_dt(s):  # копия parse_dt из 2_main_ideal_compact.py
    if s is None:
        return None
    if isinstance(s, (int, float)):
        t = float(s)
        t = t / 1000.0 if t > 1e12 else t
        return datetime.fromtimestamp(t, tz=timezone.utc).astimezone(TZ)
    s = str(s).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)


def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    d = os.environ.get("SVODKI_TASK_STORE_DIR")
    if not d:
        raise SystemExit("SVODKI_TASK_STORE_DIR не задан")
    con = sqlite3.connect(f"file:{Path(d) / 'store.sqlite'}?mode=ro", uri=True)
    daily: dict[int, dict[str, float]] = {}
    for rid, sd, ha in con.execute("SELECT rid, shift_date, ha FROM mtfmi_daily"):
        daily.setdefault(rid, {})[sd] = ha
    facts = {r[0]: r[1:] for r in con.execute("SELECT rid, covered_area, hourly_n FROM mtfmi_fact")}
    bad = n = 0
    q = "SELECT rid, raw, data FROM mtfmi" + (f" LIMIT {a.limit}" if a.limit else "")
    for rid, raw, data in con.execute(q):
        item = json.loads(zlib.decompress(raw).decode("utf-8")) if raw else json.loads(data)
        want = {k.isoformat(): v for k, v in area_ha_by_shift_date(item, parse_dt=parse_dt).items()}
        got = daily.get(rid, {})
        n += 1
        ok = set(want) == set(got) and all(abs(want[k] - got[k]) < 1e-9 for k in want)
        f = facts.get(rid)
        ok = ok and f is not None and f[1] == len(item.get("covered_area_hourly") or []) \
            and abs(f[0] - float(item.get("covered_area") or 0)) < 1e-9
        if not ok:
            bad += 1
            if bad <= 10:
                print("РАСХОЖДЕНИЕ rid", rid, want, got, f)
    print(f"проверено строк площади: {n}, расхождений: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
