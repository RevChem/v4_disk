"""Пересборка рабочего PDF: правки ячеек → комментарии."""
from __future__ import annotations

import shutil
from pathlib import Path

from app.application.summary_registry import report_id_from_title
from app.legacy.cell_edits_store import (
    get_cell_edit_pdf_base_path,
    list_summary_cell_edits,
    load_cell_edits_store,
    save_cell_edits_store,
)
from app.legacy.comments_store import (
    get_comment_pdf_base_path,
    list_summary_comments,
    load_comments_store,
    save_comments_store,
)
from app.legacy.pdf_cell_edits import apply_cell_edits_to_pdf
from app.legacy.pdf_comments import apply_comments_to_pdf
from app.models import Summary


def summary_has_pdf_layers(item: Summary) -> bool:
    """Есть ли у сводки комментарии или правки ячеек, нарисованные поверх PDF."""
    sid = int(item.id)
    return bool(
        list_summary_cell_edits(load_cell_edits_store(), sid)
        or list_summary_comments(load_comments_store(), sid)
    )


def pristine_pdf_for_summary(item: Summary) -> Path:
    """PDF без слоёв (комментарии, правки ячеек) — источник для смены порядка страниц.

    Порядок страниц — самый нижний слой: итог = комментарии(правки(порядок(оригинал))).
    Базовые снимки слоёв хранят PDF до наложения, поэтому берём самый «чистый».
    """
    sid = str(int(item.id))
    working = Path(item.file_path).resolve()
    for store, key in (
        (load_cell_edits_store(), "cell_edit_pdf_base_path"),
        (load_comments_store(), "comment_pdf_base_path"),
    ):
        bucket = store.get(sid)
        raw = bucket.get(key) if isinstance(bucket, dict) else None
        if isinstance(raw, str) and raw.strip():
            cand = Path(raw.strip()).resolve()
            if cand.is_file():
                return cand
    return working


def _drop_layer_bases(item: Summary) -> None:
    sid = str(int(item.id))
    edits_store = load_cell_edits_store()
    comments_store = load_comments_store()
    for store, key, saver in (
        (edits_store, "cell_edit_pdf_base_path", save_cell_edits_store),
        (comments_store, "comment_pdf_base_path", save_comments_store),
    ):
        bucket = store.get(sid)
        if not isinstance(bucket, dict):
            continue
        raw = bucket.pop(key, None)
        if isinstance(raw, str) and raw.strip():
            try:
                Path(raw.strip()).unlink(missing_ok=True)
            except OSError:
                pass
        saver(store)


def reapply_layers_after_page_order(
    item: Summary,
    *,
    page_remap: dict[int, int | None] | None = None,
    report_id: str | None = None,
) -> str | None:
    """Рабочий PDF только что заменён «чистой» версией (новый порядок страниц или сброс).

    Снимки слоёв устарели (другой порядок страниц) — сбрасываем их и заново накладываем
    правки ячеек и комментарии. Перед этим, если передан page_remap, переносим
    привязки со старых ordinal-страниц на новые (вместе с содержимым).

    Возвращает заметку для пользователя, если у сводки есть слои.
    """
    if not summary_has_pdf_layers(item) and not page_remap:
        return None

    stats = None
    if page_remap:
        from app.application.page_layer_remap import remap_is_identity, remap_summary_pdf_layer_pages

        if not remap_is_identity(page_remap):
            rid = report_id or report_id_from_title(item.title)
            stats = remap_summary_pdf_layer_pages(int(item.id), page_remap, report_id=rid)

    if stats and (
        stats.get("comments_moved")
        or stats.get("edits_moved")
        or stats.get("comments_dropped")
        or stats.get("edits_dropped")
    ):
        parts = []
        moved = int(stats.get("comments_moved") or 0) + int(stats.get("edits_moved") or 0)
        dropped = int(stats.get("comments_dropped") or 0) + int(stats.get("edits_dropped") or 0)
        if moved:
            parts.append(f"перенесено {moved}")
        if dropped:
            parts.append(f"снято со скрытых страниц {dropped}")
        detail = ", ".join(parts) if parts else "обновлены"
        note = f"Комментарии и правки ячеек {detail} вместе со страницами."
    else:
        note = "Комментарии и правки ячеек переложены на новый порядок страниц."

    if not summary_has_pdf_layers(item):
        _drop_layer_bases(item)
        return note if stats else None

    _drop_layer_bases(item)
    rebuild_summary_pdf_layers(item)
    return note


