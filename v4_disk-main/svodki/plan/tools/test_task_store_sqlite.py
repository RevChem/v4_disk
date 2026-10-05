# -*- coding: utf-8 -*-
"""Проверки SQLite-специфики хранилища: поля, raw/rebuild, READONLY, параллельные процессы, память.
Запуск из svodki/plan: python tools/test_task_store_sqlite.py"""
from __future__ import annotations

import asyncio, json, multiprocessing as mp, os, resource, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import plans  # noqa: F401,E402

tmp = Path(tempfile.mkdtemp(prefix="store_sqlite_"))
os.environ["SVODKI_TASK_STORE_DIR"] = str(tmp)
import svodki_task_store as ts  # noqa: E402

BASE = "http://x/api/v3"
failed = False


def ok(name, cond):
    global failed
    print(("  OK   " if cond else "  FAIL ") + name)
    failed |= not cond


def seed(n=3000):
    st = ts.get_store(BASE)
    con = st._db()
    now = time.time()
    con.execute("BEGIN IMMEDIATE")
    st.cov_lo = {10: now - 40 * 86400, 11: now - 40 * 86400}
    for i in range(1, n + 1):
        t = {"id": i, "work_type_id": 10 + i % 2, "season": "2026",
             "end_time": ts.fmt_ts(now - (i % 30) * 86400), "updated_at": ts.fmt_ts(now),
             "x_custom_tonnazh": 7, "zzz_unused_blob": "q" * 2000, "covered_area": 1.5}
        st._put_task(t)
        st._set_mtfmi(i, [{"id": i * 10, "machine_task_id": i, "covered_area": 1.5, "zzz_unused_blob": "q" * 500}])
    st.synced_ts = now; st.full_ts = st.hard_ts = now
    st._save_state(); con.execute("COMMIT")
    return st


st = seed()
print("1) поля")
row = st._db().execute("SELECT data FROM tasks WHERE id=5").fetchone()[0]
d = json.loads(row)
if ts._KEEP is not None:
    ok("неиспользуемое поле не хранится", "zzz_unused_blob" not in d)
ok("x_custom_* и covered_area сохранены", d.get("x_custom_tonnazh") == 7 and d.get("covered_area") == 1.5)
import zlib
raw = json.loads(zlib.decompress(st._db().execute("SELECT raw FROM tasks WHERE id=5").fetchone()[0]))
ok("полный JSON лежит в raw", "zzz_unused_blob" in raw)
ok("rebuild_from_raw пересобирает без API", st.rebuild_from_raw() >= 3000)

print("2) READONLY: в API не ходим")
os.environ["SVODKI_STORE_READONLY"] = "1"
ts._STORES.clear()
st2 = ts.get_store(BASE)
calls = []


class Boom:
    def __getattr__(self, n):
        calls.append(n); raise AssertionError("обращение к API")


now = time.time()
p = {"end_time_gt_eq": ts.fmt_ts(now - 35 * 86400), "end_time_lt": ts.fmt_ts(now + 86400)}
rows = asyncio.run(st2.query_tasks(Boom(), asyncio.Semaphore(1), [10], p))
ok("задания отданы из базы, обращений к API нет", len(rows) == 1500 and not calls)
ok("порядок по id", [r["id"] for r in rows] == sorted(r["id"] for r in rows))
mt = asyncio.run(st2.get_mtfmi(Boom(), asyncio.Semaphore(1), BASE, [r["id"] for r in rows[:50]], None))
ok("mtfmi из базы", len(mt) == 50 and all(len(v) == 1 for v in mt.values()))
del os.environ["SVODKI_STORE_READONLY"]


def reader(q):
    os.environ["SVODKI_TASK_STORE_DIR"] = str(tmp); os.environ["SVODKI_STORE_READONLY"] = "1"
    import svodki_task_store as t
    s = t.get_store(BASE); n = 0
    for _ in range(5):
        n = len(asyncio.run(s.query_tasks(None, None, [10, 11], p)))
    q.put((n, resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024))


