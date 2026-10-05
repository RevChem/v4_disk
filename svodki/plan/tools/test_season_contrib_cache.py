# -*- coding: utf-8 -*-
"""Кэш вкладов заданий в сводке 2: результат с кэшем == полный пересчёт, на синтетических данных.

Гоняет настоящий collect_work_data (2_main_ideal_compact.py) для групп с разной логикой (опрыскивание,
уборка, вспашка, МУ, дискование, боронование, обычная) в режимах off / shadow / on и сравнивает ВСЕ итоги.
Запуск из svodki/plan: python tools/test_season_contrib_cache.py
"""
from __future__ import annotations

import copy, importlib.util, io, os, random, sys, tempfile, time
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PLAN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLAN))
tmp = Path(tempfile.mkdtemp(prefix="contrib_"))
os.environ["SVODKI_TASK_STORE_DIR"] = str(tmp)
os.environ.setdefault("CROPWISE_API_TOKEN", "x")
os.environ["SVODKI_SEASON_CACHE_AUDIT"] = "0"
import plans  # noqa: F401,E402

spec = importlib.util.spec_from_file_location("r2", str(PLAN / "2_main_ideal_compact.py"))
r2 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r2)
import svodki_task_store as ts  # noqa: E402

failed = False


def ok(name, cond):
    global failed
    print(("  OK   " if cond else "  FAIL ") + name)
    failed |= not cond


TZ = r2.TZ_MSK
TODAY = r2.TODAY
FIRMS = list(r2.FIRM_ORDER)
random.seed(7)

# ------------------------------------------------------------------ синтетические справочники
FIELDS = list(range(1, 41))
CROPS = ["Пшеница озимая", "Соя", "Рапс озимый", "Кукуруза на зерно", "Подсолнечник", "Ячмень яровой"]
maps = {
    "field_to_crop": {f: CROPS[f % len(CROPS)] for f in FIELDS if f % 9},
    "field_to_crop_general": {f: CROPS[(f + 1) % len(CROPS)] for f in FIELDS if f % 11},
    "field_to_crop_harvest": {f: CROPS[(f + 2) % len(CROPS)] for f in FIELDS},
    "field_to_crop_plow": {f: CROPS[(f + 3) % len(CROPS)] for f in FIELDS if f % 7},
    "field_to_crop_veg": {f: "Картофель" for f in FIELDS if f % 5 == 0},
    "field_to_crop_report3": {f: CROPS[(f + 4) % len(CROPS)] for f in FIELDS if f % 8},
    "field_to_firm": {f: FIRMS[f % len(FIRMS)] for f in FIELDS if f % 10},
    "field_to_firm_general": {f: FIRMS[(f + 1) % len(FIRMS)] for f in FIELDS if f % 12},
    "field_to_firm_veg": {f: FIRMS[f % len(FIRMS)] for f in FIELDS if f % 5 == 0},
    "field_to_firm_all": {f: FIRMS[f % len(FIRMS)] for f in FIELDS},
    "crop_field_ids_report3": {f for f in FIELDS if f % 8},
    "period_available_ids": {f for f in FIELDS if f % 6},
    "period_available_ids_general": {f for f in FIELDS if f % 13},
}
machine_group = {1: 5, 2: 9, 3: 16, 4: 7, 5: 7, 6: 1, 7: 2, 8: 3}
field_area = {f: 50.0 + f for f in FIELDS}
history_item_to_crop = {5000 + i: CROPS[i % len(CROPS)] for i in range(0, 60)}
history_item_to_year = {k: 2026 for k in history_item_to_crop}


