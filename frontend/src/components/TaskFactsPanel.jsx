import { useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Chip,
  CircularProgress,
  FormControlLabel,
  IconButton,
  Link,
  MenuItem,
  Paper,
  Stack,
  Tab,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TablePagination,
  TableRow,
  TableSortLabel,
  Tabs,
  TextField,
  ToggleButton,
  ToggleButtonGroup,
  Tooltip,
  Typography,
} from "@mui/material";
import { alpha } from "@mui/material/styles";
import AutoAwesomeOutlinedIcon from "@mui/icons-material/AutoAwesomeOutlined";
import CheckRoundedIcon from "@mui/icons-material/CheckRounded";
import ContentCopyOutlinedIcon from "@mui/icons-material/ContentCopyOutlined";
import DownloadOutlinedIcon from "@mui/icons-material/DownloadOutlined";
import SendRoundedIcon from "@mui/icons-material/SendRounded";

import { askTaskQuestions, fetchPlansEditorWorkTypes, fetchProverkaAiSettings, fetchTaskQuestions } from "../api";
import { filterRows, normText, rankRows, sortRecordRows } from "../utils/namolotFilter";
import { NAMOLOT_CROPS, NAMOLOT_FIRMS } from "../utils/namolotOptions";

const EXAMPLES = [
  "Сколько гектаров по каждой операции?",
  "Сколько га внесения минеральных удобрений по агрофирмам?",
  "Какая культура лидирует по площади в этом срезе?",
  "Сколько гектаров у выбранной фамилии?",
];

function withArea(row) {
  const ha = Number(row?.ha) || 0;
  return { ...row, ha, tonnes: ha };
}

