"""Топливо и аномалии расхода из store.sqlite на синтетическом парке."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import zlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_PLAN = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PLAN))
sys.path.insert(0, str(_PLAN / "utils" / "shared"))

import fuel_analysis as fa  # noqa: E402

_TZ = timezone(timedelta(hours=3))
WT = 55


def _ts(day: int, hour: int = 12) -> float:
    return datetime(2026, 9, day, hour, tzinfo=_TZ).timestamp()


class _Store:
    def __init__(self, tmp: Path) -> None:
        self.con = sqlite3.connect(tmp / "store.sqlite")
        self.con.executescript(
            """
            CREATE TABLE tasks (id INTEGER PRIMARY KEY, wt INTEGER, end_ts REAL, season TEXT,
                data TEXT, raw BLOB, machine_id INTEGER);
            CREATE TABLE mtfmi_fact (rid INTEGER PRIMARY KEY, task_id INTEGER, field_id INTEGER,
                covered_area REAL, hourly_n INTEGER);
            """
        )
        self.n = 0

    def task(self, machine: int, day: int, ha: float, *, fuel=None, road=None, dist_km=None,
             road_h=None, wt: int = WT, driver: int = 9, field: int = 100) -> int:
        self.n += 1
        data = {"driver_id": driver}
        if ha:
            data["covered_area"] = ha
        if fuel is not None:
            data["fuel_consumption"] = fuel
        if road is not None:
            data["fuel_consumption_on_road"] = road
        if dist_km is not None:
            data["work_distance"] = dist_km * 1000
        if road_h is not None:
            data["movements_on_road_duration"] = road_h * 3600
        self.con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,NULL,?)",
                         (self.n, wt, _ts(day), "2026", json.dumps(data), machine))
        self.con.execute("INSERT INTO mtfmi_fact VALUES (?,?,?,?,1)", (self.n, self.n, field, ha))
        return self.n

    def done(self, tmp: Path) -> None:
        self.con.commit()
        self.con.close()
        refs = sqlite3.connect(tmp / "refs.sqlite")
        refs.execute("CREATE TABLE ref (key TEXT PRIMARY KEY, endpoint TEXT, params TEXT, "
                     "fetched_at REAL, n INTEGER, rows BLOB)")

        def put(endpoint: str, rows: list[dict]) -> None:
            refs.execute("INSERT INTO ref VALUES (?,?, '', 1, ?, ?)",
                         (endpoint, endpoint, len(rows), zlib.compress(json.dumps(rows).encode())))

        put("fields", [{"id": 100, "name": "Север", "field_group_id": 20}])
        put("field_groups", [{"id": 20, "group_folder_id": 3}])
        put("group_folders", [{"id": 3, "name": "АФ Чистопольская"}])
        put("machines", [{"id": m, "name": f"МТЗ-{m}", "registration_number": f"{m:03d}АА16"} for m in range(1, 13)])
        refs.commit()
        refs.close()


def _fleet() -> tuple[Path, dict[str, int]]:
    tmp = Path(tempfile.mkdtemp())
    s = _Store(tmp)
    ids: dict[str, int] = {}
    # 12 исправных машин: ≈12 л/га в поле, дорога ≈10 л/ч
    for m in range(1, 13):
        for k in range(3):
            ha = 20.0
            l_ha = 12 + (k - 1) * 0.5
            field = ha * l_ha
            s.task(m, 10 + k, ha, fuel=field + 5, road=5, dist_km=ha * 1.2, road_h=0.5)
    # перерасход в поле
    ids["over"] = s.task(1, 20, 20.0, fuel=20 * 30 + 5, road=5, dist_km=24, road_h=0.5)
    # расход без выработки
    ids["idle"] = s.task(2, 20, 0.0, fuel=60, road=0, road_h=0.0)
    # дорога: 50 л за полчаса при норме 10 л/ч
    ids["road"] = s.task(3, 21, 20.0, fuel=20 * 12 + 50, road=50, dist_km=24, road_h=0.5)
    # машина без датчика: 6 заданий без топлива
    for k in range(6):
        s.task(99, 10 + k, 15.0, driver=5)
    # датчик пропадает: половина заданий без топлива
    for k in range(6):
        if k % 2:
            s.task(98, 10 + k, 15.0, fuel=15 * 12 + 5, road=5, road_h=0.5)
        else:
            s.task(98, 10 + k, 15.0)
    # систематический перерасход у машины 12 в другом виде работ + парк в нём
    for m in range(1, 8):
        for k in range(2):
            s.task(m, 12 + k, 30.0, fuel=30 * 8 + 3, road=3, road_h=0.3, wt=60)
    for k in range(3):
        s.task(12, 15 + k, 30.0, fuel=30 * 14 + 3, road=3, road_h=0.3, wt=60)
    s.done(tmp)
    return tmp, ids


def _by_kind(payload: dict, kind: str) -> list[dict]:
    return [a for a in payload["anomalies"] if a["kind"] == kind]


def test_breakdown_and_fuel_math() -> None:
    tmp, _ = _fleet()
    out = fa.query_fuel(tmp, date(2026, 9, 1), date(2026, 9, 30))
    assert out["available"] is True
    cov = out["coverage"]
    assert cov["machines_no_fuel_count"] == 1
    assert cov["machines_no_fuel"][0]["machine_id"] == 99
    assert cov["machines_partial_count"] == 1
    assert cov["machines_partial"][0]["machine_id"] == 98
    assert cov["machines_partial"][0]["missing"] == 3
    # заполненность: машина 99 целиком без топлива
    m99 = next(r for r in out["by_machine"] if r["machine_id"] == 99)
    assert m99["fuel_tasks"] == 0 and m99["l_ha"] is None
    m9 = next(r for r in out["by_machine"] if r["machine_id"] == 9)
    assert 11.5 < m9["l_ha"] < 12.5          # топливо в поле = всего − дорога
    assert m9["fuel_road"] > 0
    assert m9["machine"].startswith("МТЗ-9")
    assert out["totals"]["tasks"] == sum(r["tasks"] for r in out["by_day"])
    firms = {r["firm"] for r in out["by_firm"]}
    assert "АФ Чистопольская" in firms


def test_anomalies_found() -> None:
    tmp, ids = _fleet()
    out = fa.query_fuel(tmp, date(2026, 9, 1), date(2026, 9, 30))

    over = [a for a in _by_kind(out, "over_field") if a["task_id"] == ids["over"]]
    assert over and over[0]["severity"] == "critical"
    assert 340 < over[0]["excess_l"] < 380   # (30 − 12) л/га × 20 га
    assert over[0]["task_url"].endswith(f"/machines/1/tasks/{ids['over']}")

    idle = [a for a in _by_kind(out, "idle_burn") if a["task_id"] == ids["idle"]]
    assert idle and idle[0]["severity"] == "critical"

    road = [a for a in _by_kind(out, "over_road") if a["task_id"] == ids["road"]]
    assert road and road[0]["road_l_h"] == 100.0

    systemic = [a for a in _by_kind(out, "machine_systemic") if a["machine_id"] == 12]
    assert systemic and systemic[0]["wt"] == 60
    assert out["anomaly_counts"]["sensor_gaps"] == 1

    # исправные задания не помечаются
    flagged = {a["task_id"] for a in out["anomalies"] if a["kind"] == "over_field"}
    assert all(t in flagged for t in (ids["over"],))
    assert len(_by_kind(out, "over_field")) < 12  # норма не ловится как нарушение
    # сортировка: critical раньше warning
    order = [a["severity"] for a in out["anomalies"]]
    assert order == sorted(order, key=lambda s: {"critical": 0, "warning": 1, "info": 2}[s])


def test_no_false_positives_on_uniform_fleet() -> None:
    tmp = Path(tempfile.mkdtemp())
    s = _Store(tmp)
    for m in range(1, 13):
        for k in range(4):
            l_ha = 12 + (k - 1.5) * 0.4
            s.task(m, 10 + k, 20.0, fuel=20 * l_ha + 5, road=5, dist_km=24, road_h=0.5)
    s.done(tmp)
    out = fa.query_fuel(tmp, date(2026, 9, 1), date(2026, 9, 30))
    assert out["anomalies"] == []


def test_too_few_peers_means_no_norm() -> None:
    tmp = Path(tempfile.mkdtemp())
    s = _Store(tmp)
    for k in range(4):
        s.task(1, 10 + k, 20.0, fuel=20 * 12 + 5, road=5)
    s.task(1, 20, 20.0, fuel=20 * 40 + 5, road=5)
    s.done(tmp)
    out = fa.query_fuel(tmp, date(2026, 9, 1), date(2026, 9, 30))
    assert out["anomalies"] == []   # 5 аналогов < min_peers: нормы нет, молчим


def test_missing_store_and_period() -> None:
    out = fa.query_fuel(Path(tempfile.mkdtemp()), date(2026, 9, 1), date(2026, 9, 30))
    assert out["available"] is False and out["anomalies"] == []
    try:
        fa.query_fuel(Path("/tmp"), date(2026, 9, 30), date(2026, 9, 1))
    except ValueError as exc:
        assert "раньше" in str(exc)
    else:
        raise AssertionError("ожидалась ошибка периода")


def test_names_applied() -> None:
    tmp, ids = _fleet()
    out = fa.query_fuel(tmp, date(2026, 9, 1), date(2026, 9, 30))
    fa.apply_names(out, {9: "Иванов Иван"}, {WT: "Вспашка"})
    assert out["names_loaded"] is True
    a = next(a for a in out["anomalies"] if a["task_id"] == ids["over"])
    assert a["driver"] == "Иванов Иван" and a["operation"] == "Вспашка"
    d9 = next(r for r in out["by_driver"] if r["driver_id"] == 9)
    assert d9["driver"] == "Иванов Иван"
