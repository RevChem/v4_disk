"""Правки ячеек PDF-сводок."""
from __future__ import annotations

import functools
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.adapters.inbound.api.deps import get_client_ip, require_cell_edit_access, require_comment_access
from app.application.audit_activity import record_activity
from app.application.summary_pdf_layers import apply_future_comments_for_summary, rebuild_summary_pdf_layers
from app.application.summary_registry import report_id_from_title
from app.database import get_db
from app.infrastructure.logging import get_logger
from app.legacy.json_store_io import store_rmw_lock
from app.legacy.cell_edits_store import (
    cell_edits_store_path,
    delete_summary_cell_edit,
    list_summary_cell_edits,
    load_cell_edits_store,
    save_cell_edits_store,
    upsert_summary_cell_edit,
)
from app.legacy.comments_store import (
    comments_store_path,
    delete_future_comment,
    delete_summary_comment,
    list_summary_comments,
    load_comments_store,
    save_comments_store,
    upsert_future_comment,
    upsert_summary_comment,
)
from app.models import Summary, User
from app.schemas import (
    SummaryCellEditUpsertIn,
    SummaryCellEditsBatchIn,
    SummaryCellEditsOut,
    SummaryCommentFutureToggleIn,
    SummaryCommentsOut,
    SummaryCommentUpsertIn,
)
from fastapi import Request

router = APIRouter(prefix="/summaries", tags=["summaries"])