function fmtHa(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return n.toLocaleString("ru-RU", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
}

function fmtAt(at) {
  if (!at) return "—";
  const [day, clock = ""] = String(at).split("T");
  const [year, month, date] = day.split("-");
  if (!year || !month || !date) return at;
  return `${date}.${month}.${year} ${clock.slice(0, 5)}`.trim();
}

function joinList(values) {
  return values?.length ? values.join(", ") : "—";
}

function workTypeLabel(item) {
  const title = String(item?.name || "").trim();
  if (!title) return "";
  return `${item.id}: ${title}`;
}

function narrowOperation(list, operation) {
  const query = normText(operation);
  if (!query) return list;
  return list.filter((row) => normText(row.operation) === query);
}

function sliceLabel(applied) {
  if (!applied) return "весь сезон";
  const parts = [];
  if (applied.operation) parts.push(applied.operation);
  if (applied.firm) parts.push(applied.firm);
  if (applied.crop) parts.push(applied.crop);
  if (applied.dateFrom || applied.dateTo) {
    const from = [applied.dateFrom, applied.timeFrom].filter(Boolean).join(" ");
    const to = [applied.dateTo, applied.timeTo].filter(Boolean).join(" ");
    parts.push(`${from || "…"} — ${to || "…"}`);
  }
  if (applied.plate) parts.push(`госномер ${applied.plate}`);
  if (applied.surname) parts.push(applied.surname);
  return parts.length ? parts.join(", ") : "весь сезон";
}

function emptySlice() {
  return {
    firm: "",
    crop: "",
    operation: "",
    dateFrom: "",
    timeFrom: "06:00",
    dateTo: "",
    timeTo: "05:59",
    plate: "",
    surname: "",
  };
}

export default function TaskFactsPanel({ active = true, summaryDate }) {
  const reportDate = summaryDate || new Date().toISOString().slice(0, 10);
  const [rows, setRows] = useState([]);
  const [windows, setWindows] = useState(null);
  const [applied, setApplied] = useState(null);
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState("");
  const [namesNote, setNamesNote] = useState("");
  const [workTypes, setWorkTypes] = useState([]);

  const [firm, setFirm] = useState("");
  const [crop, setCrop] = useState("");
  const [operation, setOperation] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [timeFrom, setTimeFrom] = useState("06:00");
  const [dateTo, setDateTo] = useState("");
  const [timeTo, setTimeTo] = useState("05:59");
  const [plate, setPlate] = useState("");
  const [surname, setSurname] = useState("");
  const [mode, setMode] = useState("rows");
  const [sortKey, setSortKey] = useState("tonnes");
  const [sortDir, setSortDir] = useState("desc");
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(50);

  const [question, setQuestion] = useState("");
  const [useLlm, setUseLlm] = useState(true);
  const [view, setView] = useState("answers");
  const [aiKeySet, setAiKeySet] = useState(true);
  const [answerHistory, setAnswerHistory] = useState([]);
  const [askLoading, setAskLoading] = useState(false);
  const [askError, setAskError] = useState("");
  const [copiedId, setCopiedId] = useState(null);
  const feedEnd = useRef(null);

  useEffect(() => {
    setRows([]);
    setWindows(null);
    setApplied(null);
    setLoaded(false);
    setError("");
    setNamesNote("");
    setAnswerHistory([]);
    setAskError("");
  }, [reportDate]);

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

  useEffect(() => {
    feedEnd.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [answerHistory.length, askLoading]);

  useEffect(() => {
    if (!active) return undefined;
    let cancelled = false;
    fetchPlansEditorWorkTypes()
      .then((data) => {
        if (!cancelled) setWorkTypes(Array.isArray(data?.items) ? data.items : []);
      })
      .catch(() => {
        if (!cancelled) setWorkTypes([]);
      });
    return () => {
      cancelled = true;
    };
  }, [active]);

  const operationOptions = useMemo(() => {
    const fromCatalog = workTypes.map(workTypeLabel).filter(Boolean);
    const known = new Set(fromCatalog);
    const extra = [];
    const seen = new Set();
    for (const row of rows) {
      const name = row.operation;
      if (!name || name === "—" || known.has(name) || seen.has(name)) continue;
      seen.add(name);
      extra.push(name);
    }
    extra.sort((a, b) => a.localeCompare(b, "ru"));
    return [...fromCatalog, ...extra];
  }, [rows, workTypes]);

  const visible = useMemo(() => {
    if (!applied) return [];
    const filtered = narrowOperation(
      filterRows(rows, {
        firms: applied.firm ? [applied.firm] : [],
        crops: applied.crop ? [applied.crop] : [],
        dateFrom: applied.dateFrom,
        timeFrom: applied.timeFrom,
        dateTo: applied.dateTo,
        timeTo: applied.timeTo,
        plate: applied.plate,
        surname: applied.surname,
      }),
      applied.operation,
    );
    if (mode === "rows") return sortRecordRows(filtered, sortKey, sortDir);
    return rankRows(filtered, mode, sortDir);
  }, [rows, applied, mode, sortKey, sortDir]);

  const headed = useMemo(() => {
    if (!applied) return [];
    return narrowOperation(
      filterRows(rows, {
        firms: applied.firm ? [applied.firm] : [],
        crops: applied.crop ? [applied.crop] : [],
      }),
      applied.operation,
    );
  }, [rows, applied]);

  const dayRows = useMemo(() => {
    const from = windows?.day?.from;
    const until = windows?.day?.until;
    if (!from || !until) return [];
    return headed.filter((row) => row.at && row.at >= from && row.at < until);
  }, [headed, windows]);

  const seasonHa = useMemo(
    () => headed.reduce((sum, row) => sum + (Number(row.ha) || 0), 0),
    [headed],
  );
  const shownHa = useMemo(
    () => visible.reduce((sum, row) => sum + (Number(row.ha ?? row.tonnes) || 0), 0),
    [visible],
  );
  const pageRows = visible.slice(page * pageSize, page * pageSize + pageSize);

  function draft() {
    return { firm, crop, operation, dateFrom, timeFrom, dateTo, timeTo, plate, surname };
  }

  async function form({ showTable = true } = {}) {
    const next = draft();
    setApplied(next);
    setPage(0);
    if (showTable) setView("table");
    if (loaded || loading) return next;
    setLoading(true);
    setError("");
    try {
      const data = await fetchTaskQuestions({ reportDate });
      if (!data?.available) {
        throw new Error(data?.detail || "Задания не прочитались.");
      }
      setWindows(data);
      setNamesNote(String(data?.names_note || ""));
      setRows((Array.isArray(data?.rows) ? data.rows : []).map(withArea));
      setLoaded(true);
      return next;
    } catch (err) {
      setRows([]);
      setWindows(null);
      setLoaded(false);
      const message = String(err.message || err);
      setError(message);
      throw new Error(message);
    } finally {
      setLoading(false);
    }
  }

  function resetFilters() {
    setFirm("");
    setCrop("");
    setOperation("");
    setDateFrom("");
    setTimeFrom("06:00");
    setDateTo("");
    setTimeTo("05:59");
    setPlate("");
    setSurname("");
    setPage(0);
    if (loaded) setApplied(emptySlice());
  }

  function toggleSort(key) {
    setPage(0);
    if (sortKey === key) {
      setSortDir((dir) => (dir === "desc" ? "asc" : "desc"));
      return;
    }
    setSortKey(key);
    setSortDir("desc");
  }

  async function download() {
    setDownloading(true);
    setError("");
    try {
      downloadTaskCsv(mode, visible);
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setDownloading(false);
    }
  }

  async function handleAsk(text) {
    const q = (text ?? question).trim();
    if (askLoading || loading || !q) return;
    setAskLoading(true);
    setAskError("");
    setView("answers");
    try {
      const slice = await form({ showTable: false });
      const data = await askTaskQuestions({ reportDate, ...slice, question: q, useLlm });
      setAnswerHistory((prev) => [
        ...prev,
        { id: `${Date.now()}`, question: q, ts: Date.now(), slice: sliceLabel(slice), ...data },
      ]);
      setQuestion("");
    } catch (err) {
      setAskError(String(err.message || err));
    } finally {
      setAskLoading(false);
    }
  }

  const busy = loading || downloading;
  const canAsk = !askLoading && !loading && Boolean(question.trim());

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
      <Paper variant="outlined" sx={{ display: "flex", flexDirection: "column", minHeight: 0, overflow: "hidden" }}>
        <Stack spacing={1.5} sx={{ p: 2, flex: 1, minHeight: 0, overflow: "auto" }}>
          <Typography variant="body2" color="text.secondary">
            Дата сводки {reportDate}. «Сформировать» без фильтров берёт все операции с 1 января до 06:00:
            внесение удобрений, посев, опрыскивание, уборку и остальные. Агрофирма, культура, агрозадание,
            даты, госномер и фамилия необязательны. Агрозадание — номер и название из справочника work_types, как в редакторе планов.
          </Typography>
          <TextField
            select
            label="Агрофирма"
            size="small"
            fullWidth
            value={firm}
            onChange={(event) => setFirm(event.target.value)}
            disabled={loading}
          >
            <MenuItem value="">Не выбрана</MenuItem>
            {NAMOLOT_FIRMS.map((name) => (
              <MenuItem key={name} value={name}>
                {name}
              </MenuItem>
            ))}
          </TextField>
          <TextField
            select
            label="Культура"
            size="small"
            fullWidth
            value={crop}
            onChange={(event) => setCrop(event.target.value)}
            disabled={loading}
          >
            <MenuItem value="">Не выбрана</MenuItem>
            {NAMOLOT_CROPS.map((name) => (
              <MenuItem key={name} value={name}>
                {name}
              </MenuItem>
            ))}
            </TextField>
          <TextField
            select
            label="Агрозадание"
            size="small"
            fullWidth
            value={operation}
            onChange={(event) => setOperation(event.target.value)}
            disabled={loading}
          >
            <MenuItem value="">Не выбрано</MenuItem>
            {operationOptions.map((name) => (
              <MenuItem key={name} value={name}>
                {name}
              </MenuItem>
            ))}
          </TextField>
            <Button variant="contained" onClick={() => void form().catch(() => {})} disabled={loading}>
            {loading ? "Запрос…" : "Сформировать"}
          </Button>
          <Stack direction="row" spacing={1}>
            <TextField
              label="Дата с"
              type="date"
              size="small"
              fullWidth
              value={dateFrom}
              onChange={(event) => setDateFrom(event.target.value)}
              slotProps={{ inputLabel: { shrink: true } }}
              disabled={loading && !loaded}
            />
            <TextField
              label="Время с"
              type="time"
              size="small"
              fullWidth
              value={timeFrom}
              onChange={(event) => setTimeFrom(event.target.value)}
              slotProps={{ inputLabel: { shrink: true } }}
              disabled={loading && !loaded}
            />
          </Stack>
          <Stack direction="row" spacing={1}>
            <TextField
              label="Дата по"
              type="date"
              size="small"
              fullWidth
              value={dateTo}
              onChange={(event) => setDateTo(event.target.value)}
              slotProps={{ inputLabel: { shrink: true } }}
              disabled={loading && !loaded}
            />
            <TextField
              label="Время по"
              type="time"
              size="small"
              fullWidth
              value={timeTo}
              onChange={(event) => setTimeTo(event.target.value)}
              slotProps={{ inputLabel: { shrink: true } }}
              disabled={loading && !loaded}
            />
          </Stack>
          <TextField
            label="Госномер"
            size="small"
            fullWidth
            value={plate}
            placeholder="2824"
            onChange={(event) => setPlate(event.target.value)}
            disabled={loading && !loaded}
          />
          <TextField
            label="Фамилия"
            size="small"
            fullWidth
            value={surname}
            placeholder="Пискеев"
            onChange={(event) => setSurname(event.target.value)}
            disabled={loading && !loaded}
          />
          <Button onClick={resetFilters} disabled={loading && !loaded}>
            Сбросить фильтр
          </Button>
          {error ? <Alert severity="error">{error}</Alert> : null}
        </Stack>
      </Paper>

      <Paper variant="outlined" sx={{ display: "flex", flexDirection: "column", minHeight: { xs: 560, md: 0 }, overflow: "hidden" }}>
        <Stack direction="row" alignItems="center" sx={{ px: 1.5, borderBottom: 1, borderColor: "divider" }} spacing={1}>
          <Tabs value={view} onChange={(_, next) => setView(next)} sx={{ minHeight: 44 }}>
            <Tab value="answers" label={`Ответы${answerHistory.length ? ` (${answerHistory.length})` : ""}`} sx={{ minHeight: 44 }} />
            <Tab value="table" label={`Таблица${applied && loaded ? ` (${visible.length.toLocaleString("ru-RU")})` : ""}`} sx={{ minHeight: 44 }} />
          </Tabs>
          <Box sx={{ flex: 1 }} />
          {view === "table" && (
            <ToggleButtonGroup
              size="small"
              exclusive
              value={mode}
              onChange={(_event, next) => {
                if (!next) return;
                setMode(next);
                setSortKey("tonnes");
                setSortDir("desc");
                setPage(0);
              }}
            >
              <ToggleButton value="rows">Записи</ToggleButton>
              <ToggleButton value="plates">Госномера</ToggleButton>
              <ToggleButton value="surnames">Фамилии</ToggleButton>
            </ToggleButtonGroup>
          )}
          {view === "table" && loaded && (
            <Button size="small" startIcon={<DownloadOutlinedIcon fontSize="small" />} onClick={() => void download()} disabled={busy}>
              {downloading ? "CSV…" : "CSV"}
            </Button>
          )}
          {view === "answers" && answerHistory.length > 0 && (
            <Button size="small" color="inherit" onClick={() => setAnswerHistory([])}>
              Очистить
            </Button>
          )}
        </Stack>

        {view === "answers" && (
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
                  <Typography variant="h6" fontWeight={700}>
                    Спросите по сформированному срезу
                  </Typography>
                  <Typography variant="body2" color="text.secondary" sx={{ maxWidth: 560, mt: 0.5 }}>
                    Можно сразу нажать «Сформировать» без опций. Либо сначала выберите агрофирму, культуру, даты, госномер или фамилию — и сформируйте этот срез.
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
                      <Typography variant="body2" sx={{ whiteSpace: "pre-wrap" }}>
                        {entry.question}
                      </Typography>
                      {entry.slice ? (
                        <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 0.5 }}>
                          {entry.slice}
                        </Typography>
                      ) : null}
                    </Box>
                  </Box>
                  <Paper variant="outlined" sx={{ p: 2, borderRadius: 2, borderTopLeftRadius: 4 }}>
                    <Stack direction="row" justifyContent="space-between" alignItems="flex-start" spacing={1}>
                      <Typography variant="body1" sx={{ whiteSpace: "pre-wrap", lineHeight: 1.65, flex: 1 }}>
                        {entry.answer || "Пустой ответ"}
                      </Typography>
                      <Tooltip title={copiedId === entry.id ? "Скопировано" : "Скопировать ответ"}>
                        <IconButton size="small" onClick={() => void handleCopy(entry, setCopiedId)}>
                          {copiedId === entry.id ? (
                            <CheckRoundedIcon fontSize="small" color="success" />
                          ) : (
                            <ContentCopyOutlinedIcon fontSize="small" />
                          )}
                        </IconButton>
                      </Tooltip>
                    </Stack>
                    <Stack direction="row" spacing={0.75} flexWrap="wrap" useFlexGap sx={{ mt: 1.5 }}>
                      <Chip size="small" color={entry.llm_used ? "primary" : "default"} label={entry.llm_used ? `модель: ${entry.model}` : "без модели"} />
                      <Chip size="small" variant="outlined" label={`записей ${entry.rows_sent ?? "—"}/${entry.rows_total ?? "—"}`} />
                      <Chip size="small" variant="outlined" label={`${fmtHa(entry.ha_total)} га`} />
                      {entry.usage?.input_tokens != null ? (
                        <Chip
                          size="small"
                          variant="outlined"
                          label={`${entry.usage.input_tokens}/${entry.usage.output_tokens ?? "?"} токенов${entry.usage.cache_hit_tokens ? `, кэш ${entry.usage.cache_hit_tokens}` : ""}`}
                        />
                      ) : null}
                      {entry.error ? <Chip size="small" color="warning" label={entry.error} /> : null}
                    </Stack>
                  </Paper>
                </Box>
              ))}
              {askLoading && (
                <Paper variant="outlined" sx={{ p: 2, borderRadius: 2 }}>
                  <Stack direction="row" spacing={1.5} alignItems="center">
                    <CircularProgress size={18} />
                    <Typography variant="body2" color="text.secondary">
                      Читаю срез и отвечаю…
                    </Typography>
                  </Stack>
                </Paper>
              )}
              {askError ? <Alert severity="warning">{askError}</Alert> : null}
              <div ref={feedEnd} />
            </Stack>
          </Box>
        )}

        {view === "table" && (
          <Box sx={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column", p: { xs: 1.5, md: 2 }, gap: 1.5, overflow: "hidden" }}>
            {loading ? (
              <Stack direction="row" spacing={1.5} alignItems="center" sx={{ py: 2 }}>
                <CircularProgress size={22} />
                <Typography variant="body2" color="text.secondary">
                  Читаю задания…
                </Typography>
              </Stack>
            ) : null}
            {!loading && applied && loaded ? (
              <>
                <Table size="small">
                  <TableHead>
                    <TableRow>
                      <TableCell>Показатель</TableCell>
                      <TableCell>Период</TableCell>
                      <TableCell align="right">Площадь, га</TableCell>
                      <TableCell align="right">Записей</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    <TableRow>
                      <TableCell>За сутки</TableCell>
                      <TableCell>{windows ? `${fmtAt(windows.day?.from)} — ${fmtAt(windows.day?.to)}` : "—"}</TableCell>
                      <TableCell align="right">{fmtHa(dayRows.reduce((sum, row) => sum + (Number(row.ha) || 0), 0))}</TableCell>
                      <TableCell align="right">{dayRows.length}</TableCell>
                    </TableRow>
                    <TableRow>
                      <TableCell>За сезон</TableCell>
                      <TableCell>{windows ? `${fmtAt(windows.season?.from)} — ${fmtAt(windows.season?.to)}` : "—"}</TableCell>
                      <TableCell align="right">{fmtHa(seasonHa)}</TableCell>
                      <TableCell align="right">{headed.length}</TableCell>
                    </TableRow>
                  </TableBody>
                </Table>
                {namesNote ? <Alert severity="warning">{namesNote}</Alert> : null}
                <Typography variant="body2">
                  {mode === "rows" ? "Записей" : "Строк"} {visible.length.toLocaleString("ru-RU")}
                  {" · "}
                  {fmtHa(shownHa)} га
                  {visible.length !== rows.length ? ` из ${rows.length.toLocaleString("ru-RU")}` : ""}. Срез: {sliceLabel(applied)}.
                </Typography>
                {visible.length ? (
                  <Box sx={{ flex: 1, minHeight: 0, overflow: "auto" }}>
                    <Table size="small" stickyHeader>
                      <TableHead>
                        <TableRow>
                          <TableCell width={48}>№</TableCell>
                          {mode === "rows" ? (
                            <RecordHead sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                          ) : (
                            <GroupHead mode={mode} sortDir={sortDir} onSort={() => toggleSort("tonnes")} />
                          )}
                        </TableRow>
                      </TableHead>
                      <TableBody>
                        {pageRows.map((row, index) =>
                          mode === "rows" ? (
                            <RecordLine key={`${row.at}-${row.plate}-${row.operator}-${index}`} row={row} rank={page * pageSize + index + 1} />
                          ) : (
                            <GroupLine key={row.key} row={row} mode={mode} rank={page * pageSize + index + 1} />
                          ),
                        )}
                      </TableBody>
                    </Table>
                  </Box>
                ) : (
                  <Typography variant="body2" color="text.secondary">
                    {applied.firm || applied.crop || applied.operation || applied.dateFrom || applied.dateTo || applied.plate || applied.surname
                      ? "Ничего не найдено. Сбросьте фильтр или расширьте условия и снова нажмите «Сформировать»."
                      : "За этот период заданий с площадью нет."}
                  </Typography>
                )}
                <TablePagination
                  component="div"
                  count={visible.length}
                  page={page}
                  onPageChange={(_event, next) => setPage(next)}
                  rowsPerPage={pageSize}
                  onRowsPerPageChange={(event) => {
                    setPageSize(Number(event.target.value));
                    setPage(0);
                  }}
                  rowsPerPageOptions={[50, 100, 200]}
                  labelRowsPerPage="На странице"
                  labelDisplayedRows={({ from, to, count }) => `${from}–${to} из ${count}`}
                />
              </>
            ) : null}
            {!loading && applied && !loaded ? (
              <Typography variant="body2" color="text.secondary" sx={{ py: 6, textAlign: "center" }}>
                Задания не загрузились. Нажмите «Сформировать» ещё раз.
              </Typography>
            ) : null}
            {!loading && !applied ? (
              <Typography variant="body2" color="text.secondary" sx={{ py: 6, textAlign: "center" }}>
                Таблица пустая, пока не нажато «Сформировать».
              </Typography>
            ) : null}
          </Box>
        )}

        {view === "answers" && (
        <Box sx={{ p: 1.5, borderTop: 1, borderColor: "divider", bgcolor: "background.paper" }}>
          <TextField
            placeholder="Например: сколько га внесения минеральных удобрений у Актая?"
            multiline
            minRows={2}
            maxRows={6}
            fullWidth
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void handleAsk();
              }
            }}
          />
          <Stack direction="row" alignItems="center" spacing={1.5} sx={{ mt: 1 }}>
            <FormControlLabel
              sx={{ m: 0 }}
              control={<Checkbox size="small" checked={useLlm} onChange={(event) => setUseLlm(event.target.checked)} />}
              label={<Typography variant="body2">Использовать модель (иначе таблица)</Typography>}
            />
            <Box sx={{ flex: 1 }} />
            <Typography variant="caption" color="text.secondary" sx={{ display: { xs: "none", sm: "block" } }}>
              Enter — отправить, Shift+Enter — новая строка
            </Typography>
            <Button variant="contained" onClick={() => void handleAsk()} disabled={!canAsk} endIcon={<SendRoundedIcon />}>
              {askLoading ? "Спрашиваю…" : "Спросить"}
            </Button>
          </Stack>
        </Box>
        )}
      </Paper>
    </Box>
  );
}

