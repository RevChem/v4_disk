# -*- coding: utf-8 -*-
"""Проверка инкрементального хранилища на мок-API (без сети). Запуск из svodki/plan."""
from __future__ import annotations

import asyncio, collections, os, random, shutil, sys, tempfile, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import plans  # noqa: F401,E402
import httpx  # noqa: E402

TZ = timezone(timedelta(hours=3))
ISO = "%Y-%m-%dT%H:%M:%S"
NOW = time.time()
tmp = Path(tempfile.mkdtemp(prefix="store_test_"))
os.environ["SVODKI_TASK_STORE_DIR"] = str(tmp)
os.environ["SVODKI_STORE_MAX_AGE_SEC"] = "0"
os.environ["SVODKI_STORE_RECENT_DAYS"] = "3"

import svodki_task_store as ts  # noqa: E402
from mtfmi_batch_fetch import fetch_mtfmi_map_for_tasks  # noqa: E402
from machine_tasks_batch_fetch import fetch_machine_tasks_by_wt_async  # noqa: E402
from live_machine_tasks_fetch import fetch_machine_tasks_windowed_async  # noqa: E402
from async_http import fetch_pages  # noqa: E402

BASE = "http://x/api/v3"
random.seed(11)
WTS = [10, 11, 12, 13, 14]
DB_T: dict[int, dict] = {}
DB_M: dict[int, dict] = {}
DB_V: list[dict] = []          # versions (журнал удалений)
_next = {"t": 1, "m": 1, "v": 1}


def iso(t): return datetime.fromtimestamp(t, tz=TZ).strftime("%Y-%m-%dT%H:%M:%S+03:00")


def add_task(wt, end_ts, rows=2):
    tid = _next["t"]; _next["t"] += 1
    DB_T[tid] = {"id": tid, "work_type_id": wt, "end_time": iso(end_ts),
                 "season": str(datetime.fromtimestamp(end_ts, tz=TZ).year), "updated_at": iso(NOW - 99 * 86400)}
    for _ in range(rows): add_row(tid, old=True)
    return tid


def add_row(tid, old=False):
    mid = _next["m"]; _next["m"] += 1
    DB_M[mid] = {"id": mid, "machine_task_id": tid, "covered_area": round(random.random() * 10, 2),
                 "updated_at": iso(NOW - 99 * 86400 if old else time.time())}
    return mid


def touch_t(tid, **kw):
    DB_T[tid].update(kw); DB_T[tid]["updated_at"] = iso(time.time())


def destroy_task(tid):
    DB_T.pop(tid, None)
    for m in [m for m, v in DB_M.items() if v["machine_task_id"] == tid]: DB_M.pop(m)
    DB_V.append({"id": _next["v"], "item_type": "MachineTask", "item_id": tid, "event": "destroy", "created_at": iso(time.time())})
    _next["v"] += 1


for _ in range(int(os.environ.get("N_T","1500"))): add_task(random.choice(WTS + [99]), NOW - random.uniform(0, 34) * 86400)
for _ in range(60): add_task(random.choice(WTS), NOW - random.uniform(0, 2) * 86400)

calls = collections.Counter()


def _page(rows, q):
    fid = int(q.get("from_id", 0)); lim = int(q.get("limit", 100))
    rows = sorted([r for r in rows if r["id"] >= fid], key=lambda r: r["id"]); pg = rows[:lim]
    return httpx.Response(200, json={"data": pg, "meta": {"response": {"last_record_id": pg[-1]["id"] if pg else None,
            "total_count": len(rows), "has_more": len(rows) > lim, "per_page": lim}}})


def _naive(s): return datetime.strptime(s[:19], ISO).replace(tzinfo=TZ).timestamp()


