"""Группировка организаций Namolot по агрофирме холдинга. Сеть не нужна."""
from app.application.namolot_view import OTHER_FIRM, holding_firm, record_rows, summarize


def test_api_organization_names_map_to_holding():
    cases = {
        "ЧИСТОПОЛЬСКАЯ АГРОФИРМА ООО": "АФ Чистопольская",
        "АГРОФИРМА 2 ООО": "АФ Чистопольская",
        "АГРОРАЗДОЛЬЕ ООО": "АгроРаздолье",
        "АКСУ АГРО АФ ООО": "Аксу-Агро",
        "АГРОФИРМА 4 ООО": "Аксу-Агро",
        "АПК АКТАЙ ООО": "Актай",
        "ДРОЖЖАНОЕ РАССВЕТ АГРО ООО": "Дрожжаное Рассвет Агро",
        "АГРОФИРМА 5 ООО": "Дрожжаное Рассвет Агро",
        "ЛЕНИНОГОРСКАЯ ПТИЦЕФАБРИКА ООО": "Лениногорская Птицефабрика",
        "АГРОФИРМА 14 ООО": "НурлатАгро",
        "АГРОФИРМА 15 ООО": "НурлатАгро",
        "Элеватор Текэ Тау ООО": OTHER_FIRM,
        "НУРЛАТСКИЙ ЭЛЕВАТОР ЗАО": OTHER_FIRM,
        "ЧИСТАЙАГРОТОРГ ООО": OTHER_FIRM,
    }
    for organization, firm in cases.items():
        assert holding_firm(organization) == firm, organization


def test_user_hierarchy_labels_map_to_holding():
    cases = {
        'АФ "Чистопольская"': "АФ Чистопольская",
        'ООО "Агрофирма 2"': "АФ Чистопольская",
        "ЧО Полив": "АФ Чистопольская",
        'ООО "АгроРаздолье"': "АгроРаздолье",
        'ООО "Агрофирма 3"': "АгроРаздолье",
        'ООО "Агрофирма 4"': "Аксу-Агро",
        'ООО "Аксу-Агро"': "Аксу-Агро",
        'ООО "Актай"': "Актай",
        'ООО "Агрофирма 5"': "Дрожжаное Рассвет Агро",
        'ООО "Дрожжаное Рассвет Агро"': "Дрожжаное Рассвет Агро",
        "Лениногорская Птицефабрика": "Лениногорская Птицефабрика",
        'ООО "Агрофирма 14"': "НурлатАгро",
        'ООО "Агрофирма 14"/ 1': "НурлатАгро",
        'ООО "Агрофирма 15"': "НурлатАгро",
    }
    for organization, firm in cases.items():
        assert holding_firm(organization) == firm, organization


def test_summarize_groups_tonnes_under_firm():
    view = summarize(
        [
            {"organization": "АГРОФИРМА 2 ООО", "crop": "Соя", "quantity": 2500},
            {"organization": "ЧИСТОПОЛЬСКАЯ АГРОФИРМА ООО", "crop": "Соя", "quantity": 7500},
            {"organization": "Элеватор Текэ Тау ООО", "crop": "Рапс яровой", "quantity": 1000},
        ]
    )
    assert view["records"] == 3
    assert view["tonnes"] == 11.0
    chistopol = view["firms"][0]
    assert chistopol["firm"] == "АФ Чистопольская"
    assert chistopol["tonnes"] == 10.0
    assert chistopol["organizations"][0]["organization"] == "ЧИСТОПОЛЬСКАЯ АГРОФИРМА ООО"
    assert view["firms"][-1]["firm"] == OTHER_FIRM


def test_record_rows_keep_plate_surname_and_msk_time():
    rows = record_rows(
        [
            {
                "organization": "АГРОРАЗДОЛЬЕ ООО",
                "crop": "Соя",
                "quantity": 5700,
                "combine_harvester_id": "АН 2824 16",
                "machine_operator": "Пискеев Александр Павлович",
                "spp": "АФ3-06-13 Рапс яровой 26г.",
                "date": "2026-10-15T11:30:00+00:00",
            }
        ]
    )
    assert rows[0]["firm"] == "АгроРаздолье"
    assert rows[0]["plate"] == "АН 2824 16"
    assert rows[0]["surname"] == "Пискеев"
    assert rows[0]["tonnes"] == 5.7
    assert rows[0]["at"] == "2026-10-15T14:30:00"
    assert rows[0]["field"].startswith("АФ3-06-13")