def _serialized(fn):
    """Правки ячеек/комментариев — read-modify-write общего JSON: без сериализации параллельные
    запросы теряют изменения друг друга."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with store_rmw_lock("summaries"):
            return fn(*args, **kwargs)

    return wrapper
log = get_logger(__name__)


def _require_visible_summary(item: Summary | None) -> Summary:
    if item is None or not item.visible:
        raise HTTPException(status_code=404, detail="Сводка не найдена.")
    return item


def _cell_edits_response(db: Session, summary_id: int) -> dict:
    item = _require_visible_summary(db.get(Summary, summary_id))
    report_id = report_id_from_title(item.title)
    store = load_cell_edits_store()
    return {
        "summary_id": summary_id,
        "editable": True,
        "report_id": report_id,
        "edits": list_summary_cell_edits(store, summary_id),
        "store_path": str(cell_edits_store_path()),
    }


@router.get("/{summary_id}/cell-edits", response_model=SummaryCellEditsOut)
def get_summary_cell_edits(
    summary_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_cell_edit_access),
):
    return _cell_edits_response(db, summary_id)


@router.put("/{summary_id}/cell-edits", response_model=SummaryCellEditsOut)
@_serialized
def save_summary_cell_edit(
    summary_id: int,
    payload: SummaryCellEditUpsertIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_cell_edit_access),
):
    item = _require_visible_summary(db.get(Summary, summary_id))
    if Path(item.file_path).suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="Правки ячеек доступны только для PDF.")
    report_id = report_id_from_title(item.title)
    store = load_cell_edits_store()
    try:
        edits = upsert_summary_cell_edit(
            store,
            summary_id,
            report_id=report_id,
            base_pdf_path=item.file_path,
            page=payload.page,
            left=payload.left,
            top=payload.top,
            width=payload.width,
            height=payload.height,
            cell_key=payload.cell_key,
            cell_text=payload.cell_text,
            replacement=payload.replacement,
            fill_color=payload.fill_color,
            text_color=payload.text_color,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    save_cell_edits_store(store)
    try:
        rebuild_summary_pdf_layers(item)
    except Exception:
        log.warning("cell_edit.apply_failed", summary_id=summary_id, exc_info=True)
        raise HTTPException(status_code=500, detail="Не удалось применить правку к PDF.") from None
    target = next((row for row in edits if str(row.get("cell_key")) == str(payload.cell_key)), None)
    record_activity(
        db,
        action="summary.cell_edit.upsert",
        user_id=user.id,
        ip=get_client_ip(request),
        summary_id=summary_id,
        report_id=report_id,
        summary_date=item.summary_date.isoformat(),
        cell_key=payload.cell_key,
        old_text=payload.cell_text,
        new_text=payload.replacement,
        edit_id=(target or {}).get("id"),
    )
    return _cell_edits_response(db, summary_id)


@router.post("/{summary_id}/cell-edits/batch", response_model=SummaryCellEditsOut)
@_serialized
def save_summary_cell_edits_batch(
    summary_id: int,
    payload: SummaryCellEditsBatchIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_cell_edit_access),
):
    """Применить пачку правок (добавление/изменение и удаление) одним запросом.

    Хранилище читается и пишется один раз, PDF пересобирается один раз. Операция атомарна по
    данным: при ошибке в любой правке (например, пустая замена) ничего не сохраняется.
    """
    item = _require_visible_summary(db.get(Summary, summary_id))
    if Path(item.file_path).suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="Правки ячеек доступны только для PDF.")
    if not payload.edits and not payload.delete_ids:
        raise HTTPException(status_code=400, detail="Нет изменений для сохранения.")
    report_id = report_id_from_title(item.title)
    store = load_cell_edits_store()

    deleted_ids: list[str] = []
    for edit_id in dict.fromkeys(str(x) for x in payload.delete_ids):
        _, deleted = delete_summary_cell_edit(store, summary_id, edit_id)
        if deleted:
            deleted_ids.append(edit_id)

    applied: list[tuple[SummaryCellEditUpsertIn, str | None]] = []
    edits: list[dict] = list_summary_cell_edits(store, summary_id)
    for index, row in enumerate(payload.edits, start=1):
        # Повторная правка приходит с ключом уже заменённого текста. Хранилище оставляет
        # исходный ключ, поэтому id ищем по строке, которая реально изменилась.
        before = {
            str(item_row.get("id")): (
                str(item_row.get("replacement") or ""),
                str(item_row.get("updated_at") or ""),
            )
            for item_row in edits
        }
        try:
            edits = upsert_summary_cell_edit(
                store,
                summary_id,
                report_id=report_id,
                base_pdf_path=item.file_path,
                page=row.page,
                left=row.left,
                top=row.top,
                width=row.width,
                height=row.height,
                cell_key=row.cell_key,
                cell_text=row.cell_text,
                replacement=row.replacement,
                fill_color=row.fill_color,
                text_color=row.text_color,
            )
        except ValueError as exc:
            # Хранилище ещё не сохранено — частично применённый пакет не попадёт на диск.
            raise HTTPException(status_code=400, detail=f"Правка №{index}: {exc}") from exc
        changed = next(
            (
                item_row
                for item_row in edits
                if before.get(str(item_row.get("id")))
                != (str(item_row.get("replacement") or ""), str(item_row.get("updated_at") or ""))
            ),
            None,
        )
        applied.append((row, None if changed is None else changed.get("id")))

    save_cell_edits_store(store)
    try:
        rebuild_summary_pdf_layers(item)
    except Exception:
        log.warning("cell_edit.batch_apply_failed", summary_id=summary_id, exc_info=True)
        raise HTTPException(status_code=500, detail="Не удалось применить правки к PDF.") from None

    for row, edit_id in applied:
        record_activity(
            db,
            action="summary.cell_edit.upsert",
            user_id=user.id,
            ip=get_client_ip(request),
            summary_id=summary_id,
            report_id=report_id,
            summary_date=item.summary_date.isoformat(),
            cell_key=row.cell_key,
            old_text=row.cell_text,
            new_text=row.replacement,
            edit_id=edit_id,
        )
    for edit_id in deleted_ids:
        record_activity(
            db,
            action="summary.cell_edit.delete",
            user_id=user.id,
            ip=get_client_ip(request),
            summary_id=summary_id,
            report_id=report_id,
            summary_date=item.summary_date.isoformat(),
            edit_id=edit_id,
        )
    return _cell_edits_response(db, summary_id)


@router.delete("/{summary_id}/cell-edits/{edit_id}", response_model=SummaryCellEditsOut)
@_serialized
def delete_summary_cell_edit_route(
    summary_id: int,
    edit_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_cell_edit_access),
):
    item = _require_visible_summary(db.get(Summary, summary_id))
    store = load_cell_edits_store()
    edits, deleted = delete_summary_cell_edit(store, summary_id, edit_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Правка не найдена.")
    save_cell_edits_store(store)
    try:
        rebuild_summary_pdf_layers(item)
    except Exception:
        log.warning("cell_edit.delete_apply_failed", summary_id=summary_id, exc_info=True)
    record_activity(
        db,
        action="summary.cell_edit.delete",
        user_id=user.id,
        ip=get_client_ip(request),
        summary_id=summary_id,
        report_id=report_id_from_title(item.title),
        summary_date=item.summary_date.isoformat(),
        edit_id=edit_id,
    )
    return _cell_edits_response(db, summary_id)


def _comments_response(db: Session, summary_id: int, apply_error: str | None = None) -> dict:
    item = _require_visible_summary(db.get(Summary, summary_id))
    report_id = report_id_from_title(item.title)
    store = load_comments_store()
    return {
        "summary_id": summary_id,
        "editable": True,
        "report_id": report_id,
        "comments": list_summary_comments(store, summary_id),
        "store_path": str(comments_store_path()),
        "apply_error": apply_error,
    }


@router.get("/{summary_id}/comments", response_model=SummaryCommentsOut)
def get_summary_comments(
    summary_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_comment_access),
):
    item = _require_visible_summary(db.get(Summary, summary_id))
    try:
        apply_future_comments_for_summary(item)
    except Exception:
        log.warning("comment.future_apply_failed", summary_id=summary_id, exc_info=True)
    return _comments_response(db, summary_id)


@router.put("/{summary_id}/comments", response_model=SummaryCommentsOut)
@_serialized
def save_summary_comment(
    summary_id: int,
    payload: SummaryCommentUpsertIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_comment_access),
):
    item = _require_visible_summary(db.get(Summary, summary_id))
    if Path(item.file_path).suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="Комментарии доступны только для PDF.")
    report_id = report_id_from_title(item.title)
    store = load_comments_store()
    comments = upsert_summary_comment(
        store,
        summary_id,
        report_id=report_id,
        base_pdf_path=item.file_path,
        page=payload.page,
        x=payload.x,
        y=payload.y,
        comment_anchor_y=payload.comment_anchor_y,
        cell_key=payload.cell_key,
        cell_text=payload.cell_text,
        comment=payload.comment,
    )
    target_row = next((row for row in comments if str(row.get("cell_key")) == str(payload.cell_key)), None)
    if target_row is not None:
        target_row["apply_to_future"] = bool(payload.apply_to_future)
        if payload.apply_to_future:
            upsert_future_comment(store, report_id, target_row)
        else:
            delete_future_comment(store, report_id, payload.cell_key)
    save_comments_store(store)
    apply_error: str | None = None
    try:
        rebuild_summary_pdf_layers(item)
    except Exception as exc:
        apply_error = str(exc) or exc.__class__.__name__
        log.warning("comment.apply_failed", summary_id=summary_id, exc_info=True)
    record_activity(
        db,
        action="summary.comment.upsert",
        user_id=user.id,
        ip=get_client_ip(request),
        summary_id=summary_id,
        report_id=report_id,
        summary_date=item.summary_date.isoformat(),
        cell_key=payload.cell_key,
        comment=payload.comment[:300],
        apply_to_future=payload.apply_to_future,
    )
    return _comments_response(db, summary_id, apply_error)


@router.put("/{summary_id}/comments/future", response_model=SummaryCommentsOut)
@_serialized
def toggle_summary_comment_future(
    summary_id: int,
    payload: SummaryCommentFutureToggleIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_comment_access),
):
    item = _require_visible_summary(db.get(Summary, summary_id))
    report_id = report_id_from_title(item.title)
    store = load_comments_store()
    comments = list_summary_comments(store, summary_id)
    found = False
    for row in comments:
        if str(row.get("cell_key")) == str(payload.cell_key):
            row["apply_to_future"] = bool(payload.enabled)
            if payload.enabled:
                upsert_future_comment(store, report_id, row)
            else:
                delete_future_comment(store, report_id, payload.cell_key)
            found = True
            break
    if not found:
        raise HTTPException(status_code=404, detail="Комментарий для выбранной ячейки не найден.")
    save_comments_store(store)
    record_activity(
        db,
        action="summary.comment.future.toggle",
        user_id=user.id,
        ip=get_client_ip(request),
        summary_id=summary_id,
        cell_key=payload.cell_key,
        enabled=payload.enabled,
    )
    return _comments_response(db, summary_id)


@router.delete("/{summary_id}")
@_serialized
def delete_summary_route(
    summary_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_comment_access),
):
    item = _require_visible_summary(db.get(Summary, summary_id))
    path = Path(item.file_path)
    item.visible = False
    db.commit()
    removed: list[str] = []
    for candidate in (path, path.with_suffix(".xlsx")):
        if candidate.is_file():
            try:
                candidate.unlink()
                removed.append(candidate.name)
            except OSError as exc:
                log.warning(
                    "summary.delete_file_failed",
                    summary_id=summary_id,
                    path=str(candidate),
                    error=str(exc),
                )
    record_activity(
        db,
        action="summary.delete",
        user_id=user.id,
        ip=get_client_ip(request),
        summary_id=summary_id,
        report_id=report_id_from_title(item.title),
        summary_date=item.summary_date.isoformat(),
        filename=path.name,
        removed=removed,
    )
    return {"ok": True, "summary_id": summary_id, "removed": removed}


@router.delete("/{summary_id}/comments/{comment_id}", response_model=SummaryCommentsOut)
@_serialized
def delete_summary_comment_route(
    summary_id: int,
    comment_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_comment_access),
):
    item = _require_visible_summary(db.get(Summary, summary_id))
    store = load_comments_store()
    _, deleted = delete_summary_comment(store, summary_id, comment_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Комментарий не найден.")
    save_comments_store(store)
    apply_error: str | None = None
    try:
        rebuild_summary_pdf_layers(item)
    except Exception as exc:
        apply_error = str(exc) or exc.__class__.__name__
        log.warning("comment.delete_apply_failed", summary_id=summary_id, exc_info=True)
    record_activity(
        db,
        action="summary.comment.delete",
        user_id=user.id,
        ip=get_client_ip(request),
        summary_id=summary_id,
        report_id=report_id_from_title(item.title),
        summary_date=item.summary_date.isoformat(),
        comment_id=comment_id,
    )
    return _comments_response(db, summary_id, apply_error)
