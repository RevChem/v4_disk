"""Группировка организаций Namolot по агрофирме холдинга. Сеть не нужна."""
from datetime import date

from app.application import namolot_view
from io import BytesIO

from openpyxl import load_workbook

from app.application.namolot_view import OTHER_FIRM, field_base_code, holding_firm, record_rows, summarize, table_excel


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


def test_triticale_masculine_name_is_winter_triticale():
    assert namolot_view.same_crop("Тритикале озимый", "Тритикале озимая")
    assert namolot_view.same_crop("Трикитакле озимый 26г.", "Тритикале озимая")
    assert namolot_view.canonical_crop("Кукуруза") == "Кукуруза на зерно"
    rows = namolot_view.record_rows(
        [
            {
                "organization": "АГРОФИРМА 15 ООО",
                "crop": "Тритикале озимый",
                "quantity": 351100,
                "date": "2026-07-20T10:00:00",
            }
        ]
    )
    assert rows[0]["crop"] == "Тритикале озимая"
    assert rows[0]["firm"] == "НурлатАгро"
    assert rows[0]["tonnes"] == 351.1


def test_brief_keeps_shift_and_season_for_one_firm_and_crop(monkeypatch):
    monkeypatch.setattr(namolot_view, "harvest_season_start", lambda _day: date(2026, 6, 10))
    records = [
        {"organization": "АГРОФИРМА 2 ООО", "crop": "Пшеница озимая", "quantity": 1000, "date": "2026-10-15T06:00:00"},
        {"organization": "АГРОФИРМА 2 ООО", "crop": "Пшеница озимая 26г.", "quantity": 500, "date": "2026-10-16T05:59:00"},
        {"organization": "АГРОФИРМА 2 ООО", "crop": "Пшеница озимая", "quantity": 9000, "date": "2026-10-16T06:00:00"},
        {"organization": "АГРОФИРМА 2 ООО", "crop": "Пшеница озимая", "quantity": 2000, "date": "2026-07-01T12:00:00"},
        {"organization": "АГРОФИРМА 2 ООО", "crop": "Пшеница озимая", "quantity": 8000, "date": "2026-06-10T05:59:00"},
        {"organization": "АГРОРАЗДОЛЬЕ ООО", "crop": "Пшеница озимая", "quantity": 7000, "date": "2026-10-15T12:00:00"},
        {"organization": "АГРОФИРМА 2 ООО", "crop": "Соя", "quantity": 6000, "date": "2026-10-15T12:00:00"},
    ]
    view = namolot_view.brief_from_records(records, date(2026, 10, 16), "АФ Чистопольская", "Пшеница озимая")
    assert view["day"]["tonnes"] == 1.5
    assert view["day"]["records"] == 2
    assert view["season"]["tonnes"] == 3.5
    assert view["season"]["records"] == 3
    assert view["day"]["from"] == "2026-10-15T06:00:00"
    assert view["day"]["to"] == "2026-10-16T05:59:00"
    assert view["season"]["from"] == "2026-06-10T06:00:00"
    assert len(view["rows"]) == 3


def test_field_code_puts_razdolye_row_on_the_holding_firm():
    assert field_base_code("АР-03-16 Соя 26г.") == "АР-03-16"
    assert field_base_code("*AP-03-16 Соя") == "АР-03-16"
    index = {"АР-03-16": "АгроРаздолье"}
    rows = record_rows(
        [
            {
                "organization": "Элеватор Текэ Тау ООО",
                "crop": "Соя",
                "quantity": 1500,
                "spp": "АР-03-16 Соя 26г.",
                "date": "2026-10-15T08:00:00",
            }
        ],
        index,
    )
    assert len(rows) == 1
    assert rows[0]["firm"] == "АгроРаздолье"
    assert rows[0]["field"] == "АР-03-16 Соя 26г."


def test_unknown_field_and_organization_are_not_other():
    rows = record_rows(
        [
            {
                "organization": "Элеватор Текэ Тау ООО",
                "crop": "Соя",
                "quantity": 1500,
                "spp": "НЕИЗВЕСТНО-1 Соя",
                "date": "2026-10-15T08:00:00",
            }
        ],
        {},
    )
    assert rows == []


