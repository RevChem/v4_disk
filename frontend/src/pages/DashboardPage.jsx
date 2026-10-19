import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import CommentRoundedIcon from "@mui/icons-material/CommentRounded";
import ReorderRoundedIcon from "@mui/icons-material/ReorderRounded";
import TitleRoundedIcon from "@mui/icons-material/TitleRounded";
import TuneRoundedIcon from "@mui/icons-material/TuneRounded";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Checkbox,
  Chip,
  CircularProgress,
  Collapse,
  FormControlLabel,
  FormGroup,
  Grid,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import CloudUploadRoundedIcon from "@mui/icons-material/CloudUploadRounded";
import FactCheckRoundedIcon from "@mui/icons-material/FactCheckRounded";
import EventRoundedIcon from "@mui/icons-material/EventRounded";
import ExpandLessRoundedIcon from "@mui/icons-material/ExpandLessRounded";
import ExpandMoreRoundedIcon from "@mui/icons-material/ExpandMoreRounded";

import PdfSummaryPanel from "../components/PdfSummaryPanel";
import { sortCatalogAsc, sortCatalogDesc } from "../utils/reportSort";
import ProverkaDialog from "../components/ProverkaDialog";
import HarvestCheckDialog from "../components/HarvestCheckDialog";
import ProverkaMachinesDialog from "../components/ProverkaMachinesDialog";
import PlansEditorDialog from "../PlansEditorDialog";
import DocumentCellEditorDialog from "../components/DocumentCellEditorDialog";
import SummaryCommentEditorDialog from "../components/SummaryCommentEditorDialog";
import FieldSeasonDialog from "../components/FieldSeasonDialog";
import WorksSummaryDialog from "../components/WorksSummaryDialog";
import Report1LayoutEditorDialog from "../components/Report1LayoutEditorDialog";
import PdfHeaderLabelsDialog from "../components/PdfHeaderLabelsDialog";
import HarvestGrossDialog from "../components/HarvestGrossDialog";
import Report27CustomDialog from "../components/Report27CustomDialog";
import {
  buildHarvestGrossPayload,
  loadHarvestGrossForm,
  saveHarvestGrossForm,
  fetchAvailableDates,
  fetchReportCatalog,
  generateReports,
  getStoredTokens,
  listGeneratedFiles,
  startGeneratedFileDownload,
  summaryIsExcel,
} from "../api";
import dayjs from "dayjs";

const CROPWISE_DIFF_MIN_HA = 1;
// Сезон: ошибкой считается расхождение больше 10 га.
const CROPWISE_SEASON_DIFF_MIN_HA = 10;

function cropwiseSeasonDiffCounts(row) {
  const delta = Number(row?.delta);
  if (Number.isFinite(delta)) return delta > CROPWISE_SEASON_DIFF_MIN_HA;
  return row?.status === "unread";
}

function cropwiseDiffCounts(row) {
  const delta = Number(row?.delta);
  if (Number.isFinite(delta)) return Math.abs(delta) >= CROPWISE_DIFF_MIN_HA;
  if (row?.status === "missing") {
    const cropwise = Number(row.cropwise_ha);
    return Number.isFinite(cropwise) && Math.abs(cropwise) >= CROPWISE_DIFF_MIN_HA;
  }
  return row?.status === "unread" || row?.status === "diff";
}

function todayIso() {
  return new Date().toISOString().slice(0, 10);
}

