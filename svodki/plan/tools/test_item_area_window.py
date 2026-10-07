"""Регрессия: площадь строки MTFMI в окне смены и flat_facts по сезону."""
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "utils" / "shared"), str(HERE.parent / "utils" / "api"), str(HERE.parent)]

import task_area_shared as t  # noqa: E402

TZ = timezone(timedelta(hours=3))
WS, WE = datetime(2026, 10, 5, 6, tzinfo=TZ), datetime(2026, 10, 6, 6, tzinfo=TZ)
P = datetime.fromisoformat


def test_harvest_shift_keeps_only_hours_inside_the_shift():
    item = {
        "covered_area": 50.0,
        "covered_area_hourly": [
            ["2026-10-05T04:00:00+03:00", 20.0],
            ["2026-10-05T10:00:00+03:00", 30.0],
        ],
    }
    assert t.harvest_item_area_for_shift(item, WE, day_start=WS, work_type_id=126, parse_dt=P) == 30.0


def test_cropwise_harvest_does_not_include_potato_task():
    from proverka.task_names import tasks_for_group

    names = tasks_for_group("5.0 Уборка")
    assert "5.4 Уборка картофеля, лука" not in names
    assert "5.4 Уборка картофеля, лука" in tasks_for_group("5.4 Уборка картофеля, лука")


def test_hourly_outside_window_is_zero_not_full_covered_area():
    item = {"covered_area": 30.0, "covered_area_hourly": [["2026-10-04T10:00:00+03:00", 30.0]]}
    assert t.item_area_in_window(item, WE, WS, parse_dt=P) == 0.0


def test_firm_without_work_in_window_gets_nothing():
    a = {"field_id": 1, "covered_area": 10.0, "covered_area_hourly": [["2026-10-05T10:00:00+03:00", 10.0]]}
    b = {"field_id": 2, "covered_area": 30.0, "covered_area_hourly": [["2026-10-04T10:00:00+03:00", 30.0]]}
    res = t.task_cov_by_firm({"covered_area": 40.0}, [a, b], cutoff=WE, window_start=WS,
                             field_ok=lambda f: True, firm_for_field=lambda f: {1: "A", 2: "B"}[f], parse_dt=P)
    assert res == {"A": 40.0}


def test_fallback_to_covered_area_only_when_no_hourly_at_all():
    assert t.item_area_in_window({"covered_area": 12.0}, WE, WS, parse_dt=P) == 12.0
    assert t.item_area_in_window({"covered_area": 12.0, "covered_area_hourly": []}, WE, WS, parse_dt=P) == 12.0
    # окно смены не задано — тоже covered_area
    zeros = {"covered_area": 12.0, "covered_area_hourly": [["2026-10-05T10:00:00+03:00", 5.0]]}
    assert t.item_area_in_window(zeros, None, None, parse_dt=P) == 12.0


def test_zero_or_unreadable_hourly_does_not_fall_back_to_full_area():
    zeros = {"covered_area": 12.0, "covered_area_hourly": [["2026-10-05T10:00:00+03:00", 0.0]]}
    assert t.item_area_in_window(zeros, WE, WS, parse_dt=P) == 0.0
    bad = {"covered_area": 12.0, "covered_area_hourly": [["x", 5.0]]}
    assert t.item_area_in_window(bad, WE, WS, parse_dt=lambda v: None) == 0.0


def test_task_with_all_hourly_outside_window_gives_no_area():
    item = {"field_id": 1, "covered_area": 100.0,
            "covered_area_hourly": [["2026-10-03T10:00:00+03:00", 60.0], ["2026-10-03T11:00:00+03:00", 40.0]]}
    res = t.task_cov_by_firm({"covered_area": 100.0}, [item], cutoff=WE, window_start=WS,
                             field_ok=lambda f: True, firm_for_field=lambda f: "A", parse_dt=P)
    assert res == {}


def test_task_without_hourly_still_split_by_firms():
    item = {"field_id": 1, "covered_area": 100.0}
    res = t.task_cov_by_firm({"covered_area": 100.0}, [item], cutoff=WE, window_start=WS,
                             field_ok=lambda f: True, firm_for_field=lambda f: "A", parse_dt=P)
    assert res == {"A": 100.0}


def test_flat_facts_respects_season():
    import svodki_task_store as ts
    from svodki_daily_facts_area import area_today_by_firm_from_store

    st = ts.TaskStore(Path(tempfile.mkdtemp()), "http://t")
    db = st._db()
    now = datetime.now(ts._tz()).replace(hour=12, minute=0, second=0, microsecond=0)
    for tid, season, ha in [(1, "2026", 10.0), (2, "2025", 7.0)]:
        db.execute("INSERT INTO tasks (id,wt,end_ts,season,data,machine_id) VALUES (?,?,?,?,?,?)",
                   (tid, 10, now.timestamp(), season, "{}", 5))
        db.execute("INSERT INTO mtfmi_fact (rid,task_id,field_id,covered_area,hourly_n) VALUES (?,?,?,?,0)",
                   (tid, tid, 100, ha))
    db.commit()
    kw = dict(work_type_ids=[10], shift_date=date.fromisoformat(ts.shift_date_of(now)),
              day_start=now - timedelta(hours=12), cutoff=now + timedelta(hours=12), area_min_ha=0,
              field_ok=lambda f: True, firm_for_field=lambda f: "F")
    assert area_today_by_firm_from_store(st, **kw) == {"F": 17.0}
    assert area_today_by_firm_from_store(st, task_seasons={"2026"}, **kw) == {"F": 10.0}