def test_table_excel_keeps_filtered_rows_and_columns():
    payload = table_excel(
        "rows",
        [
            {
                "at": "2026-10-15T08:30:00",
                "firm": "АгроРаздолье",
                "crop": "Соя",
                "plate": "АН 2824 16",
                "operator": "Пискеев Александр",
                "field": "АР-03-16 Соя 26г.",
                "tonnes": 1.5,
            }
        ],
    )
    ws = load_workbook(BytesIO(payload)).active
    assert [cell.value for cell in ws[1]] == [
        "№",
        "Дата",
        "Агрофирма",
        "Культура",
        "Госномер",
        "Механизатор",
        "Поле",
        "Намолот, т",
    ]
    assert ws["C2"].value == "АгроРаздолье"
    assert ws["G2"].value == "АР-03-16 Соя 26г."
    assert ws["B2"].value == "15.10.2026 08:30"


def test_filter_record_rows_uses_the_same_optional_fields_as_the_button():
    rows = [
        {
            "at": "2026-08-01T07:15:00",
            "firm": "Актай",
            "crop": "Соя",
            "plate": "А 2824 В",
            "operator": "Пискеев Иван",
            "surname": "Пискеев",
            "tonnes": 12.5,
            "field": "П1",
        },
        {
            "at": "2026-08-02T04:00:00",
            "firm": "Актай",
            "crop": "Соя",
            "plate": "1111",
            "operator": "Иванов",
            "surname": "Иванов",
            "tonnes": 3,
            "field": "П2",
        },
        {
            "at": "2026-08-01T10:00:00",
            "firm": "Аксу-Агро",
            "crop": "Рапс яровой",
            "plate": "2824",
            "operator": "Пискеев",
            "surname": "Пискеев",
            "tonnes": 1,
            "field": "П3",
        },
        {
            "at": "2026-07-01T12:00:00",
            "firm": "Актай",
            "crop": "Соя",
            "plate": "0000",
            "operator": "Сидоров",
            "surname": "Сидоров",
            "tonnes": 9,
            "field": "П0",
        },
    ]
    assert namolot_view.filter_record_rows(rows) == rows
    assert len(namolot_view.filter_record_rows(rows, firm="Актай", crop="Соя")) == 3
    assert {row["field"] for row in namolot_view.filter_record_rows(rows, plate="2824")} == {"П1", "П3"}
    assert len(namolot_view.filter_record_rows(rows, surname="пискеев")) == 2
    window = namolot_view.filter_record_rows(
        rows,
        date_from="2026-08-01",
        time_from="06:00",
        date_to="2026-08-02",
        time_to="05:59",
    )
    assert {row["field"] for row in window} == {"П1", "П2", "П3"}
    assert namolot_view.filter_record_rows(rows, firm="Актай", surname="Иванов", plate="999") == []


def test_format_ask_table_keeps_the_total_of_hidden_rows():
    rows = [
        {
            "at": f"2026-08-01T06:{index:02d}:00",
            "firm": "Актай",
            "crop": "Соя",
            "plate": str(index),
            "operator": "Иванов",
            "surname": "Иванов",
            "field": "П",
            "tonnes": float(index),
        }
        for index in range(5)
    ]
    text, meta = namolot_view.format_ask_table(rows, max_rows=2)
    assert meta == {"rows_total": 5, "rows_sent": 2, "tonnes_total": 10.0, "truncated": True}
    assert "Итого: 5 записей, 10.000 т" in text
    assert "не показано 3" in text


def test_ask_records_without_model_returns_the_table():
    rows = [
        {
            "at": "2026-08-01T07:00:00",
            "firm": "Актай",
            "crop": "Соя",
            "plate": "1",
            "operator": "Иванов",
            "surname": "Иванов",
            "field": "П",
            "tonnes": 1.5,
        }
    ]
    out = namolot_view.ask_records(rows, "сколько тонн?", use_llm=False)
    assert out["llm_used"] is False
    assert out["rows_total"] == 1
    assert "1.500" in out["answer"]
    empty = namolot_view.ask_records([], "сколько тонн?", use_llm=True)
    assert empty["llm_used"] is False
    assert empty["answer"] == "По заданному фильтру данных нет."
