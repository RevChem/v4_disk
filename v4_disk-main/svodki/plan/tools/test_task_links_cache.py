# -*- coding: utf-8 -*-
"""Версии заданий, связи агроопераций (mtaomi) и кэш вкладов в sqlite-хранилище — на мок-API, без сети.
Запуск из svodki/plan: python tools/test_task_links_cache.py"""
from __future__ import annotations

import asyncio, collections, json, os, sys, tempfile, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import plans  # noqa: F401,E402
import httpx  # noqa: E402

TZ = timezone(timedelta(hours=3))
NOW = time.time()
tmp = Path(tempfile.mkdtemp(prefix="links_cache_"))
os.environ["SVODKI_TASK_STORE_DIR"] = str(tmp)
os.environ["SVODKI_STORE_MAX_AGE_SEC"] = "0"
os.environ["SVODKI_LINKS_RECENT_DAYS"] = "0"      # чтобы видеть точечное перечитывание
os.environ["SVODKI_LINKS_MARGIN_SEC"] = "60"
import svodki_task_store as ts  # noqa: E402
from svodki_calc_cache import calc_key, fingerprint  # noqa: E402

BASE = "http://x/api/v3"
failed = False


def ok(name, cond):
    global failed
    print(("  OK   " if cond else "  FAIL ") + name)
    failed |= not cond


def iso(t):
    return datetime.fromtimestamp(t, tz=TZ).strftime("%Y-%m-%dT%H:%M:%S+03:00")


def naive(s):
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=TZ).timestamp()


# ------------------------------------------------------------------ мок-API
LINKS: dict[int, dict] = {}   # id -> {id, machine_task_id, agro_operation_id, updated_at}
OPS: dict[int, dict] = {}
VERS: list[dict] = []
calls = collections.Counter()
OLD = iso(NOW - 99 * 86400)
for t in range(1, 41):               # 40 заданий, у каждого по 2 связи
    for k in range(2):
        lid = t * 10 + k
        LINKS[lid] = {"id": lid, "machine_task_id": t, "agro_operation_id": 1000 + t * 10 + k, "updated_at": OLD}
        OPS[1000 + t * 10 + k] = {"id": 1000 + t * 10 + k, "history_item_id": 5000 + t, "field_id": 700 + t, "updated_at": OLD}


def page(rows, q):
    fid, lim = int(q.get("from_id", 0)), int(q.get("limit", 100))
    rows = sorted((r for r in rows if r["id"] >= fid), key=lambda r: r["id"])
    pg = rows[:lim]
    return httpx.Response(200, json={"data": pg, "meta": {"response": {
        "last_record_id": pg[-1]["id"] if pg else None, "obtained_records": len(pg)}}})


def handler(req):
    q, path = req.url.params, req.url.path
    if path.endswith("/changes") or path.endswith("/changes_ids"):
        calls["changes"] += 1
        return httpx.Response(404, json={})
    if path.endswith("/agro_operations/mass_request"):
        calls["ops_mass"] += 1
        ids = json.loads(req.content)["data"]
        return httpx.Response(200, json={"data": [OPS[i] for i in ids if i in OPS]})
    name = path.rsplit("/", 1)[-1]
    if name == "machine_task_agro_operation_mapping_items":
        calls["links"] += 1
        rows = list(LINKS.values())
        if "machine_task_id" in q:
            want = {int(x) for x in q["machine_task_id"].split(",")}
            rows = [r for r in rows if r["machine_task_id"] in want]
            calls["links_tasks"] += len(want)
        if "updated_at_gt_eq" in q:
            calls["links_delta"] += 1
            rows = [r for r in rows if naive(r["updated_at"]) >= naive(q["updated_at_gt_eq"])]
        return page(rows, q)
    if name == "agro_operations":
        calls["ops"] += 1
        rows = list(OPS.values())
        if "updated_at_gt_eq" in q:
            rows = [r for r in rows if naive(r["updated_at"]) >= naive(q["updated_at_gt_eq"])]
        return page(rows, q)
    if name == "versions":
        rows = [v for v in VERS if v["item_type"] == q.get("item_type", v["item_type"])]
        return page(rows, q)
    return page([], q)  # machine_tasks / mtfmi: пусто (задания кладём в базу напрямую)


def new_store():
    ts._STORES.clear()
    return ts.get_store(BASE)


