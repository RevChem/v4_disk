"""Сверка операций между сводками — локальный факт, не эталон."""
from __future__ import annotations

from proverka.operation_compare import (
    build_cross_check,
    operation_for_cross_check,
    operation_for_proverka_display,
    _operation_total_ha,
)
from proverka.summary import _row_from_per_operation


def test_proverka_display_prefers_local_over_canon():
    local = {"total_ha": 1854.68, "area_today_by_firm": {"АФ А": 1854.68}}
    canon = {"total_ha": 1893.31, "area_today_by_firm": {"АФ А": 1893.31}}
    oc = {"svodka_op": canon, "svodka_op_local": local, "comparison": {"total_diff_ha": 0}}
    op = operation_for_proverka_display(oc)
    assert _operation_total_ha(op) == 1854.68
    row = _row_from_per_operation("1", "2.9 Дискование", oc)
    assert row["svodka_ha"] == 1854.68


def test_operation_for_cross_check_prefers_local_over_canon():
    local = {"total_ha": 770.73, "area_today_by_firm": {"АФ А": 770.73}}
    canon = {"total_ha": 761.10, "area_today_by_firm": {"АФ А": 761.10}}
    report = {
        "svodka": {
            "operations": {
                "5.0 Уборка": canon,
            },
        },
        "operation_comparisons": {
            "5.0 Уборка": {
                "svodka_op": canon,
                "svodka_op_local": local,
            },
        },
    }
    op = operation_for_cross_check(report, "2", "5.0 Уборка")
    assert op is not None
    assert _operation_total_ha(op) == 770.73


def test_build_cross_check_uses_total_ha_not_only_firm_sum():
    batch = {
        "reports": {
            "11": {
                "svodka": {
                    "operations": {
                        "5.0 Уборка": {
                            "total_ha": 761.10,
                            "area_today_by_firm": {"АФ А": 700.0, "АФ Б": 50.0},
                        },
                    },
                },
            },
            "10": {
                "svodka": {
                    "operations": {
                        "5.0 Уборка": {
                            "total_ha": 770.73,
                            "area_today_by_firm": {"АФ А": 700.0, "АФ Б": 60.0},
                        },
                    },
                },
            },
        },
    }
    cc = build_cross_check(batch)
    row = next(r for r in cc["operations"] if r["group_name"] == "5.0 Уборка")
    assert row["status"] == "DIFF"
    assert row["totals"]["11"] == 761.10
    assert row["totals"]["10"] == 770.73
    assert row["max_diff_ha"] == round(770.73 - 761.10, 2)
