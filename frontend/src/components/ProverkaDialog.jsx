import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControlLabel,
  FormGroup,
  IconButton,
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
import QuestionAnswerOutlinedIcon from "@mui/icons-material/QuestionAnswerOutlined";
import DownloadRoundedIcon from "@mui/icons-material/DownloadRounded";
import ExpandLessRoundedIcon from "@mui/icons-material/ExpandLessRounded";
import ExpandMoreRoundedIcon from "@mui/icons-material/ExpandMoreRounded";

import AiReportPanel from "./AiReportPanel";

/** Согласовано с proverka/ha_thresholds.py DIFF_MIN_HA */
const DIFF_MIN_HA = 0.5;
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

function formatHa(value) {
  if (value == null || value === "") return "—";
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(2) : String(value);
}

function HaFileStack({ calc, fileHa }) {
  const hasFile = fileHa != null && fileHa !== "";
  const apart = fileApartFromCalc(calc, fileHa);
  return (
    <Box sx={{ display: "flex", flexDirection: "column", alignItems: "flex-end", lineHeight: 1.15 }}>
      <span>{formatHa(calc)}</span>
      {hasFile && (
        <Typography
          component="span"
          variant="caption"
          sx={{ color: apart ? "error.main" : "text.secondary", fontWeight: apart ? 600 : 400 }}
        >
          {formatHa(fileHa)}
        </Typography>
      )}
    </Box>
  );
}

function fileApartFromCalc(calc, fileHa) {
  if (fileHa == null || fileHa === "" || calc == null || calc === "") return false;
  const left = Number(calc);
  const right = Number(fileHa);
  if (!Number.isFinite(left) || !Number.isFinite(right)) return false;
  return Math.abs(left - right) >= DIFF_MIN_HA;
}

function cropwiseApart(cropwiseHa, values) {
  if (cropwiseHa == null || cropwiseHa === "") return false;
  const cw = Number(cropwiseHa);
  if (!Number.isFinite(cw)) return false;
  const nums = Object.values(values || {})
    .map(Number)
    .filter((n) => Number.isFinite(n));
  if (!nums.length) return false;
  const closest = Math.min(...nums.map((n) => Math.abs(n - cw)));
  return closest >= DIFF_MIN_HA;
}

function cropwiseIndex(summaryRows) {
  const byOp = new Map();
  for (const row of summaryRows || []) {
    if (!row?.operation) continue;
    let slot = byOp.get(row.operation);
    if (!slot) {
      slot = { total: null, crops: new Map() };
      byOp.set(row.operation, slot);
    }
    if (row.is_crop_row) {
      if (!slot.crops.has(row.crop)) {
        const firms = new Map();
        for (const fr of row.crop_firms || []) {
          if (fr?.firm) firms.set(fr.firm, fr.cropwise_ha);
        }
        slot.crops.set(row.crop, { ha: row.cropwise_ha, firms });
      }
      continue;
    }
    if (slot.total == null && row.cropwise_ha != null) slot.total = row.cropwise_ha;
  }
  return byOp;
}

function pickCropwise(primary, fallback) {
  if (primary != null && primary !== "") return primary;
  return fallback ?? null;
}

function cropKey(groupName, cropName) {
  return `${groupName}\0${cropName}`;
}

function CrossCheckStatusChips({ row, cropwiseAlert }) {
  const cropDiff = cropwiseAlert || (row.crops || []).some((c) => c.status === "DIFF");
  if (row.status === "partial") {
    return <Chip size="small" color="default" label="partial" />;
  }
  return (
    <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
      <Chip
        size="small"
        color={row.status === "DIFF" ? "error" : "success"}
        label={row.status || "—"}
      />
      {cropDiff && row.status === "OK" && (
        <Chip size="small" color="warning" variant="outlined" label="культуры" />
      )}
    </Stack>
  );
}

