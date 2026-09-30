"""Reports API."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from app.legacy.report_generation import _parse_report27_period_header
from app.legacy.report_runner import list_job_completed_report_ids

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.adapters.inbound.api.deps import (
    get_client_ip,
    require_generate_access,
    require_generate_access_flexible,
)
from app.adapters.outbound.persistence import repositories as repo
from app.adapters.outbound.reports.service import (
    ReportGenerationError,
    list_available_dates,
    list_generated_files,
    list_report_catalog_for_ui,
)
from app.application.audit_activity import record_activity
from app.application.summary_registry import enrich_generated_files
from app.application.use_cases.generate_reports import cancel_generation_job, enqueue_or_run
from app.config import settings
from app.database import get_db
from app.models import GenerationJob, User
from app.schemas import CancelGenerationJobIn, GenerateReportsIn, GeneratedFileListOut, ReportCatalogOut
from app.infrastructure.logging import get_logger
from app.schemas_auth import JobStatusOut

router = APIRouter(prefix="/reports", tags=["reports"])
log = get_logger(__name__)


def _job_to_dict(job: GenerationJob, db: Session) -> dict:
    report_ids: list[str] = []
    try:
        report_ids = json.loads(job.report_ids or "[]")
    except json.JSONDecodeError:
        report_ids = []
    failed: list[dict] = []
    cancelled: list[dict] = []
    logs: list[str] = []
    if job.result_json:
        try:
            parsed = json.loads(job.result_json)
            failed = parsed.get("failed") or []
            cancelled = parsed.get("cancelled") or []
            logs = parsed.get("logs") or []
        except json.JSONDecodeError:
            pass
    user = repo.get_user_by_id(db, job.user_id) if job.user_id else None
    can_cancel = job.status in ("pending", "running")
    completed_report_ids: list[str] = []
    if job.status in ("pending", "running") and report_ids:
        completed_report_ids = list_job_completed_report_ids(job.id, report_ids)
    return {
        "id": job.id,
        "status": job.status,
        "summary_date": job.summary_date.isoformat(),
        "report_ids": report_ids,
        "completed_report_ids": completed_report_ids,
        "created_count": job.created_count,
        "failed_count": job.failed_count,
        "error_message": job.error_message,
        "failed": failed,
        "cancelled": cancelled,
        "logs": logs,
        "cancel_requested": bool(job.cancel_requested),
        "can_cancel": can_cancel,
        "requested_by": user.username if user else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


@router.get("/catalog", response_model=ReportCatalogOut)
def get_report_catalog(_: User = Depends(require_generate_access)):
    return {"items": list_report_catalog_for_ui()}


@router.get("/dates")
def get_available_dates(_: User = Depends(require_generate_access)):
    return {"items": list_available_dates()}


@router.get("/jobs")
def list_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: User = Depends(require_generate_access),
):
    jobs = repo.list_generation_jobs(db, limit=limit)
    return {"items": [_job_to_dict(j, db) for j in jobs]}


@router.post("/generate")
def generate_reports(
    payload: GenerateReportsIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_generate_access),
    x_svodki_r27_period: str | None = Header(default=None, alias="X-Svodki-R27-Period"),
):
    summary_date = payload.summary_date
    report_ids = [str(x) for x in payload.report_ids] if payload.report_ids else []
    try:
        job_options = payload.generation_job_options()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not job_options and x_svodki_r27_period:
        d_from, d_to, t_from, t_cut = _parse_report27_period_header(x_svodki_r27_period)
        if d_from and d_to and t_from and t_cut:
            if report_ids != ["27"]:
                raise HTTPException(
                    status_code=400,
                    detail='Заголовок X-Svodki-R27-Period разрешён только с report_ids: ["27"].',
                )
            job_options = {
                "report27_cutoff": t_cut,
                "report27_time_from": t_from,
                "report27_date_from": d_from.isoformat(),
                "report27_date_to": d_to.isoformat(),
            }
    elif job_options is not None and x_svodki_r27_period and "report27_date_from" not in job_options:
        d_from, d_to, t_from, t_cut = _parse_report27_period_header(x_svodki_r27_period)
        if d_from and d_to and t_from and t_cut and report_ids == ["27"]:
            job_options = {
                **job_options,
                "report27_cutoff": t_cut,
                "report27_time_from": t_from,
                "report27_date_from": d_from.isoformat(),
                "report27_date_to": d_to.isoformat(),
            }
    try:
        job, task_id = enqueue_or_run(
            db,
            user_id=user.id,
            summary_date=summary_date,
            report_ids=report_ids or [],
            job_options=job_options,
        )
    except ReportGenerationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record_activity(
        db,
        action="reports.generation.requested",
        user_id=user.id,
        ip=get_client_ip(request),
        job_id=job.id,
        report_ids=report_ids,
        summary_date=summary_date.isoformat(),
    )
    log.info(
        "reports.generation.enqueued",
        job_id=job.id,
        user_id=user.id,
        user_email=user.email or user.username,
        summary_date=summary_date.isoformat(),
        report_ids=report_ids,
        celery_task_id=task_id,
    )
    return {
        "job_id": job.id,
        "celery_task_id": task_id,
        "status": job.status,
    }


@router.post("/jobs/{job_id}/cancel")
def cancel_job(
    job_id: str,
    request: Request,
    payload: CancelGenerationJobIn | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_generate_access),
):
    job = repo.get_generation_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job_not_found")
    if job.status not in ("pending", "running"):
        raise HTTPException(status_code=400, detail="job_not_cancellable")
    report_ids = [str(x) for x in (payload.report_ids or [])] if payload and payload.report_ids else []
    job = cancel_generation_job(db, job, report_ids=report_ids or None)
    record_activity(
        db,
        action="reports.generation.cancel_requested",
        user_id=user.id,
        ip=get_client_ip(request),
        job_id=job.id,
        report_ids=report_ids or job.report_ids,
        summary_date=job.summary_date.isoformat(),
    )
    log.info(
        "reports.generation.cancel_requested",
        job_id=job.id,
        user_id=user.id,
        report_ids=report_ids,
        full_cancel=not report_ids,
    )
    return _job_to_dict(job, db)


@router.get("/jobs/{job_id}", response_model=JobStatusOut)
def get_job_status(job_id: str, db: Session = Depends(get_db), _: User = Depends(require_generate_access)):
    job = repo.get_generation_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job_not_found")
    result = None
    if job.result_json:
        try:
            result = json.loads(job.result_json)
        except json.JSONDecodeError:
            result = None
    return JobStatusOut(
        id=job.id,
        status=job.status,
        summary_date=job.summary_date.isoformat(),
        created_count=job.created_count,
        failed_count=job.failed_count,
        result=result,
        error_message=job.error_message,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


@router.get("/files/{summary_date}", response_model=GeneratedFileListOut)
def get_generated_files(
    summary_date: date,
    db: Session = Depends(get_db),
    _: User = Depends(require_generate_access),
):
    files = enrich_generated_files(db, summary_date, list_generated_files(summary_date))
    return GeneratedFileListOut(summary_date=summary_date, files=files)


@router.get("/files/{summary_date}/{filename}")
def download_generated_file(
    summary_date: date,
    filename: str,
    _: User = Depends(require_generate_access_flexible),
):
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Некорректное имя файла")
    lower = filename.lower()
    if not (lower.endswith(".pdf") or lower.endswith(".xlsx")):
        raise HTTPException(status_code=400, detail="Доступны только PDF и Excel")
    folder = Path(settings.upload_dir) / summary_date.isoformat()
    path = folder / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")
    media = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if lower.endswith(".xlsx")
        else "application/pdf"
    )
    return FileResponse(
        path,
        media_type=media,
        filename=filename,
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "private, no-transform",
        },
    )
