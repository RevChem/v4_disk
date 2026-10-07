"""API: сверка сводок с Cropwise (proverka)."""
from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse

from app.adapters.inbound.api.deps import get_client_ip, require_developer_access, require_plans_access
from app.adapters.outbound.persistence import repositories as repo
from app.adapters.inbound.celery.tasks import (
    run_proverka_ai_task,
    run_proverka_machines_task,
    run_proverka_task,
)
from app.adapters.outbound.proverka.monitoring import (
    disable_monitoring,
    enable_monitoring,
    monitoring_status,
    set_cookie_and_enable,
)
from app.adapters.outbound.proverka.ai_prompts import (
    prompts_out,
    reset_prompts,
    save_prompts,
)
from app.adapters.outbound.proverka.ai_settings import (
    reset_settings,
    save_settings,
    settings_out,
)
from app.adapters.outbound.proverka.cropwise_ask_service import (
    cropwise_ask,
    cropwise_groups,
    cropwise_query,
)
from app.adapters.outbound.proverka.driver_work_service import (
    driver_work_detail,
    driver_work_people,
)
from app.adapters.outbound.proverka.task_facts_service import (
    facts_ask,
    facts_options,
    facts_query,
)
from app.adapters.outbound.proverka.service import (
    ai_report_for_date,
    ai_report_for_job,
    list_ai_report_history,
    create_job,
    dashboard_for_date,
    excel_path_for_job,
    latest_job_for_date,
    list_catalog,
    read_job_status,
    session_cookie_from_env,
    platform_summary,
    refresh_dashboard_check,
    request_proverka_cancel,
    season_meta_for_reports,
)
from app.config import settings
from app.database import get_db
from app.models import User
from app.schemas import (
    ProverkaAiPromptsIn,
    ProverkaAiPromptsOut,
    ProverkaAiSettingsIn,
    ProverkaAiSettingsOut,
    ProverkaAiHistoryOut,
    ProverkaAiReportOut,
    CancelProverkaJobIn,
    ProverkaCatalogOut,
    ProverkaDashboardOut,
    ProverkaJobOut,
    ProverkaMachineJobOut,
    ProverkaMachineRunIn,
    ProverkaMachineRunOut,
    ProverkaMonitoringCookieIn,
    ProverkaMonitoringStatusOut,
    ProverkaRefreshOut,
    ProverkaRunIn,
    ProverkaRunOut,
    ProverkaSeasonMetaOut,
)
from app.application.audit_activity import record_activity
from app.infrastructure.logging import get_logger
from sqlalchemy.orm import Session

router = APIRouter(prefix="/proverka", tags=["proverka"])
log = get_logger(__name__)


@router.get("/catalog", response_model=ProverkaCatalogOut)
def get_proverka_catalog(_: User = Depends(require_developer_access)):
    return {"items": list_catalog(), "env_cookie": session_cookie_from_env()}


@router.get("/platform-summary")
def get_proverka_platform_summary(_: User = Depends(require_developer_access)):
    return platform_summary()


@router.get("/season-meta", response_model=ProverkaSeasonMetaOut)
def get_proverka_season_meta(
    report_date: date = Query(..., alias="report_date"),
    report_ids: list[str] = Query(default=[], alias="report_ids"),
    _: User = Depends(require_developer_access),
):
    ids = [str(x).strip() for x in report_ids if str(x).strip()]
    if not ids:
        raise HTTPException(status_code=400, detail="Укажите report_ids")
    return season_meta_for_reports(report_date, ids)


