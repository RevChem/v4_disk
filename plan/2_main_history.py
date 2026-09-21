"""
Сводка 2 — история нескольких сезонов (2023–2025 по умолчанию).

Базовый скрипт: 2_main_detail.py (полная версия с сервера, не plan/2_main_ideal_compact.py).

Локальный запуск (из svodki/plan или svodki/new):
  python 2_main_history.py

Переменные окружения:
  HISTORY_YEARS=2023,2024,2025
  HISTORY_SEASON_START_2025=2025-06-10
  HISTORY_REPORT_STAMP_2025=2025-12-31   — REPORT_DATE (TODAY); сутки отчёта = stamp − 1
  SVODKI_HK_COMPACT_MAX_DAYS=366
  OUTPUT_FILE=report2_history.xlsx
"""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import date
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import openpyxl

_HERE = Path(__file__).resolve().parent
_DETAIL_PATH = _HERE / "2_main_detail.py"

# (season_start, report_stamp) — stamp = TODAY / REPORT_DATE платформы
DEFAULT_SEASONS: dict[int, tuple[date, date]] = {
    2023: (date(2023, 3, 15), date(2024, 1, 1)),
    2024: (date(2024, 3, 15), date(2025, 1, 1)),
    2025: (date(2025, 6, 10), date(2025, 12, 31)),
}


def _load_detail_module():
    spec = importlib.util.spec_from_file_location("r2_detail", _DETAIL_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(
            f"Cannot load {_DETAIL_PATH}. "
            "Need 2_main_detail.py (full server version), not 2_main_ideal_compact.py."
        )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _parse_years() -> list[int]:
    raw = (os.environ.get("HISTORY_YEARS") or "2023,2024,2025").strip()
    years = [int(p.strip()) for p in raw.split(",") if p.strip()]
    if not years:
        raise ValueError("HISTORY_YEARS is empty")
    return years


def _season_bounds(year: int) -> tuple[date, date]:
    start_raw = (
        os.environ.get(f"HISTORY_SEASON_START_{year}")
        or os.environ.get(f"SVODKI_SEASON_START_{year}")
        or ""
    ).strip()
    stamp_raw = (
        os.environ.get(f"HISTORY_REPORT_STAMP_{year}")
        or os.environ.get(f"HISTORY_REPORT_DATE_{year}")
        or os.environ.get("REPORT_DATE")
        or ""
    ).strip()
    default = DEFAULT_SEASONS.get(year)
    if start_raw:
        start = date.fromisoformat(start_raw)
    elif default:
        start = default[0]
    else:
        start = date(year, 3, 15)
    if stamp_raw:
        stamp = date.fromisoformat(stamp_raw)
    elif default:
        stamp = default[1]
    else:
        stamp = date(year + 1, 1, 1)
    if start >= stamp:
        raise ValueError(f"Season {year}: start {start} must be before report stamp {stamp}")
    return start, stamp


def _reset_custom_window(r2) -> None:
    """Сброс REPORT_WINDOW_* после import — для полного сезона, не произвольного окна."""
    r2.CUSTOM_WINDOW_START = None
    r2.CUSTOM_WINDOW_END = None
    r2.CUSTOM_SHIFT_DATE_MIN = None
    r2.CUSTOM_SHIFT_DATE_MAX = None


def build_history_report() -> str:
    os.environ.pop("REPORT_WINDOW_START", None)
    os.environ.pop("REPORT_WINDOW_END", None)
    os.environ.setdefault("SVODKI_HK_COMPACT_MAX_DAYS", "366")
    os.environ.setdefault("SVODKI_HK_COMPACT_ONLY_NONEMPTY", "1")
    os.environ.pop("SVODKI_R23_WORK_DATA_BUNDLE_OUT", None)

    r2 = _load_detail_module()
    _reset_custom_window(r2)
    years = _parse_years()
    headers = {"X-User-Api-Token": r2.API_TOKEN}

    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    for year in years:
        start, stamp = _season_bounds(year)
        r2.START_DATE = start
        r2.TODAY = stamp
        os.environ["SVODKI_CROP_YEAR"] = str(year)
        os.environ["REPORT_DATE"] = stamp.isoformat()
        _reset_custom_window(r2)

        report_day = stamp - r2.timedelta(days=1)
        print(f"\n{'=' * 60}")
        print(f"Сезон {year}: {start} — {report_day} (REPORT_DATE={stamp})")
        print(f"{'=' * 60}\n")

        r2._apply_manual_plan_rates()
        (
            field_to_crop,
            field_to_firm,
            field_to_firm_all,
            field_area,
            machine_group,
            firm_plan_ha,
            firm_crop_area_ha,
        ) = r2.load_reference_data()

        work_data = r2.collect_work_data(
            field_to_crop,
            field_to_firm,
            field_to_firm_all,
            field_area,
            machine_group,
            headers,
        )
        if not work_data:
            print(f"  [!] Нет данных за {year}, пропускаем листы")
            continue

        display_dates = r2.compute_display_dates(work_data)
        print(
            f"  Групп работ: {len(work_data)}; "
            f"дней с данными (колонок): {len(display_dates)}"
        )
        harvest = work_data.get("5.0 Уборка")
        if harvest:
            total_ha = sum((harvest.get("area_by_firm_total") or {}).values())
            print(f"  5.0 Уборка (итого га): {round(total_ha, 1):,}")

        r2.write_report_sheet(
            wb,
            work_data,
            display_dates,
            firm_plan_ha,
            firm_crop_area_ha,
            sheet_title_prefix=f"{year} ",
        )

    if not wb.sheetnames:
        raise RuntimeError("No sheets generated — check HISTORY_YEARS and API access")

    out = (os.environ.get("OUTPUT_FILE") or "").strip()
    if not out:
        tag = "_".join(str(y) for y in years)
        out = f"report2_history_{tag}.xlsx"
    wb.save(out)
    print(f"\nСохранено: {out} ({len(wb.sheetnames)} листов)")
    return out


if __name__ == "__main__":
    build_history_report()
