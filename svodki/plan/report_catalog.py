"""Единый каталог сводок: генерация PDF и proverka."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReportCatalogEntry:
    id: str
    script: str
    title: str
    module: str = ""
    picker_visible: bool = True
    default_selected: bool = False
    explicit_only: bool = False
    work_groups: tuple[str, ...] = ()
    season_rule: str = "none"
    fact_column_hints: tuple[str, ...] = ()
    fact_by_all_fields: bool = False
    cropwise_comparable: bool = False
    cropwise_per_operation: bool = False
    cross_check_only: bool = False


REPORT_CATALOG: tuple[ReportCatalogEntry, ...] = (
    ReportCatalogEntry("0", "0_main_status_vpr.py", "0. Статус ВПР"),
    ReportCatalogEntry("0.1", "0_1_main_status_vpr_morning.py", "0.1 Статус ВПР на утро кратко"),
    ReportCatalogEntry(
        "1",
        "1_main_ammonium__1_.py",
        "1.СВОДКА ХК Крупно",
        module="1_main_ammonium__1_",
        cropwise_per_operation=True,
    ),
    ReportCatalogEntry(
        "1.18",
        "1.18_main_ammonium__1__day.py",
        "1.18. СВОДКА ХК Крупно (за сутки)",
        module="1.18_main_ammonium__1__day",
        picker_visible=False,
    ),
    ReportCatalogEntry(
        "2",
        "2_main_ideal_compact.py",
        "2.Сводка ХК Детальная",
        module="2_main_ideal_compact",
        cropwise_per_operation=True,
    ),
    ReportCatalogEntry(
        "3",
        "3_main_real.py",
        "3.РАБОТА ТЕХНИКИ Внесение МУ",
        module="3_main_real",
        work_groups=("1.1 Внесение минеральных удобрений",),
        season_rule="both_years",
        fact_column_hints=("Минераль", "Удобрен", "Внесен"),
        cropwise_comparable=True,
    ),
    ReportCatalogEntry(
        "4",
        "4_main_fixed.py",
        "4.РАБОТА ТЕХНИКИ Боронование",
        module="4_main_fixed",
        work_groups=("2.1 Боронование",),
        fact_column_hints=("Борон", "Обработ"),
        cropwise_comparable=True,
    ),
    ReportCatalogEntry(
        "5",
        "5_main_fixed.py",
        "5.Посев 2026",
        module="5_main_fixed",
        work_groups=("4.0 Посев",),
        season_rule="year_plus_one",
        fact_column_hints=("Посев",),
        fact_by_all_fields=True,
        cropwise_comparable=True,
    ),
    ReportCatalogEntry(
        "6",
        "6_main_3.py",
        "6.РАБОТА ТЕХНИКИ Опрыскивание",
        module="6_main_3",
        work_groups=("3.1 Опрыскивание", "3.2 Опрыскивание (овощи)"),
        season_rule="year_plus_one",
        fact_column_hints=("Опрыск", "Обработ"),
        cropwise_comparable=True,
    ),
    ReportCatalogEntry(
        "7",
        "7_main__4_.py",
        "7.РАБОТА ТЕХНИКИ Дискование",
        module="7_main__4_",
        work_groups=("2.9 Дискование",),
        season_rule="both_years",
        fact_column_hints=("Диск", "Обработ"),
        cropwise_comparable=True,
    ),
    ReportCatalogEntry(
        "8",
        "8_main_fixed.py",
        "8.РАБОТА ТЕХНИКИ Культивация",
        module="8_main_fixed",
        work_groups=("2.11 Культивация",),
        fact_column_hints=("Обработка почвы", "Обработ"),
        cropwise_comparable=True,
    ),
    ReportCatalogEntry(
        "9",
        "9_main_fixed.py",
        "9.РАБОТА ТЕХНИКИ Вспашка и глубокорыхление",
        module="9_main_fixed",
        work_groups=("2.4 Вспашка", "2.8 Глубокорыхление"),
        season_rule="plow_year_plus_one",
        fact_column_hints=("Вспаш", "Глубок", "Обработ"),
        cropwise_comparable=True,
    ),
    ReportCatalogEntry(
        "10",
        "10_main_harvest.py",
        "10. Уборка культур по холдингу",
        module="10_main_harvest",
        work_groups=("5.4 Уборка картофеля, лука",),
        fact_column_hints=("Площадь", "Убор", "Обработ"),
        cropwise_comparable=True,
    ),
    ReportCatalogEntry(
        "11",
        "11_main_harvest.py",
        "11. Уборка",
        module="11_main_harvest",
        work_groups=("5.0 Уборка",),
        fact_column_hints=("Убор", "Кормозаготов", "Скашиван", "Обработ"),
        cropwise_comparable=True,
    ),
    ReportCatalogEntry("12", "12_main_harvest_idle.py", "12. Статистика простоя при уборке урожая"),
    ReportCatalogEntry(
        "13",
        "13_holding_overview.py",
        "13. Сводка обзор ХК",
        module="13_holding_overview",
        work_groups=("5.0 Уборка",),
        cross_check_only=True,
    ),
    ReportCatalogEntry(
        "14",
        "14.main.py",
        "14. Выработка комбайнов по агрофирмам",
        module="14.main",
        work_groups=("5.0 Уборка",),
        cross_check_only=True,
    ),
    ReportCatalogEntry("22", "text_svodka_2.py", "22. Выработка техники"),
    ReportCatalogEntry(
        "23",
        "23_analytics_svodka.py",
        "23. Аналитика по сводке за сутки",
        picker_visible=False,
    ),
    ReportCatalogEntry(
        "24",
        "24_menee_10.py",
        "24. Техника с выработкой от 0,5 до 10 га",
        default_selected=True,
        explicit_only=True,
    ),
    ReportCatalogEntry(
        "25",
        "25_meeting_works.py",
        "25. Сводка к совещанию по работам",
        picker_visible=False,
        explicit_only=True,
    ),
    ReportCatalogEntry(
        "26",
        "26_combine_season_avg.py",
        "26. Средняя выработка комбайнов за сезон",
        explicit_only=True,
    ),
    ReportCatalogEntry(
        "27",
        "27_mechanizator_rating.py",
        "27. Рейтинг механизаторов",
        explicit_only=True,
    ),
    ReportCatalogEntry(
        "27.1",
        "27.1_mechanizator_rating.py",
        "27.1. Рейтинг механизаторов (время)",
        picker_visible=False,
        explicit_only=True,
    ),
)

REPORT_ID_ALIASES: dict[str, str] = {"01": "0.1"}

EXPLICIT_ONLY_REPORT_IDS: frozenset[str] = frozenset(
    e.id for e in REPORT_CATALOG if e.explicit_only
)

REPORT_PICKER_HIDDEN_IDS: frozenset[str] = frozenset(
    e.id for e in REPORT_CATALOG if not e.picker_visible
)

REPORT_PICKER_DEFAULT_SELECTED_IDS: frozenset[str] = frozenset(
    e.id for e in REPORT_CATALOG if e.default_selected
) | frozenset(str(i) for i in range(1, 15))


def catalog_by_id() -> dict[str, ReportCatalogEntry]:
    return {e.id: e for e in REPORT_CATALOG}


def list_catalog_ids() -> list[str]:
    return [e.id for e in REPORT_CATALOG]