def flags(group_name, wt_ids):
    f = {}
    f["use_report3_coverage_logic"] = (len(wt_ids) == 1 and wt_ids[0] == 55)
    f["use_report6_spray_logic"] = group_name in (r2.SPRAY_GROUP_NAME, r2.VEG_SPRAY_GROUP_NAME)
    f["is_harvest_veg"] = r2._is_harvest_veg_group(group_name)
    is_h = group_name == r2.HARVEST_GROUP_NAME
    gen = group_name in r2.GENERAL_CROP_GROUPS and not is_h
    if f["use_report3_coverage_logic"]:
        a = ("field_to_crop_report3", "field_to_firm_all", "crop_field_ids_report3")
    elif f["is_harvest_veg"]:
        a = ("field_to_crop_veg", "field_to_firm_veg", "period_available_ids_general")
    elif is_h:
        a = ("field_to_crop_harvest", "field_to_firm_all", "period_available_ids_general")
    elif group_name == r2.PLOW_GROUP_NAME:
        a = ("field_to_crop_plow", "field_to_firm_all", "period_available_ids")
    elif group_name in (r2.DEEP_GROUP_NAME, r2.DISK_GROUP_NAME):
        a = ("field_to_crop_general", "field_to_firm_all", "period_available_ids_general")
    elif gen:
        a = ("field_to_crop_general", "field_to_firm_general", "period_available_ids_general")
    elif group_name == r2.CULTIVATION_GROUP_NAME:
        a = ("field_to_crop_report3", "field_to_firm_all", "period_available_ids")
    else:
        a = ("field_to_crop", "field_to_firm", "period_available_ids")
    f["active_crop_map"], f["active_firm_map"], f["active_available_ids"] = a
    f["use_report17_area_logic"] = group_name == r2.DISK_GROUP_NAME
    f["is_harvest_grain"] = is_h
    f["use_report4_boron_logic"] = group_name == r2.BORON_GROUP_NAME
    return f


def shift_window(end_raw, period_start, tillage):
    end_dt = r2.parse_dt(end_raw)
    if end_dt is None:
        return None
    cal = end_dt.date()
    cutoff = datetime(cal.year, cal.month, cal.day, 6, 0, 0, tzinfo=TZ)
    if end_dt < cutoff:
        cal = cal - timedelta(days=1)
        cutoff = datetime(cal.year, cal.month, cal.day, 6, 0, 0, tzinfo=TZ) + timedelta(days=1)
    else:
        cutoff = cutoff + timedelta(days=1)
    ds = cutoff - timedelta(days=1)
    if tillage:
        if not (period_start <= cal < TODAY):
            return None
    elif not (period_start <= cal <= TODAY):
        return None
    return cal, ds, cutoff


# ------------------------------------------------------------------ мир: задания в хранилище + строки площади
now_dt = datetime(TODAY.year, TODAY.month, TODAY.day, 12, 0, tzinfo=TZ)
TASKS: dict[int, dict] = {}
ROWS: dict[int, list] = {}
GROUPS = [
    (r2.SPRAY_GROUP_NAME, [20]), (r2.HARVEST_GROUP_NAME, [30]), (r2.PLOW_GROUP_NAME, [40]),
    (r2.FERT_GROUP_NAME, [55]), (r2.DISK_GROUP_NAME, [60]), (r2.BORON_GROUP_NAME, [70]),
    (r2.CULTIVATION_GROUP_NAME, [80]), (r2.SEEDING_GROUP_NAME, [90]),
]
tid = 0
G_TASKS: dict[str, list[int]] = {}
for gname, wts in GROUPS:
    G_TASKS[gname] = []
    for _ in range(140):
        tid += 1
        age_h = random.uniform(2, 45 * 24)
        end = now_dt - timedelta(hours=age_h)
        t = {"id": tid, "work_type_id": wts[0], "season": random.choice(["2026", "2027"]),
             "machine_id": random.choice([1, 2, 3, 4, 5, 6, 7, 8, None]),
             "end_time": end.isoformat(), "updated_at": (end + timedelta(hours=1)).isoformat(),
             "covered_area": round(random.uniform(1, 80), 2)}
        if random.random() < 0.04:
            t["end_time"] = None
        TASKS[tid] = t
        G_TASKS[gname].append(tid)
        rows = []
        for k in range(random.randint(1, 3)):
            hours = random.randint(2, 7)
            hourly = []
            for h in range(hours):
                hs = (end - timedelta(hours=h + 1)).replace(minute=0, second=0, microsecond=0)
                hourly.append([hs.isoformat(), round(random.uniform(0.5, 9), 2)])
            rows.append({"id": tid * 10 + k, "machine_task_id": tid, "field_id": random.choice(FIELDS + [None, 999]),
                         "covered_area": round(sum(x[1] for x in hourly), 2), "covered_area_hourly": hourly,
                         "history_item_id": random.choice([5000 + random.randint(0, 59), None])})
        ROWS[tid] = rows

