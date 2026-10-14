# -*- coding: utf-8 -*-
"""
Сводки по уборке — все доступные даты (группировка по фирмам)
===============================================================
Скрипт получает все возможные даты из API (agro_operations.completed_date)
и для каждой даты строит отчёт по уборке — таблицы группируются по фирмам (агрофирмам),
а не по культурам.

Ссылка на структуру БД: Untitled.sql
"""

import os
import sys
from datetime import date, timedelta
from pathlib import Path

import importlib.util
import pandas as pd

from excel_style import apply_holding_summary_sheet, apply_styles_to_sheet, apply_vegetables_sheet

def _default_report_date() -> date:
    raw = (os.environ.get("REPORT_DATE") or "").strip()
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    return date.today()


def _season_for_report(report_date: date) -> str:
    return str(report_date.year)


def _date_range_for_report(report_date: date) -> tuple[date, date]:
    year = report_date.year
    return date(year, 4, 1), date(year, 12, 31)

# work_type_id уборки: 75/76=овощи 5.4, 118=кукуруза на зерно (из plans),
# 125=подсолнечник, 126=зерновые, 145=рапс. Кукуруза на силос отдельного wt
# уборки не имеет — её даты собираются по операциям «Скашивание»
# (main10.SILAGE_CORN_MOWING_WT_IDS), см. fetch_available_dates_from_agro_ops.
HARVEST_WORK_TYPE_IDS = [75, 76, 118, 125, 126, 145]

_script_dir = Path(__file__).resolve().parent


def _output_xlsx_path(report_date: date) -> Path:
    """Пишем в cwd (temp API), не в каталог скриптов (на сервере plan/ только чтение)."""
    name = f"crop_report_holding_{report_date.isoformat()}.xlsx"
    out_env = (os.environ.get("SVODKI_OUTPUT_DIR") or "").strip()
    if out_env:
        return Path(out_env) / name
    return Path.cwd() / name


# Колонки формата «СВОДНАЯ ИНФОРМАЦИЯ ПО УБОРКЕ ПО ХОЛДИНГУ» (как в примере)
HOLDING_SUMMARY_COLUMNS = [
    "№ п/п",
    "Культура",
    "Посеянная площадь, га",
    "Убранная площадь, га",
    "Остаток к уборке, га",
    "Осталось убрать, %",
    "Валовой сбор, т",
    "Урожайность за сезон, т/га",
    "Убрано за сутки, га",
    "Валовый сбор за сутки, т",
    "Урожайность за сутки, т/га",
]


def _to_holding_format(df: pd.DataFrame, plan_col: str = "План, га", fact_col: str = "Факт общий, га",
                       harvest_col: str = "Валовый сбор общий, т", fact_today_col: str = "Факт за сутки, га",
                       harvest_today_col: str = "Валовый сбор за сутки, т") -> pd.DataFrame:
    """Преобразует вывод build_general_harvest или build_vegetables в формат макета (11 колонок)."""
    if df.empty:
        return pd.DataFrame(columns=HOLDING_SUMMARY_COLUMNS)
    rows = []
    for _, r in df.iterrows():
        plan = float(r.get(plan_col, 0) or 0)
        fact = float(r.get(fact_col, 0) or 0)
        harvest = float(r.get(harvest_col, 0) or 0)
        fact_today = float(r.get(fact_today_col, 0) or 0)
        harvest_today = float(r.get(harvest_today_col, 0) or 0)
        rest = round(plan - fact, 2)
        rest_pct = round(rest / plan * 100, 2) if plan else 0.0
        yield_season = round(harvest / fact, 1) if fact else 0.0
        yield_today = round(harvest_today / fact_today, 1) if fact_today else 0.0
        rows.append({
            "№ п/п": r.get("№ п/п", ""),
            "Культура": r.get("Культура", ""),
            "Посеянная площадь, га": plan,
            "Убранная площадь, га": round(fact, 1),
            "Остаток к уборке, га": rest,
            "Осталось убрать, %": rest_pct,
            "Валовой сбор, т": round(harvest),
            "Урожайность за сезон, т/га": yield_season,
            "Убрано за сутки, га": round(fact_today, 2),
            "Валовый сбор за сутки, т": round(harvest_today),
            "Урожайность за сутки, т/га": yield_today,
        })
    return pd.DataFrame(rows, columns=HOLDING_SUMMARY_COLUMNS)


