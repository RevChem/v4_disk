from pathlib import Path

from app.legacy.plans_editor import render_hardcoded_plan_source

SAMPLE = '''\
HARDCODED_PLAN: dict[str, dict[str, dict[str, float]]] = {
    # ── группа ──
    "2.4 Вспашка": {
        "АФ Чистопольская": {
            "Соя": 10,
            "Кукуруза на зерно": 734,
        },
    },
}
'''


def test_updates_number_and_inserts_crop():
    updated, changed = render_hardcoded_plan_source(
        SAMPLE,
        {
            "2.4 Вспашка": {
                "АФ Чистопольская": {
                    "Соя": 10,
                    "Кукуруза на зерно": 15,
                    "Кукуруза на силос": 719,
                }
            }
        },
    )
    assert changed == 2
    assert '"Кукуруза на зерно": 15' in updated
    assert '"Кукуруза на силос": 719' in updated
    assert "# ── группа ──" in updated
    assert updated.index("Кукуруза на силос") < updated.index("},")


def test_real_plans_roundtrip_is_unchanged():
    path = Path(__file__).resolve().parents[2] / "svodki" / "plan" / "plans.py"
    source = path.read_text(encoding="utf-8")
    import ast

    tree = ast.parse(source)
    plan = None
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "HARDCODED_PLAN":
            plan = ast.literal_eval(node.value)
    assert isinstance(plan, dict)
    updated, changed = render_hardcoded_plan_source(source, plan)
    assert changed == 0
    assert updated == source


# ── вставка новых элементов, когда у последнего элемента нет запятой ──────────────────────
import ast  # noqa: E402

import pytest  # noqa: E402

from app.legacy.plans_editor import _find_hardcoded_plan, _source_contains_plan, extract_base_plans  # noqa: E402


def _as_dict(source: str) -> dict:
    return ast.literal_eval(_find_hardcoded_plan(ast.parse(source)))


NO_TRAILING_COMMAS = '''\
HARDCODED_PLAN = {
    "g": {
        "f": {
            "a": 1  # без запятой, комментарий с } внутри
        }
    }
}
'''

ONE_LINER = 'HARDCODED_PLAN = {"g": {"f": {"a": 1}}}\n'


@pytest.mark.parametrize(
    "source",
    [NO_TRAILING_COMMAS, ONE_LINER],
    ids=["no-trailing-commas", "one-liner"],
)
def test_insert_without_trailing_commas(source):
    new_plan = {"g": {"f": {"a": 1, "b": 2}, "f2": {"c": 3}}, "g2": {"x": {"y": 4}}}
    updated, changed = render_hardcoded_plan_source(source, new_plan)
    assert changed == 3  # новые b, c, y; a не изменилось
    assert _as_dict(updated) == new_plan
    assert _source_contains_plan(updated, new_plan)


def test_real_plans_new_group_firm_and_crop():
    """Последняя группа в реальном plans.py закрыта без запятой — вставка после неё ломала файл."""
    path = Path(__file__).resolve().parents[2] / "svodki" / "plan" / "plans.py"
    source = path.read_text(encoding="utf-8")
    plan = extract_base_plans()["base"]["HARDCODED_PLAN"]
    group = next(iter(plan))
    firm = next(iter(plan[group]))

    import copy

    new_plan = copy.deepcopy(plan)
    new_plan[group][firm]["Тестовая культура"] = 77
    new_plan[group]["Новая АФ"] = {"Тестовая культура": 5.5}
    new_plan["9.9 Новая группа"] = {"Новая АФ": {"Тестовая культура": 1}}

    updated, changed = render_hardcoded_plan_source(source, new_plan)
    assert changed == 3
    assert _as_dict(updated) == new_plan
    assert _source_contains_plan(updated, new_plan)


EDITOR_SAMPLE = '''\
HARDCODED_PLAN = {
    "1.1 Внесение минеральных удобрений": {
        "АФ Чистопольская": {
            "Подсолнечник": 1,
            "Соя": 2,
        },
    },
    "2.4 Вспашка": {
        "АФ Чистопольская": {
            "Соя": 10,
        },
    },
}
AVAILABLE_OWN = {
    "2.4 Вспашка": {
        "АФ Чистопольская": 1,
    },
}
PLAN_RATE_PER_GROUP = {
    "2.4 Вспашка": 40,
}
CROP_DISPLAY_RANK = {}
CROP_FIRM_HIDDEN = {}
CROP_REPORT_MODE_EDITOR = {}
WORK_GROUPS = [
    ("2.4 Вспашка", [49]),
]
'''


