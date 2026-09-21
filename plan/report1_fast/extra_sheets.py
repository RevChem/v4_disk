"""Доп. листы сводки 1: намолот/овощи (10) и «Сводка овощи» (11) — без заглушек."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import traceback
from pathlib import Path
from typing import Any

import openpyxl

_PLAN_DIR = Path(__file__).resolve().parent.parent


def _ensure_plan_on_syspath() -> None:
    plan = str(_PLAN_DIR)
    if plan not in sys.path:
        sys.path.insert(0, plan)


def _load_script_module(script_name: str, mod_name: str) -> Any:
    """Загрузка svodki/plan/*.py как модуля (dataclass требует запись в sys.modules)."""
    _ensure_plan_on_syspath()
    script_path = _PLAN_DIR / script_name
    if not script_path.is_file():
        raise FileNotFoundError(f"Не найден скрипт: {script_path}")
    spec = importlib.util.spec_from_file_location(mod_name, str(script_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Не удалось загрузить {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _tag(standalone: bool) -> str:
    return "standalone" if standalone else "v2"


async def prepare_harvest_cache(legacy: Any, *, standalone: bool = False) -> tuple[Any, Any]:
    """Кэш сводки 10 для листов «Общий намолот» / «Овощи»."""
    tag = _tag(standalone)
    mod = _load_script_module("10_main_harvest.py", "main10_for_r1")
    mod.REPORT_DATE = legacy.REPORT_DATE
    cache = await mod.prepare_harvest_cache_async(legacy.REPORT_DATE)
    print(f"[{tag}] намолот: кэш сводки 10 подготовлен ({len(cache.mtfmi_map)} mtfmi)")
    return mod, cache


async def prepare_report11_veg_summary(legacy: Any, *, standalone: bool = False) -> Any:
    """Модуль сводки 11 + контекст для листа «Сводка oвощi»."""
    tag = _tag(standalone)
    mod = _load_script_module("11_main_harvest.py", "main11_for_r1")
    mod.REPORT_DATE = legacy.REPORT_DATE
    ctx = await mod.prepare_veg_summary_context_for_report1(legacy.REPORT_DATE)
    print(f"[{tag}] сводка 11: контекст «Сводка oвощi» подготовлен")
    return mod, ctx


async def append_harvest_sheets(
    wb: openpyxl.Workbook,
    legacy: Any,
    prepared: tuple[Any, Any] | None,
    *,
    standalone: bool = False,
) -> None:
    """Листы «Общий намолот» и «Овощи» из данных сводки 10."""
    tag = _tag(standalone)
    if prepared is None:
        if standalone:
            raise RuntimeError("standalone: не удалось подготовить данные сводки 10")
        print(f"[{tag}] намолот: пропуск (нет кэша сводки 10)")
        return

    mod, cache = prepared
    report_date = legacy.REPORT_DATE
    general = await asyncio.to_thread(mod.build_general_harvest, report_date, cache)
    if general is not None and not general.empty:
        mod._write_formatted_harvest_sheet(wb, "Общий намолот", general, mod._WIDTHS_GENERAL)
        print(f"[{tag}] лист «Общий намолот» добавлен ({len(general)} строк)")
    else:
        print(f"[{tag}] намолот: нет данных зерновой уборки за дату")

    vegetables = await asyncio.to_thread(mod.build_vegetables_report, report_date)
    if vegetables is not None and not vegetables.empty:
        mod._write_formatted_harvest_sheet(wb, "Овощи", vegetables, mod._WIDTHS_VEG)
        print(f"[{tag}] лист «Овощи» добавлен ({len(vegetables)} строк)")
    else:
        print(f"[{tag}] овощи: нет данных по овощной уборке за дату")


async def append_report11_veg_summary_sheet(
    wb: openpyxl.Workbook,
    legacy: Any,
    prepared: tuple[Any, Any] | None,
    *,
    standalone: bool = False,
) -> None:
    """Лист «Сводка oвощi» из расчёта сводки 11."""
    tag = _tag(standalone)
    if prepared is None:
        if standalone:
            raise RuntimeError("standalone: не удалось подготовить данные сводки 11")
        return

    mod, ctx = prepared
    mod.write_veg_summary_sheet_for_report1(wb, ctx)
    print(f"[{tag}] лист «Сводка oвощi» добавлен (данные сводки 11)")


async def load_harvest_sheets_data(
    legacy: Any, *, standalone: bool = False,
) -> tuple[tuple[Any, Any] | None, tuple[Any, Any] | None]:
    """
    Данные сводок 10 и 11 для листов уборки внутри сводки 1.
    Последовательно (10 → 11): параллельная загрузка ломала globals в 10_main_harvest.
    """
    tag = _tag(standalone)
    try:
        print(f"[{tag}] загрузка данных сводки 10 (намолот/овощи)...")
        harvest_prepared = await prepare_harvest_cache(legacy, standalone=standalone)
        print(f"[{tag}] загрузка данных сводки 11 (Сводка oвощi)...")
        veg_prepared = await prepare_report11_veg_summary(legacy, standalone=standalone)
        return harvest_prepared, veg_prepared
    except Exception:
        print(f"[{tag}] ошибка загрузки листов уборки:\n{traceback.format_exc()}")
        if standalone:
            raise
        return None, None


async def append_all_harvest_sheets(
    wb: openpyxl.Workbook,
    legacy: Any,
    harvest_prepared: tuple[Any, Any] | None,
    veg_prepared: tuple[Any, Any] | None,
    *,
    standalone: bool = False,
) -> None:
    await append_report11_veg_summary_sheet(wb, legacy, veg_prepared, standalone=standalone)
    await append_harvest_sheets(wb, legacy, harvest_prepared, standalone=standalone)
