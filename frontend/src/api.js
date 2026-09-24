import axios from "axios";

const TOKEN_KEY = "svodki_access_token";
const REFRESH_KEY = "svodki_refresh_token";

function apiBaseURL() {
  const v = import.meta.env.VITE_API_URL;
  if (v !== undefined && v !== "") return v;
  if (import.meta.env.DEV) return "http://127.0.0.1:6757";
  return "";
}

const api = axios.create({
  baseURL: apiBaseURL(),
  timeout: 3_600_000,
});

api.interceptors.request.use((config) => {
  const token = localStorage.getItem(TOKEN_KEY);
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

let refreshPromise = null;

api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const original = error.config;
    const status = error.response?.status;
    if (
      status !== 401
      || !original
      || original._retry
      || original.url?.includes("/auth/refresh")
      || original.url?.includes("/users/login")
    ) {
      return Promise.reject(error);
    }
    const { refresh } = getStoredTokens();
    if (!refresh) {
      return Promise.reject(error);
    }
    original._retry = true;
    if (!refreshPromise) {
      refreshPromise = api
        .post("/auth/refresh", { refresh_token: refresh })
        .then(({ data }) => {
          setStoredTokens(data.access_token, data.refresh_token);
          return data.access_token;
        })
        .finally(() => {
          refreshPromise = null;
        });
    }
    try {
      const access = await refreshPromise;
      original.headers.Authorization = `Bearer ${access}`;
      return api(original);
    } catch (refreshErr) {
      clearStoredTokens();
      return Promise.reject(refreshErr);
    }
  }
);

function formatError(err) {
  const d = err?.response?.data?.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((x) => x.msg || JSON.stringify(x)).join("; ");
  return err?.message || "Ошибка запроса";
}

export function getStoredTokens() {
  return {
    access: localStorage.getItem(TOKEN_KEY),
    refresh: localStorage.getItem(REFRESH_KEY),
  };
}

export function setStoredTokens(access, refresh) {
  if (access) localStorage.setItem(TOKEN_KEY, access);
  if (refresh) localStorage.setItem(REFRESH_KEY, refresh);
}

export function clearStoredTokens() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(REFRESH_KEY);
}

export async function login(email, password) {
  try {
    const { data } = await api.post("/users/login", { email, password });
    return data;
  } catch (e) {
    throw new Error(formatError(e));
  }
}

export async function verifyMfaLogin(mfaPendingToken, code) {
  try {
    const { data } = await api.post("/auth/mfa/verify-login", {
      mfa_pending_token: mfaPendingToken,
      code,
    });
    return data;
  } catch (e) {
    throw new Error(formatError(e));
  }
}

export async function fetchMe() {
  const { data } = await api.get("/users/me");
  return data;
}

export async function logout() {
  try {
    await api.post("/auth/logout");
  } catch {
    /* ignore */
  }
  clearStoredTokens();
}

export async function fetchReportCatalog() {
  const { data } = await api.get("/reports/catalog");
  return data;
}

export async function generateReports(summaryDate, reportIds) {
  try {
    const { data } = await api.post(
      "/reports/generate",
      {
        summary_date: summaryDate,
        report_ids: reportIds?.length ? reportIds : null,
      },
      { timeout: 60_000 }
    );
    return data;
  } catch (e) {
    throw new Error(formatError(e));
  }
}

export async function fetchGenerationJobs(limit = 50) {
  const { data } = await api.get("/reports/jobs", { params: { limit }, timeout: 15_000 });
  return data;
}

export async function fetchAvailableDates() {
  const { data } = await api.get("/reports/dates", { timeout: 15_000 });
  return data;
}

export async function getJobStatus(jobId) {
  const { data } = await api.get(`/reports/jobs/${jobId}`, { timeout: 15_000 });
  return data;
}

export async function cancelGenerationJob(jobId, reportIds = null) {
  try {
    const body = reportIds?.length ? { report_ids: reportIds } : {};
    const { data } = await api.post(`/reports/jobs/${encodeURIComponent(jobId)}/cancel`, body, {
      timeout: 15_000,
    });
    return data;
  } catch (e) {
    throw new Error(formatError(e));
  }
}

