"""Схемы API: редактор планов и генерация отчётов."""
from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field


class ReportCatalogItemOut(BaseModel):
    id: str
    title: str
    picker_visible: bool = True
    default_selected: bool = True


class ReportCatalogOut(BaseModel):
    items: list[ReportCatalogItemOut] = Field(default_factory=list)


class GenerateReportsIn(BaseModel):
    summary_date: date
    report_ids: list[str] | None = None


class CancelGenerationJobIn(BaseModel):
    report_ids: list[str] | None = None


class CancelProverkaJobIn(BaseModel):
    report_ids: list[str] | None = None


class GeneratedFileOut(BaseModel):
    report_id: str
    title: str
    xlsx_filename: str | None = None
    pdf_filename: str | None = None


class GenerateReportsOut(BaseModel):
    summary_date: date
    created: list[GeneratedFileOut] = Field(default_factory=list)
    failed: list[dict[str, str]] = Field(default_factory=list)
    logs: list[str] = Field(default_factory=list)


class GeneratedFileListOut(BaseModel):
    summary_date: date
    files: list[dict[str, str]] = Field(default_factory=list)


class ProverkaCatalogItemOut(BaseModel):
    id: str
    title: str


class ProverkaCatalogOut(BaseModel):
    items: list[ProverkaCatalogItemOut] = Field(default_factory=list)
    env_cookie: str = ""


class ProverkaSeasonOverride(BaseModel):
    """Переопределение сезона на один запуск (пустые поля = из Редактора планов)."""

    task_season_years: list[int] | None = None
    history_years: list[int] | None = None
    period_start: date | None = None
    period_end: date | None = None
    shift_start_hour: int | None = Field(None, ge=0, le=23)
    shift_end_hour: int | None = Field(None, ge=0, le=23)
    task_season_plow_only: bool | None = None


class ProverkaRunIn(BaseModel):
    report_date: date
    report_ids: list[str] = Field(min_length=1)
    cookie: str | None = None
    compare_mode: Literal["cropwise", "svodki_only"] = "cropwise"
    season_overrides: dict[str, ProverkaSeasonOverride] = Field(default_factory=dict)
    ai_analysis: bool = False


class ProverkaSeasonMetaOut(BaseModel):
    report_date: str
    path: str
    by_group: dict[str, Any] = Field(default_factory=dict)
    by_report: dict[str, Any] = Field(default_factory=dict)


class ProverkaRunOut(BaseModel):
    job_id: str
    status: str = "pending"


class ProverkaMachineRunIn(BaseModel):
    """Запуск независимой проверки количества машин по операциям."""

    report_date: date
    report_ids: list[str] = Field(min_length=1)
    # только для обкатки механизма на заглушке: намеренно "сломать" сверку
    # по этим операциям, чтобы проверить, что DIFF корректно отображается
    force_diff_groups: list[str] = Field(default_factory=list)


class ProverkaMachineRunOut(BaseModel):
    job_id: str
    status: str = "pending"


class ProverkaMachineJobOut(BaseModel):
    job_id: str | None = None
    status: str
    report_date: str | None = None
    report_ids: list[str] = Field(default_factory=list)
    cancel_requested: bool = False
    cancel_report_ids: list[str] = Field(default_factory=list)
    can_cancel: bool = False
    unknown_report_ids: list[str] = Field(default_factory=list)
    progress: str | None = None
    error: str | None = None
    is_stub_data: bool = True
    real_data_report_ids: list[str] = Field(default_factory=list)
    stub_data_report_ids: list[str] = Field(default_factory=list)
    machine_cross_check: dict[str, Any] = Field(default_factory=dict)
    machine_cross_check_rows: list[dict[str, Any]] = Field(default_factory=list)
    checked_at: str | None = None


class ProverkaViolationOut(BaseModel):
    kind: str
    report_id: str | None = None
    title: str | None = None
    status: str | None = None
    detail: str = ""


class ProverkaErrorLogOut(BaseModel):
    source: str
    message: str


