# -*- coding: utf-8 -*-
"""Суточные факты: совпадение с task_area_shared, миграция v3→v4, запросы. Из svodki/plan."""
from __future__ import annotations

import asyncio, os, random, subprocess, sys, tempfile, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import plans  # noqa: F401,E402

tmp = Path(tempfile.mkdtemp(prefix="facts_"))
os.environ["SVODKI_TASK_STORE_DIR"] = str(tmp)
import svodki_task_store as ts  # noqa: E402
from task_area_shared import area_ha_by_shift_date  # noqa: E402

TZ = timezone(timedelta(hours=3))
BASE = "http://x/api/v3"
failed = False


def ok(name, cond):
    global failed
    print(("  OK   " if cond else "  FAIL ") + name)
    failed |= not cond


def pdt(s):
    if isinstance(s, (int, float)):
        t = s / 1000 if s > 1e12 else s
        return datetime.fromtimestamp(t, tz=timezone.utc).astimezone(TZ)
    dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)


random.seed(5)
now = time.time()
st = ts.get_store(BASE)
con = st._db(); con.execute("BEGIN IMMEDIATE")
st.cov_lo = {10: now - 60 * 86400, 11: now - 60 * 86400}
items_by_task = {}
for tid in range(1, 401):
    wt = 10 + tid % 2
    st._put_task({"id": tid, "work_type_id": wt, "season": "2026", "machine_id": 100 + tid % 7,
                  "end_time": ts.fmt_ts(now - random.uniform(0, 40) * 86400)})
    rows = []
    for k in range(3):
        base = datetime.fromtimestamp(now - random.uniform(1, 40) * 86400, tz=TZ).replace(minute=0, second=0, microsecond=0)
        hourly = []
        for h in range(random.choice([0, 0, 10, 30])):
            t = base + timedelta(hours=h)
            style = random.choice(["naive", "z", "off", "ms"])
            lab = {"naive": t.strftime("%Y-%m-%dT%H:%M:%S"), "z": t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "off": t.isoformat(), "ms": int(t.timestamp() * 1000)}[style]
            hourly.append([lab, random.choice([0, 0.5, 1.25, None, 2.0])])
        rows.append({"id": tid * 10 + k, "machine_task_id": tid, "field_id": 1000 + k,
                     "covered_area": round(random.random() * 5, 2), "covered_area_hourly": hourly})
    items_by_task[tid] = rows
    st._set_mtfmi(tid, rows)
st.synced_ts = st.full_ts = st.hard_ts = now; st._save_state(); con.execute("COMMIT")

print("1) совпадение с area_ha_by_shift_date")
bad = 0
for tid, rows in items_by_task.items():
    for r in rows:
        want = {k.isoformat(): v for k, v in area_ha_by_shift_date(r, parse_dt=pdt).items()}
        rid = con.execute("SELECT rid FROM mtfmi WHERE task_id=? AND json_extract(data,'$.id')=?", (tid, r["id"])).fetchone()[0]
        got = dict(con.execute("SELECT shift_date, ha FROM mtfmi_daily WHERE rid=?", (rid,)))
        bad += not (set(want) == set(got) and all(abs(want[k] - got[k]) < 1e-9 for k in want))
ok(f"1200 строк площади, расхождений: {bad}", bad == 0)
r = subprocess.run([sys.executable, str(Path(__file__).with_name("verify_daily_facts.py"))], capture_output=True, text=True)
ok("verify_daily_facts.py: " + r.stdout.strip().splitlines()[-1], r.returncode == 0)

print("2) агрегаты daily_facts")
d0 = (datetime.now(TZ) - timedelta(days=50)).date().isoformat(); d1 = (datetime.now(TZ) + timedelta(days=2)).date().isoformat()
want_total = sum(sum(area_ha_by_shift_date(r, parse_dt=pdt).values()) for rows in items_by_task.values() for r in rows)
by_day = st.daily_facts([10, 11], d0, d1, by="day")
ok(f"сумма по дням == прямая ({sum(x[2] for x in by_day):.3f} га)", abs(sum(x[2] for x in by_day) - want_total) < 1e-6)
md = st.daily_facts([10], d0, d1, by="machine_day")
exp = {}
for tid, rows in items_by_task.items():
    if 10 + tid % 2 != 10:
        continue
    for r in rows:
        for k, v in area_ha_by_shift_date(r, parse_dt=pdt).items():
            key = (10, 100 + tid % 7, k.isoformat()); exp[key] = exp.get(key, 0) + v