# Колонки для сводной таблицы по фирмам (аналог GENERAL_COLUMNS, но строка = фирма)
GENERAL_BY_FIRM_COLUMNS = [
    "№ п/п",
    "Агрофирма",
    "План, га",
    "Факт общий, га",
    "Валовый сбор общий, т",
    "Факт за сутки, га",
    "Валовый сбор за сутки, т",
    "Урожайность общая (по валовому сбору), т/га",
    "Доля убранной площади в %",
    "Средняя выработка, га / комбайн",
]

# Колонки для листа по одной фирме (строки = культуры)
FIRM_SHEET_COLUMNS = [
    "№ п/п",
    "Культура",
    "План, га",
    "Факт общий, га",
    "Валовый сбор общий, т",
    "Факт за сутки, га",
    "Валовый сбор за сутки, т",
    "Доля убранной площади в %",
    "Урожайность за сутки (по валовому сбору), т/га",
    "Урожайность за сезон (по валовому сбору), т/га",
]

# Колонки сводки по холдингу (макет «СВОДНАЯ ИНФОРМАЦИЯ ПО УБОРКЕ ПО ХОЛДИНГУ»)
HOLDING_SUMMARY_COLUMNS = [
    "№ п/п",
    "Культура",
    "Посеянная площадь, га",
    "Убранная площадь, га",
    "Остаток к уборке, га",
    "Осталось убрать, %",
    "Валовой сбор, т",
    "Урожайность за сезон, т/га",
    "Убрано за сутки, га",
    "Валовый сбор за сутки, т",
    "Урожайность за сутки, т/га",
]


def _to_holding_summary_format(df: pd.DataFrame, is_grain: bool) -> pd.DataFrame:
    """
    Преобразует результат build_general_harvest или build_vegetables_report
    в формат макета «СВОДНАЯ ИНФОРМАЦИЯ» (11 колонок).
    """
    if df.empty:
        return pd.DataFrame(columns=HOLDING_SUMMARY_COLUMNS)
    # Маппинг: для зерновых — План/Факт общий/Валовый сбор/Факт за сутки/Валовый сбор за сутки
    if is_grain:
        plan_col, fact_col = "План, га", "Факт общий, га"
        harvest_col, today_fact_col = "Валовый сбор общий, т", "Факт за сутки, га"
        today_harvest_col = "Валовый сбор за сутки, т"
        yield_season_col = "Урожайность общая (по валовому сбору), т/га"
    else:
        plan_col, fact_col = "План, га", "Факт общий, га"
        harvest_col, today_fact_col = "Валовый сбор общий, т", "Факт за сутки, га"
        today_harvest_col = "Валовый сбор за сутки, т"
        yield_season_col = "Урожайность за сезон (по валовому сбору), т/га"
    rows = []
    for _, row in df.iterrows():
        plan = row.get(plan_col, 0) or 0
        fact = row.get(fact_col, 0) or 0
        harvest = row.get(harvest_col, 0) or 0
        today_fact = row.get(today_fact_col, 0) or 0
        today_harvest = row.get(today_harvest_col, 0) or 0
        remainder = round(plan - fact, 1)
        remainder_pct = round(remainder / plan * 100, 2) if plan else 0.0
        yield_season = row.get(yield_season_col, 0) or 0
        yield_today = round(today_harvest / today_fact, 1) if today_fact else 0.0
        rows.append({
            "№ п/п": row.get("№ п/п", ""),
            "Культура": row.get("Культура", ""),
            "Посеянная площадь, га": plan,
            "Убранная площадь, га": round(fact, 1),
            "Остаток к уборке, га": remainder,
            "Осталось убрать, %": remainder_pct,
            "Валовой сбор, т": round(harvest),
            "Урожайность за сезон, т/га": yield_season,
            "Убрано за сутки, га": round(today_fact, 2),
            "Валовый сбор за сутки, т": round(today_harvest),
            "Урожайность за сутки, т/га": round(yield_today, 1),
        })
    return pd.DataFrame(rows, columns=HOLDING_SUMMARY_COLUMNS)

