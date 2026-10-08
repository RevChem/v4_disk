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


def apply_editor_crop_rules(
    group_name: str | None,
    by_firm_crop: dict[str, dict[str, float]] | None,
    *,
    remap: bool = True,
) -> dict[str, dict[str, float]]:
    """Культуры Cropwise → как их показывает сводка после «Редактора планов».

    Скрытые и «не считать» отбрасываются всегда. ``remap=True`` (сводки 2 и 3) ещё
    сворачивает культуры так же, как ``crop_code_for_report``: на вспашке и дисковании
    вне плана — в «Иные культуры». ``remap=False`` (сводки 4, 5, 6, 8) только убирает
    исключённые культуры и оставляет остальные имена как в сводке.
    Без группы или при сбое импорта plans данные возвращаются как есть.
    """
    src = by_firm_crop or {}
    if not group_name or not src:
        return src
    try:
        import plans as _plans
    except Exception:
        return src
    fold = str(group_name) in _FOLD_TO_OTHER_GROUPS
    out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for crop, by_firm in src.items():
        for firm, ha in (by_firm or {}).items():
            if _plans.is_crop_user_excluded(group_name, firm, crop):
                continue
            if not remap:
                out[str(crop)][firm] += float(ha or 0)
                continue
            code = _plans.crop_code_for_report(group_name, crop)
            if code is None:
                if not fold:
                    continue
                code = _plans.OTHER_CROP_CODE
            out[code][firm] += float(ha or 0)
    return {c: dict(f) for c, f in out.items()}


def editor_rules_for(report_id: str, group_name: str) -> tuple[str | None, bool]:
    """Правила редактора для пары (сводка, группа): (группа или None, remap).

    None — правила не применяются (сводки 1, 9, 10, 11; у 9 свой маппинг).
    remap=True — сводки 2 и 3, как crop_code_for_report.
    remap=False — сводки 4, 5, 6, 7 и 8: только скрытые и «не считать».
    """
    rid = str(report_id)
    if rid in {"2", "3"}:
        return group_name, True
    if rid in {"4", "5", "6", "7", "8"}:
        return group_name, False
    return None, False


def compare_svodka_cropwise_crops(
    svodka_by_firm_crop: dict[str, dict[str, float]] | None,
    cropwise_by_firm_crop: dict[str, dict[str, float]] | None,
    *,
    tolerance_ha: float = DIFF_MIN_HA,
    group_name: str | None = None,
    remap: bool = True,
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
