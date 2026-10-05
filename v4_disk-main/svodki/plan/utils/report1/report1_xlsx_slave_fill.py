# -*- coding: utf-8 -*-
"""
Подстановка данных в XLSX сводки 1 из «Сводка» отчётов 3–8 (вместо PDF-оверлея).

У листа «Выраб. — …» сводки 1 те же метрики, что у блока «Сводка» в отчётах 3–8:
колонки 1–10 мастера — из колонок 2–11 донора. Если у донора 12 колонок (отчёт 3),
мастер col10–11 берут из col11–12 донора («доступно» и «наёмные»). Иначе col11 мастера
(наёмные) остаётся из расчёта сводки 1, а col10 — из col11 донора.

Соответствие slave_id → суффикс листа «Выраб. — {суффикс}» как в 1_main_ammonium__1_.py WORK_GROUPS.
Отчёт 5: из листа «Сводка» берётся только блок «КУЛЬТУРЫ» (таблица выработки по посеву),
без овощных подтаблиц — PDF-страница донора 5 не подставляется.

Параметр окружения: SVODKI_REPORT1_XLSX_SLAVE_FILL=0 — отключить.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from copy import copy
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from openpyxl.styles import PatternFill
from openpyxl.utils import get_column_letter

# Маркер: grep REPORT1_HARVEST_PDF_GRAFT svodki/plan/report1_xlsx_slave_fill.py
REPORT1_HARVEST_PDF_GRAFT = "2026-07-16a"

# Как в REPORT1_PDF_GRAFT_FROM_SLAVES_SPEC: (стр.PDF сводки 1, report_id донора, стр.PDF донора).
# Номера страниц — запасной вариант; фактические берутся из листа мастера (resolve_graft_spec_pages).
# Листы сводки 10 → листы сводки 1 (стр. 2–3 PDF после удаления «Выраб. — 5.4»).
_REPORT10_HARVEST_SHEETS: tuple[tuple[str, str], ...] = (
    ("Общий намолот", "Общий намолот"),
    ("Овощи", "Овощи"),
)

# dst_page PDF сводки 1 → имя листа (для resolve_graft_spec_pages / PDF-подстановки).
# Стр. 2 — «Сводка овощи» (донор 11); стр. 3–4 — намолот/овощи (донор 10).
_REPORT11_SRC_PAGE_TO_SHEET: dict[int, str] = {
    2: "Сводка овощи",
}
_REPORT11_DST_PAGE_TO_SHEET: dict[int, str] = {
    2: "Сводка овощи",
}
_REPORT10_SRC_PAGE_TO_SHEET: dict[int, str] = {
    1: "Общий намолот",
    2: "Овощи",
}
_REPORT10_DST_PAGE_TO_SHEET: dict[int, str] = {
    3: "Общий намолот",
    4: "Овощи",
    # legacy (до листа «Сводка овощи» на стр. 2)
    2: "Общий намолот",
}

DEFAULT_GRAFT_SPEC: tuple[tuple[int, str, int], ...] = (
    (2, "11", 2),
    (3, "10", 1),
    (4, "10", 2),
    # Отчёт 3 не подставляем: 1.1 МУ сводка 1 считает сама (fetch_fertilizer_11_mtfmi).
    (33, "4", 1),
    (34, "7", 1),
    (11, "8", 1),
    (5, "5", 1),
    (14, "6", 1),
)

# Листы, которые заполняет только расчёт сводки 1 — восстанавливаем после подстановки доноров.
REPORT1_OWN_CALC_SHEETS: tuple[str, ...] = (
    "Выработка техники",
    "Сводка по культурам",
    "Свод по агрофирмам",
    "Свод по удобрениям (ХК)",
)

# report_id → суффикс названия листа «Выраб. — …» в сводке 1
# Отчёт 3 намеренно отсутствует: 1.1 МУ считает только сводка 1.
_SLAVE_TO_PERF_SUFFIX: dict[str, str] = {
    "4": "2.1 Боронование",
    "5": "4.0 Посев",
    "6": "3.1 Опрыскивание",
    "7": "2.9 Дискование",
    "8": "2.11 Культивация",
}

# Доп. имена листов мастера (порядок важен).
_SLAVE_MASTER_SHEET_SUFFIXES: dict[str, tuple[str, ...]] = {
    "4": ("2.1 Боронование",),
    "5": ("4.0 Посев",),
    "6": ("3.1 Опрыскивание",),
    "7": ("2.9 Дискование",),
    "8": ("2.11 Культивация",),
}

# Переопределение целевого листа по (pdf_page, slave_id)
_SUFFIX_OVERRIDES: dict[tuple[int, str], str] = {}

DONOR_SUMMARY_SHEET = "Сводка"

# Лист «Сводка» донора 8 — только культивация (wt 10,50,51,63,96), без фрезерования.
_SLAVE_DONOR_SUMMARY_SHEET: dict[str, str] = {
    "8": "Сводка",
}

# Доп. листы того же XLSX-донора → листы «Выраб. — …» сводки 1 (slave_id, donor_sheet, perf_suffix).
_SLAVE_EXTRA_PERF_FROM_SHEET: tuple[tuple[str, str, str], ...] = (
    ("8", "Выраб. — 2.6 Фрезерование", "2.6 Фрезерование"),
)

# Пары (имя как в сводке 1 / write_perf_sheet) → подпись в «Сводка» доноров 3 и 8
# (FIRM_DISPLAY_NAMES). Без этого строки не сопоставляются → n=0 → нули в таблицах.
_FIRM_CANONICAL_AND_DISPLAY: tuple[tuple[str, str], ...] = (
    ("АФ Чистопольская", 'ООО АФ "ЧИСТОПОЛЬСКАЯ"'),
    ("АгроРаздолье", 'ООО "АГРОРАЗДОЛЬЕ"'),
    ("Аксу-Агро", 'ООО "АКСУ АГРО"'),
    ("НурлатАгро", 'ООО "НУРЛАТ АГРО"'),
    ("Дрожжаное Рассвет Агро", 'ООО "ДРОЖЖАНОЕ РАССВЕТ АГРО"'),
    ("Актай", 'ООО "АКТАЙ"'),
    ("Лениногорская Птицефабрика", 'ООО "ЛЕНИНОГОРСКАЯ ПТИЦЕФАБРИКА"'),
)


def _norm_firm_loose(s: str) -> str:
    """Агрессивное сравнение подписей: без кавычек, ООО, всех пробелов; ё→е."""
    t = s.replace("\xa0", " ").strip()
    t = " ".join(t.split())
    t = t.replace("ё", "е").replace("Ё", "Е")
    t = re.sub(r"^ООО\s+", "", t, flags=re.IGNORECASE)
    for ch in ('"', "«", "»", "'"):
        t = t.replace(ch, "")
    t = re.sub(r"\s+", "", t)
    return t.casefold()


def env_enabled() -> bool:
    raw = (os.environ.get("SVODKI_REPORT1_XLSX_SLAVE_FILL") or "").strip().lower()
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return True


def isolated_11_enabled() -> bool:
    raw = (os.environ.get("SVODKI_REPORT1_ISOLATED_11") or "").strip().lower()
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return True


def _master_sheet_title(suffix: str) -> str:
    st = f"Выраб. — {suffix.strip()}"
    return st[:31]


def _norm_for_sheet_compare(name: str) -> str:
    s = name.replace("\xa0", " ").strip()
    s = " ".join(s.split())
    for ch in ("\u2013", "\u2014", "\u2212"):
        s = s.replace(ch, "-")
    return s.casefold()


def _donor_summary_sheet(slave_id: str) -> str:
    return _SLAVE_DONOR_SUMMARY_SHEET.get(slave_id, DONOR_SUMMARY_SHEET)


def _resolve_workbook_sheet(wb, preferred: str) -> str | None:
    """Имя листа в книге (учёт обрезки Excel до 31 символа)."""
    if preferred in wb.sheetnames:
        return preferred
    n_exp = _norm_for_sheet_compare(preferred)
    for sn in wb.sheetnames:
        if _norm_for_sheet_compare(sn) == n_exp:
            return sn
    frag = preferred.strip()[:14].casefold()
    candidates: list[str] = []
    for sn in wb.sheetnames:
        if frag and frag in sn.casefold():
            candidates.append(sn)
    if len(candidates) == 1:
        return candidates[0]
    return None


def _resolve_perf_sheet(wb, suffix: str) -> str | None:
    """Имя листа в книге (Excel / обрезка 31 символ; несколько совпадений — лучший по префиксу)."""
    expected = _master_sheet_title(suffix)
    if expected in wb.sheetnames:
        return expected
    n_exp = _norm_for_sheet_compare(expected)
    for sn in wb.sheetnames:
        if _norm_for_sheet_compare(sn) == n_exp:
            return sn

    frag = suffix.strip()[:14].casefold()
    candidates: list[str] = []
    for sn in wb.sheetnames:
        sl = sn.casefold().replace(".", "")
        if "выраб" not in sl:
            continue
        if frag and frag in sn.casefold():
            candidates.append(sn)

    if not candidates:
        tail = _norm_for_sheet_compare(suffix.strip())[:22]
        for sn in wb.sheetnames:
            ns = _norm_for_sheet_compare(sn)
            if not ns.startswith("выраб"):
                continue
            if tail and tail in ns:
                candidates.append(sn)

    if len(candidates) == 1:
        return candidates[0]

    if len(candidates) > 1:

        def _score(name: str) -> tuple[int, int]:
            ns = _norm_for_sheet_compare(name)
            prefix = 0
            for a, b in zip(ns, n_exp):
                if a != b:
                    break
                prefix += 1
            return (prefix, -abs(len(ns) - len(n_exp)))

        return max(candidates, key=_score)

    return None


def resolve_master_sheet_for_slave(
    wb,
    slave_id: str,
    dst_page: int | None = None,
    src_page: int | None = None,
) -> str | None:
    """Лист сводки 1, куда подставляются данные донора (учёт «Выработка техники» для отчёта 3)."""
    if slave_id == "11":
        if src_page is not None:
            veg_sheet = _REPORT11_SRC_PAGE_TO_SHEET.get(src_page)
            if veg_sheet:
                return _resolve_workbook_sheet(wb, veg_sheet)
        if dst_page is not None:
            veg_sheet = _REPORT11_DST_PAGE_TO_SHEET.get(dst_page)
            if veg_sheet:
                return _resolve_workbook_sheet(wb, veg_sheet)
    if slave_id == "10":
        # Предпочитаем номер страницы донора (1=намолот, 2=овощи) — не зависит от сдвига PDF.
        if src_page is not None:
            harvest_sheet = _REPORT10_SRC_PAGE_TO_SHEET.get(src_page)
            if harvest_sheet:
                return _resolve_workbook_sheet(wb, harvest_sheet)
        harvest_sheet = _REPORT10_DST_PAGE_TO_SHEET.get(dst_page) if dst_page is not None else None
        if harvest_sheet:
            return _resolve_workbook_sheet(wb, harvest_sheet)
    override = (
        _SUFFIX_OVERRIDES.get((dst_page, slave_id)) if dst_page is not None else None
    )
    if override:
        if override == "Выработка техники":
            return _resolve_workbook_sheet(wb, override)
        return _resolve_perf_sheet(wb, override)
    suffixes = _SLAVE_MASTER_SHEET_SUFFIXES.get(slave_id)
    if not suffixes:
        single = _SLAVE_TO_PERF_SUFFIX.get(slave_id)
        suffixes = (single,) if single else ()
    for suffix in suffixes:
        if suffix == "Выработка техники":
            title = _resolve_workbook_sheet(wb, suffix)
        else:
            title = _resolve_perf_sheet(wb, suffix)
        if title:
            return title
    return None


def _firm_index_keys(raw: object) -> list[str]:
    """Ключи для сопоставления строк донора и мастера (варианты пробелов, ООО, кавычки)."""
    base = _firm_key(raw)
    if not base:
        return []

    def add(acc: list[str], s: str) -> None:
        t = s.strip()
        if not t:
            return
        acc.append(t.casefold())
        acc.append(re.sub(r"\s+", "", t).casefold())

    out: list[str] = []
    add(out, base)
    stripped = re.sub(r"^ООО\s+", "", base, flags=re.IGNORECASE).strip()
    stripped = re.sub(r'^["«]+|["»]+$', "", stripped).strip()
    if stripped != base:
        add(out, stripped)

    return list(dict.fromkeys(out))


_FIRM_LOOSE_TO_CANON: dict[str, str] = {}
for _canon, _disp in _FIRM_CANONICAL_AND_DISPLAY:
    for _part in (_canon, _disp):
        _nl = _norm_firm_loose(_part)
        if _nl:
            _FIRM_LOOSE_TO_CANON[_nl] = _canon


def _donor_register_firm_row(donor_by_firm: dict[str, int], firm: str, row: int) -> None:
    """Все варианты подписи фирмы донора → номер строки (как в _donor_row_index_by_firm)."""
    if not firm:
        return
    for k in _firm_index_keys(firm):
        if k not in donor_by_firm:
            donor_by_firm[k] = row
    loose_d = _norm_firm_loose(firm)
    if loose_d and loose_d not in donor_by_firm:
        donor_by_firm[loose_d] = row
    canon = _FIRM_LOOSE_TO_CANON.get(loose_d)
    if canon:
        for k in _firm_index_keys(canon):
            if k not in donor_by_firm:
                donor_by_firm[k] = row


def _donor_row_index_by_firm(d_ws, d_max: int, col_firm: int = 2, row_min: int = 2) -> dict[str, int]:
    donor_by_firm: dict[str, int] = {}
    for r in range(row_min, d_max + 1):
        firm = _firm_key(d_ws.cell(r, col_firm).value)
        _donor_register_firm_row(donor_by_firm, firm, r)
    return donor_by_firm


def _donor_row_for_master_firm(donor_by_firm: dict[str, int], firm_m: str) -> int | None:
    for k in _firm_index_keys(firm_m):
        r_d = donor_by_firm.get(k)
        if r_d is not None:
            return r_d
    lm = _norm_firm_loose(firm_m)
    r_d = donor_by_firm.get(lm)
    if r_d is not None:
        return r_d
    return None


def _normalize_locale_numeric_fragment(s: str) -> str:
    """Пробелы/неразрывные пробелы и запятая как десятичный разделитель → для float()."""
    return (
        s.replace("\u00a0", "")
        .replace("\u202f", "")
        .replace(" ", "")
        .replace(",", ".")
    )


def _donor_effective_value(src_f, src_v) -> object:
    """Литерал из data_only=False; при формуле/пустоте — значение из второго листа (data_only=True)."""
    v = src_f.value
    if isinstance(v, str) and v.startswith("="):
        return src_v.value if src_v is not None else None
    if (v is None or v == "") and src_v is not None:
        cv = src_v.value
        if cv not in (None, ""):
            return cv
    return v


def _donor_numeric_meaningful(val: object) -> bool:
    """True, если в ячейке донора есть ненулевое число (иначе оставляем расчёт сводки 1)."""
    if val in (None, ""):
        return False
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return float(val) != 0.0
    if isinstance(val, Decimal):
        return float(val) != 0.0
    if isinstance(val, str):
        t = _normalize_locale_numeric_fragment(val.replace("\xa0", " ").strip())
        if not t or t.startswith("="):
            return False
        try:
            return float(t) != 0.0
        except ValueError:
            return True
    return True


def _firm_key(cell_val: object) -> str:
    if cell_val is None:
        return ""
    return str(cell_val).replace("\xa0", " ").strip()


_PERF_ALERT_FILL = PatternFill("solid", fgColor="FCE4EC")
_PERF_NEUTRAL_FILL = PatternFill("solid", fgColor="FFFFFF")
_PERF_TOTAL_FILL = PatternFill("solid", fgColor="BDD7EE")


def _decimal_hours_to_hhmm_like_report1(hours: object) -> str:
    """Как decimal_hours_to_hhmm в 1_main_ammonium__1_.py — для колонок 7–8 листа «Выраб.»."""
    if hours is None or hours == "":
        return "0:00"
    if isinstance(hours, str):
        t = hours.strip().replace("\u00a0", " ").replace(" ", "").replace(",", ".")
        if not t:
            return "0:00"
        try:
            h = float(t)
        except ValueError:
            return hours.strip()
    elif isinstance(hours, (int, float)) and not isinstance(hours, bool):
        h = float(hours)
    else:
        return str(hours)
    if h <= 0:
        return "0:00"
    total_min = int(round(h * 60))
    hh = total_min // 60
    mm = total_min % 60
    return f"{hh}:{mm:02d}"


def _normalize_perf_time_columns(m_ws, row: int, cols: tuple[int, ...] = (7, 8)) -> None:
    """Сводки 3–8 хранят часы числом; сводка 1 в PDF — «ЧЧ:ММ»."""
    for c in cols:
        cell = m_ws.cell(row, c)
        raw = cell.value
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            cell.value = _decimal_hours_to_hhmm_like_report1(raw)
            cell.number_format = "General"


def _numeric_cell_value(value: object) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, str):
        text = _normalize_locale_numeric_fragment(value.strip().replace("\xa0", " "))
        if not text or text.startswith("="):
            return 0.0
        try:
            return float(text)
        except ValueError:
            return 0.0
    return 0.0


def _duration_to_hours(value: object) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip().replace("\xa0", " ")
    if not text:
        return 0.0
    parts = text.split(":")
    try:
        if len(parts) >= 2:
            h = float(parts[0] or 0)
            m = float(parts[1] or 0)
            s = float(parts[2] or 0) if len(parts) >= 3 else 0.0
            return h + m / 60.0 + s / 3600.0
        return float(_normalize_locale_numeric_fragment(text))
    except ValueError:
        return 0.0


def _recompute_perf_total_row(m_ws, total_row: int, data_rows: list[int]) -> None:
    """После донорской подстановки пересчитывает «Итого» из строк агрофирм."""
    if not data_rows:
        return
    first_plan = next(
        (_numeric_cell_value(m_ws.cell(r, 2).value) for r in data_rows if _numeric_cell_value(m_ws.cell(r, 2).value) > 0),
        0.0,
    )
    total_mach = sum(_numeric_cell_value(m_ws.cell(r, 3).value) for r in data_rows)
    total_area = sum(_numeric_cell_value(m_ws.cell(r, 4).value) for r in data_rows)
    total_hours = sum(_duration_to_hours(m_ws.cell(r, 7).value) for r in data_rows)
    total_avail = sum(_numeric_cell_value(m_ws.cell(r, 10).value) for r in data_rows)
    total_hired = sum(_numeric_cell_value(m_ws.cell(r, 11).value) for r in data_rows)

    m_ws.cell(total_row, 2).value = first_plan
    m_ws.cell(total_row, 3).value = int(round(total_mach))
    m_ws.cell(total_row, 4).value = round(total_area, 2)
    m_ws.cell(total_row, 5).value = round(total_area / total_mach, 2) if total_mach else 0.0
    if _numeric_cell_value(m_ws.cell(total_row, 6).value) == 0.0:
        season_vals = [_numeric_cell_value(m_ws.cell(r, 6).value) for r in data_rows]
        season_vals = [v for v in season_vals if v > 0]
        m_ws.cell(total_row, 6).value = round(sum(season_vals) / len(season_vals), 1) if season_vals else 0.0
    m_ws.cell(total_row, 7).value = _decimal_hours_to_hhmm_like_report1(total_hours)
    m_ws.cell(total_row, 8).value = _decimal_hours_to_hhmm_like_report1(total_hours / total_mach if total_mach else 0.0)
    m_ws.cell(total_row, 9).value = round(total_area / total_hours, 1) if total_hours else 0.0
    m_ws.cell(total_row, 10).value = int(round(total_avail))
    m_ws.cell(total_row, 11).value = int(round(total_hired))
    for col in (2, 3, 10, 11):
        m_ws.cell(total_row, col).number_format = "# ##0"
    for col in (4, 5, 6, 9):
        m_ws.cell(total_row, col).number_format = "# ##0.00" if col in (4, 5, 6) else "# ##0.0"
    _apply_perf_rate_below_plan_fill(m_ws, total_row)


def _coerce_plan_machines_rate(plan_v, mach_v, rate_v) -> tuple[float, int, float] | None:
    """Числа в листе могут быть int/float или текстом с пробелами/запятой (как в PDF/группировке)."""
    def _f(x: object) -> float | None:
        if x is None or x == "":
            return 0.0
        if isinstance(x, bool):
            return float(int(x))
        if isinstance(x, (int, float)):
            return float(x)
        if isinstance(x, Decimal):
            return float(x)
        if isinstance(x, str):
            t = _normalize_locale_numeric_fragment(x.strip().replace("\u00a0", " "))
            if not t:
                return 0.0
            try:
                return float(t)
            except ValueError:
                return None
        return None

    plan = _f(plan_v)
    nm_raw = _f(mach_v)
    rate = _f(rate_v)
    if plan is None or nm_raw is None or rate is None:
        return None
    return plan, int(round(nm_raw)), rate


def _apply_perf_rate_below_plan_fill(m_ws, row: int) -> None:
    """Красная заливка колонки 5, если выработка/агрегат < плана (как write_perf_sheet сводки 1)."""
    firm = _firm_key(m_ws.cell(row, 1).value)
    is_total = firm == "Итого"
    parsed = _coerce_plan_machines_rate(
        m_ws.cell(row, 2).value,
        m_ws.cell(row, 3).value,
        m_ws.cell(row, 5).value,
    )
    if parsed is None:
        return
    plan, nm, rate = parsed
    c = m_ws.cell(row, 5)
    if nm > 0 and rate < plan:
        c.fill = copy(_PERF_ALERT_FILL)
    elif is_total:
        c.fill = copy(_PERF_TOTAL_FILL)
    else:
        c.fill = copy(_PERF_NEUTRAL_FILL)


def _copy_openpyxl_cell_visual_style(src_cell, dst_cell) -> None:
    """Копирует оформление ячейки (шрифт, заливку, границы, выравнивание, формат числа)."""
    if src_cell is None or dst_cell is None:
        return
    try:
        f = src_cell.font
        if f:
            dst_cell.font = copy(f)
        fl = src_cell.fill
        if fl and getattr(fl, "fill_type", None):
            dst_cell.fill = copy(fl)
        b = src_cell.border
        if b:
            dst_cell.border = copy(b)
        a = src_cell.alignment
        if a:
            dst_cell.alignment = copy(a)
        nf = src_cell.number_format
        if nf and str(nf) != "General":
            dst_cell.number_format = nf
    except Exception:
        pass


def _fill_perf_from_donor_summary(
    m_ws,
    d_ws,
    d_ws_v,
    *,
    preserve_col: int = 11,
    logs: list[str],
    ctx: str,
    slave_id: str = "",
) -> int:
    """Возвращает число обновлённых строк (включая Итого).

    Донор «Сводка»: № в col1, фирма col2, …
    - 12 колонок (типичный отчёт 3): мастер col10←col11 донора, col12 донора→col11 мастера (наёмные).
    - 11 колонок (отчёты 4,6,7,8 и др.): мастер col10←col11 донора («в наличии»), col11 мастера не
      трогаем — наёмные остаются из расчёта сводки 1.

    Важно: эвристика «ширина листа ≥12 колонок» ложно срабатывала на отчётах 4 и 8 (примечание,
    лишняя ячейка справа в первых строках) → подставлялся неверный 12-й столбец и ломались стр. 24 и 27.

    Для отчёта 3 всегда режим 12 колонок (К мастера ← L донора): текущая генерация отчёта 3 всегда пишет
    двенадцатый столбец; эвристика по (1,12)/(2,12) давала ложный режим 11 колонок → нули в «наёмных»
    и несовпадение стиля с PDF при частичном оверлее.

    Открытие донора: data_only=False + при необходимости data_only=True (кэш формул), чтобы не копировать пустые ячейки.
    """
    d_max = int(d_ws.max_row or 0)
    donor_by_firm = _donor_row_index_by_firm(d_ws, d_max, col_firm=2)
    use_twelve = slave_id == "3"

    if use_twelve:
        for hc in (12, 11):
            h_src = d_ws.cell(1, hc)
            h_sv = d_ws_v.cell(1, hc) if d_ws_v is not None else None
            ev = _donor_effective_value(h_src, h_sv)
            if _cell_plain_text(ev):
                h_dst = m_ws.cell(1, 11)
                h_dst.value = ev
                _copy_openpyxl_cell_visual_style(h_src, h_dst)
                break
        lk = get_column_letter(11)
        ll = get_column_letter(12)
        dd = d_ws.column_dimensions.get(ll)
        md = m_ws.column_dimensions.get(lk)
        base_w = float(md.width) if md and md.width else 18.0
        donor_w = float(dd.width) if dd and dd.width else 0.0
        m_ws.column_dimensions[lk].width = max(base_w, donor_w, 22.0)

    saved_m11: dict[int, object] = {}
    m_max = int(m_ws.max_row or 0)
    updated = 0
    total_row: int | None = None
    data_rows: list[int] = []
    for r in range(2, m_max + 1):
        firm_m = _firm_key(m_ws.cell(r, 1).value)
        if not firm_m:
            continue
        is_total = firm_m == "Итого"
        if is_total:
            total_row = r
        else:
            data_rows.append(r)
        r_d = _donor_row_for_master_firm(donor_by_firm, firm_m)
        if r_d is None:
            logs.append(f"{ctx}: нет строки «{firm_m}» у донора, мастер строка {r} пропущена.")
            continue
        if not use_twelve:
            saved_m11[r] = m_ws.cell(r, preserve_col).value
        for mc in range(1, 10):
            dc = mc + 1
            src = d_ws.cell(r_d, dc)
            sv = d_ws_v.cell(r_d, dc) if d_ws_v is not None else None
            dst = m_ws.cell(r, mc)
            dst.value = _donor_effective_value(src, sv)
            if src.number_format and src.number_format != "General":
                dst.number_format = src.number_format
        src10 = d_ws.cell(r_d, 11)
        sv10 = d_ws_v.cell(r_d, 11) if d_ws_v is not None else None
        dst10 = m_ws.cell(r, 10)
        dst10.value = _donor_effective_value(src10, sv10)
        if src10.number_format and src10.number_format != "General":
            dst10.number_format = src10.number_format
        if use_twelve:
            src11 = d_ws.cell(r_d, 12)
            sv11 = d_ws_v.cell(r_d, 12) if d_ws_v is not None else None
            dst11 = m_ws.cell(r, 11)
            prior_k = dst11.value
            eff = _donor_effective_value(src11, sv11)
            if _donor_numeric_meaningful(eff):
                dst11.value = eff
                _copy_openpyxl_cell_visual_style(src11, dst11)
            else:
                dst11.value = prior_k
        else:
            m_ws.cell(r, preserve_col).value = saved_m11.get(r, m_ws.cell(r, preserve_col).value)
        _normalize_perf_time_columns(m_ws, r)
        _apply_perf_rate_below_plan_fill(m_ws, r)
        if _coerce_plan_machines_rate(
            m_ws.cell(r, 2).value,
            m_ws.cell(r, 3).value,
            m_ws.cell(r, 5).value,
        ) is None:
            _copy_openpyxl_cell_visual_style(d_ws.cell(r_d, 6), m_ws.cell(r, 5))
        updated += 1

    if total_row is not None:
        _recompute_perf_total_row(m_ws, total_row, data_rows)

    return updated


def _cell_plain_text(val: object) -> str:
    if val in (None, ""):
        return ""
    return str(val).replace("\n", " ").replace("\xa0", " ").strip()


def _find_report5_cultures_data_start(d_ws) -> int | None:
    """Первая строка данных блока «КУЛЬТУРЫ» на листе «Сводка» отчёта 5 (после шапки)."""
    max_r = min(int(d_ws.max_row or 0), 350)
    for r in range(1, max_r + 1):
        v = d_ws.cell(r, 1).value
        if isinstance(v, str) and v.strip() == "КУЛЬТУРЫ":
            header_row = r + 1
            data_row = r + 2
            h_ag = _cell_plain_text(d_ws.cell(header_row, 2).value)
            if "Агрофирма" not in h_ag:
                return None
            return data_row
    return None


def _report5_cultures_block_end(d_ws, data_start: int) -> int:
    """Последняя строка блока «КУЛЬТУРЫ» (до следующего раздела или пустой строки)."""
    d_max = int(d_ws.max_row or 0)
    last_data = data_start
    for r in range(data_start, d_max + 1):
        title = _cell_plain_text(d_ws.cell(r, 1).value)
        if title in {"Овощи", "Посев / посадка овощных культур"}:
            break
        firm = _firm_key(d_ws.cell(r, 2).value)
        if not firm:
            break
        last_data = r
    return last_data


def _collect_report5_cultures_donor_rows(d_ws, data_start: int) -> dict[str, int]:
    donor_by_firm: dict[str, int] = {}
    end_row = _report5_cultures_block_end(d_ws, data_start)
    for r in range(data_start, end_row + 1):
        firm = _firm_key(d_ws.cell(r, 2).value)
        if firm:
            _donor_register_firm_row(donor_by_firm, firm, r)
    return donor_by_firm


def _master_perf_has_daily_activity(m_ws) -> bool:
    """True, если в таблице «Выраб.» есть ненулевая суточная выработка после XLSX-fill."""
    m_max = int(m_ws.max_row or 0)
    for r in range(2, m_max + 1):
        firm = _firm_key(m_ws.cell(r, 1).value)
        if not firm or firm == "Итого":
            continue
        if _donor_numeric_meaningful(m_ws.cell(r, 3).value):
            return True
        if _donor_numeric_meaningful(m_ws.cell(r, 4).value):
            return True
    return False


def _slave_fill_disables_pdf_overlay(slave_id: str, n: int, m_ws) -> bool:
    """PDF-graft отключаем, если Excel перенёс хотя бы одну строку (в т.ч. отчёт 5 / посев)."""
    del slave_id, m_ws
    return n > 0


def _fill_perf_from_report5_cultures(
    m_ws,
    d_ws,
    d_ws_v,
    *,
    preserve_col: int = 11,
    logs: list[str],
    ctx: str,
) -> int | None:
    """None — блок «КУЛЬТУРЫ» не найден, оставляем PDF-подстановку. Иначе число обновлённых строк."""
    data_start = _find_report5_cultures_data_start(d_ws)
    if data_start is None:
        logs.append(
            f"{ctx}: в «Сводке» отчёта 5 нет блока «КУЛЬТУРЫ» — страница посева останется через PDF."
        )
        return None

    donor_by_firm = _collect_report5_cultures_donor_rows(d_ws, data_start)
    if not donor_by_firm:
        logs.append(f"{ctx}: блок «КУЛЬТУРЫ» пуст — страница посева через PDF.")
        return 0

    saved_m11: dict[int, object] = {}
    m_max = int(m_ws.max_row or 0)
    updated = 0
    total_row: int | None = None
    data_rows: list[int] = []
    for r in range(2, m_max + 1):
        firm_m = _firm_key(m_ws.cell(r, 1).value)
        if not firm_m:
            continue
        is_total = firm_m == "Итого"
        if is_total:
            total_row = r
        else:
            data_rows.append(r)
        r_d = _donor_row_for_master_firm(donor_by_firm, firm_m)
        if r_d is None:
            logs.append(f"{ctx}: нет строки «{firm_m}» у донора (блок КУЛЬТУРЫ), мастер строка {r} пропущена.")
            continue
        saved_m11[r] = m_ws.cell(r, preserve_col).value
        for mc in range(1, 10):
            dc = mc + 1
            src = d_ws.cell(r_d, dc)
            sv = d_ws_v.cell(r_d, dc) if d_ws_v is not None else None
            dst = m_ws.cell(r, mc)
            dst.value = _donor_effective_value(src, sv)
            if src.number_format and src.number_format != "General":
                dst.number_format = src.number_format
        src10 = d_ws.cell(r_d, 11)
        sv10 = d_ws_v.cell(r_d, 11) if d_ws_v is not None else None
        dst10 = m_ws.cell(r, 10)
        dst10.value = _donor_effective_value(src10, sv10)
        if src10.number_format and src10.number_format != "General":
            dst10.number_format = src10.number_format
        m_ws.cell(r, preserve_col).value = saved_m11.get(r, m_ws.cell(r, preserve_col).value)
        _normalize_perf_time_columns(m_ws, r)
        _apply_perf_rate_below_plan_fill(m_ws, r)
        if _coerce_plan_machines_rate(
            m_ws.cell(r, 2).value,
            m_ws.cell(r, 3).value,
            m_ws.cell(r, 5).value,
        ) is None:
            _copy_openpyxl_cell_visual_style(d_ws.cell(r_d, 6), m_ws.cell(r, 5))
        updated += 1

    if total_row is not None:
        _recompute_perf_total_row(m_ws, total_row, data_rows)

    return updated


def _find_report6_veg_spray_data_start(d_ws) -> int | None:
    """Первая строка данных блока «Опрыскивание овощных культур» на листе «Сводка» отчёта 6."""
    max_r = min(int(d_ws.max_row or 0), 350)
    for r in range(1, max_r + 1):
        v = d_ws.cell(r, 1).value
        if isinstance(v, str) and v.strip() == "Опрыскивание овощных культур":
            header_row = r + 1
            data_row = r + 2
            h_ag = _cell_plain_text(d_ws.cell(header_row, 2).value)
            if "Агрофирма" not in h_ag:
                return None
            return data_row
    return None


def _fill_perf_from_report6_veg_spray(
    m_ws,
    d_ws,
    d_ws_v,
    *,
    preserve_col: int = 11,
    logs: list[str],
    ctx: str,
) -> int | None:
    """None — блок не найден. Иначе число обновлённых строк."""
    data_start = _find_report6_veg_spray_data_start(d_ws)
    if data_start is None:
        logs.append(
            f"{ctx}: в «Сводке» отчёта 6 нет блока «Опрыскивание овощных культур»."
        )
        return None

    d_max = int(d_ws.max_row or 0)
    donor_by_firm: dict[str, int] = {}
    for r in range(data_start, d_max + 1):
        firm = _firm_key(d_ws.cell(r, 2).value)
        if not firm:
            break
        _donor_register_firm_row(donor_by_firm, firm, r)

    saved_m11: dict[int, object] = {}
    m_max = int(m_ws.max_row or 0)
    updated = 0
    for r in range(2, m_max + 1):
        firm_m = _firm_key(m_ws.cell(r, 1).value)
        if not firm_m:
            continue
        r_d = _donor_row_for_master_firm(donor_by_firm, firm_m)
        if r_d is None:
            logs.append(
                f"{ctx}: нет строки «{firm_m}» у донора (блок овощного опрыскивания), "
                f"мастер строка {r} пропущена."
            )
            continue
        saved_m11[r] = m_ws.cell(r, preserve_col).value
        for mc in range(1, 10):
            dc = mc + 1
            src = d_ws.cell(r_d, dc)
            sv = d_ws_v.cell(r_d, dc) if d_ws_v is not None else None
            dst = m_ws.cell(r, mc)
            dst.value = _donor_effective_value(src, sv)
            if src.number_format and src.number_format != "General":
                dst.number_format = src.number_format
        src10 = d_ws.cell(r_d, 11)
        sv10 = d_ws_v.cell(r_d, 11) if d_ws_v is not None else None
        dst10 = m_ws.cell(r, 10)
        dst10.value = _donor_effective_value(src10, sv10)
        if src10.number_format and src10.number_format != "General":
            dst10.number_format = src10.number_format
        m_ws.cell(r, preserve_col).value = saved_m11.get(r, m_ws.cell(r, preserve_col).value)
        _normalize_perf_time_columns(m_ws, r)
        _apply_perf_rate_below_plan_fill(m_ws, r)
        if _coerce_plan_machines_rate(
            m_ws.cell(r, 2).value,
            m_ws.cell(r, 3).value,
            m_ws.cell(r, 5).value,
        ) is None:
            _copy_openpyxl_cell_visual_style(d_ws.cell(r_d, 6), m_ws.cell(r, 5))
        updated += 1

    return updated


def _replace_workbook_sheet_from_donor(
    master_wb,
    master_title: str,
    donor_wb,
    donor_title: str,
) -> bool:
    """Заменяет лист мастера содержимым одноимённого листа донора (значения и оформление)."""
    resolved_donor = _resolve_workbook_sheet(donor_wb, donor_title)
    if not resolved_donor:
        return False
    src = donor_wb[resolved_donor]
    resolved_master = _resolve_workbook_sheet(master_wb, master_title)
    if resolved_master and resolved_master in master_wb.sheetnames:
        idx = master_wb.sheetnames.index(resolved_master)
        master_wb.remove(master_wb[resolved_master])
    else:
        idx = len(master_wb.sheetnames)
    dst = master_wb.create_sheet(master_title[:31], idx)
    for merged in list(src.merged_cells.ranges):
        dst.merge_cells(str(merged))
    for row in src.iter_rows():
        for cell in row:
            dc = dst.cell(row=cell.row, column=cell.column, value=cell.value)
            if cell.has_style:
                dc.font = copy(cell.font)
                dc.border = copy(cell.border)
                dc.fill = copy(cell.fill)
                dc.number_format = cell.number_format
                dc.protection = copy(cell.protection)
                dc.alignment = copy(cell.alignment)
    for col, dim in src.column_dimensions.items():
        dst.column_dimensions[col].width = dim.width
        if dim.hidden:
            dst.column_dimensions[col].hidden = True
    for row_idx, dim in src.row_dimensions.items():
        dst.row_dimensions[row_idx].height = dim.height
        if dim.hidden:
            dst.row_dimensions[row_idx].hidden = True
    dst.sheet_format = copy(src.sheet_format)
    dst.page_setup = copy(src.page_setup)
    dst.page_margins = copy(src.page_margins)
    dst.print_options = copy(src.print_options)
    if src.print_area:
        dst.print_area = src.print_area
    if getattr(src, "row_breaks", None) and getattr(src.row_breaks, "brk", None):
        dst.row_breaks = copy(src.row_breaks)
    return True


def preserve_report1_own_calc_sheets(
    master_xlsx: Path,
    pristine_xlsx: Path,
    *,
    logs: list[str],
    report_label: str = "1",
) -> bool:
    """Восстанавливает листы собственного расчёта сводки 1 из снимка до подстановки доноров."""
    if not master_xlsx.is_file() or not pristine_xlsx.is_file():
        return False
    try:
        from openpyxl import load_workbook
    except Exception as exc:
        logs.append(f"XLSX сводка {report_label}: restore own sheets — {exc}")
        return False

    m_wb = load_workbook(master_xlsx, read_only=False, data_only=False, keep_vba=True)
    p_wb = load_workbook(pristine_xlsx, read_only=False, data_only=False, keep_vba=True)
    restored: list[str] = []
    try:
        for title in REPORT1_OWN_CALC_SHEETS:
            if _replace_workbook_sheet_from_donor(m_wb, title, p_wb, title):
                restored.append(title)
        if restored:
            m_wb.save(master_xlsx)
            logs.append(
                f"XLSX сводка {report_label}: восстановлены листы собственного расчёта "
                f"({', '.join(restored)}) — 1.1 МУ не перезаписывается донором 3."
            )
            return True
        return False
    except Exception as exc:
        logs.append(f"XLSX сводка {report_label}: restore own sheets — {exc}")
        return False
    finally:
        m_wb.close()
        p_wb.close()


def _apply_report10_harvest_graft(
    wb,
    snap_dir: Path,
    *,
    logs: list[str],
    report_label: str,
) -> bool:
    """Подстановка листов «Общий намолот» и «Овощи» из сводки 10 (стр. 2–3 PDF)."""
    donor_path = snap_dir / "10.xlsx"
    if not donor_path.is_file():
        return False
    try:
        from openpyxl import load_workbook
    except Exception as exc:
        logs.append(f"XLSX сводка {report_label}: openpyxl — {exc}")
        return False
    try:
        d_wb = load_workbook(donor_path, read_only=False, data_only=False, keep_vba=False)
    except Exception as exc:
        logs.append(f"XLSX сводка {report_label}: не открыть донор 10: {exc}")
        return False
    copied = 0
    try:
        for master_title, donor_title in _REPORT10_HARVEST_SHEETS:
            if not _resolve_workbook_sheet(d_wb, donor_title):
                logs.append(
                    f"XLSX сводка {report_label}: у донора 10 нет листа «{donor_title}»."
                )
                continue
            if _replace_workbook_sheet_from_donor(wb, master_title, d_wb, donor_title):
                copied += 1
                logs.append(
                    f"XLSX сводка {report_label}: «{master_title}» ← отчёт 10 "
                    f"(лист «{donor_title}»)."
                )
    finally:
        d_wb.close()
    return copied >= len(_REPORT10_HARVEST_SHEETS)


def _apply_report6_veg_spray_fill(
    wb,
    snap_dir: Path,
    *,
    logs: list[str],
    report_label: str,
) -> bool:
    """Подстановка «Выраб. — 3.2 Опрыскивание (овощи)» из блока сводки отчёта 6."""
    perf_suffix = "3.2 Опрыскивание (овощи)"
    donor_path = snap_dir / "6.xlsx"
    if not donor_path.is_file():
        return False
    master_title = _resolve_perf_sheet(wb, perf_suffix)
    if not master_title:
        return False
    try:
        from openpyxl import load_workbook
    except Exception as exc:
        logs.append(f"XLSX сводка {report_label}: openpyxl — {exc}")
        return False
    try:
        d_wb = load_workbook(donor_path, read_only=False, data_only=False, keep_vba=False)
    except Exception as exc:
        logs.append(f"XLSX сводка {report_label}: не открыть донор 6 (овощи): {exc}")
        return False
    d_wb_v = None
    try:
        d_wb_v = load_workbook(donor_path, read_only=False, data_only=True, keep_vba=False)
    except Exception:
        d_wb_v = None
    try:
        resolved = _resolve_workbook_sheet(d_wb, DONOR_SUMMARY_SHEET)
        if not resolved:
            return False
        d_ws = d_wb[resolved]
        d_ws_v = (
            d_wb_v[resolved]
            if d_wb_v is not None and resolved in d_wb_v.sheetnames
            else None
        )
        n = _fill_perf_from_report6_veg_spray(
            wb[master_title],
            d_ws,
            d_ws_v,
            logs=logs,
            ctx=f"XLSX сводка {report_label} ←6 овощное опрыскивание",
        )
        if n and n > 0:
            logs.append(
                f"XLSX сводка {report_label}: «{master_title}» ← отчёт 6 "
                f"(блок овощного опрыскивания): обновлено строк: {n}."
            )
            return True
    finally:
        d_wb.close()
        if d_wb_v is not None:
            d_wb_v.close()
    return False


def _apply_slave_extra_perf_fills(
    wb,
    snap_dir: Path,
    *,
    logs: list[str],
    report_label: str,
) -> bool:
    """Подстановка из отдельных листов донора (напр. фрезерование из отчёта 8)."""
    try:
        from openpyxl import load_workbook
    except Exception as exc:
        logs.append(f"XLSX сводка {report_label}: openpyxl — {exc}")
        return False

    any_ok = False
    for slave_id, donor_sheet, perf_suffix in _SLAVE_EXTRA_PERF_FROM_SHEET:
        donor_path = snap_dir / f"{slave_id}.xlsx"
        if not donor_path.is_file():
            continue
        master_title = _resolve_perf_sheet(wb, perf_suffix)
        if not master_title:
            continue
        try:
            d_wb = load_workbook(donor_path, read_only=False, data_only=False, keep_vba=False)
        except Exception as exc:
            logs.append(
                f"XLSX сводка {report_label}: доп. донор {slave_id} «{donor_sheet}»: {exc}"
            )
            continue
        d_wb_v = None
        try:
            d_wb_v = load_workbook(donor_path, read_only=False, data_only=True, keep_vba=False)
        except Exception:
            d_wb_v = None
        try:
            resolved = _resolve_workbook_sheet(d_wb, donor_sheet)
            if not resolved:
                logs.append(
                    f"XLSX сводка {report_label}: у донора {slave_id} нет листа «{donor_sheet}» "
                    f"(доп. подстановка «{perf_suffix}»)."
                )
                continue
            d_ws = d_wb[resolved]
            d_ws_v = (
                d_wb_v[resolved]
                if d_wb_v is not None and resolved in d_wb_v.sheetnames
                else None
            )
            n = _fill_perf_from_donor_summary(
                wb[master_title],
                d_ws,
                d_ws_v,
                logs=logs,
                ctx=f"XLSX сводка {report_label} доп.←{slave_id} «{resolved}»",
                slave_id=slave_id,
            )
            if n > 0:
                any_ok = True
                logs.append(
                    f"XLSX сводка {report_label}: «{master_title}» ← отчёт {slave_id} "
                    f"лист «{resolved}»: обновлено строк: {n}."
                )
        finally:
            d_wb.close()
            if d_wb_v is not None:
                d_wb_v.close()
    return any_ok


def apply_slave_fills_to_report1_workbook(
    master_xlsx: Path,
    snap_dir: Path,
    graft_spec: Iterable[tuple[int, str, int]] | None = None,
    *,
    logs: list[str],
    report_label: str = "1",
) -> tuple[set[str], set[str]]:
    """
    Читает доноров из snap_dir / "{slave_id}.xlsx", правит master_xlsx на месте.

    Возвращает (filled_slaves, skip_pdf_overlay).

    skip_pdf_overlay — только те slave_id, для которых Excel **реально** перенёс данные (n>0).
    Если строки не сопоставились (n=0), PDF-слой **не** отключаем: иначе остаётся «пустая» таблица
    сводки 1 без подстановки страницы донора (типичные нули на стр.15/24/27 и др.).

    Отчёт 5: блок «КУЛЬТУРЫ» листа «Сводка» → лист «Выраб. — 4.0 Посев» (одна таблица выработки).
    """
    if not master_xlsx.is_file():
        logs.append(f"XLSX сводки {report_label}: нет файла {master_xlsx}")
        return set(), set()
    spec = tuple(graft_spec or DEFAULT_GRAFT_SPEC)
    try:
        from openpyxl import load_workbook
    except Exception as exc:
        logs.append(f"XLSX сводка {report_label}: openpyxl — {exc}")
        return set(), set()

    wb = load_workbook(master_xlsx, read_only=False, data_only=False, keep_vba=True)
    try:
        filled_slaves: set[str] = set()
        skip_pdf_overlay: set[str] = set()
        any_ok = False
        for dst_page, slave_id, _src_page in spec:
            if slave_id == "10":
                continue
            if slave_id == "3":
                # 1.1 внесение МУ сводка 1 считает сама (fetch_fertilizer_11_mtfmi).
                # Подстановка из отчёта 3 перезаписывает «Выработка техники» чужими итогами.
                logs.append(
                    f"XLSX сводка {report_label}: отчёт 3 — пропуск Excel-подстановки "
                    f"(1.1 из расчёта сводки 1, стр.{dst_page})."
                )
                continue
            suffix = _SUFFIX_OVERRIDES.get((dst_page, slave_id)) or _SLAVE_TO_PERF_SUFFIX.get(
                slave_id
            )
            if not suffix and slave_id not in _SLAVE_MASTER_SHEET_SUFFIXES:
                continue
            donor_path = snap_dir / f"{slave_id}.xlsx"
            if not donor_path.is_file():
                logs.append(
                    f"XLSX сводка {report_label}: нет снимка донора {slave_id} ({donor_path.name}), "
                    f"пропуск стр.{dst_page}."
                )
                continue
            title = resolve_master_sheet_for_slave(wb, slave_id, dst_page)
            if not title:
                exp = _master_sheet_title(suffix) if suffix else slave_id
                logs.append(
                    f"XLSX сводка {report_label}: нет листа «{exp}» или аналога "
                    f"(стр.{dst_page}←{slave_id}); листы: {', '.join(wb.sheetnames[:12])}"
                    f"{'…' if len(wb.sheetnames) > 12 else ''}. "
                    f"Будет PDF-подстановка."
                )
                continue
            try:
                d_wb = load_workbook(
                    donor_path, read_only=False, data_only=False, keep_vba=False
                )
            except Exception as exc:
                logs.append(f"XLSX сводка {report_label}: не открыть донор {slave_id}: {exc}")
                continue
            d_wb_v = None
            try:
                d_wb_v = load_workbook(
                    donor_path, read_only=False, data_only=True, keep_vba=False
                )
            except Exception:
                d_wb_v = None
            try:
                summary_sheet = _donor_summary_sheet(slave_id)
                resolved_summary = _resolve_workbook_sheet(d_wb, summary_sheet)
                if not resolved_summary:
                    logs.append(
                        f"XLSX сводка {report_label}: у донора {slave_id} нет листа «{summary_sheet}». "
                        f"Будет PDF-подстановка."
                    )
                    continue
                d_ws = d_wb[resolved_summary]
                d_ws_v = (
                    d_wb_v[resolved_summary]
                    if d_wb_v is not None and resolved_summary in d_wb_v.sheetnames
                    else None
                )
                m_ws = wb[title]
                if slave_id == "5":
                    n = _fill_perf_from_report5_cultures(
                        m_ws,
                        d_ws,
                        d_ws_v,
                        logs=logs,
                        ctx=f"XLSX сводка {report_label} стр.{dst_page}←{slave_id}",
                    )
                else:
                    n = _fill_perf_from_donor_summary(
                        m_ws,
                        d_ws,
                        d_ws_v,
                        logs=logs,
                        ctx=f"XLSX сводка {report_label} стр.{dst_page}←{slave_id}",
                        slave_id=slave_id,
                    )
                if n is None:
                    continue
                # PDF-graft отключаем только если в мастере появились суточные данные.
                if _slave_fill_disables_pdf_overlay(slave_id, n, m_ws):
                    skip_pdf_overlay.add(slave_id)
                    any_ok = True
                    filled_slaves.add(slave_id)
                    logs.append(
                        f"XLSX сводка {report_label}: стр.{dst_page} ← отчёт {slave_id} "
                        f"({title}): обновлено строк: {n} (PDF-слой для {slave_id} отключён)."
                    )
                else:
                    if n > 0:
                        any_ok = True
                    logs.append(
                        f"XLSX сводка {report_label}: отчёт {slave_id} ({title}): "
                        + (
                            f"обновлено строк {n}, но суточных данных нет — "
                            f"будет PDF-подстановка стр.{dst_page} из сводки {slave_id}."
                            if n > 0
                            else f"строк не обновлено — "
                            f"будет PDF-подстановка стр.{dst_page} из сводки {slave_id}."
                        )
                    )
            finally:
                d_wb.close()
                if d_wb_v is not None:
                    d_wb_v.close()
        if _apply_slave_extra_perf_fills(wb, snap_dir, logs=logs, report_label=report_label):
            any_ok = True
        if _apply_report10_harvest_graft(wb, snap_dir, logs=logs, report_label=report_label):
            any_ok = True
            filled_slaves.add("10")
            # PDF стр. 2–3 всё равно подставляем из сводки 10: LibreOffice при конвертации
            # всей книги сводки 1 (~36 листов) ломает переносы шапки «Общий намолот»/«Овощи»
            # (колонка «Валовый сбор» рвётся как «Валовы / й сбор»). XLSX-копия — для Excel.
            logs.append(
                f"XLSX сводка {report_label}: «Общий намолот»/«Овощи» в Excel ← отчёт 10; "
                f"PDF стр. 2–3 — подстановка страниц из PDF сводки 10 (вёрстка как в 10)."
            )
        if _apply_report6_veg_spray_fill(wb, snap_dir, logs=logs, report_label=report_label):
            any_ok = True
        if not isolated_11_enabled():
            try:
                from report1_sheet_order import reorder_report1_workbook_sheets

                if reorder_report1_workbook_sheets(wb):
                    logs.append(
                        f"XLSX сводка {report_label}: порядок листов переставлен по эталону."
                    )
                    any_ok = True
            except Exception as exc:
                logs.append(f"XLSX сводка {report_label}: перестановка листов — {exc}")
        if any_ok:
            wb.save(master_xlsx)
        return filled_slaves, skip_pdf_overlay
    finally:
        wb.close()


def regenerate_report1_pdf_from_xlsx(
    master_xlsx: Path,
    *,
    pdf_destination: Path,
    logo_root: Path,
    convert_fn,
    logs: list[str],
    report_label: str = "1",
    summary_date=None,
) -> bool:
    """
    convert_fn: (xlsx_path, output_dir, logo_root, *, summary_date=...) -> pdf_path
    — как backend._convert_xlsx_to_pdf.
    """
    if not master_xlsx.is_file():
        return False
    out_dir = Path(tempfile.mkdtemp(prefix=f"r1_pdf_{report_label}_"))
    try:
        try:
            pdf_path = convert_fn(
                master_xlsx, out_dir, logo_root, summary_date=summary_date
            )
        except TypeError:
            pdf_path = convert_fn(master_xlsx, out_dir, logo_root)
        if not pdf_path.is_file():
            logs.append(f"XLSX сводка {report_label}: PDF не создан.")
            return False
        pdf_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pdf_path, pdf_destination)
        logs.append(f"XLSX сводка {report_label}: PDF перезаписан: {pdf_destination.name}.")
        return True
    except Exception as exc:
        logs.append(f"XLSX сводка {report_label}: ошибка конвертации в PDF: {exc}")
        return False
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def recalc_workbook_via_libreoffice(
    xlsx_path: Path,
    *,
    logs: list[str],
    ctx: str = "",
) -> bool:
    """Пересчёт формул донора через LibreOffice (кэш значений для data_only)."""
    import shutil as _shutil
    import subprocess
    import uuid

    office_cmd = _shutil.which("libreoffice") or _shutil.which("soffice")
    if not office_cmd or not xlsx_path.is_file():
        return False
    out_dir = Path(tempfile.mkdtemp(prefix="r1_donor_recalc_"))
    try:
        proc = subprocess.run(
            [
                office_cmd,
                "--headless",
                "--convert-to",
                "xlsx",
                "--outdir",
                str(out_dir),
                str(xlsx_path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if proc.returncode != 0:
            return False
        recalc = out_dir / f"{xlsx_path.stem}.xlsx"
        if not recalc.is_file():
            return False
        _shutil.copy2(recalc, xlsx_path)
        if logs is not None and ctx:
            logs.append(f"{ctx}: донор {xlsx_path.name} пересчитан через LibreOffice.")
        return True
    except Exception as exc:
        if logs is not None and ctx:
            logs.append(f"{ctx}: пересчёт донора {xlsx_path.name} — {exc}")
        return False
    finally:
        _shutil.rmtree(out_dir, ignore_errors=True)


def apply_slave_fills_with_retry(
    master_xlsx: Path,
    snap_dir: Path,
    graft_spec: Iterable[tuple[int, str, int]] | None = None,
    *,
    logs: list[str],
    report_label: str = "1",
    recalc_fn=None,
) -> tuple[set[str], set[str]]:
    """Подстановка с предварительным пересчётом XLSX-доноров (кэш формул)."""
    spec = tuple(graft_spec or DEFAULT_GRAFT_SPEC)
    donor_ids = {slave_id for _, slave_id, _ in spec}
    if recalc_fn is not None:
        for slave_id in sorted(donor_ids):
            donor_path = snap_dir / f"{slave_id}.xlsx"
            if donor_path.is_file():
                try:
                    recalc_fn(donor_path)
                except Exception as exc:
                    logs.append(
                        f"XLSX сводка {report_label}: пересчёт донора {slave_id} — {exc}"
                    )
    return apply_slave_fills_to_report1_workbook(
        master_xlsx,
        snap_dir,
        graft_spec=spec,
        logs=logs,
        report_label=report_label,
    )
