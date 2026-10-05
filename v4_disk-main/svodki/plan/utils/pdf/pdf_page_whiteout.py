# -*- coding: utf-8 -*-
"""Белые «закраски» поверх выбранных страниц PDF (нормализованные координаты)."""

from __future__ import annotations

import io
import os
import re
from pathlib import Path

# Доли высоты страницы **от низа вверх** (нижний край страницы = 0).
# Зона подобрана по реальному PDF донора 5 (A4 landscape ~842×595): блок «КУЛЬТУРЫ»
# заканчивается ~y≈222, «Овощи» / «Посев / посадка овощных» — примерно y≈235–375.
# Одна полоса закрывает обе «лишние» таблицы и не задевает основную сводную таблицу.
REPORT1_R5_PAGE28_DEFAULT_RECTS: tuple[tuple[float, float, float, float], ...] = (
    (0.012, 0.34, 0.988, 0.625),
)

# Если на «ожидаемой» странице (например 28) нет маркеров — ищем по всему PDF.
_R5_VEG_MARKER_SECOND = "Посев / посадка овощных"


def parse_whiteout_rects_from_env() -> list[tuple[float, float, float, float]] | None:
    """
    SVODKI_REPORT1_PAGE28_WHITEOUT_RECTS — прямоугольники через «|», каждый: x0,y0,x1,y1
    в долях ширины/высоты (0–1), y от **нижнего** края страницы вверх.
    """
    raw = (os.environ.get("SVODKI_REPORT1_PAGE28_WHITEOUT_RECTS") or "").strip()
    if not raw:
        return None
    rects: list[tuple[float, float, float, float]] = []
    for part in raw.split("|"):
        p = part.strip()
        if not p:
            continue
        nums = [float(x.strip()) for x in re.split(r"[,\s]+", p) if x.strip()]
        if len(nums) != 4:
            continue
        rects.append((nums[0], nums[1], nums[2], nums[3]))
    return rects if rects else None


def _pypdf_page_text_snippet(page) -> str:
    try:
        return str(page.extract_text() or "")
    except Exception:
        return ""


def _page_needs_r5_veg_whiteout_text(text: str) -> bool:
    t = text.replace("\u00a0", " ")
    if _R5_VEG_MARKER_SECOND in t:
        return True
    if "Овощи" in t and "План, га" in t and "Прогнозное время" in t:
        return True
    return False


def _resolve_target_pages(reader, hints_1b: list[int]) -> list[int]:
    """Страницы с «овощными» таблицами донора 5: сначала подсказки (напр. 28), иначе полный обход."""
    n = len(reader.pages)
    hints = sorted({p for p in hints_1b if 1 <= p <= n})
    for p1 in hints:
        if _page_needs_r5_veg_whiteout_text(_pypdf_page_text_snippet(reader.pages[p1 - 1])):
            return [p1]
    found: list[int] = []
    for i in range(n):
        if _page_needs_r5_veg_whiteout_text(_pypdf_page_text_snippet(reader.pages[i])):
            found.append(i + 1)
    if found:
        return found
    # Таблицы как вектор / без извлекаемого текста — всё равно маскируем подсказанные страницы (графт 28).
    return hints


def _mediabox_wh(page) -> tuple[float, float]:
    mb = page.mediabox
    return float(mb.width), float(mb.height)