print("3) 6 параллельных читателей + писатель")
q = mp.Queue(); procs = [mp.Process(target=reader, args=(q,)) for _ in range(6)]
[x.start() for x in procs]
ts._STORES.clear(); w = ts.get_store(BASE)
async def write():
    async with w._xlock():
        for i in range(3001, 3301):
            w._put_task({"id": i, "work_type_id": 10, "season": "2026", "end_time": ts.fmt_ts(time.time() - 3600)})
asyncio.run(write())
res = [q.get(timeout=120) for _ in procs]; [x.join() for x in procs]
ok("читатели не упали и видят данные", all(n >= 3000 for n, _ in res))
print(f"     пик RSS читателя, МБ: {max(m for _, m in res):.0f} (3000 заданий)")

print("4) mtfmi: raw, rebuild, скорость get_mtfmi (нет квадратичного цикла)")
ts._STORES.clear(); st = ts.get_store(BASE)
rawm = json.loads(zlib.decompress(st._db().execute("SELECT raw FROM mtfmi WHERE task_id=5").fetchone()[0]))
ok("полный JSON строки площади лежит в raw", "zzz_unused_blob" in rawm)
ok("rebuild_from_raw пересобирает и mtfmi", st.rebuild_from_raw() >= 6000)
con = st._db(); con.execute("BEGIN IMMEDIATE")
for i in range(3301, 13301):
    st._put_task({"id": i, "work_type_id": 10, "season": "2026", "end_time": ts.fmt_ts(time.time() - 3600)})
con.execute("COMMIT")
fetched = []
async def fake_live(client, sem, base, ids, log_prefix=""):
    fetched.extend(ids); return {i: [{"id": i, "machine_task_id": i, "covered_area": 1.0}] for i in ids}
t0 = time.monotonic()
res = asyncio.run(st.get_mtfmi(None, None, BASE, list(range(1, 13301)), fake_live))
dt = time.monotonic() - t0
ok(f"13 300 заданий, из них 10 000 без mtfmi: {dt:.1f} с (< 10 с)", dt < 10 and len(res) == 13300)
ok("докачаны ровно недостающие", len(fetched) == 10300 or len(fetched) == 10000)

print("5) откат при ошибке")
ts._STORES.clear(); st = ts.get_store(BASE)
before = st._db().execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
async def boom():
    async with st._xlock():
        st._put_task({"id": 99999, "work_type_id": 10, "season": "2026", "end_time": ts.fmt_ts(time.time())})
        raise RuntimeError("обрыв API")
try:
    asyncio.run(boom())
except RuntimeError:
    pass
after = st._db().execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
ok("частичные изменения откачены", before == after)
async def again():
    async with st._xlock():
        pass
asyncio.run(again()); ok("после отката замок свободен", True)

print("6) fill_mtfmi (синхронизация перед пачкой)")
ts._STORES.clear(); st = ts.get_store(BASE)
import mtfmi_batch_fetch as mb
orig = mb.fetch_mtfmi_map_live
st._load_state()
con = st._db(); con.execute("BEGIN IMMEDIATE")
for i in range(20001, 20501):
    st._put_task({"id": i, "work_type_id": 11, "season": "2026", "end_time": ts.fmt_ts(time.time() - 7200)})
con.execute("COMMIT")
mb.fetch_mtfmi_map_live = fake_live
fetched.clear()
n = asyncio.run(st.fill_mtfmi(None, None, "t ", crops={}, remember=None))
mb.fetch_mtfmi_map_live = orig
left = st._db().execute("SELECT COUNT(*) FROM tasks WHERE id NOT IN (SELECT task_id FROM mtfmi_tasks)").fetchone()[0]
ok(f"догружено {n}, без mtfmi не осталось", left == 0 and n == 500)

print("\nИТОГ:", "ЕСТЬ ОШИБКИ" if failed else "всё прошло")
sys.exit(1 if failed else 0)
