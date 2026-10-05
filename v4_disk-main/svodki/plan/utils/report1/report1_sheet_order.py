"""Порядок листов Excel/PDF сводки 1 — как в эталоне «1.СВОДКА ХК Крупно ПРИМЕР.pdf»."""

from __future__ import annotations

from openpyxl.workbook.workbook import Workbook

# Как в report1_pdf_page_order.py — без import plans (backend грузит модуль без PYTHONPATH plan/).
AGROFIRM_SHEET_NAMES: tuple[str, ...] = (
    "АФ Чистопольская",
    "АгроРаздолье",
    "Аксу-Агро",
    "НурлатАгро",
    "Дрожжаное Рассвет Агро",
    "Актай",
    "Лениногорская Птицефабрика",
)

SPRAY_GROUP_NAME = "3.1 Опрыскивание"
VEG_SPRAY_GROUP_NAME = "3.2 Опрыскивание (овощи)"
PLOW_GROUP_NAME = "2.4 Вспашка"
DEEP_GROUP_NAME = "2.8 Глубокорыхление"

# Хвост «Выраб. — …» после листов по агрофирмам (стр. 32–35 эталона без листа 5.4).
REPORT1_PERF_TAIL_ORDER: tuple[str, ...] = (
    "2.1 Боронование",
    "2.9 Дискование",
    "2.6 Фрезерование",
    "2.7 Гребнеобразование",
)

FERT_TABLE_SHEET_TITLES: tuple[str, ...] = (
    "Удобрения — Рапс",
    "Удобрения — Пшеница",
    "Удобрения — Подсолнечник и соя",
    "Удобрения — Кукуруза",
    "Удобрения — Заготовка корма",
    "Удобрения — Пар",
    "Удобрения — Овощи",
)


def resolve_perf_sheet_name(wb: Workbook, suffix: str) -> str | None:
    """Имя листа «Выраб. — …» с учётом обрезки Excel до 31 символа."""
    expected = f"Выраб. — {suffix}"[:31]
    if expected in wb.sheetnames:
        return expected
    frag = suffix.strip()[:14].casefold()
    for sn in wb.sheetnames:
        if "выраб" not in sn.casefold():
            continue
        if frag and frag in sn.casefold():
            return sn
    return None


def build_report1_sheet_order(wb: Workbook) -> list[str]:
    """Целевой порядок листов PDF сводки 1 (эталон ПРИМЕР.pdf без стр. «Выраб. — 5.4»)."""
    existing = list(wb.sheetnames)
    order: list[str] = []

    def _add(name: str | None) -> None:
        if name and name in existing and name not in order:
            order.append(name)

    def _add_perf(suffix: str) -> None:
        _add(resolve_perf_sheet_name(wb, suffix))

    # 1: уборка (выработка); отдельный лист «Выраб. — 5.4» не включаем
    _add_perf("5.0 Уборка")
    # 2: уборка овощей (сводка 11); 3–4: намолот и овощи (сводка 10)
    _add("Сводка овощи")
    _add("Общий намолот")
    _add("Овощи")
    # 4: выработка посева
    _add_perf("4.0 Посев")
    # 5–9: сводные таблицы посева
    _add("Итого (все АФ)")
    for sn in sorted(n for n in existing if n.startswith("ЯрСев блок")):
        _add(sn)
    _add("Овощи — посев")
    # 10–11: культивация, прикатывание
    _add_perf("2.11 Культивация")
    _add_perf("2.12 Прикатывание")
    # 12: вспашка + глубокорыхление
    _add("Вспашка + Глубокорыхление")
    # 13–14: опрыскивание
    _add_perf(SPRAY_GROUP_NAME)
    _add_perf(VEG_SPRAY_GROUP_NAME)
    # 15–16: сводки по культурам и агрофирмам
    _add("Сводка по культурам")
    _add("Свод по агрофирмам")
    # 17–23: слайды удобрений
    for title in FERT_TABLE_SHEET_TITLES:
        _add(title[:31])
    # 24: выработка 1.1
    _add("Выработка техники")
    # 25–31: детализация по агрофирмам (1.1)
    for firm in AGROFIRM_SHEET_NAMES:
        _add(firm[:28])
    # 32–35: остальные «Выраб. — …»
    for suffix in REPORT1_PERF_TAIL_ORDER:
        _add_perf(suffix)
    for sn in existing:
        if sn not in order:
            order.append(sn)
    return order


def reorder_report1_workbook_sheets(wb: Workbook) -> bool:
    """
    Переставляет листы в порядок эталона «1.СВОДКА ХК Крупно ПРИМЕР.pdf».
    Возвращает True, если порядок изменился.
    """
    target_order = build_report1_sheet_order(wb)
    changed = list(wb.sheetnames) != target_order
    for target_idx, title in enumerate(target_order):
        if title not in wb.sheetnames:
            continue
        current_idx = wb.sheetnames.index(title)
        if current_idx != target_idx:
            wb.move_sheet(title, offset=target_idx - current_idx)
            changed = True
    return changed