@router.post("/run", response_model=ProverkaRunOut)
def run_proverka(
    payload: ProverkaRunIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_developer_access),
):
    report_ids = [str(x) for x in (payload.report_ids or [])]
    if not report_ids:
        raise HTTPException(status_code=400, detail="Выберите хотя бы одну сводку")
    cookie = (payload.cookie or "").strip() or session_cookie_from_env()
    if payload.compare_mode == "cropwise" and not cookie:
        raise HTTPException(
            status_code=400,
            detail="Введите cookie или задайте CROPWISE_SESSION_COOKIE в .env",
        )

    compare_cropwise = payload.compare_mode != "svodki_only"
    from app.adapters.outbound.proverka.service import editor_season_overrides

    editor_defaults = editor_season_overrides(payload.report_date, report_ids)
    user_overrides = {
        str(rid): ov.model_dump(exclude_none=True)
        for rid, ov in (payload.season_overrides or {}).items()
        if ov.model_dump(exclude_none=True)
    }
    if user_overrides:
        season_overrides = dict(editor_defaults)
        for rid, ov in user_overrides.items():
            season_overrides[rid] = {**editor_defaults.get(rid, {}), **ov}
    else:
        season_overrides = editor_defaults

    job_id = create_job(
        report_date=payload.report_date,
        report_ids=report_ids,
        compare_cropwise=compare_cropwise,
        cookie=cookie,
        season_overrides=season_overrides,
        run_ai_analysis=bool(payload.ai_analysis),
        user_id=user.id,
    )

    if settings.celery_enabled:
        run_proverka_task.delay(job_id)
    else:
        import threading
        from app.adapters.outbound.proverka.service import run_job

        threading.Thread(target=run_job, args=(job_id,), daemon=True).start()

    record_activity(
        db,
        action="proverka.requested",
        user_id=user.id,
        ip=get_client_ip(request),
        job_id=job_id,
        report_ids=report_ids,
        report_date=payload.report_date.isoformat(),
        summary_date=payload.report_date.isoformat(),
        compare_mode=payload.compare_mode,
    )
    log.info(
        "proverka.enqueued",
        job_id=job_id,
        user_id=user.id,
        report_ids=report_ids,
        compare_mode=payload.compare_mode,
    )
    return {"job_id": job_id, "status": "pending"}


@router.post("/run-machines", response_model=ProverkaMachineRunOut)
def run_proverka_machines(
    payload: ProverkaMachineRunIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_developer_access),
):
    """Независимая проверка количества машин по операциям (своя кнопка,
    свой job, ничего общего с /proverka/run по гектарам)."""
    from app.adapters.outbound.proverka import machines_service

    report_ids = [str(x) for x in (payload.report_ids or [])]
    if not report_ids:
        raise HTTPException(status_code=400, detail="Выберите хотя бы одну сводку")

    job_id = machines_service.create_job(
        report_date=payload.report_date,
        report_ids=report_ids,
        user_id=user.id,
        force_diff_groups=list(payload.force_diff_groups or []),
    )

    if settings.celery_enabled:
        run_proverka_machines_task.delay(job_id)
    else:
        import threading

        threading.Thread(target=machines_service.run_job, args=(job_id,), daemon=True).start()

    record_activity(
        db,
        action="proverka_machines.requested",
        user_id=user.id,
        ip=get_client_ip(request),
        job_id=job_id,
        report_ids=report_ids,
        report_date=payload.report_date.isoformat(),
        summary_date=payload.report_date.isoformat(),
    )
    log.info(
        "proverka_machines.enqueued",
        job_id=job_id,
        user_id=user.id,
        report_ids=report_ids,
    )
    return {"job_id": job_id, "status": "pending"}


@router.get("/machines/jobs/{job_id}", response_model=ProverkaMachineJobOut)
def get_proverka_machines_job(job_id: str, _: User = Depends(require_developer_access)):
    from app.adapters.outbound.proverka import machines_service

    st = machines_service.read_job_status(job_id)
    if st is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    st["can_cancel"] = st.get("status") in ("pending", "running")
    return st


@router.post("/machines/jobs/{job_id}/cancel")
def cancel_proverka_machines_job(
    job_id: str,
    request: Request,
    payload: CancelProverkaJobIn | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_developer_access),
):
    from app.adapters.outbound.proverka import machines_service

    report_ids = [str(x) for x in (payload.report_ids or [])] if payload and payload.report_ids else []
    try:
        st = machines_service.request_cancel(job_id, report_ids=report_ids or None)
    except ValueError as exc:
        code = str(exc)
        if code == "job_not_found":
            raise HTTPException(status_code=404, detail="Задача не найдена") from exc
        raise HTTPException(status_code=400, detail="Задачу нельзя отменить") from exc
    record_activity(
        db,
        action="proverka_machines.cancel_requested",
        user_id=user.id,
        ip=get_client_ip(request),
        job_id=job_id,
        report_ids=report_ids or st.get("report_ids") or [],
        report_date=st.get("report_date"),
        summary_date=st.get("report_date"),
    )
    st["can_cancel"] = st.get("status") in ("pending", "running")
    return st


@router.get("/monitoring/status", response_model=ProverkaMonitoringStatusOut)
def get_proverka_monitoring_status(
    report_date: date = Query(..., alias="report_date"),
    user: User = Depends(require_developer_access),
):
    return monitoring_status(user.id, report_date)


