"""Сезонные гектары («Выполнено», «Факт общий») из готового Excel сводки.

Суточные значения читает file_values.py. Здесь только накопленный за сезон факт:

* сводка 2, лист операции: «Выполнено, га» в таблице «СОВОКУПНАЯ ПЛОЩАДЬ ПО АГРОФИРМАМ»
  (итог и агрофирмы) и «Выполнено работ в Га» в таблице по культурам;
* сводки 1 и 10: лист «Общий намолот» (5.0 Уборка) и лист овощей, колонка «Факт общий, га».

Кукуруза: строка «Кукуруза, из них:» — это сумма силоса и зерна, она не читается;
силос и зерно возвращаются отдельными строками (как в сводке 1).

Ошибка чтения не превращается в «пусто»: бросается RuntimeError с именем файла.
Нет листа или нет сезонной колонки — группа просто не попадает в результат.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from openpyxl import load_workbook
except ImportError:  # pragma: no cover
    load_workbook = None  # type: ignore[assignment]

from proverka.file_consistency import (
    _CHILD_CROP_LABELS,
    _CHILD_CROPS,
    _CORN_PARENT,
    _FIRM_HEADER,
    _GENERAL_CROP_HEADER,
    _PARENT_CROP_ALIASES,
    _TOTAL_LABEL,
    SUMMARY_TITLE,
    _num,
    _text,
    group_for_sheet,
)

# Заголовки сезонных колонок (после схлопывания пробелов и переводов строки).
SEASON_FIRM_HEADER = "Выполнено, га"
SEASON_CROP_HEADER = "Выполнено работ в Га"
SEASON_GENERAL_HEADER = "Факт общий, га"

HARVEST_GROUP = "5.0 Уборка"
VEG_HARVEST_GROUP = "5.4 Уборка картофеля, лука"
_GENERAL_SHEETS = {"общий намолот": HARVEST_GROUP}
_VEG_SHEETS = {"овощи": VEG_HARVEST_GROUP, "сводка овощи": VEG_HARVEST_GROUP}

# Сводки, у которых в Excel есть сезонный факт.
SEASON_REPORT_IDS = ("1", "2", "10")


def _flat(value: Any) -> str:
    return " ".join(str(value or "").replace("\n", " ").split()).casefold()


def _index_of(values: tuple[Any, ...] | list[Any], header: str) -> int | None:
    wanted = _flat(header)
    for i, cell in enumerate(values):
        if _flat(cell) == wanted:
            return i
    return None


def _open(path: Path):
    if load_workbook is None:
        raise RuntimeError(f"{path.name}: ImportError: нет openpyxl")
    try:
        return load_workbook(path, read_only=False, data_only=True)
    except Exception as exc:
        raise RuntimeError(f"{path.name}: {type(exc).__name__}: {exc}") from exc


def _is_corn_parent(label: str) -> bool:
    folded = label.casefold()
    return folded == _CORN_PARENT or folded.startswith(_CORN_PARENT)


def read_report2_sheet_season(ws: Any) -> dict[str, Any] | None:
    """Лист операции сводки 2 → {"total", "firms", "crops"}. None — структура не узнана."""
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
    bot_col = _index_of(rows[bot_hdr], SEASON_FIRM_HEADER)
    top_col = _index_of(rows[top_hdr], SEASON_CROP_HEADER)
    if bot_col is None:
        return None

    firms: dict[str, float] = {}
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
            firms[label] = round(value, 2)
    if total is None:
        return None

    crops: dict[str, float] = {}
    if top_col is not None:
        plain: dict[str, float] = {}
        children: dict[str, float] = {}
        corn_parent: float | None = None
        for r in rows[top_hdr + 1:title_idx]:
            firm = _text(r[0]) if r else ""
            crop = _text(r[1]) if len(r) > 1 else ""
            if not firm or not crop:
                continue
            value = _num(r[top_col]) if top_col < len(r) else None
            if value is None:
                continue
            folded = crop.casefold()
            if folded in _CHILD_CROPS:
                label = _CHILD_CROP_LABELS[folded]
                children[label] = round(children.get(label, 0.0) + value, 2)
            elif folded in _PARENT_CROP_ALIASES or _is_corn_parent(crop):
                # Сумма силоса и зерна: гектары уже в дочерних строках.
                corn_parent = round((corn_parent or 0.0) + value, 2)
            else:
                plain[crop] = round(plain.get(crop, 0.0) + value, 2)
        crops = dict(plain)
        if children:
            crops.update(children)
        elif corn_parent is not None:
            crops["Кукуруза"] = corn_parent
    return {"total": round(total, 2), "firms": firms, "crops": crops}


def read_general_sheet_season(ws: Any) -> dict[str, Any] | None:
    """«Общий намолот» / «Овощи» сводок 1 и 10: культура → «Факт общий, га»."""
    rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
    hdr_idx = crop_col = val_col = None
    for i, r in enumerate(rows[:5]):
        crop_at = _index_of(r, _GENERAL_CROP_HEADER)
        value_at = _index_of(r, SEASON_GENERAL_HEADER)
        if crop_at is not None and value_at is not None:
            hdr_idx, crop_col, val_col = i, crop_at, value_at
            break
    if hdr_idx is None:
        return None
    crops: dict[str, float] = {}
    total: float | None = None
    for r in rows[hdr_idx + 1:]:
        crop = _text(r[crop_col]) if crop_col < len(r) else ""
        if not crop:
            continue
        value = _num(r[val_col]) if val_col < len(r) else None
        if crop == _TOTAL_LABEL:
            total = None if value is None else round(value, 2)
            break
        if value is None or _is_corn_parent(crop):
            continue
        crops[crop] = round(crops.get(crop, 0.0) + value, 2)
    if total is None:
        return None
    return {"total": total, "firms": {}, "crops": crops}


def season_areas_from_path(path: Path, report_id: str) -> dict[str, dict[str, Any]]:
    """Операция → {"total", "firms", "crops"} за сезон из Excel сводки.

    Для PDF и сводок без сезонной колонки возвращается {}.
    """
    rid = str(report_id)
    if path.suffix.lower() != ".xlsx" or rid not in SEASON_REPORT_IDS:
        return {}
    wb = _open(path)
    out: dict[str, dict[str, Any]] = {}
    try:
        for ws in wb.worksheets:
            title = _flat(ws.title)
            try:
                if rid == "2":
                    group = group_for_sheet(ws.title)
                    if not group:
                        continue
                    data = read_report2_sheet_season(ws)
                    if data:
                        out[group] = data
                    continue
                group = _GENERAL_SHEETS.get(title) or _VEG_SHEETS.get(title)
                if not group:
                    continue
                data = read_general_sheet_season(ws)
                if data:
                    out[group] = data
            except Exception as exc:
                raise RuntimeError(
                    f"{path.name}: лист «{ws.title}»: {type(exc).__name__}: {exc}"
                ) from exc
    finally:
        wb.close()
    return out
