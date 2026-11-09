import { useEffect, useMemo, useRef, useState } from "react";
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

import {
  askTaskQuestions,
  fetchPlansEditorWorkTypes,
  fetchProverkaAiSettings,
  fetchTaskQuestionGroups,
  fetchTaskQuestions,
} from "../api";
import { ColumnFilter, EMPTY_COLUMNS, applyColumnFilters, distinctColumnValues } from "./ColumnFilter";
import { FieldSliceTable, WorkerSliceTable } from "./TaskSliceTables";
import { filterRows, normText, rankRows, sortRecordRows } from "../utils/namolotFilter";
import { NAMOLOT_CROPS, NAMOLOT_FIRMS } from "../utils/namolotOptions";
import { readView, useViewSnapshot } from "../utils/viewMemory";

const EXAMPLES = [
  "Сколько гектаров по каждой операции?",
  "Сколько га внесения минеральных удобрений по агрофирмам?",
  "Какая культура лидирует по площади в этом срезе?",
  "Сколько гектаров у выбранной фамилии?",
];

function withArea(row) {
  const ha = Number(row?.ha) || 0;
  const trackRaw = row?.track_ha;
  const track = trackRaw == null || trackRaw === "" ? null : Number(trackRaw);
  return { ...row, ha, tonnes: ha, track_ha: Number.isFinite(track) ? track : null };
}

function selectedFirms(applied) {
  if (!applied) return [];
  if (Array.isArray(applied.firms) && applied.firms.length) return applied.firms;
  return applied.firm ? [applied.firm] : [];
}

