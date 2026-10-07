# -*- coding: utf-8 -*-
"""Проверка svodki_sanity на синтетических данных (без сети). Запуск из svodki/plan:

    python tools/test_sanity.py          # или pytest tools/test_sanity.py
"""
from __future__ import annotations

import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import plans  # noqa: F401,E402  (кладёт utils/* в sys.path)
import svodki_sanity as sn  # noqa: E402


def _row(tid=1, fid=10, hid=100, cov=50.0, **kw):
    r = {"machine_task_id": tid, "field_id": fid, "history_item_id": hid, "covered_area": cov}
    r.update(kw)
    return r


def _hist(hid=100, fid=10, crop=7, year=2026):
    sn.remember_history({"id": hid, "field_id": fid, "crop_id": crop, "year": year})


def _fresh(fields=None):
    sn.reset()
    sn.register_field_areas(fields if fields is not None else {10: 60.0, 11: 80.0})


def _codes() -> set[str]:
    return set(sn._N)


def test_clean_data_no_findings():
    _fresh()
    _hist()
    sn.on_tasks([{"id": 1, "work_type_id": 4, "covered_area": 50.0}])
    sn.on_mtfmi({1: [_row(covered_area_hourly=[["2026-09-01T07:00:00", 20.0], ["2026-09-01T08:00:00", 30.0]],
                          work_duration=3 * 3600, work_area=52.0)]})
    assert _codes() == set(), sn.summary()


def test_area_bad_and_bigger_than_field():
    _fresh()
    _hist()
    sn.on_mtfmi({1: [_row(cov=-3.0)], 2: [_row(tid=2, cov=200.0)], 3: [_row(tid=3, cov="abc")]})
    assert {"AREA_BAD", "AREA_GT_FIELD"} <= _codes()
    assert sn._N["AREA_BAD"] == 2


def test_hourly_work_rate_no_time_manual():
    _fresh()
    _hist()
    sn.on_mtfmi({
        1: [_row(tid=1, cov=50.0, covered_area_hourly=[["t", 10.0]])],                   # AREA_HOURLY
        2: [_row(tid=2, cov=50.0, work_area=20.0)],                                      # AREA_GT_WORK
        3: [_row(tid=3, cov=50.0, work_duration=600)],                                   # AREA_RATE (300 га/ч)
        4: [_row(tid=4, cov=50.0, work_duration=0)],                                     # AREA_NO_TIME
        5: [_row(tid=5, cov=50.0, work_duration=0, manually_defined_covered_area=True)], # AREA_MANUAL, без NO_TIME
    })
    assert _codes() == {"AREA_HOURLY", "AREA_GT_WORK", "AREA_RATE", "AREA_NO_TIME", "AREA_MANUAL"}


def test_crop_checks():
    _fresh()
    _hist(100, fid=10, crop=7)
    _hist(101, fid=99, crop=7)           # история чужого поля
    _hist(102, fid=10, crop=0)           # без crop_id
    _hist(103, fid=10, crop=8)           # другая культура того же поля и года
    sn.on_mtfmi({
        1: [_row(tid=1, hid=None)],                  # CROP_NO_HISTORY
        2: [_row(tid=2, hid=555)],                   # CROP_UNRESOLVED (не найден)
        3: [_row(tid=3, hid=102)],                   # CROP_UNRESOLVED (без culture)
        4: [_row(tid=4, hid=101)],                   # CROP_FIELD
        5: [_row(tid=5, hid=100)],
        6: [_row(tid=6, hid=103)],                   # CROP_SPLIT
    })
    assert _codes() == {"CROP_NO_HISTORY", "CROP_UNRESOLVED", "CROP_FIELD", "CROP_SPLIT"}
    assert sn._N["CROP_UNRESOLVED"] == 2


def test_crops_not_loaded_is_not_flagged():
    _fresh()
    sn.on_mtfmi({1: [_row()]}, crops_known=False)
    assert "CROP_UNRESOLVED" not in _codes()
    assert sn._N["CROP_UNRESOLVED"] == 0