export async function listGeneratedFiles(summaryDate) {
  const { data } = await api.get(`/reports/files/${summaryDate}`, { timeout: 15_000 });
  return data;
}

export async function downloadGeneratedFile(summaryDate, filename) {
  const { data } = await api.get(`/reports/files/${summaryDate}/${encodeURIComponent(filename)}`, {
    responseType: "blob",
  });
  return data;
}

export async function convertXlsxToPdf(file, summaryDate = null) {
  const form = new FormData();
  form.append("file", file);
  if (summaryDate) form.append("summary_date", summaryDate);
  const { data } = await api.post("/convert/xlsx-to-pdf", form, {
    responseType: "blob",
    headers: { "Content-Type": "multipart/form-data" },
  });
  return data;
}

export async function fetchPlansEditor(summaryDate = null) {
  const { data } = await api.get("/plans-editor", {
    params: summaryDate ? { summary_date: summaryDate } : undefined,
  });
  return data;
}

export async function updatePlansEditorValue({ section_key, keys, value, summaryDate = null }) {
  const { data } = await api.put(
    "/plans-editor/value",
    { section_key, keys, value },
    { params: summaryDate ? { summary_date: summaryDate } : undefined }
  );
  return data;
}

export async function resetPlansEditor(sectionKey = null, summaryDate = null) {
  const { data } = await api.post(
    "/plans-editor/reset",
    sectionKey ? { section_key: sectionKey } : {},
    { params: summaryDate ? { summary_date: summaryDate } : undefined }
  );
  return data;
}

export async function fetchPlansEditorWorkTypes() {
  const { data } = await api.get("/plans-editor/work-types");
  return data;
}

export async function updatePlansEditorWorkGroups(groups, summaryDate = null) {
  const { data } = await api.put(
    "/plans-editor/work-groups",
    {
      groups: (Array.isArray(groups) ? groups : []).map((g) => ({
        name: String(g?.name || "").trim(),
        ids: Array.isArray(g?.ids) ? g.ids.map((x) => Number(x)).filter((x) => Number.isFinite(x)) : [],
      })),
    },
    { params: summaryDate ? { summary_date: summaryDate } : undefined }
  );
  return data;
}

export async function updatePlansEditorSeasonRange(
  { group_name, period_start = null, period_end = null, season_year = null, season_years = null, clear = false },
  summaryDate = null
) {
  const { data } = await api.put(
    "/plans-editor/season-range",
    { group_name, period_start, period_end, season_year, season_years, clear },
    { params: summaryDate ? { summary_date: summaryDate } : undefined }
  );
  return data;
}

export async function updatePlansEditorCalendarSeasons({ calendar_seasons }, summaryDate = null) {
  const { data } = await api.put(
    "/plans-editor/calendar-seasons",
    { calendar_seasons: calendar_seasons || {} },
    { params: summaryDate ? { summary_date: summaryDate } : undefined }
  );
  return data;
}

export async function fetchSchedule() {
  const { data } = await api.get("/schedule");
  return data;
}

export async function updateSchedule(config) {
  const { data } = await api.put("/schedule", { config });
  return data;
}

export async function fetchAuditLogs(limit = 100) {
  const { data } = await api.get("/audit", { params: { limit } });
  return data;
}

export async function fetchActivityFeed(limit = 80) {
  const { data } = await api.get("/activity/feed", { params: { limit }, timeout: 30_000 });
  return data;
}

export async function fetchActivityJob(jobId) {
  const { data } = await api.get(`/activity/jobs/${jobId}`, { timeout: 30_000 });
  return data;
}

export async function fetchProverkaCatalog() {
  const { data } = await api.get("/proverka/catalog", { timeout: 15_000 });
  return data;
}

export async function fetchProverkaSeasonMeta({ reportDate, reportIds }) {
  const { data } = await api.get("/proverka/season-meta", {
    params: {
      report_date: reportDate,
      report_ids: reportIds,
    },
    paramsSerializer: { indexes: null },
    timeout: 15_000,
  });
  return data;
}

