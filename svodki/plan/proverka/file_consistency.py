"""Чтение Excel сводок 1/2 и проверка «внутри файла».

Сводка 2: лист на операцию — верхняя таблица (культуры), «СОВОКУПНАЯ ПЛОЩАДЬ», «Итого».

Сводка 1: лист «Выраб. — <операция>» (Обработано за сутки) ↔ «Общий намолот» (Факт за сутки)
для уборки 5.0.

Модуль ничего не бросает наружу.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from openpyxl import load_workbook
except ImportError:  # pragma: no cover
    load_workbook = None  # type: ignore[assignment]

from proverka.ha_thresholds import DIFF_MIN_HA

SUMMARY_TITLE = "СОВОКУПНАЯ ПЛОЩАДЬ ПО АГРОФИРМАМ"
_TOTAL_LABEL = "Итого"
_FIRM_HEADER = "Агрофирма"
_CHILD_CROPS = frozenset({"кукуруза на силос", "кукуруза на зерно"})
_PARENT_CROP_ALIASES = {"кукуруза, из них:": "Кукуруза"}
_ROUNDING_PER_ROW_HA = 0.05

ISSUE_LABELS = {
    "firms_vs_total": "сумма по АФ ↔ «Итого»",
    "crops_vs_total": "сумма по культурам ↔ «Итого»",
    "firm_crops_vs_firm": "культуры по АФ ↔ строка АФ",
    "perf_vs_general": "лист выработки ↔ «Общий намолот»",
    "general_vs_crops": "«Общий намолот» Итого ↔ сумма культур",
}

# Сводка 1
PERF_PREFIX = "Выраб. —"
PERF_SHEET_NO_PREFIX = "Выработка техники"
PERF_SHEET_NO_PREFIX_GROUP = "1.1 Внесение минеральных удобрений"
GENERAL_SHEET = "Общий намолот"
HARVEST_GROUP = "5.0 Уборка"
_PERF_FIRM_HEADER = "Агрофирмы"
_PERF_VALUE_HEADER = "Обработано за сутки, га"
_GENERAL_CROP_HEADER = "Культура"
_GENERAL_VALUE_HEADER = "Факт за сутки, га"
_CORN_PARENT = "кукуруза, из них"
_ROUNDING_XLSX_R1_HA = 0.01


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        if isinstance(value, str):
            raw = value.replace(" ", "").replace(",", ".")
            try:
                return float(raw)
            except ValueError:
                return None
        return None
    return float(value)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _norm(name: str) -> str:
    return "".join(ch for ch in str(name or "").casefold().replace("ё", "е") if ch.isalnum())


def _last_filled_index(row: tuple[Any, ...]) -> int | None:
    idx = None
    for i, cell in enumerate(row):
        if cell not in (None, ""):
            idx = i
    return idx


def _issue(kind: str, expected: float, actual: float, **extra: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "label": ISSUE_LABELS.get(kind, kind),
        "expected": round(expected, 2),
        "actual": round(actual, 2),
        "diff_ha": round(actual - expected, 2),
        **extra,
    }


def group_for_sheet(sheet_name: str) -> str | None:
    """Имя листа → группа работ. Excel режет имена листов до 31 символа."""
    from proverka.operation_registry import OPERATION_PEERS

    name = _text(sheet_name)
    for group in OPERATION_PEERS:
        if group == name or (len(name) >= 28 and group.startswith(name)):
            return group
    return None


def read_report2_sheet(ws: Any) -> dict[str, Any] | None:
    """Разобрать один лист операции сводки 2. None — если структура не узнана."""
    rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
    title_idx = next(
        (i for i, r in enumerate(rows) if r and _text(r[0]).upper() == SUMMARY_TITLE),
        None,
    )
    if title_idx is None or title_idx + 1 >= len(rows):
        return None
    top_hdr = next(
        (i for i in range(title_idx) if rows[i] and _text(rows[i][0]) == _FIRM_HEADER),
        None,
    )
    bot_hdr = title_idx + 1
    if top_hdr is None or _text(rows[bot_hdr][0]) != _FIRM_HEADER:
        return None
    top_col = _last_filled_index(rows[top_hdr])
    bot_col = _last_filled_index(rows[bot_hdr])
    if top_col is None or bot_col is None:
        return None

    by_firm: dict[str, float] = {}
    total: float | None = None
    for r in rows[bot_hdr + 1:]:
        label = _text(r[0]) if r else ""
        if not label:
            break
        value = _num(r[bot_col]) if bot_col < len(r) else None
        if label == _TOTAL_LABEL:
            total = value
            break
        if value is not None:
            by_firm[label] = round(value, 2)
    if total is None:
        return None

    by_crop: dict[str, float] = {}
    firm_from_crops: dict[str, float] = {}
    nonzero_by_firm: dict[str, int] = {}
    nonzero = 0
    for r in rows[top_hdr + 1:title_idx]:
        firm = _text(r[0]) if r else ""
        crop = _text(r[1]) if len(r) > 1 else ""
        if not firm or not crop:
            continue
        if crop.casefold() in _CHILD_CROPS:
            continue
        crop = _PARENT_CROP_ALIASES.get(crop.casefold(), crop)
        value = _num(r[top_col]) if top_col < len(r) else None
        if value is None:
            continue
        by_crop[crop] = round(by_crop.get(crop, 0.0) + value, 2)
        key = _norm(firm)
        firm_from_crops[key] = round(firm_from_crops.get(key, 0.0) + value, 2)
        if value:
            nonzero += 1
            nonzero_by_firm[key] = nonzero_by_firm.get(key, 0) + 1
    return {
        "date_label": _text(rows[bot_hdr][bot_col]),
        "total": round(total, 2),
        "by_firm": by_firm,
        "by_crop": by_crop,
        "firm_from_crops": firm_from_crops,
        "nonzero_rows": nonzero,
        "nonzero_rows_by_firm": nonzero_by_firm,
    }


def read_report2_workbook(path: Path) -> dict[str, dict[str, Any]]:
    """Группа работ → разобранный лист. Пусто, если файл не открылся."""
    if load_workbook is None:
        return {}
    try:
        wb = load_workbook(path, read_only=False, data_only=True)
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    try:
        for ws in wb.worksheets:
            group = group_for_sheet(ws.title)
            if not group:
                continue
            try:
                data = read_report2_sheet(ws)
            except Exception:
                data = None
            if data:
                out[group] = data
    finally:
        wb.close()
    return out


def internal_issues(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Расхождения внутри одного листа сводки 2."""
    issues: list[dict[str, Any]] = []
    total = float(data["total"])
    firms_sum = round(sum(data["by_firm"].values()), 2)
    if abs(firms_sum - total) >= DIFF_MIN_HA:
        issues.append(_issue("firms_vs_total", total, firms_sum))
    if data["by_crop"]:
        crops_sum = round(sum(data["by_crop"].values()), 2)
        tol = DIFF_MIN_HA + _ROUNDING_PER_ROW_HA * int(data["nonzero_rows"])
        if abs(crops_sum - total) > tol:
            issues.append(_issue("crops_vs_total", total, crops_sum))
        for firm, firm_ha in data["by_firm"].items():
            key = _norm(firm)
            if key not in data["firm_from_crops"]:
                continue
            crops_ha = data["firm_from_crops"][key]
            n = int(data["nonzero_rows_by_firm"].get(key, 0))
            if abs(crops_ha - firm_ha) > DIFF_MIN_HA + _ROUNDING_PER_ROW_HA * n:
                issues.append(_issue("firm_crops_vs_firm", firm_ha, crops_ha, firm=firm))
    return issues