async function handleCopy(entry, setCopiedId) {
  try {
    await navigator.clipboard.writeText(entry.answer || "");
    setCopiedId(entry.id);
    setTimeout(() => setCopiedId((current) => (current === entry.id ? null : current)), 1500);
  } catch {
    // буфер обмена недоступен
  }
}

function RecordHead({ sortKey, sortDir, onSort }) {
  const direction = sortDir === "asc" ? "asc" : "desc";
  return (
    <>
      <TableCell sortDirection={sortKey === "at" ? direction : false}>
        <TableSortLabel active={sortKey === "at"} direction={sortKey === "at" ? direction : "desc"} onClick={() => onSort("at")}>
          Дата
        </TableSortLabel>
      </TableCell>
      <TableCell>Агрофирма</TableCell>
      <TableCell>Культура</TableCell>
      <TableCell>Операция</TableCell>
      <TableCell>Госномер</TableCell>
      <TableCell>Механизатор</TableCell>
      <TableCell>Поле</TableCell>
      <TableCell>Задание</TableCell>
      <TableCell align="right" sortDirection={sortKey === "tonnes" ? direction : false}>
        <TableSortLabel active={sortKey === "tonnes"} direction={sortKey === "tonnes" ? direction : "desc"} onClick={() => onSort("tonnes")}>
          Площадь, га
        </TableSortLabel>
      </TableCell>
    </>
  );
}

