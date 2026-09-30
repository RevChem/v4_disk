"""Редактор порядка страниц PDF-сводок."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.adapters.inbound.api.deps import (
    get_client_ip,
    require_generate_access_flexible,
    require_page_order_access,
)
from app.application.audit_activity import record_activity
from app.application.page_layer_remap import build_working_page_remap
from app.application.summary_pdf_layers import (
    pristine_pdf_for_summary,
    reapply_layers_after_page_order,
    summary_has_pdf_layers,
)
from app.application.summary_registry import report_id_from_title
from app.database import get_db
from app.infrastructure.logging import get_logger
from app.legacy.report1_page_order_editor import (
    apply_page_order_permutation,
    apply_saved_page_order_to_pdf,
    get_editor_state as get_report1_page_order_state,
    normalize_report_id as normalize_page_order_report_id,
    page_order_base_path,
    reset_page_order,
    restore_page_order_base_pdf,
    save_page_order,
)
from app.legacy.report_generation import _latest_pdf_paths_by_report_id, _report_id_from_summary_title
from app.models import Summary, User
from app.schemas import Report1PageOrderOut, Report1PageOrderPreviewIn, Report1PageOrderUpdateIn

router = APIRouter(tags=["page-order"])
log = get_logger(__name__)


def _require_visible_summary(item: Summary | None) -> Summary:
    if item is None or not item.visible:
        raise HTTPException(status_code=404, detail="Сводка не найдена.")
    return item


def _page_order_report_id_or_400(report_id: str) -> str:
    try:
        return normalize_page_order_report_id(report_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/page-order/{report_id}", response_model=Report1PageOrderOut)
def get_page_order_editor(report_id: str, _: User = Depends(require_page_order_access)):
    rid = _page_order_report_id_or_400(report_id)
    return get_report1_page_order_state(rid)


@router.put("/page-order/{report_id}", response_model=Report1PageOrderOut)
def update_page_order_editor(
    report_id: str,
    payload: Report1PageOrderUpdateIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_page_order_access),
):
    rid = _page_order_report_id_or_400(report_id)
    old_state = get_report1_page_order_state(rid)
    try:
        state = save_page_order(
            list(payload.page_slots),
            report_id=rid,
            reference_summary_id=payload.reference_summary_id,
            n_pages=payload.page_count,
            hidden_pages=list(payload.hidden_pages),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    applied = False
    apply_error = ""
    layers_note: str | None = None
    if payload.reference_summary_id:
        item = db.get(Summary, payload.reference_summary_id)
        if item is not None and item.visible is not False:
            path = Path(item.file_path)
            if path.is_file() and path.suffix.lower() == ".pdf":
                donor_pdfs = (
                    _latest_pdf_paths_by_report_id(db, item.summary_date)
                    if item.summary_date is not None
                    else {}
                )
                logs: list[str] = []
                try:
                    if state.get("has_custom_order"):
                        applied = apply_saved_page_order_to_pdf(
                            path,
                            logs,
                            report_id=rid,
                            report_label=rid,
                            donor_pdfs=donor_pdfs,
                            summary_id=int(item.id),
                            refresh_base=False,
                            # если на PDF уже нарисованы комментарии/правки — база порядка
                            # берётся из «чистого» снимка, а не из рабочего файла
                            base_seed=pristine_pdf_for_summary(item)
                            if summary_has_pdf_layers(item)
                            else None,
                        )
                        if not applied and logs:
                            apply_error = logs[-1]
                    else:
                        applied = restore_page_order_base_pdf(rid, int(item.id), path)
                    if applied:
                        # порядок страниц — нижний слой: переносим привязки и накладываем слои
                        n_source = (
                            payload.page_count
                            or old_state.get("source_page_count")
                            or state.get("source_page_count")
                        )
                        if not n_source:
                            try:
                                from pypdf import PdfReader

                                seed = pristine_pdf_for_summary(item)
                                n_source = len(PdfReader(str(seed)).pages)
                            except Exception:
                                n_source = None
                        old_slots = old_state.get("page_slots") if old_state.get("has_custom_order") else None
                        new_slots = state.get("page_slots") if state.get("has_custom_order") else None
                        page_remap = build_working_page_remap(
                            old_slots,
                            new_slots,
                            n_source=int(n_source) if n_source else None,
                        )
                        layers_note = reapply_layers_after_page_order(
                            item,
                            page_remap=page_remap,
                            report_id=rid,
                        )
                except Exception as exc:
                    apply_error = str(exc)
                    log.warning(
                        "page_order.apply_failed",
                        report_id=rid,
                        summary_id=item.id,
                        error=str(exc),
                    )

    graft_count = sum(1 for s in payload.page_slots if str(s.get("kind")) == "graft")
    record_activity(
        db,
        action="report1_page_order.update",
        user_id=user.id,
        ip=get_client_ip(request),
        report_id=rid,
        page_count=len(payload.page_slots),
        hidden_count=len(payload.hidden_pages),
        graft_count=graft_count,
        reference_summary_id=payload.reference_summary_id,
        applied_now=applied,
        apply_error=apply_error or None,
    )
    if isinstance(state, dict):
        state = {
            **state,
            "applied_now": applied,
            "apply_error": apply_error or None,
            "layers_note": layers_note,
        }
    return state


@router.post("/page-order/{report_id}/reset", response_model=Report1PageOrderOut)
def reset_page_order_editor(
    report_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_page_order_access),
):
    rid = _page_order_report_id_or_400(report_id)
    state_before = get_report1_page_order_state(rid)
    ref_id = state_before.get("reference_summary_id")
    state = reset_page_order(rid)
    apply_error = ""
    layers_note: str | None = None
    if ref_id:
        item = db.get(Summary, int(ref_id))
        if item is not None and Path(item.file_path).is_file():
            try:
                if restore_page_order_base_pdf(rid, int(item.id), Path(item.file_path)):
                    n_source = state_before.get("source_page_count")
                    old_slots = (
                        state_before.get("page_slots") if state_before.get("has_custom_order") else None
                    )
                    page_remap = build_working_page_remap(
                        old_slots,
                        None,
                        n_source=int(n_source) if n_source else None,
                    )
                    layers_note = reapply_layers_after_page_order(
                        item,
                        page_remap=page_remap,
                        report_id=rid,
                    )
            except Exception as exc:
                apply_error = str(exc)
                log.warning(
                    "page_order.reset_restore_failed",
                    report_id=rid,
                    summary_id=ref_id,
                    exc_info=True,
                )
    record_activity(
        db,
        action="report1_page_order.reset",
        user_id=user.id,
        ip=get_client_ip(request),
        report_id=rid,
        reference_summary_id=ref_id,
    )
    if isinstance(state, dict):
        state = {**state, "apply_error": apply_error or None, "layers_note": layers_note}
    return state


@router.post("/page-order/{report_id}/preview")
def preview_page_order_pdf(
    report_id: str,
    payload: Report1PageOrderPreviewIn,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    _: User = Depends(require_page_order_access),
):
    rid = _page_order_report_id_or_400(report_id)
    item = db.get(Summary, payload.summary_id)
    item = _require_visible_summary(item)
    path = Path(item.file_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found on disk.")
    if path.suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="Предпросмотр доступен только для PDF.")

    fd, tmp_name = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        summary_rid = _report_id_from_summary_title(item.title) or ""
        if summary_rid and summary_rid != rid:
            raise HTTPException(
                status_code=400,
                detail=f"Сводка id={payload.summary_id} относится к {summary_rid}, а не к {rid}.",
            )
        donor_pdfs = (
            _latest_pdf_paths_by_report_id(db, item.summary_date)
            if item.summary_date is not None
            else {}
        )
        base = page_order_base_path(rid, int(item.id))
        src = base if base.is_file() else path
        apply_page_order_permutation(
            src,
            [],
            dest_pdf=tmp_path,
            hidden_pages=list(payload.hidden_pages),
            page_slots=list(payload.page_slots),
            donor_pdfs=donor_pdfs,
            report_label=rid,
        )
    except HTTPException:
        tmp_path.unlink(missing_ok=True)
        raise
    except ValueError as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Не удалось собрать предпросмотр: {exc}") from exc
    date_part = item.summary_date.isoformat() if item.summary_date else "preview"
    safe_rid = rid.replace(".", "_")
    filename = f"svodka{safe_rid}_preview_{date_part}.pdf"
    background.add_task(lambda: tmp_path.unlink(missing_ok=True))
    return FileResponse(
        path=tmp_path,
        media_type="application/pdf",
        filename=filename,
        content_disposition_type="attachment",
    )


@router.get("/page-order/{report_id}/source-pdf")
def download_page_order_source_pdf(
    report_id: str,
    summary_id: int = Query(..., ge=1),
    db: Session = Depends(get_db),
    _: User = Depends(require_generate_access_flexible),
):
    rid = _page_order_report_id_or_400(report_id)
    item = db.get(Summary, summary_id)
    item = _require_visible_summary(item)
    summary_rid = _report_id_from_summary_title(item.title) or report_id_from_title(item.title)
    if summary_rid and summary_rid != rid:
        raise HTTPException(
            status_code=400,
            detail=f"Сводка id={summary_id} относится к {summary_rid}, а не к {rid}.",
        )
    path = Path(item.file_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found on disk.")
    base = page_order_base_path(rid, int(item.id))
    src = base if base.is_file() else path
    date_part = item.summary_date.isoformat() if item.summary_date else "source"
    safe_rid = rid.replace(".", "_")
    filename = f"svodka{safe_rid}_source_{date_part}.pdf"
    return FileResponse(src, media_type="application/pdf", filename=filename)