def seed(st, n=40):
    con = st._db()
    con.execute("BEGIN IMMEDIATE")
    st.cov_lo = {10: NOW - 40 * 86400}
    for i in range(1, n + 1):
        st._put_task({"id": i, "work_type_id": 10, "season": "2026", "end_time": ts.fmt_ts(NOW - i * 3600),
                      "updated_at": ts.fmt_ts(NOW - 90 * 86400)})
        st._set_mtfmi(i, [{"id": i * 100, "machine_task_id": i, "field_id": 700 + i, "covered_area": 2.0}])
    st.synced_ts = NOW
    st.full_ts = st.hard_ts = NOW
    st._save_state()
    con.execute("COMMIT")


def run(coro):
    return asyncio.run(coro)


async def with_client(fn):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        return await fn(c, asyncio.Semaphore(8))


def rev(st, tid):
    r = st._db().execute("SELECT rev FROM task_rev WHERE task_id=?", (tid,)).fetchone()
    return r[0] if r else 0


st = new_store()
seed(st)

print("1) версии заданий")
r5 = rev(st, 5)
ok("у каждого задания есть версия", r5 > 0 and rev(st, 40) > 0)
con = st._db()
con.execute("BEGIN IMMEDIATE")
st._put_task({"id": 5, "work_type_id": 10, "season": "2026", "end_time": ts.fmt_ts(NOW - 5 * 3600),
              "updated_at": ts.fmt_ts(NOW - 90 * 86400)})
st._set_mtfmi(5, [{"id": 500, "machine_task_id": 5, "field_id": 705, "covered_area": 2.0}])
con.execute("COMMIT")
ok("то же задание и те же строки → версия не меняется", rev(st, 5) == r5)
con.execute("BEGIN IMMEDIATE")
st._set_mtfmi(5, [{"id": 500, "machine_task_id": 5, "field_id": 705, "covered_area": 3.5}])
con.execute("COMMIT")
r5b = rev(st, 5)
ok("изменилась площадь строки → версия выросла", r5b > r5)
ok("факты пересчитаны вместе с версией",
   st._db().execute("SELECT covered_area FROM mtfmi_fact WHERE task_id=5").fetchone()[0] == 3.5)
con.execute("BEGIN IMMEDIATE")
st._put_task({"id": 5, "work_type_id": 10, "season": "2026", "end_time": ts.fmt_ts(NOW - 5 * 3600),
              "updated_at": ts.fmt_ts(NOW), "machine_id": 77})
con.execute("COMMIT")
ok("изменилось само задание → версия выросла", rev(st, 5) > r5b)
ok("другие задания не затронуты", rev(st, 6) == rev(st, 6) and rev(st, 6) < rev(st, 5))
st2 = new_store()
st2._load_state()
ok("счётчик версий переживает перезапуск", st2._load_rev() >= rev(st, 5))

print("2) связи агроопераций: первая загрузка и повторное чтение из базы")
ids = list(range(1, 41))
calls.clear()
t2o, o2h, o2f = run(with_client(lambda c, s: st.get_task_agro_ops_raw(c, s, ids, "t ")))
ok("связи всех заданий загружены", len(t2o) == 40 and all(len(v) == 2 for v in t2o.values()))
ok("привязки агроопераций загружены", len(o2h) == 80 and len(o2f) == 80 and o2h[1011] == 5001 and o2f[1011] == 701)
ok("результат равен прямой выгрузке (формат _fetch_task_agro_ops_raw)",
   t2o[7] == {1070, 1071} and isinstance(t2o, dict))
n_api = calls["links"] + calls["ops_mass"]
calls.clear()
t2o_b, o2h_b, o2f_b = run(with_client(lambda c, s: st.get_task_agro_ops_raw(c, s, ids, "t ")))
ok("повторный вызов: ни одного обращения к API", sum(calls.values()) == 0 and n_api > 0)
ok("те же данные", (dict(t2o_b), o2h_b, o2f_b) == (dict(t2o), o2h, o2f))
rv = {i: rev(st, i) for i in ids}

print("3) синхронизация связей: ничего не менялось")
calls.clear()
stt = run(with_client(lambda c, s: st.sync_links(c, s, "t ")))
ok("перечитано 0 заданий", stt["refetched"] == 0 and calls["links_tasks"] == 0)
ok("версии не изменились", all(rev(st, i) == rv[i] for i in ids))

