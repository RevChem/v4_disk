import { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TablePagination,
  TableRow,
  TableSortLabel,
  TextField,
  ToggleButton,
  ToggleButtonGroup,
  Typography,
} from "@mui/material";

import { downloadNamolotExcel, fetchNamolot } from "../api";
import { filterRows, rankRows, sortRecordRows } from "../utils/namolotFilter";

const FIRM_ORDER = [
  "АФ Чистопольская",
  "АгроРаздолье",
  "Аксу-Агро",
  "Актай",
  "Дрожжаное Рассвет Агро",
  "Лениногорская Птицефабрика",
  "НурлатАгро",
  "Прочие",
];

function shiftIso(iso, days) {
  const base = new Date(`${iso}T12:00:00`);
  if (Number.isNaN(base.getTime())) return iso;
  base.setDate(base.getDate() + days);
  const month = String(base.getMonth() + 1).padStart(2, "0");
  const day = String(base.getDate()).padStart(2, "0");
  return `${base.getFullYear()}-${month}-${day}`;
}

function fmtTonnes(value) {
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

export default function NamolotDialog({ open, onClose, summaryDate }) {
  const [dateBegin, setDateBegin] = useState("");
  const [dateEnd, setDateEnd] = useState("");
  const [timeFrom, setTimeFrom] = useState("");
  const [timeTo, setTimeTo] = useState("");
  const [loadedBegin, setLoadedBegin] = useState("");
  const [loadedEnd, setLoadedEnd] = useState("");
  const [rows, setRows] = useState([]);
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState("");
  const [firms, setFirms] = useState([]);
  const [crops, setCrops] = useState([]);
  const [plate, setPlate] = useState("");
  const [surname, setSurname] = useState("");
  const [mode, setMode] = useState("rows");
  const [sortKey, setSortKey] = useState("tonnes");
  const [sortDir, setSortDir] = useState("desc");
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(50);

  async function load(begin, end) {
    if (!begin || !end) return;
    setLoading(true);
    setError("");
    try {
      const data = await fetchNamolot({ dateBegin: begin, dateEnd: end });
      setRows(Array.isArray(data?.rows) ? data.rows : []);
      setLoadedBegin(begin);
      setLoadedEnd(end);
      setLoaded(true);
    } catch (err) {
      setRows([]);
      setLoaded(false);
      setError(String(err.message || err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (!open) return undefined;
    const end = summaryDate || new Date().toISOString().slice(0, 10);
    const begin = shiftIso(end, -34);
    setDateBegin(begin);
    setDateEnd(end);
    setTimeFrom("");
    setTimeTo("");
    setFirms([]);
    setCrops([]);
    setPlate("");
    setSurname("");
    setMode("rows");
    setSortKey("tonnes");
    setSortDir("desc");
    setPage(0);
    setRows([]);
    setLoaded(false);
    void load(begin, end);
    return undefined;
  }, [open, summaryDate]);

  const cropOptions = useMemo(() => {
    const names = new Set(rows.map((row) => row.crop).filter(Boolean));
    return [...names].sort((a, b) => a.localeCompare(b, "ru"));
  }, [rows]);

  const firmOptions = useMemo(() => {
    const present = new Set(rows.map((row) => row.firm).filter(Boolean));
    const ordered = FIRM_ORDER.filter((name) => present.has(name));
    for (const name of present) {
      if (!ordered.includes(name)) ordered.push(name);
    }
    return ordered;
  }, [rows]);

  const visible = useMemo(() => {
    const filtered = filterRows(rows, {
      dateFrom: dateBegin,
      dateTo: dateEnd,
      timeFrom,
      timeTo,
      firms,
      crops,
      plate,
      surname,
    });
    if (mode === "rows") return sortRecordRows(filtered, sortKey, sortDir);
    return rankRows(filtered, mode, sortDir);
  }, [rows, dateBegin, dateEnd, timeFrom, timeTo, firms, crops, plate, surname, mode, sortKey, sortDir]);

  const shownTonnes = useMemo(
    () => visible.reduce((sum, row) => sum + (Number(row.tonnes) || 0), 0),
    [visible],
  );

  const pageRows = visible.slice(page * pageSize, page * pageSize + pageSize);
  const widerThanLoaded = Boolean(
    loaded && loadedBegin && loadedEnd && (dateBegin < loadedBegin || dateEnd > loadedEnd),
  );

  function toggleSort(key) {
    setPage(0);
    if (sortKey === key) {
      setSortDir((dir) => (dir === "desc" ? "asc" : "desc"));
      return;
    }
    setSortKey(key);
    setSortDir("desc");
  }

  function resetFilters() {
    setTimeFrom("");
    setTimeTo("");
    setFirms([]);
    setCrops([]);
    setPlate("");
    setSurname("");
    setPage(0);
  }

  async function download() {
    setDownloading(true);
    setError("");
    try {
      await downloadNamolotExcel({ dateBegin, dateEnd });
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setDownloading(false);
    }
  }

  const busy = loading || downloading;

  return (
    <Dialog open={open} onClose={busy ? undefined : onClose} fullWidth maxWidth="lg">
      <DialogTitle>Namolot</DialogTitle>
      <DialogContent>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Записи 1С с даты и времени по дату и время. Пустое время — с начала или до конца дня.
          Пустые культура и агрофирма означают «все». Госномер ищется без пробелов, фамилия — по ФИО.
        </Typography>
        <Stack direction={{ xs: "column", sm: "row" }} spacing={1} sx={{ mb: 1.5 }} flexWrap="wrap" useFlexGap>
          <TextField
            label="Дата с"
            type="date"
            size="small"
            value={dateBegin}
            onChange={(event) => {
              setDateBegin(event.target.value);
              setPage(0);
            }}
            slotProps={{ inputLabel: { shrink: true } }}
            disabled={loading}
          />
          <TextField
            label="Дата по"
            type="date"
            size="small"
            value={dateEnd}
            onChange={(event) => {
              setDateEnd(event.target.value);
              setPage(0);
            }}
            slotProps={{ inputLabel: { shrink: true } }}
            disabled={loading}
          />
          <TextField
            label="Время с"
            type="time"
            size="small"
            value={timeFrom}
            onChange={(event) => {
              setTimeFrom(event.target.value);
              setPage(0);
            }}
            slotProps={{ inputLabel: { shrink: true } }}
            disabled={loading}
          />
          <TextField
            label="Время по"
            type="time"
            size="small"
            value={timeTo}
            onChange={(event) => {
              setTimeTo(event.target.value);
              setPage(0);
            }}
            slotProps={{ inputLabel: { shrink: true } }}
            disabled={loading}
          />
          <Button variant="contained" onClick={() => void load(dateBegin, dateEnd)} disabled={loading || !dateBegin || !dateEnd}>
            Обновить
          </Button>
        </Stack>
        {widerThanLoaded ? (
          <Alert severity="info" sx={{ mb: 1.5 }}>
            Даты шире загруженного периода {loadedBegin} — {loadedEnd}. Нажмите «Обновить», чтобы запросить 1С.
          </Alert>
        ) : null}
        <FilterChips
          label="Культура"
          options={cropOptions}
          selected={crops}
          onChange={(next) => {
            setCrops(next);
            setPage(0);
          }}
        />
        <FilterChips
          label="Агрофирма"
          options={firmOptions}
          selected={firms}
          onChange={(next) => {
            setFirms(next);
            setPage(0);
          }}
        />
        <Stack direction={{ xs: "column", sm: "row" }} spacing={1} sx={{ mb: 1.5 }} alignItems={{ sm: "center" }}>
          <TextField
            label="Госномер"
            size="small"
            value={plate}
            placeholder="2824"
            onChange={(event) => {
              setPlate(event.target.value);
              setPage(0);
            }}
            disabled={loading && !loaded}
          />
          <TextField
            label="Фамилия"
            size="small"
            value={surname}
            placeholder="Пискеев"
            onChange={(event) => {
              setSurname(event.target.value);
              setPage(0);
            }}
            disabled={loading && !loaded}
          />
          <Button onClick={resetFilters} disabled={loading && !loaded}>
            Сбросить фильтр
          </Button>
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
        </Stack>
        {error ? (
          <Alert severity="error" sx={{ mb: 2 }}>
            {error}
          </Alert>
        ) : null}
        {loading ? (
          <Stack direction="row" spacing={1.5} alignItems="center" sx={{ py: 2 }}>
            <CircularProgress size={22} />
            <Typography variant="body2" color="text.secondary">
              Запрос к Namolot…
            </Typography>
          </Stack>
        ) : null}
        {!loading && loaded ? (
          <>
            <Typography variant="body2" sx={{ mb: 1 }}>
              {mode === "rows" ? "Записей" : "Строк"} {visible.length.toLocaleString("ru-RU")}
              {" · "}
              {fmtTonnes(shownTonnes)} т
              {visible.length !== rows.length ? ` из ${rows.length.toLocaleString("ru-RU")}` : ""}
              . Сортировка по намолоту {sortDir === "desc" ? "убывает" : "растёт"}.
            </Typography>
            {visible.length ? (
              <Box sx={{ maxHeight: 460, overflow: "auto" }}>
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
                Ничего не найдено. Сбросьте фильтр или расширьте период.
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
      </DialogContent>
      <DialogActions>
        <Button onClick={() => void download()} disabled={busy || !dateBegin || !dateEnd}>
          {downloading ? "Excel…" : "Скачать Excel"}
        </Button>
        <Button onClick={onClose} disabled={busy}>
          Закрыть
        </Button>
      </DialogActions>
    </Dialog>
  );
}

function FilterChips({ label, options, selected, onChange }) {
  if (!options.length) return null;
  return (
    <Stack direction="row" spacing={0.5} useFlexGap flexWrap="wrap" alignItems="center" sx={{ mb: 1 }}>
      <Typography variant="caption" color="text.secondary" sx={{ minWidth: 72 }}>
        {label}
      </Typography>
      {options.map((name) => {
        const on = selected.includes(name);
        return (
          <Chip
            key={name}
            size="small"
            label={name}
            color={on ? "primary" : "default"}
            variant={on ? "filled" : "outlined"}
            onClick={() => onChange(on ? selected.filter((item) => item !== name) : [...selected, name])}
          />
        );
      })}
      {!selected.length ? (
        <Typography variant="caption" color="text.secondary">
          все
        </Typography>
      ) : null}
    </Stack>
  );
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
      <TableCell>Госномер</TableCell>
      <TableCell>Механизатор</TableCell>
      <TableCell>Поле</TableCell>
      <TableCell align="right" sortDirection={sortKey === "tonnes" ? direction : false}>
        <TableSortLabel
          active={sortKey === "tonnes"}
          direction={sortKey === "tonnes" ? direction : "desc"}
          onClick={() => onSort("tonnes")}
        >
          Намолот, т
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
          Намолот, т
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
      <TableCell sx={{ whiteSpace: "nowrap" }}>{row.plate || "—"}</TableCell>
      <TableCell>{row.operator || "—"}</TableCell>
      <TableCell>{row.field || "—"}</TableCell>
      <TableCell align="right">{fmtTonnes(row.tonnes)}</TableCell>
    </TableRow>
  );
}

function GroupLine({ row, mode, rank }) {
  return (
    <TableRow>
      <TableCell>{rank}</TableCell>
      <TableCell sx={{ fontWeight: 600 }}>{row.key}</TableCell>
      <TableCell align="right">{fmtTonnes(row.tonnes)}</TableCell>
      <TableCell align="right">{row.records.toLocaleString("ru-RU")}</TableCell>
      <TableCell>{joinList(mode === "plates" ? row.names : row.plates)}</TableCell>
      <TableCell>{joinList(row.crops)}</TableCell>
      <TableCell>{joinList(row.firms)}</TableCell>
    </TableRow>
  );
}
