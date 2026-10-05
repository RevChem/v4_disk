"""
Сезоны в «Проверка сводок» при SQLite-хранилище machine_tasks.

Хранилище отдаёт все season в окне end_time; фильтр machine_task.season
остаётся в модулях сводки (6_main_3._filter_tasks_by_season) и в proverka
extractors через task_season_years из season_overrides / редактора.

Запуск: python3 proverka/tests/test_season_task_store.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path

PLAN = Path(__file__).resolve().parents[2]
for extra in (PLAN, PLAN / "utils" / "season", PLAN / "utils" / "shared"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import plans  # noqa: F401,E402

TZ = timezone.utc
ISO = "%Y-%m-%dT%H:%M:%S"


def _filter_tasks_by_season(tasks, allowed_seasons: set[str] | None):
    """Тот же контракт, что 6_main_3._filter_tasks_by_season (после выборки из store)."""
    if not allowed_seasons:
        return list(tasks)
    return [t for t in tasks if str(t.get("season") or "") in allowed_seasons]


def test_six_main_filter_after_store_query():
    """Как при SQLite: в выборке оба season, в расчёт попадает только разрешённый."""
    tasks = [
        {"id": 1, "season": "2026", "covered_area": 100.0, "work_type_id": 64},
        {"id": 2, "season": "2027", "covered_area": 200.0, "work_type_id": 64},
    ]
    only_2027 = _filter_tasks_by_season(tasks, {"2027"})
    assert [t["id"] for t in only_2027] == [2]
    both = _filter_tasks_by_season(tasks, {"2026", "2027"})
    assert sorted(t["id"] for t in both) == [1, 2]


def test_task_store_sql_season_param():
    """query_tasks с season=… — тот же контракт, что при API-фильтре по season."""
    try:
        import httpx  # noqa: F401
    except ImportError as exc:
        raise AssertionError(f"httpx required: {exc}") from exc
    import svodki_task_store as ts

    tmp = Path(tempfile.mkdtemp(prefix="season_store_"))
    os.environ["SVODKI_TASK_STORE_DIR"] = str(tmp)
    os.environ["SVODKI_STORE_MAX_AGE_SEC"] = "0"
    try:
        store = ts.get_store("http://mock/api/v3")
        wt = 64
        t0 = time.time()
        end_2026 = t0 - 3600
        end_2027 = t0 - 1800
        store._put_task(
            {
                "id": 101,
                "work_type_id": wt,
                "end_time": datetime.fromtimestamp(end_2026, tz=TZ).strftime(ISO),
                "season": "2026",
            }
        )
        store._put_task(
            {
                "id": 102,
                "work_type_id": wt,
                "end_time": datetime.fromtimestamp(end_2027, tz=TZ).strftime(ISO),
                "season": "2027",
            }
        )
        lo = end_2026 - 86400
        hi = t0 + 60
        store.cov_lo[wt] = lo

        async def _run():
            c = httpx.AsyncClient()
            sem = asyncio.Semaphore(4)
            rows = await store.query_tasks(
                c,
                sem,
                [wt],
                {
                    "end_time_gt_eq": datetime.fromtimestamp(lo, tz=TZ).strftime(ISO),
                    "end_time_lt": datetime.fromtimestamp(hi, tz=TZ).strftime(ISO),
                    "season": "2027",
                },
            )
            await c.aclose()
            return rows

        rows = asyncio.run(_run())
        assert sorted(r["id"] for r in rows) == [102]
    finally:
        os.environ.pop("SVODKI_TASK_STORE_DIR", None)
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def test_proverka_run_override_changes_spray_seasons():
    """season_overrides из job → resolve_group_season в extract_6."""
    import season_unified as su

    store = {
        "calendar_seasons": {},
        "by_group": {
            "3.1 Опрыскивание": {"season_years": [2026, 2027]},
            "3.2 Опрыскивание (овощи)": {"season_years": [2026]},
        },
    }
    rd = date(2026, 10, 1)
    original = su.load_season_store
    su.load_season_store = lambda _rd, reload=False: store  # noqa: ARG005
    su.clear_season_cache()
    try:
        from proverka.season_runtime import resolve_group_season

        spray = resolve_group_season(
            "3.1 Опрыскивание",
            rd,
            run_override={"task_season_years": [2027], "by_group": store["by_group"]},
        )
        assert spray["task_season_years"] == [2026, 2027]
    finally:
        su.load_season_store = original
        su.clear_season_cache()


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ok  {name}")
        except AssertionError as exc:
            if "httpx required" in str(exc):
                print(f"SKIP  {name}: {exc}")
            else:
                failed += 1
                print(f"FAIL  {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}: {exc!r}")
    print("Провалено:" if failed else "Все тесты пройдены.", failed or "")
    sys.exit(1 if failed else 0)
