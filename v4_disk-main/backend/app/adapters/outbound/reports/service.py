"""Адаптер генерации отчётов поверх legacy report_runner."""
from __future__ import annotations

from datetime import date

from app.legacy.report_generation import ReportGenerationError, list_report_catalog_for_ui
from app.legacy.report_runner import (
    convert_xlsx_file_to_pdf,
    generate_reports_to_disk,
    list_available_dates,
    list_generated_files,
)

__all__ = [
    "ReportGenerationError",
    "convert_xlsx_file_to_pdf",
    "generate_reports_to_disk",
    "list_available_dates",
    "list_generated_files",
    "list_report_catalog_for_ui",
]