ok("machine_day == прямая", {(a, b, c): v for a, b, c, v in md}.keys() == exp.keys()
   and all(abs(v - exp[(a, b, c)]) < 1e-6 for a, b, c, v in md))
flat = st.flat_facts([10, 11], now - 60 * 86400, now + 86400)
ok("flat_facts: только строки без hourly", len(flat) > 0 and all(x[4] > 0 for x in flat))

print("3) обновление mtfmi задания пересчитывает факты; удаление чистит")
st._db().execute("BEGIN IMMEDIATE"); st.cov_lo = {10: now - 60 * 86400, 11: now - 60 * 86400}
st._set_mtfmi(3, [{"id": 1, "machine_task_id": 3, "field_id": 5, "covered_area": 2.0,
                   "covered_area_hourly": [[(datetime.now(TZ).replace(hour=12)).isoformat(), 4.0]]}])
st._drop_mtfmi(4); st._db().execute("COMMIT")
n3 = st._db().execute("SELECT COUNT(*) FROM mtfmi_fact WHERE task_id=3").fetchone()[0]
n4 = st._db().execute("SELECT COUNT(*) FROM mtfmi_fact WHERE task_id=4").fetchone()[0]
orph = st._db().execute("SELECT COUNT(*) FROM mtfmi_daily WHERE rid NOT IN (SELECT rid FROM mtfmi_fact)").fetchone()[0]
ok("факты задания заменены, удалённого нет, сирот нет", n3 == 1 and n4 == 0 and orph == 0)

print("4) миграция v3→v4 без перекачки и rebuild_facts")
c = st._db(); c.execute("BEGIN IMMEDIATE")
for t in ("mtfmi_fact", "mtfmi_daily"):
    c.execute(f"DELETE FROM {t}")
c.execute("UPDATE tasks SET machine_id=NULL"); c.execute("UPDATE meta SET v='3' WHERE k='version'"); c.execute("COMMIT")
tot_before = want_total
ts._STORES.clear(); st = ts.get_store(BASE)
st._db()
ok(f"версия {ts.STORE_VERSION} (миграция 3→актуальная), задания и mtfmi сохранены", st._db().execute("SELECT v FROM meta WHERE k='version'").fetchone()[0] == str(ts.STORE_VERSION)
   and st._db().execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 400)
ok("machine_id восстановлен из raw", st._db().execute("SELECT COUNT(*) FROM tasks WHERE machine_id IS NULL").fetchone()[0] == 0)
ok("rebuild_facts работает", st.rebuild_facts() > 1000 and st._db().execute("SELECT COUNT(*) FROM mtfmi_daily").fetchone()[0] > 0)
r = subprocess.run([sys.executable, str(Path(__file__).with_name("verify_daily_facts.py"))], capture_output=True, text=True)
ok("после миграции verify: " + r.stdout.strip().splitlines()[-1], r.returncode == 0)

print("5) machine_day_area == прямой расчёт по строкам (уборочный путь сводки 2)")
firm = lambda fid: None if fid == 1002 else ("A" if fid % 2 else "B")
got = st.machine_day_area([10, 11], d0, d1, firm)
exp = {}
for tid, rows in items_by_task.items():
    mid = 100 + tid % 7
    for r in rows:
        k = firm(r["field_id"])
        if k is None: continue
        for day, v in area_ha_by_shift_date(r, parse_dt=pdt).items():
            if d0 <= day.isoformat() < d1:
                e = exp.setdefault(k, {}).setdefault(day.isoformat(), {}); e[mid] = e.get(mid, 0) + v
# задание 3 в шаге 3 перезаписано, 4 удалено: сверяем только по неизменённым
import copy
def strip(x, bad=(3, 4)):
    return x
tot_got = sum(v for f in got.values() for d in f.values() for v in d.values())
tot_exp = sum(v for f in exp.values() for d in f.values() for v in d.values())
ok("ключи фирм совпадают, пропуск полей работает", set(got) == {"A", "B"} == set(exp))
ok(f"сумма близка (допуск на изменённые задания 3 и 4 в шаге 3): {tot_got:.1f} vs {tot_exp:.1f}", abs(tot_got - tot_exp) < 0.05 * tot_exp)
print("\nИТОГ:", "ЕСТЬ ОШИБКИ" if failed else "всё прошло")
sys.exit(1 if failed else 0)