def report2_xlsx_areas(path: Path) -> dict[str, float]:
    return {g: d["total"] for g, d in read_report2_workbook(path).items()}


def report2_xlsx_firm_areas(path: Path) -> dict[str, dict[str, float]]:
    return {g: dict(d["by_firm"]) for g, d in read_report2_workbook(path).items()}


def report2_xlsx_crop_areas(path: Path) -> dict[str, dict[str, float]]:
    return {g: dict(d["by_crop"]) for g, d in read_report2_workbook(path).items() if d["by_crop"]}


def report2_xlsx_internal_issues(path: Path) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for group, data in read_report2_workbook(path).items():
        found = internal_issues(data)
        if found:
            out[group] = found
    return out


def _perf_group(sheet_title: str) -> str | None:
    title = _text(sheet_title)
    if title == PERF_SHEET_NO_PREFIX:
        return PERF_SHEET_NO_PREFIX_GROUP
    if not title.startswith(PERF_PREFIX):
        return None
    return group_for_sheet(title[len(PERF_PREFIX):].strip())


def _read_perf_sheet(ws: Any) -> dict[str, Any] | None:
    rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
    hdr_idx = firm_col = val_col = None
    for i, r in enumerate(rows[:5]):
        texts = [_text(c) for c in r]
        if _PERF_FIRM_HEADER in texts and _PERF_VALUE_HEADER in texts:
            hdr_idx = i
            firm_col = texts.index(_PERF_FIRM_HEADER)
            val_col = texts.index(_PERF_VALUE_HEADER)
            break
    if hdr_idx is None:
        return None
    by_firm: dict[str, float] = {}
    total: float | None = None
    for r in rows[hdr_idx + 1:]:
        label = _text(r[firm_col]) if firm_col < len(r) else ""
        if not label:
            continue
        value = _num(r[val_col]) if val_col < len(r) else None
        if value is None:
            continue
        if label == _TOTAL_LABEL:
            total = round(value, 2)
            break
        by_firm[label] = round(value, 2)
    if total is None:
        return None
    return {"total": total, "by_firm": by_firm}


