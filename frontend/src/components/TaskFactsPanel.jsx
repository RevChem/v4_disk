import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Autocomplete,
  Box,
  Button,
  Checkbox,
  Chip,
  CircularProgress,
  FormControlLabel,
  IconButton,
  Link,
  Paper,
  Stack,
  Tab,
  Tabs,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import { alpha } from "@mui/material/styles";
import AutoAwesomeOutlinedIcon from "@mui/icons-material/AutoAwesomeOutlined";
import CheckRoundedIcon from "@mui/icons-material/CheckRounded";
import ContentCopyOutlinedIcon from "@mui/icons-material/ContentCopyOutlined";
import DownloadOutlinedIcon from "@mui/icons-material/DownloadOutlined";
import RefreshIcon from "@mui/icons-material/Refresh";
import SendRoundedIcon from "@mui/icons-material/SendRounded";
import TableChartOutlinedIcon from "@mui/icons-material/TableChartOutlined";

import { askTaskFacts, fetchProverkaAiSettings, fetchTaskFactsOptions, queryTaskFacts } from "../api";
import { POPPER_PROPS, SortableTable, downloadCsv, fmt } from "./analysis/common";

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

// Подстроки из operation_registry.py — совпадение без учёта регистра.
const QUICK_GROUPS = [
  { label: "МУ", value: "минеральных" },
  { label: "Боронование", value: "боронование" },
  { label: "Культивация", value: "культивация" },
  { label: "Посев", value: "посев" },
  { label: "Опрыскивание", value: "опрыскивание" },
  { label: "Вспашка", value: "вспашка" },
  { label: "Уборка", value: "уборка" },
];

const EXAMPLES = [
  "Сколько гектаров по каждой агрофирме?",
  "Какие 10 заданий с наибольшей площадью?",
  "Какие машины работали и сколько га у каждой?",
  "Есть ли поля, где работали разные машины?",
];

const TABLE_COLUMNS = [
  { key: "firm", label: "Фирма", wrap: true },
  { key: "crop", label: "Культура", wrap: true },
  { key: "field", label: "Поле", wrap: true },
  { key: "machine", label: "Машина", wrap: true },
  {
    key: "task_id",
    label: "Задание",
    render: (r) =>
      r.task_url ? (
        <Link href={r.task_url} target="_blank" rel="noopener noreferrer">
          #{r.task_id}
        </Link>
      ) : (
        `#${r.task_id}`
      ),
    csv: (r) => r.task_url || r.task_id,
  },
  { key: "group", label: "Операция", wrap: true },
  { key: "own", label: "Техника", render: (r) => (r.own === "" || r.own == null ? "—" : String(r.own)) },
  { key: "ha", label: "Га", align: "right", render: (r) => fmt(r.ha, 2) },
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
    return new Date(iso).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  } catch {
    return iso;
  }
}

function SectionTitle({ children }) {
  return (
    <Typography variant="overline" color="text.secondary" sx={{ lineHeight: 1.6, letterSpacing: 0.8 }}>
      {children}
    </Typography>
  );
}

