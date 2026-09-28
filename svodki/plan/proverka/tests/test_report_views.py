"""report_views: тематические сводки 3–9."""
from __future__ import annotations

from proverka.report_views import build_focus_report_view, build_report_views


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
    return {
        "reports": {
            "1": {
                "svodka": {"operations": {group: op1}},
                "file_crops": {group: {"Соя": 59.0}},
                "file_firms": {group: {"АФ А": 59.0, "АФ Б": 40.0}},
            },
            "2": {
                "svodka": {"operations": {group: op2}},
                "operation_comparisons": {
                    group: {"svodka_op_local": op2, "comparison": {}},
                },
                "file_crops": {group: {"Соя": 58.0, "Рапс яровой": 40.0}},
                "file_firms": {group: {"АФ А": 58.0, "АФ Б": 40.0}},
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
                        "firms": [
                            {"firm": "АФ А", "svodka_ha": 60.0, "cropwise_ha": 55.0, "status": "DIFF"},
                        ],
                    },
                    {
                        "group": group,
                        "crop": "Рапс яровой",
                        "svodka_ha": 40.0,
                        "cropwise_ha": 40.0,
                        "diff_ha": 0.0,
                        "status": "OK",
                        "firms": [],
                    },
                ],
                "comparison": {"cropwise_by_firm": {"АФ А": 55.0, "АФ Б": 40.0}},
                "file_crops": {group: {"Соя": 60.0}},
                "file_firms": {group: {"АФ А": 60.0, "АФ Б": 40.0}},
            },
        },
    }


def test_report3_view_includes_peers_1_and_2():
    view = build_focus_report_view("3", _batch_mu())
    assert view is not None
    assert "1" in view["peer_ids"]
    assert "2" in view["peer_ids"]


def test_report3_crop_table_has_module_file_for_peers():
    view = build_focus_report_view("3", _batch_mu())
    soya = next(r for r in view["crops"] if r["crop"] == "Соя")
    assert soya["values"]["m_3"] == 60.0
    assert soya["values"]["f_3"] == 60.0
    assert soya["values"]["m_2"] == 58.0
    assert soya["values"]["f_2"] == 58.0
    assert soya["values"]["m_1"] == 99.0
    assert soya["values"]["f_1"] == 59.0
    assert soya["values"]["cropwise"] == 55.0
    assert soya["status"] == "DIFF"


def test_build_report_views_includes_3_to_9_when_selected():
    batch = _batch_mu()
    assert "3" in build_report_views(batch, None, ["3", "4"])
    assert build_report_views(batch, None, ["4", "5"]) == {}
