"""Наложение правок ячеек на PDF: закрасить исходный фрагмент цветом ячейки и нарисовать новый текст."""

from __future__ import annotations

import io
import re
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")

# Фигуры тоньше этого (пункты) считаем линиями сетки, а не заливкой.
_THIN_SHAPE_PT = 1.8


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _normalize_hex_color(value: Any, *, default: str = "#ffffff") -> str:
    raw = str(value or "").strip()
    if not raw:
        return default
    if not raw.startswith("#"):
        raw = f"#{raw}"
    if not _HEX_RE.match(raw):
        return default
    return f"#{raw[-6:].lower()}"


def _luminance(r: int, g: int, b: int) -> float:
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


def _contrast_text_hex(fill_hex: str) -> str:
    h = _normalize_hex_color(fill_hex)
    r = int(h[1:3], 16)
    g = int(h[3:5], 16)
    b = int(h[5:7], 16)
    return "#111827" if _luminance(r, g, b) > 0.55 else "#ffffff"


def _rgb01_to_hex(fr: float, fg: float, fb: float) -> str:
    r = max(0, min(255, int(round(float(fr) * 255.0))))
    g = max(0, min(255, int(round(float(fg) * 255.0))))
    b = max(0, min(255, int(round(float(fb) * 255.0))))
    return f"#{r:02x}{g:02x}{b:02x}"


def _find_cell_fill(
    fitz_page: Any,
    *,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
) -> tuple[str | None, tuple[float, float, float, float] | None]:
    """Цвет и прямоугольник заливки ячейки (PyMuPDF top-left)."""
    try:
        import fitz
    except Exception:
        return None, None
    try:
        page_rect = fitz_page.rect
        probe = fitz.Rect(x0 - 14.0, y0 - 1.5, x1 + 28.0, y1 + 4.0) & page_rect
        if probe.is_empty:
            return None, None
        cell_cy = (y0 + y1) / 2.0
        best_fill: tuple[float, float, float] | None = None
        best_rect: tuple[float, float, float, float] | None = None
        best_score = -1.0
        for drawing in fitz_page.get_drawings() or []:
            fill = drawing.get("fill")
            if not fill or len(fill) < 3:
                continue
            raw_rect = drawing.get("rect")
            if raw_rect is None:
                continue
            rect = fitz.Rect(raw_rect)
            # Тонкие закрашенные полосы — это линии границ, а не заливка ячейки.
            if min(float(rect.width), float(rect.height)) < _THIN_SHAPE_PT:
                continue
            inter = rect & probe
            if inter.is_empty:
                continue
            # Заливка обязана накрывать саму цифру. Иначе узкая цветная ячейка
            # справа (её задевает расширенный вправо поиск) побеждает белый фон
            # строки, и маска вместе с новым текстом уезжает в соседний столбец.
            cx = (x0 + x1) / 2.0
            tol = 1.5
            if not (rect.x0 - tol <= cx <= rect.x1 + tol and rect.y0 - tol <= cell_cy <= rect.y1 + tol):
                continue
            area = float(inter.width * inter.height)
            if area < 0.8:
                continue
            fr, fg, fb = float(fill[0]), float(fill[1]), float(fill[2])
            is_white = fr > 0.97 and fg > 0.97 and fb > 0.97
            row_h = float(rect.height)
            score = area
            if 5.0 <= row_h <= 22.0:
                score *= 10.0
            elif 22.0 < row_h <= 45.0:
                score *= 3.0
            else:
                score *= 0.25
            if is_white:
                score *= 0.12
            rect_cy = (rect.y0 + rect.y1) / 2.0
            if abs(cell_cy - rect_cy) <= max(5.0, row_h * 0.6):
                score *= 2.2
            # Предпочитаем узкие ячейки столбца, а не всю строку таблицы.
            if 8.0 <= float(rect.width) <= 120.0:
                score *= 1.8
            if score > best_score:
                best_score = score
                best_fill = (fr, fg, fb)
                best_rect = (float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1))
        if best_fill is None:
            return None, None
        return _rgb01_to_hex(*best_fill), best_rect
    except Exception:
        return None, None


