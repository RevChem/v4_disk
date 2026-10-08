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