def handler(req):
    q = req.url.params; path = req.url.path.rsplit("/", 1)[-1]
    if req.url.path.endswith("/machine_tasks/ids"):
        calls["ids"] += 1
        rows = list(DB_T.values())
        if "end_time_gt_eq" in q: rows = [r for r in rows if ts.parse_ts(r["end_time"]) >= _naive(q["end_time_gt_eq"])]
        data = sorted(r["id"] for r in rows)
        if os.environ.get("MOCK_IDS_TRUNCATE"): data = data[: len(data) // 2]
        return httpx.Response(200, json={"data": data, "meta": {"response": {"obtained_records": len(data)}}})
    calls[path + ("+delta" if "updated_at_gt_eq" in q else "")] += 1
    if path == "machine_tasks":
        rows = list(DB_T.values())
        if "work_type_id" in q:
            w = {int(x) for x in q["work_type_id"].split(",")}; rows = [r for r in rows if r["work_type_id"] in w]
        if "end_time_gt_eq" in q: rows = [r for r in rows if ts.parse_ts(r["end_time"]) >= _naive(q["end_time_gt_eq"])]
        if "end_time_lt" in q: rows = [r for r in rows if ts.parse_ts(r["end_time"]) < _naive(q["end_time_lt"])]
        if "season" in q: rows = [r for r in rows if r["season"] == q["season"]]
        if "updated_at_gt_eq" in q: rows = [r for r in rows if ts.parse_ts(r["updated_at"]) >= _naive(q["updated_at_gt_eq"])]
        return _page(rows, q)
    if path == "machine_task_field_mapping_items":
        rows = list(DB_M.values())
        if "machine_task_id" in q:
            ids = {int(x) for x in q["machine_task_id"].split(",")}; rows = [r for r in rows if r["machine_task_id"] in ids]
        if "updated_at_gt_eq" in q: rows = [r for r in rows if ts.parse_ts(r["updated_at"]) >= _naive(q["updated_at_gt_eq"])]
        return _page(rows, q)
    if path == "versions":
        rows = [v for v in DB_V if v["item_type"] == q.get("item_type", v["item_type"]) and v["event"] == q.get("event", v["event"])]
        if "created_at_gt_eq" in q: rows = [r for r in rows if ts.parse_ts(r["created_at"]) >= _naive(q["created_at_gt_eq"])]
        return _page(rows, q)
    return httpx.Response(200, json={"data": [], "meta": {"response": {}}})


def truth_tasks(wts, gt, lt, season=None):
    a, b = _naive(gt), _naive(lt)
    return sorted(t["id"] for t in DB_T.values() if t["work_type_id"] in wts and a <= ts.parse_ts(t["end_time"]) < b and (season is None or t["season"] == season))


def truth_mtfmi(ids):
    out = collections.defaultdict(list)
    for m in DB_M.values():
        if m["machine_task_id"] in ids: out[m["machine_task_id"]].append(m["id"])
    return {k: sorted(v) for k, v in out.items()}


def new_process():
    ts._STORES.clear()
    import mtfmi_batch_fetch as mb; mb._HISTORY_CROP.clear()


async def run(wts, gt, lt, season=None):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        sem = asyncio.Semaphore(20)
        p = {"end_time_gt_eq": gt, "end_time_lt": lt, **({"season": season} if season else {})}
        rows = await fetch_machine_tasks_by_wt_async(fetch_pages, c, BASE, wts, p, sem)
        rows2 = await fetch_machine_tasks_windowed_async(c, BASE, wts, p, sem)
        mt = await fetch_mtfmi_map_for_tasks(c, sem, BASE, [r["id"] for r in rows])
        return rows, rows2, mt


def ok(name, cond):
    print(("  OK   " if cond else "  FAIL ") + name)
    if not cond: ok.failed = True
ok.failed = False


def mt_ok(mt, ids):
    return {k: sorted(r["id"] for r in v) for k, v in mt.items()} == truth_mtfmi(set(ids))


lo = datetime.fromtimestamp(NOW - 36 * 86400, tz=TZ).strftime(ISO)
hi = datetime.fromtimestamp(NOW + 86400, tz=TZ).strftime(ISO)
W = [10, 11, 12]

print("1) холодный старт")
calls.clear(); rows, rows2, mt = asyncio.run(run(W, lo, hi))
ok("задания == эталон", sorted(r["id"] for r in rows) == truth_tasks(set(W), lo, hi))
ok("windowed-вход == эталон", sorted(r["id"] for r in rows2) == truth_tasks(set(W), lo, hi))
ok("mtfmi == эталон", mt_ok(mt, [r["id"] for r in rows]))
cold = dict(calls); print("     запросов:", sum(cold.values()), cold)

print("2) тёплый запуск без изменений (новый процесс)")
new_process(); calls.clear(); rows, rows2, mt = asyncio.run(run(W, lo, hi))
ok("задания == эталон", sorted(r["id"] for r in rows) == truth_tasks(set(W), lo, hi))
ok("mtfmi == эталон", mt_ok(mt, [r["id"] for r in rows]))
warm = dict(calls); n_warm = sum(warm.values()); print("     запросов:", n_warm, warm)
ok("выгрузка сезона не повторяется (заданий и mtfmi — меньше запросов, чем в холодном)",
   warm.get("machine_tasks", 0) < cold["machine_tasks"] and warm.get("machine_task_field_mapping_items", 0) < cold["machine_task_field_mapping_items"])
ok("mtfmi сезона не качается заново (только недавние сутки)", warm.get("machine_task_field_mapping_items", 0) <= 4)

print("3) правки между запусками")
old_ids = [t for t, v in DB_T.items() if v["work_type_id"] in W and ts.parse_ts(v["end_time"]) < NOW - 10 * 86400]
recent_ids = [t for t, v in DB_T.items() if v["work_type_id"] in W and ts.parse_ts(v["end_time"]) > NOW - 2 * 86400]
a, b, c_, d, e = old_ids[:5]
touch_t(a, end_time=iso(NOW - 12 * 86400))
touch_t(b, work_type_id=99)
touch_t(c_, work_type_id=11)
other = [t for t, v in DB_T.items() if v["work_type_id"] == 99 and ts.parse_ts(v["end_time"]) > NOW - 20 * 86400][0]
touch_t(other, work_type_id=10)
new_t = add_task(10, NOW - 3600, rows=3)
destroy_task(recent_ids[0])
mid = next(m for m, v in DB_M.items() if v["machine_task_id"] == d)
DB_M[mid]["covered_area"] = 777.0; DB_M[mid]["updated_at"] = iso(time.time())
add_row(e)
stale_old = old_ids[10]; destroy_task(stale_old)       # удалено СТАРОЕ — узнаём из versions

new_process(); calls.clear(); rows, rows2, mt = asyncio.run(run(W, lo, hi))
got = sorted(r["id"] for r in rows); want = truth_tasks(set(W), lo, hi)
ok("правка/перенос/добавление/удаления учтены (== эталон)", got == want)
ok("старое удалённое задание исчезло (через versions)", stale_old not in got)
ok("mtfmi == эталон, правка и новая строка на месте", mt_ok(mt, got) and any(r["covered_area"] == 777.0 for r in mt[d]))
ok("новое задание и его mtfmi на месте", new_t in got and len(mt.get(new_t, [])) == 3)
print("     запросов:", sum(calls.values()), dict(calls))

print("4) TTL: сверка по /ids вместо пересбора")
os.environ["SVODKI_STORE_FULL_REFRESH_HOURS"] = "0.0000001"
time.sleep(0.05); new_process(); calls.clear(); rows, rows2, mt = asyncio.run(run(W, lo, hi))
ok("задания == эталон", sorted(r["id"] for r in rows) == truth_tasks(set(W), lo, hi))
ok("mtfmi == эталон", mt_ok(mt, [r["id"] for r in rows]))

print("4b) сверка по /ids: тихо удалённое задание (в versions записи нет), без перекачки сезона")
silent = [t for t, v in DB_T.items() if v["work_type_id"] in W and ts.parse_ts(v["end_time"]) < NOW - 15 * 86400][3]
DB_T.pop(silent)
for m in [m for m, v in DB_M.items() if v["machine_task_id"] == silent]: DB_M.pop(m)
os.environ["SVODKI_STORE_FULL_REFRESH_HOURS"] = "0.0000001"
time.sleep(0.05); new_process(); calls.clear(); rows, rows2, mt = asyncio.run(run(W, lo, hi))
ok("задания == эталон (тихое удаление убрано)", sorted(r["id"] for r in rows) == truth_tasks(set(W), lo, hi) and silent not in {r["id"] for r in rows})
ok("mtfmi == эталон", mt_ok(mt, [r["id"] for r in rows]))
ok("использован /ids, а не пересбор сезона", calls.get("ids", 0) >= 1 and calls.get("machine_task_field_mapping_items", 0) <= 4)
print("     запросов:", sum(calls.values()), dict(calls))

print("4c) обрезанный ответ /ids → защита, пересбор")
os.environ["MOCK_IDS_TRUNCATE"] = "1"
time.sleep(0.05); new_process(); calls.clear(); rows, rows2, mt = asyncio.run(run(W, lo, hi))
ok("задания == эталон", sorted(r["id"] for r in rows) == truth_tasks(set(W), lo, hi))
ok("mtfmi == эталон", mt_ok(mt, [r["id"] for r in rows]))
ok("сработал пересбор (сезон перекачан)", calls.get("machine_task_field_mapping_items", 0) >= 3)
os.environ.pop("MOCK_IDS_TRUNCATE")

print("4d) жёсткий пересбор по HARD_REFRESH")
os.environ["SVODKI_STORE_HARD_REFRESH_HOURS"] = "0.0000001"
time.sleep(0.05); new_process(); calls.clear(); rows, rows2, mt = asyncio.run(run(W, lo, hi))
ok("задания == эталон", sorted(r["id"] for r in rows) == truth_tasks(set(W), lo, hi))
ok("пересбор без обращения к /ids", calls.get("ids", 0) == 0)
os.environ["SVODKI_STORE_HARD_REFRESH_HOURS"] = "168"
os.environ["SVODKI_STORE_FULL_REFRESH_HOURS"] = "24"

print("5) расширение покрытия: новые wt и более ранняя дата")
lo2 = datetime.fromtimestamp(NOW - 50 * 86400, tz=TZ).strftime(ISO)
new_process(); calls.clear(); rows, rows2, mt = asyncio.run(run([10, 13, 14], lo2, hi))
ok("задания == эталон", sorted(r["id"] for r in rows) == truth_tasks({10, 13, 14}, lo2, hi))
ok("mtfmi == эталон", mt_ok(mt, [r["id"] for r in rows]))
new_process(); rows, rows2, mt = asyncio.run(run(W, lo, hi))
ok("после расширения прежний набор тоже верен", sorted(r["id"] for r in rows) == truth_tasks(set(W), lo, hi))

print("6) season и запросы вне шаблона")
sy = str(datetime.fromtimestamp(NOW, tz=TZ).year)
new_process(); rows, rows2, mt = asyncio.run(run(W, lo, hi, season=sy))
ok("season == эталон", sorted(r["id"] for r in rows) == truth_tasks(set(W), lo, hi, sy))
ok("eligible: status/start_time не в хранилище", not ts.eligible({"end_time_gt_eq": lo, "end_time_lt": hi, "status": "x"}) and not ts.eligible({"start_time_gt_eq": lo}))

print("7) выключено без переменной окружения")
os.environ.pop("SVODKI_TASK_STORE_DIR"); ts._STORES.clear()
calls.clear(); rows, rows2, mt = asyncio.run(run(W, lo, hi))
ok("прежний live-путь, данные те же", sorted(r["id"] for r in rows) == truth_tasks(set(W), lo, hi) and calls.get("machine_tasks+delta", 0) == 0)

shutil.rmtree(tmp, ignore_errors=True)
print("\nИТОГ:", "ЕСТЬ ОШИБКИ" if ok.failed else "всё прошло")
sys.exit(1 if ok.failed else 0)