export default function DashboardPage({
  user,
  generationQueue = [],
  onGenerationQueued,
  summaryDate: summaryDateProp,
  onSummaryDateChange,
  onGenerationDiagnostics,
  onProverkaComplete,
  onOpenLogs,
  onHeaderToolsChange,
  onOpenTaskFacts,
  compareCropwise = false,
  compareCropwiseSeason = false,
  equipmentRequest = 0,
  showDataErrors = false,
}) {
  const [summaryDateLocal, setSummaryDateLocal] = useState(todayIso());
  const summaryDate = summaryDateProp ?? summaryDateLocal;
  const setSummaryDate = onSummaryDateChange ?? setSummaryDateLocal;
  const [availableDates, setAvailableDates] = useState([]);
  const [catalog, setCatalog] = useState([]);
  const [selectedIds, setSelectedIds] = useState([]);
  const [files, setFiles] = useState([]);
  const [alertsByReport, setAlertsByReport] = useState({});
  const [filesLoading, setFilesLoading] = useState(false);
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");
  const [enqueueing, setEnqueueing] = useState(false);
  const [plansOpen, setPlansOpen] = useState(false);
  const [proverkaOpen, setProverkaOpen] = useState(false);
  const [harvestCheckOpen, setHarvestCheckOpen] = useState(false);
  const [proverkaMachinesOpen, setProverkaMachinesOpen] = useState(false);
  const [reportPickerExpanded, setReportPickerExpanded] = useState(true);
  const [failedItems, setFailedItems] = useState([]);
  const [generationLogs, setGenerationLogs] = useState([]);
  const [failedJobId, setFailedJobId] = useState(null);
  const [fieldSeasonOpen, setFieldSeasonOpen] = useState(false);
  const [worksOpen, setWorksOpen] = useState(false);
  const [pageOrderOpen, setPageOrderOpen] = useState(false);
  const [pdfLabelsOpen, setPdfLabelsOpen] = useState(false);
  const [cellEditOpen, setCellEditOpen] = useState(false);
  const [cellEditItem, setCellEditItem] = useState(null);
  const [commentsOpen, setCommentsOpen] = useState(false);
  const [commentsInitialSummaryId, setCommentsInitialSummaryId] = useState(null);
  const [harvestGrossOpen, setHarvestGrossOpen] = useState(false);
  const [harvestGrossPendingIds, setHarvestGrossPendingIds] = useState([]);
  const [harvestGrossForm, setHarvestGrossForm] = useState(() => loadHarvestGrossForm());
  const [report27CustomOpen, setReport27CustomOpen] = useState(false);
  const [report27CustomDateFrom, setReport27CustomDateFrom] = useState("");
  const [report27CustomDateTo, setReport27CustomDateTo] = useState("");
  const [report27CustomTimeFrom, setReport27CustomTimeFrom] = useState("06:00");
  const [report27CustomTimeTo, setReport27CustomTimeTo] = useState("06:00");
  const [report27CustomGenerating, setReport27CustomGenerating] = useState(false);
  const [report27CustomError, setReport27CustomError] = useState("");

  useEffect(() => {
    if (!equipmentRequest) return;
    setProverkaMachinesOpen(true);
  }, [equipmentRequest]);

  const canPlans = user?.role === "developer" || user?.role === "admin" || user?.role === "user";
  const canProverka = user?.role === "developer";
  const canSitcentrOps = user?.role === "developer" || user?.role === "sitcentr";
  const canComments = canPlans;
  const canCellEdit = user?.role === "developer" || user?.role === "user";
  const accessToken = getStoredTokens().access || "";

  const prevQueueRef = useRef([]);

  const activeJobsForDate = useMemo(
    () =>
      generationQueue.filter(
        (j) =>
          j.summary_date === summaryDate && (j.status === "pending" || j.status === "running")
      ),
    [generationQueue, summaryDate]
  );

  const loadCatalog = useCallback(async () => {
    try {
      const data = await fetchReportCatalog();
      const items = sortCatalogDesc(data.items || []);
      setCatalog(items);
      setSelectedIds(items.filter((i) => i.default_selected !== false).map((i) => i.id));
    } catch (e) {
      setError(String(e.message || e));
    }
  }, []);

  const loadFiles = useCallback(async () => {
    setFilesLoading(true);
    try {
      const data = await listGeneratedFiles(summaryDate);
      setFiles(data.files || []);
      setAlertsByReport(data.alerts || {});
    } catch {
      setFiles([]);
      setAlertsByReport({});
    } finally {
      setFilesLoading(false);
    }
  }, [summaryDate]);

  const loadDates = useCallback(async () => {
    try {
      const data = await fetchAvailableDates();
      setAvailableDates(data.items || []);
    } catch {
      setAvailableDates([]);
    }
  }, []);

  useEffect(() => {
    loadCatalog();
  }, [loadCatalog]);

  useEffect(() => {
    loadFiles();
    loadDates();
  }, [loadFiles, loadDates]);

  useEffect(() => {
    const prev = prevQueueRef.current;
    const prevActive = prev.filter(
      (j) =>
        j.summary_date === summaryDate && (j.status === "pending" || j.status === "running")
    ).length;
    const shouldReload = prevActive > 0 && activeJobsForDate.length === 0;
    for (const job of generationQueue) {
      const old = prev.find((item) => item.id === job.id);
      if (!old || (old.status !== "pending" && old.status !== "running")) continue;
      if (job.status !== "completed") continue;
      const excelRows = (job.created || []).filter(
        (row) => String(row?.report_id) === "28" && row.xlsx_filename
      );
      if (!excelRows.length || !job.summary_date) continue;
      for (const row of excelRows) {
        startGeneratedFileDownload(job.summary_date, row.xlsx_filename);
      }
      setInfo("Сводка 28 готова. Файл Excel скачивается.");
    }
    prevQueueRef.current = generationQueue;

    if (shouldReload) {
      loadFiles();
      loadDates();
    }
    const latestDone = generationQueue.find(
      (j) =>
        j.summary_date === summaryDate &&
        (j.status === "completed" || j.status === "failed") &&
        (j.failed?.length || j.logs?.length)
    );
    if (latestDone) {
      setFailedItems(latestDone.failed || []);
      setGenerationLogs(latestDone.logs || []);
      setFailedJobId(latestDone.id || latestDone.job_id || null);
    }
  }, [generationQueue, summaryDate, activeJobsForDate.length, loadFiles, loadDates]);

  useEffect(() => {
    onGenerationDiagnostics?.({ failedItems, logs: generationLogs });
  }, [failedItems, generationLogs, onGenerationDiagnostics]);

  const visibleCatalog = useMemo(
    () => sortCatalogAsc(catalog.filter((item) => item.picker_visible !== false)),
    [catalog]
  );

  const headerToolBtnSx = {
    fontSize: "0.75rem",
    py: 0.35,
    px: 1.25,
    minHeight: 28,
    textTransform: "none",
  };

  const sidebarToolBtnSx = {
    fontSize: "0.8125rem",
    py: 0.65,
    textTransform: "none",
  };

  const commentItems = useMemo(
    () =>
      files
        .filter((f) => f.summary_id && !summaryIsExcel(f))
        .map((f) => ({
          id: Number(f.summary_id),
          title: f.title || f.filename,
          summary_date: summaryDate,
          filename: f.filename,
        })),
    [files, summaryDate]
  );

  function openCommentsDialog(summaryId = null) {
    setCommentsInitialSummaryId(summaryId);
    setCommentsOpen(true);
  }

  useEffect(() => {
    if (!canPlans || !onHeaderToolsChange) {
      onHeaderToolsChange?.(null);
      return undefined;
    }
    onHeaderToolsChange(
      <>
        <Button
          size="small"
          variant="outlined"
          sx={headerToolBtnSx}
          startIcon={<TuneRoundedIcon sx={{ fontSize: 16 }} />}
          onClick={() => setPlansOpen(true)}
        >
          Редактор планов
        </Button>
        <Button
          size="small"
          variant="outlined"
          sx={headerToolBtnSx}
          startIcon={<ReorderRoundedIcon sx={{ fontSize: 16 }} />}
          onClick={() => setPageOrderOpen(true)}
        >
          Порядок страниц
        </Button>
        <Button
          size="small"
          variant="outlined"
          sx={headerToolBtnSx}
          startIcon={<TitleRoundedIcon sx={{ fontSize: 16 }} />}
          onClick={() => setPdfLabelsOpen(true)}
        >
          Подписи PDF
        </Button>
        <Button
          size="small"
          variant="outlined"
          sx={headerToolBtnSx}
          startIcon={<CommentRoundedIcon sx={{ fontSize: 16 }} />}
          onClick={() => openCommentsDialog()}
          disabled={commentItems.length === 0}
        >
          Комментарии к сводке
        </Button>
      </>
    );
    return () => onHeaderToolsChange?.(null);
  }, [canPlans, commentItems.length, onHeaderToolsChange]);

  function toggleReport(id) {
    setSelectedIds((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  }

  const cropwiseByReport = useMemo(() => {
    const map = {};
    const jobs = generationQueue
      .filter(
        (job) =>
          job.summary_date === summaryDate &&
          job.status === "completed" &&
          Array.isArray(job.cropwise_diffs),
      )
      .slice()
      .sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")));
    for (const job of jobs) {
      const requested = (job.report_ids || []).map(String).filter(Boolean);
      const createdIds = (job.created || [])
        .map((item) => String(item?.report_id || ""))
        .filter(Boolean);
      for (const rid of requested.length ? requested : createdIds) {
        delete map[String(rid)];
      }
      for (const row of job.cropwise_diffs) {
        const rid = String(row.report_id || "");
        if (!rid || !cropwiseDiffCounts(row)) continue;
        if (!map[rid]) map[rid] = [];
        map[rid].push(row);
      }
    }
    return map;
  }, [generationQueue, summaryDate]);

  const cropwiseSeasonByReport = useMemo(() => {
    const map = {};
    const jobs = generationQueue
      .filter(
        (job) =>
          job.summary_date === summaryDate &&
          job.status === "completed" &&
          Array.isArray(job.cropwise_season_diffs),
      )
      .slice()
      .sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")));
    for (const job of jobs) {
      const requested = (job.report_ids || []).map(String).filter(Boolean);
      const createdIds = (job.created || [])
        .map((item) => String(item?.report_id || ""))
        .filter(Boolean);
      for (const rid of requested.length ? requested : createdIds) {
        delete map[String(rid)];
      }
      for (const row of job.cropwise_season_diffs) {
        const rid = String(row.report_id || "");
        if (!rid || !cropwiseSeasonDiffCounts(row)) continue;
        if (!map[rid]) map[rid] = [];
        map[rid].push(row);
      }
    }
    return map;
  }, [generationQueue, summaryDate]);

  function setAllReportsSelected(checked) {
    setSelectedIds(checked ? visibleCatalog.map((i) => i.id) : []);
  }

  async function enqueueReports(ids, options = {}) {
    setEnqueueing(true);
    setError("");
    setInfo("");
    setFailedItems([]);
    setGenerationLogs([]);
    setFailedJobId(null);
    try {
      const res = await generateReports(summaryDate, ids, {
        ...options,
        compareCropwise,
        compareCropwiseSeason,
      });
      setInfo(`Задача поставлена в очередь (${res.job_id?.slice(0, 8) || "—"}…)`);
      onGenerationQueued?.();
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setEnqueueing(false);
    }
  }

  async function handleGenerate() {
    if (!selectedIds.length) return;
    if (selectedIds.some((id) => id === "10" || id === "13" || id === "1")) {
      setHarvestGrossPendingIds([...selectedIds]);
      setHarvestGrossForm(loadHarvestGrossForm());
      setHarvestGrossOpen(true);
      return;
    }
    await enqueueReports(selectedIds);
  }

  function updateHarvestGrossField(section, cropName, firmName, field, value) {
    setHarvestGrossForm((prev) => {
      const next = {
        ...prev,
        [section]: {
          ...prev[section],
          [cropName]: {
            ...(prev[section]?.[cropName] || {}),
            [firmName]: {
              ...(prev[section]?.[cropName]?.[firmName] || { season_t: "", day_t: "" }),
              [field]: value,
            },
          },
        },
      };
      saveHarvestGrossForm(next);
      return next;
    });
  }

  async function confirmHarvestGrossModal() {
    const ids = harvestGrossPendingIds.length ? harvestGrossPendingIds : selectedIds;
    saveHarvestGrossForm(harvestGrossForm);
    const payload = buildHarvestGrossPayload(harvestGrossForm);
    setHarvestGrossOpen(false);
    await enqueueReports(ids, payload ? { reportHarvestGross: payload } : {});
  }

  function openReport27Custom() {
    const base = summaryDate || todayIso();
    const prev = dayjs(base).subtract(1, "day").format("YYYY-MM-DD");
    setReport27CustomDateFrom(prev);
    setReport27CustomDateTo(base);
    setReport27CustomTimeFrom("06:00");
    setReport27CustomTimeTo("06:00");
    setReport27CustomError("");
    setReport27CustomOpen(true);
  }

  async function handleGenerateReport27Custom() {
    if (!canSitcentrOps) return;
    const dateFrom = String(report27CustomDateFrom || "").trim();
    const dateTo = String(report27CustomDateTo || "").trim();
    const timeFrom = String(report27CustomTimeFrom || "").trim();
    const timeTo = String(report27CustomTimeTo || "").trim();
    if (!dateFrom || !dateTo) {
      setReport27CustomError("Укажите даты начала и конца периода.");
      return;
    }
    if (!/^\d{2}:\d{2}$/.test(timeFrom) || !/^\d{2}:\d{2}$/.test(timeTo)) {
      setReport27CustomError("Укажите время «от» и «до» в формате HH:MM.");
      return;
    }
    const fromDt = dayjs(`${dateFrom}T${timeFrom}`);
    const toDt = dayjs(`${dateTo}T${timeTo}`);
    if (!fromDt.isValid() || !toDt.isValid()) {
      setReport27CustomError("Некорректная дата или время.");
      return;
    }
    if (!fromDt.isBefore(toDt)) {
      setReport27CustomError("Начало периода должно быть раньше конца.");
      return;
    }
    setReport27CustomGenerating(true);
    setReport27CustomError("");
    setError("");
    setInfo("");
    try {
      const res = await generateReports(dateTo, ["27"], {
        report27Cutoff: timeTo,
        report27TimeFrom: timeFrom,
        report27DateFrom: dateFrom,
        report27DateTo: dateTo,
      });
      setInfo(
        `Сводки 27 и 27.1 Excel (${dateFrom} ${timeFrom} — ${dateTo} ${timeTo}) ` +
          `поставлены в очередь (${res.job_id?.slice(0, 8) || "—"}…).`
      );
      setReport27CustomOpen(false);
      setSummaryDate(dateTo);
      onGenerationQueued?.();
    } catch (e) {
      const msg = String(e.message || e || "Не удалось поставить сводку 27 в очередь.");
      setReport27CustomError(msg);
      setError(msg);
    } finally {
      setReport27CustomGenerating(false);
    }
  }

  return (
    <>
      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      {info && <Alert severity="info" sx={{ mb: 2 }}>{info}</Alert>}
      {activeJobsForDate.length > 0 && (
        <Alert severity="warning" sx={{ mb: 2 }}>
          Генерация за {summaryDate}: {activeJobsForDate.length} задач(а) в работе.
        </Alert>
      )}

      <Grid container spacing={3}>
        {/* Слева — дата и генерация (как в архиве, md=4) */}
        <Grid size={{ xs: 12, md: 4 }}>
          <Card elevation={0} sx={{ border: 1, borderColor: "divider" }}>
            <CardContent sx={{ p: 2.5 }}>
              <Stack spacing={2}>
                <Stack direction="row" alignItems="center" spacing={1}>
                  <EventRoundedIcon color="primary" fontSize="small" />
                  <Typography variant="h6" fontWeight={600}>
                    Дата
                  </Typography>
                </Stack>

                <TextField
                  label="Календарь"
                  type="date"
                  value={summaryDate}
                  onChange={(e) => setSummaryDate(e.target.value)}
                  InputLabelProps={{ shrink: true }}
                  fullWidth
                  size="small"
                />

                <Typography variant="body2" color="text.secondary">
                  На выбранную дату: {files.length} сводок
                </Typography>

                <Box sx={{ p: 1.25, border: 1, borderColor: "divider", borderRadius: 1.5 }}>
                  <Stack direction="row" justifyContent="space-between" alignItems="center">
                    <Typography variant="body2" color="text.secondary">
                      Выбрано: {selectedIds.length} из {visibleCatalog.length}
                    </Typography>
                    <Button
                      size="small"
                      variant="text"
                      onClick={() => setReportPickerExpanded((p) => !p)}
                      startIcon={
                        reportPickerExpanded ? (
                          <ExpandLessRoundedIcon fontSize="small" />
                        ) : (
                          <ExpandMoreRoundedIcon fontSize="small" />
                        )
                      }
                    >
                      {reportPickerExpanded ? "Свернуть" : "Развернуть"}
                    </Button>
                  </Stack>
                  <Collapse in={reportPickerExpanded}>
                    <FormControlLabel
                      control={
                        <Checkbox
                          size="small"
                          checked={
                            selectedIds.length === visibleCatalog.length && visibleCatalog.length > 0
                          }
                          indeterminate={
                            selectedIds.length > 0 && selectedIds.length < visibleCatalog.length
                          }
                          onChange={(e) => setAllReportsSelected(e.target.checked)}
                        />
                      }
                      label="Выбрать все"
                    />
                    <FormGroup
                      className="report-picker-list"
                      sx={{
                        maxHeight: 420,
                        overflowY: "auto",
                        overflowX: "hidden",
                        pr: 0.5,
                      }}
                    >
                      {visibleCatalog.map((item) => (
                        <FormControlLabel
                          key={item.id}
                          sx={{ alignItems: "flex-start", mr: 0, display: "flex" }}
                          control={
                            <Checkbox
                              size="small"
                              checked={selectedIds.includes(item.id)}
                              onChange={() => toggleReport(item.id)}
                            />
                          }
                          label={
                            <Typography variant="body2" sx={{ pt: 0.75, lineHeight: 1.35 }}>
                              {item.title}
                            </Typography>
                          }
                        />
                      ))}
                    </FormGroup>
                  </Collapse>
                  {availableDates.length > 0 && (
                    <Stack direction="row" spacing={0.75} flexWrap="wrap" useFlexGap sx={{ mt: 1 }}>
                      {availableDates.slice(0, 8).map((d) => {
                        const dateStr = d.summary_date;
                        const selected = dateStr === summaryDate;
                        return (
                          <Chip
                            key={dateStr}
                            size="small"
                            variant={selected ? "filled" : "outlined"}
                            color={selected ? "primary" : "default"}
                            clickable
                            onClick={() => setSummaryDate(dateStr)}
                            label={`${dateStr} · ${d.count}`}
                          />
                        );
                      })}
                    </Stack>
                  )}
                </Box>

                <Button
                  variant="contained"
                  fullWidth
                  onClick={handleGenerate}
                  disabled={enqueueing || !selectedIds.length}
                  startIcon={
                    enqueueing ? <CircularProgress size={16} color="inherit" /> : <CloudUploadRoundedIcon />
                  }
                >
                  {enqueueing ? "Создание…" : "Создать выбранные сводки"}
                </Button>

                {canSitcentrOps && (
                  <Button
                    variant="outlined"
                    fullWidth
                    size="small"
                    sx={sidebarToolBtnSx}
                    onClick={openReport27Custom}
                  >
                    27·Excel — рейтинг за период
                  </Button>
                )}

                {canPlans && (
                  <>
                    <Button
                      variant="outlined"
                      fullWidth
                      size="small"
                      sx={sidebarToolBtnSx}
                      onClick={() => setFieldSeasonOpen(true)}
                    >
                      Поля с начала сезона
                    </Button>
                    <Button
                      variant="outlined"
                      fullWidth
                      size="small"
                      sx={sidebarToolBtnSx}
                      onClick={() => setWorksOpen(true)}
                    >
                      Работы 2025/2026
                    </Button>
                    {canProverka && (
                      <Button
                        variant="outlined"
                        fullWidth
                        size="small"
                        sx={sidebarToolBtnSx}
                        onClick={() => setProverkaOpen(true)}
                        startIcon={<FactCheckRoundedIcon sx={{ fontSize: 18 }} />}
                      >
                        Проверка сводок
                      </Button>
                    )}
                    {canProverka && (
                      <Button
                        variant="outlined"
                        fullWidth
                        size="small"
                        sx={sidebarToolBtnSx}
                        onClick={() => setHarvestCheckOpen(true)}
                        startIcon={<FactCheckRoundedIcon sx={{ fontSize: 18 }} />}
                      >
                        Сверка
                      </Button>
                    )}
                  </>
                )}

              </Stack>
            </CardContent>
          </Card>
        </Grid>

        {/* Справа — список PDF (как в архиве, md=8) */}
        <Grid size={{ xs: 12, md: 8 }}>
          <PdfSummaryPanel
            summaryDate={summaryDate}
            files={files}
            loading={filesLoading}
            onError={setError}
            canCellEdit={canCellEdit}
            onCellEdit={(item) => {
              setCellEditItem(item);
              setCellEditOpen(true);
            }}
            canComments={canComments}
            onComment={(item) => openCommentsDialog(item.id)}
            canDelete={canComments}
            cropwiseByReport={cropwiseByReport}
            cropwiseSeasonByReport={cropwiseSeasonByReport}
            alertsByReport={
              showDataErrors
                ? Object.fromEntries(
                    Object.entries(alertsByReport)
                      .map(([id, items]) => [
                        id,
                        (items || []).filter((a) => !String(a?.message || "").includes("AREA_GT_FIELD")),
                      ])
                      .filter(([, items]) => items.length),
                  )
                : {}
            }
            onDeleted={() => {
              loadFiles();
              loadDates();
            }}
          />
        </Grid>
      </Grid>

      {failedItems.length > 0 && (
        <Stack spacing={2} sx={{ mt: 3 }}>
          <Card elevation={0} sx={{ border: 1, borderColor: "divider" }}>
            <CardContent sx={{ p: 2.5 }}>
              <Typography variant="h6" sx={{ mb: 2 }}>
                Ошибки генерации
              </Typography>
              <Stack spacing={1.5}>
                {failedItems.map((item, idx) => (
                  <Alert key={`${item.report_id}-${idx}`} severity="error" variant="outlined">
                    <Typography variant="subtitle2" sx={{ fontWeight: 600 }}>
                      {item.report_id ? `Сводка ${item.report_id}` : "Сводка"}
                      {item.title ? ` — ${item.title}` : ""}
                    </Typography>
                    <Typography variant="body2" sx={{ mt: 0.5, whiteSpace: "pre-wrap" }}>
                      {item.error || "Неизвестная ошибка"}
                    </Typography>
                    {failedJobId && onOpenLogs && (
                      <Button
                        size="small"
                        sx={{ mt: 1, px: 0 }}
                        onClick={() =>
                          onOpenLogs({
                            tab: "reports",
                            jobId: failedJobId,
                            reportId: item.report_id,
                          })
                        }
                      >
                        Подробнее в логах
                      </Button>
                    )}
                  </Alert>
                ))}
              </Stack>
            </CardContent>
          </Card>
        </Stack>
      )}

      <HarvestGrossDialog
        open={harvestGrossOpen}
        onClose={() => {
          if (!enqueueing) setHarvestGrossOpen(false);
        }}
        form={harvestGrossForm}
        onFieldChange={updateHarvestGrossField}
        generating={enqueueing}
        onSubmit={() => void confirmHarvestGrossModal()}
      />
      <Report27CustomDialog
        open={report27CustomOpen}
        onClose={() => {
          if (!report27CustomGenerating) setReport27CustomOpen(false);
        }}
        dateFrom={report27CustomDateFrom}
        dateTo={report27CustomDateTo}
        timeFrom={report27CustomTimeFrom}
        timeTo={report27CustomTimeTo}
        onDateFromChange={setReport27CustomDateFrom}
        onDateToChange={setReport27CustomDateTo}
        onTimeFromChange={setReport27CustomTimeFrom}
        onTimeToChange={setReport27CustomTimeTo}
        generating={report27CustomGenerating}
        error={report27CustomError}
        onSubmit={() => void handleGenerateReport27Custom()}
      />
      <PlansEditorDialog open={plansOpen} onClose={() => setPlansOpen(false)} summaryDate={summaryDate} />
      <ProverkaDialog
        open={proverkaOpen}
        onClose={() => setProverkaOpen(false)}
        summaryDate={summaryDate}
        onComplete={onProverkaComplete}
        onOpenTaskFacts={onOpenTaskFacts}
        showDataErrors={showDataErrors}
      />
      <HarvestCheckDialog
        open={harvestCheckOpen}
        onClose={() => setHarvestCheckOpen(false)}
        summaryDate={summaryDate}
      />
      <ProverkaMachinesDialog
        open={proverkaMachinesOpen}
        onClose={() => setProverkaMachinesOpen(false)}
        summaryDate={summaryDate}
        runToken={equipmentRequest}
      />
      <FieldSeasonDialog
        open={fieldSeasonOpen}
        onClose={() => setFieldSeasonOpen(false)}
        accessToken={accessToken}
        onError={setError}
        onInfo={setInfo}
      />
      <WorksSummaryDialog
        open={worksOpen}
        onClose={() => setWorksOpen(false)}
        onError={setError}
        onInfo={setInfo}
      />
      <Report1LayoutEditorDialog
        open={pageOrderOpen}
        onClose={() => setPageOrderOpen(false)}
        summaryDate={summaryDate}
        accessToken={accessToken}
        generationBatchReportIds={selectedIds}
        catalog={catalog}
      />
      <PdfHeaderLabelsDialog open={pdfLabelsOpen} onClose={() => setPdfLabelsOpen(false)} />
      <DocumentCellEditorDialog
        open={cellEditOpen}
        onClose={() => {
          setCellEditOpen(false);
          setCellEditItem(null);
        }}
        summaryItem={cellEditItem}
        accessToken={accessToken}
        onSaved={() => loadFiles()}
      />
      <SummaryCommentEditorDialog
        open={commentsOpen}
        onClose={() => {
          setCommentsOpen(false);
          setCommentsInitialSummaryId(null);
        }}
        items={commentItems}
        initialSummaryId={commentsInitialSummaryId}
        accessToken={accessToken}
        onSaved={() => loadFiles()}
      />
    </>
  );
}
