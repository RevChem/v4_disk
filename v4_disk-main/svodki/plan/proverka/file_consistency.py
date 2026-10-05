"""Чтение Excel сводок 1/2 и проверка «внутри файла».

Сводка 2: лист на операцию — верхняя таблица (культуры), «СОВОКУПНАЯ ПЛОЩАДЬ», «Итого».

Сводка 1: лист «Выраб. — <операция>» (Обработано за сутки) ↔ «Общий намолот» (Факт за сутки)
для уборки 5.0.

Ошибки чтения не прячутся за «пусто»: сводки 1 и 2 бросают ``ReportFileError``
(или возвращают проблемы через ``read_report{1,2}_workbook_checked``), чтобы сломанный,
пустой или нераспознанный файл не выглядел как «нечего проверять».
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
    "read_error": "файл сводки не прочитан",
}

class ReportFileError(RuntimeError):
    """Файл сводки не открылся, пуст или разобран не полностью.

    ``problems`` — список ``{"scope": лист или «файл», "message": текст}``.
    """

    def __init__(self, message: str, problems: list[dict[str, str]] | None = None) -> None:
        super().__init__(message)
        self.problems = list(problems or [])


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


def _problem(scope: str, message: str) -> dict[str, str]:
    return {"scope": scope, "message": message}


def read_report2_workbook_checked(
    path: Path,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    """(группа → разобранный лист, проблемы). Сама ничего не бросает.

    Проблема — всё, что нельзя молча принять за «нечего проверять»:
    файл не открылся; лист операции упал при разборе; на листе операции нет
    блока «СОВОКУПНАЯ ПЛОЩАДЬ ПО АГРОФИРМАМ» / «Итого»; в книге нет ни одного
    листа операции. Листы, имён которых нет в реестре операций, пропускаются
    намеренно (это не листы операций).
    """
    if load_workbook is None:
        return {}, [_problem("файл", "openpyxl не установлен, Excel не прочитать")]
    try:
        wb = load_workbook(path, read_only=False, data_only=True)
    except Exception as exc:
        return {}, [_problem("файл", f"не открылся: {type(exc).__name__}: {exc}")]

    out: dict[str, dict[str, Any]] = {}
    problems: list[dict[str, str]] = []
    try:
        for ws in wb.worksheets:
            group = group_for_sheet(ws.title)
            if not group:
                continue
            scope = f"лист «{ws.title}»"
            try:
                data = read_report2_sheet(ws)
            except Exception as exc:
                problems.append(_problem(scope, f"ошибка разбора: {type(exc).__name__}: {exc}"))
                continue
            if data is None:
                problems.append(
                    _problem(
                        scope,
                        "структура не распознана (нет блока "
                        f"«{SUMMARY_TITLE}» или строки «{_TOTAL_LABEL}»)",
                    )
                )
                continue
            out[group] = data
    finally:
        wb.close()
    if not out and not problems:
        problems.append(_problem("файл", "нет ни одного листа операции — файл пуст или не тот"))
    return out, problems


def _raise_if_problems(path: Path, problems: list[dict[str, str]]) -> None:
    if not problems:
        return
    text = "; ".join(f"{p['scope']}: {p['message']}" for p in problems)
    raise ReportFileError(f"{path.name}: {text}", problems)


def read_report2_workbook(path: Path) -> dict[str, dict[str, Any]]:
    """Группа работ → разобранный лист.

    Бросает ``ReportFileError``, если файл не открылся, пуст или хотя бы один
    лист операции не разобран. Раньше здесь возвращался ``{}``/пропуск листа,
    и битый файл выглядел как «нечего проверять».
    """
    data, problems = read_report2_workbook_checked(path)
    _raise_if_problems(path, problems)
    return data


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


_SEEDING_SUMMARY_TITLE = "Посев — Сводно по всем агрофирмам"
_SEEDING_GROUP = "4.0 Посев"


def read_report1_seeding_crops(path: Path) -> dict[str, dict[str, float]]:
    """Таблица «Посев — Сводно…» из xlsx сводки 1/5 → культура → га за сутки.

    ``{}`` — только когда таблицы в книге нет вообще (в сводке 1 это нормально:
    сравнивать нечего). Всё остальное — ошибка ``ReportFileError``: нет openpyxl,
    файл не открылся, лист не читается, таблица найдена, но шапка не распознана.
    """
    if load_workbook is None:
        raise ReportFileError(f"{path.name}: openpyxl не установлен, Excel не прочитать")
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        raise ReportFileError(
            f"{path.name}: не открылся: {type(exc).__name__}: {exc}",
            [_problem("файл", f"не открылся: {type(exc).__name__}: {exc}")],
        ) from exc
    out: dict[str, float] = {}
    try:
        for ws in wb.worksheets:
            scope = f"лист «{ws.title}»"
            try:
                rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
                title_row = None
                for i, r in enumerate(rows[:8]):
                    if any(_SEEDING_SUMMARY_TITLE in _text(c) for c in r):
                        title_row = i
                        break
                if title_row is None:
                    continue
                hdr_idx = crop_col = val_col = None
                for i in range(title_row, min(title_row + 6, len(rows))):
                    texts = [_text(c).replace("\n", " ") for c in rows[i]]
                    if _GENERAL_CROP_HEADER in texts and _GENERAL_VALUE_HEADER in texts:
                        hdr_idx = i
                        crop_col = texts.index(_GENERAL_CROP_HEADER)
                        val_col = texts.index(_GENERAL_VALUE_HEADER)
                        break
                if hdr_idx is None:
                    raise ReportFileError(
                        f"{path.name}: {scope}: таблица «{_SEEDING_SUMMARY_TITLE}» найдена, но нет шапки "
                        f"«{_GENERAL_CROP_HEADER}» / «{_GENERAL_VALUE_HEADER}»",
                        [_problem(scope, "таблица посева найдена, шапка не распознана")],
                    )
                for r in rows[hdr_idx + 1:]:
                    crop = _text(r[crop_col]) if crop_col < len(r) else ""
                    if not crop or crop == _TOTAL_LABEL:
                        if crop == _TOTAL_LABEL:
                            break
                        continue
                    value = _num(r[val_col]) if val_col < len(r) else None
                    if value is None:
                        continue
                    out[crop] = round(out.get(crop, 0.0) + value, 2)
            except ReportFileError:
                raise
            except Exception as exc:
                raise ReportFileError(
                    f"{path.name}: {scope}: ошибка разбора: {type(exc).__name__}: {exc}",
                    [_problem(scope, f"ошибка разбора: {type(exc).__name__}: {exc}")],
                ) from exc
            if out:
                break
    finally:
        wb.close()
    return {_SEEDING_GROUP: out} if out else {}


def read_report1_workbook_checked(path: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """(``{"perf", "general"}``, проблемы). Сама ничего не бросает.

    Проблема — лист «Выраб. — …» или «Общий намолот», который не прочитался
    (исключение или нераспознанная структура), файл, который не открылся, и
    книга без единого такого листа. Прочие листы пропускаются намеренно.
    """
    out: dict[str, Any] = {"perf": {}, "general": None}
    if load_workbook is None:
        return out, [_problem("файл", "openpyxl не установлен, Excel не прочитать")]
    try:
        wb = load_workbook(path, read_only=False, data_only=True)
    except Exception as exc:
        return out, [_problem("файл", f"не открылся: {type(exc).__name__}: {exc}")]
    problems: list[dict[str, str]] = []
    try:
        for ws in wb.worksheets:
            is_general = _text(ws.title) == GENERAL_SHEET
            group = None if is_general else _perf_group(ws.title)
            if not is_general and not group:
                continue
            scope = f"лист «{ws.title}»"
            try:
                data = _read_general_sheet(ws) if is_general else _read_perf_sheet(ws)
            except Exception as exc:
                problems.append(_problem(scope, f"ошибка разбора: {type(exc).__name__}: {exc}"))
                continue
            if data is None:
                hdr = (
                    f"«{_GENERAL_CROP_HEADER}» / «{_GENERAL_VALUE_HEADER}»"
                    if is_general
                    else f"«{_PERF_FIRM_HEADER}» / «{_PERF_VALUE_HEADER}»"
                )
                problems.append(
                    _problem(scope, f"структура не распознана (нет шапки {hdr} или строки «{_TOTAL_LABEL}»)")
                )
                continue
            if is_general:
                out["general"] = data
            else:
                out["perf"][group] = data
    finally:
        wb.close()
    if not out["perf"] and out["general"] is None and not problems:
        problems.append(_problem("файл", "нет ни одного листа «Выраб. — …» / «Общий намолот» — файл пуст или не тот"))
    return out, problems


def read_report1_workbook(path: Path) -> dict[str, Any]:
    """``{"perf": группа → лист, "general": лист «Общий намолот» | None}``.

    Бросает ``ReportFileError``, если файл не открылся, пуст или хотя бы один
    нужный лист не разобран. Раньше такие случаи давали пустой результат.
    """
    data, problems = read_report1_workbook_checked(path)
    _raise_if_problems(path, problems)
    return data


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


def _error_row(report_id: str, scope: str, message: str, source: str) -> dict[str, Any]:
    """Строка таблицы «Листы сводки» для сбоя чтения: статус ERROR, а не OK и не пропуск."""
    return {
        "report_id": str(report_id),
        "group_name": scope,
        "display_name": scope,
        "source": source,
        "total_ha": None,
        "firms_ha": None,
        "general_ha": None,
        "crops_ha": None,
        "max_diff_ha": 0.0,
        "status": "ERROR",
        "issues": [
            {
                "kind": "read_error",
                "label": ISSUE_LABELS["read_error"],
                "message": message,
                "expected": None,
                "actual": None,
                "diff_ha": None,
            }
        ],
    }


def report2_sheet_check_rows(path: Path) -> list[dict[str, Any]]:
    """Строки проверки листов сводки 2. Исправные листы проверяются как обычно,
    а нечитаемые получают строку со статусом ERROR — их нельзя пропускать молча."""
    rows: list[dict[str, Any]] = []
    data_by_group, problems = read_report2_workbook_checked(path)
    for group, data in data_by_group.items():
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
    for prob in problems:
        rows.append(_error_row("2", f"{path.name}: {prob['scope']}", prob["message"], "xlsx"))
    return rows


def report1_sheet_check_rows_from_path(path: Path) -> list[dict[str, Any]]:
    """Строки проверки листов сводки 1. Исправные листы проверяются как обычно,
    нечитаемые получают строку ERROR (их нельзя пропускать молча)."""
    data, problems = read_report1_workbook_checked(path)
    rows = report1_sheet_check_rows(data)
    for prob in problems:
        rows.append(_error_row("1", f"{path.name}: {prob['scope']}", prob["message"], "xlsx"))
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
    """Строки «Согласованность листов» для сводок 1 и 2.

    Сбой проверки одной сводки не останавливает другую и не теряется в логе:
    он возвращается строкой со статусом ERROR.
    """
    import logging

    from proverka import file_values as fv

    wanted = {str(x) for x in report_ids}
    out: list[dict[str, Any]] = []

    if "1" in wanted:
        try:
            xlsx = fv.find_report_xlsx(report_date, "1")
            xlsx_rows = report1_sheet_check_rows_from_path(xlsx) if xlsx else []
            rows = [r for r in xlsx_rows if r["status"] != "ERROR"]
            errors = [r for r in xlsx_rows if r["status"] == "ERROR"]
            if not rows:
                # Excel не дал ни одной проверки — берём PDF, но ошибки чтения Excel
                # остаются в результате: запасной путь не должен их заглушать.
                pdf = fv.find_report_file(report_date, "1", ".pdf")
                rows = _pdf_report1_rows(pdf) if pdf else []
            out.extend(rows)
            out.extend(errors)
        except Exception as exc:
            logging.getLogger(__name__).exception("proverka: согласованность листов сводки 1 не посчитана")
            out.append(_error_row("1", "сводка 1", f"{type(exc).__name__}: {exc}", "file"))
    if "2" in wanted:
        try:
            xlsx = fv.find_report_xlsx(report_date, "2")
            if xlsx:
                out.extend(report2_sheet_check_rows(xlsx))
        except Exception as exc:
            logging.getLogger(__name__).exception("proverka: согласованность листов сводки 2 не посчитана")
            out.append(_error_row("2", "сводка 2", f"{type(exc).__name__}: {exc}", "file"))
    return out