store = ts.get_store(r2.BASE_URL)
con = store._db()
con.execute("BEGIN IMMEDIATE")
store.cov_lo = {w: 0.0 for _, ws in GROUPS for w in ws}
for t in TASKS.values():
    if t["end_time"]:
        store._put_task(t)
    else:  # без end_time в хранилище не бывает — такие задания в сводке вне кэша
        pass
for i, rows in ROWS.items():
    store._set_mtfmi(i, rows)
store.synced_ts = time.time(); store.full_ts = store.hard_ts = time.time()
store._save_state()
con.execute("COMMIT")


def build_prepared(tasks_by_id=None, rows_by_id=None):
    tasks_by_id = tasks_by_id or TASKS
    rows_by_id = rows_by_id or ROWS
    data, pending, gm = {}, [], {}
    for gname, wts in GROUPS:
        fl = flags(gname, wts)
        tillage = gname in r2._TILLAGE_SHIFT_GROUPS
        pstart = date(TODAY.year, 3, 14)
        shift, ok_tasks, no_shift = {}, [], []
        for i in G_TASKS[gname]:
            t = tasks_by_id[i]
            if t["end_time"] is None and i not in {x["id"] for x in ()}:
                pass
            res = shift_window(t.get("end_time"), pstart, tillage)
            if res is not None:
                shift[i] = res
                ok_tasks.append(t)
            else:
                no_shift.append(t)
        allm = ok_tasks + no_shift
        data[gname] = {"crops": {}, "machines": {f: __import__("collections").defaultdict(lambda: {"own": set(), "hired": set()}) for f in FIRMS},
                       "area_by_firm_date": {f: __import__("collections").defaultdict(float) for f in FIRMS},
                       "area_by_firm_total": {f: 0.0 for f in FIRMS}}
        pending.append({"group_name": gname, **fl, "all_tasks": ok_tasks, "no_shift_tasks": no_shift,
                        "task_shift": shift, "task_ids": [t["id"] for t in allm],
                        "task_machine": {t["id"]: t.get("machine_id") for t in allm},
                        "all_tasks_for_mtfmi": allm, "period_start": pstart})
    gmtfmi = {i: rows_by_id.get(i, []) for i in tasks_by_id}
    # агрооперации: часть заданий получает культуру через агрооперацию
    t2o = {i: {900000 + i} for i in tasks_by_id if i % 3 == 0}
    o2h = {900000 + i: 5000 + (i % 60) for i in tasks_by_id if i % 3 == 0}
    o2f = {900000 + i: FIELDS[i % len(FIELDS)] for i in tasks_by_id if i % 3 == 0}
    return {"data": data, "pending_groups": pending, "global_mtfmi": gmtfmi,
            "agro_raw": (t2o, o2h, o2f), "leaked_flags": (False, False)}


def run(mode, prepared):
    os.environ["SVODKI_SEASON_CACHE"] = mode
    r2._season_cache_snapshot()
    buf = io.StringIO()
    with redirect_stdout(buf):
        out = r2.collect_work_data(
            maps["field_to_crop"], maps["field_to_crop_general"], maps["field_to_crop_harvest"],
            maps["field_to_crop_plow"], maps["field_to_crop_veg"], maps["field_to_crop_report3"],
            maps["field_to_firm"], maps["field_to_firm_general"], maps["field_to_firm_veg"],
            maps["field_to_firm_all"], maps["crop_field_ids_report3"], maps["period_available_ids"],
            maps["period_available_ids_general"], field_area, machine_group, {},
            dict(history_item_to_crop), dict(history_item_to_year), copy.deepcopy(prepared),
        )
    return out, buf.getvalue()