export async function runProverka({
  reportDate,
  reportIds,
  cookie,
  compareMode,
  seasonOverrides,
  aiAnalysis,
}) {
  try {
    const { data } = await api.post(
      "/proverka/run",
      {
        report_date: reportDate,
        report_ids: reportIds,
        cookie: cookie || null,
        compare_mode: compareMode || "cropwise",
        season_overrides: seasonOverrides || {},
        ai_analysis: Boolean(aiAnalysis),
      },
      { timeout: 60_000 }
    );
    return data;
  } catch (e) {
    throw new Error(formatError(e));
  }
}

export async function fetchProverkaAiReport(reportDate) {
  const { data } = await api.get("/proverka/ai-report", {
    params: { report_date: reportDate },
    timeout: 30_000,
  });
  return data;
}

export async function fetchProverkaAiReportHistory(limit = 40) {
  const { data } = await api.get("/proverka/ai-report/history", {
    params: { limit },
    timeout: 30_000,
  });
  return data;
}

export async function fetchProverkaAiReportByJob(jobId) {
  const { data } = await api.get(`/proverka/ai-report/by-job/${jobId}`, { timeout: 30_000 });
  return data;
}

export async function fetchProverkaAiPrompts() {
  const { data } = await api.get("/proverka/ai-prompts", { timeout: 30_000 });
  return data;
}

export async function saveProverkaAiPrompts(payload) {
  try {
    const { data } = await api.put("/proverka/ai-prompts", payload, { timeout: 30_000 });
    return data;
  } catch (e) {
    throw new Error(formatError(e));
  }
}

export async function resetProverkaAiPrompts() {
  try {
    const { data } = await api.post("/proverka/ai-prompts/reset", null, { timeout: 30_000 });
    return data;
  } catch (e) {
    throw new Error(formatError(e));
  }
}

export async function fetchProverkaAiSettings() {
  const { data } = await api.get("/proverka/ai-settings", { timeout: 30_000 });
  return data;
}

export async function saveProverkaAiSettings(payload) {
  try {
    const { data } = await api.put("/proverka/ai-settings", payload, { timeout: 30_000 });
    return data;
  } catch (e) {
    throw new Error(formatError(e));
  }
}

export async function runProverkaAiReport(reportDate) {
  try {
    const { data } = await api.post(
      "/proverka/ai-report/run",
      null,
      { params: { report_date: reportDate }, timeout: 60_000 }
    );
    return data;
  } catch (e) {
    throw new Error(formatError(e));
  }
}

export async function fetchProverkaDashboard(reportDate) {
  const { data } = await api.get("/proverka/dashboard", {
    params: { report_date: reportDate },
    timeout: 15_000,
  });
  return data;
}

/** Последний запуск «Проверки сводок» за дату: { job_id | null, checked_at }. */
export async function fetchLatestProverkaJob(reportDate) {
  const { data } = await api.get("/proverka/latest-job", {
    params: { report_date: reportDate },
    timeout: 15_000,
  });
  return data;
}

/** Последний запуск «Проверки кол-ва машин» за дату. */
export async function fetchLatestProverkaMachinesJob(reportDate) {
  const { data } = await api.get("/proverka/machines/latest-job", {
    params: { report_date: reportDate },
    timeout: 15_000,
  });
  return data;
}

export async function fetchProverkaJob(jobId) {
  const { data } = await api.get(`/proverka/jobs/${jobId}`, { timeout: 15_000 });
  return data;
}

export async function cancelProverkaJob(jobId, reportIds = null) {
  try {
    const body = reportIds?.length ? { report_ids: reportIds } : {};
    const { data } = await api.post(`/proverka/jobs/${encodeURIComponent(jobId)}/cancel`, body, {
      timeout: 15_000,
    });
    return data;
  } catch (e) {
    throw new Error(formatError(e));
  }
}

export async function downloadProverkaExcel(jobId) {
  const { data } = await api.get(`/proverka/jobs/${jobId}/download`, {
    responseType: "blob",
    timeout: 120_000,
  });
  return data;
}

// --- Вопросы по уже сохранённым фактам заданий (задание × поле), без ---
// --- повторного прогона сводок. См. backend .../task_facts_service.py. ---

