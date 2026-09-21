import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Checkbox,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControlLabel,
  FormGroup,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Typography,
} from "@mui/material";
import FactCheckRoundedIcon from "@mui/icons-material/FactCheckRounded";
import DownloadRoundedIcon from "@mui/icons-material/DownloadRounded";

import AiReportPanel from "./AiReportPanel";
import { sortCatalogDesc } from "../utils/reportSort";
import { formatDateTimeRu } from "../utils/formatDuration";
import {
  cancelProverkaJob,
  downloadProverkaExcel,
  fetchLatestProverkaJob,
  fetchProverkaCatalog,
  fetchProverkaJob,
  runProverka,
} from "../api";

export default function ProverkaDialog({ open, onClose, summaryDate, onComplete }) {
  const [catalog, setCatalog] = useState([]);
  const [selectedIds, setSelectedIds] = useState([]);
  const [cookie, setCookie] = useState("");
  const [jobId, setJobId] = useState(null);
  const [job, setJob] = useState(null);
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);
  const [checkedAt, setCheckedAt] = useState(null);
  const runningRef = useRef(false);
  runningRef.current = running;
  const [cancelling, setCancelling] = useState(false);
  const [aiAnalysis, setAiAnalysis] = useState(false);

  const sortedCatalog = useMemo(() => sortCatalogDesc(catalog), [catalog]);

  const loadCatalog = useCallback(async () => {
    try {
      const data = await fetchProverkaCatalog();
      const items = sortCatalogDesc(data.items || []);
      setCatalog(items);
      // выбор сохраняется между открытиями окна; по умолчанию — все сводки
      setSelectedIds((prev) => {
        const kept = prev.filter((id) => items.some((i) => i.id === id));
        return kept.length ? kept : items.map((i) => i.id);
      });
    } catch (e) {
      setError(String(e.message || e));
    }
  }, []);

  // Подтягиваем последний запуск за выбранную дату: результат не теряется
  // при закрытии окна; если запуск ещё идёт — продолжаем следить за ним.
  const restoreLatest = useCallback(async () => {
    try {
      const latest = await fetchLatestProverkaJob(summaryDate);
      if (runningRef.current) return; // уже идёт запуск из этого окна
      if (!latest?.job_id) {
        setJobId(null);
        setJob(null);
        setCheckedAt(null);
        return;
      }
      const st = await fetchProverkaJob(latest.job_id);
      if (runningRef.current) return;
      setJobId(latest.job_id);
      setJob(st);
      setCheckedAt(latest.checked_at || null);
      setRunning(st.status === "pending" || st.status === "running");
      if (st.status === "failed") setError(st.error || "Ошибка проверки");
    } catch {
      // не критично: окно откроется с тем, что уже есть в памяти
    }
  }, [summaryDate]);

  useEffect(() => {
    if (open) {
      setError("");
      loadCatalog();
      restoreLatest();
    }
  }, [open, loadCatalog, restoreLatest]);

  useEffect(() => {
    if (!jobId || !running) return undefined;
    let cancelled = false;
    const tick = async () => {
      try {
        const st = await fetchProverkaJob(jobId);
        if (cancelled) return;
        setJob(st);
        if (st.status === "completed" || st.status === "failed" || st.status === "cancelled") {
          setRunning(false);
          setCheckedAt(new Date().toISOString());
          if (st.status === "failed") {
            setError(st.error || "Ошибка проверки");
          }
          if (st.status === "cancelled") {
            setError("");
          }
          onComplete?.();
        }
      } catch (e) {
        if (!cancelled) {
          setError(String(e.message || e));
          setRunning(false);
        }
      }
    };
    tick();
    const id = setInterval(tick, 2500);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [jobId, running, onComplete]);

  function toggleId(id) {
    setSelectedIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]
    );
  }

  function setAllSelected(checked) {
    setSelectedIds(checked ? sortedCatalog.map((i) => i.id) : []);
  }

  async function handleRun() {
    setError("");
    if (!selectedIds.length) {
      setError("Выберите хотя бы одну сводку");
      return;
    }
    if (!cookie.trim()) {
      setError("Введите cookie");
      return;
    }
    setRunning(true);
    setJob(null);
    try {
      const res = await runProverka({
        reportDate: summaryDate,
        reportIds: selectedIds,
        cookie: cookie.trim(),
        compareMode: "cropwise",
        seasonOverrides: {},
        aiAnalysis,
      });
      setJobId(res.job_id);
    } catch (e) {
      setError(String(e.message || e));
      setRunning(false);
    }
  }

  async function handleCancel(reportIds = null) {
    if (!jobId) return;
    setCancelling(true);
    setError("");
    try {
      const st = await cancelProverkaJob(jobId, reportIds);
      setJob(st);
      if (st.status === "cancelled") {
        setRunning(false);
        onComplete?.();
      }
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setCancelling(false);
    }
  }

  async function handleDownload() {
    if (!jobId) return;
    try {
      const blob = await downloadProverkaExcel(jobId);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = job?.excel_filename || `proverka_${summaryDate}.xlsx`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(String(e.message || e));
    }
  }

  const completed = job?.status === "completed";
  const crossCheckRows = job?.cross_check_rows ?? job?.cross_check?.operations ?? [];
  const cropwiseRows = useMemo(() => job?.summary_rows || [], [job?.summary_rows]);

  const crossCheckPeerIds = useMemo(() => {
    const ids = new Set();
    for (const row of crossCheckRows) {
      for (const rid of row.active_reports || row.peer_reports || []) {
        ids.add(String(rid));
      }
    }
    return [...ids].sort((a, b) => {
      const na = Number(a);
      const nb = Number(b);
      if (Number.isFinite(na) && Number.isFinite(nb)) return nb - na;
      return String(b).localeCompare(String(a), undefined, { numeric: true });
    });
  }, [crossCheckRows]);

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="md">
      <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1 }}>
        <FactCheckRoundedIcon color="primary" />
        Проверка сводок
      </DialogTitle>
      <DialogContent dividers>
        <Stack spacing={2}>
          <Typography variant="body2" color="text.secondary">
            Дата отчёта: <strong>{summaryDate}</strong> · Сводки ↔ Cropwise UI (Отчёт об агро работах машин)
          </Typography>

          <TextField
            placeholder="_cropio_ses=…; crossdomain_auth=…"
            value={cookie}
            onChange={(e) => setCookie(e.target.value)}
            fullWidth
            size="small"
            disabled={running}
            sx={{ "& .MuiInputBase-root": { fontSize: "0.8rem" } }}
          />

          <FormControlLabel
            control={
              <Checkbox
                size="small"
                checked={aiAnalysis}
                onChange={(e) => setAiAnalysis(e.target.checked)}
                disabled={running}
              />
            }
            label="AI-разбор (DeepSeek) после проверки"
          />

          <Box>
            <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 0.5 }}>
              <Typography variant="subtitle2">Сводки для проверки</Typography>
              <Typography variant="caption" color="text.secondary">
                {selectedIds.length} / {sortedCatalog.length}
              </Typography>
            </Stack>
            <FormControlLabel
              control={
                <Checkbox
                  size="small"
                  checked={selectedIds.length === sortedCatalog.length && sortedCatalog.length > 0}
                  indeterminate={
                    selectedIds.length > 0 && selectedIds.length < sortedCatalog.length
                  }
                  onChange={(e) => setAllSelected(e.target.checked)}
                  disabled={running}
                />
              }
              label="Выбрать все"
            />
            <FormGroup sx={{ maxHeight: 200, overflowY: "auto" }}>
              {sortedCatalog.map((item) => (
                <FormControlLabel
                  key={item.id}
                  control={
                    <Checkbox
                      size="small"
                      checked={selectedIds.includes(item.id)}
                      onChange={() => toggleId(item.id)}
                      disabled={running}
                    />
                  }
                  label={<Typography variant="body2">{item.title}</Typography>}
                />
              ))}
            </FormGroup>
          </Box>

          {running && (
            <Stack spacing={1}>
              <Stack direction="row" spacing={1.5} alignItems="center" flexWrap="wrap" useFlexGap>
                <CircularProgress size={22} />
                <Typography variant="body2">{job?.progress || "Запуск…"}</Typography>
                {jobId && (
                  <Button
                    size="small"
                    variant="outlined"
                    color="warning"
                    onClick={() => handleCancel()}
                    disabled={cancelling || job?.cancel_requested}
                  >
                    {job?.cancel_requested ? "Отмена…" : "Отменить всё"}
                  </Button>
                )}
              </Stack>
              {jobId && selectedIds.length > 1 && (
                <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
                  {(job?.report_ids?.length ? job.report_ids : selectedIds).map((rid) => (
                    <Button
                      key={rid}
                      size="small"
                      variant="text"
                      color="warning"
                      sx={{ minWidth: 0, px: 0.75 }}
                      onClick={() => handleCancel([rid])}
                      disabled={cancelling}
                    >
                      ✕ {rid}
                    </Button>
                  ))}
                </Stack>
              )}
            </Stack>
          )}

          {job && !running && formatDateTimeRu(checkedAt) && (
            <Typography variant="caption" color="text.secondary">
              Результат последнего запуска: {formatDateTimeRu(checkedAt)}
            </Typography>
          )}

          {job?.status === "cancelled" && (
            <Alert severity="info">Проверка отменена.</Alert>
          )}

          {completed && crossCheckRows.length > 0 && (
            <Box sx={{ border: 1, borderColor: "divider", borderRadius: 1, overflow: "auto" }}>
              <Typography variant="subtitle2" sx={{ p: 1 }}>
                Сверка операций между сводками
              </Typography>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>Операция</TableCell>
                    <TableCell>Сводки</TableCell>
                    {crossCheckPeerIds.map((rid) => (
                      <TableCell key={rid} align="right">
                        {rid}
                      </TableCell>
                    ))}
                    <TableCell align="right">Max diff</TableCell>
                    <TableCell>Статус</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {crossCheckRows.map((row) => (
                    <TableRow key={row.group_name}>
                      <TableCell>{row.display_name || row.group_name}</TableCell>
                      <TableCell>{(row.active_reports || []).join("+")}</TableCell>
                      {crossCheckPeerIds.map((rid) => (
                        <TableCell key={rid} align="right">
                          {row.totals?.[rid]?.toFixed?.(2) ?? row.totals?.[rid] ?? "—"}
                        </TableCell>
                      ))}
                      <TableCell align="right">
                        {row.max_diff_ha != null ? row.max_diff_ha.toFixed?.(2) ?? row.max_diff_ha : "—"}
                      </TableCell>
                      <TableCell>{row.status}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Box>
          )}

          {completed && cropwiseRows.length > 0 && (
            <Box sx={{ border: 1, borderColor: "divider", borderRadius: 1, overflow: "auto" }}>
              <Typography variant="subtitle2" sx={{ p: 1 }}>
                Сводки ↔ Cropwise UI
              </Typography>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>ID</TableCell>
                    <TableCell>Работа</TableCell>
                    <TableCell align="right">Сводка</TableCell>
                    <TableCell align="right">Cropwise</TableCell>
                    <TableCell align="right">API v3</TableCell>
                    <TableCell align="right">Diff</TableCell>
                    <TableCell>Статус</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {cropwiseRows.map((row) => (
                    <TableRow key={`${row.id}-${row.operation || row.title}`}>
                      <TableCell>{row.id}</TableCell>
                      <TableCell title={(row.warnings || []).join("\n") || undefined}>
                        {row.title}
                      </TableCell>
                      <TableCell align="right">{row.svodka_ha?.toFixed?.(2) ?? row.svodka_ha}</TableCell>
                      <TableCell align="right">{row.cropwise_ha?.toFixed?.(2) ?? row.cropwise_ha}</TableCell>
                      <TableCell align="right">
                        {row.cropwise_api_ha != null && row.cropwise_api_ha !== 0
                          ? row.cropwise_api_ha?.toFixed?.(2) ?? row.cropwise_api_ha
                          : "—"}
                      </TableCell>
                      <TableCell align="right">{row.diff_ha?.toFixed?.(2) ?? row.diff_ha}</TableCell>
                      <TableCell>{row.status}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Box>
          )}

          {completed && job?.ai_status && job.ai_status !== "skipped" && (
            <Box sx={{ border: 1, borderColor: "divider", borderRadius: 1, p: 2 }}>
              <Typography variant="subtitle2" gutterBottom>
                Отчёт AI
                {job.ai_violation_count > 0 ? ` (${job.ai_violation_count})` : ""}
              </Typography>
              <AiReportPanel
                aiData={{
                  ai_status: job.ai_status,
                  ai_violation_count: job.ai_violation_count,
                  ai_report: job.ai_report,
                  ai_error: job.ai_error,
                }}
                loading={job.ai_status === "running" || job.ai_status === "pending"}
              />
            </Box>
          )}

          {error && <Alert severity="error">{error}</Alert>}
        </Stack>
      </DialogContent>
      <DialogActions sx={{ px: 2, py: 1.5 }}>
        <Button onClick={onClose} disabled={running && !job?.cancel_requested}>
          Закрыть
        </Button>
        {completed && (
          <Button variant="outlined" startIcon={<DownloadRoundedIcon />} onClick={handleDownload}>
            Скачать Excel
          </Button>
        )}
        <Button
          variant="contained"
          onClick={handleRun}
          disabled={running || !selectedIds.length}
          startIcon={running ? <CircularProgress size={16} color="inherit" /> : <FactCheckRoundedIcon />}
        >
          {running ? "Формирование…" : "Запустить проверку"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
