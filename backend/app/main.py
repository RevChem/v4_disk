"""Composition root — Svodki PDF API."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.adapters.inbound.api.routers import (
    activity,
    analytics,
    audit,
    auth,
    convert,
    field_season,
    interface_settings,
    users_manage,
    namolot,
    page_order,
    pdf_header_labels,
    plans,
    proverka,
    reports,
    schedule,
    summaries,
    works_summary,
)
from app.config import cors_origins_list, settings
from app.infrastructure.bootstrap import init_db
from app.infrastructure.logging import get_logger, setup_logging
from app.infrastructure.middleware import RequestContextMiddleware

setup_logging(settings.log_level)
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    log.info("app.started")
    yield
    log.info("app.stopped")


app = FastAPI(title="Центр PDF-сводок", lifespan=lifespan)

app.add_middleware(RequestContextMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins_list(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health_check():
    return {"status": "ok"}


app.include_router(interface_settings.router)
app.include_router(auth.router)
app.include_router(users_manage.router)
app.include_router(plans.router)
app.include_router(reports.router)
app.include_router(proverka.router)
app.include_router(namolot.router)
app.include_router(convert.router)
app.include_router(schedule.router)
app.include_router(audit.router)
app.include_router(activity.router)
app.include_router(analytics.router)
app.include_router(summaries.router)
app.include_router(page_order.router)
app.include_router(field_season.router)
app.include_router(works_summary.router)
app.include_router(pdf_header_labels.router)
