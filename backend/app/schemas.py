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
    report27_cutoff: str | None = Field(default=None, description="Только для сводки 27: конец окна HH:MM.")
    report27_time_from: str | None = Field(default=None, description="Только для сводки 27: начало окна HH:MM.")
    report27_date_from: date | None = Field(default=None, description="Только для сводки 27: дата начала окна.")
    report27_date_to: date | None = Field(default=None, description="Только для сводки 27: дата конца окна.")
    report_harvest_gross: dict[str, Any] | None = Field(
        default=None,
        description="Ручной валовый сбор для сводок 1 / 10 / 13 (crops / vegetables).",
    )
    compare_cropwise: bool = False

    def report27_options(self) -> dict[str, str] | None:
        if not any(
            (
                self.report27_cutoff,
                self.report27_time_from,
                self.report27_date_from,
                self.report27_date_to,
            )
        ):
            return None
        ids = [str(x).strip() for x in (self.report_ids or []) if str(x).strip()]
        if ids != ["27"]:
            raise ValueError('Поля report27_* разрешены только вместе с report_ids: ["27"].')
        if (self.report27_date_from or self.report27_date_to) and not (
            self.report27_date_from and self.report27_date_to
        ):
            raise ValueError(
                "Для произвольного периода сводки 27 укажите report27_date_from и report27_date_to."
            )
        if self.report27_date_from and self.report27_date_to:
            if not self.report27_time_from or not self.report27_cutoff:
                raise ValueError(
                    "Для произвольного периода сводки 27 укажите report27_time_from и report27_cutoff."
                )
        return {
            "report27_cutoff": str(self.report27_cutoff or "").strip() or None,
            "report27_time_from": str(self.report27_time_from or "").strip() or None,
            "report27_date_from": self.report27_date_from.isoformat() if self.report27_date_from else None,
            "report27_date_to": self.report27_date_to.isoformat() if self.report27_date_to else None,
        }

    def generation_job_options(self) -> dict[str, Any] | None:
        opts: dict[str, Any] = {}
        r27 = self.report27_options()
        if r27:
            opts.update(r27)
        if self.report_harvest_gross:
            opts["report_harvest_gross"] = self.report_harvest_gross
        if self.compare_cropwise:
            opts["compare_cropwise"] = True
        return opts or None


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


class CropwiseSessionCookieIn(BaseModel):
    cookie: str = ""


class CropwiseSessionCookieOut(BaseModel):
    cookie: str = ""
    configured: bool = False


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
    file_report_ids: list[str] = Field(default_factory=list)
    missing_file_report_ids: list[str] = Field(default_factory=list)
    no_machine_data_report_ids: list[str] = Field(default_factory=list)
    file_read_errors: dict[str, str] = Field(default_factory=dict)
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
    report_views: dict[str, Any] = Field(default_factory=dict)
    exit_code: int | None = None
    violation_count: int = 0
    cropwise_diff_count: int = 0
    cropwise_violation_count: int = 0
    cross_check_violation_count: int = 0
    sheet_consistency_violation_count: int = 0
    violations: list[dict[str, Any]] = Field(default_factory=list)
    error_logs: list[dict[str, str]] = Field(default_factory=list)
    data_checks: list[str] = Field(default_factory=list)
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
    monitoring_enabled: bool = False
    monitoring_violation_count: int = 0
    monitoring_cropwise_diff_count: int = 0
    monitoring_violations: list[dict[str, Any]] = Field(default_factory=list)
    monitoring_error_logs: list[dict[str, str]] = Field(default_factory=list)
    monitoring_cookie_expires_at: str | None = None


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


class PlansEditorYearRangeIn(BaseModel):
    period_start: str
    period_end: str


class PlansEditorSeasonRangeUpdateIn(BaseModel):
    group_name: str = Field(min_length=1, max_length=300)
    period_start: str | None = None
    period_end: str | None = None
    season_year: int | None = None
    season_years: list[int] | None = None
    year_ranges: dict[str, PlansEditorYearRangeIn] | None = None
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
    crops: list[str] = Field(default_factory=list)
    base: dict[str, Any]
    edits: dict[str, Any]
    effective: dict[str, Any]
    path: str
    has_edits: bool = False
    season_meta: dict[str, Any] | None = None
    plans_py_note: str | None = None


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


class PlansEditPresenceIn(BaseModel):
    token: str = Field(min_length=16, max_length=80)


class PlansEditPresenceUserOut(BaseModel):
    user_id: int
    name: str


class PlansEditPresenceOut(BaseModel):
    others: list[PlansEditPresenceUserOut] = Field(default_factory=list)


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