function GroupHead({ mode, sortDir, onSort }) {
  const direction = sortDir === "asc" ? "asc" : "desc";
  return (
    <>
      <TableCell>{mode === "plates" ? "Госномер" : "Фамилия"}</TableCell>
      <TableCell align="right" sortDirection={direction}>
        <TableSortLabel active direction={direction} onClick={onSort}>
          Площадь, га
        </TableSortLabel>
      </TableCell>
      <TableCell align="right">Записей</TableCell>
      <TableCell>{mode === "plates" ? "Фамилии" : "Госномера"}</TableCell>
      <TableCell>Культуры</TableCell>
      <TableCell>Агрофирмы</TableCell>
    </>
  );
}

function RecordLine({ row, rank }) {
  return (
    <TableRow>
      <TableCell>{rank}</TableCell>
      <TableCell sx={{ whiteSpace: "nowrap" }}>{fmtAt(row.at)}</TableCell>
      <TableCell>{row.firm}</TableCell>
      <TableCell>{row.crop}</TableCell>
      <TableCell>{row.operation || "—"}</TableCell>
      <TableCell sx={{ whiteSpace: "nowrap" }}>{row.plate || "—"}</TableCell>
      <TableCell>{row.operator || "—"}</TableCell>
      <TableCell>{row.field || "—"}</TableCell>
      <TableCell sx={{ whiteSpace: "nowrap" }}>
        {row.task_url ? (
          <Link href={row.task_url} target="_blank" rel="noopener noreferrer">
            {row.task_id}
          </Link>
        ) : (
          row.task_id || "—"
        )}
      </TableCell>
      <TableCell align="right">{fmtHa(row.ha)}</TableCell>
    </TableRow>
  );
}

