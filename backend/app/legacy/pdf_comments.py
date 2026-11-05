import io
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any


def page_content_bottom_norm(pdf_path: Path) -> dict[int, float]:
    """Низ содержимого страницы (таблица) как доля высоты, 0 — верх листа.

    Ключ — номер страницы с 1. Пустые страницы в словарь не попадают.
    """
    try:
        import pymupdf
    except Exception:
        return {}
    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception:
        return {}
    out: dict[int, float] = {}
    try:
        for index, page in enumerate(doc, start=1):
            height = float(page.rect.height) or 1.0
            width = float(page.rect.width) or 1.0
            bottom = 0.0
            for block in page.get_text("blocks") or []:
                if len(block) < 4:
                    continue
                bottom = max(bottom, float(block[3]))
            for drawing in page.get_drawings() or []:
                rect = drawing.get("rect")
                if rect is None:
                    continue
                if float(rect.width) >= width * 0.35 and float(rect.height) <= 2.5:
                    bottom = max(bottom, float(rect.y1))
            if bottom > 0:
                out[index] = min(0.98, bottom / height)
    finally:
        doc.close()
    return out


def relocate_comments_below_table(pdf_path: Path, comments: list[dict[str, Any]]) -> int:
    """Сдвинуть приписку под таблицу, если старая точка теперь внутри строк.

    Положение, которое и так ниже таблицы, не трогаем: его задал пользователь.
    Возвращает число сдвинутых приписок.
    """
    bottoms = page_content_bottom_norm(pdf_path)
    if not bottoms:
        return 0
    gap = 0.018
    moved = 0
    for row in comments:
        if not isinstance(row, dict):
            continue
        try:
            page = int(row.get("page", 1))
        except (TypeError, ValueError):
            page = 1
        bottom = bottoms.get(page)
        if bottom is None:
            continue
        raw = row.get("comment_anchor_y")
        if raw is None or raw == "":
            raw = row.get("y", 0.0)
        try:
            anchor = float(raw)
        except (TypeError, ValueError):
            anchor = 0.0
        anchor = max(0.0, min(1.0, anchor))
        # Белая полоса рисуется ниже якоря. Якорь внутри таблицы разрезает строки.
        if anchor >= bottom + 0.008:
            continue
        row["comment_anchor_y"] = round(min(0.985, bottom + gap), 4)
        moved += 1
    return moved


# Редактор ячеек считает координаты при масштабе 1.2 и кладёт их в cell_key.
_CELL_KEY_SCALE = 1.2
# Звёздочка, уехавшая внутри своей ячейки, всё ещё левее следующей цифры.
_MAX_STAR_GAP_PT = 55.0


def _norm_comment_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\u00a0", " ").split())


def _span_star_anchor(span: dict[str, Any]) -> tuple[float, float, float, float, float, float] | None:
    """Правый край цифры и точка звёздочки.

    Точка та же, что в редакторе: 96% ширины фрагмента и 52% кегля от базовой
    линии вверх (рамка pdf.js, не чёрная краска).
    """
    bbox = span.get("bbox") or (0, 0, 0, 0)
    origin = span.get("origin") or (bbox[0], bbox[3])
    try:
        x0 = float(bbox[0])
        y0 = float(bbox[1])
        x1 = float(bbox[2])
        y1 = float(bbox[3])
        baseline = float(origin[1])
        size = float(span.get("size") or 0.0)
    except (TypeError, ValueError):
        return None
    width = x1 - x0
    if width <= 0.2 or width > 90.0 or size < 1.0:
        return None
    return (x0, y0, x1, y1, x0 + 0.96 * width, baseline - 0.48 * size)


def _cell_key_pdf_point(cell_key: Any) -> tuple[float, float] | None:
    parts = str(cell_key or "").split(":", 3)
    if len(parts) < 3:
        return None
    try:
        left = float(parts[1]) / _CELL_KEY_SCALE
        top = float(parts[2]) / _CELL_KEY_SCALE
    except (TypeError, ValueError):
        return None
    if left < 0 or top < 0:
        return None
    return (left, top)


