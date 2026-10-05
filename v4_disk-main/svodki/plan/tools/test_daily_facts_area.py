# -*- coding: utf-8 -*-
"""Сводка 1: area_today из TaskStore (shadow/daily). Из svodki/plan."""
from __future__ import annotations

import os
import random
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import plans  # noqa: F401,E402

tmp = Path(tempfile.mkdtemp(prefix="facts_area_"))
os.environ["SVODKI_TASK_STORE_DIR"] = str(tmp)
import svodki_task_store as ts  # noqa: E402
from svodki_daily_facts_area import (  # noqa: E402
    apply_report1_area_today_facts,
    area_today_by_firm_from_store,
    daily_facts_env_flags,
)
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


random.seed(7)
now = time.time()
shift = datetime.now(TZ).date()
day_start = datetime(shift.year, shift.month, shift.day, 6, 0, 0, tzinfo=TZ)
cutoff = day_start + timedelta(days=1)

st = ts.get_store(BASE)
con = st._db()
con.execute("BEGIN IMMEDIATE")
st.cov_lo = {10: now - 60 * 86400}
st._put_task({
    "id": 1, "work_type_id": 10, "season": "2026", "machine_id": 50,
    "end_time": ts.fmt_ts(datetime.combine(shift, datetime.min.time().replace(hour=12), tzinfo=TZ).timestamp()),
})
hourly = []
for h in range(8):
    t = day_start + timedelta(hours=h + 1)
    hourly.append([t.isoformat(), 2.0])
st._set_mtfmi(1, [{"id": 101, "machine_task_id": 1, "field_id": 1001,
                   "covered_area": 16.0, "covered_area_hourly": hourly}])
st.synced_ts = st.full_ts = st.hard_ts = now
st._save_state()
con.execute("COMMIT")

firm_map = {1001: "FirmA", 1002: "FirmB"}

by_firm = area_today_by_firm_from_store(
    st,
    work_type_ids=[10],
    shift_date=shift,
    day_start=day_start,
    cutoff=cutoff,
    area_min_ha=0.5,
    field_ok=lambda fid: fid in firm_map,
    firm_for_field=lambda fid: firm_map.get(fid),
)
ok("area_today_by_firm FirmA=16", abs(by_firm.get("FirmA", 0) - 16.0) < 0.02)

legacy_stats = {"FirmA": {"area_today": 10.0}, "FirmB": {"area_today": 0.0}}
os.environ["SVODKI_DAILY_FACTS_SHADOW"] = "1"
os.environ.pop("SVODKI_DAILY_FACTS_DAILY", None)
out = apply_report1_area_today_facts(
    legacy_stats,
    group_name="2.1 Боронование",
    work_type_ids=[10],
    shift_date=shift,
    day_start=day_start,
    cutoff=cutoff,
    area_min_ha=0.5,
    field_ok=lambda fid: fid in firm_map,
    firm_for_field=lambda fid: firm_map.get(fid),
    base_url=BASE,
    firm_keys=["FirmA", "FirmB"],
)
ok("shadow не меняет legacy", out["FirmA"]["area_today"] == 10.0)

os.environ["SVODKI_DAILY_FACTS_DAILY"] = "1"
out2 = apply_report1_area_today_facts(
    {"FirmA": {"area_today": 10.0}},
    group_name="2.1 Боронование",
    work_type_ids=[10],
    shift_date=shift,
    day_start=day_start,
    cutoff=cutoff,
    area_min_ha=0.5,
    field_ok=lambda fid: fid in firm_map,
    firm_for_field=lambda fid: firm_map.get(fid),
    base_url=BASE,
    firm_keys=["FirmA"],
)
ok("daily подставляет facts", abs(out2["FirmA"]["area_today"] - 16.0) < 0.02)
ok("флаги env", daily_facts_env_flags() == (True, True))

print("\nИТОГ:", "всё прошло" if not failed else "есть ошибки")
sys.exit(1 if failed else 0)
