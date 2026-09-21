#!/usr/bin/env python3
"""Unit-тест: группировка regionRows по подразделениям агрофирм."""
from __future__ import annotations

import sys
from pathlib import Path

PLAN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLAN))

from proverka.cropwise_ui import parse_work_report_body  # noqa: E402
from plans import FIRM_ORDER  # noqa: E402

COLUMNS = [
    {"name": "Группа полей / Культура / Поле / Задание машины", "key": "name"},
    {"name": "Площадь, га", "key": "area"},
    {"name": "Внесение удобрений, га", "key": "fact"},
]

BODY = {
    "reportBlank": False,
    "regionColumns": COLUMNS,
    "regionRows": [
        {
            "name": "Аксу-Агро",
            "fact": "436,37",
            "children": [
                {"name": 'ООО "Агрофирма 4"', "fact": "436,37", "children": []},
                {"name": 'ООО "Аксу-Агро"', "fact": "0", "children": []},
            ],
        },
        {
            "name": "НурлатАгро",
            "fact": "38,10",
            "children": [
                {"name": 'ООО "Агрофирма 14"', "fact": "0", "children": []},
                {"name": 'ООО "Агрофирма 15"', "fact": "38,10", "children": []},
            ],
        },
        {
            "name": "ЗУ ПОД ЗДАНИЯМИ",
            "fact": "100,00",
            "children": [{"name": "АФЧ (под зданиями)", "fact": "100,00", "children": []}],
        },
        {"name": "Всего", "fact": "474,47", "children": []},
    ],
    "cropRows": [],
    "cropColumns": [],
}


def main() -> None:
    parsed = parse_work_report_body(BODY, FIRM_ORDER, ["Внесен"])
    by_firm = parsed["fact_by_firm"]
    assert by_firm.get("Аксу-Агро") == 436.37, by_firm
    assert by_firm.get("НурлатАгро") == 38.10, by_firm
    assert parsed["fact_total_ha"] == 474.47
    assert "ЗУ ПОД ЗДАНИЯМИ" not in str(by_firm)
    print("OK: subdivision grouping + skip ZU")


if __name__ == "__main__":
    main()