export default function ProverkaDialog({ open, onClose, summaryDate, onComplete, onOpenTaskFacts }) {
  const [catalog, setCatalog] = useState([]);
  const [selectedIds, setSelectedIds] = useState([]);
  const [cookie, setCookie] = useState("");
  const [envCookie, setEnvCookie] = useState(false);
  const [jobId, setJobId] = useState(null);
  const [job, setJob] = useState(null);
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);
  const [checkedAt, setCheckedAt] = useState(null);
  const runningRef = useRef(false);
  runningRef.current = running;
  const [cancelling, setCancelling] = useState(false);
  const [aiAnalysis, setAiAnalysis] = useState(false);
  const [showAllCrops, setShowAllCrops] = useState(true);
  const [expandedCropKeys, setExpandedCropKeys] = useState(() => new Set());

  const sortedCatalog = useMemo(() => sortCatalogDesc(catalog), [catalog]);

  const loadCatalog = useCallback(async () => {
    try {
      const data = await fetchProverkaCatalog();
      const items = sortCatalogDesc(data.items || []);
      const saved = String(data.env_cookie || "").trim();
      setEnvCookie(Boolean(saved));
      if (saved) setCookie((prev) => (prev.trim() ? prev : saved));
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
    if (!cookie.trim() && !envCookie) {
      setError("Введите cookie или задайте CROPWISE_SESSION_COOKIE в .env");
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
  const sheetCheckRows = job?.sheet_checks ?? [];
  const sheetCheckDiffRows = useMemo(
    () => sheetCheckRows.filter((r) => r.status === "DIFF"),
    [sheetCheckRows]
  );
  const sheetCheckEligible = useMemo(() => {
    const ids = job?.report_ids || [];
    return ids.some((id) => String(id) === "1" || String(id) === "2");
  }, [job?.report_ids]);
  const cropwiseRows = useMemo(() => job?.summary_rows || [], [job?.summary_rows]);

  useEffect(() => {
    setExpandedCropKeys(new Set());
  }, [jobId]);

  const hasCropBreakdown = useMemo(
    () => crossCheckRows.some((row) => (row.crops || []).length > 0),
    [crossCheckRows]
  );

  const cropwiseByOp = useMemo(
    () => cropwiseIndex(job?.summary_rows),
    [job?.summary_rows]
  );

  const cropwiseFor = useCallback(
    (groupName, cropName, firmName, primary) => {
      const slot = cropwiseByOp.get(groupName);
      if (firmName) {
        return pickCropwise(primary, slot?.crops.get(cropName)?.firms.get(firmName));
      }
      if (cropName) {
        return pickCropwise(primary, slot?.crops.get(cropName)?.ha);
      }
      return pickCropwise(primary, slot?.total);
    },
    [cropwiseByOp]
  );

  const hasCropwiseColumn = useMemo(() => {
    for (const row of crossCheckRows) {
      if (cropwiseFor(row.group_name, null, null, row.cropwise_ha) != null) return true;
      for (const cr of row.crops || []) {
        if (cropwiseFor(row.group_name, cr.crop, null, cr.cropwise_ha) != null) return true;
      }
    }
    return false;
  }, [crossCheckRows, cropwiseFor]);

  const crossCheckTableRows = useMemo(() => {
    const out = [];
    for (const row of crossCheckRows) {
      out.push({ kind: "op", row });
      const crops = (row.crops || []).filter(
        (c) =>
          showAllCrops ||
          c.status === "DIFF" ||
          cropwiseApart(cropwiseFor(row.group_name, c.crop, null, c.cropwise_ha), c.totals)
      );
      for (const cr of crops) {
        const key = cropKey(row.group_name, cr.crop);
        const firmRows = (cr.firms || []).filter(
          (fr) =>
            showAllCrops ||
            (fr.max_diff_ha || 0) >= DIFF_MIN_HA ||
            cropwiseApart(
              cropwiseFor(row.group_name, cr.crop, fr.firm, fr.cropwise_ha),
              fr.values
            )
        );
        out.push({
          kind: "crop",
          op: row,
          crop: cr,
          key,
          firmCount: firmRows.length,
          expandable: firmRows.length > 0,
        });
        if (expandedCropKeys.has(key)) {
          for (const fr of firmRows) {
            out.push({ kind: "firm", op: row, crop: cr, firm: fr, key });
          }
        }
      }
    }
    return out;
  }, [crossCheckRows, showAllCrops, expandedCropKeys, cropwiseFor]);

  function toggleCropFirms(key) {
    setExpandedCropKeys((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

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

  const firmCheckRows = useMemo(() => {
    const rows = [];
    for (const row of crossCheckRows) {
      for (const firmRow of row.firms || []) {
        rows.push({
          group_name: row.group_name,
          display_name: row.display_name || row.group_name,
          firm: firmRow.firm,
          values: firmRow.values || {},
          file_totals: firmRow.file_totals || {},
          max_diff_ha: firmRow.max_diff_ha,
          cropwise_ha: firmRow.cropwise_ha,
        });
      }
    }
    return rows;
  }, [crossCheckRows]);

  const hasFirmCropwise = useMemo(
    () => firmCheckRows.some((row) => row.cropwise_ha != null && row.cropwise_ha !== ""),
    [firmCheckRows]
  );

  return (
    <Dialog
      open={open}
      onClose={onClose}
      fullWidth
      maxWidth="xl"
      PaperProps={{ sx: { width: "95vw", maxWidth: "95vw" } }}
    >
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
            label="Cookie Cropwise"
            placeholder={envCookie ? "Cookie из .env" : "_cropio_ses=…; crossdomain_auth=…"}
            value={cookie}
            onChange={(e) => setCookie(e.target.value)}
            fullWidth
            size="small"
            disabled={running}
            helperText={
              envCookie
                ? "Подставлен из .env. Можно заменить; пустое поле снова берёт cookie сервера."
                : "В .env нет CROPWISE_SESSION_COOKIE — вставьте cookie вручную."
            }
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

          {completed && sheetCheckEligible && (
            <Box sx={{ border: 1, borderColor: "divider", borderRadius: 1, overflow: "auto" }}>
              <Typography variant="subtitle2" sx={{ p: 1 }}>
                Согласованность листов (сводки 1 и 2)
                {sheetCheckDiffRows.length > 0 ? ` — расхождений: ${sheetCheckDiffRows.length}` : ""}
              </Typography>
              <Typography variant="caption" color="text.secondary" sx={{ px: 1, pb: 1, display: "block" }}>
                «Обработано за сутки» на листе выработки ↔ «Общий намолот» / таблицы сводки 2.
                Данные берутся из xlsx или pdf в uploads за дату проверки (не из расчёта proverka).
              </Typography>
              {sheetCheckRows.length === 0 ? (
                <Alert severity="warning" sx={{ m: 1, mt: 0 }}>
                  Нет данных по файлам: сохраните сводки 1/2 (xlsx или pdf) в uploads на{" "}
                  {summaryDate} и перезапустите проверку. Если API не обновлён — перезапустите backend.
                </Alert>
              ) : (
                <Table size="small">
                  <TableHead>
                    <TableRow>
                      <TableCell>Сводка</TableCell>
                      <TableCell>Операция</TableCell>
                      <TableCell align="right">Выработка, га</TableCell>
                      <TableCell align="right">Σ АФ, га</TableCell>
                      <TableCell align="right">Намолот, га</TableCell>
                      <TableCell align="right">Σ культур, га</TableCell>
                      <TableCell align="right">Max diff</TableCell>
                      <TableCell>Статус</TableCell>
                      <TableCell>Что не сходится</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {sheetCheckRows.map((row) => (
                      <TableRow
                        key={`${row.report_id}-${row.group_name}`}
                        sx={
                          row.status === "DIFF"
                            ? { backgroundColor: "rgba(252, 228, 214, 0.45)" }
                            : undefined
                        }
                      >
                        <TableCell>{row.report_id}</TableCell>
                        <TableCell>{row.display_name || row.group_name}</TableCell>
                        <TableCell align="right">{formatHa(row.total_ha)}</TableCell>
                        <TableCell align="right">{formatHa(row.firms_ha)}</TableCell>
                        <TableCell align="right">{formatHa(row.general_ha)}</TableCell>
                        <TableCell align="right">{formatHa(row.crops_ha)}</TableCell>
                        <TableCell align="right">{formatHa(row.max_diff_ha)}</TableCell>
                        <TableCell>{row.status}</TableCell>
                        <TableCell sx={{ maxWidth: 280 }}>
                          {(row.issues || [])
                            .map(
                              (i) =>
                                `${i.label || i.kind}: ${i.expected} ↔ ${i.actual} (Δ ${i.diff_ha} га)`
                            )
                            .join("; ") || "—"}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </Box>
          )}

          {completed && crossCheckRows.length > 0 && (
            <Box sx={{ border: 1, borderColor: "divider", borderRadius: 1, overflow: "auto" }}>
              <Stack
                direction="row"
                alignItems="center"
                justifyContent="space-between"
                flexWrap="wrap"
                useFlexGap
                sx={{ p: 1, gap: 1 }}
              >
                <Typography variant="subtitle2">Сверка операций между сводками</Typography>
                {hasCropBreakdown && (
                  <FormControlLabel
                    control={
                      <Checkbox
                        size="small"
                        checked={showAllCrops}
                        onChange={(e) => setShowAllCrops(e.target.checked)}
                      />
                    }
                    label={
                      <Typography variant="body2">
                        Показать все культуры (не только расхождения)
                      </Typography>
                    }
                  />
                )}
              </Stack>
              {hasCropBreakdown && (
                <Typography variant="caption" color="text.secondary" sx={{ px: 1, pb: 1, display: "block" }}>
                  Под операцией — сумма за смену по культуре (все АФ).
                  {hasCropwiseColumn
                    ? " Max diff — наибольшая разница |сводка − Cropwise| по этой строке."
                    : ""}{" "}
                  Нажмите ▶ у культуры, чтобы развернуть расхождения по агрофирмам.
                </Typography>
              )}
              {crossCheckRows.some((row) => row.file_totals && Object.keys(row.file_totals).length > 0) && (
                <Typography variant="caption" color="text.secondary" sx={{ px: 1, pb: 1, display: "block" }}>
                  Под номером сводки две строки: сверху расчёт, снизу гектары в файле.
                  Так же у каждой культуры, если в файле сводки есть разбивка.
                </Typography>
              )}
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>Операция / культура</TableCell>
                    <TableCell>Сводки</TableCell>
                    {crossCheckPeerIds.map((rid) => (
                      <TableCell key={rid} align="right">
                        {rid}
                      </TableCell>
                    ))}
                    {hasCropwiseColumn && (
                      <TableCell align="right">Cropwise</TableCell>
                    )}
                    <TableCell align="right">Max diff</TableCell>
                    <TableCell>Статус</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {crossCheckTableRows.map((item) => {
                    if (item.kind === "op") {
                      const row = item.row;
                      return (
                        <TableRow key={`op-${row.group_name}`}>
                          <TableCell sx={{ fontWeight: 600 }}>
                            {row.display_name || row.group_name}
                          </TableCell>
                          <TableCell>{(row.active_reports || []).join("+")}</TableCell>
                          {crossCheckPeerIds.map((rid) => (
                            <TableCell
                              key={rid}
                              align="right"
                              title={
                                row.file_totals?.[rid] != null
                                  ? `В файле сводки ${rid}: ${formatHa(row.file_totals[rid])} га`
                                  : undefined
                              }
                              sx={{ whiteSpace: "nowrap", verticalAlign: "top" }}
                            >
                              <HaFileStack calc={row.totals?.[rid]} fileHa={row.file_totals?.[rid]} />
                            </TableCell>
                          ))}
                          {hasCropwiseColumn && (
                            <TableCell
                              align="right"
                              sx={
                                cropwiseApart(
                                  cropwiseFor(row.group_name, null, null, row.cropwise_ha),
                                  row.totals
                                )
                                  ? { color: "error.main", fontWeight: 600 }
                                  : undefined
                              }
                            >
                              {formatHa(cropwiseFor(row.group_name, null, null, row.cropwise_ha))}
                            </TableCell>
                          )}
                          <TableCell align="right">{formatHa(row.max_diff_ha)}</TableCell>
                          <TableCell>
                            <CrossCheckStatusChips
                              row={row}
                              cropwiseAlert={
                                cropwiseApart(
                                  cropwiseFor(row.group_name, null, null, row.cropwise_ha),
                                  row.totals
                                ) ||
                                (row.crops || []).some((c) =>
                                  cropwiseApart(
                                    cropwiseFor(row.group_name, c.crop, null, c.cropwise_ha),
                                    c.totals
                                  )
                                )
                              }
                            />
                          </TableCell>
                        </TableRow>
                      );
                    }
                    if (item.kind === "crop") {
                      const { op, crop: cr, key, expandable, firmCount } = item;
                      const expanded = expandedCropKeys.has(key);
                      const cwApart = cropwiseApart(
                        cropwiseFor(op.group_name, cr.crop, null, cr.cropwise_ha),
                        cr.totals
                      );
                      return (
                        <TableRow
                          key={`crop-${key}`}
                          sx={{
                            bgcolor:
                              cr.status === "DIFF" || cwApart ? "action.selected" : "action.hover",
                          }}
                        >
                          <TableCell sx={{ pl: 3 }}>
                            <Stack direction="row" alignItems="center" spacing={0.5}>
                              {expandable ? (
                                <IconButton
                                  size="small"
                                  aria-label={expanded ? "Свернуть фирмы" : "Развернуть фирмы"}
                                  onClick={() => toggleCropFirms(key)}
                                >
                                  {expanded ? (
                                    <ExpandLessRoundedIcon fontSize="small" />
                                  ) : (
                                    <ExpandMoreRoundedIcon fontSize="small" />
                                  )}
                                </IconButton>
                              ) : (
                                <Box sx={{ width: 28 }} />
                              )}
                              <Typography variant="body2" component="span">
                                {cr.crop}
                                {firmCount > 0 && (
                                  <Typography
                                    component="span"
                                    variant="caption"
                                    color="text.secondary"
                                    sx={{ ml: 0.5 }}
                                  >
                                    ({firmCount} АФ)
                                  </Typography>
                                )}
                              </Typography>
                            </Stack>
                          </TableCell>
                          <TableCell>{(cr.active_reports || op.active_reports || []).join("+")}</TableCell>
                          {crossCheckPeerIds.map((rid) => (
                            <TableCell
                              key={rid}
                              align="right"
                              title={
                                cr.file_totals?.[rid] != null
                                  ? `В файле сводки ${rid}, ${cr.crop}: ${formatHa(cr.file_totals[rid])} га`
                                  : undefined
                              }
                              sx={{ whiteSpace: "nowrap", verticalAlign: "top" }}
                            >
                              <HaFileStack calc={cr.totals?.[rid]} fileHa={cr.file_totals?.[rid]} />
                            </TableCell>
                          ))}
                          {hasCropwiseColumn && (
                            <TableCell
                              align="right"
                              sx={
                                cropwiseApart(
                                  cropwiseFor(op.group_name, cr.crop, null, cr.cropwise_ha),
                                  cr.totals
                                )
                                  ? { color: "error.main", fontWeight: 600 }
                                  : undefined
                              }
                            >
                              {formatHa(cropwiseFor(op.group_name, cr.crop, null, cr.cropwise_ha))}
                            </TableCell>
                          )}
                          <TableCell align="right">{formatHa(cr.max_diff_ha)}</TableCell>
                          <TableCell>
                            <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
                              <Chip
                                size="small"
                                color={cr.status === "DIFF" ? "error" : "success"}
                                label={cr.status}
                              />
                              {cwApart && (
                                <Chip size="small" color="warning" variant="outlined" label="Cropwise" />
                              )}
                            </Stack>
                          </TableCell>
                        </TableRow>
                      );
                    }
                    const { op, crop: cr, firm: fr, key } = item;
                    return (
                      <TableRow key={`firm-${key}-${fr.firm}`} sx={{ bgcolor: "background.default" }}>
                        <TableCell sx={{ pl: 7, py: 0.75 }}>
                          <Typography variant="caption" color="text.secondary">
                            {fr.firm}
                          </Typography>
                        </TableCell>
                        <TableCell />
                        {crossCheckPeerIds.map((rid) => (
                          <TableCell key={rid} align="right">
                            <Typography variant="caption">{formatHa(fr.values?.[rid])}</Typography>
                          </TableCell>
                        ))}
                        {hasCropwiseColumn && (
                          <TableCell
                            align="right"
                            sx={
                              cropwiseApart(
                                cropwiseFor(op.group_name, cr.crop, fr.firm, fr.cropwise_ha),
                                fr.values
                              )
                                ? { color: "error.main" }
                                : undefined
                            }
                          >
                            <Typography variant="caption">
                              {formatHa(cropwiseFor(op.group_name, cr.crop, fr.firm, fr.cropwise_ha))}
                            </Typography>
                          </TableCell>
                        )}
                        <TableCell align="right">
                          <Typography variant="caption">{formatHa(fr.max_diff_ha)}</Typography>
                        </TableCell>
                        <TableCell />
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </Box>
          )}

          {completed && firmCheckRows.length > 0 && (
            <Box sx={{ border: 1, borderColor: "divider", borderRadius: 1, overflow: "auto" }}>
              <Typography variant="subtitle2" sx={{ p: 1 }}>
                Сверка по агрофирмам
              </Typography>
              <Typography variant="caption" color="text.secondary" sx={{ px: 1, display: "block", mb: 0.5 }}>
                Итог за смену по каждой АФ внутри операции: сводки между собой и Cropwise.
                Сводка 1 для посева, опрыскивания и др. подставляет гектары с доноров — как в PDF.
                Под номером сводки две строки: сверху расчёт, снизу гектары этой агрофирмы в файле.
              </Typography>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>Операция</TableCell>
                    <TableCell>Агрофирма</TableCell>
                    {crossCheckPeerIds.map((rid) => (
                      <TableCell key={rid} align="right">
                        {rid}
                      </TableCell>
                    ))}
                    {hasFirmCropwise && (
                      <TableCell align="right">Cropwise</TableCell>
                    )}
                    <TableCell align="right">Max diff</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {firmCheckRows.map((row, idx) => (
                    <TableRow key={`${row.group_name}-${row.firm}-${idx}`}>
                      <TableCell>{row.display_name}</TableCell>
                      <TableCell>{row.firm}</TableCell>
                      {crossCheckPeerIds.map((rid) => (
                        <TableCell
                          key={rid}
                          align="right"
                          title={
                            row.file_totals?.[rid] != null
                              ? `В файле сводки ${rid}, ${row.firm}: ${formatHa(row.file_totals[rid])} га`
                              : undefined
                          }
                          sx={{ whiteSpace: "nowrap", verticalAlign: "top" }}
                        >
                          <HaFileStack calc={row.values?.[rid]} fileHa={row.file_totals?.[rid]} />
                        </TableCell>
                      ))}
                      {hasFirmCropwise && (
                        <TableCell
                          align="right"
                          sx={
                            cropwiseApart(row.cropwise_ha, row.values)
                              ? { color: "error.main" }
                              : undefined
                          }
                        >
                          {formatHa(row.cropwise_ha)}
                        </TableCell>
                      )}
                      <TableCell align="right">{formatHa(row.max_diff_ha)}</TableCell>
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
              <Typography variant="caption" color="text.secondary" sx={{ px: 1, pb: 1, display: "block" }}>
                Строки с названием культуры (после «·») — сводка ↔ Cropwise по культурам;
                в таблице выше — те же peer-сводки, что и по операции, если есть разбивка.
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
        {completed && onOpenTaskFacts && (
          <Button
            variant="outlined"
            startIcon={<QuestionAnswerOutlinedIcon />}
            onClick={() => onOpenTaskFacts(jobId)}
          >
            Вопросы по фактам
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
