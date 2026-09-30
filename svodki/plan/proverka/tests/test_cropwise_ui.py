"""
Регрессия на разбор work.json.

Каждый кейс здесь — реально наблюдавшийся способ потерять гектары,
из-за которого отчёт показывал, например, 1545 га при 1766 га в Cropwise.

Запуск:  python3 -m pytest proverka/tests -q
   или:  python3 proverka/tests/test_cropwise_ui.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "cropwise_ui_under_test", Path(__file__).resolve().parents[1] / "cropwise_ui.py",
)
cui = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(cui)

FIRMS = ["Агрофирма А", "Агрофирма Б"]
HINTS = ["Минераль"]


def _body() -> dict:
    """Дерево с вложенной строкой «Всего» и заданием, которого нет в API."""
    return {
        "regionColumns": [
            {"key": "name", "name": "Культура / Поле"},
            {"key": "f", "name": "Минеральные удобрения, га"},
        ],
        "regionRows": [
            {"name": "Всего", "f": "1 766"},
            {"name": "Агрофирма А", "f": "1 200", "children": [
                {"name": "ООО «Агрофирма 1»", "f": "1 200", "children": [
                    {"name": "Поле 1 #101", "f": "700"},
                    {"name": "Поле 2 #102", "f": "500"},
                    {"name": "Всего", "f": "1 200"},
                ]},
            ]},
            {"name": "Агрофирма Б", "f": "566", "children": [
                {"name": "Поле 3 #103", "f": "566"},
            ]},
        ],
        "cropRows": [],
        "cropColumns": [],
    }


def test_numbers_with_unicode_spaces() -> None:
    assert cui._parse_ha_number("1\u202f766,35") == 1766.35   # узкий неразрывный
    assert cui._parse_ha_number("1\u00a0766") == 1766.0       # неразрывный
    assert cui._parse_ha_number("1 545") == 1545.0
    assert cui._parse_ha_number("1.282,35") == 1282.35
    assert cui._parse_ha_number("—") == 0.0


def test_nested_total_row_does_not_override_root() -> None:
    parsed = cui.parse_work_report_body(_body(), FIRMS, HINTS)
    assert parsed["fact_total_ha"] == 1766.0


def test_orphan_tasks_stay_in_total_and_are_flagged() -> None:
    # #103 нет в API (задание закрылось после cutoff) — гектары обязаны остаться.
    parsed = cui.parse_work_report_body(_body(), FIRMS, HINTS, valid_task_ids={101, 102})
    assert parsed["fact_total_ha_verified"] == 1766.0
    assert parsed["ui_orphan_ha"] == 566.0
    assert parsed["ui_balance"]["balanced"]
    assert parsed["ui_balance"]["residual_ha"] == 0.0


def test_detail_rows_have_no_duplicates() -> None:
    parsed = cui.parse_work_report_body(_body(), FIRMS, HINTS)
    leaves = [d for d in parsed["detail_rows"] if not d.get("aggregate")]
    assert round(sum(d["area_ha"] for d in leaves), 2) == 1766.0


def test_unmatched_firm_hectares_are_reported() -> None:
    body = _body()
    body["regionRows"].append({"name": "Неизвестное хозяйство", "f": "100"})
    parsed = cui.parse_work_report_body(body, FIRMS, HINTS)
    assert parsed["unmatched_firm_ha"] == 100.0


def test_ambiguous_firm_name_is_not_guessed() -> None:
    firms = ["Нива", "Нива-Юг"]
    assert cui._match_firm("Нива", firms) == "Нива"
    assert cui._match_firm("Нива-Юг", firms) == "Нива-Юг"


def test_fact_column_confidence_is_exposed() -> None:
    parsed = cui.parse_work_report_body(_body(), FIRMS, HINTS)
    assert parsed["fact_column_confidence"] == "hint"
    guessed = cui.parse_work_report_body(_body(), FIRMS, ["НетТакогоХинта"])
    assert guessed["fact_column_confidence"] in ("guess", "area", "none")


def _grouped_body() -> dict:
    """
    Колонка Cropwise — группа видов работ, ячейка — список по work_type_names.

    Так отдаёт work.json, если в запросе были work_type_ids сразу двух операций
    группы «Обработка СЗР»: 3.1 Опрыскивание и 3.2 Опрыскивание на овощах.
    """
    return {
        "regionColumns": [
            {"key": "name", "name": "Группа полей / Культура / Поле / Задание машины"},
            {"key": "area", "name": "Площадь, га"},
            {
                "key": "Обработка СЗР",
                "name": "Обработка СЗР, га",
                "work_type_names": [
                    "3.1 Опрыскивание",
                    "3.2 Опрыскивание на овощах",
                ],
            },
        ],
        "regionRows": [
            {"name": "Всего", "Обработка СЗР": ["3 501.98", "185.66"]},
            {
                "name": "Агрофирма А",
                "Обработка СЗР": ["3 501.98", "185.66"],
                "children": [{"name": "Поле 1 #101", "Обработка СЗР": ["3 501.98", "185.66"]}],
            },
        ],
        "cropRows": [],
        "cropColumns": [],
    }


def test_group_column_work_types_are_exposed() -> None:
    # Без этого поля склейка 3.1+3.2 в одно число была не видна в отчёте.
    parsed = cui.parse_work_report_body(_grouped_body(), FIRMS, ["Обработ"])
    assert parsed["fact_column"] == "Обработка СЗР, га"
    assert parsed["fact_work_types"] == [
        "3.1 Опрыскивание",
        "3.2 Опрыскивание на овощах",
    ]
    # Сумма группы: сверять с одной операцией такое число нельзя.
    assert parsed["fact_total_ha"] == 3687.64


def test_empty_firm_list_accepts_subgroup_mapped_to_parent() -> None:
    """Оценка work.json разбирает отчёт с пустым списком фирм."""
    body = {
        "regionColumns": [
            {"key": "name", "name": "Группа полей"},
            {"key": "f", "name": "Уборка, га"},
        ],
        "regionRows": [
            {"name": "Всего", "f": "100"},
            {
                "name": "ЧО Полив",
                "f": "100",
                "children": [{"name": "Поле 1", "f": "100"}],
            },
        ],
        "cropRows": [],
        "cropColumns": [],
    }
    cui._SUBGROUP_TO_FIRM = {cui._normalize_name("ЧО Полив"): "АФ Чистопольская"}
    try:
        parsed = cui.parse_work_report_body(body, [], ["Уборка"])
        score = cui._work_response_score({"ok": True, "body": body}, ["Уборка"])
    finally:
        cui._SUBGROUP_TO_FIRM = None
    assert parsed["fact_by_firm"]["АФ Чистопольская"] == 100.0
    assert parsed["fact_total_ha"] == 100.0
    assert score[1] == 100.0


def test_harvest_columns_uborka_and_kormozagotovka_are_summed() -> None:
    body = {
        "regionColumns": [
            {"key": "name", "name": "Группа полей"},
            {"key": "area", "name": "Площадь, га"},
            {
                "key": "Уборка",
                "name": "Уборка, га",
                "work_type_names": [
                    "5.2 Уборка зерновых и зернобобовых",
                    "5.8 Уборка рапса, горчицы, гречихи и проса",
                ],
            },
            {
                "key": "Кормозаготовка",
                "name": "Кормозаготовка, га",
                "work_type_names": ["6.3 Скашивание с измельчением кукурузы"],
            },
        ],
        "regionRows": [
            {"name": "Всего", "Уборка": ["349.78", "1 752.99"], "Кормозаготовка": ["95.78"]},
            {"name": "Агрофирма А", "Уборка": ["349.78", "1 752.99"], "Кормозаготовка": ["95.78"]},
        ],
        "cropRows": [],
        "cropColumns": [],
    }
    parsed = cui.parse_work_report_body(
        body, FIRMS, ["Убор", "Кормозаготов", "Скашиван", "Обработ"],
    )
    assert parsed["fact_total_ha"] == 2198.55
    assert "6.3 Скашивание с измельчением кукурузы" in parsed["fact_work_types"]


def test_group_column_keeps_only_the_named_operation() -> None:
    """Колонка-группа не должна отдавать вспашку в глубокорыхление и 3.1 в овощи."""
    veg = cui.parse_work_report_body(
        _grouped_body(), FIRMS, ["овощ", "3.2", "Обработ"],
    )
    assert veg["fact_total_ha"] == 185.66
    assert veg["fact_work_types"] == ["3.2 Опрыскивание на овощах"]
    assert veg["fact_by_firm"]["Агрофирма А"] == 185.66

    spray = cui.parse_work_report_body(
        _grouped_body(), FIRMS, ["3.1", "Опрыскивание", "Опрыск", "Обработ"],
    )
    assert spray["fact_total_ha"] == 3501.98
    assert spray["fact_work_types"] == ["3.1 Опрыскивание"]

    tillage = {
        "regionColumns": [
            {"key": "name", "name": "Группа полей"},
            {
                "key": "Обработка почвы",
                "name": "Обработка почвы, га",
                "work_type_names": ["2.4 Вспашка", "2.8 Глубокорыхление"],
            },
        ],
        "regionRows": [
            {"name": "Всего", "Обработка почвы": ["6.69", "2.43"]},
            {"name": "Агрофирма А", "Обработка почвы": ["6.69", "2.43"]},
        ],
        "cropRows": [],
        "cropColumns": [],
    }
    deep = cui.parse_work_report_body(
        tillage, FIRMS, ["Глубокорых", "Глубок", "2.8", "Обработ"],
    )
    assert deep["fact_total_ha"] == 2.43
    assert deep["fact_by_firm"]["Агрофирма А"] == 2.43
    plow = cui.parse_work_report_body(tillage, FIRMS, ["Вспаш", "2.4", "Обработ"])
    assert plow["fact_total_ha"] == 6.69


def test_single_work_type_group_column_is_the_operation_total() -> None:
    # Запрос work.json по wt одной операции оставляет в группе только её вид работ.
    body = _grouped_body()
    body["regionColumns"][2]["work_type_names"] = ["3.1 Опрыскивание"]
    for row in body["regionRows"]:
        row["Обработка СЗР"] = ["3 501.98"]
        for ch in row.get("children") or []:
            ch["Обработка СЗР"] = ["3 501.98"]
    parsed = cui.parse_work_report_body(body, FIRMS, ["Обработ"])
    assert parsed["fact_work_types"] == ["3.1 Опрыскивание"]
    assert parsed["fact_total_ha"] == 3501.98


if __name__ == "__main__":
    import sys

    failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ok  {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {name}: {exc}")
    print("Провалено:" if failed else "Все тесты пройдены.", failed or "")
    sys.exit(1 if failed else 0)