def _apply_whiteout_fitz(
    pdf_path: Path,
    pages_1b: list[int],
    rects_norm: list[tuple[float, float, float, float]],
) -> bool:
    import fitz
    from pypdf import PdfReader

    rdr = PdfReader(str(pdf_path))
    targets = _resolve_target_pages(rdr, pages_1b)
    if not targets:
        return False
    doc = fitz.open(str(pdf_path))
    tmp = pdf_path.with_suffix(".whiteout_tmp.pdf")
    try:
        n = len(doc)
        for p1 in targets:
            if p1 < 1 or p1 > n:
                continue
            page = doc[p1 - 1]
            # Иначе rect/закраска смещены относительно видимого контента при /Rotate ≠ 0.
            try:
                if getattr(page, "rotation", 0):
                    page.remove_rotation()
            except Exception:
                pass
            pr = page.rect
            rw = pr.width
            rh = pr.height
            rlist: list = []
            pad = float((os.environ.get("SVODKI_PDF_WHITEOUT_PAD_PT") or "2").strip() or "2")
            for x0n, y0n, x1n, y1n in rects_norm:
                xa = pr.x0 + min(x0n, x1n) * rw
                xb = pr.x0 + max(x0n, x1n) * rw
                fa = min(y0n, y1n)
                fb = max(y0n, y1n)
                # Доли от нижнего края вверх; в PyMuPDF y растёт вниз (pr.y1 — нижний край).
                y_lo = pr.y1 - fa * rh
                y_hi = pr.y1 - fb * rh
                rr = fitz.Rect(xa, y_hi, xb, y_lo)
                if pad > 0:
                    try:
                        rr = (rr + (-pad, -pad, pad, pad)) & pr
                    except Exception:
                        rr = fitz.Rect(xa, y_hi, xb, y_lo)
                rlist.append(rr)
            for rr in rlist:
                page.add_redact_annot(rr, fill=(1, 1, 1))
            gra = getattr(fitz, "PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED", 2)
            try:
                page.apply_redactions(graphics=gra)
            except TypeError:
                page.apply_redactions()
            for rr in rlist:
                page.draw_rect(
                    rr, color=(1, 1, 1), fill=(1, 1, 1), width=0, overlay=True
                )
        doc.save(str(tmp), garbage=4, deflate=True)
    finally:
        doc.close()
    if tmp.is_file():
        tmp.replace(pdf_path)
        return True
    return False


def _apply_whiteout_pypdf_reportlab(
    pdf_path: Path,
    pages_1b: list[int],
    rects_norm: list[tuple[float, float, float, float]],
) -> bool:
    from pypdf import PdfReader, PdfWriter
    from reportlab.lib.colors import HexColor
    from reportlab.pdfgen import canvas

    reader = PdfReader(str(pdf_path))
    n = len(reader.pages)
    targets = _resolve_target_pages(reader, pages_1b)
    if not targets:
        return False
    want = set(targets)
    writer = PdfWriter()
    for i in range(n):
        p1 = i + 1
        page = reader.pages[i]
        if p1 not in want:
            writer.add_page(page)
            continue
        vw, vh = _mediabox_wh(page)
        packet = io.BytesIO()
        c = canvas.Canvas(packet, pagesize=(vw, vh))
        c.setFillColor(HexColor("#FFFFFF"))
        for x0n, y0n, x1n, y1n in rects_norm:
            xa = min(x0n, x1n) * vw
            xb = max(x0n, x1n) * vw
            ya = min(y0n, y1n) * vh
            yb = max(y0n, y1n) * vh
            c.rect(xa, ya, xb - xa, yb - ya, fill=1, stroke=0)
        c.save()
        packet.seek(0)
        overlay = PdfReader(packet).pages[0]
        try:
            page.merge_page(overlay, over=True)
        except TypeError:
            page.merge_page(overlay)
        writer.add_page(page)
    tmp = pdf_path.with_suffix(".whiteout.pdf")
    with tmp.open("wb") as f:
        writer.write(f)
    tmp.replace(pdf_path)
    return True


def apply_pdf_whiteout_normalized(
    pdf_path: Path,
    pages_1b: list[int],
    rects_norm: list[tuple[float, float, float, float]],
) -> bool:
    """
    Белые прямоугольники поверх страниц с овощными таблицами отчёта 5.
    Номер страницы может отличаться от 28 — ищем по тексту маркеров.
    """
    if not pdf_path.is_file() or not pages_1b or not rects_norm:
        return False
    dbg = (os.environ.get("SVODKI_PDF_WHITEOUT_DEBUG") or "").strip() in {"1", "true", "yes"}
    try:
        if _apply_whiteout_fitz(pdf_path, pages_1b, rects_norm):
            return True
    except ImportError:
        pass
    except Exception:
        if dbg:
            import traceback

            traceback.print_exc()
    try:
        return _apply_whiteout_pypdf_reportlab(pdf_path, pages_1b, rects_norm)
    except Exception:
        if dbg:
            import traceback

            traceback.print_exc()
        return False