def test_task_level():
    _fresh()
    _hist()
    sn.on_tasks([
        {"id": 1, "covered_area": 100.0},                                    # Σ строк 50 → TASK_SUM
        {"id": 2, "covered_area": 40.0},                                     # нет строк → TASK_NO_ROWS (по requested)
        {"id": 3, "covered_area": 0.0},                                      # строки есть → TASK_ROWS_ONLY
        {"id": 4, "covered_area": 10.0, "start_time": "2026-09-02T10:00:00+03:00",
         "end_time": "2026-09-02T08:00:00+03:00"},                           # TASK_TIME
        {"id": 5, "covered_area": 50.0},                                     # OK
    ])
    sn.on_mtfmi({1: [_row(tid=1)], 3: [_row(tid=3)], 5: [_row(tid=5)]}, requested=[1, 2, 3, 5])
    assert _codes() == {"TASK_SUM", "TASK_NO_ROWS", "TASK_ROWS_ONLY", "TASK_TIME"}


def test_duplicate_rows_and_unknown_field():
    _fresh()
    _hist()
    sn.on_mtfmi({1: [_row(), _row()], 2: [_row(tid=2, fid=777, hid=100)]})
    assert {"ROW_DUP", "FIELD_UNKNOWN"} <= _codes()


def test_harvest_on_fallow(monkeypatch=None):
    _fresh()
    _hist()
    sn._HARVEST_WT = frozenset({20})
    import mtfmi_batch_fetch as mb

    mb._HISTORY_CROP[100] = "Чистый пар"
    try:
        sn.on_tasks([{"id": 1, "work_type_id": 20, "covered_area": 50.0}])
        sn.on_mtfmi({1: [_row()]})
        assert "CROP_WORK" in _codes()
    finally:
        mb._HISTORY_CROP.pop(100, None)
        sn._HARVEST_WT = None


def test_each_task_checked_once():
    _fresh()
    _hist()
    m = {1: [_row(cov=-1.0)]}
    sn.on_mtfmi(m)
    sn.on_mtfmi(m)
    assert sn._N["AREA_BAD"] == 1


def test_settled_and_dirty_roundtrip():
    _fresh()
    assert not sn.settled(100, True) and not sn.settled(100, False)
    _hist(100, crop=7)
    _hist(101, crop=0)
    assert sn.settled(100, True) and not sn.settled(100, False)
    assert sn.settled(101, False)                       # без культуры — не перекачиваем
    dirty = dict(sn.take_dirty())
    assert set(dirty) == {100, 101} and not sn.has_dirty()
    assert sn.unpack_hist(dirty[100]) == (7, 2026, 10)


def test_report_output_and_disabled():
    _fresh()
    _hist()
    sn.on_mtfmi({1: [_row(cov=-1.0)]})
    buf = io.StringIO()
    sn.report(buf)
    out = buf.getvalue()
    assert "DATA_CHECK [CRIT]" in out and "AREA_BAD" in out and "итог" in out
    sn.ENABLED = False
    try:
        sn.reset()
        sn.on_mtfmi({1: [_row(cov=-1.0)]})
        assert not sn._N
        assert sn.settled(5, True) is True and sn.settled(5, False) is False  # прежнее поведение
    finally:
        sn.ENABLED = True


def test_store_roundtrip():
    """history_meta переживает перезапуск: хранилище отдаёт год/поле/культуру без запросов к API."""
    import os
    import shutil
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="sanity_store_"))
    os.environ["SVODKI_TASK_STORE_DIR"] = str(tmp)
    try:
        import svodki_task_store as ts
        import season_mismatch_log as sml

        _fresh()
        _hist(100, fid=10, crop=7, year=2026)
        _hist(101, fid=11, crop=0, year=2025)
        st = ts.TaskStore(tmp, "http://x/api/v3")
        st._save_crops({100: "Пшеница"})
        assert not sn.has_dirty()
        n = st._db().execute("SELECT COUNT(*) FROM history_meta").fetchone()[0]
        assert n == 2, n

        sn.reset()
        sml._HISTORY_YEAR.clear()
        st2 = ts.TaskStore(tmp, "http://x/api/v3")
        st2._load_meta(st2._db())
        assert sn.unpack_hist(sn._HIST[100]) == (7, 2026, 10)
        assert sn.settled(101, False)          # без культуры — уже просмотрен
        assert sml._HISTORY_YEAR.get(100) == 2026   # проверка сезонов снова видит год
        st2._db().close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        os.environ.pop("SVODKI_TASK_STORE_DIR", None)