def _pick_span_for_star(
    spans: list[tuple[str, float, float, float, float, float, float, bool]],
    row: dict[str, Any],
    *,
    width: float,
    height: float,
) -> tuple[str, float, float] | None:
    """Фрагмент, к которому относится звёздочка: (текст, anchor_x, anchor_y)."""
    try:
        stored_x = float(row.get("x", 0.0)) * width
        stored_y = float(row.get("y", 0.0)) * height
    except (TypeError, ValueError):
        stored_x, stored_y = 0.0, 0.0
    wanted = _norm_comment_text(row.get("cell_text"))
    wanted_digit = any(ch.isdigit() for ch in wanted)

    def near_saved(anchor_x: float, anchor_y: float, limit: float) -> bool:
        return (anchor_x - stored_x) ** 2 + (anchor_y - stored_y) ** 2 <= limit * limit

    text_hits = [span for span in spans if wanted and span[0] == wanted and near_saved(span[5], span[6], 80.0)]
    if text_hits:
        chosen = min(text_hits, key=lambda span: (span[5] - stored_x) ** 2 + (span[6] - stored_y) ** 2)
        return (chosen[0], chosen[5], chosen[6])

    point = _cell_key_pdf_point(row.get("cell_key"))
    if point is not None:
        px, py = point
        inside = []
        for span in spans:
            norm, x0, y0, x1, y1, ax, ay, has_digit = span
            if wanted_digit and not has_digit:
                continue
            if (x0 - 4.0) <= px <= (x1 + 10.0) and (y0 - 8.0) <= py <= (y1 + 6.0):
                inside.append(span)
        if inside:
            chosen = min(inside, key=lambda span: (span[1] - px) ** 2 + (span[2] - py) ** 2)
            return (chosen[0], chosen[5], chosen[6])

    # Старая точка уже правее цифры. Берём ближайший левый край той же строки.
    left_of_star = []
    for span in spans:
        norm, _x0, _y0, _x1, _y1, ax, ay, has_digit = span
        if wanted_digit and not has_digit:
            continue
        gap = stored_x - ax
        if gap < -2.0 or gap > _MAX_STAR_GAP_PT:
            continue
        if abs(ay - stored_y) > 14.0:
            continue
        left_of_star.append((gap, span))
    if not left_of_star:
        return None
    chosen = min(left_of_star, key=lambda item: (item[0], abs(item[1][6] - stored_y)))[1]
    return (chosen[0], chosen[5], chosen[6])


def snap_comment_stars(pdf_path: Path, comments: list[dict[str, Any]]) -> int:
    """Подтянуть звёздочку к концу цифры в её ячейке.

    Точка хранится с того дня, когда комментарий поставили. Число с тех пор
    меняется (56 367 → 56 372), а точка остаётся на старом месте и отстаёт
    от цифры. Сначала ищем тот же текст, потом ячейку по координатам из
    cell_key, и только затем цифру слева от самой звёздочки.
    """
    if not pdf_path.is_file() or not comments:
        return 0
    try:
        import pymupdf
    except Exception:
        return 0
    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception:
        return 0
    moved = 0
    try:
        by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in comments:
            if not isinstance(row, dict):
                continue
            try:
                page_no = int(row.get("page", 1))
            except (TypeError, ValueError):
                page_no = 1
            if page_no < 1:
                page_no = 1
            by_page[page_no].append(row)

        for page_no, rows in by_page.items():
            if page_no > doc.page_count:
                continue
            page = doc[page_no - 1]
            width = float(page.rect.width) or 1.0
            height = float(page.rect.height) or 1.0
            spans: list[tuple[str, float, float, float, float, float, float, bool]] = []
            for block in page.get_text("dict").get("blocks") or []:
                if int(block.get("type", 0) or 0) != 0:
                    continue
                for line in block.get("lines") or []:
                    for span in line.get("spans") or []:
                        raw = str(span.get("text") or "")
                        if set(raw.strip()) <= {"*"}:
                            continue
                        norm = _norm_comment_text(raw)
                        if not norm:
                            continue
                        anchor = _span_star_anchor(span)
                        if anchor is None:
                            continue
                        x0, y0, x1, y1, ax, ay = anchor
                        spans.append((norm, x0, y0, x1, y1, ax, ay, any(ch.isdigit() for ch in norm)))
            if not spans:
                continue
            for row in rows:
                picked = _pick_span_for_star(spans, row, width=width, height=height)
                if picked is None:
                    continue
                text, ax, ay = picked
                try:
                    stored_x = float(row.get("x", 0.0)) * width
                    stored_y = float(row.get("y", 0.0)) * height
                except (TypeError, ValueError):
                    stored_x, stored_y = ax + 99.0, ay + 99.0
                same_text = _norm_comment_text(row.get("cell_text")) == text
                if same_text and (ax - stored_x) ** 2 + (ay - stored_y) ** 2 < 0.36:
                    continue
                row["x"] = round(max(0.0, min(1.0, ax / width)), 6)
                row["y"] = round(max(0.0, min(1.0, ay / height)), 6)
                if text:
                    row["cell_text"] = text
                moved += 1
    finally:
        doc.close()
    return moved


def _wrap_lines(text: str, *, max_width: float, font_name: str, font_size: float, canvas_obj: Any) -> list[str]:
    words = str(text or "").split()
    if not words:
        return [""]
    out: list[str] = []
    cur = words[0]
    for word in words[1:]:
        probe = f"{cur} {word}"
        if canvas_obj.stringWidth(probe, font_name, font_size) <= max_width:
            cur = probe
        else:
            out.append(cur)
            cur = word
    out.append(cur)
    return out


