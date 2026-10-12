"""После перевыгрузки сводки текст приписки снова попадает в PDF, а не только звёздочка."""
from __future__ import annotations

from types import SimpleNamespace

from pypdf import PdfReader
from reportlab.pdfgen import canvas

from app.config import settings
from app.application.summary_comments_hooks import apply_comments_after_generation
from app.application.summary_pdf_layers import rebuild_summary_pdf_layers
from app.legacy.cell_edits_store import load_cell_edits_store, save_cell_edits_store
from app.legacy.comments_store import load_comments_store, save_comments_store


def _write_pdf(path, label: str) -> None:
    c = canvas.Canvas(str(path), pagesize=(420, 300))
    c.drawString(48, 180, label)
    c.save()


def _text(path) -> str:
    return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)


def _summary(path):
    return SimpleNamespace(id=7, title="4. Боронование", file_path=str(path))


def test_regeneration_stamps_saved_comment_onto_fresh_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    fresh = tmp_path / "4. Боронование за 12.10.2026.pdf"
    stale = tmp_path / "stale.pdf"
    _write_pdf(fresh, "FRESH_REPORT")
    _write_pdf(stale, "STALE_BASE_MARKER")

    bases = tmp_path / "_comment_pdf_bases"
    bases.mkdir()
    stale_base = bases / "7.pdf"
    _write_pdf(stale_base, "STALE_BASE_MARKER")

    cell_bases = tmp_path / "_cell_edit_pdf_bases"
    cell_bases.mkdir()
    stale_cells = cell_bases / "7.pdf"
    _write_pdf(stale_cells, "STALE_CELL_BASE")
    save_cell_edits_store(
        {
            "7": {
                "report_id": "4",
                "base_pdf_path": str(fresh),
                "edits": [],
                "cell_edit_pdf_base_path": str(stale_cells),
            }
        }
    )

    row = {
        "id": "c1",
        "page": 1,
        "x": 0.22,
        "y": 0.35,
        "comment_anchor_y": 0.82,
        "cell_key": "1:10:20:12",
        "cell_text": "12",
        "comment": "Приписка про дождь",
        "apply_to_future": True,
        "starred": True,
    }
    save_comments_store(
        {
            "7": {
                "report_id": "4",
                "base_pdf_path": str(fresh),
                "comment_pdf_base_path": str(stale_base),
                "comments": [dict(row)],
            },
            "_future_comments": {"4": {"1:10:20:12": dict(row)}},
        }
    )

    logs: list[str] = []
    apply_comments_after_generation(created=[_summary(fresh)], logs=logs)

    text = _text(fresh)
    assert "FRESH_REPORT" in text
    assert "Приписка про дождь" in text
    assert "STALE_BASE_MARKER" not in text
    assert "STALE_CELL_BASE" not in text
    assert any("применены комментарии" in line for line in logs)

    # Повторная сборка из снимка не дублирует абзац.
    rebuild_summary_pdf_layers(_summary(fresh))
    again = _text(fresh)
    assert again.count("Приписка про дождь") == 1
    assert "FRESH_REPORT" in again

    stored = load_comments_store()
    assert stored["7"]["comments"][0]["comment"] == "Приписка про дождь"
    assert load_cell_edits_store()["7"].get("cell_edit_pdf_base_path") in (None, "")


def _write_table_pdf(path) -> None:
    c = canvas.Canvas(str(path), pagesize=(420, 400))
    c.drawString(40, 360, "HEADER")
    for i, y in enumerate(range(320, 100, -16)):
        c.drawString(40, y, f"row {i} value")
        c.line(30, y - 4, 390, y - 4)
    c.save()


def test_regeneration_moves_comment_below_grown_table(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    fresh = tmp_path / "4. Боронование за 12.10.2026.pdf"
    _write_table_pdf(fresh)
    # 0.40 — внутри новой, более длинной таблицы. Раньше белая полоса резала строки.
    row = {
        "id": "c1",
        "page": 1,
        "x": 0.5,
        "y": 0.3,
        "comment_anchor_y": 0.40,
        "cell_key": "1:10:20:12",
        "cell_text": "12",
        "comment": "Приписка про дождь",
        "apply_to_future": True,
        "starred": True,
    }
    save_comments_store(
        {
            "8": {
                "report_id": "4",
                "base_pdf_path": str(fresh),
                "comments": [dict(row)],
            }
        }
    )
    item = SimpleNamespace(id=8, title="4. Боронование", file_path=str(fresh))
    logs: list[str] = []
    apply_comments_after_generation(created=[item], logs=logs)

    stored = load_comments_store()
    anchor = float(stored["8"]["comments"][0]["comment_anchor_y"])
    assert anchor > 0.72
    text = _text(fresh)
    assert "row 0 value" in text
    assert "Приписка про дождь" in text
    assert any("сдвинуты под таблицу" in line for line in logs)