def _bulk(n):
    _fresh({i: 100.0 for i in range(5000)})
    for h in range(1, 5001):
        _hist(h, fid=h - 1, crop=3)
    return {t: [_row(tid=t, fid=t % 5000, hid=t % 5000 + 1, cov=50.0,
                     covered_area_hourly=[["t", 25.0], ["t", 25.0]], work_duration=14400, work_area=52.0)]
            for t in range(1, n + 1)}


def test_task_season_warns_when_outside_policy(monkeypatch=None):
    import os
    from datetime import date

    _fresh()
    os.environ["REPORT_DATE"] = "2026-10-01"
    os.environ["SVODKI_REPORT_ID"] = "5"
    try:
        from season_unified import resolve_group_season_config

        cfg = resolve_group_season_config("4.0 Посев", date(2026, 10, 1))
        allowed = cfg.get("task_season_years") or []
        if len(allowed) < 1:
            return
        bad = str(max(int(y) for y in allowed) + 1)
        sn.on_tasks([{"id": 9001, "work_type_id": 4, "season": bad, "covered_area": 10.0}])
        assert "TASK_SEASON" in _codes()
    finally:
        os.environ.pop("REPORT_DATE", None)
        os.environ.pop("SVODKI_REPORT_ID", None)


def test_wt_unmapped():
    _fresh()
    sn.on_tasks([
        {"id": 1, "work_type_id": 84, "covered_area": 50.0},    # известен через overrides
        {"id": 2, "work_type_id": 9999, "covered_area": 30.0},  # неизвестен -> WT_UNMAPPED
        {"id": 3, "work_type_id": 9998, "covered_area": 0.2},   # ниже порога
        {"id": 4, "work_type_id": 55, "covered_area": 10.0},    # известен
    ])
    assert sn._N["WT_UNMAPPED"] == 1
    assert "work_type=9999" in sn._EX["WT_UNMAPPED"][0]


def test_crop_silage_only_wt_84():
    _fresh()
    _hist()
    sn.on_tasks([{"id": 1, "work_type_id": 84, "covered_area": 12.0}])
    sn.on_mtfmi({1: [_row(tid=1, cov=12.0, work_type_id=84)]})
    sn.report()
    assert "CROP_SILAGE" in _codes()
    sn.reset()
    _fresh()
    _hist()
    sn.on_tasks([{"id": 2, "work_type_id": 74, "covered_area": 12.0}])
    sn.on_mtfmi({2: [_row(tid=2, cov=12.0)]})
    sn.report()
    assert "CROP_SILAGE" not in _codes()


def test_speed():
    data = _bulk(200_000)
    t0 = time.perf_counter()
    sn.on_mtfmi(data)
    dt = time.perf_counter() - t0
    print(f"\n  200k строк: {dt:.2f} с ({dt / 0.2:.1f} мкс/строка)")
    assert sn._STAT["rows"] == 200_000 and not sn._N
    assert dt < 3.0


def test_memory():
    """Доп. память — только множество проверенных заданий (~40 байт/задание), строки не копируются."""
    import tracemalloc

    data = _bulk(50_000)
    tracemalloc.start()
    sn.on_mtfmi(data)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"\n  50k строк: пик доп. памяти {peak / 1e6:.1f} МБ")
    assert peak < 6e6


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("OK  ", name)
            except Exception as exc:  # noqa: BLE001
                fails += 1
                import traceback

                print("FAIL", name, repr(exc))
                traceback.print_exc()
    sys.exit(1 if fails else 0)
