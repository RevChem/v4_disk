"""Reports API."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from app.legacy.report_generation import (
    DEVELOPER_ONLY_REPORT_IDS,
    _parse_report27_period_header,
    load_generation_alerts,
)
from app.legacy.report_runner import _report_id_from_filename, list_job_completed_report_ids

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


def _visible_report_ids(report_ids: list[str], user: User) -> list[str]:
    if user.role == "developer":
        return report_ids
    return [rid for rid in report_ids if rid not in DEVELOPER_ONLY_REPORT_IDS]


def _job_to_dict(job: GenerationJob, db: Session, user: User | None = None) -> dict:
    report_ids: list[str] = []
    try:
        report_ids = json.loads(job.report_ids or "[]")
    except json.JSONDecodeError:
        report_ids = []
    failed: list[dict] = []
    cancelled: list[dict] = []
    logs: list[str] = []
    cropwise_diffs: list[dict] = []
    cropwise_season_diffs: list[dict] = []
    created: list[dict] = []
    if job.result_json:
        try:
            parsed = json.loads(job.result_json)
            failed = parsed.get("failed") or []
            cancelled = parsed.get("cancelled") or []
            logs = parsed.get("logs") or []
            raw_diffs = parsed.get("cropwise_diffs") or []
            if isinstance(raw_diffs, list):
                cropwise_diffs = [row for row in raw_diffs if isinstance(row, dict)]
            raw_season = parsed.get("cropwise_season_diffs") or []
            if isinstance(raw_season, list):
                cropwise_season_diffs = [row for row in raw_season if isinstance(row, dict)]
            raw_created = parsed.get("created") or []
            if isinstance(raw_created, list):
                created = [row for row in raw_created if isinstance(row, dict)]
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
        "report_ids": _visible_report_ids(report_ids, user) if user is not None else report_ids,
        "completed_report_ids": (
            _visible_report_ids(completed_report_ids, user) if user is not None else completed_report_ids
        ),
        "created_count": job.created_count,
        "failed_count": job.failed_count,
        "error_message": job.error_message,
        "failed": failed,
        "cancelled": cancelled,
        "logs": logs,
        "cropwise_diffs": cropwise_diffs,
        "cropwise_season_diffs": cropwise_season_diffs,
        "created": created,
        "cancel_requested": bool(job.cancel_requested),
        "can_cancel": can_cancel,
        "requested_by": user.username if user else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


@router.get("/catalog", response_model=ReportCatalogOut)
def get_report_catalog(user: User = Depends(require_generate_access)):
    return {
        "items": list_report_catalog_for_ui(include_developer_only=user.role == "developer")
    }


@router.get("/cropwise-cookie")
def cropwise_cookie_available(_: User = Depends(require_generate_access)):
    """Есть ли служебная cookie веб-сессии. Саму cookie наружу не отдаём."""
    from app.adapters.outbound.proverka.service import session_cookie_from_env

    try:
        available = bool(session_cookie_from_env())
    except Exception:
        available = False
    return {"available": available}


@router.get("/dates")
def get_available_dates(_: User = Depends(require_generate_access)):
    return {"items": list_available_dates()}


@router.get("/jobs")
def list_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(require_generate_access),
):
    jobs = repo.list_generation_jobs(db, limit=limit)
    return {"items": [_job_to_dict(j, db, user) for j in jobs]}


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
    if user.role != "developer" and any(rid in DEVELOPER_ONLY_REPORT_IDS for rid in report_ids):
        raise HTTPException(status_code=403, detail="Сводка доступна только роли «Разработчик».")
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
        report_ids=report_ids or json.loads(job.report_ids or "[]"),
        summary_date=job.summary_date.isoformat(),
    )
    log.info(
        "reports.generation.cancel_requested",
        job_id=job.id,
        user_id=user.id,
        report_ids=report_ids,
        full_cancel=not report_ids,
    )
    return _job_to_dict(job, db, user)


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
    user: User = Depends(require_generate_access),
):
    files = enrich_generated_files(db, summary_date, list_generated_files(summary_date))
    if user.role != "developer":
        files = [row for row in files if str(row.get("report_id") or "") not in DEVELOPER_ONLY_REPORT_IDS]
    try:
        alerts = load_generation_alerts(summary_date)
    except Exception:  # индикатор не должен ломать список файлов
        alerts = {}
    shown = {str(row.get("report_id") or "") for row in files}
    alerts = {rid: items for rid, items in alerts.items() if rid in shown}
    return GeneratedFileListOut(summary_date=summary_date, files=files, alerts=alerts)


@router.get("/files/{summary_date}/{filename}")
def download_generated_file(
    summary_date: date,
    filename: str,
    user: User = Depends(require_generate_access_flexible),
):
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Некорректное имя файла")
    if (
        user.role != "developer"
        and _report_id_from_filename(filename) in DEVELOPER_ONLY_REPORT_IDS
    ):
        raise HTTPException(status_code=404, detail="Файл не найден")
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