def _fill_hex_from_drawings(
    fitz_page: Any,
    *,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
) -> str | None:
    fill_hex, _ = _find_cell_fill(fitz_page, x0=x0, y0=y0, x1=x1, y1=y1)
    return fill_hex


def _sample_fill_hex_from_pixmap(samples: bytes, n: int) -> str | None:
    """Запасной сэмпл заливки из pixmap, если в drawings не нашли."""
    if n <= 0 or len(samples) < n * 3:
        return None
    buckets: dict[tuple[int, int, int], list[tuple[int, int, int]]] = defaultdict(list)
    step = max(1, n // 1200)
    for i in range(0, n, step):
        o = i * 3
        r, g, b = samples[o], samples[o + 1], samples[o + 2]
        lum = _luminance(r, g, b)
        if lum < 0.18:
            continue
        chroma = max(r, g, b) - min(r, g, b)
        if lum < 0.48 and chroma < 28:
            continue
        key = (r >> 4, g >> 4, b >> 4)
        buckets[key].append((r, g, b))
    if not buckets:
        return None

    ranked = sorted(buckets.items(), key=lambda kv: len(kv[1]), reverse=True)[:4]

    def _cluster_rgb(pixels: list[tuple[int, int, int]]) -> tuple[tuple[float, float, float], float]:
        ordered = sorted(pixels, key=lambda p: _luminance(*p))
        cut = int(len(ordered) * 0.40)
        keep = ordered[cut:] or ordered
        rr = sum(p[0] for p in keep) / len(keep)
        gg = sum(p[1] for p in keep) / len(keep)
        bb = sum(p[2] for p in keep) / len(keep)
        return (rr, gg, bb), _luminance(rr, gg, bb)

    best_rgb: tuple[float, float, float] | None = None
    best_score = -1.0
    for _, pixels in ranked:
        rgb, lum = _cluster_rgb(pixels)
        score = float(len(pixels)) * (1.0 + lum * 1.6)
        if score > best_score:
            best_score = score
            best_rgb = rgb
    if best_rgb is None:
        return None
    r = max(0, min(255, int(round(best_rgb[0]))))
    g = max(0, min(255, int(round(best_rgb[1]))))
    b = max(0, min(255, int(round(best_rgb[2]))))
    return f"#{r:02x}{g:02x}{b:02x}"


def _sample_cell_fill(
    fitz_page: Any,
    *,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
) -> tuple[str | None, tuple[float, float, float, float] | None]:
    """Цвет и rect заливки. Координаты — PyMuPDF (origin сверху)."""
    fill_hex, fill_rect = _find_cell_fill(fitz_page, x0=x0, y0=y0, x1=x1, y1=y1)
    if fill_hex:
        return fill_hex, fill_rect
    try:
        import fitz
    except Exception:
        return None, None
    try:
        page_rect = fitz_page.rect
        pad = max(1.2, min(3.0, (y1 - y0) * 0.35))
        clip = fitz.Rect(x0 - pad, y0 - pad, x1 + pad, y1 + pad) & page_rect
        if clip.is_empty or clip.width < 0.4 or clip.height < 0.4:
            return None, None
        pix = fitz_page.get_pixmap(matrix=fitz.Matrix(4, 4), clip=clip, alpha=False)
        return _sample_fill_hex_from_pixmap(pix.samples, pix.width * pix.height), None
    except Exception:
        return None, None


def _sample_cell_fill_hex(
    fitz_page: Any,
    *,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
) -> str | None:
    fill_hex, _ = _sample_cell_fill(fitz_page, x0=x0, y0=y0, x1=x1, y1=y1)
    return fill_hex


def _rgb_int_to_hex(color: int) -> str:
    c = int(color) & 0xFFFFFF
    return f"#{c:06x}"


def _detect_local_span(
    fitz_page: Any,
    *,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
) -> dict[str, Any] | None:
    """Шрифт/кегль/цвет исходного текста. Координаты — PyMuPDF (origin сверху)."""
    try:
        import fitz
    except Exception:
        return None
    try:
        clip = fitz.Rect(x0, y0, x1, y1) & fitz_page.rect
        if clip.is_empty:
            return None
        data = fitz_page.get_text("dict", clip=clip)
        best: dict[str, Any] | None = None
        best_score = -1.0
        for block in data.get("blocks", []) or []:
            if int(block.get("type", 0) or 0) != 0:
                continue
            for line in block.get("lines", []) or []:
                for span in line.get("spans", []) or []:
                    txt = str(span.get("text") or "").strip()
                    if not txt:
                        continue
                    size = float(span.get("size") or 0.0)
                    bbox = span.get("bbox") or [0, 0, 0, 0]
                    area = abs(float(bbox[2]) - float(bbox[0])) * abs(float(bbox[3]) - float(bbox[1]))
                    score = len(txt) * 10.0 + size + area * 0.01
                    if score > best_score:
                        best_score = score
                        best = span
        if not best:
            return None
        origin = best.get("origin") or (0.0, 0.0)
        return {
            "font": str(best.get("font") or ""),
            "size": float(best.get("size") or 0.0),
            "color": int(best.get("color") or 0),
            "flags": int(best.get("flags") or 0),
            # Базовая линия исходного текста (PyMuPDF, origin сверху).
            "origin_y": float(origin[1]),
        }
    except Exception:
        return None


def _register_svodki_fonts(pdfmetrics: Any, TTFont: Any) -> dict[str, str]:
    """Регистрирует шрифты сводок (Liberation + запасной DejaVu)."""
    catalog = {
        "liberation_sans": "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "liberation_sans_bold": "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "liberation_serif": "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
        "liberation_serif_bold": "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
        "dejavu_sans": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "dejavu_sans_bold": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "dejavu_serif": "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
        "dejavu_serif_bold": "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    }
    registered: dict[str, str] = {}
    for key, path in catalog.items():
        p = Path(path)
        if not p.exists():
            continue
        name = f"SvodkiCellEdit_{key}"
        try:
            pdfmetrics.registerFont(TTFont(name, str(p)))
            registered[key] = name
        except Exception:
            continue
    return registered


def _pick_font_name(registered: dict[str, str], span_font: str | None) -> str:
    name = str(span_font or "").lower().replace(" ", "")
    bold = ("bold" in name) or ("black" in name)
    serif = ("serif" in name) and ("sans" not in name)
    if "dejavu" in name:
        if serif:
            key = "dejavu_serif_bold" if bold else "dejavu_serif"
        else:
            key = "dejavu_sans_bold" if bold else "dejavu_sans"
    elif serif:
        key = "liberation_serif_bold" if bold else "liberation_serif"
    else:
        # В табличных сводках основной шрифт — LiberationSans.
        key = "liberation_sans_bold" if bold else "liberation_sans"
    if key in registered:
        return registered[key]
    for fallback in (
        "liberation_sans",
        "liberation_sans_bold",
        "liberation_serif",
        "liberation_serif_bold",
        "dejavu_sans",
        "dejavu_sans_bold",
        "dejavu_serif",
        "dejavu_serif_bold",
    ):
        if fallback in registered:
            return registered[fallback]
    return "Helvetica"


def _fit_font_size(
    canvas_obj: Any,
    text: str,
    *,
    font_name: str,
    max_width: float,
    max_height: float,
    min_size: float = 6.0,
    max_size: float = 12.0,
) -> float:
    size = min(max_size, max(min_size, max_height * 0.92))
    while size > min_size:
        if canvas_obj.stringWidth(text, font_name, size) <= max_width and size <= max_height:
            return size
        size -= 0.25
    return min_size


def _collect_border_lines(fitz_page: Any) -> list[tuple[str, float, float, float, float]]:
    """Линии сетки страницы: ('h', y0, y1, x0, x1) или ('v', x0, x1, y0, y1). PyMuPDF, origin сверху.

    Граница в PDF бывает штрихом (stroke), тонким закрашенным прямоугольником или краем
    обведённого прямоугольника. Толщина штриха учитывается, чтобы не задеть его край.
    """
    try:
        import fitz
    except Exception:
        return []
    out: list[tuple[str, float, float, float, float]] = []

    def add_rect(r: Any, half: float) -> None:
        x0, y0, x1, y1 = float(r.x0) - half, float(r.y0) - half, float(r.x1) + half, float(r.y1) + half
        if (y1 - y0) <= _THIN_SHAPE_PT and (x1 - x0) >= 3.0:
            out.append(("h", y0, y1, x0, x1))
        elif (x1 - x0) <= _THIN_SHAPE_PT and (y1 - y0) >= 3.0:
            out.append(("v", x0, x1, y0, y1))

    try:
        for d in fitz_page.get_drawings() or []:
            kind = str(d.get("type") or "")
            stroked = "s" in kind and d.get("color") is not None
            half = (float(d.get("width") or 0.5) / 2.0) if stroked else 0.0
            for item in d.get("items") or []:
                op = item[0]
                if op == "l":
                    p1, p2 = item[1], item[2]
                    if abs(p1.y - p2.y) < 0.05 and abs(p1.x - p2.x) >= 3.0 and stroked:
                        out.append(("h", p1.y - half, p1.y + half, min(p1.x, p2.x), max(p1.x, p2.x)))
                    elif abs(p1.x - p2.x) < 0.05 and abs(p1.y - p2.y) >= 3.0 and stroked:
                        out.append(("v", p1.x - half, p1.x + half, min(p1.y, p2.y), max(p1.y, p2.y)))
                elif op == "re":
                    r = fitz.Rect(item[1])
                    if min(r.width, r.height) <= _THIN_SHAPE_PT:
                        add_rect(r, half)
                    elif stroked:
                        # Обведённый прямоугольник: четыре края — границы.
                        add_rect(fitz.Rect(r.x0, r.y0, r.x1, r.y0), half)
                        add_rect(fitz.Rect(r.x0, r.y1, r.x1, r.y1), half)
                        add_rect(fitz.Rect(r.x0, r.y0, r.x0, r.y1), half)
                        add_rect(fitz.Rect(r.x1, r.y0, r.x1, r.y1), half)
                elif op == "qu":
                    r = fitz.Rect(item[1].rect)
                    if min(r.width, r.height) <= _THIN_SHAPE_PT:
                        add_rect(r, half)
    except Exception:
        return out
    return out


def _clip_rect_to_borders(
    lines: list[tuple[str, float, float, float, float]],
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    *,
    cx: float,
    cy: float,
    gap: float = 0.2,
) -> tuple[float, float, float, float]:
    """Подрезать прямоугольник так, чтобы он не заходил на линии сетки (PyMuPDF, origin сверху).

    Линия ниже/правее центра ограничивает низ/право, выше/левее — верх/лево.
    """
    for kind, a0, a1, b0, b1 in lines:
        if kind == "h":
            if min(x1, b1) - max(x0, b0) < min(2.0, 0.3 * (x1 - x0)):
                continue
            if a1 <= y0 or a0 >= y1:
                continue
            if (a0 + a1) / 2.0 >= cy:
                y1 = min(y1, a0 - gap)
            else:
                y0 = max(y0, a1 + gap)
        else:
            if min(y1, b1) - max(y0, b0) < min(2.0, 0.3 * (y1 - y0)):
                continue
            if a1 <= x0 or a0 >= x1:
                continue
            if (a0 + a1) / 2.0 >= cx:
                x1 = min(x1, a0 - gap)
            else:
                x0 = max(x0, a1 + gap)
    return x0, y0, x1, y1


def _edit_rect_pdf_top_left(row: dict[str, Any], page_w: float, page_h: float) -> tuple[float, float, float, float]:
    """Прямоугольник правки в координатах PyMuPDF (origin сверху), пункты."""
    left = _clamp01(float(row.get("left", 0.0)))
    top = _clamp01(float(row.get("top", 0.0)))
    width = max(0.001, float(row.get("width", 0.01)))
    height = max(0.001, float(row.get("height", 0.02)))
    return left * page_w, top * page_h, (left + width) * page_w, (top + height) * page_h


def _superseded_edit_ids(rows: list[dict[str, Any]], page_w: float, page_h: float) -> set[int]:
    """Индексы правок, чей текст перекрыт более новой правкой того же места.

    Раньше повторная правка ячейки (по уже заменённому тексту) сохранялась отдельной строкой,
    и обе замены рисовались друг на друге. Рисуем только самую новую.
    """
    order = sorted(range(len(rows)), key=lambda i: str(rows[i].get("updated_at") or ""))
    dead: set[int] = set()
    for pos, i in enumerate(order):
        ax0, ay0, ax1, ay1 = _edit_rect_pdf_top_left(rows[i], page_w, page_h)
        for j in order[pos + 1 :]:
            bx0, by0, bx1, by1 = _edit_rect_pdf_top_left(rows[j], page_w, page_h)
            cx, cy = (bx0 + bx1) / 2.0, (by0 + by1) / 2.0
            tol_x = max(2.0, (ax1 - ax0) * 0.5)
            tol_y = max(1.5, (ay1 - ay0) * 0.5)
            if ax0 - tol_x <= cx <= ax1 + tol_x and ay0 - tol_y <= cy <= ay1 + tol_y:
                dead.add(i)
                break
    return dead


def _glyphs_remain(page: Any, probe: Any) -> bool:
    """Остались ли в области видимые символы (центр бокса внутри probe)."""
    try:
        raw = page.get_text("rawdict", clip=probe)
    except Exception:
        return True
    for block in raw.get("blocks", []) or []:
        if int(block.get("type", 0) or 0) != 0:
            continue
        for line in block.get("lines", []) or []:
            for span in line.get("spans", []) or []:
                for ch in span.get("chars", []) or []:
                    if not str(ch.get("c") or "").strip():
                        continue
                    bb = ch.get("bbox") or (0, 0, 0, 0)
                    cx, cy = (bb[0] + bb[2]) / 2.0, (bb[1] + bb[3]) / 2.0
                    if probe.x0 <= cx <= probe.x1 and probe.y0 <= cy <= probe.y1:
                        return True
    return False


def _strip_original_text(
    base_pdf_path: Path, page_map: dict[int, list[dict[str, Any]]]
) -> tuple[Path | None, set[int]]:
    """Физически удалить исходный текст редактируемых ячеек из копии PDF.

    Раньше исходное значение только закрашивалось прямоугольником и оставалось в файле
    под новым («1», поверх которой «0»). Здесь глифы вырезаются из потока содержимого
    страницы (redaction без заливки), линии сетки и картинки не трогаем.
    Возвращает (путь к временной копии или None, id() строк правок, у которых глифы
    действительно вырезаны). Для таких правок маску рисовать не нужно.
    """
    try:
        import fitz
    except Exception:
        return None, set()
    out = base_pdf_path.with_suffix(".stripped.tmp.pdf")
    doc = None
    removed: set[int] = set()
    try:
        doc = fitz.open(str(base_pdf_path))
        touched = False
        for page_no, rows in page_map.items():
            if not (1 <= page_no <= doc.page_count):
                continue
            page = doc[page_no - 1]
            pw, ph = float(page.rect.width), float(page.rect.height)
            page_probes: list[tuple[dict[str, Any], Any]] = []
            for row in rows:
                x0, y0, x1, y1 = _edit_rect_pdf_top_left(row, pw, ph)
                # pdf.js отдаёт рамку по базовой линии; даём запас по краям.
                probe = fitz.Rect(x0 - 1.5, y0 - 2.0, x1 + 1.5, y1 + 2.5) & page.rect
                if probe.is_empty:
                    continue
                found = False
                raw = page.get_text("rawdict", clip=probe)
                for block in raw.get("blocks", []) or []:
                    if int(block.get("type", 0) or 0) != 0:
                        continue
                    for line in block.get("lines", []) or []:
                        for span in line.get("spans", []) or []:
                            for ch in span.get("chars", []) or []:
                                if not str(ch.get("c") or "").strip():
                                    continue
                                bb = fitz.Rect(ch.get("bbox") or (0, 0, 0, 0))
                                cx, cy = (bb.x0 + bb.x1) / 2.0, (bb.y0 + bb.y1) / 2.0
                                if not (probe.x0 <= cx <= probe.x1 and probe.y0 <= cy <= probe.y1):
                                    continue
                                # Чуть сжимаем бокс символа, чтобы не задеть соседей.
                                shrink_x, shrink_y = bb.width * 0.2, bb.height * 0.2
                                tight = fitz.Rect(
                                    bb.x0 + shrink_x, bb.y0 + shrink_y, bb.x1 - shrink_x, bb.y1 - shrink_y
                                )
                                page.add_redact_annot(tight, fill=False)
                                found = True
                if found:
                    page_probes.append((row, probe))
                if not found:
                    # Символы не нашлись (текст — картинка/контуры): режем по центру рамки.
                    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
                    page.add_redact_annot(fitz.Rect(cx - 0.5, cy - 0.5, cx + 0.5, cy + 0.5), fill=False)
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=getattr(fitz, "PDF_REDACT_LINE_ART_NONE", 0),
                text=getattr(fitz, "PDF_REDACT_TEXT_REMOVE", 0),
            )
            # Самопроверка: после вырезания в области правки не должно остаться символов.
            # Если остались (мелкие знаки вроде запятой, нестандартный шрифт) — для такой правки
            # нужна маска, поэтому в «очищенные» её не записываем.
            for row, probe in page_probes:
                if not _glyphs_remain(page, probe):
                    removed.add(id(row))
            touched = True
        if not touched:
            return None, set()
        doc.save(str(out), garbage=3, deflate=True)
        return out, removed
    except Exception:
        out.unlink(missing_ok=True)
        return None, set()
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass


def apply_cell_edits_to_pdf(
    *,
    target_pdf_path: Path,
    base_pdf_path: Path,
    edits: list[dict[str, Any]],
) -> None:
    if not base_pdf_path.exists():
        raise FileNotFoundError(f"Базовый PDF не найден: {base_pdf_path}")
    if not edits:
        tmp_copy = target_pdf_path.with_suffix(".tmp.pdf")
        shutil.copy2(base_pdf_path, tmp_copy)
        tmp_copy.replace(target_pdf_path)
        return

    try:
        from pypdf import PdfReader, PdfWriter
    except Exception:
        from PyPDF2 import PdfReader, PdfWriter
    from reportlab.lib.colors import HexColor, white
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    registered_fonts = _register_svodki_fonts(pdfmetrics, TTFont)

    page_map: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in edits:
        try:
            page = int(row.get("page", 1))
        except Exception:
            page = 1
        if page < 1:
            page = 1
        page_map[page].append(row)
    for rows_on_page in page_map.values():
        # Новые правки рисуем последними — они должны лежать сверху.
        rows_on_page.sort(key=lambda r: str(r.get("updated_at") or ""))

    stripped_pdf, removed_ids = _strip_original_text(base_pdf_path, page_map)
    reader = PdfReader(str(stripped_pdf or base_pdf_path))
    writer = PdfWriter()

    fitz_doc = None
    try:
        import fitz

        fitz_doc = fitz.open(str(base_pdf_path))
    except Exception:
        fitz_doc = None

    try:
        for idx, page in enumerate(reader.pages, start=1):
            rows = page_map.get(idx, [])
            if not rows:
                writer.add_page(page)
                continue
            w = float(page.mediabox.width)
            h = float(page.mediabox.height)
            packet = io.BytesIO()
            c = canvas.Canvas(packet, pagesize=(w, h))
            fitz_page = None
            if fitz_doc is not None and 0 <= idx - 1 < fitz_doc.page_count:
                fitz_page = fitz_doc[idx - 1]

            border_lines = _collect_border_lines(fitz_page) if fitz_page is not None else []

            dead_rows = _superseded_edit_ids(rows, w, h)
            for row_idx, row in enumerate(rows):
                left = _clamp01(float(row.get("left", 0.0)))
                top = _clamp01(float(row.get("top", 0.0)))
                width = max(0.001, float(row.get("width", 0.01)))
                height = max(0.001, float(row.get("height", 0.02)))
                text = str(row.get("replacement") or "").strip()
                orig_text = str(row.get("cell_text") or "").strip()

                # PDF (reportlab): origin снизу. PyMuPDF: origin сверху.
                orig_x0 = left * w
                orig_x1 = (left + width) * w
                orig_y1 = h - top * h
                orig_y0 = h - (top + height) * h
                fitz_y0 = top * h
                fitz_y1 = (top + height) * h
                orig_w = max(1.0, orig_x1 - orig_x0)
                orig_h = max(1.0, orig_y1 - orig_y0)

                span = None
                if fitz_page is not None:
                    span = _detect_local_span(
                        fitz_page,
                        x0=orig_x0,
                        y0=fitz_y0,
                        x1=orig_x1,
                        y1=fitz_y1,
                    )
                text_font = _pick_font_name(
                    registered_fonts,
                    str((span or {}).get("font") or "LiberationSans"),
                )
                span_size = float((span or {}).get("size") or 0.0)
                # Кегль: как у исходного span, иначе от высоты глифа.
                target_font = span_size if span_size >= 4.5 else max(5.5, min(10.0, orig_h * 0.88))
                if text:
                    probe_w = c.stringWidth(text, text_font, target_font)
                else:
                    probe_w = 0.0

                # Центр по горизонтали — по исходному фрагменту; маску вниз увеличиваем.
                cell_cx = (orig_x0 + orig_x1) / 2.0

                # По ширине расширяем симметрично под длинную замену.
                pad_x = max(1.2, orig_w * 0.2, 0.002 * w)
                content_w = max(orig_w, probe_w + 1.2)
                half_w = max(orig_w / 2.0 + pad_x * 0.25, content_w / 2.0 + pad_x * 0.35)
                x0 = max(0.0, cell_cx - half_w)
                x1 = min(w, cell_cx + half_w)

                # Верх маски не поднимаем. Низ опускаем под запятые, но не на линии сетки.
                top_clear = max(0.25, min(0.55, orig_h * 0.1))
                y1 = orig_y1 - top_clear
                needs_comma_cover = any(ch in orig_text for ch in ",;.")
                expand_down = 2.1 if needs_comma_cover else 1.4
                y0 = orig_y0 - expand_down
                if y1 - y0 < 2.5:
                    y0 = y1 - 2.5

                # Цвет и границы заливки ячейки (чтобы не перекрывать линии сетки).
                fill_hex = None
                fill_fitz_rect = None
                if fitz_page is not None:
                    fill_hex, fill_fitz_rect = _sample_cell_fill(
                        fitz_page,
                        x0=orig_x0,
                        y0=fitz_y0,
                        x1=orig_x1,
                        y1=fitz_y1,
                    )
                if not fill_hex:
                    stored_fill = str(row.get("fill_color") or row.get("bg_color") or "").strip()
                    if stored_fill:
                        candidate = _normalize_hex_color(stored_fill, default="")
                        fill_hex = candidate or None
                if not fill_hex:
                    fill_hex = "#ffffff"

                # Жёстко держим маску внутри заливки ячейки с отступом от бордеров.
                border_inset = 0.7
                if fill_fitz_rect is not None:
                    fx0, fy0, fx1, fy1 = fill_fitz_rect
                    # fitz top-left → PDF bottom-left
                    cell_pdf_x0 = float(fx0)
                    cell_pdf_x1 = float(fx1)
                    cell_pdf_y1 = h - float(fy0)
                    cell_pdf_y0 = h - float(fy1)
                    x0 = max(x0, cell_pdf_x0 + border_inset)
                    x1 = min(x1, cell_pdf_x1 - border_inset)
                    y0 = max(y0, cell_pdf_y0 + border_inset)
                    y1 = min(y1, cell_pdf_y1 - border_inset)
                else:
                    # Без rect заливки — хотя бы не прижимаемся к краям глифа слишком широко.
                    x0 += 0.35
                    x1 -= 0.35
                    y0 += 0.25
                    y1 -= 0.25

                # Не заходим на линии сетки: ни маской, ни текстом (иначе граница «съедается»).
                if border_lines:
                    cx0, cy0, cx1, cy1 = _clip_rect_to_borders(
                        border_lines,
                        x0,
                        h - y1,
                        x1,
                        h - y0,
                        cx=cell_cx,
                        cy=(fitz_y0 + fitz_y1) / 2.0,
                    )
                    if cx1 - cx0 >= 1.0 and cy1 - cy0 >= 2.0:
                        x0, x1, y0, y1 = cx0, cx1, h - cy1, h - cy0

                if x1 - x0 < 1.0:
                    mid = (x0 + x1) / 2.0
                    x0, x1 = mid - 0.5, mid + 0.5
                if y1 - y0 < 2.0:
                    mid = (y0 + y1) / 2.0
                    y0, y1 = mid - 1.0, mid + 1.0

                box_w = max(1.0, x1 - x0)
                box_h = max(1.0, y1 - y0)

                # Цвет текста: из исходного span, иначе контраст к фону.
                text_hex = None
                if span is not None:
                    text_hex = _rgb_int_to_hex(int(span.get("color") or 0))
                    # color=0 — чёрный; белый на тёмном фоне тоже валиден.
                if not text_hex:
                    stored_text = str(row.get("text_color") or "").strip()
                    if stored_text:
                        text_hex = _normalize_hex_color(
                            stored_text, default=_contrast_text_hex(fill_hex)
                        )
                    else:
                        text_hex = _contrast_text_hex(fill_hex)

                # Исходные глифы уже вырезаны из PDF — закрашивать нечего, и фон/границы остаются
                # нетронутыми. Маска нужна только в запасном случае (текст не удалось вырезать).
                needs_mask = stripped_pdf is None or id(row) not in removed_ids
                if needs_mask:
                    try:
                        c.setFillColor(HexColor(fill_hex))
                    except Exception:
                        c.setFillColor(white)
                    c.rect(x0, y0, box_w, box_h, stroke=0, fill=1)

                if not text or row_idx in dead_rows:
                    continue

                max_size = max(5.0, min(14.0, target_font + 0.5))
                # Кегль подбираем по ширине исходного фрагмента, а не по подрезанной маске:
                # маска бывает уже цифры, и тогда шрифт сжимался, а текст уезжал.
                fit_width = max(4.0, orig_w)
                font_size = _fit_font_size(
                    c,
                    text,
                    font_name=text_font,
                    max_width=fit_width,
                    max_height=max(3.5, min(max(box_h - 0.5, orig_h), max(orig_h + 0.5, target_font + 0.5))),
                    min_size=max(4.5, min(target_font, 5.0)),
                    max_size=max_size,
                )
                # Если влез исходный кегль — используем его (совпадение со сводкой).
                if span_size >= 4.5 and c.stringWidth(text, text_font, span_size) <= fit_width + 1.0:
                    font_size = span_size
                try:
                    c.setFillColor(HexColor(text_hex))
                except Exception:
                    c.setFillColor(HexColor("#111827"))
                c.setFont(text_font, font_size)
                text_w = c.stringWidth(text, text_font, font_size)
                # Центр исходного фрагмента. К краю маски не прижимаем: если заливка
                # нашлась неверно, прижатие сдвигает цифру вправо, в соседнюю ячейку.
                tx = cell_cx - text_w / 2.0
                if tx < 0.4:
                    tx = 0.4
                if tx + text_w > w - 0.4:
                    tx = max(0.4, w - text_w - 0.4)
                # Вертикально — на исходной высоте текста (не уезжаем вниз вместе с маской).
                baseline = None
                if span is not None and span.get("origin_y") is not None:
                    origin_y = float(span["origin_y"])
                    if abs(origin_y - fitz_y1) <= max(3.0, orig_h * 0.6):
                        baseline = h - origin_y
                if baseline is not None:
                    # Та же базовая линия, что у исходной цифры, — значения в строке не «прыгают».
                    ty = baseline
                else:
                    text_cy = (orig_y0 + orig_y1) / 2.0
                    ty = text_cy - font_size * 0.35
                c.drawString(tx, ty, text)

            c.save()
            packet.seek(0)
            overlay = PdfReader(packet)
            page.merge_page(overlay.pages[0])
            writer.add_page(page)

        tmp = target_pdf_path.with_suffix(".cell_edit_tmp.pdf")
        with tmp.open("wb") as fh:
            writer.write(fh)
        tmp.replace(target_pdf_path)
    finally:
        if stripped_pdf is not None:
            stripped_pdf.unlink(missing_ok=True)
        if fitz_doc is not None:
            try:
                fitz_doc.close()
            except Exception:
                pass
