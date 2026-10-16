"""Цифра остаётся в своей ячейке, даже если справа цветная заливка соседнего столбца."""
from __future__ import annotations

from pathlib import Path

import fitz
from reportlab.lib.colors import Color, black
from reportlab.pdfgen import canvas

from app.legacy.pdf_cell_edits import apply_cell_edits_to_pdf


def _page_with_neighbor_fill(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=(842, 595))
    c.setFillColor(Color(1, 1, 1))
    c.rect(31, 595 - 184, 312, 58, stroke=0, fill=1)
    c.setFillColor(Color(0.988, 0.894, 0.925))
    c.rect(343, 595 - 184, 67, 58, stroke=0, fill=1)
    c.setFillColor(black)
    c.setFont("Helvetica", 6)
    c.drawString(300.3, 595 - 140, "737.70")
    c.save()


def test_replacement_stays_in_its_column_when_neighbor_is_colored(tmp_path):
    src = tmp_path / "base.pdf"
    _page_with_neighbor_fill(src)
    base = fitz.open(src)
    span = next(
        span
        for block in base[0].get_text("dict")["blocks"]
        if block.get("type") == 0
        for line in block["lines"]
        for span in line["spans"]
        if "737.70" in span["text"]
    )
    bb = span["bbox"]
    orig_cx = (bb[0] + bb[2]) / 2.0
    page = base[0]
    width, height = float(page.rect.width), float(page.rect.height)
    base.close()

    out = tmp_path / "edited.pdf"
    apply_cell_edits_to_pdf(
        target_pdf_path=out,
        base_pdf_path=src,
        edits=[
            {
                "page": 1,
                "left": bb[0] / width,
                "top": bb[1] / height,
                "width": (bb[2] - bb[0]) / width,
                "height": (bb[3] - bb[1]) / height,
                "cell_text": "737.70",
                "replacement": "738.70",
                "updated_at": "2026-01-01T00:00:00",
            }
        ],
    )

    edited = fitz.open(out)
    placed = next(
        span
        for block in edited[0].get_text("dict")["blocks"]
        if block.get("type") == 0
        for line in block["lines"]
        for span in line["spans"]
        if "738.70" in span["text"]
    )
    box = placed["bbox"]
    new_cx = (box[0] + box[2]) / 2.0
    assert abs(new_cx - orig_cx) < 1.5
    # Розовая ячейка начинается с x=343. Цифра не должна в неё переехать.
    assert box[2] < 340