class ProverkaJobOut(BaseModel):
    job_id: str | None = None
    status: str
    report_date: str | None = None
    report_ids: list[str] = Field(default_factory=list)
    cancel_requested: bool = False
    cancel_report_ids: list[str] = Field(default_factory=list)
    can_cancel: bool = False
    compare_mode: str | None = None
    progress: str | None = None
    excel_filename: str | None = None
    error: str | None = None
    summary_rows: list[dict[str, Any]] = Field(default_factory=list)
    cross_check: dict[str, Any] = Field(default_factory=dict)
    cross_check_rows: list[dict[str, Any]] = Field(default_factory=list)
    sheet_checks: list[dict[str, Any]] = Field(default_factory=list)
    exit_code: int | None = None
    violation_count: int = 0
    cropwise_diff_count: int = 0
    cropwise_violation_count: int = 0
    cross_check_violation_count: int = 0
    sheet_consistency_violation_count: int = 0
    violations: list[dict[str, Any]] = Field(default_factory=list)
    error_logs: list[dict[str, str]] = Field(default_factory=list)
    checked_at: str | None = None
    ai_status: str | None = None
    ai_violation_count: int = 0
    ai_report: dict[str, Any] | None = None
    ai_error: str | None = None


class ProverkaAiReportOut(BaseModel):
    report_date: str
    job_id: str | None = None
    ai_status: str = "none"
    ai_violation_count: int = 0
    ai_report: dict[str, Any] | None = None
    ai_error: str | None = None
    updated_at: str | None = None


class ProverkaAiHistoryItem(BaseModel):
    report_date: str
    job_id: str
    ai_status: str = "none"
    ai_violation_count: int = 0
    executive_summary: str = ""
    updated_at: str | None = None


class ProverkaAiHistoryOut(BaseModel):
    items: list[ProverkaAiHistoryItem] = Field(default_factory=list)


class ProverkaAiPromptRule(BaseModel):
    id: str = ""
    title: str = ""
    description: str = ""


class ProverkaAiPromptsOut(BaseModel):
    prompt_context: str = ""
    rules: list[ProverkaAiPromptRule] = Field(default_factory=list)
    is_custom: bool = False
    updated_at: str | None = None


class ProverkaAiPromptsIn(BaseModel):
    prompt_context: str = ""
    rules: list[ProverkaAiPromptRule] = Field(default_factory=list)


class ProverkaAiSettingsOut(BaseModel):
    model: str = "deepseek-flash"
    models: list[str] = Field(default_factory=list)
    api_key_set: bool = False
    api_key_masked: str = ""
    api_key_source: str = "none"
    is_custom: bool = False
    updated_at: str | None = None


class ProverkaAiSettingsIn(BaseModel):
    model: str = "deepseek-flash"
    api_key: str | None = None


class ProverkaDashboardOut(BaseModel):
    report_date: str
    status: str = "none"
    job_id: str | None = None
    violation_count: int = 0
    cropwise_diff_count: int = 0
    cropwise_violation_count: int = 0
    cross_check_violation_count: int = 0
    violations: list[dict[str, Any]] = Field(default_factory=list)
    error_logs: list[dict[str, str]] = Field(default_factory=list)
    checked_at: str | None = None
    progress: str | None = None
    ai_status: str | None = None
    ai_violation_count: int = 0


class ProverkaRefreshOut(BaseModel):
    job_id: str
    status: str = "pending"


class ProverkaMonitoringStatusOut(BaseModel):
    monitoring_enabled: bool = False
    cookie_valid: bool = False
    cookie_expires_at: str | None = None
    violation_count: int = 0
    cropwise_diff_count: int = 0
    violations: list[dict[str, Any]] = Field(default_factory=list)
    error_logs: list[dict[str, str]] = Field(default_factory=list)


class ProverkaMonitoringCookieIn(BaseModel):
    cookie: str = ""


class PlansEditorSectionOut(BaseModel):
    key: str
    label: str
    kind: str


class PlansEditorSeasonRangeUpdateIn(BaseModel):
    group_name: str = Field(min_length=1, max_length=300)
    period_start: str | None = None
    period_end: str | None = None
    season_year: int | None = None
    season_years: list[int] | None = None
    clear: bool = False


class PlansEditorCalendarSeasonEntryIn(BaseModel):
    period_start: str
    period_end: str


