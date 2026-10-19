"""Чтение Excel сверки: секции на общем листе, обрезка имени и культуры с листа."""
from datetime import date

from openpyxl import Workbook

from proverka.harvest_check import (
    DEFAULT_TOLERANCE_HA,
    DEFAULT_TOLERANCE_SEASON_HA,
    SEASON,
    SPECS,
    _status,
    read_specs,
    tolerance_for_metric,
)
from proverka.operation_registry import OPERATION_PEERS

DAY = date(2026, 10, 16)
FIRM = "АФ Чистопольская"


def test_every_peer_operation_has_specs():
    assert set(SPECS) == set(OPERATION_PEERS)
    assert list(SPECS["5.0 Уборка"]) == ["1", "2", "10", "11", "13"]


def test_truncated_sheet_name_matches():
    spec = SPECS["1.1 Внесение минеральных удобрений"]["2"][0]
    full = "1.1 Внесение минеральных удобрений"
    assert len(full) > 31
    assert spec.matches(full[:31])
    assert not spec.matches("2.1 Боронование")


def test_tillage_sections_stay_separate():
    wb = Workbook()
    ws = wb.active
    ws.title = "Вспашка + Глубокорыхление"

    def block(title: str, area: float) -> None:
        ws.append([title])
        ws.append(["Агрофирмы", "Плановая выработка, га/сутки", "Количество", "Обработано за сутки, га"])
        ws.append([FIRM, 40, 1, area])
        ws.append(["Итого", 40, 1, area])
        ws.append([None])

    block("2.4 Вспашка", 10)
    block("2.8 Глубокорыхление", 3.5)

    plow, _ = read_specs(wb, SPECS["2.4 Вспашка"]["1"], DAY)
    deep, _ = read_specs(wb, SPECS["2.8 Глубокорыхление"]["1"], DAY)
    assert {f.value for f in plow} == {10}
    assert {f.value for f in deep} == {3.5}
    assert {f.firm for f in plow} == {FIRM, "ALL"}


def test_sowing_section_does_not_take_the_next_block():
    wb = Workbook()
    ws = wb.active
    ws.title = "Сводка"
    ws.append(["КУЛЬТУРЫ"])
    ws.append(["Агрофирма", "Общий объем выполненной\nработы за сутки, га"])
    ws.append([FIRM, 12])
    ws.append(["Итого", 12])
    ws.append([None])
    ws.append(["Посев / посадка овощных культур"])
    ws.append(["Агрофирма", "Общий объем выполненной\nработы за сутки, га"])
    ws.append([FIRM, 4])
    ws.append(["Итого", 4])

    cultures, _ = read_specs(wb, (SPECS["4.0 Посев"]["5"][0],), DAY)
    veg, _ = read_specs(wb, SPECS["4.1 Посев (овощи)"]["5"], DAY)
    assert {f.value for f in cultures} == {12}
    assert {f.value for f in veg} == {4}


def test_crop_prefix_from_sheet_title():
    wb = Workbook()
    ws = wb.active
    ws.title = "Вспашка Рапс озимый"
    ws.append(["Агрофирмы", "План, га", "Факт общий, га", "Факт за сутки, га"])
    ws.append([FIRM, 100, 40, 5])
    facts, _ = read_specs(wb, (SPECS["2.4 Вспашка"]["9"][2],), DAY)
    crops = {f.crop for f in facts}
    assert crops == {"Рапс озимый"}
    by_metric = {f.metric: f.value for f in facts if f.firm == FIRM}
    assert by_metric == {"season": 40, "day": 5}


def test_season_tolerance_is_wider_than_day():
    assert tolerance_for_metric("day", DEFAULT_TOLERANCE_HA, DEFAULT_TOLERANCE_SEASON_HA) == 0.5
    assert tolerance_for_metric(SEASON, 0.5, 10) == 10
    # 7 га — ошибка за сутки и норма за сезон
    assert _status({"1": 100}, 107, 0.5)[2] == "cropwise"
    assert _status({"1": 100}, 107, 10)[2] == "ok"
