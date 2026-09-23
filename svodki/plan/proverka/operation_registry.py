"""Какие сводки должны совпадать по одной операции (work group)."""
from __future__ import annotations

# group_name -> report IDs, где есть факт за смену по этой операции
OPERATION_PEERS: dict[str, tuple[str, ...]] = {
    "1.1 Внесение минеральных удобрений": ("1", "2", "3"),
    "2.1 Боронование": ("1", "2", "4"),
    "2.9 Дискование": ("1", "2", "7"),
    "2.4 Вспашка": ("1", "2", "9"),
    "2.8 Глубокорыхление": ("1", "2", "9"),
    "2.11 Культивация": ("1", "2", "8"),
    "4.0 Посев": ("1", "2", "5"),
    "3.1 Опрыскивание": ("1", "2", "6"),
    "3.2 Опрыскивание (овощи)": ("1", "2", "6"),
    "5.0 Уборка": ("1", "2", "11", "10", "13"),
    "4.1 Посев (овощи)": ("1", "2"),
    "2.12 Прикатывание": ("1", "2"),
    "2.7 Гребнеобразование": ("1", "2"),
    "2.6 Фрезерование": ("1", "2"),
    "5.4 Уборка картофеля, лука": ("1", "2", "10"),
}

# Сверка кол-ва машин: для уборки эталон — сводка 14 (выработка комбайнов),
# а не 10/13 (там другая семантика счётчиков / нет зерновых комбайнов).
MACHINE_OPERATION_PEER_OVERRIDES: dict[str, tuple[str, ...]] = {
    "5.0 Уборка": ("1", "2", "11", "14"),
}

def peer_reports_for_machine_group(group_name: str) -> tuple[str, ...]:
    if group_name in MACHINE_OPERATION_PEER_OVERRIDES:
        return MACHINE_OPERATION_PEER_OVERRIDES[group_name]
    return peer_reports_for_group(group_name)


# Имена операций так, как их показывает Cropwise. Внутренний ключ группы не
# меняем: он лежит в season_ranges.json, в названиях листов Excel и в правках
# PDF, поэтому переименование потеряло бы уже сохранённые настройки.
CROPWISE_DISPLAY_NAMES: dict[str, str] = {
    "3.2 Опрыскивание (овощи)": "3.2 Опрыскивание на овощах",
}


def display_group_name(group_name: str) -> str:
    """Название операции для отчётов сверки — как в Cropwise."""
    return CROPWISE_DISPLAY_NAMES.get(group_name, group_name)


def peer_reports_for_group(group_name: str) -> tuple[str, ...]:
    return OPERATION_PEERS.get(group_name, ("1", "2"))


def peer_reports_for_crop_cross_check(group_name: str) -> tuple[str, ...]:
    """Те же peer-сводки, что и для итога по операции; строка по культуре — только если разбивка есть ≥2 сводках."""
    return peer_reports_for_group(group_name)


def canonical_cropwise_report(group_name: str) -> str | None:
    """Тематическая сводка (3, 4, …) для сравнения операции с work.json."""
    from proverka.reports_config import REPORTS

    for rid in peer_reports_for_group(group_name):
        if rid in ("1", "2"):
            continue
        spec = REPORTS.get(rid)
        if not spec or not spec.cropwise_comparable or spec.cropwise_per_operation:
            continue
        if spec.work_groups and group_name not in spec.work_groups:
            continue
        return rid
    return None


def all_cross_check_groups() -> list[str]:
    return list(OPERATION_PEERS.keys())
