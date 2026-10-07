# -*- coding: utf-8 -*-
"""
Подстановка страниц в PDF сводки 1: левая часть страницы — данные донора (3–8),
правая полоса — как на исходной странице сводки 1 (последний столбец и т.п.).

Сетка таблицы задаётся страницей сводки 1: полный master, затем слева подставляется
донор; правая полоса — как на сводке 1. У донора и мастера часто разная высота шапки
и строк — тогда шапка и блок до «Итого» масштабируются по вертикали (PyMuPDF), иначе
цифры строк не попадают в одну линию с правым столбцом мастера. Ранняя подстановка
1:1 по координатам без масштаба давала нули/перепутанное время.

Требуется PyMuPDF (pymupdf).

По умолчанию левая часть (после подгонки по вертикали) превращается в одно растровое
изображение и вставляется поверх вырезанной области мастера: так в PDF не остаётся
двух «таблиц» друг на друге (нет лишних при выделении строк/столбцов, как при чистом
векторном наложении). Вернуть векторный вариант: SVODKI_PDF_GRAFT_VECTOR_LEFT=1

Параметры окружения:
  SVODKI_PDF_GRAFT_LAST_COL_FRAC — доля ширины справа, остающаяся от сводки 1 (0.02–0.45), по умол. 0.11
  SVODKI_PDF_GRAFT_NO_LAST_COL_MERGE=1 — не склеивать, только целые страницы донора
  SVODKI_PDF_GRAFT_LEGACY_VERTICAL=1 — старая подстановка 1:1 по Y (без сжатия шапки/тела)
  SVODKI_PDF_GRAFT_VECTOR_LEFT=1 — не растеризовать левую половину (как раньше: вектор донора поверх мастера)
  SVODKI_PDF_GRAFT_LEFT_RASTER_DPI — DPI для левой растра (по умол. 132, диапазон 96–240)
  SVODKI_PDF_GRAFT_TIME_ROW_MIN_Y — через запятую пороги min_y (pt) для поиска первой строки
      с временем слева от склейки; по умолчанию встроенная «лесенка», чтобы не цеплять шапку
  SVODKI_PDF_GRAFT_LEGACY_TIME_ROW_PAGES — номера стр. master (с 1), где не применять лесенку
      (по умол. 25,30 — доноры 7 и 6). «-» — везде лесенка; если у вас смещение на физ. стр. 6–7 — «6,7».
"""

from __future__ import annotations

import io
import os
import re
from pathlib import Path


def last_column_frac_from_env() -> float:
    raw = (os.environ.get("SVODKI_PDF_GRAFT_LAST_COL_FRAC") or "").strip()
    if not raw:
        return 0.11
    try:
        v = float(raw.replace(",", "."))
        if 0.02 <= v <= 0.45:
            return v
    except ValueError:
        pass
    return 0.11