def test_persist_writes_every_operation_and_keeps_json(tmp_path, monkeypatch):
    """Любая правка всех групп работ сразу попадает в plans.py, JSON не стирается."""
    from app.config import settings
    from app.legacy import plans_editor as pe

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    (plan_dir / "plans.py").write_text(EDITOR_SAMPLE, encoding="utf-8")
    edits_path = tmp_path / "plans_edits.json"
    original_json = '{"HARDCODED_PLAN": {"2.4 Вспашка": {"АФ Чистопольская": {"Соя": 99}}}}'
    edits_path.write_text(original_json, encoding="utf-8")
    monkeypatch.setattr(pe, "_svodki_plan_dir", lambda: plan_dir)
    monkeypatch.setattr(settings, "svodki_plans_edits_path", str(edits_path))
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "uploads"))

    note = pe._persist_editor_to_plans_py(
        {
            "HARDCODED_PLAN": {
                "1.1 Внесение минеральных удобрений": {
                    "АФ Чистопольская": {"Подсолнечник": 3367, "Соя": 3457},
                },
                "2.4 Вспашка": {"АФ Чистопольская": {"Соя": 99}},
            },
            "AVAILABLE_OWN": {"2.4 Вспашка": {"АФ Чистопольская": 8}},
            "PLAN_RATE_PER_GROUP": {"2.4 Вспашка": 41},
            "CROP_DISPLAY_RANK": {"2.4 Вспашка": {"Соя": 3}},
            "CROP_FIRM_HIDDEN": {"2.4 Вспашка": {"АФ Чистопольская": {"Соя": 1}}},
            "WORK_GROUPS": [
                {"name": "2.4 Вспашка", "ids": [49, 57]},
                {"name": "2.1 Боронование", "ids": [12]},
            ],
        }
    )
    assert not str(note).startswith("Не удалось")
    written = (plan_dir / "plans.py").read_text(encoding="utf-8")
    assert '"Подсолнечник": 3367' in written
    assert '"Соя": 99' in written
    assert '"АФ Чистопольская": 8' in written
    assert '"2.4 Вспашка": 41' in written
    assert '"Соя": 3' in written
    assert "[49, 57]" in written
    assert "2.1 Боронование" in written
    assert edits_path.read_text(encoding="utf-8") == original_json


def test_sync_writes_plans_py_and_keeps_editor_json(tmp_path, monkeypatch):
    """Синхронизация копирует план в plans.py и не переписывает JSON редактора."""
    from app.config import settings
    from app.legacy import plans_editor as pe

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    (plan_dir / "plans.py").write_text(SAMPLE, encoding="utf-8")
    edits_path = tmp_path / "plans_edits.json"
    original = (
        '{"HARDCODED_PLAN": {"2.4 Вспашка": {"АФ Чистопольская": {"Соя": 99}}}, '
        '"AVAILABLE_OWN": {"2.4 Вспашка": {"АФ Чистопольская": 3}}}'
    )
    edits_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(pe, "_svodki_plan_dir", lambda: plan_dir)
    monkeypatch.setattr(settings, "svodki_plans_edits_path", str(edits_path))
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "uploads"))

    note = pe._sync_hardcoded_plan_note(
        {
            "HARDCODED_PLAN": {
                "2.4 Вспашка": {
                    "АФ Чистопольская": {
                        "Соя": 99,
                        "Кукуруза на зерно": 734,
                    }
                }
            }
        }
    )
    assert not str(note).startswith("Не удалось")
    written = (plan_dir / "plans.py").read_text(encoding="utf-8")
    assert '"Соя": 99' in written
    assert edits_path.read_text(encoding="utf-8") == original


def test_season_year_ranges_stay_in_editor_json(tmp_path, monkeypatch):
    from app.config import settings
    from app.legacy.season_meta import load_season_range_overrides, set_season_range_override

    path = tmp_path / "season_ranges.json"
    monkeypatch.setattr(settings, "svodki_season_ranges_path", str(path))
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    set_season_range_override(
        "1.1 Внесение минеральных удобрений",
        season_years=[2026, 2027],
        year_ranges={
            "2026": {"period_start": "2026-03-01", "period_end": "2026-06-30"},
            "2027": {"period_start": "2026-07-01", "period_end": "2027-03-01"},
        },
    )
    stored = load_season_range_overrides()["1.1 Внесение минеральных удобрений"]
    assert stored["season_years"] == [2026, 2027]
    assert stored["year_ranges"]["2026"]["period_start"] == "2026-03-01"
    assert stored["year_ranges"]["2027"]["period_end"] == "2027-03-01"
