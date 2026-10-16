import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Chip,
  CircularProgress,
  Collapse,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControlLabel,
  MenuItem,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TextField,
  ToggleButton,
  ToggleButtonGroup,
  Tooltip,
  Typography,
} from "@mui/material";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import ChevronRightIcon from "@mui/icons-material/ChevronRight";

import {
  fetchHarvestCheck,
  fetchHarvestCheckReports,
  generateReports,
  getJobStatus,
  runHarvestCheck,
} from "../api";

const ALL = "ALL";
const CROPWISE = "cropwise";
const METRIC_LABEL = { day: "За сутки", season: "За сезон" };
const STATUS_META = {
  ok: { label: "совпадает", color: "success" },
  svodki: { label: "сводки расходятся", color: "error" },
  cropwise: { label: "расходится с Cropwise", color: "warning" },
  both: { label: "сводки и Cropwise", color: "error" },
  single: { label: "один источник", color: "default" },
};
const STATUS_RANK = { ok: 0, single: 1, cropwise: 2, svodki: 3, both: 4 };
const GEN_TERMINAL = new Set(["completed", "failed", "cancelled"]);
const POLL_MS = 2500;

const storageKey = (date) => `harvestCheck:${date}`;
const firmLabel = (f) => (f === ALL ? "Холдинг" : f);
const cropLabel = (c) => (c === ALL ? "Все культуры" : c);
const fmt = (v) =>
  v == null ? "—" : Number(v).toLocaleString("ru-RU", { minimumFractionDigits: 0, maximumFractionDigits: 2 });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function median(nums) {
  if (!nums.length) return null;
  const s = [...nums].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

function worstStatus(list) {
  return list.reduce((acc, row) => ((STATUS_RANK[row.status] ?? 0) > (STATUS_RANK[acc] ?? 0) ? row.status : acc), "ok");
}

function isDiff(row) {
  return row.status !== "ok" && row.status !== "single";
}

function headlineValue(row) {
  if (!row) return "—";
  const nums = Object.values(row.values || {}).map((cell) => cell.value);
  if (row.status === "ok" || row.status === "single") return fmt(nums[0] ?? row.cropwise?.value);
  if (row.spread != null) return `разброс ${fmt(row.spread)}`;
  return fmt(nums[0] ?? row.cropwise?.value);
}

function ValueCell({ cell, off }) {
  if (!cell) return <TableCell align="right" sx={{ color: "text.disabled" }}>—</TableCell>;
  const tip = cell.derived ? `${cell.where} (в файле этой строки нет)` : cell.where;
  return (
    <TableCell
      align="right"
      sx={{
        fontVariantNumeric: "tabular-nums",
        fontStyle: cell.derived ? "italic" : "normal",
        color: cell.derived ? "text.secondary" : "inherit",
        backgroundColor: off ? "rgba(211, 47, 47, 0.10)" : undefined,
        fontWeight: off ? 600 : 400,
      }}
    >
      <Tooltip title={tip} placement="top" arrow>
        <span>{fmt(cell.value)}</span>
      </Tooltip>
    </TableCell>
  );
}

export default function HarvestCheckDialog({ open, onClose, summaryDate }) {
  const [reportIds, setReportIds] = useState(["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "13"]);
  const [operations, setOperations] = useState([]);
  const [peers, setPeers] = useState({});
  const [cookieConfigured, setCookieConfigured] = useState(false);
  const [cookie, setCookie] = useState("");
  const [tolerance, setTolerance] = useState(0.5);
  const [regenerate, setRegenerate] = useState(true);
  const [phase, setPhase] = useState("idle"); // idle | generating | checking
  const [progress, setProgress] = useState("");
  const [error, setError] = useState("");
  const [genWarning, setGenWarning] = useState("");
  const [job, setJob] = useState(null);
  const [openOps, setOpenOps] = useState([]);
  const [firm, setFirm] = useState("*");
  const [crop, setCrop] = useState("*");
  const [metric, setMetric] = useState("all");
  const [onlyDiff, setOnlyDiff] = useState(true);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  const pollCheck = useCallback(async (jobId) => {
    for (;;) {
      const st = await fetchHarvestCheck(jobId);
      if (!aliveRef.current) return null;
      setJob(st);
      setProgress(st.progress || "");
      if (st.status === "completed" || st.status === "failed") return st;
      await sleep(POLL_MS);
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    setError("");
    fetchHarvestCheckReports()
      .then((d) => {
        setReportIds(d.report_ids || []);
        setOperations(d.operations || []);
        setPeers(d.peers || {});
        setCookieConfigured(Boolean(d.cookie_configured));
      })
      .catch(() => {});
    const saved = localStorage.getItem(storageKey(summaryDate));
    if (!saved) {
      setJob(null);
      return;
    }
    fetchHarvestCheck(saved)
      .then((st) => {
        setJob(st);
        if (st.status === "pending" || st.status === "running") {
          setPhase("checking");
          pollCheck(saved).finally(() => setPhase("idle"));
        }
      })
      .catch(() => setJob(null));
  }, [open, summaryDate, pollCheck]);

  async function waitGeneration(jobId) {
    for (;;) {
      const st = await getJobStatus(jobId);
      if (!aliveRef.current) return null;
      const done = (st.created_count || 0) + (st.failed_count || 0);
      setProgress(`Генерация сводок ${reportIds.join(", ")}: готово ${done} из ${reportIds.length}`);
      if (GEN_TERMINAL.has(st.status)) return st;
      await sleep(POLL_MS);
    }
  }

  async function handleRun() {
    setError("");
    setGenWarning("");
    try {
      if (regenerate) {
        setPhase("generating");
        setProgress("Ставим сводки в очередь генерации…");
        const gen = await generateReports(summaryDate, reportIds);
        const st = await waitGeneration(gen.job_id);
        if (!st) return;
        if (st.status !== "completed") {
          setGenWarning(`Генерация завершилась со статусом «${st.status}». Сверяем то, что есть в файлах.`);
        } else if (st.failed_count) {
          const failed = st.result?.failed || [];
          const details = failed.map((f) => `сводка ${f.report_id}: ${f.error || "ошибка"}`);
          const unique = [...new Set(failed.map((f) => f.error))].length === 1 && failed.length > 1;
          const text = unique
            ? `сводки ${failed.map((f) => f.report_id).join(", ")}: ${failed[0].error}`
            : details.join("; ");
          setGenWarning(
            `Не сгенерировано сводок: ${st.failed_count} (${text}). Для них сверяются прежние файлы.`
          );
        }
      }
      setPhase("checking");
      setProgress("Запуск сверки…");
      const started = await runHarvestCheck({ reportDate: summaryDate, cookie: cookie.trim(), tolerance });
      localStorage.setItem(storageKey(summaryDate), started.job_id);
      const st = await pollCheck(started.job_id);
      if (st?.status === "failed") setError(st.error || "Ошибка сверки");
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      if (aliveRef.current) setPhase("idle");
    }
  }

  const result = job?.status === "completed" ? job.result : null;
  const rows = useMemo(
    () => (result?.rows || []).map((r) => ({ ...r, operation: r.operation || "5.0 Уборка" })),
    [result]
  );
  const tol = result?.tolerance ?? tolerance;
  const busy = phase !== "idle";
  const opList = result?.operations?.length ? result.operations : operations;
  const peerMap = result?.peers || peers;

  const firms = useMemo(() => [...new Set(rows.map((r) => r.firm).filter((name) => name !== ALL))], [rows]);
  const crops = useMemo(() => [...new Set(rows.map((r) => r.crop).filter((name) => name !== ALL))], [rows]);

  const groups = useMemo(() => {
    const bag = new Map(opList.map((op) => [op, []]));
    for (const row of rows) {
      if (metric !== "all" && row.metric !== metric) continue;
      if (firm === "*") {
        if (row.firm !== ALL) continue;
      } else if (row.firm !== firm) continue;
      if (!bag.has(row.operation)) bag.set(row.operation, []);
      bag.get(row.operation).push(row);
    }
    const out = [];
    for (const [op, list] of bag) {
      const totals = list.filter((row) => row.crop === ALL);
      const crops = list.filter((row) => row.crop !== ALL && (crop === "*" || row.crop === crop));
      if (crop !== "*" && !crops.length) continue;
      const watched = crop === "*" ? [...totals, ...crops] : crops;
      if (!watched.length) continue;
      if (onlyDiff && !watched.some(isDiff)) continue;
      out.push({
        op,
        headline: crop === "*" ? totals : crops,
        crops,
        reportIds: peerMap[op] || [],
        status: worstStatus(watched),
        warnings: result?.by_operation?.[op]?.warnings || [],
        seasonStart: result?.by_operation?.[op]?.season_start,
      });
    }
    return out;
  }, [rows, opList, firm, crop, metric, onlyDiff, peerMap, result]);

  const summary = useMemo(() => {
    const counts = {};
    for (const group of groups) counts[group.status] = (counts[group.status] || 0) + 1;
    return counts;
  }, [groups]);
  const cw = result?.cropwise;
  const cwErrors = useMemo(() => {
    const bag = result?.by_operation || {};
    const byMsg = new Map();
    for (const [name, info] of Object.entries(bag)) {
      const err = info?.cropwise?.error;
      if (!err) continue;
      const ops = byMsg.get(err) || [];
      ops.push(name);
      byMsg.set(err, ops);
    }
    return [...byMsg.entries()].map(([err, ops]) =>
      ops.length === 1 ? `${ops[0]}: ${err}` : `${ops.length} операций: ${err}`
    );
  }, [result]);

  function toggleOp(name) {
    setOpenOps((prev) => (prev.includes(name) ? prev.filter((item) => item !== name) : [...prev, name]));
  }

  return (
    <Dialog open={open} onClose={busy ? undefined : onClose} maxWidth="xl" fullWidth>
      <DialogTitle>Сверка: Excel сводок и Cropwise</DialogTitle>
      <DialogContent dividers>
        <Stack spacing={2}>
          <Typography variant="body2" color="text.secondary">
            Дата сводок {summaryDate}. Сравниваются гектары за сутки и за сезон по всем
            агрооперациям: Excel сводок {reportIds.join(", ")} между собой и с отчётом Cropwise
            «Работы машин». Подчинённые юрлица (например, ООО «Агрофирма 5» у Дрожжаного) уже входят
            в свою агрофирму.
          </Typography>

          <Stack direction={{ xs: "column", md: "row" }} spacing={2} alignItems={{ md: "center" }}>
            <TextField
              size="small"
              label="Cookie Cropwise"
              placeholder={cookieConfigured ? "пусто — берём из .env" : "_cropio_ses=…"}
              value={cookie}
              onChange={(e) => setCookie(e.target.value)}
              disabled={busy}
              sx={{ flex: 1, minWidth: 260 }}
            />
            <TextField
              size="small"
              type="number"
              label="Допуск, га"
              value={tolerance}
              onChange={(e) => setTolerance(Math.max(0, Number(e.target.value) || 0))}
              disabled={busy}
              inputProps={{ step: 0.1, min: 0 }}
              sx={{ width: 120 }}
            />
            <FormControlLabel
              control={
                <Checkbox checked={regenerate} onChange={(e) => setRegenerate(e.target.checked)} disabled={busy} />
              }
              label={`Перегенерировать сводки ${reportIds.join(", ")}`}
            />
            <Button
              variant="contained"
              onClick={handleRun}
              disabled={busy}
              startIcon={busy ? <CircularProgress size={16} color="inherit" /> : null}
            >
              {busy ? "Идёт…" : "Проверить"}
            </Button>
          </Stack>

          {busy && <Alert severity="info">{progress}</Alert>}
          {error && <Alert severity="error">{error}</Alert>}
          {genWarning && <Alert severity="warning">{genWarning}</Alert>}

          {result && (
            <>
              <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
                {result.sources.map((s) => (
                  <Tooltip
                    key={s.id}
                    arrow
                    title={[s.file, s.error, ...(s.warnings || [])].filter(Boolean).join(" · ") || ""}
                  >
                    <Chip
                      size="small"
                      variant="outlined"
                      color={!s.found ? "error" : s.warnings?.length ? "warning" : "success"}
                      label={`Сводка ${s.id}${s.found ? "" : ": нет файла"}`}
                    />
                  </Tooltip>
                ))}
                <Tooltip
                  arrow
                  title={
                    !cw?.enabled
                      ? "Cookie не задана — сверка только между сводками"
                      : cwErrors.join(" · ") || "work.json по каждой операции, сутки и сезон"
                  }
                >
                  <Chip
                    size="small"
                    variant="outlined"
                    color={!cw?.enabled ? "default" : cwErrors.length ? "error" : "success"}
                    label={
                      !cw?.enabled
                        ? "Cropwise: выключен"
                        : cwErrors.length
                          ? `Cropwise: ошибки ${cwErrors.length}`
                          : "Cropwise"
                    }
                  />
                </Tooltip>
              </Stack>

              {cwErrors.map((text) => (
                <Alert key={text} severity="warning">
                  Cropwise: {text}
                </Alert>
              ))}
              {result.sources
                .filter((s) => s.error)
                .map((s) => (
                  <Alert key={s.id} severity="warning">
                    Сводка {s.id}: {s.error}
                  </Alert>
                ))}

              <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
                <Chip size="small" color="success" label={`Совпадает: ${summary.ok || 0}`} />
                <Chip size="small" color="error" label={`Сводки расходятся: ${(summary.svodki || 0) + (summary.both || 0)}`} />
                <Chip size="small" color="warning" label={`Расходится с Cropwise: ${(summary.cropwise || 0) + (summary.both || 0)}`} />
                <Typography variant="caption" color="text.secondary" sx={{ alignSelf: "center" }}>
                  смена {result.shift_date}, допуск {tol} га. Нажмите на операцию, чтобы открыть культуры.
                </Typography>
              </Stack>

              <Stack direction={{ xs: "column", md: "row" }} spacing={2} alignItems={{ md: "center" }}>
                <TextField select size="small" label="Агрофирма" value={firm} onChange={(e) => setFirm(e.target.value)} sx={{ minWidth: 220 }}>
                  <MenuItem value="*">Холдинг</MenuItem>
                  {firms.map((f) => (
                    <MenuItem key={f} value={f}>
                      {firmLabel(f)}
                    </MenuItem>
                  ))}
                </TextField>
                <TextField select size="small" label="Культура" value={crop} onChange={(e) => setCrop(e.target.value)} sx={{ minWidth: 200 }}>
                  <MenuItem value="*">Все</MenuItem>
                  {crops.map((c) => (
                    <MenuItem key={c} value={c}>
                      {cropLabel(c)}
                    </MenuItem>
                  ))}
                </TextField>
                <ToggleButtonGroup size="small" exclusive value={metric} onChange={(_, v) => v && setMetric(v)}>
                  <ToggleButton value="all">Сутки и сезон</ToggleButton>
                  <ToggleButton value="day">Сутки</ToggleButton>
                  <ToggleButton value="season">Сезон</ToggleButton>
                </ToggleButtonGroup>
                <FormControlLabel
                  control={<Checkbox checked={onlyDiff} onChange={(e) => setOnlyDiff(e.target.checked)} />}
                  label="Только расхождения"
                />
              </Stack>

              <Stack spacing={1}>
                {groups.map((group) => {
                  const openGroup = openOps.includes(group.op);
                  const meta = STATUS_META[group.status] || STATUS_META.single;
                  return (
                    <Box key={group.op} sx={{ border: 1, borderColor: "divider", borderRadius: 1 }}>
                      <Box
                        role="button"
                        tabIndex={0}
                        aria-expanded={openGroup}
                        onClick={() => toggleOp(group.op)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            toggleOp(group.op);
                          }
                        }}
                        sx={{
                          display: "flex",
                          alignItems: "center",
                          gap: 1,
                          px: 1,
                          py: 0.5,
                          cursor: "pointer",
                          "&:hover": { bgcolor: "action.hover" },
                        }}
                      >
                        {openGroup ? <ExpandMoreIcon fontSize="small" /> : <ChevronRightIcon fontSize="small" />}
                        <Typography sx={{ fontWeight: 600, minWidth: 220 }}>{group.op}</Typography>
                        <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap sx={{ flex: 1 }}>
                          {group.headline.map((row) => (
                            <Chip
                              key={`${row.crop}|${row.metric}`}
                              size="small"
                              variant="outlined"
                              label={`${crop === "*" ? METRIC_LABEL[row.metric] : `${cropLabel(row.crop)}, ${METRIC_LABEL[row.metric].toLowerCase()}`}: ${headlineValue(row)} га`}
                            />
                          ))}
                        </Stack>
                        <Chip size="small" variant="outlined" color={meta.color} label={meta.label} />
                      </Box>
                      <Collapse in={openGroup} unmountOnExit>
                        <Box sx={{ px: 1, pb: 1 }}>
                          {group.warnings.map((text) => (
                            <Alert key={text} severity="warning" sx={{ mb: 1 }}>
                              {text}
                            </Alert>
                          ))}
                          {group.seasonStart && (
                            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1 }}>
                              Сезон с {group.seasonStart}
                            </Typography>
                          )}
                          {group.crops.length ? (
                            <TableContainer>
                              <Table size="small">
                                <TableHead>
                                  <TableRow>
                                    <TableCell>Культура</TableCell>
                                    <TableCell>Показатель, га</TableCell>
                                    {group.reportIds.map((rid) => (
                                      <TableCell key={rid} align="right">
                                        Сводка {rid}
                                      </TableCell>
                                    ))}
                                    <TableCell align="right">Cropwise</TableCell>
                                    <TableCell align="right">Разброс сводок</TableCell>
                                    <TableCell>Статус</TableCell>
                                  </TableRow>
                                </TableHead>
                                <TableBody>
                                  {group.crops.map((r) => {
                                    const nums = Object.values(r.values).map((v) => v.value);
                                    const med = median(nums);
                                    const rowMeta = STATUS_META[r.status] || STATUS_META.single;
                                    return (
                                      <TableRow key={`${r.firm}|${r.crop}|${r.metric}`} hover>
                                        <TableCell>{cropLabel(r.crop)}</TableCell>
                                        <TableCell>{METRIC_LABEL[r.metric]}</TableCell>
                                        {group.reportIds.map((rid) => {
                                          const cell = r.values[rid];
                                          const off = cell && nums.length >= 2 && Math.abs(cell.value - med) > tol;
                                          return <ValueCell key={rid} cell={cell} off={off} />;
                                        })}
                                        <ValueCell cell={r[CROPWISE]} off={r.cropwise_diff != null && r.cropwise_diff > tol} />
                                        <TableCell align="right" sx={{ fontVariantNumeric: "tabular-nums" }}>
                                          {fmt(r.spread)}
                                        </TableCell>
                                        <TableCell>
                                          <Chip size="small" variant="outlined" color={rowMeta.color} label={rowMeta.label} />
                                        </TableCell>
                                      </TableRow>
                                    );
                                  })}
                                </TableBody>
                              </Table>
                            </TableContainer>
                          ) : (
                            <Typography variant="body2" color="text.secondary" sx={{ py: 1 }}>
                              По культурам разбивки нет.
                            </Typography>
                          )}
                        </Box>
                      </Collapse>
                    </Box>
                  );
                })}
                {!groups.length && (
                  <Box sx={{ py: 2, textAlign: "center", color: "text.secondary" }}>
                    {onlyDiff ? "Расхождений нет" : "Нет данных"}
                  </Box>
                )}
              </Stack>
              <Typography variant="caption" color="text.secondary">
                На строке операции — итог {firm === "*" ? "по холдингу" : `по «${firm}»`}. Курсивом — итог, которого нет в файле: досчитан суммой
                строк этой сводки. Наведите на число, чтобы увидеть лист и строку. Красным — значение,
                отличающееся от медианы сводок больше допуска.
              </Typography>
            </>
          )}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={busy}>
          Закрыть
        </Button>
      </DialogActions>
    </Dialog>
  );
}
