"""Сверка факта за смену по культурам: сводка ↔ Cropwise UI (вкладка cropRows)."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from proverka.cropwise_ui import (
    _normalize_name,
    parse_work_report_detail_rows,
    subgroup_name_to_firm,
)
from proverka.ha_thresholds import DIFF_MIN_HA


def _body_is_corn_silage_mowing(body: dict | None, fact_hints: list[str]) -> bool:
    """Колонка факта только из скашивания кукурузы (6.3 …) — культура = силос."""
    if not isinstance(body, dict):
        return False
    from proverka.cropwise_ui import column_work_types, matching_fact_columns

    cols = body.get("cropColumns") or body.get("regionColumns") or []
    fact_cols = matching_fact_columns(cols, list(fact_hints))
    text = " ".join(
        name for col in fact_cols for name in column_work_types(col)
    ).casefold()
    if "скашив" in text and "кукуруз" in text:
        return True
    if "6.3" in text:
        return True
    return False


def _crop_label(raw: str) -> str:
    try:
        from proverka.extractors import _crop_label as label_fn

        return label_fn(raw)
    except Exception:
        return str(raw or "").strip()


def cropwise_area_by_firm_crop(
    body: Any,
    firm_names: list[str],
    fact_hints: list[str],
) -> dict[str, dict[str, float]]:
    """Культура → агрофирма → га из дерева cropRows work.json."""
    if not isinstance(body, dict):
        return {}
    rows = body.get("cropRows") or []
    cols = body.get("cropColumns") or []
    if not rows or not cols:
        return {}
    detail = parse_work_report_detail_rows(rows, cols, firm_names, list(fact_hints))
    out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    firm_set = set(firm_names)
    for d in detail:
        if d.get("aggregate"):
            continue
        path = str(d.get("path") or "")
        parts = [p.strip() for p in path.split("→") if p.strip()]
        if not parts:
            continue
        crop = _crop_label(parts[0])
        if _body_is_corn_silage_mowing(body, list(fact_hints)):
            if "кукуруз" in crop.casefold() and "зерно" in crop.casefold():
                try:
                    from utils.shared.task_area_shared import CORN_SILAGE_CROP

                    crop = CORN_SILAGE_CROP
                except ImportError:
                    crop = "Кукуруза на силос"
        firm = str(d.get("firm") or "").strip()
        if not firm and len(parts) > 1:
            firm = parts[1].strip()
        if firm not in firm_set:
            firm = subgroup_name_to_firm().get(_normalize_name(firm), "")
        if firm not in firm_set:
            continue
        ha = float(d.get("area_ha") or 0)
        if ha <= 0:
            continue
        out[crop][firm] = round(out[crop].get(firm, 0.0) + ha, 2)
    return {c: dict(by_firm) for c, by_firm in out.items()}


# Группы, где культуры вне плана сворачиваются в «Иные культуры» (как в сводке 2).
_FOLD_TO_OTHER_GROUPS = ("2.4 Вспашка", "2.9 Дискование")

# Режимы правил редактора на стороне Cropwise (какую функцию plans.py вызывает сама сводка):
#   remap  — сводки 2, 3: crop_code_for_report, вне плана на вспашке/дисковании → «Иные»;
#   totals — сводки 4, 7, 8, 10, 11: is_crop_excluded_from_totals (вне плана не считается);
#   user   — сводки 1, 5, 6: только is_crop_user_excluded (скрытие, ранг 0, «Не считать»).
MODE_REMAP = "remap"
MODE_TOTALS = "totals"
MODE_USER = "user"


def _normalize_mode(remap: bool | str) -> str:
    """Старый булев ``remap`` (True/False) остаётся рабочим."""
    if isinstance(remap, str):
        return remap if remap in (MODE_REMAP, MODE_TOTALS, MODE_USER) else MODE_USER
    return MODE_REMAP if remap else MODE_USER


def _crop_code_after_editor(plans_mod: Any, mode: str, group_name: str, firm: str, crop: str) -> str | None:
    """Имя культуры в сводке после правил редактора. None — гектары в итог не входят."""
    if plans_mod.is_crop_user_excluded(group_name, firm, crop):
        return None
    if mode == MODE_USER:
        return str(crop)
    if mode == MODE_TOTALS:
        if plans_mod.is_crop_excluded_from_totals(group_name, firm, crop):
            return None
        return str(crop)
    code = plans_mod.crop_code_for_report(group_name, crop)
    if code is None:
        if str(group_name) in _FOLD_TO_OTHER_GROUPS:
            return plans_mod.OTHER_CROP_CODE
        return None
    return code


def _load_plans() -> Any | None:
    try:
        import plans as _plans
    except Exception:
        return None
    return _plans


def apply_editor_crop_rules(
    group_name: str | None,
    by_firm_crop: dict[str, dict[str, float]] | None,
    *,
    remap: bool | str = True,
) -> dict[str, dict[str, float]]:
    """Культуры Cropwise → как их показывает сводка после «Редактора планов».

    ``remap``: True / ``"remap"`` (сводки 2 и 3) — как ``crop_code_for_report``, на вспашке и
    дисковании культуры вне плана сворачиваются в «Иные культуры». ``"totals"`` (сводки
    4, 7, 8, 10, 11) — исключённые и вне плана культуры не считаются, остальные имена
    остаются как в сводке. False / ``"user"`` (сводки 1, 5, 6) — только скрытые, ранг 0 и
    «Не считать». Без группы или при сбое импорта plans данные возвращаются как есть.
    """
    src = by_firm_crop or {}
    if not group_name or not src:
        return src
    plans_mod = _load_plans()
    if plans_mod is None:
        return src
    mode = _normalize_mode(remap)
    out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for crop, by_firm in src.items():
        for firm, ha in (by_firm or {}).items():
            code = _crop_code_after_editor(plans_mod, mode, str(group_name), firm, str(crop))
            if code is None:
                continue
            out[code][firm] += float(ha or 0)
    return {c: dict(f) for c, f in out.items()}


def editor_excluded_by_firm(
    group_name: str | None,
    by_firm_crop: dict[str, dict[str, float]] | None,
    *,
    remap: bool | str = True,
) -> dict[str, float]:
    """Га Cropwise по фирмам, которые редактор исключил из сводки (скрыто, «Не считать», вне плана)."""
    src = by_firm_crop or {}
    if not group_name or not src:
        return {}
    plans_mod = _load_plans()
    if plans_mod is None:
        return {}
    mode = _normalize_mode(remap)
    out: dict[str, float] = defaultdict(float)
    for crop, by_firm in src.items():
        for firm, ha in (by_firm or {}).items():
            if _crop_code_after_editor(plans_mod, mode, str(group_name), firm, str(crop)) is None:
                out[firm] += float(ha or 0)
    return {firm: round(ha, 2) for firm, ha in out.items() if ha >= 0.01}


def adjust_cropwise_for_editor(
    group_name: str | None,
    crops_by_firm: dict[str, dict[str, float]] | None,
    total_ha: float,
    by_firm: dict[str, float] | None,
    *,
    remap: bool | str = True,
) -> dict[str, Any]:
    """Итог Cropwise и итоги по фирмам без гектаров культур, исключённых в редакторе.

    Вычитаем исключённые гектары из уже посчитанных сумм work.json (а не пересобираем их
    из cropRows): в сумме могут быть строки, которых нет в дереве культур.
    Без группы или без разбивки по культурам суммы возвращаются как есть.
    """
    firms = {str(f): float(v or 0) for f, v in (by_firm or {}).items()}
    excluded = editor_excluded_by_firm(group_name, crops_by_firm, remap=remap)
    if not excluded:
        return {
            "total_ha": round(float(total_ha or 0), 2),
            "by_firm": {f: round(v, 2) for f, v in firms.items()},
            "excluded_ha": 0.0,
            "excluded_by_firm": {},
        }
    adj_firms = {f: round(max(v - excluded.get(f, 0.0), 0.0), 2) for f, v in firms.items()}
    excl_total = round(sum(excluded.values()), 2)
    return {
        "total_ha": round(max(float(total_ha or 0) - excl_total, 0.0), 2),
        "by_firm": adj_firms,
        "excluded_ha": excl_total,
        "excluded_by_firm": excluded,
    }


def editor_rules_for(report_id: str, group_name: str) -> tuple[str | None, str]:
    """Правила редактора для пары (сводка, группа): (группа или None, режим).

    None — правила не применяются (сводка 9: у неё свой маппинг вспашки и глубокорыхления).
    Режимы: ``remap`` — сводки 2 и 3; ``totals`` — 4, 7, 8, 10, 11; ``user`` — 1, 5, 6.
    """
    rid = str(report_id)
    if rid in {"2", "3"}:
        return group_name, MODE_REMAP
    if rid in {"4", "7", "8", "10", "11"}:
        return group_name, MODE_TOTALS
    if rid in {"1", "5", "6"}:
        return group_name, MODE_USER
    return None, MODE_USER


def compare_svodka_cropwise_crops(
    svodka_by_firm_crop: dict[str, dict[str, float]] | None,
    cropwise_by_firm_crop: dict[str, dict[str, float]] | None,
    *,
    tolerance_ha: float = DIFF_MIN_HA,
    group_name: str | None = None,
    remap: bool | str = True,
) -> list[dict[str, Any]]:
    """
    Список сравнений по культурам (сумма по всем АФ + разбивка по фирмам).
    Сводка без area_today_by_firm_crop не участвует — только Cropwise не с чем.
    group_name — группа работ: к Cropwise применяются правила редактора планов.
    Порог один с быстрой сверкой файла: DIFF_MIN_HA. tolerance_ha оставлен для
    старых вызовов и не расширяет допуск ниже этого порога.
    """
    sv = svodka_by_firm_crop or {}
    cw = apply_editor_crop_rules(group_name, cropwise_by_firm_crop, remap=remap)
    if not sv:
        return []
    crops = sorted(set(sv.keys()) | set(cw.keys()))
    rows: list[dict[str, Any]] = []
    for crop in crops:
        sf = sv.get(crop) or {}
        cf = cw.get(crop) or {}
        sv_total = round(sum(float(v or 0) for v in sf.values()), 2)
        cw_total = round(sum(float(v or 0) for v in cf.values()), 2)
        diff = round(sv_total - cw_total, 2)
        if sv_total < 0.01 and cw_total < 0.01:
            continue
        limit = min(float(tolerance_ha), DIFF_MIN_HA)
        status = "OK" if abs(diff) < limit else "DIFF"
        firm_rows: list[dict[str, Any]] = []
        for firm in sorted(set(sf.keys()) | set(cf.keys())):
            sv_ha = round(float(sf.get(firm) or 0), 2)
            cw_ha = round(float(cf.get(firm) or 0), 2)
            fd = round(sv_ha - cw_ha, 2)
            if sv_ha < 0.01 and cw_ha < 0.01:
                continue
            flimit = min(float(tolerance_ha), DIFF_MIN_HA)
            fstatus = "OK" if abs(fd) < flimit else "DIFF"
            firm_rows.append({
                "firm": firm,
                "svodka_ha": sv_ha,
                "cropwise_ha": cw_ha,
                "diff_ha": fd,
                "status": fstatus,
            })
        rows.append({
            "crop": crop,
            "svodka_ha": sv_total,
            "cropwise_ha": cw_total,
            "diff_ha": diff,
            "status": status,
            "firms": firm_rows,
        })
    return rows