@router.post("/monitoring/enable", response_model=ProverkaMonitoringStatusOut)
def enable_proverka_monitoring(
    report_date: date = Query(..., alias="report_date"),
    payload: ProverkaMonitoringCookieIn | None = None,
    user: User = Depends(require_developer_access),
):
    try:
        if payload and (payload.cookie or "").strip():
            set_cookie_and_enable(user.id, payload.cookie.strip())
        else:
            enable_monitoring(user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return monitoring_status(user.id, report_date)


@router.post("/monitoring/disable", response_model=ProverkaMonitoringStatusOut)
def disable_proverka_monitoring(
    report_date: date = Query(..., alias="report_date"),
    user: User = Depends(require_developer_access),
):
    disable_monitoring(user.id)
    return monitoring_status(user.id, report_date)


@router.get("/dashboard", response_model=ProverkaDashboardOut)
def get_proverka_dashboard(
    report_date: date = Query(..., alias="report_date"),
    user: User = Depends(require_developer_access),
):
    st = monitoring_status(user.id, report_date)
    if st.get("monitoring_enabled"):
        return {
            "report_date": report_date.isoformat(),
            "status": "completed" if st.get("violations") or st.get("violation_count") else "none",
            "job_id": None,
            "violation_count": st.get("violation_count", 0),
            "cropwise_diff_count": st.get("cropwise_diff_count", 0),
            "cropwise_violation_count": st.get("violation_count", 0),
            "cross_check_violation_count": 0,
            "violations": st.get("violations") or [],
            "error_logs": st.get("error_logs") or [],
            "checked_at": st.get("cookie_expires_at"),
            "progress": None,
        }
    return dashboard_for_date(report_date)


@router.post("/refresh", response_model=ProverkaRefreshOut)
def refresh_proverka_dashboard(
    request: Request,
    report_date: date = Query(..., alias="report_date"),
    db: Session = Depends(get_db),
    user: User = Depends(require_developer_access),
):
    try:
        job_id = refresh_dashboard_check(report_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if settings.celery_enabled:
        run_proverka_task.delay(job_id)
    else:
        import threading
        from app.adapters.outbound.proverka.service import run_job

        threading.Thread(target=run_job, args=(job_id,), daemon=True).start()

    record_activity(
        db,
        action="proverka.refresh",
        user_id=user.id,
        ip=get_client_ip(request),
        job_id=job_id,
        report_date=report_date.isoformat(),
        summary_date=report_date.isoformat(),
    )
    return {"job_id": job_id, "status": "pending"}


@router.post("/jobs/{job_id}/cancel")
def cancel_proverka_job(
    job_id: str,
    request: Request,
    payload: CancelProverkaJobIn | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_developer_access),
):
    report_ids = [str(x) for x in (payload.report_ids or [])] if payload and payload.report_ids else []
    try:
        st = request_proverka_cancel(job_id, report_ids=report_ids or None)
    except ValueError as exc:
        code = str(exc)
        if code == "job_not_found":
            raise HTTPException(status_code=404, detail="Задача не найдена") from exc
        raise HTTPException(status_code=400, detail="Задачу нельзя отменить") from exc
    record_activity(
        db,
        action="proverka.cancel_requested",
        user_id=user.id,
        ip=get_client_ip(request),
        job_id=job_id,
        report_ids=report_ids or st.get("report_ids") or [],
        report_date=st.get("report_date"),
        summary_date=st.get("report_date"),
    )
    log.info(
        "proverka.cancel_requested",
        job_id=job_id,
        user_id=user.id,
        report_ids=report_ids,
    )
    st["can_cancel"] = st.get("status") in ("pending", "running")
    return st


def _latest_job_payload(found: tuple[str, float] | None, report_date: date | None = None) -> dict:
    from app.legacy.report_generation import load_generation_data_checks

    data_checks = load_generation_data_checks(report_date) if report_date else []
    if not found:
        return {"job_id": None, "checked_at": None, "data_checks": data_checks}
    job_id, mtime = found
    checked_at = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat() if mtime else None
    return {"job_id": job_id, "checked_at": checked_at, "data_checks": data_checks}


@router.get("/latest-job")
def get_latest_proverka_job(
    report_date: date = Query(..., alias="report_date"),
    _: User = Depends(require_developer_access),
):
    """Последний запуск «Проверки сводок» за дату — чтобы диалог показывал прошлые данные."""
    from app.adapters.outbound.proverka.service import latest_job_for_date_light

    return _latest_job_payload(latest_job_for_date_light(report_date), report_date)


@router.get("/machines/latest-job")
def get_latest_proverka_machines_job(
    report_date: date = Query(..., alias="report_date"),
    _: User = Depends(require_developer_access),
):
    """Последний запуск «Проверки кол-ва машин» за дату."""
    from app.adapters.outbound.proverka import machines_service

    return _latest_job_payload(machines_service.latest_job_for_date_light(report_date))


@router.get("/jobs/{job_id}", response_model=ProverkaJobOut)
def get_proverka_job(job_id: str, _: User = Depends(require_developer_access)):
    st = read_job_status(job_id)
    if st is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if st.get("status") == "completed" and not st.get("file_enriched"):
        from app.adapters.outbound.proverka.service import enrich_job_file_column, write_job_status

        st = enrich_job_file_column(st)
        if st.get("file_enriched"):
            write_job_status(job_id, st)
    from app.legacy.report_generation import load_generation_data_checks

    raw_date = st.get("report_date")
    if raw_date:
        try:
            st["data_checks"] = load_generation_data_checks(date.fromisoformat(str(raw_date)))
        except ValueError:
            st["data_checks"] = []
    if st.get("status") == "completed" and "violation_count" not in st:
        from app.adapters.outbound.proverka.service import _attach_violations

        st = _attach_violations(st)
    st["can_cancel"] = st.get("status") in ("pending", "running")
    return st


@router.get("/jobs/{job_id}/download")
def download_proverka_excel(job_id: str, _: User = Depends(require_developer_access)):
    path = excel_path_for_job(job_id)
    if path is None:
        st = read_job_status(job_id)
        if st and st.get("status") == "running":
            raise HTTPException(status_code=409, detail="Файл ещё формируется")
        raise HTTPException(status_code=404, detail="Excel не найден")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name,
    )