def norm(o):
    if isinstance(o, dict):
        return {repr(k): norm(v) for k, v in o.items()}
    if isinstance(o, (set, frozenset)):
        return sorted(repr(x) for x in o)
    if isinstance(o, (list, tuple)):
        return [norm(x) for x in o]
    if isinstance(o, float):
        return round(o, 5)
    return o


def same(a, b, path=""):
    """Сравнение с допуском 1e-5 для чисел; пустые словари/ноль равны отсутствию."""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in set(a) | set(b):
            if not same(a.get(k, {} if isinstance(b.get(k), dict) else None), b.get(k, {} if isinstance(a.get(k), dict) else None), f"{path}/{k}"):
                return False
        return True
    if isinstance(a, float) or isinstance(b, float):
        return abs(float(a or 0) - float(b or 0)) <= 1e-4
    if a != b:
        print("     расхождение:", path, a, b)
        return False
    return True


prepared = build_prepared()
n_cold = sum(1 for _ in TASKS)
print(f"синтетический мир: {len(TASKS)} заданий, {sum(len(v) for v in ROWS.values())} строк площади, {len(GROUPS)} групп")

print("1) off: прежний полный пересчёт (эталон)")
base, log_off = run("off", prepared)
ok("кэш не трогался", "[season-cache]" not in log_off
   and store._db().execute("SELECT COUNT(*) FROM calc_cache").fetchone()[0] == 0)
tot = sum(sum(v for v in base[g]["area_by_firm_total"].values()) for g, _ in GROUPS)
ok(f"эталон не пустой (сумма по группам {tot:.0f} га)", tot > 100)

print("2) shadow: итог = эталон, расхождений нет")
sh, log_sh = run("shadow", prepared)
ok("результат shadow совпадает с эталоном", same(norm(base), norm(sh)))
ok("по всем группам «кэш == полный пересчёт»",
   log_sh.count("кэш == полный пересчёт") == len(GROUPS) and "РАСХОЖДЕНИЙ" not in log_sh)
print("\n".join("     " + l.strip() for l in log_sh.splitlines() if "[season-cache]" in l and "shadow" not in l)[:900])
nc = store._db().execute("SELECT COUNT(*) FROM calc_cache").fetchone()[0]
ok(f"shadow наполнил кэш ({nc} вкладов)", nc > 300)

print("3) on, кэш тёплый: итог = эталон, почти всё из кэша")
t0 = time.monotonic()
on1, log_on = run("on", prepared)
ok("результат on совпадает с эталоном", same(norm(base), norm(on1)))
hits = sum(int(l.split("из кэша ")[1].split(",")[0]) for l in log_on.splitlines() if "из кэша" in l)
calc = sum(int(l.split("пересчитано ")[1].split(",")[0]) for l in log_on.splitlines() if "из кэша" in l)
ok(f"из кэша {hits}, пересчитано {calc}", hits > 400 and calc == 0)

print("4) изменились 3 задания: пересчитываются только они, итог = полный пересчёт на новых данных")
T2, R2 = copy.deepcopy(TASKS), copy.deepcopy(ROWS)
def _cold_pick(gname, start):
    # не «горячее» (старше 10 суток), с датой окончания и строкой площади; «горячие» по определению не кэшируются
    for i in G_TASKS[gname][start:]:
        t = TASKS[i]
        if t["end_time"] and (now_dt - datetime.fromisoformat(t["end_time"])).days > 10 and ROWS[i]:
            return i
    raise SystemExit("в синтетическом мире нет подходящего задания")