def env_skip_last_col_merge() -> bool:
    return (os.environ.get("SVODKI_PDF_GRAFT_NO_LAST_COL_MERGE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }


def env_legacy_vertical_merge() -> bool:
    return (os.environ.get("SVODKI_PDF_GRAFT_LEGACY_VERTICAL") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }


def env_vector_left_merge() -> bool:
    """Векторное наложение донора без растра левой полосы (старое поведение)."""
    return (os.environ.get("SVODKI_PDF_GRAFT_VECTOR_LEFT") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }


def left_raster_dpi_from_env() -> int:
    raw = (os.environ.get("SVODKI_PDF_GRAFT_LEFT_RASTER_DPI") or "").strip()
    if not raw:
        return 132
    try:
        v = int(float(raw.replace(",", ".")))
        if 96 <= v <= 240:
            return v
    except ValueError:
        pass
    return 132


def _finalize_page_raster_left(
    mdoc: "fitz.Document",
    mid0: int,
    page_rect: "fitz.Rect",
    left_rect: "fitz.Rect",
    composed_page: "fitz.Page",
    dpi: int,
) -> bytes:
    """Одна картинка слева вместо слоёв вектор+текст — без «двойной» таблицы в PDF."""
    import fitz

    pm = composed_page.get_pixmap(clip=left_rect, dpi=dpi, alpha=False)
    out2 = fitz.open()
    try:
        n2 = out2.new_page(width=page_rect.width, height=page_rect.height)
        n2.show_pdf_page(n2.rect, mdoc, mid0)
        _apply_left_redact(n2, left_rect)
        n2.insert_image(left_rect, pixmap=pm, keep_proportion=False, overlay=True)
        return out2.tobytes(deflate=True, garbage=4)
    finally:
        out2.close()


def _x_split_nr(nr: "fitz.Rect", last_col_frac: float) -> float:
    return nr.x0 + (1.0 - last_col_frac) * nr.width


def _first_time_row_y(
    page: "fitz.Page", x_split: float, min_y: float = 0.0
) -> float | None:
    """Первая строка таблицы по времени слева от границы склейки (ЧЧ:ММ… в ячейке).

    min_y — отсечь многострочную шапку: совпадения времени в заголовке давали ложный y0,
    из-за чего вертикальная склейка стр. 24/25/27/28/30 (и др.) «плавала» относительно
    последних столбцов мастера (как наложение поверх).
    """
    pat = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?")
    best: float | None = None
    for w in page.get_text("words"):
        x0, y0 = w[0], w[1]
        text = (w[4] or "").strip()
        if x0 >= x_split or y0 < min_y or not pat.search(text):
            continue
        best = y0 if best is None else min(best, y0)
    return best


def _time_row_min_y_thresholds() -> list[float]:
    """Пороги для поиска первой строки данных (сверху вниз — сперва ниже шапки)."""
    raw = (os.environ.get("SVODKI_PDF_GRAFT_TIME_ROW_MIN_Y") or "").strip()
    if raw:
        try:
            return sorted(
                (float(x.replace(",", ".")) for x in raw.split(",") if x.strip()),
                reverse=True,
            )
        except ValueError:
            pass
    return [
        130.0,
        115.0,
        100.0,
        88.0,
        75.0,
        62.0,
        50.0,
        38.0,
        25.0,
        12.0,
        0.0,
    ]


def _paired_first_time_row_y(
    mp: "fitz.Page", dp: "fitz.Page", x_split: float
) -> tuple[float | None, float | None]:
    """Одинаковый min_y для мастера и донора, чтобы совпали границы шапка/тело."""
    for t in _time_row_min_y_thresholds():
        y_m = _first_time_row_y(mp, x_split, t)
        y_d = _first_time_row_y(dp, x_split, t)
        if y_m is not None and y_d is not None:
            return y_m, y_d
    return None, None


def legacy_time_row_master_pages() -> frozenset[int]:
    """Страницы сводки 1 (1-based), где вертикаль якорится по старой логике (без лесенки min_y).

    По умолчанию 25 и 30 — отчёты 7 и 6 в graft-спеке; для них лесенка min_y ломала выравнивание.
    Остальные склейки (24, 27, 28, …) — лесенка. «-» — везде лесенка; добавить физ. стр. 6–7: «6,7,25,30».
    """
    raw = (os.environ.get("SVODKI_PDF_GRAFT_LEGACY_TIME_ROW_PAGES") or "").strip()
    if raw == "":
        return frozenset({25, 30})
    if raw in {"-", "none", "0"}:
        return frozenset()
    out: set[int] = set()
    for p in raw.split(","):
        p = p.strip()
        if p.isdigit():
            out.add(int(p))
    return frozenset(out)


def _itogo_row_top(page: "fitz.Page", x_split: float) -> float | None:
    tops: list[float] = []
    for w in page.get_text("words"):
        x0, y0 = w[0], w[1]
        text = (w[4] or "").strip()
        if x0 >= x_split or text != "Итого":
            continue
        tops.append(y0)
    return min(tops) if tops else None


def _itogo_row_bottom(
    page: "fitz.Page", x_split: float, y_top: float, *, slack: float = 14.0
) -> float:
    bottoms: list[float] = []
    for w in page.get_text("words"):
        x0, y0, _x1, y1 = w[0], w[1], w[2], w[3]
        if x0 >= x_split or abs(y0 - y_top) > 8.0:
            continue
        bottoms.append(y1)
    return max(bottoms) if bottoms else y_top + slack


def _apply_left_redact(npage: "fitz.Page", left_rect: "fitz.Rect") -> None:
    """Удалить вектор/текст мастера слева, чтобы не дублировался с донором под белой заливкой."""
    try:
        import fitz

        npage.add_redact_annot(left_rect, fill=(1, 1, 1))
        try:
            npage.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_PIXELS | fitz.PDF_REDACT_TEXT_REMOVE
            )
        except TypeError:
            npage.apply_redactions()
    except Exception:
        npage.draw_rect(
            left_rect, color=(1, 1, 1), fill=(1, 1, 1), width=0, overlay=True
        )


def _merge_vertical_adaptive_fitz(
    mdoc: "fitz.Document",
    mid0: int,
    ddoc: "fitz.Document",
    did0: int,
    last_col_frac: float,
    *,
    master_page_1b: int,
) -> bytes | None:
    """Шапка и тело донора подгоняются по вертикали под мастер (разная высота строк/шапки)."""
    try:
        import fitz
    except ImportError:
        return None
    mp = mdoc[mid0]
    dp = ddoc[did0]
    pr = mp.rect
    x_split = _x_split_nr(pr, last_col_frac)
    im = _itogo_row_top(mp, x_split)
    id_ = _itogo_row_top(dp, x_split)
    legacy_pages = legacy_time_row_master_pages()
    if master_page_1b in legacy_pages:
        y_m0 = _first_time_row_y(mp, x_split, 0.0)
        y_d0 = _first_time_row_y(dp, x_split, 0.0)
        if (
            y_m0 is None
            or y_d0 is None
            or im is None
            or id_ is None
            or y_m0 < 40.0
            or y_d0 < 40.0
            or im <= y_m0
            or id_ <= y_d0
        ):
            return None
    else:
        y_m0, y_d0 = _paired_first_time_row_y(mp, dp, x_split)
        if (
            y_m0 is None
            or y_d0 is None
            or im is None
            or id_ is None
            or im <= y_m0
            or id_ <= y_d0
        ):
            return None
    ym1 = _itogo_row_bottom(mp, x_split, im)
    yd1 = _itogo_row_bottom(dp, x_split, id_)
    if ym1 <= y_m0 or yd1 <= y_d0:
        return None

    out = fitz.open()
    try:
        npage = out.new_page(width=pr.width, height=pr.height)
        nr = npage.rect
        npage.show_pdf_page(nr, mdoc, mid0)
        left_rect = fitz.Rect(nr.x0, nr.y0, x_split, nr.y1)
        _apply_left_redact(npage, left_rect)

        hdr_dest = fitz.Rect(nr.x0, nr.y0, x_split, y_m0)
        hdr_clip = fitz.Rect(nr.x0, nr.y0, x_split, y_d0)
        npage.show_pdf_page(hdr_dest, ddoc, did0, clip=hdr_clip, keep_proportion=False)

        body_dest = fitz.Rect(nr.x0, y_m0, x_split, ym1)
        body_clip = fitz.Rect(nr.x0, y_d0, x_split, yd1)
        npage.show_pdf_page(body_dest, ddoc, did0, clip=body_clip, keep_proportion=False)

        footer_dest = fitz.Rect(nr.x0, ym1, x_split, nr.y1)
        footer_clip = fitz.Rect(nr.x0, yd1, x_split, nr.y1)
        fh_d = float(nr.y1) - yd1
        fh_m = float(nr.y1) - ym1
        npage.show_pdf_page(
            footer_dest,
            ddoc,
            did0,
            clip=footer_clip,
            keep_proportion=abs(fh_d - fh_m) < 0.5,
        )
        if env_vector_left_merge():
            return out.tobytes(deflate=True, garbage=4)
        return _finalize_page_raster_left(
            mdoc,
            mid0,
            pr,
            left_rect,
            npage,
            left_raster_dpi_from_env(),
        )
    finally:
        out.close()


def _merge_one_page_fitz(
    master_pdf: Path,
    master_1b: int,
    donor_pdf: Path,
    donor_1b: int,
    last_col_frac: float,
) -> bytes | None:
    try:
        import fitz
    except ImportError:
        return None
    mid0 = master_1b - 1
    did0 = donor_1b - 1
    mdoc = None
    ddoc = None
    out = None
    try:
        mdoc = fitz.open(str(master_pdf))
        ddoc = fitz.open(str(donor_pdf))
        if mid0 < 0 or mid0 >= len(mdoc) or did0 < 0 or did0 >= len(ddoc):
            return None
        if not env_legacy_vertical_merge():
            merged = _merge_vertical_adaptive_fitz(
                mdoc,
                mid0,
                ddoc,
                did0,
                last_col_frac,
                master_page_1b=master_1b,
            )
            if merged is not None:
                return merged

        mp = mdoc[mid0]
        pr = mp.rect
        out = fitz.open()
        npage = out.new_page(width=pr.width, height=pr.height)
        nr = npage.rect
        # 1) Вся страница сводки 1 — эталон вертикальных линий и правый столбец.
        npage.show_pdf_page(nr, mdoc, mid0)
        x_split = nr.x0 + (1.0 - last_col_frac) * nr.width
        left_rect = fitz.Rect(nr.x0, nr.y0, x_split, nr.y1)
        _apply_left_redact(npage, left_rect)
        npage.show_pdf_page(left_rect, ddoc, did0, clip=left_rect)
        if env_vector_left_merge():
            return out.tobytes(deflate=True, garbage=4)
        return _finalize_page_raster_left(
            mdoc,
            mid0,
            pr,
            left_rect,
            npage,
            left_raster_dpi_from_env(),
        )
    except Exception:
        return None
    finally:
        if out is not None:
            out.close()
        if mdoc is not None:
            mdoc.close()
        if ddoc is not None:
            ddoc.close()


def replace_pages(
    master_pdf: Path,
    operations: list[tuple[int, Path, int]],
    *,
    merge_last_column_from_master: bool = False,
    last_column_frac: float | None = None,
) -> tuple[bool, list[str]]:
    """
    operations: (номер стр. в master с 1, путь донора, номер стр. в доноре с 1).
    merge_last_column_from_master: для сводки 1 — справа сохраняется полоса исходной
        страницы сводки 1; слева подставляется донор (та же геометрия клипа).
    """
    try:
        from pypdf import PdfReader, PdfWriter
    except Exception as e:
        return False, [str(e)]

    msgs: list[str] = []
    if last_column_frac is None:
        last_column_frac = last_column_frac_from_env()

    do_merge = merge_last_column_from_master and not env_skip_last_col_merge()
    if not master_pdf.is_file():
        return False, ["master PDF не найден"]

    master_r = PdfReader(str(master_pdf))
    n = len(master_r.pages)
    op_pages = {o[0] for o in operations}
    for dst_p in sorted(op_pages):
        if dst_p > n:
            msgs.append(
                f"ВНИМАНИЕ: в master только {n} стр. — подстановка для стр.{dst_p} пропущена "
                f"(в Excel сводки 1 другое число страниц)."
            )

    donors: dict[str, PdfReader] = {}
    writer = PdfWriter()
    try:
        for i in range(n):
            page_1b = i + 1
            op = next((o for o in operations if o[0] == page_1b), None)
            if op:
                _, donor_pdf, d_1b = op
                if not donor_pdf.is_file():
                    msgs.append(f"стр.{page_1b}: нет донора {donor_pdf.name}")
                    writer.add_page(master_r.pages[i])
                    continue
                key = str(donor_pdf.resolve())
                if key not in donors:
                    donors[key] = PdfReader(str(donor_pdf))
                dr = donors[key]
                di = d_1b - 1
                if di < 0 or di >= len(dr.pages):
                    msgs.append(f"стр.{page_1b}: у {donor_pdf.name} нет стр.{d_1b}")
                    writer.add_page(master_r.pages[i])
                    continue

                merged_bytes: bytes | None = None
                if do_merge:
                    merged_bytes = _merge_one_page_fitz(
                        master_pdf,
                        page_1b,
                        donor_pdf,
                        d_1b,
                        last_column_frac,
                    )
                if merged_bytes:
                    try:
                        mr = PdfReader(io.BytesIO(merged_bytes))
                        writer.add_page(mr.pages[0])
                    except Exception as exc:
                        msgs.append(
                            f"стр.{page_1b}: ошибка импорта склейки ({exc}), подставлена целая стр. донора"
                        )
                        writer.add_page(dr.pages[di])
                    else:
                        msgs.append(
                            f"стр.{page_1b} ← {donor_pdf.name} стр.{d_1b} "
                            f"(слева донор, справа {last_column_frac:.0%} — сводка 1)"
                        )
                else:
                    writer.add_page(dr.pages[di])
                    if do_merge:
                        msgs.append(
                            f"стр.{page_1b}: склейка столбца невозможна (нет pymupdf?) — "
                            f"целая стр. {donor_pdf.name} стр.{d_1b}"
                        )
                    else:
                        msgs.append(f"стр.{page_1b} ← {donor_pdf.name} стр.{d_1b}")
            else:
                writer.add_page(master_r.pages[i])

        tmp = master_pdf.with_suffix(".graft.pdf")
        with tmp.open("wb") as fp:
            writer.write(fp)
        tmp.replace(master_pdf)
        return True, msgs
    except Exception as e:
        return False, [str(e)]