@router.get("/ai-prompts", response_model=ProverkaAiPromptsOut)
def get_proverka_ai_prompts(_: User = Depends(require_developer_access)):
    """Промпты DeepSeek (редактор + правила для LLM)."""
    return prompts_out()


@router.put("/ai-prompts", response_model=ProverkaAiPromptsOut)
def put_proverka_ai_prompts(
    payload: ProverkaAiPromptsIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_developer_access),
):
    result = save_prompts(payload.model_dump())
    record_activity(
        db,
        action="proverka.ai_prompts.updated",
        user_id=user.id,
        ip=get_client_ip(request),
        is_custom=result.get("is_custom"),
        rules_count=len(result.get("rules") or []),
    )
    return result


@router.get("/ai-settings", response_model=ProverkaAiSettingsOut)
def get_proverka_ai_settings(_: User = Depends(require_developer_access)):
    return settings_out()


@router.put("/ai-settings", response_model=ProverkaAiSettingsOut)
def put_proverka_ai_settings(
    payload: ProverkaAiSettingsIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_developer_access),
):
    try:
        result = save_settings(model=payload.model, api_key=payload.api_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record_activity(
        db,
        action="proverka.ai_settings.updated",
        user_id=user.id,
        ip=get_client_ip(request),
        model=result.get("model"),
    )
    return result


@router.post("/ai-settings/reset", response_model=ProverkaAiSettingsOut)
def reset_proverka_ai_settings(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_developer_access),
):
    result = reset_settings()
    record_activity(
        db,
        action="proverka.ai_settings.reset",
        user_id=user.id,
        ip=get_client_ip(request),
    )
    return result


@router.post("/ai-prompts/reset", response_model=ProverkaAiPromptsOut)
def reset_proverka_ai_prompts(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_developer_access),
):
    result = reset_prompts()
    record_activity(
        db,
        action="proverka.ai_prompts.reset",
        user_id=user.id,
        ip=get_client_ip(request),
    )
    return result


@router.get("/ai-report/history", response_model=ProverkaAiHistoryOut)
def get_proverka_ai_report_history(
    limit: int = Query(default=40, ge=1, le=200),
    _: User = Depends(require_developer_access),
):
    return {"items": list_ai_report_history(limit=limit)}


@router.get("/ai-report/by-job/{job_id}", response_model=ProverkaAiReportOut)
def get_proverka_ai_report_by_job(job_id: str, _: User = Depends(require_developer_access)):
    detail = ai_report_for_job(job_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="ai_report_not_found")
    return detail


@router.get("/ai-report", response_model=ProverkaAiReportOut)
def get_proverka_ai_report(
    report_date: date = Query(..., alias="report_date"),
    job_id: str | None = Query(default=None),
    _: User = Depends(require_developer_access),
):
    if job_id:
        detail = ai_report_for_job(job_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="ai_report_not_found")
        return detail
    return ai_report_for_date(report_date)