class PlansEditorCalendarSeasonsUpdateIn(BaseModel):
    calendar_seasons: dict[str, PlansEditorCalendarSeasonEntryIn] = Field(default_factory=dict)


class PlansEditorOut(BaseModel):
    sections: list[PlansEditorSectionOut]
    work_groups: list[str]
    firms: list[str]
    base: dict[str, Any]
    edits: dict[str, Any]
    effective: dict[str, Any]
    path: str
    has_edits: bool = False
    season_meta: dict[str, Any] | None = None


class PlansEditorWorkGroupItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    ids: list[int] = Field(default_factory=list)


class PlansEditorWorkGroupsUpdateIn(BaseModel):
    groups: list[PlansEditorWorkGroupItemIn] = Field(min_length=1)


class PlansEditorWorkTypeOut(BaseModel):
    id: int
    name: str


class PlansEditorWorkTypesOut(BaseModel):
    items: list[PlansEditorWorkTypeOut] = Field(default_factory=list)


class PlansEditorValueUpdateIn(BaseModel):
    section_key: str
    keys: list[str] = Field(default_factory=list)
    value: float | int


class PlansEditorResetIn(BaseModel):
    section_key: str | None = None


class Report1LayoutDonorReportOut(BaseModel):
    id: str
    title: str
    label: str = ""
    common_donor: bool = False
    pages: list[dict[str, Any]] = Field(default_factory=list)


class Report1LayoutDonorPageOut(BaseModel):
    page: int
    sheet: str = ""
    desc: str = ""


class Report1PageOrderOut(BaseModel):
    report_id: str = "1"
    page_slots: list[dict[str, Any]] | None = None
    page_order: list[int] | None = None
    hidden_pages: list[int] | None = None
    source_page_count: int | None = None
    reference_summary_id: int | None = None
    updated_at: str | None = None
    has_custom_order: bool = False
    graft_count: int = 0
    donor_catalog: list[Report1LayoutDonorReportOut] = Field(default_factory=list)
    path: str = ""
    applied_now: bool | None = None
    apply_error: str | None = None
    layers_note: str | None = None


class Report1PageOrderUpdateIn(BaseModel):
    page_slots: list[dict[str, Any]] = Field(min_length=1)
    hidden_pages: list[int] = Field(default_factory=list)
    reference_summary_id: int | None = None
    page_count: int | None = Field(default=None, ge=1)


class Report1PageOrderPreviewIn(BaseModel):
    summary_id: int = Field(ge=1)
    page_slots: list[dict[str, Any]] = Field(min_length=1)
    hidden_pages: list[int] = Field(default_factory=list)


class SummaryCellEditOut(BaseModel):
    id: str
    page: int
    left: float
    top: float
    width: float
    height: float
    cell_key: str
    cell_text: str = ""
    replacement: str
    fill_color: str = "#ffffff"
    text_color: str = ""
    created_at: str
    updated_at: str


class SummaryCellEditsOut(BaseModel):
    summary_id: int
    editable: bool
    report_id: str
    edits: list[SummaryCellEditOut]
    store_path: str


class SummaryCommentOut(BaseModel):
    id: str
    page: int
    x: float
    y: float
    comment_anchor_y: float | None = None
    cell_key: str
    cell_text: str = ""
    comment: str
    starred: bool = True
    apply_to_future: bool = False
    created_at: str
    updated_at: str


class SummaryCommentsOut(BaseModel):
    summary_id: int
    editable: bool
    report_id: str
    comments: list[SummaryCommentOut]
    store_path: str
    apply_error: str | None = None


class SummaryCommentUpsertIn(BaseModel):
    page: int = Field(ge=1)
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    comment_anchor_y: float | None = Field(default=None, ge=0.0, le=1.0)
    cell_key: str = Field(min_length=1)
    cell_text: str = ""
    comment: str = Field(min_length=1)
    apply_to_future: bool = False


class SummaryCommentFutureToggleIn(BaseModel):
    cell_key: str = Field(min_length=1)
    enabled: bool = True