# Колонки сводки по холдингу (как в макете: ЗЕРНОВЫЕ КУЛЬТУРЫ / ОВОЩНЫЕ КУЛЬТУРЫ)
HOLDING_SUMMARY_COLUMNS = [
    "№ п/п",
    "Культура",
    "Посеянная площадь, га",
    "Убранная площадь, га",
    "Остаток к уборке, га",
    "Осталось убрать, %",
    "Валовой сбор, т",
    "Урожайность за сезон, т/га",
    "Убрано за сутки, га",
    "Валовый сбор за сутки, т",
    "Урожайность за сутки, т/га",
]


def _to_holding_summary_format(df: pd.DataFrame, plan_col: str, fact_col: str, harvest_col: str,
                               fact_today_col: str, harvest_today_col: str) -> pd.DataFrame:
    """Преобразует DataFrame в формат сводки по холдингу (11 колонок)."""
    if df.empty:
        return pd.DataFrame(columns=HOLDING_SUMMARY_COLUMNS)
    df = df.copy()
    df["_остаток"] = df[plan_col] - df[fact_col]
    df["_осталось_пct"] = (df["_остаток"] / df[plan_col] * 100).round(2) if (df[plan_col] != 0).any() else 0
    df["_урож_сезон"] = (df[harvest_col] / df[fact_col]).round(1).replace(float("inf"), 0).fillna(0)
    df["_урож_сутки"] = (df[harvest_today_col] / df[fact_today_col]).round(1).replace(float("inf"), 0).fillna(0)
    rows = []
    for _, r in df.iterrows():
        crop = r.get("Культура", "")
        is_total = crop == "Итого"
        rows.append({
            "№ п/п": "" if is_total else r.get("№ п/п", ""),
            "Культура": crop,
            "Посеянная площадь, га": r[plan_col],
            "Убранная площадь, га": round(r[fact_col], 1) if not is_total else round(r[fact_col], 1),
            "Остаток к уборке, га": round(r["_остаток"], 1) if not is_total else round(r["_остаток"], 1),
            "Осталось убрать, %": r["_осталось_пct"] if not is_total else round(r["_осталось_пct"], 2),
            "Валовой сбор, т": round(r[harvest_col]) if not is_total else round(r[harvest_col]),
            "Урожайность за сезон, т/га": r["_урож_сезон"] if not is_total else round(r["_урож_сезон"], 1),
            "Убрано за сутки, га": round(r[fact_today_col], 2) if not is_total else round(r[fact_today_col], 2),
            "Валовый сбор за сутки, т": round(r[harvest_today_col]) if not is_total else round(r[harvest_today_col]),
            "Урожайность за сутки, т/га": r["_урож_сутки"] if not is_total else round(r["_урож_сутки"], 1),
        })
    return pd.DataFrame(rows, columns=HOLDING_SUMMARY_COLUMNS)


