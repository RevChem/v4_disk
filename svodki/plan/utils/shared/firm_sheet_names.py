"""Имена вкладок Excel = как заголовок фирмы в PDF."""
from __future__ import annotations

FIRM_LABELS_TABLE: dict[str, str] = {
    "НурлатАгро": "Нурлат Агро",
}

FIRM_DISPLAY_NAMES: dict[str, str] = {
    "АФ Чистопольская": 'ООО АФ "ЧИСТОПОЛЬСКАЯ"',
    "АгроРаздолье": 'ООО "АГРОРАЗДОЛЬЕ"',
    "Аксу-Агро": 'ООО "АКСУ АГРО"',
    "НурлатАгро": 'ООО "НУРЛАТ АГРО"',
    "Дрожжаное Рассвет Агро": 'ООО "ДРОЖЖАНОЕ РАССВЕТ АГРО"',
    "Актай": 'ООО "АКТАЙ"',
    "Лениногорская Птицефабрика": 'ООО "ЛЕНИНОГОРСКАЯ ПТИЦЕФАБРИКА"',
}


def firm_sheet_tab_name(firm_key: str) -> str:
    return (
        FIRM_LABELS_TABLE.get(firm_key)
        or FIRM_DISPLAY_NAMES.get(firm_key)
        or firm_key
    )[:31]
