import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Autocomplete,
  Box,
  Button,
  Checkbox,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  FormControlLabel,
  IconButton,
  Link,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import CheckRoundedIcon from "@mui/icons-material/CheckRounded";
import ContentCopyOutlinedIcon from "@mui/icons-material/ContentCopyOutlined";
import DownloadOutlinedIcon from "@mui/icons-material/DownloadOutlined";
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";
import QuestionAnswerOutlinedIcon from "@mui/icons-material/QuestionAnswerOutlined";
import RefreshIcon from "@mui/icons-material/Refresh";

import { askTaskFacts, fetchProverkaAiSettings, fetchTaskFactsOptions, queryTaskFacts } from "../api";

const POPPER_PROPS = { popper: { sx: { zIndex: 1600 } } };
const FILTERS_STORAGE_KEY = "svodki_task_facts_filters_v1";

const EMPTY_FILTERS = {
  reportIds: [],
  group: "",
  crop: "",
  firm: "",
  machine: "",
  field: "",
  minHa: "",
  exact: false,
};

const CSV_COLUMNS = [
  ["firm", "Фирма"],
  ["crop", "Культура"],
  ["field", "Поле"],
  ["field_id", "ID поля"],
  ["machine", "Машина"],
  ["machine_id", "ID машины"],
  ["task_id", "Задание"],
  ["ha", "Га"],
  ["own", "Свои/наёмн."],
  ["season", "Сезон"],
  ["end", "Окончание"],
  ["date", "Дата"],
  ["report_id", "Сводка"],
  ["group", "Операция"],
  ["task_url", "Ссылка"],
];

// Подстроки из operation_registry.py — совпадение без учёта регистра, поэтому
// достаточно короткого ключевого слова из названия группы операций.
const QUICK_GROUPS = [
  { label: "МУ", value: "минеральных" },
  { label: "Боронование", value: "боронование" },
  { label: "Культивация", value: "культивация" },
  { label: "Посев", value: "посев" },
  { label: "Опрыскивание", value: "опрыскивание" },
  { label: "Вспашка", value: "вспашка" },
  { label: "Уборка", value: "уборка" },
];