export default function TaskFactsPanel({ active = true, summaryDate, initialJobId }) {
  const [reportDate, setReportDate] = useState(summaryDate || "");
  const [jobId, setJobId] = useState(initialJobId || "");
  const [filters, setFilters] = useState(() => loadStoredFilters()?.filters || EMPTY_FILTERS);
  const [question, setQuestion] = useState("");
  const [useLlm, setUseLlm] = useState(() => loadStoredFilters()?.useLlm ?? true);
  const [view, setView] = useState("answers");

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
  const feedEnd = useRef(null);

  useEffect(() => {
    setReportDate(summaryDate || "");
  }, [summaryDate]);

  useEffect(() => {
    if (initialJobId) setJobId(initialJobId);
  }, [initialJobId]);

  // Дата или job_id сменились: старые результаты относятся к другому снимку.
  useEffect(() => {
    setResult(null);
    setQueryError("");
    setAnswerHistory([]);
    setAskError("");
  }, [reportDate, jobId]);

  useEffect(() => {
    try {
      localStorage.setItem(FILTERS_STORAGE_KEY, JSON.stringify({ filters, useLlm }));
    } catch {
      // localStorage может быть недоступен — не критично
    }
  }, [filters, useLlm]);

  useEffect(() => {
    if (!active) return undefined;
    let cancelled = false;
    fetchProverkaAiSettings()
      .then((data) => {
        if (!cancelled) setAiKeySet(Boolean(data?.api_key_set));
      })
      .catch(() => {
        if (!cancelled) setAiKeySet(true);
      });
    return () => {
      cancelled = true;
    };
  }, [active]);

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
    if (active) loadOptions();
  }, [active, loadOptions]);

  useEffect(() => {
    feedEnd.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [answerHistory.length, askLoading]);

  const setFilter = (key, value) => setFilters((f) => ({ ...f, [key]: value }));

  const filtersActive = useMemo(
    () =>
      Boolean(
        filters.group || filters.crop || filters.firm || filters.machine || filters.field ||
          filters.minHa !== "" || filters.exact || (filters.reportIds && filters.reportIds.length > 0),
      ),
    [filters],
  );

  const sessionStats = useMemo(() => {
    let tokens = 0;
    for (const e of answerHistory) {
      const actual = (e.usage?.input_tokens || 0) + (e.usage?.output_tokens || 0);
      tokens += actual || e.est_input_tokens || 0;
    }
    return { count: answerHistory.length, tokens };
  }, [answerHistory]);

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
    [reportDate, jobId, filters],
  );

  async function runQuery() {
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

  function showTable() {
    setView("table");
    runQuery();
  }

  async function handleAsk(text) {
    const q = (text ?? question).trim();
    if (askLoading || !reportDate || !q || factsUnavailable) return;
    setAskLoading(true);
    setAskError("");
    setView("answers");
    try {
      const data = await askTaskFacts({ ...baseFilters, question: q, useLlm });
      if (data.available === false) {
        setAskError(data.detail || "Данных нет.");
      } else {
        setAnswerHistory((prev) => [...prev, { id: `${Date.now()}`, question: q, ts: Date.now(), ...data }]);
        setQuestion("");
      }
    } catch (e) {
      setAskError(String(e.message || e));
    } finally {
      setAskLoading(false);
    }
  }

  function onQuestionKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey && (e.ctrlKey || e.metaKey || !e.nativeEvent.isComposing)) {
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
      // буфер обмена недоступен — просто не подтверждаем
    }
  }

  function exportCsv() {
    if (!result?.rows?.length) return;
    const jobPart = result.job_id ? `_${result.job_id}` : "";
    downloadCsv(`task_facts_${reportDate}${jobPart}.csv`, TABLE_COLUMNS, result.rows);
  }

  const groupOptions = options?.values?.group || [];
  const cropOptions = options?.values?.crop || [];
  const firmOptions = options?.values?.firm || [];
  const reportIdOptions = options?.values?.report_id || [];
  const canAsk = !askLoading && Boolean(reportDate) && Boolean(question.trim()) && !factsUnavailable;

  return (
    <Box
      sx={{
        display: "grid",
        gridTemplateColumns: { xs: "1fr", md: "340px minmax(0, 1fr)" },
        gap: 2,
        height: "100%",
        minHeight: 0,
        overflow: { xs: "auto", md: "hidden" },
      }}
    >
      {/* ───────── Фильтры ───────── */}
      <Paper variant="outlined" sx={{ display: "flex", flexDirection: "column", minHeight: 0, overflow: "hidden" }}>
        <Stack spacing={1.5} sx={{ p: 2, flex: 1, minHeight: 0, overflow: "auto" }}>
          <Stack direction="row" alignItems="center" justifyContent="space-between">
            <SectionTitle>Срез данных</SectionTitle>
            <Tooltip title="Обновить списки значений">
              <span>
                <IconButton size="small" onClick={loadOptions} disabled={optionsLoading || !reportDate}>
                  {optionsLoading ? <CircularProgress size={16} /> : <RefreshIcon fontSize="small" />}
                </IconButton>
              </span>
            </Tooltip>
          </Stack>

          <TextField
            label="Дата сводки"
            type="date"
            size="small"
            fullWidth
            value={reportDate || ""}
            onChange={(e) => setReportDate(e.target.value)}
            slotProps={{ inputLabel: { shrink: true } }}
          />
          <TextField
            label="job_id"
            size="small"
            fullWidth
            value={jobId}
            onChange={(e) => setJobId(e.target.value)}
            placeholder="по умолчанию — самый свежий"
          />

          {!optionsLoading && optionsNote && <Alert severity="info" sx={{ py: 0.25 }}>{optionsNote}</Alert>}
          {!optionsLoading && options?.available && (
            <Typography variant="caption" color="text.secondary">
              Снимок {formatWhen(options.generated_at)}: {fmt(options.rows, 0)} строк, {fmt(options.ha_total, 0)} га.
            </Typography>
          )}

          <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ pt: 0.5 }}>
            <SectionTitle>Фильтры</SectionTitle>
            <Button size="small" color="inherit" onClick={() => setFilters(EMPTY_FILTERS)} disabled={!filtersActive}>
              Сбросить
            </Button>
          </Stack>

          <Stack direction="row" spacing={0.75} flexWrap="wrap" useFlexGap>
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

          <Autocomplete
            freeSolo size="small" fullWidth options={groupOptions}
            inputValue={filters.group} onInputChange={(_, v) => setFilter("group", v)} slotProps={POPPER_PROPS}
            renderInput={(params) => <TextField {...params} label="Операция" placeholder="напр. минеральных" />}
          />
          <Autocomplete
            freeSolo size="small" fullWidth options={cropOptions}
            inputValue={filters.crop} onInputChange={(_, v) => setFilter("crop", v)} slotProps={POPPER_PROPS}
            renderInput={(params) => <TextField {...params} label="Культура" placeholder="напр. соя" />}
          />
          <Autocomplete
            freeSolo size="small" fullWidth options={firmOptions}
            inputValue={filters.firm} onInputChange={(_, v) => setFilter("firm", v)} slotProps={POPPER_PROPS}
            renderInput={(params) => <TextField {...params} label="Агрофирма" />}
          />
          <Stack direction="row" spacing={1}>
            <TextField size="small" fullWidth label="Машина" value={filters.machine} onChange={(e) => setFilter("machine", e.target.value)} />
            <TextField size="small" fullWidth label="Поле" value={filters.field} onChange={(e) => setFilter("field", e.target.value)} />
          </Stack>
          <Stack direction="row" spacing={1} alignItems="center">
            <TextField size="small" label="Мин. га" type="number" value={filters.minHa} onChange={(e) => setFilter("minHa", e.target.value)} sx={{ width: 110 }} />
            <FormControlLabel
              sx={{ m: 0 }}
              control={<Checkbox size="small" checked={filters.exact} onChange={(e) => setFilter("exact", e.target.checked)} />}
              label={<Typography variant="body2">точное совпадение</Typography>}
            />
          </Stack>
          <Autocomplete
            multiple size="small" options={reportIdOptions} value={filters.reportIds}
            onChange={(_, v) => setFilter("reportIds", v)} slotProps={POPPER_PROPS}
            renderTags={(value, getTagProps) =>
              value.map((option, index) => {
                const { key, ...tagProps } = getTagProps({ index });
                return <Chip key={key} size="small" label={`сводка ${option}`} {...tagProps} />;
              })
            }
            renderInput={(params) => <TextField {...params} label="Сводки (все, если пусто)" />}
          />

        </Stack>
        <Box sx={{ p: 1.5, borderTop: 1, borderColor: "divider" }}>
          <Button
            fullWidth
            variant="outlined"
            startIcon={<TableChartOutlinedIcon />}
            onClick={showTable}
            disabled={queryLoading || !reportDate || factsUnavailable}
          >
            {queryLoading ? "Загрузка…" : "Показать таблицу · 0 токенов"}
          </Button>
        </Box>
      </Paper>

      {/* ───────── Ответы и таблица ───────── */}
      <Paper variant="outlined" sx={{ display: "flex", flexDirection: "column", minHeight: { xs: 560, md: 0 }, overflow: "hidden" }}>
        <Stack direction="row" alignItems="center" sx={{ px: 1.5, borderBottom: 1, borderColor: "divider" }} spacing={1}>
          <Tabs value={view} onChange={(_, v) => setView(v)} sx={{ minHeight: 44 }}>
            <Tab value="answers" label={`Ответы${answerHistory.length ? ` (${answerHistory.length})` : ""}`} sx={{ minHeight: 44 }} />
            <Tab value="table" label={`Таблица${result?.available ? ` (${fmt(result.rows_total, 0)})` : ""}`} sx={{ minHeight: 44 }} />
          </Tabs>
          <Box sx={{ flex: 1 }} />
          {view === "answers" && answerHistory.length > 0 && (
            <>
              <Chip size="small" variant="outlined" label={`за сессию: ${sessionStats.count} · ~${fmt(sessionStats.tokens, 0)} ток.`} />
              <Button size="small" color="inherit" onClick={() => setAnswerHistory([])}>Очистить</Button>
            </>
          )}
          {view === "table" && result?.rows?.length > 0 && (
            <Button size="small" startIcon={<DownloadOutlinedIcon fontSize="small" />} onClick={exportCsv}>CSV</Button>
          )}
        </Stack>

        {view === "answers" && (
          <>
            <Box sx={{ flex: 1, minHeight: 0, overflow: "auto", p: { xs: 1.5, md: 2.5 } }}>
              {!aiKeySet && (
                <Alert severity="warning" sx={{ mb: 2 }}>
                  Ключ DeepSeek не задан: «Спросить» вернёт таблицу без ответа модели. Ключ задаётся в меню AI, раздел «DeepSeek».
                </Alert>
              )}
              {!answerHistory.length && !askLoading && (
                <Stack alignItems="center" justifyContent="center" spacing={2} sx={{ minHeight: "100%", textAlign: "center", py: 4 }}>
                  <AutoAwesomeOutlinedIcon color="primary" sx={{ fontSize: 44, opacity: 0.8 }} />
                  <Box>
                    <Typography variant="h6" fontWeight={700}>Спросите по заданиям</Typography>
                    <Typography variant="body2" color="text.secondary" sx={{ maxWidth: 520, mt: 0.5 }}>
                      Сначала задайте срез слева (операция, культура, фирма), затем вопрос. Модель увидит только этот срез,
                      поэтому чем уже срез, тем дешевле и точнее ответ.
                    </Typography>
                  </Box>
                  <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap justifyContent="center" sx={{ maxWidth: 640 }}>
                    {EXAMPLES.map((ex) => (
                      <Chip key={ex} variant="outlined" label={ex} onClick={() => setQuestion(ex)} />
                    ))}
                  </Stack>
                </Stack>
              )}

              <Stack spacing={2.5}>
                {answerHistory.map((entry) => (
                  <Box key={entry.id}>
                    <Box sx={{ display: "flex", justifyContent: "flex-end", mb: 1 }}>
                      <Box
                        sx={(theme) => ({
                          maxWidth: "80%",
                          px: 1.75,
                          py: 1,
                          borderRadius: 2,
                          borderTopRightRadius: 4,
                          bgcolor: alpha(theme.palette.primary.main, theme.palette.mode === "dark" ? 0.22 : 0.1),
                        })}
                      >
                        <Typography variant="body2" sx={{ whiteSpace: "pre-wrap" }}>{entry.question}</Typography>
                      </Box>
                    </Box>
                    <Paper variant="outlined" sx={{ p: 2, borderRadius: 2, borderTopLeftRadius: 4 }}>
                      <Stack direction="row" justifyContent="space-between" alignItems="flex-start" spacing={1}>
                        <Typography variant="body1" sx={{ whiteSpace: "pre-wrap", lineHeight: 1.65, flex: 1 }}>
                          {entry.answer || "Пустой ответ"}
                        </Typography>
                        <Tooltip title={copiedId === entry.id ? "Скопировано" : "Скопировать ответ"}>
                          <IconButton size="small" onClick={() => handleCopy(entry)}>
                            {copiedId === entry.id ? <CheckRoundedIcon fontSize="small" color="success" /> : <ContentCopyOutlinedIcon fontSize="small" />}
                          </IconButton>
                        </Tooltip>
                      </Stack>
                      <Stack direction="row" spacing={0.75} flexWrap="wrap" useFlexGap sx={{ mt: 1.5 }}>
                        <Chip size="small" color={entry.llm_used ? "primary" : "default"} label={entry.llm_used ? `модель: ${entry.model}` : "без модели"} />
                        <Chip size="small" variant="outlined" label={`строк ${entry.rows_sent ?? "—"}/${entry.rows_total ?? "—"}`} />
                        <Chip size="small" variant="outlined" label={`${fmt(entry.ha_total, 1)} га`} />
                        {entry.usage?.input_tokens != null ? (
                          <Chip
                            size="small"
                            variant="outlined"
                            label={`${entry.usage.input_tokens}/${entry.usage.output_tokens ?? "?"} токенов${entry.usage.cache_hit_tokens ? `, кэш ${entry.usage.cache_hit_tokens}` : ""}`}
                          />
                        ) : (
                          entry.est_input_tokens != null && <Chip size="small" variant="outlined" label={`≈${entry.est_input_tokens} вх. токенов`} />
                        )}
                        {entry.error && <Chip size="small" color="warning" label={entry.error} />}
                      </Stack>
                    </Paper>
                  </Box>
                ))}
                {askLoading && (
                  <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }}>
                    <Stack direction="row" spacing={1.5} alignItems="center">
                      <CircularProgress size={18} />
                      <Typography variant="body2" color="text.secondary">Читаю срез и отвечаю…</Typography>
                    </Stack>
                  </Paper>
                )}
                {askError && <Alert severity="warning">{askError}</Alert>}
                <div ref={feedEnd} />
              </Stack>
            </Box>

            <Box sx={{ p: 1.5, borderTop: 1, borderColor: "divider", bgcolor: "background.paper" }}>
              <TextField
                placeholder="Например: какие задания были по МУ на сое?"
                multiline
                minRows={2}
                maxRows={6}
                fullWidth
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                onKeyDown={onQuestionKeyDown}
                disabled={factsUnavailable}
              />
              <Stack direction="row" alignItems="center" spacing={1.5} sx={{ mt: 1 }}>
                <FormControlLabel
                  sx={{ m: 0 }}
                  control={<Checkbox size="small" checked={useLlm} onChange={(e) => setUseLlm(e.target.checked)} />}
                  label={<Typography variant="body2">Использовать модель (иначе таблица)</Typography>}
                />
                <Box sx={{ flex: 1 }} />
                <Typography variant="caption" color="text.secondary" sx={{ display: { xs: "none", sm: "block" } }}>
                  Enter — отправить, Shift+Enter — новая строка
                </Typography>
                <Button variant="contained" onClick={() => handleAsk()} disabled={!canAsk} endIcon={<SendRoundedIcon />}>
                  {askLoading ? "Спрашиваю…" : "Спросить"}
                </Button>
              </Stack>
            </Box>
          </>
        )}

        {view === "table" && (
          <Box sx={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column", p: { xs: 1.5, md: 2 }, gap: 1.5 }}>
            {queryLoading && (
              <Stack direction="row" spacing={1} alignItems="center">
                <CircularProgress size={16} />
                <Typography variant="body2" color="text.secondary">Загрузка…</Typography>
              </Stack>
            )}
            {queryError && <Alert severity="warning">{queryError}</Alert>}
            {!result && !queryLoading && !queryError && (
              <Typography variant="body2" color="text.secondary" sx={{ py: 6, textAlign: "center" }}>
                Задайте срез слева и нажмите «Показать таблицу».
              </Typography>
            )}
            {result?.available && (
              <>
                <Stack direction="row" spacing={0.75} flexWrap="wrap" useFlexGap alignItems="center">
                  <Chip color="primary" size="small" label={`${fmt(result.rows_total, 0)} строк · ${fmt(result.ha_total, 1)} га`} />
                  {(result.by_firm || []).slice(0, 8).map((f) => (
                    <Chip key={f.firm || "none"} size="small" variant="outlined" label={`${f.firm || "—"}: ${fmt(f.ha, 1)} га`} />
                  ))}
                  {result.truncated && <Chip size="small" color="warning" label={`показаны первые ${result.rows.length}`} />}
                </Stack>
                <Box sx={{ flex: 1, minHeight: 0 }}>
                  <SortableTable
                    fill
                    columns={TABLE_COLUMNS}
                    rows={result.rows}
                    rowKey={(r, i) => `${r.task_id}-${r.field_id}-${i}`}
                    defaultSort="ha"
                    emptyText="По этому срезу строк нет"
                  />
                </Box>
              </>
            )}
          </Box>
        )}
      </Paper>
    </Box>
  );
}
