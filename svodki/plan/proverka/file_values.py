"""Читает фактическое значение «Итого» из уже сохранённого .xlsx отчёта.

Пилотный источник правды «В файле»: то, что реально лежит на листе «Сводка»
сохранённого отчёта в uploads/<дата>/ — в отличие от «Модуль» (наш расчёт,
svodka.total_ha) и «Cropwise» (сверка с внешней системой). Полезно как
дополнительная сверка «расчёт → файл, который видит пользователь» и как
защита от возможных багов в самом экспорте (лист/колонка не совпали,
строку «Итого» не туда дописали и т.п.).

ВАЖНО (см. обсуждение пилота по отчёту 6 «Опрыскивание»): сумма «Итого» по
листу «Сводка» — это сумма «опрыскивание + овощи» из файла, тогда как
svodka.total_ha в некоторых отчётах считает по-другому (например, только
по фирмам основного списка). На части дат числа могут разойтись не из-за
ошибки, а из-за разной методики подсчёта — это НЕ помечается как ошибка,
только выводится рядом как есть, для ручной сверки.

Модуль не бросает исключений наружу: если файла нет, он ещё не был
сохранён (см. report_runner._save_artifact), либо структура листа не
совпала с ожидаемой — просто возвращается {"found": False}, и вызывающий
код (run_proverka_batch) продолжает работу как обычно.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

try:
    from openpyxl import load_workbook
except ImportError:  # pragma: no cover - openpyxl всегда должен быть доступен в этом окружении
    load_workbook = None  # type: ignore[assignment]

from report_platform import upload_dir

_TOTAL_LABEL = "Итого"
_FIRM_HEADERS = ("Агрофирма", "Агрофирмы")
_VALUE_HEADERS = (
    "Общий объем выполненной работы за сутки, га",
    "Обработано за сутки, га",
)
_SHEET_NAME = "Сводка"

# Заголовок блока на листе «Сводка» → операция сверки, если он не совпадает
# с именем группы (отчёт 5: «КУЛЬТУРЫ», отчёт 6: овощи).
_GROUP_BLOCK_TITLES: dict[str, tuple[str, ...]] = {
    "3.1 Опрыскивание": ("3.1 Опрыскивание",),
    "3.2 Опрыскивание (овощи)": ("Опрыскивание овощных культур",),
    "4.0 Посев": ("КУЛЬТУРЫ",),
    "4.1 Посев (овощи)": ("Посев / посадка овощных культур",),
}

_FILENAME_HASH_PREFIX_RE = re.compile(r"^[0-9a-f]{8}_", re.IGNORECASE)
_REPORT_ID_RE = re.compile(r"^(\d+(?:\.\d+)?)")


def _report_id_from_title(title: str) -> str:
    m = _REPORT_ID_RE.match(str(title or "").strip())
    return m.group(1) if m else ""


def _report_id_from_filename(filename: str) -> str:
    """Копия правила именования из backend/app/legacy/report_runner.py.

    Дублируется намеренно: proverka (svodki/plan) и backend — разные
    процессы/деревья импорта, кросс-импорт между ними не предполагается.
    """
    stem = Path(filename).name
    if _FILENAME_HASH_PREFIX_RE.match(stem):
        stem = stem.split("_", 1)[1]
    stem = Path(stem).stem
    return _report_id_from_title(stem)


def find_report_file(report_date: date, report_id: str, suffix: str) -> Path | None:
    """Ищет сохранённый отчёт report_id в uploads/<дата>/ с заданным расширением."""
    folder = upload_dir() / report_date.isoformat()
    if not folder.is_dir():
        return None
    rid = str(report_id)
    want = suffix.lower()
    candidates = [
        p
        for p in folder.iterdir()
        if p.is_file()
        and p.suffix.lower() == want
        and _report_id_from_filename(p.name) == rid
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def find_report_xlsx(report_date: date, report_id: str) -> Path | None:
    """Ищет уже сохранённый .xlsx для report_id в uploads/<дата>/."""
    return find_report_file(report_date, report_id, ".xlsx")


def _header_indexes(values: list[Any]) -> tuple[int, int] | None:
    firm_idx: int | None = None
    value_idx: int | None = None
    for i, cell in enumerate(values):
        text = str(cell or "").strip()
        if text in _FIRM_HEADERS:
            firm_idx = i
        if text in _VALUE_HEADERS:
            value_idx = i
    if firm_idx is None or value_idx is None:
        return None
    return firm_idx, value_idx


def _section_title(values: list[Any]) -> str | None:
    """Строка-заголовок блока: одна непустая ячейка, не «Итого» и не шапка."""
    texts = [str(cell).strip() for cell in values if cell not in (None, "")]
    if len(texts) != 1:
        return None
    text = texts[0]
    if text == _TOTAL_LABEL or text in _FIRM_HEADERS or text in _VALUE_HEADERS:
        return None
    return text


def read_summary_blocks(ws) -> list[dict[str, Any]]:
    """Блоки листа «Сводка»: заголовок секции и «Итого» по суточной площади.

    У сводок 3, 4, 7, 8 блок один и без заголовка. У 5 и 6 на листе два блока
    со своими шапками — их итоги не складываются, чтобы «В файле» сравнивалось
    с конкретной операцией, а не с суммой соседнего блока.
    """
    blocks: list[dict[str, Any]] = []
    pending_title = ""
    firm_idx: int | None = None
    value_idx: int | None = None
    current_title = ""
    total = 0.0
    found = False

    def flush() -> None:
        nonlocal total, found
        if found:
            blocks.append({"title": current_title, "file_ha": round(total, 2)})
        total = 0.0
        found = False

    for row in ws.iter_rows(values_only=True):
        values = list(row)
        header = _header_indexes(values)
        if header:
            flush()
            firm_idx, value_idx = header
            current_title = pending_title
            pending_title = ""
            continue
        title = _section_title(values)
        if title:
            pending_title = title
            continue
        if firm_idx is None or value_idx is None:
            continue
        if max(firm_idx, value_idx) >= len(values):
            continue
        if str(values[firm_idx] or "").strip() != _TOTAL_LABEL:
            continue
        try:
            total += float(values[value_idx] or 0)
            found = True
        except (TypeError, ValueError):
            continue
    flush()
    return blocks


def file_ha_for_operation(
    file_values: dict[str, Any] | None,
    group_name: str,
    report_id: str,
) -> float | None:
    """Итог Excel тематической сводки для одной строки сверки операций."""
    if not file_values or not file_values.get("found"):
        return None
    blocks = list(file_values.get("blocks") or [])
    if not blocks:
        return None
    aliases = {
        name.casefold()
        for name in _GROUP_BLOCK_TITLES.get(group_name, (group_name,))
    }
    for block in blocks:
        title = str(block.get("title") or "").strip()
        if title and title.casefold() in aliases:
            return float(block.get("file_ha") or 0)
    if len(blocks) == 1:
        from proverka.reports_config import REPORTS

        spec = REPORTS.get(str(report_id))
        groups = tuple(spec.work_groups) if spec else ()
        if len(groups) <= 1:
            return float(blocks[0].get("file_ha") or 0)
    return None


def _canonical_report_id(row: dict[str, Any]) -> str:
    rid = str(row.get("canonical_report") or "").strip()
    if rid:
        return rid
    peers = row.get("active_reports") or row.get("peer_reports") or []
    for peer in peers:
        peer_id = str(peer)
        if peer_id not in ("1", "2"):
            return peer_id
    return ""


def _norm_crop_name(name: str) -> str:
    return " ".join(str(name or "").casefold().replace("ё", "е").split())


def file_totals_for_row(
    row: dict[str, Any],
    by_report: dict[str, dict[str, float]],
) -> dict[str, float]:
    group = str(row.get("group_name") or "")
    peers = row.get("active_reports") or row.get("peer_reports") or []
    totals: dict[str, float] = {}
    for peer in peers:
        rid = str(peer)
        ha = (by_report.get(rid) or {}).get(group)
        if ha is not None:
            totals[rid] = ha
    return totals


def file_crop_totals_for_row(
    row: dict[str, Any],
    crop_row: dict[str, Any],
    by_report: dict[str, dict[str, dict[str, float]]],
) -> dict[str, float]:
    """Сводка → га этой культуры из файла. Имя культуры сравнивается без регистра."""
    group = str(row.get("group_name") or "")
    crop = str(crop_row.get("crop") or "")
    wanted = _norm_crop_name(crop)
    peers = list(crop_row.get("active_reports") or [])
    for peer in row.get("active_reports") or row.get("peer_reports") or []:
        if peer not in peers:
            peers.append(peer)
    totals: dict[str, float] = {}
    for peer in peers:
        rid = str(peer)
        crops = (by_report.get(rid) or {}).get(group) or {}
        if crop in crops:
            totals[rid] = crops[crop]
            continue
        for name, ha in crops.items():
            if _norm_crop_name(name) == wanted:
                totals[rid] = ha
                break
    return totals


def _stamp_crop_file_totals(
    row: dict[str, Any],
    by_report: dict[str, dict[str, dict[str, float]]],
) -> None:
    for crop_row in row.get("crops") or []:
        if isinstance(crop_row, dict):
            crop_row["file_totals"] = file_crop_totals_for_row(row, crop_row, by_report)


def apply_file_totals_to_rows(rows: list[Any], report_date: date) -> None:
    """В операцию и в каждую культуру кладёт file_totals: сводка → га из её файла."""
    try:
        by_report, crops_by_report = load_saved_areas(report_date)
    except Exception:
        return
    for row in rows:
        if not isinstance(row, dict):
            continue
        row["file_totals"] = file_totals_for_row(row, by_report)
        _stamp_crop_file_totals(row, crops_by_report)


def apply_file_ha_to_rows(rows: list[Any], report_date: date) -> None:
    """Совместимость: колонка «В файле» больше не используется, цифры лежат в file_totals."""
    apply_file_totals_to_rows(rows, report_date)


def stamp_cross_check_file_ha(all_results: dict[str, Any]) -> None:
    """Кладёт file_totals на строки сверки: по каждой сводке своё число из файла."""
    reports = all_results.get("reports") or {}
    by_report: dict[str, dict[str, float]] = {}
    crops_by_report: dict[str, dict[str, dict[str, float]]] = {}
    for rid, result in reports.items():
        if not isinstance(result, dict):
            continue
        if isinstance(result.get("file_operations"), dict):
            by_report[str(rid)] = result["file_operations"]
        if isinstance(result.get("file_crops"), dict):
            crops_by_report[str(rid)] = result["file_crops"]
    rows = ((all_results.get("cross_check") or {}).get("operations")) or []
    for row in rows:
        if isinstance(row, dict):
            row["file_totals"] = file_totals_for_row(row, by_report)
            _stamp_crop_file_totals(row, crops_by_report)


# Сводки, у которых на первой странице PDF «Итого» устроен одинаково:
# план, число машин, обработано за сутки. Остальные отчёты не угадываем.
_PDF_SUMMARY_REPORTS = frozenset({"3", "4", "5", "6", "7", "8", "9"})
_PDF_AREA_INDEX = 2
_PDF_MERGE_GAP = 8.0
_PDF_MIN_NUMBERS = 5
_SECTION_LINE_RE = re.compile(r"^\d+\.\d+\s+\S.*$")
_KNOWN_SECTION_TITLES = frozenset({
    "культуры",
    "опрыскивание овощных культур",
    "посев / посадка овощных культур",
    "3.1 опрыскивание",
})


def _pdf_section_title(line: str) -> str | None:
    text = " ".join(line.split())
    if not text or "ВИД РАБОТ" in text or "Итого" in text or "%" in text:
        return None
    if text.casefold() in _KNOWN_SECTION_TITLES:
        return text
    if "," in text or len(text) > 40:
        return None
    if _SECTION_LINE_RE.match(text):
        return text
    return None


def _cells_from_words(words: list[tuple[float, float, str]]) -> list[str]:
    """Склеивает «1» и «949.16», если между ними тонкий пробел разряда, а не колонка."""
    cells: list[str] = []
    prev_x1: float | None = None
    for x0, x1, text in words:
        if prev_x1 is not None and cells and x0 - prev_x1 < _PDF_MERGE_GAP:
            cells[-1] = cells[-1] + text
            prev_x1 = x1
            continue
        cells.append(text)
        prev_x1 = x1
    return cells


def _total_area_from_cells(
    cells: list[str],
    area_index: int = _PDF_AREA_INDEX,
) -> float | None:
    if not any(cell == _TOTAL_LABEL for cell in cells):
        return None
    if any("%" in cell for cell in cells):
        return None
    nums: list[float] = []
    for cell in cells:
        if cell == _TOTAL_LABEL or ":" in cell:
            continue
        raw = cell.replace(" ", "").replace(",", ".")
        try:
            nums.append(float(raw))
        except ValueError:
            continue
    if len(nums) <= area_index or len(nums) < _PDF_MIN_NUMBERS:
        return None
    return round(nums[area_index], 2)


def _pdf_page_lines(page) -> list[list[tuple[float, float, str]]]:
    buckets: dict[int, list[tuple[float, float, str]]] = {}
    for word in page.get_text("words"):
        y = round(word[1])
        buckets.setdefault(y, []).append((float(word[0]), float(word[2]), str(word[4])))
    return [sorted(buckets[y], key=lambda item: item[0]) for y in sorted(buckets)]


def read_pdf_summary_blocks(path: Path, area_index: int = _PDF_AREA_INDEX) -> list[dict[str, Any]]:
    """«Итого» за сутки с листа сводки PDF. Детализация по машинам не входит:
    она начинается со второго заголовка «ВИД РАБОТ»."""
    try:
        import pymupdf
    except ImportError:
        return []
    try:
        doc = pymupdf.open(path)
    except Exception:
        return []
    blocks: list[dict[str, Any]] = []
    pending_title = ""
    vid_headers = 0
    try:
        for page in doc:
            for words in _pdf_page_lines(page):
                line = " ".join(text for _x0, _x1, text in words)
                if "ВИД РАБОТ" in line:
                    vid_headers += 1
                    if vid_headers >= 2:
                        return blocks
                    continue
                title = _pdf_section_title(line)
                if title:
                    pending_title = title
                    continue
                area = _total_area_from_cells(_cells_from_words(words), area_index)
                if area is None:
                    continue
                blocks.append({"title": pending_title, "file_ha": area})
                pending_title = ""
    except Exception:
        return blocks
    finally:
        doc.close()
    return blocks


def _dedupe_words(text: str) -> str:
    parts = text.split()
    out: list[str] = []
    for part in parts:
        if not out or out[-1] != part:
            out.append(part)
    return " ".join(out)


# Заголовок листа сводки 2 отличается от имени операции в сверке.
_FILE_GROUP_ALIASES = {
    "5.4 уборка овощей": "5.4 Уборка картофеля, лука",
}


def _match_group(line: str) -> str | None:
    """Имя операции из заголовка листа. Строка с двумя операциями не выбирается."""
    from proverka.operation_registry import OPERATION_PEERS

    text = _dedupe_words(line)
    if not text or "Итого" in text or len(text) > 120:
        return None
    aliased = _FILE_GROUP_ALIASES.get(text.casefold())
    if aliased:
        return aliased
    hits: list[str] = []
    for group in OPERATION_PEERS:
        exact = text == group or text.startswith(group + " ") or text.startswith(group + ",")
        in_header = group in text and text.startswith("ВИД РАБОТ")
        if exact or in_header:
            hits.append(group)
    if len(hits) != 1:
        return None
    return hits[0]


def _page_has_daily_header(text: str) -> bool:
    # В PDF подписи колонок рвутся на строки, поэтому ищем короткие маркеры.
    if "Плановая" not in text:
        return False
    return any(phrase in text for phrase in ("Обработано", "Общий объем", "Убрано"))


def areas_from_blocks(report_id: str, blocks: list[dict[str, Any]]) -> dict[str, float]:
    """Блоки листа «Сводка» → гектары по операциям, которые есть в этой сводке."""
    from proverka.operation_registry import OPERATION_PEERS

    payload = {"found": True, "blocks": blocks}
    out: dict[str, float] = {}
    for group, peers in OPERATION_PEERS.items():
        if str(report_id) not in peers:
            continue
        ha = file_ha_for_operation(payload, group, report_id)
        if ha is not None:
            out[group] = ha
    return out


def _perf_areas_from_pdf(path: Path, area_index: int = _PDF_AREA_INDEX) -> dict[str, float]:
    """Сводка 1: на каждой странице «Выработка» своё «Итого» и свой ВИД РАБОТ."""
    try:
        import pymupdf
    except ImportError:
        return {}
    try:
        doc = pymupdf.open(path)
    except Exception:
        return {}
    out: dict[str, float] = {}
    try:
        for page in doc:
            if not _page_has_daily_header(page.get_text("text")):
                continue
            current = ""
            for words in _pdf_page_lines(page):
                line = " ".join(text for _x0, _x1, text in words)
                group = _match_group(line)
                if group:
                    current = group
                    continue
                if not current or current in out:
                    continue
                area = _total_area_from_cells(_cells_from_words(words), area_index)
                if area is None:
                    continue
                out[current] = area
                current = ""
    except Exception:
        return out
    finally:
        doc.close()
    return out


_RU_MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def _rightmost_day_x(words: list[tuple[float, float, str]]) -> float | None:
    if not any(any(month in text.casefold() for month in _RU_MONTHS) for _x0, _x1, text in words):
        return None
    days = [
        x0
        for x0, _x1, text in words
        if text.isdigit() and 1 <= int(text) <= 31
    ]
    if not days:
        return None
    return max(days)


def _number_nearest_x(
    words: list[tuple[float, float, str]],
    target_x: float,
    *,
    require_total: bool = True,
) -> float | None:
    cells: list[tuple[float, str]] = []
    prev_x1: float | None = None
    for x0, x1, text in words:
        if prev_x1 is not None and cells and x0 - prev_x1 < _PDF_MERGE_GAP:
            cells[-1] = (cells[-1][0], cells[-1][1] + text)
            prev_x1 = x1
            continue
        cells.append((x0, text))
        prev_x1 = x1
    if require_total and not any(text == _TOTAL_LABEL for _x, text in cells):
        return None
    best: float | None = None
    best_dist = 10**9
    for x0, text in cells:
        if text == _TOTAL_LABEL or ":" in text or "%" in text:
            continue
        try:
            value = float(text.replace(" ", "").replace(",", "."))
        except ValueError:
            continue
        dist = abs(x0 - target_x)
        if dist < best_dist:
            best_dist = dist
            best = value
    if best is None or best_dist > 40:
        return None
    return round(best, 2)


def _report2_daily_areas(path: Path) -> dict[str, float]:
    """Сводка 2: «Итого» колонки последнего дня в таблице «Совокупная площадь»."""
    try:
        import pymupdf
    except ImportError:
        return {}
    try:
        doc = pymupdf.open(path)
    except Exception:
        return {}
    out: dict[str, float] = {}
    current = ""
    waiting_header = False
    date_x: float | None = None
    try:
        for page in doc:
            for words in _pdf_page_lines(page):
                line = " ".join(text for _x0, _x1, text in words)
                group = _match_group(line)
                if group:
                    current = group
                    waiting_header = False
                    date_x = None
                if "СОВОКУПНАЯ" in line:
                    waiting_header = True
                    date_x = None
                    continue
                if waiting_header and date_x is None:
                    date_x = _rightmost_day_x(words)
                    continue
                if not current or date_x is None or current in out:
                    continue
                area = _number_nearest_x(words, date_x)
                if area is None:
                    continue
                out[current] = area
                waiting_header = False
                date_x = None
    except Exception:
        return out
    finally:
        doc.close()
    return out


def _crop_label(words: list[tuple[float, float, str]], x_min: float, x_max: float) -> str:
    parts: list[str] = []
    for x0, _x1, text in words:
        if x0 < x_min or x0 >= x_max:
            continue
        token = text.strip(",;")
        if not token or token in {"№", "п/п"}:
            continue
        parts.append(token)
    label = " ".join(parts)
    folded = label.casefold()
    if not label or folded in {"итого", "культура", "агрофирма", "агрофирмы"} or "из них" in folded:
        return ""
    return label


def _add_crop_ha(bucket: dict[str, float], crop: str, ha: float) -> None:
    bucket[crop] = round(bucket.get(crop, 0.0) + ha, 2)


def _report2_crop_areas(path: Path) -> dict[str, dict[str, float]]:
    """Сводка 2: сумма по фирмам колонки последнего дня в таблице культур."""
    try:
        import pymupdf
    except ImportError:
        return {}
    try:
        doc = pymupdf.open(path)
    except Exception:
        return {}
    out: dict[str, dict[str, float]] = {}
    current = ""
    in_crops = False
    date_x: float | None = None
    try:
        for page in doc:
            for words in _pdf_page_lines(page):
                line = " ".join(text for _x0, _x1, text in words)
                group = _match_group(line)
                if group:
                    current = group
                    in_crops = False
                    date_x = None
                if "СОВОКУПНАЯ" in line:
                    in_crops = False
                    date_x = None
                    continue
                if "Культура" in line:
                    found_x = _rightmost_day_x(words)
                    if found_x is not None:
                        date_x = found_x
                        in_crops = True
                        continue
                if not current or not in_crops or date_x is None:
                    continue
                crop = _crop_label(words, 105, 250)
                if not crop:
                    continue
                area = _number_nearest_x(words, date_x, require_total=False)
                if area is None:
                    continue
                _add_crop_ha(out.setdefault(current, {}), crop, area)
    except Exception:
        return out
    finally:
        doc.close()
    return out


_CROP_SUMMARY_MARKERS = ("Сводно", "ПО КУЛЬТУРАМ", "ПО ХОЛДИНГУ", "НАМОЛОТ", "УБОРКА ОВОЩНЫХ")


def _is_crop_summary_page(text: str) -> bool:
    return any(marker in text for marker in _CROP_SUMMARY_MARKERS)


def _daily_fact_x(words: list[tuple[float, float, str]]) -> float | None:
    """Колонка «Факт за сутки, га», не валовый сбор в тоннах."""
    for index, (x0, _x1, text) in enumerate(words):
        if not text.casefold().lstrip().startswith("сут"):
            continue
        nxt = words[index + 1][2] if index + 1 < len(words) else ""
        if nxt.casefold().lstrip().startswith("га"):
            return x0
    return None


def _daily_fact_crop_areas(path: Path) -> dict[str, dict[str, float]]:
    """Сводные страницы «Факт за сутки» (сводки 1 и 5): культура → га, уже по всем фирмам."""
    try:
        import pymupdf
    except ImportError:
        return {}
    try:
        doc = pymupdf.open(path)
    except Exception:
        return {}
    out: dict[str, dict[str, float]] = {}
    try:
        for page in doc:
            if not _is_crop_summary_page(page.get_text("text")):
                continue
            lines = _pdf_page_lines(page)
            group = ""
            fact_x: float | None = None
            for words in lines:
                line = " ".join(text for _x0, _x1, text in words)
                if not group:
                    group = _match_group(line) or ""
                if fact_x is None:
                    fact_x = _daily_fact_x(words)
            if not group or fact_x is None or group in out:
                continue
            crops: dict[str, float] = {}
            for words in lines:
                crop = _crop_label(words, 52, 165)
                if not crop:
                    continue
                area = _number_nearest_x(words, fact_x, require_total=False)
                if area is None:
                    continue
                crops[crop] = area
            if crops:
                out[group] = crops
    except Exception:
        return out
    finally:
        doc.close()
    return out


def operation_areas_from_path(path: Path, report_id: str) -> dict[str, float]:
    """Гектары за сутки по операциям из одного файла сводки."""
    rid = str(report_id)
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        blocks = _read_xlsx_blocks(path)
        return areas_from_blocks(rid, blocks) if blocks else {}
    if suffix != ".pdf":
        return {}
    if rid == "2":
        return _report2_daily_areas(path)
    if rid == "1":
        return _perf_areas_from_pdf(path)
    if rid == "11":
        blocks = read_pdf_summary_blocks(path, area_index=1)
        return areas_from_blocks(rid, blocks) if blocks else {}
    if rid in _PDF_SUMMARY_REPORTS:
        blocks = read_pdf_summary_blocks(path)
        return areas_from_blocks(rid, blocks) if blocks else {}
    return {}


def crop_areas_from_path(path: Path, report_id: str) -> dict[str, dict[str, float]]:
    """Операция → культура → га за сутки из файла. Тематические сводки без культур пустые."""
    rid = str(report_id)
    if path.suffix.lower() != ".pdf":
        return {}
    if rid == "2":
        return _report2_crop_areas(path)
    if rid in {"1", "5"}:
        return _daily_fact_crop_areas(path)
    return {}


def _saved_report_files(report_date: date) -> list[Path]:
    folder = upload_dir() / report_date.isoformat()
    if not folder.is_dir():
        return []
    files = [
        path
        for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() in {".pdf", ".xlsx"}
    ]
    files.sort(key=lambda path: (path.suffix.lower() == ".xlsx", path.name))
    return files


def load_saved_areas(
    report_date: date,
) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, dict[str, float]]]]:
    """(операции, культуры) из сохранённых файлов. Excel перекрывает PDF по операциям."""
    operations: dict[str, dict[str, float]] = {}
    crops: dict[str, dict[str, dict[str, float]]] = {}
    for path in _saved_report_files(report_date):
        rid = _report_id_from_filename(path.name)
        if not rid:
            continue
        try:
            areas = operation_areas_from_path(path, rid)
        except Exception:
            areas = {}
        if areas:
            operations[rid] = areas
        try:
            by_crop = crop_areas_from_path(path, rid)
        except Exception:
            by_crop = {}
        if by_crop:
            crops[rid] = by_crop
    return operations, crops


def load_operation_areas(report_date: date) -> dict[str, dict[str, float]]:
    """report_id → операция → га из сохранённого файла. Excel перекрывает PDF."""
    operations, _crops = load_saved_areas(report_date)
    return operations


def _values_from_blocks(blocks: list[dict[str, Any]], path: Path) -> dict[str, Any]:
    total = round(sum(float(block.get("file_ha") or 0) for block in blocks), 2)
    return {"found": True, "file_ha": total, "blocks": blocks, "path": str(path)}


def _read_xlsx_blocks(path: Path) -> list[dict[str, Any]]:
    if load_workbook is None:
        return []
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception:
        return []
    try:
        if _SHEET_NAME not in wb.sheetnames:
            return []
        return read_summary_blocks(wb[_SHEET_NAME])
    except Exception:
        return []
    finally:
        wb.close()


def read_file_total_ha(report_date: date, report_id: str) -> dict[str, Any]:
    """{"found": False} либо {"found": True, "file_ha": .., "blocks": [...], "path": ".."}."""
    xlsx_path = find_report_xlsx(report_date, report_id)
    if xlsx_path is not None:
        blocks = _read_xlsx_blocks(xlsx_path)
        if blocks:
            return _values_from_blocks(blocks, xlsx_path)
    if str(report_id) not in _PDF_SUMMARY_REPORTS:
        return {"found": False}
    pdf_path = find_report_file(report_date, report_id, ".pdf")
    if pdf_path is None:
        return {"found": False}
    blocks = read_pdf_summary_blocks(pdf_path)
    if not blocks:
        return {"found": False}
    return _values_from_blocks(blocks, pdf_path)


def attach_file_values(all_results: dict[str, Any], report_date: date) -> None:
    """Дополняет all_results["reports"][rid]["file_values"] для каждого отчёта.

    Тихо: любая ошибка на отдельном report_id не должна прерывать batch —
    в худшем случае для этого отчёта просто не будет колонки «В файле».
    """
    reports = all_results.get("reports") or {}
    try:
        by_report, crops_by_report = load_saved_areas(report_date)
    except Exception:
        by_report, crops_by_report = {}, {}
    for rid, result in reports.items():
        if not isinstance(result, dict):
            continue
        try:
            result["file_values"] = read_file_total_ha(report_date, rid)
        except Exception:
            result["file_values"] = {"found": False}
        result["file_operations"] = by_report.get(str(rid), {})
        result["file_crops"] = crops_by_report.get(str(rid), {})
    try:
        stamp_cross_check_file_ha(all_results)
    except Exception:
        return
