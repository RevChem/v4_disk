"""Хуки комментариев после генерации сводок."""
from __future__ import annotations

from pathlib import Path

from app.application.summary_pdf_layers import apply_future_comments_for_summary
from app.legacy.report_generation import _report_id_from_summary_title
from app.models import Summary


def apply_comments_after_generation(*, created: list[Summary], logs: list[str]) -> None:
    for item in created:
        rid = _report_id_from_summary_title(item.title)
        if not rid:
            continue
        pdf_path = Path(item.file_path).resolve()
        if not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
            continue
        try:
            comments = apply_future_comments_for_summary(item)
            if comments:
                logs.append(f"Сводка {rid}: применены комментарии ({len(comments)}).")
        except Exception as exc:
            logs.append(f"Сводка {rid}: ошибка комментариев — {exc}")