function csvCell(value) {
  const text = String(value ?? "");
  if (/[;"\n]/.test(text)) return `"${text.replace(/"/g, '""')}"`;
  return text;
}

function downloadTaskCsv(mode, rows) {
  const lines = [];
  if (mode === "rows") {
    lines.push(["Дата", "Агрофирма", "Культура", "Операция", "Госномер", "Механизатор", "Поле", "Задание", "Ссылка", "га"].join(";"));
    for (const row of rows) {
      lines.push(
        [fmtAt(row.at), row.firm, row.crop, row.operation, row.plate, row.operator, row.field, row.task_id, row.task_url, row.ha]
          .map(csvCell)
          .join(";"),
      );
    }
  } else {
    lines.push(["Ключ", "га", "Записей", mode === "plates" ? "Фамилии" : "Госномера", "Культуры", "Агрофирмы"].join(";"));
    for (const row of rows) {
      lines.push(
        [
          row.key,
          row.tonnes,
          row.records,
          joinList(mode === "plates" ? row.names : row.plates),
          joinList(row.crops),
          joinList(row.firms),
        ]
          .map(csvCell)
          .join(";"),
      );
    }
  }
  const blob = new Blob(["\uFEFF" + lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "zadaniya.csv";
  link.click();
  URL.revokeObjectURL(url);
}

function GroupLine({ row, mode, rank }) {
  return (
    <TableRow>
      <TableCell>{rank}</TableCell>
      <TableCell sx={{ fontWeight: 600 }}>{row.key}</TableCell>
      <TableCell align="right">{fmtHa(row.tonnes)}</TableCell>
      <TableCell align="right">{row.records.toLocaleString("ru-RU")}</TableCell>
      <TableCell>{joinList(mode === "plates" ? row.names : row.plates)}</TableCell>
      <TableCell>{joinList(row.crops)}</TableCell>
      <TableCell>{joinList(row.firms)}</TableCell>
    </TableRow>
  );
}
