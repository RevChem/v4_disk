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
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";
import QuestionAnswerOutlinedIcon from "@mui/icons-material/QuestionAnswerOutlined";
import RefreshIcon from "@mui/icons-material/Refresh";

import { askTaskFacts, fetchTaskFactsOptions, queryTaskFacts } from "../api";

const POPPER_PROPS = { popper: { sx: { zIndex: 1600 } } };

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

export default function TaskFactsDialog({ open, onClose, summaryDate }) {
  const [reportDate, setReportDate] = useState(summaryDate || "");
  const [jobId, setJobId] = useState("");
  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const [question, setQuestion] = useState("");
  const [useLlm, setUseLlm] = useState(true);

  const [options, setOptions] = useState(null);
  const [optionsLoading, setOptionsLoading] = useState(false);
  const [optionsNote, setOptionsNote] = useState("");

  const [result, setResult] = useState(null);
  const [queryLoading, setQueryLoading] = useState(false);
  const [queryError, setQueryError] = useState("");

  const [answer, setAnswer] = useState(null);
  const [askLoading, setAskLoading] = useState(false);
  const [askError, setAskError] = useState("");

  useEffect(() => {
    if (open) setReportDate(summaryDate || "");
  }, [open, summaryDate]);

  useEffect(() => {
    if (!open) {
      setFilters(EMPTY_FILTERS);
      setQuestion("");
      setResult(null);
      setAnswer(null);
      setQueryError("");
      setAskError("");
      setOptions(null);
      setOptionsNote("");
    }
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
    setAskLoading(true);
    setAskError("");
    setAnswer(null);
    try {
      const data = await askTaskFacts({ ...baseFilters, question, useLlm });
      setAnswer(data);
      if (data.available === false) setAskError(data.detail || "Данных нет.");
    } catch (e) {
      setAskError(String(e.message || e));
    } finally {
      setAskLoading(false);
    }
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

          <Stack direction="row" spacing={1}>
            <Button variant="outlined" onClick={handleQuery} disabled={queryLoading || !reportDate}>
              {queryLoading ? "Загрузка…" : "Показать список (0 токенов)"}
            </Button>
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
          />
          <Stack direction="row" spacing={1.5} alignItems="center">
            <Button
              variant="contained"
              onClick={handleAsk}
              disabled={askLoading || !reportDate || !question.trim()}
            >
              {askLoading ? "Спрашиваю…" : "Спросить"}
            </Button>
            <FormControlLabel
              control={
                <Checkbox size="small" checked={useLlm} onChange={(e) => setUseLlm(e.target.checked)} />
              }
              label="Использовать модель (иначе — просто таблица)"
            />
          </Stack>

          {askError && <Alert severity="warning">{askError}</Alert>}
          {answer && (
            <Box sx={{ p: 1.5, border: 1, borderColor: "divider", borderRadius: 1 }}>
              <Typography variant="body2" sx={{ whiteSpace: "pre-wrap" }}>
                {answer.answer}
              </Typography>
              <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap sx={{ mt: 1.5 }}>
                <Chip
                  size="small"
                  color={answer.llm_used ? "primary" : "default"}
                  label={answer.llm_used ? `модель: ${answer.model}` : "без модели"}
                />
                <Chip size="small" variant="outlined" label={`строк ${answer.rows_sent}/${answer.rows_total}`} />
                <Chip size="small" variant="outlined" label={`${answer.ha_total} га`} />
                {answer.est_input_tokens != null && (
                  <Chip size="small" variant="outlined" label={`оценка: ≈${answer.est_input_tokens} вх. токенов`} />
                )}
                {answer.usage?.input_tokens != null && (
                  <Chip
                    size="small"
                    variant="outlined"
                    label={`факт: ${answer.usage.input_tokens}/${answer.usage.output_tokens ?? "?"} токенов${
                      answer.usage.cache_hit_tokens ? `, кэш ${answer.usage.cache_hit_tokens}` : ""
                    }`}
                  />
                )}
                {answer.error && <Chip size="small" color="warning" label={answer.error} />}
              </Stack>
            </Box>
          )}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Закрыть</Button>
      </DialogActions>
    </Dialog>
  );
}