def apply_comments_to_pdf(
    *,
    target_pdf_path: Path,
    base_pdf_path: Path,
    comments: list[dict[str, Any]],
) -> None:
    if not base_pdf_path.exists():
        raise FileNotFoundError(f"Базовый PDF не найден: {base_pdf_path}")
    if not comments:
        tmp_copy = target_pdf_path.with_suffix(".tmp.pdf")
        shutil.copy2(base_pdf_path, tmp_copy)
        tmp_copy.replace(target_pdf_path)
        return

    # Точка в JSON могла отойти от цифры. Печатаем по текущему тексту страницы.
    snap_comment_stars(base_pdf_path, comments)

    try:
        from pypdf import PdfReader, PdfWriter
    except Exception:
        # Совместимость со старыми окружениями, где пакет называется PyPDF2.
        from PyPDF2 import PdfReader, PdfWriter
    from reportlab.lib.colors import HexColor
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    star_font = "Helvetica-Bold"
    text_font = "Helvetica"
    font_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/calibri.ttf",
    ]
    for font_path in font_candidates:
        p = Path(font_path)
        if not p.exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont("SvodkiCyrRegular", str(p)))
            text_font = "SvodkiCyrRegular"
            break
        except Exception:
            continue

    page_map: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in comments:
        try:
            page = int(row.get("page", 1))
        except Exception:
            page = 1
        if page < 1:
            page = 1
        page_map[page].append(row)

    reader = PdfReader(str(base_pdf_path))
    writer = PdfWriter()

    for idx, page in enumerate(reader.pages, start=1):
        rows = page_map.get(idx, [])
        if not rows:
            writer.add_page(page)
            continue
        w = float(page.mediabox.width)
        h = float(page.mediabox.height)
        packet = io.BytesIO()
        c = canvas.Canvas(packet, pagesize=(w, h))

        rows_sorted = sorted(
            rows,
            key=lambda r: (
                float(r.get("y", 0.0)),
                float(r.get("x", 0.0)),
                str(r.get("cell_key", "")),
            ),
        )
        row_marks: dict[str, str] = {}
        for i, row in enumerate(rows_sorted, start=1):
            mark = "*" * i
            row_id = str(row.get("id", "")) or str(row.get("cell_key", ""))
            row_marks[row_id] = mark

        # Звёздочки рядом с выбранными ячейками.
        c.setFont(star_font, 11)
        c.setFillColor(HexColor("#B91C1C"))
        for row in rows_sorted:
            x = float(row.get("x", 0.0))
            y = float(row.get("y", 0.0))
            x = max(0.0, min(1.0, x))
            y = max(0.0, min(1.0, y))
            px = x * w
            py = h - (y * h)
            row_id = str(row.get("id", "")) or str(row.get("cell_key", ""))
            mark = row_marks.get(row_id, "*")
            sx = min(w - 10.0, px + 1.0)
            sy = max(8.0, py - 2.0)
            c.drawString(sx, sy, mark)

        # Блок комментариев сразу под таблицей (под самой нижней выбранной ячейкой).
        margin_x = 18.0
        content_w = max(120.0, w - (margin_x * 2.0))
        line_h = 10.6
        comment_lines: list[str] = []
        c.setFont(text_font, 8.2)
        for row in rows_sorted:
            comment = str(row.get("comment", "")).strip()
            row_id = str(row.get("id", "")) or str(row.get("cell_key", ""))
            prefix = f"{row_marks.get(row_id, '*')} "
            line = f"{prefix}{comment}"
            wrapped = _wrap_lines(line, max_width=content_w, font_name=text_font, font_size=8.2, canvas_obj=c)
            comment_lines.extend(wrapped)

        if comment_lines:
            anchor_ys: list[float] = []
            for row in rows_sorted:
                y_raw = row.get("comment_anchor_y")
                if y_raw is None:
                    y_raw = row.get("y", 0.0)
                y = float(y_raw)
                y = max(0.0, min(1.0, y))
                anchor_ys.append(h - (y * h))
            lowest_anchor = min(anchor_ys) if anchor_ys else 60.0
            block_top = max(36.0, lowest_anchor - 8.0)
            desired_h = (len(comment_lines) * line_h) + 10.0
            block_bottom = max(8.0, block_top - desired_h)
            max_lines = max(1, int((block_top - block_bottom - 2.0) // line_h))

            c.setFillColor(HexColor("#FFFFFF"))
            c.rect(0, max(0.0, block_bottom - 2.0), w, block_top - block_bottom + 4.0, fill=1, stroke=0)
            c.setFillColor(HexColor("#111827"))
            y_cursor = block_top - 1.5
            for sub in comment_lines[:max_lines]:
                if y_cursor < block_bottom:
                    break
                c.drawString(margin_x, y_cursor, sub)
                y_cursor -= line_h

            if len(comment_lines) > max_lines and y_cursor >= block_bottom:
                c.drawString(margin_x, y_cursor, "...")

        c.save()
        packet.seek(0)
        overlay_page = PdfReader(packet).pages[0]
        page.merge_page(overlay_page)
        writer.add_page(page)

    tmp_pdf = target_pdf_path.with_suffix(".tmp.pdf")
    with tmp_pdf.open("wb") as fp:
        writer.write(fp)
    tmp_pdf.replace(target_pdf_path)