def _read_general_sheet(ws: Any) -> dict[str, Any] | None:
    rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
    hdr_idx = crop_col = val_col = None
    for i, r in enumerate(rows[:5]):
        texts = [_text(c).replace("\n", " ") for c in r]
        if _GENERAL_CROP_HEADER in texts and _GENERAL_VALUE_HEADER in texts:
            hdr_idx = i
            crop_col = texts.index(_GENERAL_CROP_HEADER)
            val_col = texts.index(_GENERAL_VALUE_HEADER)
            break
    if hdr_idx is None:
        return None
    crops: dict[str, float] = {}
    total: float | None = None
    in_corn_children = False
    nonzero = 0
    for r in rows[hdr_idx + 1:]:
        crop = _text(r[crop_col]) if crop_col < len(r) else ""
        if not crop:
            continue
        value = _num(r[val_col]) if val_col < len(r) else None
        if crop == _TOTAL_LABEL:
            total = None if value is None else round(value, 2)
            break
        if value is None:
            continue
        folded = crop.casefold()
        if folded == _CORN_PARENT or folded.startswith(_CORN_PARENT):
            in_corn_children = True
            crops["Кукуруза"] = round(value, 2)
            nonzero += 1 if value else 0
            continue
        if in_corn_children and folded in _CHILD_CROPS:
            continue
        in_corn_children = False
        crops[crop] = round(crops.get(crop, 0.0) + value, 2)
        nonzero += 1 if value else 0
    if total is None:
        return None
    return {"total": total, "crops": crops, "nonzero_rows": nonzero}


def read_report1_workbook(path: Path) -> dict[str, Any]:
    out: dict[str, Any] = {"perf": {}, "general": None}
    if load_workbook is None:
        return out
    try:
        wb = load_workbook(path, read_only=False, data_only=True)
    except Exception:
        return out
    try:
        for ws in wb.worksheets:
            try:
                if _text(ws.title) == GENERAL_SHEET:
                    out["general"] = _read_general_sheet(ws)
                    continue
                group = _perf_group(ws.title)
                if group:
                    data = _read_perf_sheet(ws)
                    if data:
                        out["perf"][group] = data
            except Exception:
                continue
    finally:
        wb.close()
    return out


def _spread_ha(values: dict[str, float | None]) -> float:
    nums = [v for v in values.values() if v is not None]
    return round(max(nums) - min(nums), 2) if len(nums) >= 2 else 0.0


