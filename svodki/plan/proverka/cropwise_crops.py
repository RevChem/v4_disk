"""Разбивка Cropwise по культурам: вкладка cropRows + сезоны, которых нет в сессии UI."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from proverka.crop_compare import cropwise_area_by_firm_crop


def _add_crop_areas(
    dst: dict[str, dict[str, float]],
    src: dict[str, dict[str, float]],
) -> None:
    for crop, by_firm in src.items():
        bucket = dst.setdefault(crop, {})
        for firm, ha in by_firm.items():
            bucket[firm] = round(float(bucket.get(firm, 0.0)) + float(ha or 0), 2)


def seasons_missing_from_ui(
    task_years: list[int] | None,
    ui_session: int | None,
    ui_has_crops: bool,
    *,
    seasons_in_shift: list[str] | None = None,
) -> list[str]:
    """
    work.json cropRows — только сезон открытой сессии.
    Остальные сезоны плана (и сам сезон сессии, если вкладка пустая) берём из API v3.

    Годы, которых нет в плане, не добавляем: один сезон в редакторе
    проверяется отдельно, даже если в смене встретился другой год.
    Поэтому при пустом списке сезонов плана результат всегда пустой.
    Параметр seasons_in_shift сохранён только для совместимости вызовов
    и на результат не влияет.
    """
    del seasons_in_shift  # намеренно не используется (см. docstring)
    years = [str(int(y)) for y in (task_years or [])]
    if not years:
        return []
    if not ui_has_crops:
        return years
    session = str(int(ui_session)) if ui_session is not None else None
    return [y for y in years if y != session]


def _run_coro(coro):
    """asyncio.run из синхронного кода. Из уже запущенного цикла — в отдельном потоке.

    Иначе RuntimeError проглатывается выше, и все строки остаются без культуры.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _ensure_history_crops(items_by_task: dict[int, list[dict]]) -> None:
    """Дозагрузить культуры записей истории, которых ещё нет в кэше mtfmi_batch_fetch.

    Обычно кэш уже заполнен загрузкой mtfmi (remember_history_crops). Если mtfmi пришли
    из хранилища заданий, а записи истории не перечитывались, догружаем только недостающие.
    Сбой загрузки не останавливает проверку: он пишется в лог и в предупреждения,
    а строки без культуры остаются «Культура не определена».
    """
    import httpx
    from plans import API_TOKEN, BASE_URL
    import mtfmi_batch_fetch as mb

    missing: dict[int, list[dict]] = {}
    for tid, items in items_by_task.items():
        for item in items:
            hid = item.get("history_item_id")
            if hid is None:
                continue
            try:
                known = int(hid) in mb._HISTORY_CROP
            except (TypeError, ValueError):
                continue
            if not known:
                missing.setdefault(tid, []).append(item)
    if not missing:
        return

    async def _load() -> None:
        sem = asyncio.Semaphore(20)
        headers = {"X-User-Api-Token": API_TOKEN}
        async with httpx.AsyncClient(headers=headers, timeout=120) as client:
            await mb.remember_history_crops(client, BASE_URL, missing, sem)

    try:
        _run_coro(_load())
    except Exception as exc:  # noqa: BLE001 — сводка/проверка не должна падать из-за справочника
        mb.note_history_load_error(exc, prefix="proverka: ")


