"""
Порядок страниц PDF сводки 1 после сборки (без перестановки листов Excel).

Сводка 1 формируется «как раньше»; после PDF + graft страницы переставляются
в порядок эталона «1.СВОДКА ХК Крупно», затем накладывается шапка PDF.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import sys
from pathlib import Path

SPRAY_GROUP_NAME = "3.1 Опрыскивание"
VEG_SPRAY_GROUP_NAME = "3.2 Опрыскивание (овощи)"
PLOW_GROUP_NAME = "2.4 Вспашка"
DEEP_GROUP_NAME = "2.8 Глубокорыхление"

# Хвост perf-листов после «Выработка техники» и листов по АФ (эталон ПРИМЕР.pdf).
REPORT1_PERF_TAIL_ORDER: tuple[str, ...] = (
    "2.1 Боронование",
    "2.9 Дискование",
    "2.6 Фрезерование",
    "2.7 Гребнеобразование",
)

FERT_TABLE_SLIDE_TITLES: tuple[str, ...] = (
    "Удобрения — Рапс",
    "Удобрения — Пшеница",
    "Удобрения — Подсолнечник и соя",
    "Удобрения — Кукуруза",
    "Удобрения — Заготовка корма",
    "Удобрения — Пар",
    "Удобрения — Овощи",
)

AGROFIRM_NAMES: tuple[str, ...] = (
    "АФ Чистопольская",
    "АгроРаздолье",
    "Аксу-Агро",
    "НурлатАгро",
    "Дрожжаное Рассвет Агро",
    "Актай",
    "Лениногорская Птицефабрика",
)


def env_enabled() -> bool:
    raw = os.environ.get("SVODKI_REPORT1_PDF_PAGE_REORDER", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _resolve_perf_sheet_name(sheetnames: list[str], group_fragment: str) -> str | None:
    expected = f"Выраб. — {group_fragment}"[:31]
    if expected in sheetnames:
        return expected
    frag = group_fragment.strip()[:14].casefold()
    for sn in sheetnames:
        if "выраб" not in sn.casefold():
            continue
        if frag and frag in sn.casefold():
            return sn
    return None


def build_desired_sheet_order(sheetnames: list[str]) -> list[str]:
    """Целевой порядок листов (и страниц PDF) — как в эталоне «1.СВОДКА ХК Крупно ПРИМЕР.pdf»."""
    existing = list(sheetnames)
    order: list[str] = []

    def _add(name: str | None) -> None:
        if name and name in existing and name not in order:
            order.append(name)

    def _add_perf(suffix: str) -> None:
        _add(_resolve_perf_sheet_name(existing, suffix))

    _add_perf("5.0 Уборка")
    # Без «Выраб. — 5.4»: уборка овощей — лист «Сводка овощи» (сводка 11).
    _add("Сводка овощи")
    _add("Общий намолот")
    _add("Овощи")
    _add_perf("4.0 Посев")
    _add("Итого (все АФ)")
    for sn in sorted(n for n in existing if n.startswith("ЯрСев блок")):
        _add(sn)
    _add("Овощи — посев")
    _add_perf("2.11 Культивация")
    _add_perf("2.12 Прикатывание")
    _add("Вспашка + Глубокорыхление")
    _add_perf(SPRAY_GROUP_NAME)
    _add_perf(VEG_SPRAY_GROUP_NAME)
    _add("Сводка по культурам")
    _add("Свод по агрофирмам")
    for title in FERT_TABLE_SLIDE_TITLES:
        _add(title[:31])
    _add("Выработка техники")
    for firm in AGROFIRM_NAMES:
        _add(firm[:28])
    for suffix in REPORT1_PERF_TAIL_ORDER:
        _add_perf(suffix)
    for sn in existing:
        if sn not in order:
            order.append(sn)
    return order


def _print_area_last_row(ws) -> int:
    found = False
    max_row = 1
    pa = getattr(ws, "print_area", None)
    if pa:
        for m in re.findall(r"\$?[A-Z]+\$?(\d+)", str(pa)):
            try:
                max_row = max(max_row, int(m))
                found = True
            except ValueError:
                pass
    for row_s in getattr(getattr(ws, "row_breaks", None), "brk", []) or []:
        try:
            max_row = max(max_row, int(row_s))
            found = True
        except (TypeError, ValueError):
            pass
    return max_row if found else int(ws.max_row or 1)


def _estimated_sheet_page_count(ws) -> int:
    breaks = getattr(getattr(ws, "row_breaks", None), "brk", []) or []
    if breaks:
        return max(1, len(breaks) + 1)
    last_row = _print_area_last_row(ws)
    first_row = 1
    total_height = 0.0
    for row in range(first_row, max(first_row, last_row) + 1):
        dim = ws.row_dimensions[row]
        if getattr(dim, "hidden", False):
            continue
        total_height += float(dim.height or ws.sheet_format.defaultRowHeight or 15.0)
    is_landscape = str(getattr(ws.page_setup, "orientation", "") or "").lower() == "landscape"
    page_height_pt = 595.0 if is_landscape else 842.0
    margins = ws.page_margins
    usable_height = page_height_pt - float(margins.top or 0) * 72.0 - float(margins.bottom or 0) * 72.0
    usable_height -= 92.0
    usable_height = max(260.0, usable_height)
    return max(1, math.ceil(total_height / usable_height))


def _fit_sheet_page_counts_to_pdf(counts: list[int], n_pages: int) -> list[int]:
    counts = [max(1, int(c or 1)) for c in counts]
    if not counts or n_pages <= 0:
        return counts
    while sum(counts) > n_pages and any(c > 1 for c in counts):
        idx = max(range(len(counts)), key=lambda i: counts[i])
        counts[idx] -= 1
    while sum(counts) < n_pages:
        idx = max(range(len(counts)), key=lambda i: counts[i])
        counts[idx] += 1
    return counts


def _page_ranges_by_sheet(sheetnames: list[str], counts: list[int]) -> dict[str, tuple[int, int]]:
    page = 1
    out: dict[str, tuple[int, int]] = {}
    for sn, cnt in zip(sheetnames, counts):
        cnt = max(1, int(cnt))
        out[sn] = (page, page + cnt - 1)
        page += cnt
    return out


# Маркеры «ВИД РАБОТ» в PDF сводки 1 для точного graft (если эвристика листов ошиблась).
_GRAFT_SLAVE_WORK_MARKERS: dict[str, str] = {
    # Отчёт 3 не graft'им — 1.1 МУ считает сама сводка 1.
    "4": "2.1 Боронование",
    "5": "4.0 Посев",
    "6": "3.1 Опрыскивание",
    "7": "2.9 Дискование",
    "8": "2.11 Культивация",
}

# Текстовые маркеры листов собственного расчёта сводки 1 (устойчивее эвристики страниц).
_OWN_CALC_SHEET_PDF_MARKERS: dict[str, tuple[str, ...]] = {
    "Выработка техники": (
        "1.1",
        "внесение минеральных",
        "выработка техники по агрофирмам",
    ),
    "Сводка по культурам": (
        "сводка по культурам",
        "факт общий",
    ),
    "Свод по агрофирмам": ("свод по агрофирмам",),
    "Свод по удобрениям (ХК)": ("свод по удобрениям",),
}


def _pdf_page_text(page) -> str:
    return (page.extract_text() or "").casefold()


def _find_pdf_pages_by_markers(
    pdf_path: Path,
    required: tuple[str, ...],
) -> list[int]:
    """1-based номера страниц, где в тексте есть все подстроки из required."""
    if not required or not pdf_path.is_file():
        return []
    try:
        from pypdf import PdfReader
    except Exception:
        return []
    try:
        reader = PdfReader(str(pdf_path))
    except Exception:
        return []
    needles = tuple(s.casefold() for s in required if s)
    if not needles:
        return []
    out: list[int] = []
    for idx, page in enumerate(reader.pages, start=1):
        text = _pdf_page_text(page)
        if all(n in text for n in needles):
            out.append(idx)
    return out


def _build_page_remap_by_markers(
    initial_pdf: Path,
    final_pdf: Path,
    sheet_titles: tuple[str, ...],
) -> tuple[dict[int, int], list[str]]:
    """Строит {dst_page: src_page} по текстовым маркерам листов."""
    page_remap: dict[int, int] = {}
    restored: list[str] = []
    for title in sheet_titles:
        markers = _OWN_CALC_SHEET_PDF_MARKERS.get(title)
        if not markers:
            continue
        init_pages = _find_pdf_pages_by_markers(initial_pdf, markers)
        fin_pages = _find_pdf_pages_by_markers(final_pdf, markers)
        if not init_pages or not fin_pages:
            continue
        n_copy = min(len(init_pages), len(fin_pages))
        for off in range(n_copy):
            page_remap[fin_pages[off]] = init_pages[off]
        restored.append(title)
    return page_remap, restored


def _find_pdf_page_for_work_marker(
    pdf_path: Path,
    marker: str,
    *,
    slave_id: str = "",
) -> int | None:
    """Ищет 1-based номер страницы PDF по «ВИД РАБОТ: …» на листе «Выраб. — …».

    Стр. «Итого (все АФ)» / «ЯрСев» тоже имеют подпись «4.0 Посев», но без
    «ВЫРАБОТКА ТЕХНИКИ» — их не трогаем.
    """
    try:
        from pypdf import PdfReader
    except Exception:
        return None
    needle = marker.casefold()
    require_perf = slave_id in _GRAFT_SLAVE_WORK_MARKERS
    try:
        reader = PdfReader(str(pdf_path))
    except Exception:
        return None
    found: int | None = None
    for idx, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").casefold()
        if require_perf and "выработка техники" not in text:
            continue
        if "вид работ" in text and needle in text:
            found = idx
    return found


def resolve_donor_sheet_first_pdf_page(
    donor_xlsx: Path,
    donor_pdf: Path,
    sheet_name: str,
) -> int | None:
    """1-based номер первой страницы листа в PDF донора (учёт «Иные культуры» и др.)."""
    if not donor_xlsx.is_file() or not donor_pdf.is_file():
        return None
    try:
        from openpyxl import load_workbook
        from pypdf import PdfReader
    except Exception:
        return None
    try:
        n_pages = len(PdfReader(str(donor_pdf)).pages)
    except Exception:
        return None
    try:
        wb = load_workbook(donor_xlsx, read_only=False, data_only=True)
        try:
            sheetnames = list(wb.sheetnames)
            target = None
            n_exp = sheet_name.strip().casefold()
            for sn in sheetnames:
                if sn == sheet_name or sn.strip().casefold() == n_exp:
                    target = sn
                    break
            if not target:
                frag = sheet_name.strip()[:14].casefold()
                matches = [sn for sn in sheetnames if frag and frag in sn.casefold()]
                if len(matches) == 1:
                    target = matches[0]
            if not target:
                return None
            counts = [_estimated_sheet_page_count(wb[sn]) for sn in sheetnames]
            counts = _fit_sheet_page_counts_to_pdf(counts, n_pages)
            ranges = _page_ranges_by_sheet(sheetnames, counts)
            start, _end = ranges.get(target, (None, None))
            return start
        finally:
            wb.close()
    except Exception:
        return None


def resolve_graft_spec_pages(
    xlsx_path: Path,
    pdf_path: Path,
    graft_spec: tuple[tuple[int, str, int], ...],
    sheet_for_slave,
    *,
    logs: list[str] | None = None,
) -> tuple[tuple[int, str, int], ...]:
    """
    Пересчитывает номера страниц PDF-подстановки по фактическому порядку листов мастера.

    sheet_for_slave(wb, slave_id) -> имя листа или None.
    """
    if not graft_spec or not xlsx_path.is_file() or not pdf_path.is_file():
        return graft_spec
    try:
        from openpyxl import load_workbook
        from pypdf import PdfReader
    except Exception as exc:
        if logs is not None:
            logs.append(f"PDF подстановка: не пересчитать страницы — {exc}")
        return graft_spec

    try:
        n_pages = len(PdfReader(str(pdf_path)).pages)
    except Exception as exc:
        if logs is not None:
            logs.append(f"PDF подстановка: не прочитать PDF для пересчёта страниц — {exc}")
        return graft_spec

    try:
        wb = load_workbook(xlsx_path, read_only=False, data_only=True)
        try:
            sheetnames = list(wb.sheetnames)
            counts = [_estimated_sheet_page_count(wb[sn]) for sn in sheetnames]
            counts = _fit_sheet_page_counts_to_pdf(counts, n_pages)
            ranges = _page_ranges_by_sheet(sheetnames, counts)
            resolved: list[tuple[int, str, int]] = []
            for fallback_dst, slave_id, src_p in graft_spec:
                try:
                    target = sheet_for_slave(wb, slave_id, fallback_dst, src_p)
                except TypeError:
                    try:
                        target = sheet_for_slave(wb, slave_id, fallback_dst)
                    except TypeError:
                        target = sheet_for_slave(wb, slave_id)
                if target and target in ranges:
                    dst_p = ranges[target][0]
                else:
                    dst_p = fallback_dst
                marker = _GRAFT_SLAVE_WORK_MARKERS.get(slave_id)
                if marker:
                    found = _find_pdf_page_for_work_marker(
                        pdf_path, marker, slave_id=slave_id,
                    )
                    if found is not None:
                        if logs is not None and found != dst_p:
                            logs.append(
                                f"PDF подстановка: отчёт {slave_id} → стр.{found} "
                                f"(по «ВИД РАБОТ: {marker}», было {dst_p})."
                            )
                        dst_p = found
                elif logs is not None and dst_p != fallback_dst:
                    logs.append(
                        f"PDF подстановка: отчёт {slave_id} → стр.{dst_p} "
                        f"(лист «{target or '?'}», было {fallback_dst})."
                    )
                resolved.append((dst_p, slave_id, src_p))
            return tuple(resolved)
        finally:
            wb.close()
    except Exception as exc:
        if logs is not None:
            logs.append(f"PDF подстановка: пересчёт страниц отменён — {exc}")
        return graft_spec


def build_page_permutation(
    sheetnames: list[str],
    page_counts: list[int],
    *,
    desired_order: list[str] | None = None,
) -> list[int]:
    """1-based номера страниц в новом порядке."""
    ranges = _page_ranges_by_sheet(sheetnames, page_counts)
    order = desired_order or build_desired_sheet_order(sheetnames)
    new_pages: list[int] = []
    used: set[str] = set()
    for sn in order:
        if sn not in ranges:
            continue
        start, end = ranges[sn]
        new_pages.extend(range(start, end + 1))
        used.add(sn)
    for sn in sheetnames:
        if sn in used:
            continue
        start, end = ranges[sn]
        new_pages.extend(range(start, end + 1))
    return new_pages


def build_report1_pdf_headers(
    xlsx_path: Path,
    n_pages: int,
    sheet_header_fn,
    default_header: tuple[str, str],
) -> list[tuple[str, str]] | None:
    """
    Шапки PDF сводки 1 в эталонном порядке листов (как reorder_report1_pdf).

    Использует оценку числа страниц по листам Excel, а не поштучную конвертацию LO:
    после graft/ensure посева иначе ломается соответствие «страница → шапка».
    """
    if n_pages < 1 or not xlsx_path.is_file():
        return None
    try:
        from openpyxl import load_workbook
    except Exception:
        return None
    try:
        wb = load_workbook(xlsx_path, read_only=False, data_only=True)
        try:
            sheetnames = list(wb.sheetnames)
            if not sheetnames:
                return None
            counts = [_estimated_sheet_page_count(wb[sn]) for sn in sheetnames]
        finally:
            wb.close()
        counts = _fit_sheet_page_counts_to_pdf(counts, n_pages)
        ranges = _page_ranges_by_sheet(sheetnames, counts)
        desired = build_desired_sheet_order(sheetnames)
        headers: list[tuple[str, str]] = []
        used: set[str] = set()
        for sn in desired:
            if sn not in ranges:
                continue
            start, end = ranges[sn]
            hdr = sheet_header_fn(sn)
            headers.extend([hdr] * max(0, end - start + 1))
            used.add(sn)
        for sn in sheetnames:
            if sn in used:
                continue
            start, end = ranges[sn]
            hdr = sheet_header_fn(sn)
            headers.extend([hdr] * max(0, end - start + 1))
        if len(headers) < n_pages:
            pad = headers[-1] if headers else default_header
            headers.extend([pad] * (n_pages - len(headers)))
        return headers[:n_pages]
    except Exception:
        return None


def prepare_header_workbook(src_xlsx: Path, dest_xlsx: Path) -> Path:
    """Копия XLSX с листами в целевом порядке — только для штампа шапки PDF."""
    from openpyxl import load_workbook

    shutil.copy2(src_xlsx, dest_xlsx)
    wb = load_workbook(dest_xlsx, read_only=False, data_only=False, keep_vba=True)
    try:
        desired = build_desired_sheet_order(list(wb.sheetnames))
        for target_idx, title in enumerate(desired):
            if title not in wb.sheetnames:
                continue
            current_idx = wb.sheetnames.index(title)
            if current_idx != target_idx:
                wb.move_sheet(title, offset=target_idx - current_idx)
        wb.save(dest_xlsx)
    finally:
        wb.close()
    return dest_xlsx


def reorder_report1_pdf(
    pdf_path: Path,
    xlsx_path: Path,
    logs: list[str],
    *,
    report_label: str = "1",
) -> bool:
    """Переставляет страницы PDF по целевому порядку листов."""
    if not env_enabled():
        logs.append(f"Сводка {report_label}: перестановка страниц PDF отключена.")
        return False
    if not pdf_path.is_file() or not xlsx_path.is_file():
        logs.append(f"Сводка {report_label}: нет PDF или XLSX для перестановки страниц.")
        return False
    try:
        from openpyxl import load_workbook
        from pypdf import PdfReader, PdfWriter
    except Exception as exc:
        logs.append(f"Сводка {report_label}: перестановка страниц — {exc}")
        return False

    try:
        reader = PdfReader(str(pdf_path))
        n_pages = len(reader.pages)
        if n_pages < 2:
            logs.append(f"Сводка {report_label}: PDF слишком короткий для перестановки ({n_pages} стр.).")
            return False

        wb = load_workbook(xlsx_path, read_only=False, data_only=True)
        try:
            sheetnames = list(wb.sheetnames)
            counts = [_estimated_sheet_page_count(wb[sn]) for sn in sheetnames]
            counts = _fit_sheet_page_counts_to_pdf(counts, n_pages)
        finally:
            wb.close()

        perm = build_page_permutation(sheetnames, counts)
        if len(perm) != n_pages:
            logs.append(
                f"Сводка {report_label}: перестановка отменена — "
                f"ожидалось {n_pages} стр., получено {len(perm)}."
            )
            return False
        if perm == list(range(1, n_pages + 1)):
            logs.append(f"Сводка {report_label}: порядок страниц PDF уже соответствует эталону.")
            return True

        tmp = pdf_path.with_suffix(".reorder_tmp.pdf")
        writer = PdfWriter()
        for p in perm:
            writer.add_page(reader.pages[p - 1])
        with tmp.open("wb") as fh:
            writer.write(fh)
        tmp.replace(pdf_path)
        logs.append(
            f"Сводка {report_label}: страницы PDF переставлены в порядок эталона "
            f"({n_pages} стр.; первые листы: {', '.join(build_desired_sheet_order(sheetnames)[:4])}…)."
        )
        return True
    except Exception as exc:
        logs.append(f"Сводка {report_label}: ошибка перестановки страниц PDF — {exc}")
        return False


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _estimate_pdf_pages_from_xlsx(xlsx_path: Path) -> int:
    """Сумма оценок страниц по листам Excel (без подгонки под фактический PDF)."""
    try:
        from openpyxl import load_workbook
    except Exception:
        return 0
    try:
        wb = load_workbook(xlsx_path, read_only=False, data_only=True)
        try:
            return sum(_estimated_sheet_page_count(wb[sn]) for sn in wb.sheetnames)
        finally:
            wb.close()
    except Exception:
        return 0


def _xlsx_has_sheet(xlsx_path: Path, preferred: str) -> bool:
    try:
        from openpyxl import load_workbook
    except Exception:
        return False
    try:
        wb = load_workbook(xlsx_path, read_only=False, data_only=True)
        try:
            return _resolve_sheet_title(wb, preferred) is not None
        finally:
            wb.close()
    except Exception:
        return False


def _pdf_has_seeding_perf_page(pdf_path: Path) -> bool:
    """Страница «Выраб. — 4.0 Посев» (таблица ВЫРАБОТКА ТЕХНИКИ по АФ)."""
    try:
        from pypdf import PdfReader
    except Exception:
        return False
    try:
        reader = PdfReader(str(pdf_path))
    except Exception:
        return False
    for page in reader.pages:
        text = (page.extract_text() or "").casefold()
        if "выработка техники" not in text:
            continue
        if "вид работ" not in text or "4.0" not in text or "посев" not in text:
            continue
        # Листы «Посев — {АФ}» / ЯрСев — без шапки выработки по АФ.
        if "посев —" in text and "сводно" not in text:
            continue
        return True
    return False


def _pdf_has_seeding_summary_page(pdf_path: Path) -> bool:
    """Страница «Итого (все АФ)» / «Посев — Сводно по всем агрофирмам»."""
    try:
        from pypdf import PdfReader
    except Exception:
        return False
    try:
        reader = PdfReader(str(pdf_path))
    except Exception:
        return False
    for page in reader.pages:
        text = (page.extract_text() or "").casefold()
        if "посев — сводно" in text or "сводно по всем агрофирм" in text:
            return True
    return False


def _seeding_pages_missing(pdf_path: Path, xlsx_path: Path) -> tuple[bool, bool]:
    """(need_perf, need_summary) — нужно ли восстанавливать страницы посева."""
    perf_ok = _pdf_has_seeding_perf_page(pdf_path)
    summary_ok = _pdf_has_seeding_summary_page(pdf_path)
    has_perf_sheet = _xlsx_has_sheet(xlsx_path, "Выраб. — 4.0 Посев")
    has_summary_sheet = _xlsx_has_sheet(xlsx_path, "Итого (все АФ)")

    need_perf = has_perf_sheet and not perf_ok
    need_summary = has_summary_sheet and not summary_ok

    try:
        from pypdf import PdfReader
        n_pdf = len(PdfReader(str(pdf_path)).pages)
    except Exception:
        n_pdf = 0
    n_expected = _estimate_pdf_pages_from_xlsx(xlsx_path)
    shortfall = (n_expected - n_pdf) if n_expected > 0 and n_pdf > 0 else 0

    # LibreOffice при конвертации всей книги часто теряет 1–2 листа посева.
    if shortfall >= 1:
        if has_perf_sheet and not perf_ok:
            need_perf = True
        if has_summary_sheet and not summary_ok:
            need_summary = True
    if shortfall >= 2:
        if has_perf_sheet and not perf_ok:
            need_perf = True
        if has_summary_sheet and not summary_ok:
            need_summary = True

    return need_perf, need_summary


def _find_pdf_page_after_veg_harvest(pdf_path: Path) -> int:
    """1-based: после страницы «Овощи» (уборка овощей), перед блоком посева."""
    try:
        from pypdf import PdfReader
    except Exception:
        return 4
    try:
        reader = PdfReader(str(pdf_path))
    except Exception:
        return 4
    for idx, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").casefold()
        if "общая уборка овощных" in text or (
            "овощи" in text and "вид работ" in text and "5.4" in text
        ):
            return idx
    for idx, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").casefold()
        if "общий намолот" in text:
            continue
        if "овощ" in text and "культур" in text:
            return idx
    return min(4, max(1, len(reader.pages)))


def _resolve_sheet_title(wb, preferred: str) -> str | None:
    if preferred in wb.sheetnames:
        return preferred
    pref = preferred.casefold().strip()
    for sn in wb.sheetnames:
        if sn.casefold().strip() == pref:
            return sn
    if preferred.startswith("Выраб"):
        matches = [
            sn
            for sn in wb.sheetnames
            if "выраб" in sn.casefold() and "4.0" in sn and "посев" in sn.casefold()
        ]
        if len(matches) == 1:
            return matches[0]
    if preferred.startswith("Итого"):
        matches = [sn for sn in wb.sheetnames if "итого" in sn.casefold() and "аф" in sn.casefold()]
        if len(matches) == 1:
            return matches[0]
    return None


def _sheet_only_xlsx(src_xlsx: Path, sheet_title: str, dest_xlsx: Path) -> bool:
    """Копия книги с одним листом — надёжнее, чем конвертировать всю сводку 1."""
    try:
        from openpyxl import load_workbook
    except Exception:
        return False
    wb = load_workbook(src_xlsx, read_only=False, data_only=False, keep_vba=True)
    try:
        resolved = _resolve_sheet_title(wb, sheet_title)
        if not resolved:
            return False
        for sn in list(wb.sheetnames):
            if sn != resolved:
                wb.remove(wb[sn])
        dest_xlsx.parent.mkdir(parents=True, exist_ok=True)
        wb.save(dest_xlsx)
        return bool(wb.sheetnames)
    finally:
        wb.close()


def ensure_report1_seeding_pages_in_pdf(
    pdf_path: Path,
    xlsx_path: Path,
    *,
    logo_root: Path,
    convert_fn,
    logs: list[str],
    report_label: str = "1",
    summary_date=None,
) -> bool:
    """
    Если LibreOffice при конвертации всей книги не вывел «Выраб. — 4.0 Посев»
    и/или «Итого (все АФ)», добавляем только эти страницы из одно-листовых PDF.
    """
    if not _env_flag("SVODKI_REPORT1_ENSURE_SEEDING_PAGES", True):
        logs.append(
            f"Сводка {report_label}: восстановление страниц посева отключено "
            f"(SVODKI_REPORT1_ENSURE_SEEDING_PAGES=0)."
        )
        return False
    if not pdf_path.is_file() or not xlsx_path.is_file():
        return False

    need_perf, need_summary = _seeding_pages_missing(pdf_path, xlsx_path)

    # Эталон «ПРИМЕР.pdf»: 36 стр. при обоих листах посева в Excel.
    has_perf_sheet = _xlsx_has_sheet(xlsx_path, "Выраб. — 4.0 Посев")
    has_summary_sheet = _xlsx_has_sheet(xlsx_path, "Итого (все АФ)")
    if has_perf_sheet and has_summary_sheet:
        try:
            from pypdf import PdfReader

            n_pdf = len(PdfReader(str(pdf_path)).pages)
        except Exception:
            n_pdf = 0
        target_pages = 36
        if 0 < n_pdf < target_pages:
            perf_ok = _pdf_has_seeding_perf_page(pdf_path)
            summary_ok = _pdf_has_seeding_summary_page(pdf_path)
            if not perf_ok:
                need_perf = True
            if not summary_ok:
                need_summary = True
            # PDF короче эталона на 2+ стр. — вставляем оба листа, если ещё не найдены.
            if (target_pages - n_pdf) >= 2:
                if not perf_ok:
                    need_perf = True
                if not summary_ok:
                    need_summary = True

    if not need_perf and not need_summary:
        return False

    sheets_to_insert: list[str] = []
    if need_perf:
        sheets_to_insert.append("Выраб. — 4.0 Посев")
    if need_summary:
        sheets_to_insert.append("Итого (все АФ)")

    try:
        from pypdf import PdfReader, PdfWriter
    except Exception as exc:
        logs.append(f"Сводка {report_label}: восстановление посева — {exc}")
        return False

    import tempfile

    tmp_root = Path(tempfile.mkdtemp(prefix=f"r1_seed_{report_label}_"))
    inserted: list = []
    try:
        n_before = len(PdfReader(str(pdf_path)).pages)
        for title in sheets_to_insert:
            one_xlsx = tmp_root / f"sheet_{len(inserted)}.xlsx"
            if not _sheet_only_xlsx(xlsx_path, title, one_xlsx):
                logs.append(
                    f"Сводка {report_label}: в Excel нет «{title}» — "
                    "страницу PDF не восстановить."
                )
                continue
            try:
                try:
                    one_pdf = convert_fn(
                        one_xlsx, tmp_root, logo_root, summary_date=summary_date
                    )
                except TypeError:
                    one_pdf = convert_fn(one_xlsx, tmp_root, logo_root)
            except Exception as exc:
                logs.append(
                    f"Сводка {report_label}: PDF для «{title}» — {exc}"
                )
                continue
            if not one_pdf or not Path(one_pdf).is_file():
                logs.append(f"Сводка {report_label}: PDF для «{title}» не создан.")
                continue
            rdr = PdfReader(str(one_pdf))
            if not rdr.pages:
                continue
            inserted.append(rdr)
            logs.append(
                f"Сводка {report_label}: восстановлен лист «{title}» "
                f"({len(rdr.pages)} стр.)."
            )

        if not inserted:
            return False

        after = _find_pdf_page_after_veg_harvest(pdf_path)
        master = PdfReader(str(pdf_path))
        writer = PdfWriter()
        for i, page in enumerate(master.pages, start=1):
            writer.add_page(page)
            if i == after:
                for rdr in inserted:
                    for p in rdr.pages:
                        writer.add_page(p)

        tmp_out = pdf_path.with_suffix(".seed_fix.pdf")
        with tmp_out.open("wb") as fh:
            writer.write(fh)
        tmp_out.replace(pdf_path)
        new_n = len(PdfReader(str(pdf_path)).pages)
        logs.append(
            f"Сводка {report_label}: после стр.{after} добавлены страницы посева "
            f"({new_n} стр. всего, было {n_before})."
        )

        still_perf, still_summary = _seeding_pages_missing(pdf_path, xlsx_path)
        if still_perf or still_summary:
            logs.append(
                f"Сводка {report_label}: предупреждение — после восстановления посева "
                f"всё ещё нет "
                f"{'perf ' if still_perf else ''}"
                f"{'summary' if still_summary else ''}."
            )
        return True
    except Exception as exc:
        logs.append(f"Сводка {report_label}: ошибка восстановления посева — {exc}")
        return False
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _workbook_sheet_page_ranges(
    xlsx_path: Path,
    pdf_path: Path,
) -> dict[str, tuple[int, int]]:
    """1-based диапазоны страниц PDF по именам листов Excel."""
    from openpyxl import load_workbook
    from pypdf import PdfReader

    wb = load_workbook(xlsx_path, read_only=False, data_only=True)
    try:
        sheetnames = list(wb.sheetnames)
        counts = [_estimated_sheet_page_count(wb[sn]) for sn in sheetnames]
    finally:
        wb.close()
    n_pages = len(PdfReader(str(pdf_path)).pages)
    counts = _fit_sheet_page_counts_to_pdf(counts, n_pages)
    return _page_ranges_by_sheet(sheetnames, counts)


def restore_own_calc_pdf_pages(
    final_pdf: Path,
    initial_pdf: Path,
    initial_xlsx: Path,
    final_xlsx: Path,
    sheet_titles: tuple[str, ...],
    *,
    logs: list[str],
    report_label: str = "1",
) -> bool:
    """
    Копирует страницы PDF листов собственного расчёта сводки 1 из снимка до postprocess.

    Excel preserve не всегда попадает в финальный PDF после regenerate/reorder/graft;
    исходный PDF скрипта 1_main_ammonium остаётся эталоном для 1.1 МУ.
    """
    paths = (final_pdf, initial_pdf, initial_xlsx, final_xlsx)
    if not sheet_titles or not all(p.is_file() for p in paths):
        return False
    try:
        from openpyxl import load_workbook
        from pypdf import PdfReader, PdfWriter
    except Exception as exc:
        logs.append(f"Сводка {report_label}: восстановление PDF листов МУ — {exc}")
        return False

    try:
        init_reader = PdfReader(str(initial_pdf))
        fin_reader = PdfReader(str(final_pdf))
        n_fin = len(fin_reader.pages)
        if n_fin < 1:
            return False

        page_remap, restored_sheets = _build_page_remap_by_markers(
            initial_pdf, final_pdf, sheet_titles,
        )
        method = "маркеры PDF"

        if not page_remap:
            init_ranges = _workbook_sheet_page_ranges(initial_xlsx, initial_pdf)
            fin_ranges = _workbook_sheet_page_ranges(final_xlsx, final_pdf)
            init_wb = load_workbook(initial_xlsx, read_only=False, data_only=True)
            fin_wb = load_workbook(final_xlsx, read_only=False, data_only=True)
            restored_sheets = []
            try:
                for title in sheet_titles:
                    init_sn = _resolve_sheet_title(init_wb, title)
                    fin_sn = _resolve_sheet_title(fin_wb, title)
                    if not init_sn or not fin_sn:
                        continue
                    init_rng = init_ranges.get(init_sn)
                    fin_rng = fin_ranges.get(fin_sn)
                    if not init_rng or not fin_rng:
                        continue
                    i0, i1 = init_rng
                    f0, f1 = fin_rng
                    n_copy = min(i1 - i0 + 1, f1 - f0 + 1)
                    for off in range(n_copy):
                        src_p = i0 + off
                        dst_p = f0 + off
                        if 1 <= src_p <= len(init_reader.pages) and 1 <= dst_p <= n_fin:
                            page_remap[dst_p] = src_p
                    if n_copy > 0:
                        restored_sheets.append(title)
            finally:
                init_wb.close()
                fin_wb.close()
            method = "диапазоны листов Excel"

        if not page_remap:
            logs.append(
                f"Сводка {report_label}: восстановление PDF листов МУ — "
                "не найдены страницы для копирования."
            )
            return False

        tmp = final_pdf.with_suffix(".own_calc_restore_tmp.pdf")
        writer = PdfWriter()
        for dst_p in range(1, n_fin + 1):
            src_p = page_remap.get(dst_p)
            if src_p is not None:
                writer.add_page(init_reader.pages[src_p - 1])
            else:
                writer.add_page(fin_reader.pages[dst_p - 1])
        with tmp.open("wb") as fh:
            writer.write(fh)
        tmp.replace(final_pdf)
        logs.append(
            f"Сводка {report_label}: восстановлены PDF-страницы собственного расчёта "
            f"({', '.join(restored_sheets)}) из снимка до postprocess "
            f"({len(page_remap)} стр., {method})."
        )
        return True
    except Exception as exc:
        logs.append(
            f"Сводка {report_label}: ошибка восстановления PDF листов МУ — {exc}"
        )
        return False