print("4) изменилась одна связь → перечитывается только её задание")
time.sleep(1.1)
LINKS[120]["agro_operation_id"] = 1999; LINKS[120]["updated_at"] = iso(time.time())
OPS[1999] = {"id": 1999, "history_item_id": 5999, "field_id": 799, "updated_at": iso(time.time())}
calls.clear()
stt = run(with_client(lambda c, s: st.sync_links(c, s, "t ")))
ok("перечитано ровно 1 задание", stt["refetched"] == 1 and calls["links_tasks"] == 1)
t2o_c, o2h_c, _ = run(with_client(lambda c, s: st.get_task_agro_ops_raw(c, s, [12], "t ")))
ok("новая связь и новая агрооперация видны", t2o_c[12] == {1999, 1121} and o2h_c.get(1999) == 5999)
ok("версия изменилась только у задания 12",
   rev(st, 12) > rv[12] and all(rev(st, i) == rv[i] for i in ids if i != 12))

print("5) изменилась агрооперация (привязка к культуре) → версии ссылающихся заданий растут")
time.sleep(1.1)
rv = {i: rev(st, i) for i in ids}
OPS[1070]["history_item_id"] = 6000; OPS[1070]["updated_at"] = iso(time.time())
stt = run(with_client(lambda c, s: st.sync_links(c, s, "t ")))
ok("обновлена 1 агрооперация", stt["ops_changed"] == 1)
ok("версия выросла только у задания 7", rev(st, 7) > rv[7] and all(rev(st, i) == rv[i] for i in ids if i != 7))
_, o2h_d, _ = run(with_client(lambda c, s: st.get_task_agro_ops_raw(c, s, [7], "t ")))
ok("новая привязка читается", o2h_d[1070] == 6000)

print("6) связь удалена (журнал /versions)")
time.sleep(1.1)
rv = {i: rev(st, i) for i in ids}
LINKS.pop(150)
VERS.append({"id": 1, "item_type": "MachineTaskAgroOperationMappingItem", "event": "destroy",
             "created_at": iso(time.time()), "item_id": 150,
             "snapshot_before_change": json.dumps({"machine_task_id": 15})})
stt = run(with_client(lambda c, s: st.sync_links(c, s, "t ")))
t2o_e, _, _ = run(with_client(lambda c, s: st.get_task_agro_ops_raw(c, s, [15], "t ")))
ok("удалённая связь исчезла у задания 15", stt["refetched"] >= 1 and t2o_e[15] == {1151} and rev(st, 15) > rv[15])

print("7) правка задания (дельта заданий) помечает его связи на перечитывание")
st._db().execute("BEGIN IMMEDIATE")
st._mark_links_dirty([20])
st._db().execute("COMMIT")
LINKS[200]["agro_operation_id"] = 1208  # без updated_at: ловится только через links_dirty
OPS[1208] = {"id": 1208, "history_item_id": 5020, "field_id": 720, "updated_at": OLD}
calls.clear()
stt = run(with_client(lambda c, s: st.sync_links(c, s, "t ")))
t2o_f, _, _ = run(with_client(lambda c, s: st.get_task_agro_ops_raw(c, s, [20], "t ")))
ok("задание 20 перечитано и новая связь видна", t2o_f[20] == {1208, 1201} and stt["refetched"] >= 1)

print("8) полная сверка раз в N часов")
os.environ["SVODKI_LINKS_HARD_REFRESH_HOURS"] = "1"
st._db().execute("UPDATE meta SET v=? WHERE k='links_hard'", (repr(time.time() - 7200),))
LINKS[300]["agro_operation_id"] = 1301  # тихая правка без updated_at
stt = run(with_client(lambda c, s: st.sync_links(c, s, "t ")))
t2o_g, _, _ = run(with_client(lambda c, s: st.get_task_agro_ops_raw(c, s, [30], "t ")))
ok("полная сверка перечитала все задания и поймала тихую правку",
   stt["hard"] and stt["refetched"] == 40 and t2o_g[30] == {1301})
os.environ.pop("SVODKI_LINKS_HARD_REFRESH_HOURS")

print("9) задание удалено из хранилища → связи убираются")
st._db().execute("BEGIN IMMEDIATE")
st._remove_task(40)
st._db().execute("COMMIT")
run(with_client(lambda c, s: st.sync_links(c, s, "t ")))
ok("связей удалённого задания нет", st._db().execute("SELECT COUNT(*) FROM mtaomi WHERE task_id=40").fetchone()[0] == 0)

