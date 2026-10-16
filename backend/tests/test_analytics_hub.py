"""Хаб аналитики: все срезы на синтетическом хранилище, суммы сходятся со svodki_analytics."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("SVODKI_ROOT", str(ROOT / "svodki"))
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "svodki" / "plan"))

TZ = timezone(timedelta(hours=3))
QUERY_IDS = [
    "people_rating", "people_low", "people_load", "machine_work", "machine_idle", "fuel", "norm_vs_fact",
    "fleet_load", "speed", "field_history", "plan_progress", "fields_idle", "repeat_passes", "firm_compare",
    "holding_totals", "q_no_driver", "q_no_crop_agro", "q_no_hourly", "q_suspicious",
]


def _schema() -> str:
    import re

    src = (ROOT / "svodki/plan/utils/shared/svodki_task_store.py").read_text(encoding="utf-8")
    return re.search(r'_SCHEMA = """(.*?)"""', src, re.S).group(1)


@pytest.fixture
def store(tmp_path, monkeypatch):
    con = sqlite3.connect(tmp_path / "store.sqlite")
    con.executescript(_schema())
    rid = 0
    for tid, (mid, driver, ha, hourly, extra) in enumerate([
        (101, 900, 100.0, 8, {"fuel_consumption": 700.0, "avg_speed": 8.0}),
        (101, 900, 60.0, 6, {"fuel_consumption": 420.0, "avg_speed": 8.5}),
        (102, None, 40.0, 0, {}),
    ], 1):
        day = datetime(2026, 9, tid, 15, tzinfo=TZ)
        data = {"machine_id": mid, **extra, **({"driver_id": driver} if driver else {})}
        con.execute("INSERT INTO tasks(id,wt,end_ts,season,data,machine_id) VALUES(?,?,?,?,?,?)",
                    (tid, 4, day.timestamp(), "2026", json.dumps(data), mid))
        rid += 1
        con.execute("INSERT INTO mtfmi_fact(rid,task_id,field_id,covered_area,hourly_n) VALUES(?,?,?,?,?)",
                    (rid, tid, 1, ha, hourly))
        con.execute("INSERT INTO mtfmi(rid,task_id,data) VALUES(?,?,?)", (rid, tid, json.dumps({"history_item_id": 1})))
        if hourly:
            con.execute("INSERT INTO mtfmi_daily(rid,shift_date,ha) VALUES(?,?,?)", (rid, f"2026-09-{tid:02d}", ha))
    con.execute("INSERT INTO history_crop(hid,crop) VALUES(1,'Соя')")
    con.commit()
    con.close()
    refs = sqlite3.connect(tmp_path / "refs.sqlite")
    refs.execute("CREATE TABLE ref (key TEXT PRIMARY KEY, endpoint TEXT, params TEXT, fetched_at REAL, n INTEGER, rows BLOB)")
    for ep, rows in (("machines", [{"id": 101, "name": "МТЗ"}, {"id": 102, "name": "Нива"}]),
                     ("fields", [{"id": 1, "name": "Поле 1", "area": 200}])):
        refs.execute("INSERT INTO ref VALUES(?,?,?,?,?,?)",
                     (ep, ep, "", 1.0, len(rows), zlib.compress(json.dumps(rows, ensure_ascii=False).encode())))
    refs.commit()
    refs.close()
    monkeypatch.setenv("SVODKI_TASK_STORE_DIR", str(tmp_path))
    from plan_paths import ensure_plan_sys_path

    ensure_plan_sys_path()
    return tmp_path


def _run(store, qid, **kw):
    import svodki_hub

    return svodki_hub.run(qid, date_from="2026-09-01", date_to="2026-09-10", d=store, **kw)


@pytest.mark.parametrize("qid", QUERY_IDS)
def test_every_query_returns_uniform_shape(store, qid):
    extra = {"machine_id": 101, "field_id": 1, "plan": {"4.0 Посев": {"АФ Чистопольская": {"Соя": 500}}}}
    out = _run(store, qid, **extra)
    assert {"columns", "rows", "summary", "note", "chart", "tree", "truncated"} <= set(out)
    json.dumps(out)
    keys = {c["key"] for c in out["columns"]}
    for row in out["rows"][:5]:
        assert keys <= set(row), f"{qid}: колонки без данных {keys - set(row)}"


def test_totals_match_svodki_analytics(store):
    import svodki_analytics as sa

    wts = sa._wts_for(None)
    with sa.store_conn(store) as con:
        ref = con.execute(
            f"SELECT SUM(ha) FROM ({sa._facts_sql()}) WHERE day>=? AND day<? AND wt IN ({sa._in(len(wts))})",
            ["2026-09-01", "2026-09-10", *wts]).fetchone()[0]
    out = _run(store, "firm_compare")
    assert out["summary"][1]["value"] == pytest.approx(ref, abs=0.01)


def test_no_driver_hectares_are_not_lost(store):
    rating = _run(store, "people_rating")
    lost = _run(store, "q_no_driver")
    with_driver = rating["summary"][1]["value"]
    assert with_driver + lost["summary"][1]["value"] == pytest.approx(200.0, abs=0.01)
    assert lost["rows"][0]["task_id"] == 3


def test_fuel_missing_field_is_reported_not_invented(store, tmp_path):
    con = sqlite3.connect(store / "store.sqlite")
    con.execute("UPDATE tasks SET data=json_remove(data,'$.fuel_consumption')")
    con.commit()
    con.close()
    import svodki_hub

    svodki_hub._KEY_CACHE.clear()
    out = _run(store, "fuel")
    assert out["rows"] == [] and "нет поля расхода топлива" in out["note"]


def test_machine_work_requires_selection(store):
    out = _run(store, "machine_work")
    assert out["rows"] == [] and "Выберите машину" in out["note"]


def test_unknown_query_and_bad_period(store):
    import svodki_hub

    with pytest.raises(ValueError):
        svodki_hub.run("nope", date_from="2026-09-01", date_to="2026-09-10", d=store)
    with pytest.raises(ValueError):
        svodki_hub.run("people_rating", date_from="2026-09-10", date_to="2026-09-01", d=store)


def test_wrapper_makes_date_to_inclusive_and_degrades_without_cropwise(store, monkeypatch):
    from app.config import settings
    from app.legacy import analytics_hub

    monkeypatch.setattr(settings, "svodki_task_store_dir", str(store))
    out = analytics_hub.run_query("people_rating", date_from="2026-09-02", date_to="2026-09-02", group=None,
                                  driver_id=None, machine_id=None, field_id=None, threshold=None)
    assert out["date_to"] == "2026-09-02"
    # «по» включительно: однодневный период даёт смену 02.09 (60 га), имени без токена Cropwise нет
    assert [r["driver_id"] for r in out["rows"]] == [900] and out["rows"][0]["ha"] == 60.0


# ----------------------------------------------------------------------------- переходы по клику
@pytest.mark.parametrize("qid", QUERY_IDS)
def test_drill_columns_have_ids_in_every_row(store, qid):
    """Кликабельная колонка без *_id в строке даст мёртвую ссылку, поэтому контракт проверяем на всех срезах."""
    out = _run(store, qid, machine_id=101, field_id=1, threshold=1.0,
               plan={"4.0 Посев": {"АФ Чистопольская": {"Соя": 500}}})
    for column in out["columns"]:
        kind = column.get("drill")
        if not kind:
            continue
        assert kind in {"driver", "machine", "field"}
        for row in out["rows"]:
            assert f"{kind}_id" in row, f"{qid}: в строке нет {kind}_id для колонки {column['key']}"


def test_drill_targets_exist_for_the_main_paths(store):
    drills = {}
    for qid in QUERY_IDS:
        out = _run(store, qid, machine_id=101, field_id=1, threshold=1.0,
                   plan={"4.0 Посев": {"АФ Чистопольская": {"Соя": 500}}})
        drills[qid] = {c["drill"] for c in out["columns"] if c.get("drill")}
    assert drills["people_rating"] == {"driver"}
    assert {"driver", "machine"} <= drills["people_low"]
    assert drills["machine_idle"] == {"machine"} and drills["fuel"] == {"machine"}
    assert drills["fields_idle"] == {"field"} and drills["repeat_passes"] == {"field"}
    assert {"machine", "field"} <= drills["q_suspicious"]


# ----------------------------------------------------------------------------- права доступа
def _client(role):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.adapters.inbound.api.deps import get_current_active_user
    from app.adapters.inbound.api.routers import analytics
    from app.models import User

    app = FastAPI()
    app.include_router(analytics.router)
    app.dependency_overrides[get_current_active_user] = lambda: User(role=role, is_active=True)
    return TestClient(app)


# Старые ручки аналитики по-прежнему открыты любому активному пользователю.
# Новые проверки отдают ФИО, поэтому закрыты правом на планы.
HUB_URLS = [
    "/summaries/analytics/hub/people_rating",
    "/summaries/analytics/hub/options/machines",
]


@pytest.mark.parametrize("url", HUB_URLS)
@pytest.mark.parametrize("role", ["sitcentr", "viewer", ""])
def test_roles_without_plans_access_are_forbidden(store, monkeypatch, url, role):
    from app.config import settings

    monkeypatch.setattr(settings, "svodki_task_store_dir", str(store))
    assert _client(role).get(url).status_code == 403


@pytest.mark.parametrize("role", ["developer", "admin", "user"])
def test_roles_with_plans_access_get_data(store, monkeypatch, role):
    from app.config import settings

    monkeypatch.setattr(settings, "svodki_task_store_dir", str(store))
    c = _client(role)
    ok = c.get("/summaries/analytics/hub/people_rating", params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
    assert ok.status_code == 200 and ok.json()["title"] == "Рейтинг исполнителей"
    assert c.get("/summaries/analytics/overview").status_code == 200
