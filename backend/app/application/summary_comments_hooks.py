"""Хуки комментариев после генерации сводок."""
from __future__ import annotations

from pathlib import Path

from app.application.summary_pdf_layers import (
    apply_future_comments_for_summary,
    rebuild_summary_pdf_layers,
)
from app.legacy.cell_edits_store import (
    list_summary_cell_edits,
    load_cell_edits_store,
    save_cell_edits_store,
)
from app.legacy.comments_store import (
    get_comment_pdf_base_path,
    load_comments_store,
    save_comments_store,
)
from app.legacy.report_generation import _report_id_from_summary_title
from app.models import Summary


def _drop_stale_cell_edit_base(summary_id: int) -> None:
    """Пустой список правок не должен подменять свежий PDF вчерашним снимком."""
    store = load_cell_edits_store()
    if list_summary_cell_edits(store, summary_id):
        return
    bucket = store.get(str(int(summary_id)))
    if not isinstance(bucket, dict):
        return
    if bucket.pop("cell_edit_pdf_base_path", None) is None:
        return
    save_cell_edits_store(store)


def apply_comments_after_generation(*, created: list[Summary], logs: list[str]) -> None:
    for item in created:
        rid = _report_id_from_summary_title(item.title)
        if not rid:
            continue
        pdf_path = Path(item.file_path).resolve()
        if not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
            continue
        try:
            # Строки уже есть с прошлой выгрузки — changed=False, и без явного
            # штампа на диске остаётся только звёздочка в редакторе, без текста.
            comments = apply_future_comments_for_summary(item, rebuild=False)
            if not comments:
                continue
            _drop_stale_cell_edit_base(int(item.id))
            store = load_comments_store()
            get_comment_pdf_base_path(
                store,
                int(item.id),
                pdf_path,
                report_id=rid,
                refresh=True,
            )
            save_comments_store(store)
            rebuild_summary_pdf_layers(item)
            logs.append(f"Сводка {rid}: применены комментарии ({len(comments)}).")
        except Exception as exc:
            logs.append(f"Сводка {rid}: ошибка комментариев — {exc}")