print("10) новые задания уже отслеживаемого вида работ подтягиваются при синхронизации")
st._db().execute("BEGIN IMMEDIATE")
st._put_task({"id": 41, "work_type_id": 10, "season": "2026", "end_time": ts.fmt_ts(NOW - 600), "updated_at": ts.fmt_ts(NOW)})
st._db().execute("COMMIT")
LINKS[410] = {"id": 410, "machine_task_id": 41, "agro_operation_id": 1410, "updated_at": iso(time.time())}
OPS[1410] = {"id": 1410, "history_item_id": 5041, "field_id": 741, "updated_at": OLD}
stt = run(with_client(lambda c, s: st.sync_links(c, s, "t ")))
ok("связи нового задания загружены синхронизацией", stt["new"] == 1 and 41 in st._has_links([41]))

print("11) кэш вкладов")
st = new_store()
ck = calc_key("report2", "group", {"p": 1}, fingerprint({1: "a", 2: "b"}))
ok("отпечаток справочника устойчив к порядку", fingerprint({1: "a", 2: "b"}) == fingerprint({2: "b", 1: "a"})
   and fingerprint({1: "a"}) != fingerprint({1: "b"}))
snap = st.calc_snapshot()
items = [(i, snap.get(i, 0), f"s{i}", {"area": i * 1.5, "rows": [[1, "x"]]}) for i in range(1, 11)]
ok("запись вкладов", st.calc_put(ck, items, label="t") == 10)
got = st.calc_get(ck, range(1, 11), {i: f"s{i}" for i in range(1, 11)})
ok("чтение вкладов", len(got) == 10 and got[3]["area"] == 4.5)
got = st.calc_get(ck, range(1, 11), {i: ("zz" if i == 4 else f"s{i}") for i in range(1, 11)})
ok("другой stamp → вклад задания не считается верным", 4 not in got and len(got) == 9)
st._db().execute("BEGIN IMMEDIATE"); st._set_mtfmi(6, [{"id": 600, "machine_task_id": 6, "field_id": 706, "covered_area": 9.9}])
st._db().execute("COMMIT")
got = st.calc_get(ck, range(1, 11), {i: f"s{i}" for i in range(1, 11)})
ok("изменилось задание 6 → его вклад невалиден, остальные целы", 6 not in got and len(got) == 9)
stale = [(6, snap.get(6, 0), "s6", {"area": 0})]
ok("вклад, посчитанный по старым данным, не записывается", st.calc_put(ck, stale) == 0 and 6 not in st.calc_get(ck, [6]))
snap2 = st.calc_snapshot()
ok("свежий вклад записывается", st.calc_put(ck, [(6, snap2[6], "s6", {"area": 14.85})]) == 1
   and st.calc_get(ck, [6], {6: "s6"})[6]["area"] == 14.85)
ok("другой ключ расчёта не видит чужих вкладов", st.calc_get(calc_key("other"), range(1, 11)) == {})
st._db().execute("BEGIN IMMEDIATE"); st._remove_task(2); st._db().execute("COMMIT")
ok("удалённое задание теряет вклад", 2 not in st.calc_get(ck, [2]))
st._db().execute("UPDATE calc_keys SET used=?", (time.time() - 30 * 86400,))
ok("gc убирает вклады давно не использованных ключей", st.calc_gc(14) > 0
   and st._db().execute("SELECT COUNT(*) FROM calc_cache").fetchone()[0] == 0)

print("12) запись кэша при занятой базе не ломает расчёт")
st = new_store()
snap = st.calc_snapshot()
con = st._db()
other = ts.TaskStore(tmp, BASE)
oc = other._db()
oc.execute("BEGIN IMMEDIATE")
con.execute("PRAGMA busy_timeout=100")
t0 = time.monotonic()
res = st.calc_put(ck, [(1, snap.get(1, 0), "s1", {"a": 1})])
oc.execute("ROLLBACK")
ok(f"при занятой записи кэш молча пропускается ({time.monotonic() - t0:.1f} с)", res == 0)

print("13) миграция v4 → v5 не стирает данные")
st = new_store()
n_before = st._db().execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
st._db().execute("UPDATE meta SET v='4' WHERE k='version'")
st = new_store()
ok("версия обновлена, задания на месте",
   st._db().execute("SELECT v FROM meta WHERE k='version'").fetchone()[0] == str(ts.STORE_VERSION)
   and st._db().execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == n_before)

print("\nИТОГ:", "ЕСТЬ ОШИБКИ" if failed else "всё прошло")
sys.exit(1 if failed else 0)