def _load_main10():
    """Загружает 10_main_harvest как модуль."""
    candidates = [
        _script_dir / "10_main_harvest.py",
        _script_dir / "10.main.py",
        _script_dir.parent / "new" / "10_main_harvest.py",
        _script_dir.parent / "10.main.py",
    ]
    main10_path = next((p for p in candidates if p.exists()), None)
    if main10_path is None:
        raise FileNotFoundError(
            "Не найден 10_main_harvest.py / 10.main.py "
            f"(каталог: {_script_dir})"
        )
    spec = importlib.util.spec_from_file_location("main10", main10_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Не удалось создать spec для {main10_path}")
    main10 = importlib.util.module_from_spec(spec)
    sys.modules["main10"] = main10
    spec.loader.exec_module(main10)
    return main10


def fetch_available_dates_from_agro_ops(main10, report_date: date | None = None) -> list[date]:
    """
    Собирает уникальные даты из agro_operations (completed_date/completed_datetime)
    по всем work_type_id уборки за сезон (плюс операции «Скашивание» —
    даты кукурузы на силос, main10.SILAGE_CORN_MOWING_WT_IDS).
    """
    anchor = report_date or _default_report_date()
    season = _season_for_report(anchor)
    date_range = _date_range_for_report(anchor)
    wt_ids = set(HARVEST_WORK_TYPE_IDS)
    silage_wt_ids = getattr(main10, "SILAGE_CORN_MOWING_WT_IDS", None)
    if silage_wt_ids:
        wt_ids |= set(silage_wt_ids)

    date_str_set: set[str] = set()
    for wt_id in sorted(wt_ids):
        ops = main10.get_agro_operations(wt_id, season)
        for o in ops:
            d = o.get("completed_date") or o.get("completed_datetime") or ""
            if d:
                date_str_set.add(str(d)[:10])
    dates = []
    for s in date_str_set:
        try:
            d = date.fromisoformat(s)
            if date_range[0] <= d <= date_range[1]:
                dates.append(d)
        except (ValueError, TypeError):
            pass
    return sorted(set(dates))


def build_general_harvest_by_firms(main10, report_date: date, cache=None) -> pd.DataFrame:
    """
    Общий намолот: по строкам — агрофирмы (фирмы), данные агрегированы по всем культурам.
    """
    firm_data: dict[str, dict] = {}
    # Суммируем через build_crop_report: per-firm валовый сбор и подстановка с фронта.
    # Старый use_cache-путь ошибочно брал holding-total из _holding_harvest_from_agro на каждую фирму.
    for crop_name, (work_type_id, agrofirms) in main10.CROP_CONFIGS.items():
        report = main10.build_crop_report(
            crop_name, work_type_id, agrofirms, report_date, cache,
        )
        if report.empty:
            continue
        data_rows = report[report["Агрофирма"] != "Итого"]
        if data_rows.empty:
            continue
        for _, row in data_rows.iterrows():
            firm_name = row["Агрофирма"]
            if firm_name not in firm_data:
                firm_data[firm_name] = {
                    "План, га": 0.0,
                    "Факт общий, га": 0.0,
                    "Валовый сбор общий, т": 0.0,
                    "Факт за сутки, га": 0.0,
                    "Валовый сбор за сутки, т": 0.0,
                }
            firm_data[firm_name]["План, га"] += row["План, га"]
            firm_data[firm_name]["Факт общий, га"] += row["Факт общий, га"]
            firm_data[firm_name]["Валовый сбор общий, т"] += row["Валовый сбор общий, т"]
            firm_data[firm_name]["Факт за сутки, га"] += row["Факт за сутки, га"]
            firm_data[firm_name]["Валовый сбор за сутки, т"] += row["Валовый сбор за сутки, т"]

    rows = []
    for i, firm_name in enumerate(main10.AGROFARM_ORDER, start=1):
        if firm_name not in firm_data:
            continue
        d = firm_data[firm_name]
        fact = d["Факт общий, га"]
        harvest = d["Валовый сбор общий, т"]
        plan = d["План, га"]
        yield_season = round(harvest / fact, 1) if fact else 0.0
        share = round(fact / plan * 100, 1) if plan else 0.0
        rows.append({
            "№ п/п": i,
            "Агрофирма": firm_name,
            "План, га": plan,
            "Факт общий, га": round(fact, 1),
            "Валовый сбор общий, т": round(harvest),
            "Факт за сутки, га": round(d["Факт за сутки, га"], 2),
            "Валовый сбор за сутки, т": round(d["Валовый сбор за сутки, т"]),
            "Урожайность общая (по валовому сбору), т/га": yield_season,
            "Доля убранной площади в %": share,
            "Средняя выработка, га / комбайн": 0.0,
        })

    result = pd.DataFrame(rows, columns=GENERAL_BY_FIRM_COLUMNS)
    if result.empty:
        return result
    total_plan = result["План, га"].sum()
    total_fact = result["Факт общий, га"].sum()
    total_harvest = result["Валовый сбор общий, т"].sum()
    total_today = result["Факт за сутки, га"].sum()
    total_h_today = result["Валовый сбор за сутки, т"].sum()
    totals = {
        "№ п/п": "",
        "Агрофирма": "Итого",
        "План, га": total_plan,
        "Факт общий, га": round(total_fact, 1),
        "Валовый сбор общий, т": round(total_harvest),
        "Факт за сутки, га": round(total_today, 2),
        "Валовый сбор за сутки, т": round(total_h_today),
        "Урожайность общая (по валовому сбору), т/га": (
            round(total_harvest / total_fact, 1) if total_fact else 0.0
        ),
        "Доля убранной площади в %": round(total_fact / total_plan * 100, 1) if total_plan else 0.0,
        "Средняя выработка, га / комбайн": 0.0,
    }
    result = pd.concat([result, pd.DataFrame([totals])], ignore_index=True)
    return result


SVODKA13_ZERO_PLAN_FIX = "2026-10-14a"


def _append_firm_total_row(result: pd.DataFrame, cache, main10, firm_name: str) -> pd.DataFrame:
    """Строка «Итого» по отфильтрованным культурам фирмы."""
    if result.empty:
        return result
    total_plan = result["План, га"].sum()
    total_harvest = result["Валовый сбор общий, т"].sum()
    total_h_today = result["Валовый сбор за сутки, т"].sum()
    if cache and not getattr(main10, "USE_FAST_MODE", False):
        total_fact, total_today = main10._firm_area_from_cache_11_style(cache, firm_name)
    else:
        total_fact = result["Факт общий, га"].sum()
        total_today = result["Факт за сутки, га"].sum()
    totals = {
        "№ п/п": "",
        "Культура": "Итого",
        "План, га": total_plan,
        "Факт общий, га": round(total_fact, 1),
        "Валовый сбор общий, т": round(total_harvest),
        "Факт за сутки, га": round(total_today, 2),
        "Валовый сбор за сутки, т": round(total_h_today),
        "Доля убранной площади в %": round(total_fact / total_plan * 100, 1) if total_plan else 0.0,
        "Урожайность за сутки (по валовому сбору), т/га": (
            round(total_h_today / total_today, 1) if total_today else 0.0
        ),
        "Урожайность за сезон (по валовому сбору), т/га": (
            round(total_harvest / total_fact, 1) if total_fact else 0.0
        ),
    }
    return pd.concat([result, pd.DataFrame([totals])], ignore_index=True)


_HARVEST_PLAN_GROUP = "5.0 Уборка"
_HARVEST_DATA_COLS = (
    "Факт общий, га", "Валовый сбор общий, т",
    "Факт за сутки, га", "Валовый сбор за сутки, т",
)


def _harvest_crop_excluded(main10, firm_name: str, crop_name: str) -> bool:
    """Культура исключена для фирмы в «Редакторе планов» (скрытие, «не считать», ранг 0)."""
    check = getattr(main10, "is_crop_excluded_from_totals", None)
    if not callable(check):
        return False
    try:
        return bool(check(_HARVEST_PLAN_GROUP, firm_name, crop_name))
    except Exception:  # noqa: BLE001
        return False


def _row_has_harvest_data(row) -> bool:
    """Есть ли по строке фактическая уборка (га или тонны) — тогда она нужна и без плана."""
    for col in _HARVEST_DATA_COLS:
        try:
            if float(row[col] or 0) > 0:
                return True
        except (KeyError, TypeError, ValueError):
            continue
    return False



def build_firm_report(main10, firm_name: str, report_date: date,
                      cache=None) -> pd.DataFrame:
    """
    Отчёт по одной фирме: строки — культуры.
    """
    use_cache = (
        cache is not None
        and not getattr(main10, "USE_FAST_MODE", False)
        and cache.area_by_firm_crop
    )
    label_date = (
        cache.label_date if cache
        else main10._label_report_date(main10.compute_time_range(report_date)[0])
    )
    rows = []
    crop_order = list(main10.GENERAL_CROP_ORDER)
    if hasattr(main10, "_HARVEST_CROP_ORDER"):
        crop_order = list(dict.fromkeys([*crop_order, *main10._HARVEST_CROP_ORDER]))
    row_num = 0
    for crop_name in crop_order:
        if crop_name not in main10.CROP_CONFIGS:
            continue
        work_type_id, agrofirms = main10.CROP_CONFIGS[crop_name]
        if firm_name not in agrofirms:
            continue
        plan_ha, _plan_ids = agrofirms[firm_name]
        # Культура скрыта / «не считать» / ранг 0 в «Редакторе планов» — строки нет.
        if _harvest_crop_excluded(main10, firm_name, crop_name):
            continue
        if use_cache:
            row = main10.firm_crop_row_from_cache(
                cache, firm_name, crop_name, work_type_id, plan_ha, row_num + 1, label_date,
            )
            # Как в сводке 10: без плана строка остаётся, если есть факт/сбор
            # (например, кукуруза на силос — плана уборки у неё обычно нет).
            if not plan_ha and not _row_has_harvest_data(row):
                continue
            row_num += 1
            rows.append({
                "№ п/п": row_num,
                "Культура": crop_name,
                "План, га": row["План, га"],
                "Факт общий, га": row["Факт общий, га"],
                "Валовый сбор общий, т": row["Валовый сбор общий, т"],
                "Факт за сутки, га": row["Факт за сутки, га"],
                "Валовый сбор за сутки, т": row["Валовый сбор за сутки, т"],
                "Доля убранной площади в %": row["Доля убранной площади в %"],
                "Урожайность за сутки (по валовому сбору), т/га": row[
                    "Урожайность за сутки (по валовому сбору), т/га"
                ],
                "Урожайность за сезон (по валовому сбору), т/га": row[
                    "Урожайность за сезон (по валовому сбору), т/га"
                ],
            })
            continue
        report = main10.build_crop_report(
            crop_name, work_type_id, agrofirms, report_date, cache,
        )
        if report.empty:
            continue
        firm_row = report[report["Агрофирма"] == firm_name]
        if firm_row.empty:
            continue
        r = firm_row.iloc[0]
        plan = r["План, га"]
        if not plan and not _row_has_harvest_data(r):
            continue
        row_num += 1
        fact = r["Факт общий, га"]
        share = r["Доля убранной площади в %"]
        yield_today = r["Урожайность за сутки (по валовому сбору), т/га"]
        yield_season = r["Урожайность за сезон (по валовому сбору), т/га"]
        rows.append({
            "№ п/п": row_num,
            "Культура": crop_name,
            "План, га": plan,
            "Факт общий, га": fact,
            "Валовый сбор общий, т": r["Валовый сбор общий, т"],
            "Факт за сутки, га": r["Факт за сутки, га"],
            "Валовый сбор за сутки, т": r["Валовый сбор за сутки, т"],
            "Доля убранной площади в %": share,
            "Урожайность за сутки (по валовому сбору), т/га": yield_today,
            "Урожайность за сезон (по валовому сбору), т/га": yield_season,
        })

    if use_cache:
        other_t, other_d = main10.other_crop_area_for_firm(cache, firm_name)
        if other_t > 0 or other_d > 0:
            rows.append({
                "№ п/п": len(rows) + 1,
                "Культура": "Иные культуры",
                "План, га": 0.0,
                "Факт общий, га": other_t,
                "Валовый сбор общий, т": 0,
                "Факт за сутки, га": other_d,
                "Валовый сбор за сутки, т": 0,
                "Доля убранной площади в %": 0.0,
                "Урожайность за сутки (по валовому сбору), т/га": 0.0,
                "Урожайность за сезон (по валовому сбору), т/га": 0.0,
            })

    result = pd.DataFrame(rows, columns=FIRM_SHEET_COLUMNS)
    if result.empty:
        return result
    return _append_firm_total_row(result, cache, main10, firm_name)


def _apply_holding_sheet(writer, sheet_name: str, section_title: str, df, report_date: date) -> None:
    """Оформление листа ЗЕРНОВЫЕ/ОВОЩНЫЕ КУЛЬТУРЫ — заголовок, дата, стили."""
    from openpyxl.styles import Alignment, Font, PatternFill
    from excel_style import style_report_range, add_standard_footnotes

    ws = writer.book[sheet_name]
    ws["A1"] = section_title
    ws["A1"].font = Font(bold=True)
    date_str = report_date.strftime("%d.%m.%Y")
    ws["K1"] = f"Сводка за {date_str}"
    ws["K1"].alignment = Alignment(horizontal="right")
    ws["K1"].fill = PatternFill(start_color="E0E0E0", end_color="E0E0E0", fill_type="solid")
    if not df.empty:
        n_rows, n_cols = len(df) + 1, len(df.columns)
        style_report_range(ws, start_row=5, n_rows=n_rows, n_cols=n_cols, has_total_row=True)
        add_standard_footnotes(ws, 5 + n_rows - 1)


def run_for_dates(dates: list[date] | None = None) -> None:
    """
    Строит отчёт по фирмам для каждой даты.
    """
    main10 = _load_main10()
    if dates is None:
        print("Загружаем доступные даты из agro_operations...")
        dates = fetch_available_dates_from_agro_ops(main10)
        if not dates:
            print("  Нет данных за сезон. Завершение.")
            return
        print(f"  Найдено дат: {len(dates)}")
        preview = [d.strftime("%Y-%m-%d") for d in dates[:7]]
        print(f"  Даты: {preview}{'...' if len(dates) > 7 else ''}\n")

    for report_date in dates:
        configure = getattr(main10, "configure_for_report_date", None)
        if callable(configure):
            configure(report_date)
        label_date = report_date
        cache = None
        if not getattr(main10, "USE_FAST_MODE", False):
            cache = main10.prepare_harvest_cache(report_date)
            label_date = cache.label_date
        print(f"\n=== Сводка за {label_date.strftime('%d.%m.%Y')} ({SVODKA13_ZERO_PLAN_FIX}) ===")
        output_file = _output_xlsx_path(report_date)

        grain_holding = main10.build_holding_summary_grain(report_date, cache)
        veg_holding = main10.build_holding_summary_vegetables(report_date)
        firm_reports = {}
        for firm_name in main10.AGROFARM_ORDER:
            firm_reports[firm_name] = build_firm_report(
                main10, firm_name, report_date, cache,
            )

        with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
            if not grain_holding.empty:
                grain_holding.to_excel(writer, sheet_name="Зерновые культуры", index=False, startrow=0)
                apply_holding_summary_sheet(
                    writer, "Зерновые культуры", "ЗЕРНОВЫЕ КУЛЬТУРЫ",
                    grain_holding, label_date,
                )
                print("  Лист: Зерновые культуры")

            if not veg_holding.empty:
                veg_holding.to_excel(writer, sheet_name="Овощные культуры", index=False, startrow=0)
                apply_holding_summary_sheet(
                    writer, "Овощные культуры", "ОВОЩНЫЕ КУЛЬТУРЫ",
                    veg_holding, label_date,
                )
                print("  Лист: Овощные культуры")

            for firm_name, report in firm_reports.items():
                if report.empty:
                    continue
                sheet_name = main10._safe_sheet_name(firm_name)
                report.to_excel(writer, sheet_name=sheet_name, index=False)
                apply_styles_to_sheet(writer, sheet_name, report, has_total_row=True)
                print(f"  Лист: {sheet_name} ({len(report)} строк)")

        print(f"\nСохранено: {output_file.name}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Сводки по уборке (по фирмам) за все доступные даты из API"
    )
    parser.add_argument(
        "--date",
        type=str,
        metavar="YYYY-MM-DD",
        help="Одна дата (например: 2025-12-25)",
    )
    parser.add_argument(
        "--from",
        dest="date_from",
        type=str,
        metavar="YYYY-MM-DD",
        help="Начало диапазона дат",
    )
    parser.add_argument(
        "--to",
        dest="date_to",
        type=str,
        metavar="YYYY-MM-DD",
        help="Конец диапазона дат",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Только вывести список дат, не строить отчёты",
    )
    parser.add_argument(
        "--only-with-data",
        action="store_true",
        help="Для --from/--to: только даты, по которым есть данные в API",
    )
    args = parser.parse_args()

    if args.date:
        d = date.fromisoformat(args.date)
        dates = [d]
    elif os.environ.get("REPORT_DATE"):
        try:
            d = date.fromisoformat(os.environ["REPORT_DATE"])
            dates = [d]
        except (ValueError, TypeError):
            dates = None
    elif args.date_from or args.date_to:
        anchor = _default_report_date()
        dr = _date_range_for_report(anchor)
        d_from = date.fromisoformat(args.date_from or str(dr[0]))
        d_to = date.fromisoformat(args.date_to or str(dr[1]))
        if d_from > d_to:
            d_from, d_to = d_to, d_from
        if args.only_with_data:
            main10 = _load_main10()
            available = fetch_available_dates_from_agro_ops(main10)
            dates = [d for d in available if d_from <= d <= d_to]
        else:
            dates = [d_from + timedelta(days=i) for i in range((d_to - d_from).days + 1)]
    else:
        dates = None

    if args.list_only:
        main10 = _load_main10()
        available = fetch_available_dates_from_agro_ops(main10)
        if dates:
            available = [d for d in available if d in set(dates)]
        print("Доступные даты:")
        for d in available:
            print(f"  {d.strftime('%Y-%m-%d')}")
        sys.exit(0)

    run_for_dates(dates)