def api_area_by_firm_crop(
    tasks: list[dict],
    mtfmi_map: dict[int, list[dict]],
    field_to_firm: dict[int, str],
    seasons: set[str],
    cutoff,
    day_start=None,
) -> dict[str, dict[str, float]]:
    """
    Культура строки площади — HistoryItem из ``mtfmi.history_item_id``, тот же источник,
    что у сводок (``mtfmi_batch_fetch.crop_of_item``). Раньше проверка шла через
    mtaomi → agro_operation.history_item_id, а без агрооперации брала культуру поля
    в сезоне: на заданиях, где эти источники расходились, появлялись ложные расхождения.

    Строка без history_item_id (или с записью чужого поля) остаётся в итоге строкой
    «Культура не определена» — как в сводках, гектары не теряются.
    Площадь — covered_area_hourly в окне [day_start, cutoff); без day_start нижней границы нет.
    """
    from datetime import datetime

    from mtfmi_batch_fetch import UNDEFINED_CROP, crop_of_item
    from proverka.crop_compare import _crop_label
    from proverka.season_cropwise import _hourly_in_shift

    if not isinstance(cutoff, datetime) or not seasons:
        return {}
    selected = [
        t for t in tasks
        if str(t.get("season") or "") in seasons and t.get("id") is not None
    ]
    if not selected:
        return {}

    items_by_task: dict[int, list[dict]] = {}
    for t in selected:
        tid = int(t["id"])
        items_by_task[tid] = list(mtfmi_map.get(tid) or mtfmi_map.get(t["id"]) or [])
    _ensure_history_crops(items_by_task)

    try:
        from utils.shared.task_area_shared import (
            CORN_SILAGE_CROP,
            SILAGE_CORN_MOWING_WT_IDS,
        )
    except ImportError:
        CORN_SILAGE_CROP, SILAGE_CORN_MOWING_WT_IDS = None, frozenset()

    out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for t in selected:
        tid = int(t["id"])
        wt = t.get("work_type_id")
        silage = False
        if wt is not None and CORN_SILAGE_CROP:
            try:
                silage = int(wt) in SILAGE_CORN_MOWING_WT_IDS
            except (TypeError, ValueError):
                silage = False
        for item in items_by_task[tid]:
            fid = item.get("field_id")
            if fid is None:
                continue
            ha = _hourly_in_shift(item, day_start, cutoff)
            if ha <= 0:
                continue
            firm = field_to_firm.get(int(fid), "")
            if not firm:
                continue
            crop = crop_of_item(item, "") or UNDEFINED_CROP
            if silage and "кукуруза" in crop.casefold():
                crop = CORN_SILAGE_CROP
            out[_crop_label(crop)][firm] += ha
    return {
        crop: {firm: round(ha, 2) for firm, ha in by_firm.items() if ha >= 0.01}
        for crop, by_firm in out.items()
    }


def fill_crop_comparisons(
    target: dict,
    operations: dict,
    *,
    work: dict | None,
    firm_names: list[str],
    hints: list[str],
    shift_cache,
    task_years: list[int],
    ui_session: int | None,
    cutoff_iso: str,
    group_names: list[str] | None = None,
) -> None:
    """Сверка культур, когда work.json недоступен: культуры Cropwise из API v3."""
    from proverka.crop_compare import compare_svodka_cropwise_crops
    from proverka.season_cropwise import _parse_dt

    names = list(group_names or operations.keys())
    rows: list[dict] = []
    body = work.get("body") if isinstance(work, dict) else None
    for gn in names:
        op = operations.get(gn) or {}
        svodka_crop = op.get("area_today_by_firm_crop") or {}
        if not svodka_crop:
            continue
        cw_crop = cropwise_crops_all_seasons(
            body,
            firm_names,
            hints,
            shift_cache=shift_cache,
            task_years=list(task_years),
            ui_session=ui_session,
            wt_ids=list(op.get("work_type_ids") or []),
            cutoff=_parse_dt(cutoff_iso),
        )
        compared = compare_svodka_cropwise_crops(svodka_crop, cw_crop, group_name=gn)
        for row in compared:
            row["group"] = gn
        rows.extend(compared)
    if not rows:
        return
    target["crop_comparisons"] = rows
    target.setdefault("comparison", {})
    target["comparison"]["crop_diff_count"] = sum(
        1 for row in rows if row.get("status") == "DIFF"
    )


def cropwise_crops_api_by_operation(
    wt_ids: list[int],
    shift_cache: Any,
    cutoff,
    task_years: list[int] | None,
    *,
    plow_season_only: bool = False,
    day_start=None,
) -> dict[str, dict[str, float]]:
    """
    Культуры Cropwise только по wt операции (API v3), без cropRows work.json.

    В отчёте 9 work.json отдаёт одни cropRows на вспашку+глубокорыхление —
    при разборе по hints обе операции получали одинаковые итоги по культурам.
    """
    if shift_cache is None:
        return {}
    wt_set = {int(x) for x in wt_ids}
    tasks = [
        t for t in (shift_cache.all_tasks or [])
        if int(t.get("work_type_id") or 0) in wt_set
    ]
    seasons = {
        str(t.get("season"))
        for t in tasks
        if t.get("season") not in (None, "")
    }
    if task_years:
        seasons |= {str(int(y)) for y in task_years}
    if plow_season_only and task_years:
        seasons = {str(int(y)) for y in task_years}
    if not seasons or not tasks:
        return {}
    return api_area_by_firm_crop(
        tasks,
        shift_cache.mtfmi_map,
        shift_cache.field_to_firm,
        seasons,
        cutoff,
        day_start=day_start,
    )


