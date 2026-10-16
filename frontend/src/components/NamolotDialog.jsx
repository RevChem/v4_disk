import { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Box,
  Button,
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
  MenuItem,
  ToggleButton,
  ToggleButtonGroup,
  Typography,
} from "@mui/material";

import { downloadNamolotExcel, fetchNamolotBrief } from "../api";
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

const CROPS = [
  "Рапс озимый",
  "Рапс яровой",
  "Пшеница озимая",
  "Пшеница яровая",
  "Тритикале озимая",
  "Соя",
  "Подсолнечник",
  "Кукуруза на зерно",
  "Кукуруза на силос",
];

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
  const reportDate = summaryDate || new Date().toISOString().slice(0, 10);
  const [rows, setRows] = useState([]);
  const [brief, setBrief] = useState(null);
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState("");
  const [firm, setFirm] = useState("");
  const [crop, setCrop] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [timeFrom, setTimeFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [timeTo, setTimeTo] = useState("");
  const [plate, setPlate] = useState("");
  const [surname, setSurname] = useState("");
  const [mode, setMode] = useState("rows");
  const [sortKey, setSortKey] = useState("tonnes");
  const [sortDir, setSortDir] = useState("desc");
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(50);
  const ready = Boolean(firm && crop);

  useEffect(() => {
    if (open) return undefined;
    setFirm("");
    setCrop("");
    setDateFrom("");
    setTimeFrom("");
    setDateTo("");
    setTimeTo("");
    setPlate("");
    setSurname("");
    setMode("rows");
    setSortKey("tonnes");
    setSortDir("desc");
    setPage(0);
    setRows([]);
    setBrief(null);
    setLoaded(false);
    setError("");
    return undefined;
  }, [open]);

  useEffect(() => {
    if (!open || !ready) {
      setRows([]);
      setBrief(null);
      setLoaded(false);
      return undefined;
    }
    let cancelled = false;
    setLoading(true);
    setError("");
    setPage(0);
    setBrief(null);
    setRows([]);
    setLoaded(false);
    fetchNamolotBrief({ reportDate, firm, crop })
      .then((data) => {
        if (cancelled) return;
        setBrief(data);
        setRows(Array.isArray(data?.rows) ? data.rows : []);
        setLoaded(true);
      })
      .catch((err) => {
        if (cancelled) return;
        setRows([]);
        setBrief(null);
        setLoaded(false);
        setError(String(err.message || err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, ready, reportDate, firm, crop]);

  const visible = useMemo(() => {
    const filtered = filterRows(rows, {
      dateFrom,
      timeFrom,
      dateTo,
      timeTo,
      plate,
      surname,
    });
    if (mode === "rows") return sortRecordRows(filtered, sortKey, sortDir);
    return rankRows(filtered, mode, sortDir);
  }, [rows, dateFrom, timeFrom, dateTo, timeTo, plate, surname, mode, sortKey, sortDir]);

  const shownTonnes = useMemo(
    () => visible.reduce((sum, row) => sum + (Number(row.tonnes) || 0), 0),
    [visible],
  );

  const pageRows = visible.slice(page * pageSize, page * pageSize + pageSize);

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
    setDateFrom("");
    setTimeFrom("");
    setDateTo("");
    setTimeTo("");
    setPlate("");
    setSurname("");
    setPage(0);
  }

  async function download() {
    setDownloading(true);
    setError("");
    try {
      await downloadNamolotExcel({ reportDate, firm, crop });
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
          Дата сводки {reportDate}. За сутки — смена с 06:00 предыдущего дня по 05:59 этой даты.
          За сезон — с 06:00 начала уборки по то же 05:59. Запрос в 1С уходит только после выбора
          агрофирмы и культуры. Дата, время, госномер и фамилия сужают уже загруженные записи сезона.
          Пустое время — с начала или до конца указанного дня.
        </Typography>
        <Stack direction={{ xs: "column", sm: "row" }} spacing={1} sx={{ mb: 1.5 }} flexWrap="wrap" useFlexGap>
          <TextField
            select
            label="Агрофирма"
            size="small"
            value={firm}
            onChange={(event) => setFirm(event.target.value)}
            disabled={loading}
            sx={{ minWidth: 240 }}
          >
            <MenuItem value="">Не выбрана</MenuItem>
            {FIRM_ORDER.map((name) => (
              <MenuItem key={name} value={name}>
                {name}
              </MenuItem>
            ))}
          </TextField>
          <TextField
            select
            label="Культура"
            size="small"
            value={crop}
            onChange={(event) => setCrop(event.target.value)}
            disabled={loading}
            sx={{ minWidth: 220 }}
          >
            <MenuItem value="">Не выбрана</MenuItem>
            {CROPS.map((name) => (
              <MenuItem key={name} value={name}>
                {name}
              </MenuItem>
            ))}
          </TextField>
        </Stack>
        <Table size="small" sx={{ mb: 2 }}>
          <TableHead>
            <TableRow>
              <TableCell>Показатель</TableCell>
              <TableCell>Период</TableCell>
              <TableCell align="right">Намолот, т</TableCell>
              <TableCell align="right">Записей</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            <TableRow>
              <TableCell>За сутки</TableCell>
              <TableCell>{ready && brief ? `${fmtAt(brief.day?.from)} — ${fmtAt(brief.day?.to)}` : "—"}</TableCell>
              <TableCell align="right">{ready && brief ? fmtTonnes(brief.day?.tonnes) : "—"}</TableCell>
              <TableCell align="right">{ready && brief ? brief.day?.records ?? 0 : "—"}</TableCell>
            </TableRow>
            <TableRow>
              <TableCell>За сезон</TableCell>
              <TableCell>{ready && brief ? `${fmtAt(brief.season?.from)} — ${fmtAt(brief.season?.to)}` : "—"}</TableCell>
              <TableCell align="right">{ready && brief ? fmtTonnes(brief.season?.tonnes) : "—"}</TableCell>
              <TableCell align="right">{ready && brief ? brief.season?.records ?? 0 : "—"}</TableCell>
            </TableRow>
          </TableBody>
        </Table>
        <Stack direction={{ xs: "column", sm: "row" }} spacing={1} sx={{ mb: 1.5 }} flexWrap="wrap" useFlexGap>
          <TextField
            label="Дата с"
            type="date"
            size="small"
            value={dateFrom}
            onChange={(event) => {
              setDateFrom(event.target.value);
              setPage(0);
            }}
            slotProps={{ inputLabel: { shrink: true } }}
            disabled={loading && !loaded}
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
            disabled={loading && !loaded}
          />
          <TextField
            label="Дата по"
            type="date"
            size="small"
            value={dateTo}
            onChange={(event) => {
              setDateTo(event.target.value);
              setPage(0);
            }}
            slotProps={{ inputLabel: { shrink: true } }}
            disabled={loading && !loaded}
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
            disabled={loading && !loaded}
          />
        </Stack>
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
        {!loading && ready && loaded ? (
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
                {dateFrom || timeFrom || dateTo || timeTo || plate || surname
                  ? "Ничего не найдено в этом диапазоне. Сбросьте фильтр или расширьте даты."
                  : "По этой агрофирме и культуре записей нет."}
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
        {!loading && !ready ? (
          <Typography variant="body2" color="text.secondary">
            Таблица пустая, пока не выбраны агрофирма и культура.
          </Typography>
        ) : null}
      </DialogContent>
      <DialogActions>
        <Button onClick={() => void download()} disabled={busy || !ready}>
          {downloading ? "Excel…" : "Скачать Excel"}
        </Button>
        <Button onClick={onClose} disabled={busy}>
          Закрыть
        </Button>
      </DialogActions>
    </Dialog>
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