@router.post("/ai-report/run", response_model=ProverkaAiReportOut)
def run_proverka_ai_report(
    report_date: date = Query(..., alias="report_date"),
    _: User = Depends(require_developer_access),
):
    """AI-разбор по последнему completed proverka job за дату."""
    latest = latest_job_for_date(report_date)
    if not latest or latest.get("status") != "completed":
        raise HTTPException(
            status_code=404,
            detail="Нет завершённой proverka за эту дату. Сначала запустите проверку.",
        )
    job_id = str(latest.get("job_id") or "")
    if not job_id:
        raise HTTPException(status_code=404, detail="job_id не найден")

    if settings.celery_enabled:
        run_proverka_ai_task.delay(job_id)
        return {
            "report_date": report_date.isoformat(),
            "job_id": job_id,
            "ai_status": "running",
            "ai_violation_count": 0,
            "ai_report": None,
        }

    from app.adapters.outbound.proverka.service import run_ai_job

    run_ai_job(job_id)
    return ai_report_for_date(report_date)


class TaskFactsFilterIn(BaseModel):
    report_date: date
    job_id: str | None = None
    report_ids: list[str] | None = None
    group: str | None = None
    crop: str | None = None
    firm: str | None = None
    machine: str | None = None
    field: str | None = None
    task_id: int | None = None
    min_ha: float | None = None
    exact: bool = False


class TaskFactsAskIn(TaskFactsFilterIn):
    question: str = ""
    use_llm: bool = True
    max_rows: int | None = Field(default=None, ge=1, le=500)


def _facts_filter(body: TaskFactsFilterIn) -> dict:
    return body.model_dump(exclude={"report_date", "job_id", "report_ids"})


@router.get("/task-facts/options")
def get_task_facts_options(
    report_date: date = Query(..., alias="report_date"),
    job_id: str | None = Query(default=None),
    _: User = Depends(require_developer_access),
):
    """Какие группы, культуры и фирмы есть в сохранённых фактах заданий."""
    try:
        return facts_options(report_date, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/task-facts/query")
def post_task_facts_query(
    body: TaskFactsFilterIn,
    _: User = Depends(require_developer_access),
):
    """Список заданий по фильтру. Модель не вызывается."""
    try:
        return facts_query(
            body.report_date,
            body.job_id,
            _facts_filter(body),
            report_ids=body.report_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/task-facts/ask")
def post_task_facts_ask(
    body: TaskFactsAskIn,
    _: User = Depends(require_developer_access),
):
    """Ответ по уже сохранённым фактам. В модель уходит только отфильтрованный срез."""
    try:
        flt = _facts_filter(body)
        return facts_ask(
            body.report_date,
            body.job_id,
            flt,
            body.question,
            report_ids=body.report_ids,
            max_rows=body.max_rows,
            use_llm=body.use_llm,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class CropwiseFilterIn(BaseModel):
    cookie: str
    date_from: date
    date_to: date
    groups: list[str] = Field(min_length=1)
    firm: str | None = None
    crop: str | None = None
    season: int | None = Field(default=None, ge=2020, le=2035)


class CropwiseAskIn(CropwiseFilterIn):
    question: str = ""
    use_llm: bool = True


@router.get("/cropwise/groups")
def get_cropwise_groups(_: User = Depends(require_developer_access)):
    """Агрооперации, которые можно запросить в Cropwise отдельно от сверки сводок."""
    return cropwise_groups()


@router.post("/cropwise/query")
def post_cropwise_query(
    body: CropwiseFilterIn,
    _: User = Depends(require_developer_access),
):
    """Задания Cropwise по cookie. Модель не вызывается."""
    try:
        return cropwise_query(
            body.cookie,
            body.date_from,
            body.date_to,
            body.groups,
            firm=body.firm,
            crop=body.crop,
            season=body.season,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/cropwise/ask")
def post_cropwise_ask(
    body: CropwiseAskIn,
    _: User = Depends(require_developer_access),
):
    """Вопрос DeepSeek по отфильтрованным заданиям Cropwise."""
    try:
        return cropwise_ask(
            body.cookie,
            body.date_from,
            body.date_to,
            body.groups,
            body.question,
            firm=body.firm,
            crop=body.crop,
            season=body.season,
            use_llm=body.use_llm,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/driver-work/people")
def get_driver_work_people(
    date_from: date = Query(..., alias="date_from"),
    date_to: date = Query(..., alias="date_to"),
    _: User = Depends(require_plans_access),
):
    """Исполнители, у которых за период есть гектары в хранилище заданий."""
    try:
        return driver_work_people(date_from, date_to)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/driver-work")
def get_driver_work(
    date_from: date = Query(..., alias="date_from"),
    date_to: date = Query(..., alias="date_to"),
    driver_id: int = Query(..., alias="driver_id", ge=1),
    _: User = Depends(require_plans_access),
):
    """День, агрофирма, поле, операция, гектары и ссылка на задание."""
    try:
        return driver_work_detail(date_from, date_to, driver_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