function loadStoredFilters() {
  try {
    const raw = localStorage.getItem(FILTERS_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return {
      filters: { ...EMPTY_FILTERS, ...(parsed.filters || {}) },
      useLlm: parsed.useLlm !== undefined ? Boolean(parsed.useLlm) : true,
    };
  } catch {
    return null;
  }
}

function formatWhen(iso) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function csvEscape(value) {
  const s = value == null ? "" : String(value);
  return /["; \n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function rowsToCsv(rows) {
  const lines = [CSV_COLUMNS.map(([, label]) => csvEscape(label)).join(";")];
  for (const r of rows) {
    lines.push(CSV_COLUMNS.map(([key]) => csvEscape(r[key])).join(";"));
  }
  return `\uFEFF${lines.join("\r\n")}`;
}

function downloadTextFile(text, filename, mime) {
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export default function TaskFactsDialog({ open, onClose, summaryDate, initialJobId }) {
  const [reportDate, setReportDate] = useState(summaryDate || "");
  const [jobId, setJobId] = useState("");
  const [filters, setFilters] = useState(() => loadStoredFilters()?.filters || EMPTY_FILTERS);
  const [question, setQuestion] = useState("");
  const [useLlm, setUseLlm] = useState(() => loadStoredFilters()?.useLlm ?? true);

  const [options, setOptions] = useState(null);
  const [optionsLoading, setOptionsLoading] = useState(false);
  const [optionsNote, setOptionsNote] = useState("");

  const [aiKeySet, setAiKeySet] = useState(true);

  const [result, setResult] = useState(null);
  const [queryLoading, setQueryLoading] = useState(false);
  const [queryError, setQueryError] = useState("");

  const [answerHistory, setAnswerHistory] = useState([]);
  const [askLoading, setAskLoading] = useState(false);
  const [askError, setAskError] = useState("");
  const [copiedId, setCopiedId] = useState(null);

  // Дата подтягивается из шапки, job_id — либо явно передан (клик «Вопросы по
  // фактам» на карточке уже готовой проверки), либо вводится вручную.
  useEffect(() => {
    if (!open) return;
    setReportDate(summaryDate || "");
    if (initialJobId) setJobId(initialJobId);
  }, [open, summaryDate, initialJobId]);

  useEffect(() => {
    if (!open) {
      setQuestion("");
      setResult(null);
      setAnswerHistory([]);
      setQueryError("");
      setAskError("");
      setOptions(null);
      setOptionsNote("");
    }
  }, [open]);

  // Дата или job_id сменились — старые результаты относятся к другому
  // контексту, показывать их дальше может ввести в заблуждение.
  useEffect(() => {
    setResult(null);
    setQueryError("");
    setAnswerHistory([]);
    setAskError("");
  }, [reportDate, jobId]);

  // Фильтры и переключатель «модель» сохраняются между открытиями диалога.
  useEffect(() => {
    try {
      localStorage.setItem(FILTERS_STORAGE_KEY, JSON.stringify({ filters, useLlm }));
    } catch {
      // localStorage может быть недоступен (приватный режим и т.п.) — не критично
    }
  }, [filters, useLlm]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    fetchProverkaAiSettings()
      .then((data) => {
        if (!cancelled) setAiKeySet(Boolean(data?.api_key_set));
      })
      .catch(() => {
        if (!cancelled) setAiKeySet(true); // не смогли проверить — не пугаем зря
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  const loadOptions = useCallback(async () => {
    if (!reportDate) return;
    setOptionsLoading(true);
    setOptionsNote("");
    try {
      const data = await fetchTaskFactsOptions({ reportDate, jobId });
      setOptions(data);
      if (!data.available) setOptionsNote(data.detail || "Факты по заданиям за эту дату не найдены.");
    } catch (e) {
      setOptions(null);
      setOptionsNote(String(e.message || e));
    } finally {
      setOptionsLoading(false);
    }
  }, [reportDate, jobId]);

  useEffect(() => {
    if (open) loadOptions();
  }, [open, loadOptions]);

  function setFilter(key, value) {
    setFilters((f) => ({ ...f, [key]: value }));
  }

  const filtersActive = useMemo(
    () =>
      Boolean(
        filters.group ||
          filters.crop ||
          filters.firm ||
          filters.machine ||
          filters.field ||
          filters.minHa !== "" ||
          filters.exact ||
          (filters.reportIds && filters.reportIds.length > 0)
      ),
    [filters]
  );

  function handleResetFilters() {
    setFilters(EMPTY_FILTERS);
  }

  // Сумма по уже полученным в этом открытии диалога ответам — наглядно
  // показывает эффект от фильтрации (весь смысл фичи — экономия токенов).
  const sessionStats = useMemo(() => {
    let tokens = 0;
    for (const e of answerHistory) {
      const actual = (e.usage?.input_tokens || 0) + (e.usage?.output_tokens || 0);
      tokens += actual || e.est_input_tokens || 0;
    }
    return { count: answerHistory.length, tokens };
  }, [answerHistory]);

  // Явно известно (по /task-facts/options), что фактов на эту дату/job нет —
  // не даём кликать «в пустоту».
  const factsUnavailable = options?.available === false;

  const baseFilters = useMemo(
    () => ({
      reportDate,
      jobId: jobId || undefined,
      reportIds: filters.reportIds,
      group: filters.group,
      crop: filters.crop,
      firm: filters.firm,
      machine: filters.machine,
      field: filters.field,
      minHa: filters.minHa,
      exact: filters.exact,
    }),
    [reportDate, jobId, filters]
  );

  async function handleQuery() {
    setQueryLoading(true);
    setQueryError("");
    setResult(null);
    try {
      const data = await queryTaskFacts(baseFilters);
      setResult(data);
      if (data.available === false) setQueryError(data.detail || "Данных нет.");
    } catch (e) {
      setQueryError(String(e.message || e));
    } finally {
      setQueryLoading(false);
    }
  }

  async function handleAsk() {
    if (askLoading || !reportDate || !question.trim() || factsUnavailable) return;
    setAskLoading(true);
    setAskError("");
    try {
      const data = await askTaskFacts({ ...baseFilters, question, useLlm });
      if (data.available === false) {
        setAskError(data.detail || "Данных нет.");
      } else {
        setAnswerHistory((prev) => [{ id: `${Date.now()}`, question, ts: Date.now(), ...data }, ...prev]);
        setQuestion("");
      }
    } catch (e) {
      setAskError(String(e.message || e));
    } finally {
      setAskLoading(false);
    }
  }

  function handleQuestionKeyDown(e) {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      handleAsk();
    }
  }

  async function handleCopy(entry) {
    try {
      await navigator.clipboard.writeText(entry.answer || "");
      setCopiedId(entry.id);
      setTimeout(() => setCopiedId((c) => (c === entry.id ? null : c)), 1500);
    } catch {
      // буфер обмена недоступен (нет разрешения/не https) — просто не подтверждаем
    }
  }

  function handleExportCsv() {
    if (!result?.rows?.length) return;
    const jobPart = result.job_id ? `_${result.job_id}` : "";
    downloadTextFile(rowsToCsv(result.rows), `task_facts_${reportDate}${jobPart}.csv`, "text/csv;charset=utf-8;");
  }

  const groupOptions = options?.values?.group || [];
  const cropOptions = options?.values?.crop || [];
  const firmOptions = options?.values?.firm || [];
  const reportIdOptions = options?.values?.report_id || [];

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="md" scroll="paper">
      <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1 }}>
        <QuestionAnswerOutlinedIcon color="primary" />
        Вопросы по заданиям
        <Tooltip title="Ответ строится по уже сохранённым фактам заданий (задание × поле за смену) — без повторного расчёта сводок. В модель уходит только отфильтрованный срез.">
          <InfoOutlinedIcon fontSize="small" color="disabled" />
        </Tooltip>
      </DialogTitle>
      <DialogContent dividers>
        <Stack spacing={2}>
          {!aiKeySet && (
            <Alert severity="warning">
              Ключ DeepSeek не задан — «Спросить» вернёт таблицу без ответа модели. Задать ключ можно в меню
              AI, в разделе «DeepSeek».
            </Alert>
          )}

          <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5}>
            <TextField
              label="Дата"
              type="date"
              size="small"
              value={reportDate || ""}
              onChange={(e) => setReportDate(e.target.value)}
              slotProps={{ inputLabel: { shrink: true } }}
              sx={{ minWidth: 160 }}
            />
            <TextField
              label="job_id (необязательно)"
              size="small"
              value={jobId}
              onChange={(e) => setJobId(e.target.value)}
              placeholder="по умолчанию — самый свежий job с фактами"
              fullWidth
            />
            <Tooltip title="Обновить список фильтров">
              <span>
                <IconButton onClick={loadOptions} disabled={optionsLoading || !reportDate}>
                  <RefreshIcon fontSize="small" />
                </IconButton>
              </span>
            </Tooltip>
          </Stack>

          {optionsLoading && (
            <Stack direction="row" spacing={1} alignItems="center">
              <CircularProgress size={16} />
              <Typography variant="caption" color="text.secondary">
                Загрузка фильтров…
              </Typography>
            </Stack>
          )}
          {!optionsLoading && optionsNote && <Alert severity="info">{optionsNote}</Alert>}
          {!optionsLoading && options?.available && (
            <Typography variant="caption" color="text.secondary">
              Снимок job {options.job_id} от {formatWhen(options.generated_at)}: {options.rows} строк, {options.ha_total} га.
            </Typography>
          )}

          <Divider />

          <Stack direction="row" justifyContent="space-between" alignItems="center">
            <Typography variant="subtitle2" color="text.secondary">
              Фильтры
            </Typography>
            <Button size="small" color="inherit" onClick={handleResetFilters} disabled={!filtersActive}>
              Сбросить фильтры
            </Button>
          </Stack>

          <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5}>
            <Autocomplete
              freeSolo
              size="small"
              fullWidth
              options={groupOptions}
              inputValue={filters.group}
              onInputChange={(_, v) => setFilter("group", v)}
              slotProps={POPPER_PROPS}
              renderInput={(params) => (
                <TextField {...params} label="Операция (группа)" placeholder="напр. минеральных" />
              )}
            />
            <Autocomplete
              freeSolo
              size="small"
              fullWidth
              options={cropOptions}
              inputValue={filters.crop}
              onInputChange={(_, v) => setFilter("crop", v)}
              slotProps={POPPER_PROPS}
              renderInput={(params) => <TextField {...params} label="Культура" placeholder="напр. соя" />}
            />
            <Autocomplete
              freeSolo
              size="small"
              fullWidth
              options={firmOptions}
              inputValue={filters.firm}
              onInputChange={(_, v) => setFilter("firm", v)}
              slotProps={POPPER_PROPS}
              renderInput={(params) => <TextField {...params} label="Агрофирма" />}
            />
          </Stack>

          <Stack direction="row" spacing={0.75} flexWrap="wrap" useFlexGap alignItems="center">
            <Typography variant="caption" color="text.secondary" sx={{ mr: 0.5 }}>
              Часто:
            </Typography>
            {QUICK_GROUPS.map((g) => (
              <Chip
                key={g.value}
                size="small"
                variant={filters.group === g.value ? "filled" : "outlined"}
                color={filters.group === g.value ? "primary" : "default"}
                label={g.label}
                onClick={() => setFilter("group", filters.group === g.value ? "" : g.value)}
              />
            ))}
          </Stack>

          <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} alignItems="center">
            <TextField
              size="small"
              fullWidth
              label="Машина"
              value={filters.machine}
              onChange={(e) => setFilter("machine", e.target.value)}
            />
            <TextField
              size="small"
              fullWidth
              label="Поле"
              value={filters.field}
              onChange={(e) => setFilter("field", e.target.value)}
            />
            <TextField
              size="small"
              label="Мин. га"
              type="number"
              value={filters.minHa}
              onChange={(e) => setFilter("minHa", e.target.value)}
              sx={{ minWidth: 110 }}
            />
            <FormControlLabel
              sx={{ whiteSpace: "nowrap" }}
              control={
                <Checkbox
                  size="small"
                  checked={filters.exact}
                  onChange={(e) => setFilter("exact", e.target.checked)}
                />
              }
              label="точное совпадение"
            />
          </Stack>

          <Autocomplete
            multiple
            size="small"
            options={reportIdOptions}
            value={filters.reportIds}
            onChange={(_, v) => setFilter("reportIds", v)}
            slotProps={POPPER_PROPS}
            renderTags={(value, getTagProps) =>
              value.map((option, index) => {
                const { key, ...tagProps } = getTagProps({ index });
                return <Chip key={key} size="small" label={`сводка ${option}`} {...tagProps} />;
              })
            }
            renderInput={(params) => (
              <TextField {...params} label="Сводки (необязательно, по умолчанию — все)" />
            )}
          />

          <Stack direction="row" spacing={1.5} alignItems="center">
            <Button
              variant="outlined"
              onClick={handleQuery}
              disabled={queryLoading || !reportDate || factsUnavailable}
            >
              {queryLoading ? "Загрузка…" : "Показать список (0 токенов)"}
            </Button>
            {result?.rows?.length > 0 && (
              <Tooltip title="Скачать отфильтрованную таблицу в CSV">
                <Button
                  size="small"
                  variant="text"
                  startIcon={<DownloadOutlinedIcon fontSize="small" />}
                  onClick={handleExportCsv}
                >
                  CSV
                </Button>
              </Tooltip>
            )}
          </Stack>

          {queryError && <Alert severity="warning">{queryError}</Alert>}
          {result?.available && (
            <Box>
              <Typography variant="caption" color="text.secondary" display="block" sx={{ mb: 0.5 }}>
                Найдено {result.rows_total} строк, {result.ha_total} га
                {result.truncated ? ` (показаны первые ${result.rows.length})` : ""}.
                {result.by_firm?.length ? (
                  <> {" "}По фирмам: {result.by_firm.map((f) => `${f.firm || "—"}=${f.ha}`).join("; ")}.</>
                ) : null}
              </Typography>
              <Box
                sx={{
                  maxHeight: 260,
                  overflow: "auto",
                  border: 1,
                  borderColor: "divider",
                  borderRadius: 1,
                }}
              >
                <Table size="small" stickyHeader>
                  <TableHead>
                    <TableRow>
                      <TableCell>Фирма</TableCell>
                      <TableCell>Культура</TableCell>
                      <TableCell>Поле</TableCell>
                      <TableCell>Машина</TableCell>
                      <TableCell>Задание</TableCell>
                      <TableCell align="right">Га</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {result.rows.map((r, i) => (
                      <TableRow key={`${r.task_id}-${r.field_id}-${i}`} hover>
                        <TableCell>{r.firm}</TableCell>
                        <TableCell>{r.crop}</TableCell>
                        <TableCell>{r.field}</TableCell>
                        <TableCell>{r.machine}</TableCell>
                        <TableCell>
                          {r.task_url ? (
                            <Link href={r.task_url} target="_blank" rel="noopener noreferrer">
                              #{r.task_id}
                            </Link>
                          ) : (
                            r.task_id
                          )}
                        </TableCell>
                        <TableCell align="right">{r.ha}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </Box>
            </Box>
          )}

          <Divider />

          <TextField
            label="Вопрос для DeepSeek"
            placeholder="Например: какие задания были по МУ на сое?"
            multiline
            minRows={2}
            fullWidth
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={handleQuestionKeyDown}
            helperText="Ctrl+Enter — отправить"
          />
          <Stack direction="row" spacing={1.5} alignItems="center">
            <Button
              variant="contained"
              onClick={handleAsk}
              disabled={askLoading || !reportDate || !question.trim() || factsUnavailable}
            >
              {askLoading ? "Спрашиваю…" : "Спросить"}
            </Button>
            <FormControlLabel
              control={
                <Checkbox size="small" checked={useLlm} onChange={(e) => setUseLlm(e.target.checked)} />
              }
              label="Использовать модель (иначе — просто таблица)"
            />
            {answerHistory.length > 0 && (
              <Stack direction="row" spacing={1} alignItems="center" sx={{ ml: "auto" }}>
                <Chip
                  size="small"
                  variant="outlined"
                  label={`за сессию: ${sessionStats.count} · ~${sessionStats.tokens} ток.`}
                />
                <Button size="small" color="inherit" onClick={() => setAnswerHistory([])}>
                  Очистить историю
                </Button>
              </Stack>
            )}
          </Stack>

          {askError && <Alert severity="warning">{askError}</Alert>}

          {answerHistory.map((entry) => (
            <Box key={entry.id} sx={{ p: 1.5, border: 1, borderColor: "divider", borderRadius: 1 }}>
              <Stack direction="row" justifyContent="space-between" alignItems="flex-start" spacing={1}>
                <Typography variant="caption" color="text.secondary" sx={{ fontStyle: "italic" }}>
                  {entry.question} · {formatWhen(new Date(entry.ts).toISOString())}
                </Typography>
                <Tooltip title={copiedId === entry.id ? "Скопировано" : "Скопировать ответ"}>
                  <IconButton size="small" onClick={() => handleCopy(entry)}>
                    {copiedId === entry.id ? (
                      <CheckRoundedIcon fontSize="small" color="success" />
                    ) : (
                      <ContentCopyOutlinedIcon fontSize="small" />
                    )}
                  </IconButton>
                </Tooltip>
              </Stack>
              <Typography variant="body2" sx={{ whiteSpace: "pre-wrap", mt: 0.5 }}>
                {entry.answer}
              </Typography>
              <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap sx={{ mt: 1.5 }}>
                <Chip
                  size="small"
                  color={entry.llm_used ? "primary" : "default"}
                  label={entry.llm_used ? `модель: ${entry.model}` : "без модели"}
                />
                <Chip size="small" variant="outlined" label={`строк ${entry.rows_sent}/${entry.rows_total}`} />
                <Chip size="small" variant="outlined" label={`${entry.ha_total} га`} />
                {entry.est_input_tokens != null && (
                  <Chip size="small" variant="outlined" label={`оценка: ≈${entry.est_input_tokens} вх. токенов`} />
                )}
                {entry.usage?.input_tokens != null && (
                  <Chip
                    size="small"
                    variant="outlined"
                    label={`факт: ${entry.usage.input_tokens}/${entry.usage.output_tokens ?? "?"} токенов${
                      entry.usage.cache_hit_tokens ? `, кэш ${entry.usage.cache_hit_tokens}` : ""
                    }`}
                  />
                )}
                {entry.error && <Chip size="small" color="warning" label={entry.error} />}
              </Stack>
            </Box>
          ))}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Закрыть</Button>
      </DialogActions>
    </Dialog>
  );
}
