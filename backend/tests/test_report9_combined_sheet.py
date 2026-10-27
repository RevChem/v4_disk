"""Шапка PDF сводки 9 для листа суммы вспашки и глубокорыхления."""
from app.legacy.report_generation import (
    _is_detail_sheet_without_data,
    _report9_pdf_sheet_headers,
    _report9_plow_crop_from_sheet,
)


class _Sheet:
    def cell(self, *_args, **_kwargs):
        return None


def test_combined_sheet_is_not_a_crop_page_and_keeps_both_work_types():
    name = "Вспашка + Глубокорыхление"
    assert _report9_plow_crop_from_sheet(name) is None
    assert _report9_plow_crop_from_sheet("Вспашка Соя") == "Соя"
    title, work = _report9_pdf_sheet_headers(name)
    assert title == "ВЫРАБОТКА ТЕХНИКИ ПО АГРОФИРМАМ"
    assert work == "ВИД РАБОТ: 2.4 Вспашка + 2.8 Глубокорыхление — Сводно по всем агрофирмам"
    assert _is_detail_sheet_without_data(_Sheet(), name) is False