export async function fetchTaskFactsOptions({ reportDate, jobId } = {}) {
  try {
    const { data } = await api.get("/proverka/task-facts/options", {
      params: { report_date: reportDate, job_id: jobId || undefined },
      timeout: 20_000,
    });
    return data;
  } catch (e) {
    throw new Error(formatError(e));
  }
}

function taskFactsFilterBody({
  reportDate,
  jobId,
  reportIds,
  group,
  crop,
  firm,
  machine,
  field,
  taskId,
  minHa,
  exact,
}) {
  return {
    report_date: reportDate,
    job_id: jobId || null,
    report_ids: reportIds && reportIds.length ? reportIds : null,
    group: group || null,
    crop: crop || null,
    firm: firm || null,
    machine: machine || null,
    field: field || null,
    task_id: taskId === "" || taskId === undefined || taskId === null ? null : Number(taskId),
    min_ha: minHa === "" || minHa === undefined || minHa === null ? null : Number(minHa),
    exact: Boolean(exact),
  };
}

/** Отфильтрованные строки + агрегаты. Модель не вызывается, 0 токенов. */
export async function queryTaskFacts(filters) {
  try {
    const { data } = await api.post(
      "/proverka/task-facts/query",
      taskFactsFilterBody(filters),
      { timeout: 30_000 }
    );
    return data;
  } catch (e) {
    throw new Error(formatError(e));
  }
}

/** Ответ DeepSeek по отфильтрованному срезу (или таблица, если useLlm=false). */
export async function askTaskFacts({ question, useLlm = true, maxRows, ...filters }) {
  try {
    const { data } = await api.post(
      "/proverka/task-facts/ask",
      {
        ...taskFactsFilterBody(filters),
        question: question || "",
        use_llm: Boolean(useLlm),
        max_rows: maxRows === "" || maxRows === undefined ? null : Number(maxRows),
      },
      { timeout: 120_000 }
    );
    return data;
  } catch (e) {
    throw new Error(formatError(e));
  }
}

// --- Проверка количества машин по операциям: независимый механизм, ---
// --- свои job'ы, свой API, ничего общего с /proverka/run по гектарам. ---

export async function runProverkaMachines({ reportDate, reportIds, forceDiffGroups }) {
  try {
    const { data } = await api.post(
      "/proverka/run-machines",
      {
        report_date: reportDate,
        report_ids: reportIds,
        force_diff_groups: forceDiffGroups || [],
      },
      { timeout: 60_000 }
    );
    return data;
  } catch (e) {
    throw new Error(formatError(e));
  }
}

export async function fetchProverkaMachinesJob(jobId) {
  const { data } = await api.get(`/proverka/machines/jobs/${jobId}`, { timeout: 15_000 });
  return data;
}

export async function cancelProverkaMachinesJob(jobId, reportIds = null) {
  try {
    const body = reportIds?.length ? { report_ids: reportIds } : {};
    const { data } = await api.post(
      `/proverka/machines/jobs/${encodeURIComponent(jobId)}/cancel`,
      body,
      { timeout: 15_000 }
    );
    return data;
  } catch (e) {
    throw new Error(formatError(e));
  }
}

export async function fetchProverkaMonitoring(reportDate) {
  const { data } = await api.get("/proverka/monitoring/status", {
    params: { report_date: reportDate },
    timeout: 15_000,
  });
  return data;
}

export async function enableProverkaMonitoring({ reportDate, cookie = null }) {
  try {
    const { data } = await api.post(
      "/proverka/monitoring/enable",
      cookie ? { cookie } : {},
      { params: { report_date: reportDate }, timeout: 30_000 }
    );
    return data;
  } catch (e) {
    throw new Error(formatError(e));
  }
}

export async function disableProverkaMonitoring(reportDate) {
  const { data } = await api.post("/proverka/monitoring/disable", null, {
    params: { report_date: reportDate },
    timeout: 15_000,
  });
  return data;
}

export function reportIdFromSummaryTitle(title) {
  const m = String(title || "").match(/^\s*(\d+(?:\.\d+)?)/);
  return m ? m[1] : "";
}

export function summaryIsExcel(item) {
  const fmt = String(item?.file_format || item?.format || "").trim().toLowerCase();
  if (fmt === "excel" || fmt === "xlsx") return true;
  const title = String(item?.title || item?.filename || "").toLowerCase();
  return title.includes("(excel)") || title.endsWith(".xlsx");
}

