"""
Сводка 2 — уборка за период (по умолчанию июль 2023 — 31.12.2025).

Excel в формате сводки 2 (2_main_detail.py): таблицы по культурам, технике, выработке.
Только группы уборки: 5.0 и 5.4. По каждому году — свой сезон (START_DATE / REPORT_DATE).

Запуск (из svodki/plan, нужен API_TOKEN в plans_1.py):
  python 2_main_harvest_history.py

Переменные окружения (необязательно):
  HISTORY_YEARS=2023,2024,2025
  HISTORY_SEASON_START_2023=2023-07-01
  HISTORY_SEASON_START_2024=2024-03-15
  HISTORY_SEASON_START_2025=2025-06-10
  HISTORY_REPORT_STAMP_2025=2026-01-01
  OUTPUT_FILE=2. report_harvest_2023-07_2025-12-31.xlsx
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

HARVEST_GROUPS = frozenset({
    "5.0 Уборка",
    "5.4 Уборка картофеля, лука",
})

# season_start, report_stamp (TODAY = штамп платформы; сутки отчёта = stamp − 1)
DEFAULT_SEASONS: dict[int, tuple[date, date]] = {
    # С июля 2023 — только уборочный хвост сезона
    2023: (date(2023, 7, 1), date(2024, 1, 1)),
    2024: (date(2024, 3, 15), date(2025, 1, 1)),
    # С 10 июня — как на платформе для 2025
    2025: (date(2025, 6, 10), date(2026, 1, 1)),
}


def _load_detail_module():
    spec = importlib.util.spec_from_file_location("r2_detail", _DETAIL_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {_DETAIL_PATH}")
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
    r2.CUSTOM_WINDOW_START = None
    r2.CUSTOM_WINDOW_END = None
    r2.CUSTOM_SHIFT_DATE_MIN = None
    r2.CUSTOM_SHIFT_DATE_MAX = None


def _filter_harvest_work_data(work_data: dict) -> dict:
    return {k: v for k, v in work_data.items() if k in HARVEST_GROUPS}


def build_harvest_history_report() -> str:
    os.environ.pop("REPORT_WINDOW_START", None)
    os.environ.pop("REPORT_WINDOW_END", None)
    os.environ.setdefault("SVODKI_HK_COMPACT_MAX_DAYS", "366")
    os.environ.setdefault("SVODKI_HK_COMPACT_ONLY_NONEMPTY", "1")
    os.environ.pop("SVODKI_R23_WORK_DATA_BUNDLE_OUT", None)

    r2 = _load_detail_module()
    _reset_custom_window(r2)
    years = _parse_years()
    headers = {"X-User-Api-Token": r2.API_TOKEN}

    # Только уборка — меньше запросов к API
    r2.WORK_GROUPS = [
        (name, wt_ids) for name, wt_ids in r2.WORK_GROUPS if name in HARVEST_GROUPS
    ]

    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    totals_by_group: dict[str, float] = {}

    for year in years:
        start, stamp = _season_bounds(year)
        r2.START_DATE = start
        r2.TODAY = stamp
        os.environ["SVODKI_CROP_YEAR"] = str(year)
        os.environ["SVODKI_SEASON_START_DATE"] = start.isoformat()
        os.environ["REPORT_DATE"] = stamp.isoformat()
        _reset_custom_window(r2)

        report_day = stamp - r2.timedelta(days=1)
        print(f"\n{'=' * 60}")
        print(f"Уборка {year}: сезон с {start} по {report_day} (REPORT_DATE={stamp})")
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

        work_data = _filter_harvest_work_data(
            r2.collect_work_data(
                field_to_crop,
                field_to_firm,
                field_to_firm_all,
                field_area,
                machine_group,
                headers,
            )
        )
        if not work_data:
            print(f"  [!] Нет данных по уборке за {year}")
            continue

        display_dates = r2.compute_display_dates(work_data)
        print(f"  Групп: {list(work_data.keys())}; колонок с датами: {len(display_dates)}")

        for gname, gd in work_data.items():
            total_ha = sum((gd.get("area_by_firm_total") or {}).values())
            totals_by_group[gname] = totals_by_group.get(gname, 0.0) + total_ha
            print(f"  {gname}: {round(total_ha, 1):,} га")

        r2.write_report_sheet(
            wb,
            work_data,
            display_dates,
            firm_plan_ha,
            firm_crop_area_ha,
            sheet_title_prefix=f"{year} ",
        )

    if not wb.sheetnames:
        raise RuntimeError("Нет листов — проверьте HISTORY_YEARS и доступ к API")

    out = (os.environ.get("OUTPUT_FILE") or "").strip()
    if not out:
        out = "2. report_harvest_2023-07_2025-12-31.xlsx"
    wb.save(out)
    print(f"\nСохранено: {out} ({len(wb.sheetnames)} листов)")
    for gname, total_ha in totals_by_group.items():
        print(f"{gname} итого за все годы: {round(total_ha, 1):,} га")
    if "5.4 Уборка картофеля, лука" in totals_by_group:
        print("Примечание: 5.4 входит в 5.0 по wt=76, поэтому общий итог не суммируем.")
    return out


if __name__ == "__main__":
    build_harvest_history_report()