function sliceIsNarrow(applied) {
  if (!applied) return false;
  return Boolean(
    selectedFirms(applied).length ||
      applied.crop ||
      applied.operations?.length ||
      applied.details?.length ||
      applied.dateFrom ||
      applied.dateTo ||
      applied.plate ||
      applied.surname ||
      applied.crew === "hired" ||
      applied.crew === "own",
  );
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

const POPPER_PROPS = { popper: { sx: { zIndex: 1600 } } };

function asGroups(raw) {
  return (Array.isArray(raw) ? raw : [])
    .map((item) => ({
      name: String(item?.name || "").trim(),
      ids: (Array.isArray(item?.ids) ? item.ids : []).map(Number).filter((id) => Number.isFinite(id)),
    }))
    .filter((item) => item.name);
}

function sameDetail(a, b) {
  if (a?.id != null && b?.id != null) return a.id === b.id;
  return a?.label === b?.label;
}

function rowMatchesGroups(row, selected) {
  if (!selected?.length) return true;
  const id = row?.work_type_id;
  const operation = normText(row?.operation);
  return selected.some((group) => {
    if (typeof id === "number" && group.ids.includes(id)) return true;
    return normText(group.name) === operation;
  });
}

function rowMatchesDetails(row, selected) {
  if (!selected?.length) return true;
  const id = row?.work_type_id;
  const operation = normText(row?.operation);
  return selected.some((item) => {
    if (item.id != null && id === item.id) return true;
    return normText(item.label) === operation;
  });
}

function narrowTasks(list, applied) {
  if (!applied?.operations?.length && !applied?.details?.length) return list;
  return list.filter((row) => rowMatchesGroups(row, applied.operations) && rowMatchesDetails(row, applied.details));
}

function sliceLabel(applied) {
  if (!applied) return "весь сезон";
  const parts = [];
  const firms = selectedFirms(applied);
  if (firms.length) parts.push(firms.join(", "));
  if (applied.crew === "hired") parts.push("наемники");
  if (applied.crew === "own") parts.push("собственные");
  if (applied.operations?.length) parts.push(applied.operations.map((item) => item.name).join(", "));
  if (applied.details?.length) parts.push(applied.details.map((item) => item.label).join(", "));
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

function pad2(value) {
  return String(value).padStart(2, "0");
}

/** Смена отчётной даты: вчера 06:00 — сегодня 05:59. */
function shiftOf(iso) {
  const day = String(iso || "").slice(0, 10);
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day);
  const base = match
    ? new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]))
    : new Date();
  const prev = new Date(base);
  prev.setDate(prev.getDate() - 1);
  const stamp = (date) => `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
  return {
    dateFrom: stamp(prev),
    timeFrom: "06:00",
    dateTo: stamp(base),
    timeTo: "05:59",
  };
}

function emptySlice(iso) {
  return {
    firms: [],
    firm: "",
    crew: "all",
    crop: "",
    operations: [],
    details: [],
    plate: "",
    surname: "",
    ...shiftOf(iso),
  };
}

export default function TaskFactsPanel({ active = true, summaryDate }) {
  const reportDate = summaryDate || new Date().toISOString().slice(0, 10);
  const bootRef = useRef(undefined);
  if (bootRef.current === undefined) {
    bootRef.current = readView(`facts:${reportDate}`) || {};
  }
  const boot = bootRef.current;
  const shift0 = shiftOf(reportDate);
  const [rows, setRows] = useState(() => (Array.isArray(boot.rows) ? boot.rows : []));
  const [windows, setWindows] = useState(boot.windows || null);
  const [applied, setApplied] = useState(boot.applied ?? null);
  const [loaded, setLoaded] = useState(Boolean(boot.loaded));
  const [loading, setLoading] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState(boot.error || "");
  const [namesNote, setNamesNote] = useState(boot.namesNote || "");
  const [workTypes, setWorkTypes] = useState([]);
  const [groups, setGroups] = useState([]);

  const [firms, setFirms] = useState(() => (Array.isArray(boot.firms) ? boot.firms : boot.firm ? [boot.firm] : []));
  const [crew, setCrew] = useState(boot.crew === "hired" || boot.crew === "own" ? boot.crew : "all");
  const [crop, setCrop] = useState(boot.crop || "");
  const [operations, setOperations] = useState(() => (Array.isArray(boot.operations) ? boot.operations : []));
  const [details, setDetails] = useState(() => (Array.isArray(boot.details) ? boot.details : []));
  const [dateFrom, setDateFrom] = useState(boot.dateFrom ?? shift0.dateFrom);
  const [timeFrom, setTimeFrom] = useState(boot.timeFrom ?? shift0.timeFrom);
  const [dateTo, setDateTo] = useState(boot.dateTo ?? shift0.dateTo);
  const [timeTo, setTimeTo] = useState(boot.timeTo ?? shift0.timeTo);
  const [columns, setColumns] = useState(boot.columns || EMPTY_COLUMNS);
  const [plate, setPlate] = useState(boot.plate || "");
  const [surname, setSurname] = useState(boot.surname || "");
  const [mode, setMode] = useState(boot.mode || "rows");
  const [sortKey, setSortKey] = useState(boot.sortKey || "tonnes");
  const [sortDir, setSortDir] = useState(boot.sortDir || "desc");
  const [page, setPage] = useState(Number.isFinite(boot.page) ? boot.page : 0);
  const [pageSize, setPageSize] = useState(boot.pageSize || 50);

  const [question, setQuestion] = useState(boot.question || "");
  const [useLlm, setUseLlm] = useState(boot.useLlm !== false);
  const [view, setView] = useState(boot.view || "answers");
  const [aiKeySet, setAiKeySet] = useState(true);
  const [answerHistory, setAnswerHistory] = useState(() =>
    Array.isArray(boot.answerHistory) ? boot.answerHistory : [],
  );
  const [askLoading, setAskLoading] = useState(false);
  const [askError, setAskError] = useState("");
  const [copiedId, setCopiedId] = useState(null);
  const feedEnd = useRef(null);

  function restore(saved) {
    const shift = shiftOf(reportDate);
    const data = saved || {};
    setRows(Array.isArray(data.rows) ? data.rows : []);
    setWindows(data.windows || null);
    setApplied(data.applied ?? null);
    setLoaded(Boolean(data.loaded));
    setError(data.error || "");
    setNamesNote(data.namesNote || "");
    setFirms(Array.isArray(data.firms) ? data.firms : data.firm ? [data.firm] : []);
    setCrew(data.crew === "hired" || data.crew === "own" ? data.crew : "all");
    setCrop(data.crop || "");
    setOperations(Array.isArray(data.operations) ? data.operations : []);
    setDetails(Array.isArray(data.details) ? data.details : []);
    setDateFrom(data.dateFrom ?? shift.dateFrom);
    setTimeFrom(data.timeFrom ?? shift.timeFrom);
    setDateTo(data.dateTo ?? shift.dateTo);
    setTimeTo(data.timeTo ?? shift.timeTo);
    setColumns(data.columns || EMPTY_COLUMNS);
    setPlate(data.plate || "");
    setSurname(data.surname || "");
    setMode(data.mode || "rows");
    setSortKey(data.sortKey || "tonnes");
    setSortDir(data.sortDir || "desc");
    setPage(Number.isFinite(data.page) ? data.page : 0);
    setPageSize(data.pageSize || 50);
    setQuestion(data.question || "");
    setUseLlm(data.useLlm !== false);
    setView(data.view || "answers");
    setAnswerHistory(Array.isArray(data.answerHistory) ? data.answerHistory : []);
    setAskError("");
  }

  const memoryKey = `facts:${reportDate}`;
  const snapshot = useMemo(
    () => ({
      rows,
      windows,
      applied,
      loaded,
      error,
      namesNote,
      firms,
      crew,
      crop,
      operations,
      details,
      dateFrom,
      timeFrom,
      dateTo,
      timeTo,
      columns,
      plate,
      surname,
      mode,
      sortKey,
      sortDir,
      page,
      pageSize,
      question,
      useLlm,
      view,
      answerHistory,
    }),
    [
      rows,
      windows,
      applied,
      loaded,
      error,
      namesNote,
      firms,
      crew,
      crop,
      operations,
      details,
      dateFrom,
      timeFrom,
      dateTo,
      timeTo,
      columns,
      plate,
      surname,
      mode,
      sortKey,
      sortDir,
      page,
      pageSize,
      question,
      useLlm,
      view,
      answerHistory,
    ],
  );
  useViewSnapshot(memoryKey, snapshot, restore);

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
    fetchTaskQuestionGroups()
      .then((data) => {
        if (!cancelled) setGroups(asGroups(data?.groups));
      })
      .catch(() => {
        if (!cancelled) setGroups([]);
      });
    return () => {
      cancelled = true;
    };
  }, [active]);

  const detailOptions = useMemo(() => {
    const byId = new Map();
    for (const item of workTypes) {
      const id = Number(item?.id);
      const title = String(item?.name || "").trim();
      if (!Number.isFinite(id) || !title || title.startsWith("work_type_")) continue;
      byId.set(id, { id, label: `${id}: ${title}` });
    }
    const extras = [];
    const seenLabels = new Set([...byId.values()].map((item) => normText(item.label)));
    for (const row of rows) {
      const id = Number(row.work_type_id);
      const name = String(row.operation || "").trim();
      if (!name || name === "—") continue;
      if (Number.isFinite(id)) {
        if (!byId.has(id)) byId.set(id, { id, label: name });
        continue;
      }
      if (seenLabels.has(normText(name))) continue;
      seenLabels.add(normText(name));
      extras.push({ id: null, label: name });
    }
    const catalog = [...byId.values()].sort((a, b) => a.label.localeCompare(b.label, "ru"));
    extras.sort((a, b) => a.label.localeCompare(b.label, "ru"));
    return [...catalog, ...extras];
  }, [rows, workTypes]);

  const ranged = useMemo(() => {
    if (!applied) return [];
    return narrowTasks(
      filterRows(rows, {
        firms: selectedFirms(applied),
        crops: applied.crop ? [applied.crop] : [],
        dateFrom: applied.dateFrom,
        timeFrom: applied.timeFrom,
        dateTo: applied.dateTo,
        timeTo: applied.timeTo,
        plate: applied.plate,
        surname: applied.surname,
        crew: applied.crew,
      }),
      applied,
    );
  }, [rows, applied]);

  const firmPick = selectedFirms(applied);
  const distinctFirms = useMemo(() => new Set(ranged.map((row) => row.firm).filter(Boolean)).size, [ranged]);
  const showFirm = firmPick.length > 1 || (firmPick.length === 0 && distinctFirms > 1);
  const sliceNote = applied ? `Срез: ${sliceLabel(applied)}` : "";

  const columnOptions = useMemo(() => distinctColumnValues(ranged), [ranged]);

  const visible = useMemo(() => {
    const picked = applyColumnFilters(ranged, columns);
    if (mode === "rows") return sortRecordRows(picked, sortKey, sortDir);
    return rankRows(picked, mode, sortDir);
  }, [ranged, columns, mode, sortKey, sortDir]);

  const headed = useMemo(() => {
    if (!applied) return [];
    return narrowTasks(
      filterRows(rows, {
        firms: selectedFirms(applied),
        crops: applied.crop ? [applied.crop] : [],
        crew: applied.crew,
      }),
      applied,
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
    return {
      firms: [...firms],
      firm: firms.length === 1 ? firms[0] : "",
      crew,
      crop,
      operations: operations.map((item) => ({ name: item.name, ids: [...item.ids] })),
      details: details.map((item) => ({ id: item.id, label: item.label })),
      dateFrom,
      timeFrom,
      dateTo,
      timeTo,
      plate,
      surname,
    };
  }

  async function form({ showTable = true, force = false } = {}) {
    const next = draft();
    setColumns(EMPTY_COLUMNS);
    setApplied(next);
    setPage(0);
    if (showTable) setView((current) => (current === "slice" || current === "fields" ? current : "table"));
    // Сохранённый срез только на экране. «Сформировать» снова читает задания.
    if (loading) return next;
    if (!force && loaded) return next;
    const hadRows = loaded;
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
      if (!hadRows) {
        setRows([]);
        setWindows(null);
        setLoaded(false);
        setNamesNote("");
      }
      const message = String(err.message || err);
      setError(message);
      throw new Error(message);
    } finally {
      setLoading(false);
    }
  }

  function pickColumn(key, value) {
    setPage(0);
    setColumns((current) => ({ ...current, [key]: value }));
  }

  function resetFilters() {
    const shift = shiftOf(reportDate);
    setFirms([]);
    setCrew("all");
    setCrop("");
    setOperations([]);
    setDetails([]);
    setDateFrom(shift.dateFrom);
    setTimeFrom(shift.timeFrom);
    setDateTo(shift.dateTo);
    setTimeTo(shift.timeTo);
    setPlate("");
    setSurname("");
    setColumns(EMPTY_COLUMNS);
    setPage(0);
    if (loaded) setApplied(emptySlice(reportDate));
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
      const data = await askTaskQuestions({
        reportDate,
        firm: slice.firms?.length === 1 ? slice.firms[0] : slice.firm || "",
        firms: slice.firms?.length ? slice.firms : slice.firm ? [slice.firm] : [],
        crew: slice.crew === "hired" || slice.crew === "own" ? slice.crew : "",
        crop: slice.crop,
        dateFrom: slice.dateFrom,
        timeFrom: slice.timeFrom,
        dateTo: slice.dateTo,
        timeTo: slice.timeTo,
        plate: slice.plate,
        surname: slice.surname,
        operations: slice.operations.map((item) => item.name),
        workTypeIds: slice.details.filter((item) => item.id != null).map((item) => item.id),
        operationLabels: slice.details.filter((item) => item.id == null).map((item) => item.label),
        question: q,
        useLlm,
      });
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
            Дата сводки {reportDate}. По умолчанию стоит смена: вчера 06:00 — сегодня 05:59.
            Можно выбрать несколько агрофирм. Культура, госномер и фамилия необязательны. Агрооперации — те же группы, что в планах,
            можно выбрать несколько. Агрозадания ниже — отдельные виды работ, они необязательны: достаточно одного из списков.
          </Typography>
          <Autocomplete
            multiple
            disableCloseOnSelect
            size="small"
            options={NAMOLOT_FIRMS}
            value={firms}
            onChange={(_event, value) => setFirms(value)}
            limitTags={2}
            disabled={loading}
            slotProps={POPPER_PROPS}
            renderOption={(props, option, { selected }) => {
              const { key, ...rest } = props;
              return (
                <li key={key} {...rest}>
                  <Checkbox size="small" checked={selected} sx={{ mr: 1, p: 0.5, pointerEvents: "none" }} />
                  {option}
                </li>
              );
            }}
            renderTags={(value, getTagProps) =>
              value.map((option, index) => {
                const { key, ...tagProps } = getTagProps({ index });
                return <Chip key={key} size="small" label={option} {...tagProps} />;
              })
            }
            renderInput={(params) => (
              <TextField {...params} label="Агрофирма" placeholder={firms.length ? "" : "Не выбраны"} />
            )}
          />
          <TextField
            select
            label="Учет наемников"
            size="small"
            fullWidth
            value={crew}
            onChange={(event) => setCrew(event.target.value)}
            disabled={loading}
            helperText="Наемники — машины группы 7 в Cropwise. По умолчанию учитываются и наемники, и собственные."
          >
            <MenuItem value="all">Наемники + Собственные</MenuItem>
            <MenuItem value="hired">Наемники</MenuItem>
            <MenuItem value="own">Собственные</MenuItem>
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
          <Autocomplete
            multiple
            disableCloseOnSelect
            size="small"
            options={groups}
            value={operations}
            onChange={(_event, value) => setOperations(value)}
            getOptionLabel={(option) => option.name}
            isOptionEqualToValue={(a, b) => a.name === b.name}
            limitTags={2}
            disabled={loading}
            slotProps={POPPER_PROPS}
            renderOption={(props, option, { selected }) => {
              const { key, ...rest } = props;
              return (
                <li key={key} {...rest}>
                  <Checkbox size="small" checked={selected} sx={{ mr: 1, p: 0.5, pointerEvents: "none" }} />
                  {option.name}
                </li>
              );
            }}
            renderTags={(value, getTagProps) =>
              value.map((option, index) => {
                const { key, ...tagProps } = getTagProps({ index });
                return <Chip key={key} size="small" label={option.name} {...tagProps} />;
              })
            }
            renderInput={(params) => (
              <TextField {...params} label="Агрооперации" placeholder={operations.length ? "" : "Не выбраны"} />
            )}
          />
          <Autocomplete
            multiple
            disableCloseOnSelect
            size="small"
            options={detailOptions}
            value={details}
            onChange={(_event, value) => setDetails(value)}
            getOptionLabel={(option) => option.label}
            isOptionEqualToValue={sameDetail}
            limitTags={2}
            disabled={loading}
            slotProps={POPPER_PROPS}
            renderOption={(props, option, { selected }) => {
              const { key, ...rest } = props;
              return (
                <li key={key} {...rest}>
                  <Checkbox size="small" checked={selected} sx={{ mr: 1, p: 0.5, pointerEvents: "none" }} />
                  {option.label}
                </li>
              );
            }}
            renderTags={(value, getTagProps) =>
              value.map((option, index) => {
                const { key, ...tagProps } = getTagProps({ index });
                return <Chip key={key} size="small" label={option.label} {...tagProps} />;
              })
            }
            renderInput={(params) => (
              <TextField
                {...params}
                label="Агрозадания"
                placeholder={details.length ? "" : "Необязательно"}
                helperText="Отдельные виды работ. Если выбрать и группы, и задания, останутся только совпадения."
              />
            )}
          />
            <Button variant="contained" onClick={() => void form({ force: true }).catch(() => {})} disabled={loading}>
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
          <Tabs value={view} onChange={(_, next) => setView(next)} variant="scrollable" scrollButtons="auto" sx={{ minHeight: 44 }}>
            <Tab value="answers" label={`Ответы${answerHistory.length ? ` (${answerHistory.length})` : ""}`} sx={{ minHeight: 44 }} />
            <Tab value="table" label={`Таблица${applied && loaded ? ` (${visible.length.toLocaleString("ru-RU")})` : ""}`} sx={{ minHeight: 44 }} />
            <Tab value="slice" label="Срез" sx={{ minHeight: 44 }} />
            <Tab value="fields" label="Срезы, поля" sx={{ minHeight: 44 }} />
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
                Ключ DeepSeek не задан: «Спросить» вернёт таблицу без ответа модели. Ключ задаётся в меню «Задания», раздел «DeepSeek».
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
            {applied && loaded ? (
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
                            <RecordHead
                              sortKey={sortKey}
                              sortDir={sortDir}
                              onSort={toggleSort}
                              columns={columns}
                              options={columnOptions}
                              onColumn={pickColumn}
                            />
                          ) : (
                            <GroupHead
                              mode={mode}
                              sortDir={sortDir}
                              onSort={() => toggleSort("tonnes")}
                              columns={columns}
                              options={columnOptions}
                              onColumn={pickColumn}
                            />
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
                    {sliceIsNarrow(applied)
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

        {(view === "slice" || view === "fields") && (
          <Box sx={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column", p: { xs: 1.5, md: 2 }, gap: 1.5, overflow: "hidden" }}>
            {loading ? (
              <Stack direction="row" spacing={1.5} alignItems="center" sx={{ py: 2 }}>
                <CircularProgress size={22} />
                <Typography variant="body2" color="text.secondary">
                  Читаю задания…
                </Typography>
              </Stack>
            ) : null}
            {applied && loaded && ranged.length ? (
              view === "slice" ? (
                <WorkerSliceTable rows={ranged} showFirm={showFirm} note={sliceNote} />
              ) : (
                <FieldSliceTable rows={ranged} showFirm={showFirm} note={sliceNote} />
              )
            ) : null}
            {applied && loaded && !loading && !ranged.length ? (
              <Typography variant="body2" color="text.secondary">
                {sliceIsNarrow(applied)
                  ? "Ничего не найдено. Сбросьте фильтр или расширьте условия и снова нажмите «Сформировать»."
                  : "За этот период заданий с площадью нет."}
              </Typography>
            ) : null}
            {!loading && applied && !loaded ? (
              <Typography variant="body2" color="text.secondary" sx={{ py: 6, textAlign: "center" }}>
                Задания не загрузились. Нажмите «Сформировать» ещё раз.
              </Typography>
            ) : null}
            {!loading && !applied ? (
              <Typography variant="body2" color="text.secondary" sx={{ py: 6, textAlign: "center" }}>
                Здесь появятся фамилии, машины и поля после «Сформировать».
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

function RecordHead({ sortKey, sortDir, onSort, columns, options, onColumn }) {
  const direction = sortDir === "asc" ? "asc" : "desc";
  return (
    <>
      <TableCell sortDirection={sortKey === "at" ? direction : false}>
        <TableSortLabel active={sortKey === "at"} direction={sortKey === "at" ? direction : "desc"} onClick={() => onSort("at")}>
          Дата
        </TableSortLabel>
      </TableCell>
      <TableCell>Агрофирма</TableCell>
      <TableCell>
        <ColumnFilter label="Культура" values={options.crop} selected={columns.crop} onSelect={(value) => onColumn("crop", value)} />
      </TableCell>
      <TableCell>Операция</TableCell>
      <TableCell>
        <ColumnFilter label="Госномер" values={options.plate} selected={columns.plate} onSelect={(value) => onColumn("plate", value)} />
      </TableCell>
      <TableCell>
        <ColumnFilter label="Механизатор" values={options.operator} selected={columns.operator} onSelect={(value) => onColumn("operator", value)} />
      </TableCell>
      <TableCell>
        <ColumnFilter label="Поле" values={options.field} selected={columns.field} onSelect={(value) => onColumn("field", value)} />
      </TableCell>
      <TableCell>Задание</TableCell>
      <TableCell align="right" sortDirection={sortKey === "tonnes" ? direction : false}>
        <TableSortLabel active={sortKey === "tonnes"} direction={sortKey === "tonnes" ? direction : "desc"} onClick={() => onSort("tonnes")}>
          Площадь, га
        </TableSortLabel>
      </TableCell>
    </>
  );
}

function GroupHead({ mode, sortDir, onSort, columns, options, onColumn }) {
  const direction = sortDir === "asc" ? "asc" : "desc";
  return (
    <>
      <TableCell>
        {mode === "plates" ? (
          <ColumnFilter label="Госномер" values={options.plate} selected={columns.plate} onSelect={(value) => onColumn("plate", value)} />
        ) : (
          "Фамилия"
        )}
      </TableCell>
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