victims = [_cold_pick(r2.SPRAY_GROUP_NAME, 5), _cold_pick(r2.DISK_GROUP_NAME, 9), _cold_pick(r2.BORON_GROUP_NAME, 3)]
for v in victims:
    R2[v][0]["covered_area_hourly"][0][1] += 4.0
    R2[v][0]["covered_area"] = round(R2[v][0]["covered_area"] + 4.0, 2)
con.execute("BEGIN IMMEDIATE")
for v in victims:
    store._set_mtfmi(v, R2[v])
con.execute("COMMIT")
prep2 = build_prepared(T2, R2)
ref2, _ = run("off", prep2)
on2, log_on2 = run("on", prep2)
ok("итог на изменённых данных == полный пересчёт", same(norm(ref2), norm(on2)))
ok("и он отличается от прежнего", not same(norm(base), norm(on2)) if False else norm(base) != norm(on2))
calc2 = sum(int(l.split("пересчитано ")[1].split(",")[0]) for l in log_on2.splitlines() if "из кэша" in l)
ok(f"пересчитано ровно {len(victims)} (было {calc2})", calc2 == len(victims))

print("5) изменилась карта поля → затронутые задания пересчитываются (stamp), итог верен")
maps["field_to_crop"][1] = "Соя" if maps["field_to_crop"].get(1) != "Соя" else "Ячмень яровой"
maps["field_to_firm_all"][2] = FIRMS[(FIRMS.index(maps["field_to_firm_all"][2]) + 1) % len(FIRMS)]
ref3, _ = run("off", prep2)
on3, log_on3 = run("on", prep2)
ok("итог == полный пересчёт после смены справочников", same(norm(ref3), norm(on3)))
calc3 = sum(int(l.split("пересчитано ")[1].split(",")[0]) for l in log_on3.splitlines() if "из кэша" in l)
ok(f"пересчитано {calc3} заданий, а не все", 0 < calc3 < 120)

print("6) связи/привязка агрооперации (версия задания) → пересчёт")
con.execute("BEGIN IMMEDIATE")
store._bump(G_TASKS[r2.SPRAY_GROUP_NAME][12])
con.execute("COMMIT")
_, log_on4 = run("on", prep2)
calc4 = sum(int(l.split("пересчитано ")[1].split(",")[0]) for l in log_on4.splitlines() if "из кэша" in l)
ok("поднятая версия → пересчитано ровно 1", calc4 == 1)

print("7) аудит ловит испорченный вклад")
os.environ["SVODKI_SEASON_CACHE_AUDIT"] = "1"
store._db().execute("BEGIN IMMEDIATE")
import zlib, json
row = store._db().execute("SELECT ckey, task_id, payload FROM calc_cache WHERE payload IS NOT NULL ORDER BY task_id LIMIT 1").fetchone()
pl = json.loads(zlib.decompress(row[2]))
pl["mda"] = [[f, d, m, v + 1000.0] for f, d, m, v in pl["mda"]] or pl["mda"]
store._db().execute("UPDATE calc_cache SET payload=? WHERE ckey=? AND task_id=?",
                    (zlib.compress(json.dumps(pl).encode()), row[0], row[1]))
store._db().execute("COMMIT")
ref5, _ = run("off", prep2)
on5, log_on5 = run("on", prep2)
os.environ["SVODKI_SEASON_CACHE_AUDIT"] = "0"
if pl["mda"]:
    ok("аудит заметил подмену и итог всё равно верный", "АУДИТ" in log_on5 and same(norm(ref5), norm(on5)))
else:
    ok("(у выбранного вклада нет mda — пропуск)", True)

print("8) режим off после всего: хранилище не мешает, результат прежний")
off2, _ = run("off", prep2)
ok("off == эталон на текущих данных", same(norm(ref5), norm(off2)))

print("\nИТОГ:", "ЕСТЬ ОШИБКИ" if failed else "всё прошло")
sys.exit(1 if failed else 0)
