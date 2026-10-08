"""Правка WORK_GROUPS из редактора остаётся списком пар (название, id)."""
from __future__ import annotations

from utils.season._apply_plans_edits import apply_plans_edits_data


def test_editor_work_groups_stay_name_id_pairs() -> None:
    sandbox = {
        "WORK_GROUPS": [("4.0 Посев", [3, 4]), ("5.0 Уборка", [6])],
        "ALL_WT_IDS": {3, 4},
        "VEG_WT_IDS": set(),
    }
    apply_plans_edits_data(
        {
            "WORK_GROUPS": [
                {"name": "4.0 Посев", "ids": [3, 4, 159, "160", 161, 163]},
                {"name": "5.0 Уборка", "ids": [6, 7, 111]},
                {"name": "4.1 Посев (овощи)", "ids": [68, 148]},
            ]
        },
        sandbox,
    )
    groups = sandbox["WORK_GROUPS"]
    assert groups == [
        ("4.0 Посев", [3, 4, 159, 160, 161, 163]),
        ("5.0 Уборка", [6, 7, 111]),
        ("4.1 Посев (овощи)", [68, 148]),
    ]
    # Раньше dict(список словарей) схлопывался в {"name": "ids"}.
    assert dict(groups)["4.0 Посев"] == [3, 4, 159, 160, 161, 163]
    names = [name for name, _ids in groups]
    assert "name" not in names
    assert sandbox["ALL_WT_IDS"] == {3, 4, 159, 160, 161, 163}
    assert sandbox["VEG_WT_IDS"] == {68, 148}

    # Тот же разбор, что в 11_main_harvest._wt_ids_from_plans1.
    harvest = {
        wt
        for gname, wt_ids in groups
        if gname == "5.0 Уборка"
        for wt in wt_ids
    }
    assert harvest == {6, 7, 111}


def test_already_paired_work_groups_roundtrip() -> None:
    sandbox = {"WORK_GROUPS": [("2.9 Дискование", [60])], "ALL_WT_IDS": set(), "VEG_WT_IDS": set()}
    apply_plans_edits_data(
        {"WORK_GROUPS": [("2.9 Дискование", [60, 61])]},
        sandbox,
    )
    assert sandbox["WORK_GROUPS"] == [("2.9 Дискование", [60, 61])]
