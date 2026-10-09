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
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";
import PrecisionManufacturingRoundedIcon from "@mui/icons-material/PrecisionManufacturingRounded";

import { sortCatalogDesc } from "../utils/reportSort";
import { formatDateTimeRu } from "../utils/formatDuration";
import {
  cancelProverkaMachinesJob,
  fetchLatestProverkaMachinesJob,
  fetchProverkaCatalog,
  fetchProverkaMachinesJob,
  runProverkaMachines,
} from "../api";

// Полностью независимый от ProverkaDialog компонент: свой стейт, свой job,
// свой API. Проверка читает уже готовые Excel за выбранную дату, сводки не пересчитываются.
export default function ProverkaMachinesDialog({ open, onClose, summaryDate, onComplete, runToken = 0 }) {
  const [catalog, setCatalog] = useState([]);
  const [selectedIds, setSelectedIds] = useState([]);
  const [jobId, setJobId] = useState(null);
  const [job, setJob] = useState(null);
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);
  const [checkedAt, setCheckedAt] = useState(null);
  const runningRef = useRef(false);
  const summaryDateRef = useRef(summaryDate);
  summaryDateRef.current = summaryDate;
  runningRef.current = running;
  const [cancelling, setCancelling] = useState(false);

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
      const latest = await fetchLatestProverkaMachinesJob(summaryDate);
      if (runningRef.current) return; // уже идёт запуск из этого окна
      if (!latest?.job_id) {
        setJobId(null);
        setJob(null);
        setCheckedAt(null);
        return;
      }
      const st = await fetchProverkaMachinesJob(latest.job_id);
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
        const st = await fetchProverkaMachinesJob(jobId);
        if (cancelled) return;
        setJob(st);
        if (st.status === "completed" || st.status === "failed" || st.status === "cancelled") {
          setRunning(false);
          setCheckedAt(new Date().toISOString());
          if (st.status === "failed") {
            setError(st.error || "Ошибка проверки");
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

  async function startCheck(reportIds) {
    setError("");
    if (!reportIds.length) {
      setError("Выберите хотя бы одну сводку");
      setRunning(false);
      runningRef.current = false;
      return;
    }
    setRunning(true);
    runningRef.current = true;
    setJob(null);
    try {
      const res = await runProverkaMachines({
        reportDate: summaryDate,
        reportIds,
      });
      setJobId(res.job_id);
    } catch (e) {
      setError(String(e.message || e));
      setRunning(false);
      runningRef.current = false;
    }
  }

  // Кнопка «Оборудование» в шапке: сразу сверяем сводку 2 и все сводки,
  // которые с ней пересекаются по операциям. Таймер отсекает повтор StrictMode.
  useEffect(() => {
    if (!runToken) return undefined;
    let cancelled = false;
    let started = false;
    runningRef.current = true;
    setRunning(true);
    setJob(null);
    setError("");
    const timer = setTimeout(() => {
      if (cancelled) return;
      started = true;
      runProverkaMachines({ reportDate: summaryDateRef.current, reportIds: ["2"] })
        .then((res) => {
          if (cancelled) return;
          setJobId(res.job_id);
        })
        .catch((e) => {
          if (cancelled) return;
          setError(String(e.message || e));
          setRunning(false);
          runningRef.current = false;
        });
    }, 0);
    return () => {
      cancelled = true;
      clearTimeout(timer);
      if (!started) {
        runningRef.current = false;
        setRunning(false);
      }
    };
  }, [runToken]);

  async function handleRun() {
    await startCheck(selectedIds);
  }

  async function handleCancel() {
    if (!jobId) return;
    setCancelling(true);
    setError("");
    try {
      const st = await cancelProverkaMachinesJob(jobId);
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

  const completed = job?.status === "completed";
  const rows = job?.machine_cross_check_rows ?? job?.machine_cross_check?.operations ?? [];

  const peerIds = useMemo(() => {
    const ids = new Set();
    for (const row of rows) {
      for (const rid of row.active_reports || row.peer_reports || []) {
        ids.add(String(rid));
      }
    }
    return [...ids].sort((a, b) => {
      if (a === "2") return -1;
      if (b === "2") return 1;
      const na = Number(a);
      const nb = Number(b);
      if (Number.isFinite(na) && Number.isFinite(nb)) return nb - na;
      return String(b).localeCompare(String(a), undefined, { numeric: true });
    });
  }, [rows]);

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="md">
      <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1 }}>
        <PrecisionManufacturingRoundedIcon color="primary" />
        Оборудование
      </DialogTitle>
      <DialogContent dividers>
        <Stack spacing={2}>
          <Typography variant="body2" color="text.secondary">
            Дата отчёта: <strong>{summaryDate}</strong> · по готовым Excel число техники
            сверяется со <strong>сводкой 2</strong> по общим операциям. Сводки заново не
            считаются: сначала сформируйте файлы за эту дату.
          </Typography>

          {job?.missing_file_report_ids?.length > 0 && (
            <Alert severity="warning">
              Нет готового Excel за {summaryDate}: {job.missing_file_report_ids.join(", ")}. Эти
              сводки в сверке не участвуют.
            </Alert>
          )}
          {job?.no_machine_data_report_ids?.length > 0 && (
            <Alert severity="info">
              В Excel нет числа машин: {job.no_machine_data_report_ids.join(", ")}.
            </Alert>
          )}
          {Object.entries(job?.file_read_errors || {}).map(([rid, msg]) => (
            <Alert severity="error" key={rid}>
              Сводка {rid}: файл не прочитан ({msg})
            </Alert>
          ))}

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
            <Stack direction="row" spacing={1.5} alignItems="center">
              <CircularProgress size={22} />
              <Typography variant="body2">{job?.progress || "Запуск…"}</Typography>
              {jobId && (
                <Button
                  size="small"
                  variant="outlined"
                  color="warning"
                  onClick={handleCancel}
                  disabled={cancelling || job?.cancel_requested}
                >
                  {job?.cancel_requested ? "Отмена…" : "Отменить"}
                </Button>
              )}
            </Stack>
          )}

          {job && !running && formatDateTimeRu(checkedAt) && (
            <Typography variant="caption" color="text.secondary">
              Результат последнего запуска: {formatDateTimeRu(checkedAt)}
            </Typography>
          )}

          {job?.status === "cancelled" && <Alert severity="info">Проверка отменена.</Alert>}

          {completed && rows.length > 0 && (
            <Box sx={{ border: 1, borderColor: "divider", borderRadius: 1, overflow: "auto" }}>
              <Typography variant="subtitle2" sx={{ p: 1 }}>
                Сверка количества машин между сводками
              </Typography>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>Операция</TableCell>
                    <TableCell>Сводки</TableCell>
                    {peerIds.map((rid) => (
                      <TableCell key={rid} align="right" sx={rid === "2" ? { fontWeight: 700 } : undefined}>
                        {rid === "2" ? "2" : rid}
                      </TableCell>
                    ))}
                    <TableCell align="right">Max diff</TableCell>
                    <TableCell>Статус</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {rows.map((row) => (
                    <TableRow key={row.group_name}>
                      <TableCell>{row.display_name || row.group_name}</TableCell>
                      <TableCell>{(row.active_reports || row.peer_reports || []).join("+")}</TableCell>
                      {peerIds.map((rid) => (
                        <TableCell key={rid} align="right" sx={rid === "2" ? { fontWeight: 700 } : undefined}>
                          {row.totals?.[rid] ?? "—"}
                        </TableCell>
                      ))}
                      <TableCell align="right">
                        {row.max_diff_machines != null ? row.max_diff_machines : "—"}
                      </TableCell>
                      <TableCell>
                        {row.status === "DIFF" ? (
                          <Chip size="small" color="error" label="DIFF" />
                        ) : row.status === "partial" ? (
                          <Chip size="small" color="default" label="partial" />
                        ) : (
                          <Chip size="small" color="success" label="OK" />
                        )}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Box>
          )}

          {completed && rows.length === 0 && (
            <Alert severity="info">
              Нет операций, по которым выбранные сводки пересекаются друг с другом.
            </Alert>
          )}

          {error && <Alert severity="error">{error}</Alert>}
        </Stack>
      </DialogContent>
      <DialogActions sx={{ px: 2, py: 1.5 }}>
        <Button onClick={onClose} disabled={running && !job?.cancel_requested}>
          Закрыть
        </Button>
        <Button
          variant="contained"
          onClick={handleRun}
          disabled={running || !selectedIds.length}
          startIcon={
            running ? <CircularProgress size={16} color="inherit" /> : <PrecisionManufacturingRoundedIcon />
          }
        >
          {running ? "Проверка…" : "Запустить проверку"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