class SummaryCellEditUpsertIn(BaseModel):
    page: int = Field(ge=1)
    left: float = Field(ge=0.0, le=1.0)
    top: float = Field(ge=0.0, le=1.0)
    width: float = Field(gt=0.0, le=1.0)
    height: float = Field(gt=0.0, le=1.0)
    cell_key: str = Field(min_length=1)
    cell_text: str = ""
    replacement: str = Field(min_length=1)
    fill_color: str = ""
    text_color: str = ""


class FieldSeasonFieldOut(BaseModel):
    field_id: int
    field_name: str


class FieldSeasonRowOut(BaseModel):
    field_id: int
    field_name: str
    field_area: float
    crop: str
    work_type: str
    material_name: str = ""
    yield_value: float = 0.0
    fact_rate: float = 0.0
    planned_rate: float = 0.0
    firm_name: str = ""
    completed_area: float
    covered_area: float = 0.0
    date_start: str
    date_end: str
    folder_id: int | None = None


class FieldSeasonStateOut(BaseModel):
    exists: bool
    season_year: int | None = None
    created_at: str | None = None
    fields: list[FieldSeasonFieldOut] = Field(default_factory=list)
    running: bool = False
    error: str = ""


class FieldSeasonTableOut(BaseModel):
    field_id: int
    field_name: str
    rows: list[FieldSeasonRowOut] = Field(default_factory=list)
    total: int = 0
    offset: int = 0
    limit: int = 0
    has_more: bool = False
    next_offset: int | None = None


class WorksSummaryRowOut(BaseModel):
    year: int
    date: str
    group: str
    crop: str
    firm: str
    area: float


class WorksSummaryStateOut(BaseModel):
    exists: bool
    season_year: int | None = None
    prev_year: int | None = None
    created_at: str | None = None
    groups: list[str] = Field(default_factory=list)
    crops: list[str] = Field(default_factory=list)
    firms: list[str] = Field(default_factory=list)
    running: bool = False
    error: str = ""
    queue_job_id: str | None = None


class WorksSummaryTableOut(BaseModel):
    rows: list[WorksSummaryRowOut] = Field(default_factory=list)
    total: int = 0
    offset: int = 0
    limit: int = 0
    has_more: bool = False
    next_offset: int | None = None


class WorksSummaryGridDateHeaderOut(BaseModel):
    date: str
    label: str


class WorksSummaryGridFirmRowOut(BaseModel):
    firm: str
    daily: list[float] = Field(default_factory=list)
    total: float = 0.0


class WorksSummaryGridBlockOut(BaseModel):
    header_year: int
    is_current_season: bool
    title: str
    date_headers: list[WorksSummaryGridDateHeaderOut] = Field(default_factory=list)
    firms: list[WorksSummaryGridFirmRowOut] = Field(default_factory=list)
    grand_total: float = 0.0


class WorksSummaryGridSheetOut(BaseModel):
    group: str
    crop: str
    sheet_title: str
    display_title: str = ""
    blocks: list[WorksSummaryGridBlockOut] = Field(default_factory=list)


class WorksSummaryGridOut(BaseModel):
    season_year: int
    prev_year: int
    effective_date_from: str
    effective_date_to: str
    sheet_count: int = 0
    sheets: list[WorksSummaryGridSheetOut] = Field(default_factory=list)


class InterfaceSettingsOut(BaseModel):
    theme: str
    has_custom_background: bool = False
    background_version: int = 0


class InterfaceSettingsIn(BaseModel):
    theme: str
    custom_background: str | None = None


class PdfHeaderLabelItemOut(BaseModel):
    default_work_line: str
    work_line: str
    is_custom: bool = False
    report_ids: list[str] = Field(default_factory=list)


class PdfHeaderLabelsOut(BaseModel):
    path: str = ""
    items: list[PdfHeaderLabelItemOut] = Field(default_factory=list)
    work_line_map: dict[str, str] = Field(default_factory=dict)


class PdfHeaderLabelUpdateItemIn(BaseModel):
    default_work_line: str
    work_line: str | None = None


class PdfHeaderLabelsUpdateIn(BaseModel):
    items: list[PdfHeaderLabelUpdateItemIn] = Field(default_factory=list)