def cropwise_crops_all_seasons(
    body: Any,
    firm_names: list[str],
    fact_hints: list[str],
    *,
    shift_cache: Any | None = None,
    task_years: list[int] | None = None,
    ui_session: int | None = None,
    wt_ids: list[int] | None = None,
    cutoff=None,
) -> dict[str, dict[str, float]]:
    """Культуры только из cropRows work.json. API v3 в эту колонку не подставляется."""
    del shift_cache, task_years, ui_session, wt_ids, cutoff
    ui = cropwise_area_by_firm_crop(body, firm_names, fact_hints)
    if not ui or (isinstance(body, dict) and body.get("reportBlank")):
        return {}
    return ui


def svodka_counted_years(
    ui_years: list[int],
    *,
    group_name: str | None,
    season_rule: str,
    task_years: list[int] | None,
) -> list[int] | None:
    """
    None — сводка не режет machine_task.season, в diff входят все сезоны UI.

    Иначе только годы, которые сводка сама учитывает.
    """
    from season_unified import applies_task_season_filter

    if group_name:
        filters = applies_task_season_filter(group_name)
    else:
        filters = season_rule not in ("none", "")
    if not filters:
        return None
    wanted = {int(y) for y in (task_years or [])}
    if not wanted:
        return None
    return [int(y) for y in ui_years if int(y) in wanted]


def combine_loaded_seasons(
    by_year: dict,
    years: list[int],
    firm_names: list[str],
    fact_hints: list[str],
    *,
    counted_years: list[int] | None,
) -> dict[str, Any]:
    """
    Сумма work.json по сезонам загрузчика.

    Ответ записывается в год только если load_status это разрешает и
    ui_session_seasonValue совпадает с ключом. Пустой UI не заменяется API.
    """
    from proverka.crop_compare import cropwise_area_by_firm_crop
    from proverka.cropwise_ui import parse_work_report_body

    counted = {int(y) for y in counted_years} if counted_years is not None else {int(y) for y in years}
    seasons: dict[str, dict[str, Any]] = {}
    by_firm: dict[str, float] = defaultdict(float)
    crops: dict[str, dict[str, float]] = {}
    compare_ha = 0.0
    both_ha = 0.0
    missing: list[int] = []
    warnings: list[str] = []
    parsed_keep: dict[str, Any] | None = None
    for year in years:
        year = int(year)
        slot = by_year.get(year) or by_year.get(str(year)) or {}
        status = str(slot.get("load_status") or "")
        session = slot.get("ui_session_seasonValue")
        body = slot.get("body") if isinstance(slot.get("body"), dict) else None
        if status == "loaded" and session is not None and int(session) != year:
            status = "not_loaded"
        if status not in ("loaded", "empty"):
            missing.append(year)
            seasons[str(year)] = {
                "total_ha": None,
                "by_firm": {},
                "source": None,
                "status": status or "not_loaded",
            }
            continue
        if status == "empty" or (isinstance(body, dict) and body.get("reportBlank")):
            ha = 0.0
            firms: dict[str, float] = {}
            status = "empty"
        else:
            parsed = parse_work_report_body(body, firm_names, list(fact_hints))
            if parsed_keep is None or float(parsed.get("fact_total_ha") or 0) > 0:
                parsed_keep = parsed
            ha = float(parsed.get("fact_total_ha") or 0)
            firms = {
                str(firm): round(float(value or 0), 2)
                for firm, value in (parsed.get("fact_by_firm") or {}).items()
                if float(value or 0) >= 0.01
            }
            if year in counted:
                _add_crop_areas(crops, cropwise_area_by_firm_crop(body, firm_names, list(fact_hints)))
        seasons[str(year)] = {
            "total_ha": round(ha, 2),
            "by_firm": firms,
            "source": "work_json",
            "status": status,
        }
        both_ha += ha
        if year in counted:
            compare_ha += ha
            for firm, value in firms.items():
                by_firm[firm] += value
        elif ha > 0.01:
            warnings.append(
                f"есть задания сезона {year} — проверьте сезон заданий в Cropwise"
            )
    incomplete_years = [year for year in missing if year in counted]
    return {
        "compare_ha": round(compare_ha, 2),
        "both_seasons_ha": round(both_ha, 2),
        "by_firm": {k: round(v, 2) for k, v in by_firm.items() if v >= 0.01},
        "crops": {c: dict(f) for c, f in crops.items() if f},
        "seasons": seasons,
        "incomplete": bool(incomplete_years),
        "incomplete_years": incomplete_years,
        "warnings": warnings,
        "parsed": parsed_keep or {},
    }


