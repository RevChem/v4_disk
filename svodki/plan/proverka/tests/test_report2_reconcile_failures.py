"""Сбой одной операции в сводке 1/2 не должен ронять всю сверку."""
from __future__ import annotations

from datetime import date

import pytest

from plan_paths import ensure_plan_sys_path

ensure_plan_sys_path()

from proverka import report2_reconcile as r2  # noqa: E402
from proverka.reports_config import REPORTS  # noqa: E402

GROUPS = ["2.1 Боронование", "2.9 Дискование", "5.0 Уборка"]


class _FakeShiftCache:
    @classmethod
    def build(cls, *a, **k):
        return cls()


def _ok_result(group_name, **_kw):
    return {
        "group_name": group_name,
        "svodka_op": {"total_ha": 10.0, "area_today_by_firm": {}},
        "comparison": {"total_diff_ha": 0.0},
        "cropwise_work_report": {"ok": True},
    }


@pytest.fixture()
def patched(monkeypatch):
    ops = {g: {"total_ha": 10.0, "area_today_by_firm": {}, "work_type_ids": [1]} for g in GROUPS}

    async def no_canon(*a, **k):
        return {}

    monkeypatch.setattr(r2, "operations_from_svodka", lambda sv: dict(ops))
    monkeypatch.setattr(r2, "_load_canonical_svodka_ops", no_canon)
    monkeypatch.setattr(r2, "ShiftWindowCache", _FakeShiftCache)
    monkeypatch.setattr(r2, "_collect_all_wt_ids", lambda o: [1])
    return ops


def _run(workers, monkeypatch):
    monkeypatch.setattr(r2, "_max_op_workers", lambda n: workers)
    spec = REPORTS["2"]
    return r2.reconcile_per_operation(
        spec, date(2026, 9, 25), "", ["АФ А"], None,
        prefetched_svodka={"operations": {}}, prefetched_work={},
    )


@pytest.mark.parametrize("workers", [1, 4], ids=["sequential", "threaded"])
def test_one_failing_operation_does_not_abort_the_rest(workers, patched, monkeypatch):
    def fake(group_name, op, **kw):
        if group_name == "2.9 Дискование":
            raise KeyError("season_cfg")
        return _ok_result(group_name)

    monkeypatch.setattr(r2, "_reconcile_one_operation", fake)
    out = _run(workers, monkeypatch)

    comps = out["operation_comparisons"]
    assert set(comps) == set(GROUPS)  # ни одна операция не потеряна
    bad = comps["2.9 Дискование"]
    assert bad["exception"] is True
    assert "KeyError" in bad["error"] and "season_cfg" in bad["error"]
    assert "Traceback" in bad["traceback"]
    assert bad["cropwise_work_report"]["ok"] is False
    assert "comparison" not in bad
    for g in ("2.1 Боронование", "5.0 Уборка"):
        assert comps[g]["cropwise_work_report"]["ok"] is True

    cw = out["cropwise_work_report"]
    assert cw["ok"] is False  # сводка в целом не «ok»
    assert cw["operations_total"] == 3
    assert cw["operations_ok"] == 2
    assert cw["operations_error"] == 1
    assert cw["operations_exception"] == 1


def test_all_operations_failing_still_returns_a_result(patched, monkeypatch):
    def fake(group_name, op, **kw):
        raise RuntimeError("cookie протухла")

    monkeypatch.setattr(r2, "_reconcile_one_operation", fake)
    out = _run(4, monkeypatch)
    assert out["cropwise_work_report"]["operations_exception"] == 3
    assert out["cropwise_work_report"]["operations_ok"] == 0


@pytest.mark.parametrize("workers", [1, 4], ids=["sequential", "threaded"])
def test_cancellation_is_not_swallowed(workers, patched, monkeypatch):
    from proverka.runner import ProverkaCancelled

    def fake(group_name, op, **kw):
        raise ProverkaCancelled("cancelled")

    monkeypatch.setattr(r2, "_reconcile_one_operation", fake)
    with pytest.raises(ProverkaCancelled):
        _run(workers, monkeypatch)


def test_failed_operation_is_visible_in_summary_and_violations(patched, monkeypatch):
    from proverka.summary import build_rows, compute_violations

    def fake(group_name, op, **kw):
        if group_name == "2.1 Боронование":
            raise ValueError("нет данных")
        return _ok_result(group_name)

    monkeypatch.setattr(r2, "_reconcile_one_operation", fake)
    out = _run(1, monkeypatch)
    rows = build_rows({"report_date": "2026-09-25", "reports": {"2": out}})
    failed = [r for r in rows if r.get("operation") == "2.1 Боронование" and not r.get("is_crop_row")]
    assert failed and failed[0]["status"] == "ERROR"
    assert any("нет данных" in w for w in failed[0]["warnings"])

    viol = compute_violations(rows, [])
    assert [v["status"] for v in viol["violations"] if v["kind"] == "cropwise"] == ["ERROR"]
    assert "Сбой сверки операции" in viol["violations"][0]["detail"]
    assert viol["error_logs"]