def _existing_base(store: dict, summary_id: int, key: str) -> Path | None:
    bucket = store.get(str(summary_id))
    raw = bucket.get(key) if isinstance(bucket, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    p = Path(raw.strip()).resolve()
    return p if p.is_file() else None


def _restore_pdf_without_layers(
    comments_store: dict, summary_id: int, working: Path, edits_store: dict | None = None
) -> None:
    """Все комментарии и правки ячеек удалены — вернуть PDF без нарисованных слоёв.

    Раньше функция просто выходила, и последний удалённый слой оставался в PDF.
    """
    base = _existing_base(edits_store or {}, summary_id, "cell_edit_pdf_base_path") or _existing_base(
        comments_store, summary_id, "comment_pdf_base_path"
    )
    if base is None:
        return
    try:
        from pypdf import PdfReader

        # Устаревший снимок (например, после смены порядка страниц) не подставляем.
        if len(PdfReader(str(base)).pages) != len(PdfReader(str(working)).pages):
            return
    except Exception:
        return
    tmp = working.with_suffix(".restore.tmp.pdf")
    shutil.copy2(base, tmp)
    tmp.replace(working)


def rebuild_summary_pdf_layers(item: Summary) -> None:
    """Собрать рабочий PDF: базовый → правки ячеек → комментарии."""
    report_id = report_id_from_title(item.title)
    working = Path(item.file_path).resolve()
    if not working.is_file():
        raise FileNotFoundError(f"PDF не найден: {working}")

    edits_store = load_cell_edits_store()
    comments_store = load_comments_store()
    edits = list_summary_cell_edits(edits_store, int(item.id))
    comments = list_summary_comments(comments_store, int(item.id))

    if not edits:
        if comments:
            base_pdf = get_comment_pdf_base_path(
                comments_store, int(item.id), working, report_id=report_id
            )
            # Снимок комментариев мог быть сделан поверх правок ячеек (mid). Правок больше нет —
            # берём чистый снимок, иначе удалённая правка остаётся в PDF.
            pristine = _existing_base(edits_store, int(item.id), "cell_edit_pdf_base_path")
            if pristine is not None and pristine != base_pdf:
                shutil.copy2(pristine, base_pdf)
            apply_comments_to_pdf(
                target_pdf_path=working,
                base_pdf_path=base_pdf,
                comments=comments,
            )
            save_comments_store(comments_store)
        else:
            _restore_pdf_without_layers(comments_store, int(item.id), working, edits_store)
        return

    preferred_base: Path | None = None
    comment_bucket = comments_store.get(str(int(item.id)))
    if isinstance(comment_bucket, dict):
        raw_comment_base = comment_bucket.get("comment_pdf_base_path")
        if isinstance(raw_comment_base, str) and raw_comment_base.strip():
            cand = Path(raw_comment_base.strip()).resolve()
            if cand.is_file():
                preferred_base = cand

    cell_base = get_cell_edit_pdf_base_path(
        edits_store,
        int(item.id),
        working,
        report_id=report_id,
        preferred_base=preferred_base,
        refresh=False,
    )
    mid = working.with_suffix(".layers_mid.tmp.pdf")
    try:
        apply_cell_edits_to_pdf(
            target_pdf_path=mid,
            base_pdf_path=cell_base,
            edits=edits,
        )
        if comments:
            comment_base = get_comment_pdf_base_path(
                comments_store, int(item.id), working, report_id=report_id
            )
            shutil.copy2(mid, comment_base)
            save_comments_store(comments_store)
            apply_comments_to_pdf(
                target_pdf_path=working,
                base_pdf_path=mid,
                comments=comments,
            )
            # Звёздочка могла притянуться к цифре — записать уже новую точку.
            save_comments_store(comments_store)
        else:
            shutil.copy2(mid, working)
    finally:
        mid.unlink(missing_ok=True)
    save_cell_edits_store(edits_store)


def apply_future_comments_for_summary(item: Summary, *, rebuild: bool = True) -> list[dict]:
    """Подтянуть шаблоны комментариев «на будущее».

    rebuild=False — только записать строки. После генерации PDF снимок базы ещё
    от прошлой сводки, его нельзя подмешивать здесь: штамп делает хук генерации.
    """
    from app.legacy.comments_store import apply_future_comments_to_summary

    report_id = report_id_from_title(item.title)
    store = load_comments_store()
    comments, changed = apply_future_comments_to_summary(
        store,
        int(item.id),
        report_id=report_id,
        base_pdf_path=item.file_path,
    )
    save_comments_store(store)
    if rebuild and changed:
        try:
            rebuild_summary_pdf_layers(item)
        except Exception:
            save_comments_store(store)
            raise
    return comments