export function getGeneratedFileDownloadUrl(summaryDate, filename, accessToken = "") {
  const base = (api.defaults.baseURL || "").replace(/\/$/, "");
  let url = `${base}/reports/files/${encodeURIComponent(summaryDate)}/${encodeURIComponent(filename)}`;
  const token = String(accessToken || getStoredTokens().access || "").trim();
  if (token) {
    url += `?access_token=${encodeURIComponent(token)}`;
  }
  return url;
}

export function getPreviewUrl(item, accessToken = "") {
  if (summaryIsExcel(item)) return "#";
  const summaryDate = item.summary_date || item.summaryDate;
  const filename = item.filename || item.file_path;
  if (!summaryDate || !filename) return "#";
  return getGeneratedFileDownloadUrl(summaryDate, filename, accessToken);
}

export async function fetchSummariesByDate(summaryDate) {
  const data = await listGeneratedFiles(summaryDate);
  return (data.files || [])
    .map((f) => ({
      id: Number(f.summary_id),
      title: f.title,
      summary_date: summaryDate,
      filename: f.filename,
      report_id: f.report_id,
    }))
    .filter((row) => row.id > 0);
}

export async function fetchSummaryCellEdits(summaryId) {
  const { data } = await api.get(`/summaries/${summaryId}/cell-edits`);
  return data;
}

export async function saveSummaryCellEdit({
  summaryId,
  page,
  left,
  top,
  width,
  height,
  cellKey,
  cellText,
  replacement,
  fillColor = "",
  textColor = "",
}) {
  const { data } = await api.put(`/summaries/${summaryId}/cell-edits`, {
    page,
    left,
    top,
    width,
    height,
    cell_key: cellKey,
    cell_text: cellText,
    replacement,
    fill_color: fillColor || "",
    text_color: textColor || "",
  });
  return data;
}

export async function deleteSummaryCellEdit({ summaryId, editId }) {
  const { data } = await api.delete(`/summaries/${summaryId}/cell-edits/${editId}`);
  return data;
}

export async function fetchSummaryComments(summaryId) {
  const { data } = await api.get(`/summaries/${summaryId}/comments`);
  return data;
}

export async function saveSummaryComment({
  summaryId,
  page,
  x,
  y,
  commentAnchorY,
  cellKey,
  cellText,
  comment,
  applyToFuture = false
}) {
  const { data } = await api.put(`/summaries/${summaryId}/comments`, {
    page,
    x,
    y,
    comment_anchor_y: commentAnchorY ?? null,
    cell_key: cellKey,
    cell_text: String(cellText || ""),
    comment: String(comment || ""),
    apply_to_future: Boolean(applyToFuture)
  });
  return data;
}

export async function toggleSummaryCommentFuture({ summaryId, cellKey, enabled }) {
  const { data } = await api.put(`/summaries/${summaryId}/comments/future`, {
    cell_key: String(cellKey || ""),
    enabled: Boolean(enabled)
  });
  return data;
}

export async function deleteSummaryComment({ summaryId, commentId }) {
  const { data } = await api.delete(`/summaries/${summaryId}/comments/${commentId}`);
  return data;
}

export async function deleteSummary(summaryId) {
  const { data } = await api.delete(`/summaries/${summaryId}`);
  return data;
}

export async function fetchFieldSeasonState() {
  const { data } = await api.get("/field-season");
  return data;
}

export async function generateFieldSeasonSummary() {
  const { data } = await api.post("/field-season/generate");
  return data;
}

export async function fetchFieldSeasonTable(fieldId, options = {}) {
  const offset = Number(options?.offset || 0);
  const limit = Number(options?.limit || 200);
  const normalized = String(fieldId) === "all" ? 0 : Number(fieldId || 0);
  const { data } = await api.get("/field-season/table", {
    params: { field_id: normalized, offset, limit },
  });
  return data;
}