def _check_row(
    report_id: str,
    group: str,
    source: str,
    *,
    total: float | None,
    firms: float | None = None,
    crops: float | None = None,
    general: float | None = None,
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    from proverka.operation_registry import display_group_name

    values = {"total_ha": total, "firms_ha": firms, "general_ha": general, "crops_ha": crops}
    return {
        "report_id": str(report_id),
        "group_name": group,
        "display_name": display_group_name(group),
        "source": source,
        **values,
        "max_diff_ha": _spread_ha(values),
        "status": "DIFF" if issues else "OK",
        "issues": issues,
    }


def report1_sheet_check_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    general = data.get("general")
    for group, perf in sorted(data.get("perf", {}).items()):
        total = float(perf["total"])
        firms = round(sum(perf["by_firm"].values()), 2)
        issues: list[dict[str, Any]] = []
        if abs(firms - total) >= DIFF_MIN_HA:
            issues.append(_issue("firms_vs_total", total, firms))
        gen_total = crops_sum = None
        if group == HARVEST_GROUP and general:
            gen_total = float(general["total"])
            crops_sum = round(sum(general["crops"].values()), 2)
            if abs(gen_total - total) >= DIFF_MIN_HA:
                issues.append(_issue("perf_vs_general", total, gen_total))
            tol = DIFF_MIN_HA + _ROUNDING_XLSX_R1_HA * int(general["nonzero_rows"])
            if abs(crops_sum - gen_total) > tol:
                issues.append(_issue("general_vs_crops", gen_total, crops_sum))
        rows.append(
            _check_row(
                "1",
                group,
                "xlsx",
                total=total,
                firms=firms,
                crops=crops_sum,
                general=gen_total,
                issues=issues,
            )
        )
    return rows


def report2_sheet_check_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for group, data in read_report2_workbook(path).items():
        issues = internal_issues(data)
        firms = round(sum(data["by_firm"].values()), 2)
        crops = round(sum(data["by_crop"].values()), 2) if data["by_crop"] else None
        if not issues and max(abs(data["total"]), firms, crops or 0.0) < 0.01:
            continue
        rows.append(
            _check_row(
                "2",
                group,
                "xlsx",
                total=data["total"],
                firms=firms,
                crops=crops,
                issues=issues,
            )
        )
    return rows


def _pdf_report1_rows(path: Path) -> list[dict[str, Any]]:
    from proverka import file_values as fv

    perf = fv._perf_areas_from_pdf(path)
    crops_by_group = fv._daily_fact_crop_areas(path)
    rows: list[dict[str, Any]] = []
    for group, total in sorted(perf.items()):
        crops = crops_by_group.get(group)
        if not crops:
            continue
        crops_sum = round(sum(crops.values()), 2)
        n = sum(1 for v in crops.values() if v)
        issues: list[dict[str, Any]] = []
        if abs(crops_sum - total) > DIFF_MIN_HA + _ROUNDING_XLSX_R1_HA * n:
            issues.append(_issue("crops_vs_total", total, crops_sum))
        rows.append(
            _check_row(
                "1",
                group,
                "pdf",
                total=round(total, 2),
                crops=crops_sum,
                issues=issues,
            )
        )
    return rows


def build_sheet_checks(report_date: Any, report_ids: Any) -> list[dict[str, Any]]:
    """Строки «Согласованность листов» для сводок 1 и 2."""
    from proverka import file_values as fv

    wanted = {str(x) for x in report_ids}
    out: list[dict[str, Any]] = []
    try:
        if "1" in wanted:
            xlsx = fv.find_report_xlsx(report_date, "1")
            rows = report1_sheet_check_rows(read_report1_workbook(xlsx)) if xlsx else []
            if not rows:
                pdf = fv.find_report_file(report_date, "1", ".pdf")
                rows = _pdf_report1_rows(pdf) if pdf else []
            out.extend(rows)
        if "2" in wanted:
            xlsx = fv.find_report_xlsx(report_date, "2")
            if xlsx:
                out.extend(report2_sheet_check_rows(xlsx))
    except Exception:
        import logging

        logging.getLogger(__name__).exception("proverka: согласованность листов не посчитана")
    return out
