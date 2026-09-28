"""report_views: карточки сводок 1–14."""
from __future__ import annotations

from proverka.report_views import (
    build_focus_report_view,
    build_report_views,
    _work_groups_for_focus,
)


def _batch_mu():
    group = "1.1 Внесение минеральных удобрений"
    op3 = {
        "total_ha": 100.0,
        "area_today_by_firm": {"АФ А": 60.0, "АФ Б": 40.0},
        "area_today_by_firm_crop": {
            "Соя": {"АФ А": 60.0},
            "Рапс яровой": {"АФ Б": 40.0},
        },
    }
    op2 = {
        "total_ha": 100.0,
        "area_today_by_firm": {"АФ А": 60.0, "АФ Б": 40.0},
        "area_today_by_firm_crop": {
            "Соя": {"АФ А": 58.0},
            "Рапс яровой": {"АФ Б": 40.0},
        },
    }
    op1 = {
        "total_ha": 99.0,
        "area_today_by_firm": {"АФ А": 99.0},
        "area_today_by_firm_crop": {"Соя": {"АФ А": 99.0}},
    }
    seed = "4.0 Посев"
    op5 = {
        "total_ha": 884.69,
        "area_today_by_firm": {"АФ А": 884.69},
        "area_today_by_firm_crop": {"Пшеница озимая": {"АФ А": 884.69}},
    }
    return {
        "reports": {
            "1": {
                "svodka": {
                    "operations": {
                        group: op1,
                        seed: {"total_ha": 884.0, "area_today_by_firm": {"АФ А": 884.0}},
                    }
                },
                "file_crops": {group: {"Соя": 59.0}, seed: {"Пшеница ozimая": 884.0}},
            },
            "2": {
                "svodka": {"operations": {group: op2, seed: op5}},
                "operation_comparisons": {
                    group: {"svodka_op_local": op2, "comparison": {}},
                },
                "file_crops": {group: {"Соя": 58.0}},
            },
            "3": {
                "svodka": {"operations": {group: op3}},
                "crop_comparisons": [
                    {
                        "group": group,
                        "crop": "Соя",
                        "svodka_ha": 60.0,
                        "cropwise_ha": 55.0,
                        "diff_ha": 5.0,
                        "status": "DIFF",
                        "firms": [],
                    },
                ],
                "file_crops": {group: {"Соя": 60.0}},
            },
            "5": {
                "svodka": {"operations": {seed: op5}},
                "crop_comparisons": [
                    {
                        "group": seed,
                        "crop": "Пшеница ozimая",
                        "cropwise_ha": 884.0,
                        "status": "OK",
                        "firms": [],
                    },
                ],
            },
        },
    }


def test_work_groups_for_report1_from_operations():
    from proverka.reports_config import REPORTS

    batch = _batch_mu()
    spec = REPORTS["1"]
    groups = _work_groups_for_focus("1", batch, spec)
    assert "4.0 Посев" in groups
    assert "1.1 Внесение минеральных удобрений" in groups


def test_report1_view_has_multiple_sections():
    view = build_focus_report_view("1", _batch_mu())
    assert view is not None
    assert len(view.get("sections") or []) >= 2


def test_report3_crop_table_has_module_file_for_peers():
    view = build_focus_report_view("3", _batch_mu())
    soya = next(r for r in view["crops"] if r["crop"] == "Соя")
    assert soya["values"]["m_3"] == 60.0
    assert soya["values"]["f_3"] == 60.0
    assert soya["values"]["m_2"] == 58.0


def test_build_report_views_all_selected_in_reports():
    batch = _batch_mu()
    views = build_report_views(batch, None, ["1", "2", "3", "5"])
    assert "1" in views
    assert "3" in views
    assert "5" in views