export function getFieldSeasonPdfUrl(fieldId, accessToken = "", fieldIds = []) {
  const base = (api.defaults.baseURL || "").replace(/\/$/, "");
  const normalized = String(fieldId) === "all" ? 0 : Number(fieldId || 0);
  let url = `${base}/field-season/pdf?field_id=${encodeURIComponent(normalized)}`;
  const ids = (Array.isArray(fieldIds) ? fieldIds : [])
    .map((x) => Number(x))
    .filter((x) => Number.isFinite(x) && x > 0);
  if (ids.length) url += `&field_ids=${encodeURIComponent(ids.join(","))}`;
  const token = String(accessToken || getStoredTokens().access || "").trim();
  if (token) url += `&access_token=${encodeURIComponent(token)}`;
  return url;
}

export function getFieldSeasonExcelUrl(fieldId, accessToken = "", fieldIds = []) {
  const base = (api.defaults.baseURL || "").replace(/\/$/, "");
  const normalized = String(fieldId) === "all" ? 0 : Number(fieldId || 0);
  let url = `${base}/field-season/excel?field_id=${encodeURIComponent(normalized)}`;
  const ids = (Array.isArray(fieldIds) ? fieldIds : [])
    .map((x) => Number(x))
    .filter((x) => Number.isFinite(x) && x > 0);
  if (ids.length) url += `&field_ids=${encodeURIComponent(ids.join(","))}`;
  const token = String(accessToken || getStoredTokens().access || "").trim();
  if (token) url += `&access_token=${encodeURIComponent(token)}`;
  return url;
}

function buildWorksSummaryQuery(params = {}) {
  const query = new URLSearchParams();
  const years = Array.isArray(params?.years) ? params.years : [];
  const groups = Array.isArray(params?.groups) ? params.groups : [];
  const crops = Array.isArray(params?.crops) ? params.crops : [];
  const firms = Array.isArray(params?.firms) ? params.firms : [];
  if (years.length) query.set("years", years.join(","));
  if (groups.length) query.set("groups", groups.join(","));
  if (crops.length) query.set("crops", crops.join(","));
  if (firms.length) query.set("firms", firms.join(","));
  if (params?.date_from) query.set("date_from", String(params.date_from));
  if (params?.date_to) query.set("date_to", String(params.date_to));
  if (Number.isFinite(Number(params?.offset))) query.set("offset", String(Number(params.offset)));
  if (Number.isFinite(Number(params?.limit))) query.set("limit", String(Number(params.limit)));
  return query.toString();
}

export async function fetchWorksSummaryState() {
  const { data } = await api.get("/field-season/works-summary");
  return data;
}

export async function generateWorksSummary() {
  const { data } = await api.post("/field-season/works-summary/generate");
  return data;
}

export async function fetchWorksSummaryGrid(params = {}) {
  const query = buildWorksSummaryQuery(params);
  const { data } = await api.get(`/field-season/works-summary/grid${query ? `?${query}` : ""}`);
  return data;
}

export async function fetchWorksSummaryTablePage(params = {}) {
  const query = buildWorksSummaryQuery(params);
  const { data } = await api.get(`/field-season/works-summary/table${query ? `?${query}` : ""}`);
  return data;
}

// Тянет все строки сводки по работам постранично (для клиентской агрегации в графиках).
// Ограничен на случай экстремально большой выборки, чтобы не подвесить вкладку.
const WORKS_ROWS_PAGE_LIMIT = 5000;
const WORKS_ROWS_HARD_CAP = 200000;

export async function fetchAllWorksSummaryRows(params = {}, { signal } = {}) {
  const rows = [];
  let offset = 0;
  for (;;) {
    if (signal?.aborted) break;
    const page = await fetchWorksSummaryTablePage({
      ...params,
      offset,
      limit: WORKS_ROWS_PAGE_LIMIT,
    });
    const pageRows = Array.isArray(page?.rows) ? page.rows : [];
    rows.push(...pageRows);
    if (!page?.has_more || rows.length >= WORKS_ROWS_HARD_CAP) break;
    offset = Number(page?.next_offset ?? offset + pageRows.length);
    if (!Number.isFinite(offset) || pageRows.length === 0) break;
  }
  return rows;
}

