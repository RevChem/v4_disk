"""Post-hooks после disk-генерации: Summary в БД, порядок страниц, правки, комментарии."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from app.application.summary_comments_hooks import apply_comments_after_generation
from app.application.summary_registry import ensure_summary_for_file
from app.config import settings
from app.legacy.report_generation import (
    _apply_report1_saved_page_order,
    _apply_saved_cell_edits_after_generation,
)
from app.legacy.report_runner import GeneratedFile


def apply_post_generation_hooks(
    db: Session,
    *,
    summary_date: date,
    created: list[GeneratedFile],
    logs: list[str],
) -> None:
    if not created:
        return
    folder = Path(settings.upload_dir) / summary_date.isoformat()
    summaries = []
    for item in created:
        pdf_path = item.pdf_path
        if pdf_path is None or not pdf_path.is_file():
            continue
        filename = pdf_path.name
        summary = ensure_summary_for_file(
            db,
            summary_date=summary_date,
            filename=filename,
            title=item.title,
            file_path=pdf_path,
        )
        summaries.append(summary)

    if not summaries:
        return

    _apply_report1_saved_page_order(
        created=summaries,
        logs=logs,
        db=db,
        summary_date=summary_date,
    )
    _apply_saved_cell_edits_after_generation(created=summaries, logs=logs)
    apply_comments_after_generation(created=summaries, logs=logs)