def api_reference_ha(shift_cache: Any | None, wt_ids: list[int]) -> float | None:
    """Справочная сумма API v3. В колонку Cropwise не входит."""
    if shift_cache is None or not hasattr(shift_cache, "api_for_wt_ids"):
        return None
    try:
        api, _ids = shift_cache.api_for_wt_ids(list(wt_ids))
    except Exception:
        return None
    total = 0.0
    for block in (api or {}).values():
        total += float((block or {}).get("total_ha") or 0)
    return round(total, 2)


def build_season_cropwise(
    work: dict[str, Any],
    *,
    report_date: Any,
    firm_names: list[str],
    fact_hints: list[str],
    season_rule: str,
    task_years: list[int] | None,
    group_name: str | None,
    shift_cache: Any | None,
    wt_ids: list[int],
) -> dict[str, Any]:
    """Cropwise для сверки: сумма сезонов загрузчика и отдельная цифра API."""
    from proverka.cropwise_ui import ui_check_years

    years = [int(y) for y in (work.get("years") or ui_check_years(report_date))]
    counted = svodka_counted_years(
        years, group_name=group_name, season_rule=season_rule, task_years=task_years,
    )
    combined = combine_loaded_seasons(
        work.get("by_year") or {},
        years,
        firm_names,
        fact_hints,
        counted_years=counted,
    )
    api_ha = api_reference_ha(shift_cache, wt_ids)
    parts: list[str] = []
    for year in years:
        block = combined["seasons"].get(str(year)) or {}
        status = block.get("status")
        if status in ("loaded", "empty") and block.get("total_ha") is not None:
            parts.append(f"{year}={float(block['total_ha']):.2f}")
        elif status == "unstable":
            parts.append(f"{year}=UI нестабилен")
        else:
            parts.append(f"{year}=не проверен")
    note = "Cropwise: сумма work.json по сезонам (" + ", ".join(parts) + ")."
    if combined["incomplete"]:
        missed = ", ".join(str(y) for y in combined["incomplete_years"])
        note += f" Итог неполный, сезон {missed} не проверен."
    if combined["warnings"]:
        note += " " + " ".join(combined["warnings"]) + "."
    combined["api_ha"] = api_ha
    combined["note"] = note
    combined["years"] = years
    if api_ha is not None:
        combined["ui_vs_api_ha"] = round(combined["both_seasons_ha"] - api_ha, 2)
    else:
        combined["ui_vs_api_ha"] = None
    return combined


def combine_editor_seasons(
    by_year: dict,
    years: list[int],
    firm_names: list[str],
    fact_hints: list[str],
    *,
    shift_cache: Any | None = None,
    wt_ids: list[int] | None = None,
    cutoff=None,
) -> dict[str, Any]:
    """Сумма уже загруженных сезонов. API на место пустого UI не ставится."""
    del shift_cache, wt_ids, cutoff
    combined = combine_loaded_seasons(
        by_year, [int(y) for y in years], firm_names, fact_hints, counted_years=None,
    )
    return {
        "total_ha": combined["compare_ha"],
        "by_firm": combined["by_firm"],
        "crops": combined["crops"],
        "by_year": {
            int(year): float(block.get("total_ha") or 0)
            for year, block in combined["seasons"].items()
            if block.get("status") in ("loaded", "empty")
        },
        "incomplete": combined["incomplete"],
        "incomplete_years": combined["incomplete_years"],
    }