export async function downloadWorksSummaryExcel(params = {}) {
  const query = buildWorksSummaryQuery(params);
  const path = `/field-season/works-summary/excel${query ? `?${query}` : ""}`;
  const res = await api.get(path, { responseType: "blob" });
  const blob = res.data;
  const cd = res.headers["content-disposition"] || "";
  const m = /filename="?([^";]+)"?/i.exec(cd);
  const filename = m?.[1] || "works_summary.xlsx";
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export async function fetchReport1PageOrder(reportId = "1") {
  const rid = encodeURIComponent(String(reportId || "1"));
  const { data } = await api.get(`/page-order/${rid}`);
  return data;
}

export function getPageOrderSourcePdfUrl(reportId, summaryId, accessToken = "") {
  const rid = encodeURIComponent(String(reportId || "1"));
  const sid = encodeURIComponent(String(summaryId));
  const base = (api.defaults.baseURL || "").replace(/\/$/, "");
  let url = `${base}/page-order/${rid}/source-pdf?summary_id=${sid}`;
  const token = String(accessToken || getStoredTokens().access || "").trim();
  if (token) url += `&access_token=${encodeURIComponent(token)}`;
  return url;
}

export async function saveReport1PageOrder({
  reportId = "1",
  pageSlots,
  hiddenPages = [],
  referenceSummaryId = null,
  pageCount = null,
}) {
  const rid = encodeURIComponent(String(reportId || "1"));
  const { data } = await api.put(`/page-order/${rid}`, {
    page_slots: pageSlots,
    hidden_pages: hiddenPages,
    reference_summary_id: referenceSummaryId,
    page_count: pageCount,
  });
  return data;
}

export async function resetReport1PageOrder(reportId = "1") {
  const rid = encodeURIComponent(String(reportId || "1"));
  const { data } = await api.post(`/page-order/${rid}/reset`);
  return data;
}

export async function fetchManagedUsers() {
  const { data } = await api.get("/users/manage");
  return data;
}

export async function createManagedUser({ email, password, username, fullName, role = "user" }) {
  const { data } = await api.post("/users/manage", {
    email,
    password,
    username,
    full_name: fullName,
    role,
  });
  return data;
}

export async function fetchMyProfile() {
  const { data } = await api.get("/users/me/profile");
  return data;
}

export async function fetchUserProfile(userId) {
  const { data } = await api.get(`/users/manage/${userId}/profile`);
  return data;
}

export async function updateManagedUser(userId, payload) {
  const { data } = await api.patch(`/users/manage/${userId}`, payload);
  return data;
}

export async function changeMyPassword(currentPassword, newPassword) {
  await api.patch("/users/me/password", {
    current_password: currentPassword,
    new_password: newPassword,
  });
}

export async function fetchInterfaceSettings() {
  const { data } = await api.get("/interface-settings");
  return data;
}

export async function fetchInterfaceBackground(backgroundVersion) {
  const { data } = await api.get("/interface-settings/background", {
    params: backgroundVersion ? { v: backgroundVersion } : undefined,
    responseType: "blob",
  });
  return URL.createObjectURL(data);
}

export async function saveInterfaceSettings({ theme, customBackground = undefined }) {
  const body = { theme };
  if (customBackground !== undefined) {
    body.custom_background = customBackground;
  }
  try {
    const { data } = await api.put("/interface-settings", body);
    return data;
  } catch (e) {
    throw new Error(formatError(e));
  }
}

export async function fetchPdfHeaderLabels() {
  const { data } = await api.get("/pdf-header-labels");
  return data;
}

export async function savePdfHeaderLabels(items) {
  const { data } = await api.put("/pdf-header-labels", { items });
  return data;
}

export async function resetPdfHeaderLabels() {
  const { data } = await api.post("/pdf-header-labels/reset");
  return data;
}

export async function stripPdfHeaderGroupNumbers(items) {
  const { data } = await api.post("/pdf-header-labels/strip-numbers", { items });
  return data;
}

export async function downloadReport1PageOrderPreview(
  summaryId,
  pageSlots,
  hiddenPages = [],
  reportId = "1"
) {
  const rid = encodeURIComponent(String(reportId || "1"));
  const { data } = await api.post(
    `/page-order/${rid}/preview`,
    { summary_id: summaryId, page_slots: pageSlots, hidden_pages: hiddenPages },
    { responseType: "blob" }
  );
  return data;
}
