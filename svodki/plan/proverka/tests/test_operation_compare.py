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


def test_build_cross_check_crop_rows_when_two_peers_have_breakdown():
    batch = {
        "reports": {
            "1": {
                "svodka": {
                    "operations": {
                        "4.0 Посев": {
                            "total_ha": 100.0,
                            "area_today_by_firm": {"АФ А": 100.0},
                            "area_today_by_firm_crop": {
                                "Рапс яровой": {"АФ А": 40.0},
                                "Пшеница озимая": {"АФ А": 60.0},
                            },
                        },
                    },
                },
            },
            "5": {
                "svodka": {
                    "operations": {
                        "4.0 Посев": {
                            "total_ha": 100.0,
                            "area_today_by_firm": {"АФ А": 100.0},
                            "area_today_by_firm_crop": {
                                "Рапс яровой": {"АФ А": 35.0},
                                "Пшеница озимая": {"АФ А": 65.0},
                            },
                        },
                    },
                },
            },
        },
    }
    cc = build_cross_check(batch)
    row = next(r for r in cc["operations"] if r["group_name"] == "4.0 Посев")
    rapeseed = next(c for c in row["crops"] if c["crop"] == "Рапс яровой")
    assert rapeseed["status"] == "DIFF"
    assert rapeseed["max_diff_ha"] == 5.0
    wheat = next(c for c in row["crops"] if c["crop"] == "Пшеница озимая")
    assert wheat["status"] == "DIFF"


def test_sub_half_ha_crop_diff_is_ok():
    batch = {
        "reports": {
            "1": {
                "svodka": {
                    "operations": {
                        "4.0 Посев": {
                            "total_ha": 100.0,
                            "area_today_by_firm": {"АФ А": 100.0},
                            "area_today_by_firm_crop": {
                                "Рапс яровой": {"АФ А": 40.0},
                            },
                        },
                    },
                },
            },
            "5": {
                "svodka": {
                    "operations": {
                        "4.0 Посев": {
                            "total_ha": 100.0,
                            "area_today_by_firm": {"АФ А": 100.0},
                            "area_today_by_firm_crop": {
                                "Рапс яровой": {"АФ А": 40.3},
                            },
                        },
                    },
                },
            },
        },
    }
    cc = build_cross_check(batch)
    row = next(r for r in cc["operations"] if r["group_name"] == "4.0 Посев")
    rapeseed = next(c for c in row["crops"] if c["crop"] == "Рапс яровой")
    assert rapeseed["status"] == "OK"


def test_spray_crop_rows_when_two_peers_have_breakdown():
    batch = {
        "reports": {
            "1": {
                "svodka": {
                    "operations": {
                        "3.1 Опрыскивание": {
                            "total_ha": 1000.0,
                            "area_today_by_firm": {"АФ А": 1000.0},
                            "area_today_by_firm_crop": {
                                "Рапс яровой": {"АФ А": 727.1},
                            },
                        },
                    },
                },
            },
            "2": {
                "svodka": {
                    "operations": {
                        "3.1 Опрыскивание": {
                            "total_ha": 1000.0,
                            "area_today_by_firm": {"АФ А": 1000.0},
                            "area_today_by_firm_crop": {
                                "Рапс яровой": {"АФ А": 728.0},
                            },
                        },
                    },
                },
            },
        },
    }
    cc = build_cross_check(batch)
    row = next(r for r in cc["operations"] if r["group_name"] == "3.1 Опрыскивание")
    rapeseed = next(c for c in row["crops"] if c["crop"] == "Рапс яровой")
    assert rapeseed["status"] == "DIFF"


def test_cross_check_crop_row_carries_one_cropwise_figure():
    batch = {
        "reports": {
            "1": {
                "svodka": {
                    "operations": {
                        "4.0 Посев": {
                            "total_ha": 100.0,
                            "area_today_by_firm": {"АФ А": 60.0, "АФ Б": 40.0},
                            "area_today_by_firm_crop": {
                                "Рапс яровой": {"АФ А": 40.0, "АФ Б": 10.0},
                            },
                        },
                    },
                },
                "operation_comparisons": {
                    "4.0 Посев": {
                        "comparison": {"cropwise_fact_total_ha": 90.0},
                        "crop_comparisons": [
                            {
                                "crop": "Рапс яровой",
                                "cropwise_ha": 35.0,
                                "firms": [
                                    {"firm": "АФ А", "cropwise_ha": 30.0},
                                    {"firm": "АФ Б", "cropwise_ha": 5.0},
                                ],
                            },
                        ],
                    },
                },
            },
            "2": {
                "svodka": {
                    "operations": {
                        "4.0 Посев": {
                            "total_ha": 100.0,
                            "area_today_by_firm": {"АФ А": 60.0, "АФ Б": 40.0},
                            "area_today_by_firm_crop": {
                                "Рапс яровой": {"АФ А": 40.0, "АФ Б": 10.0},
                            },
                        },
                    },
                },
                "operation_comparisons": {
                    "4.0 Посев": {
                        "comparison": {"cropwise_fact_total_ha": 1.0},
                        "crop_comparisons": [
                            {"crop": "Рапс яровой", "cropwise_ha": 1.0, "firms": []},
                        ],
                    },
                },
            },
            "5": {
                "svodka": {
                    "operations": {
                        "4.0 Посев": {
                            "total_ha": 100.0,
                            "area_today_by_firm": {"АФ А": 55.0, "АФ Б": 45.0},
                            "area_today_by_firm_crop": {
                                "Рапс яровой": {"АФ А": 45.0, "АФ Б": 10.0},
                            },
                        },
                    },
                },
            },
        },
    }
    cc = build_cross_check(batch)
    row = next(r for r in cc["operations"] if r["group_name"] == "4.0 Посев")
    assert row["status"] == "OK"
    assert row["cropwise_ha"] == 90.0
    rapeseed = next(c for c in row["crops"] if c["crop"] == "Рапс яровой")
    assert rapeseed["status"] == "DIFF"
    assert rapeseed["cropwise_ha"] == 35.0
    by_firm = {fr["firm"]: fr["cropwise_ha"] for fr in rapeseed["firms"]}
    assert by_firm["АФ А"] == 30.0
    assert by_firm["АФ Б"] == 5.0
