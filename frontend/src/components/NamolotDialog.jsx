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

import { downloadNamolotExcel, fetchNamolotSeason } from "../api";
import { filterRows, rankRows, sortRecordRows } from "../utils/namolotFilter";
import { NAMOLOT_CROPS as CROPS, NAMOLOT_FIRMS as FIRM_ORDER } from "../utils/namolotOptions";

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

export default function NamolotDialog({ open, onClose, summaryDate, onOpenMappings = null }) {
  const reportDate = summaryDate || new Date().toISOString().slice(0, 10);
  const [rows, setRows] = useState([]);
  const [windows, setWindows] = useState(null);
  const [applied, setApplied] = useState(null);
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState("");
  const [firm, setFirm] = useState("");
  const [crop, setCrop] = useState("");
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

  useEffect(() => {
    if (open) return undefined;
    setFirm("");
    setCrop("");
    setDateFrom("");
    setTimeFrom("06:00");
    setDateTo("");
    setTimeTo("05:59");
    setPlate("");
    setSurname("");
    setMode("rows");
    setSortKey("tonnes");
    setSortDir("desc");
    setPage(0);
    setRows([]);
    setWindows(null);
    setApplied(null);
    setLoaded(false);
    setError("");
    return undefined;
  }, [open]);

  useEffect(() => {
    setRows([]);
    setWindows(null);
    setApplied(null);
    setLoaded(false);
    setError("");
  }, [reportDate]);

  const visible = useMemo(() => {
    if (!applied) return [];
    const filtered = filterRows(rows, {
      firms: applied.firm ? [applied.firm] : [],
      crops: applied.crop ? [applied.crop] : [],
      dateFrom: applied.dateFrom,
      timeFrom: applied.timeFrom,
      dateTo: applied.dateTo,
      timeTo: applied.timeTo,
      plate: applied.plate,
      surname: applied.surname,
    });
    if (mode === "rows") return sortRecordRows(filtered, sortKey, sortDir);
    return rankRows(filtered, mode, sortDir);
  }, [rows, applied, mode, sortKey, sortDir]);

  const headed = useMemo(() => {
    if (!applied) return [];
    return filterRows(rows, {
      firms: applied.firm ? [applied.firm] : [],
      crops: applied.crop ? [applied.crop] : [],
    });
  }, [rows, applied]);

  const dayRows = useMemo(() => {
    const from = windows?.day?.from;
    const until = windows?.day?.until;
    if (!from || !until) return [];
    return headed.filter((row) => row.at && row.at >= from && row.at < until);
  }, [headed, windows]);

  const seasonTonnes = useMemo(
    () => headed.reduce((sum, row) => sum + (Number(row.tonnes) || 0), 0),
    [headed],
  );

  async function form() {
    const next = { firm, crop, dateFrom, timeFrom, dateTo, timeTo, plate, surname };
    setApplied(next);
    setPage(0);
    if (loaded || loading) return;
    setLoading(true);
    setError("");
    try {
      const data = await fetchNamolotSeason({ reportDate });
      setWindows(data);
      setRows(Array.isArray(data?.rows) ? data.rows : []);
      setLoaded(true);
    } catch (err) {
      setRows([]);
      setWindows(null);
      setLoaded(false);
      setError(String(err.message || err));
    } finally {
      setLoading(false);
    }
  }

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
    setFirm("");
    setCrop("");
    setDateFrom("");
    setTimeFrom("06:00");
    setDateTo("");
    setTimeTo("05:59");
    setPlate("");
    setSurname("");
    setPage(0);
    if (loaded) {
      setApplied({
        firm: "",
        crop: "",
        dateFrom: "",
        timeFrom: "06:00",
        dateTo: "",
        timeTo: "05:59",
        plate: "",
        surname: "",
      });
    }
  }

  async function download() {
    setDownloading(true);
    setError("");
    try {
      await downloadNamolotExcel({ mode, rows: visible });
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
          Дата сводки {reportDate}. Нажмите «Сформировать»: без фильтров придут все записи сезона.
          Агрофирма, культура, даты, госномер и фамилия необязательны и применяются по кнопке.
          «Тритикале озимый» и опечатки вроде «Трикитакле» считаются как «Тритикале озимая», кукуруза — как зерно.
          Сутки и сезон вверху считаются по агрофирме и культуре. Дата, госномер и фамилия сужают таблицу.
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
          <Button variant="contained" onClick={() => void form()} disabled={loading}>
            {loading ? "Запрос…" : "Сформировать"}
          </Button>
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
              <TableCell>{applied && windows ? `${fmtAt(windows.day?.from)} — ${fmtAt(windows.day?.to)}` : "—"}</TableCell>
              <TableCell align="right">{applied && windows ? fmtTonnes(dayRows.reduce((sum, row) => sum + (Number(row.tonnes) || 0), 0)) : "—"}</TableCell>
              <TableCell align="right">{applied && windows ? dayRows.length : "—"}</TableCell>
            </TableRow>
            <TableRow>
              <TableCell>За сезон</TableCell>
              <TableCell>{applied && windows ? `${fmtAt(windows.season?.from)} — ${fmtAt(windows.season?.to)}` : "—"}</TableCell>
              <TableCell align="right">{applied && windows ? fmtTonnes(seasonTonnes) : "—"}</TableCell>
              <TableCell align="right">{applied && windows ? headed.length : "—"}</TableCell>
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
        {!loading && applied && loaded ? (
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
                {applied?.firm || applied?.crop || applied?.dateFrom || applied?.dateTo || applied?.plate || applied?.surname
                  ? "Ничего не найдено. Сбросьте фильтр или расширьте условия и снова нажмите «Сформировать»."
                  : "За сезон записей нет."}
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
        {!loading && !applied ? (
          <Typography variant="body2" color="text.secondary">
            Таблица пустая, пока не нажато «Сформировать».
          </Typography>
        ) : null}
      </DialogContent>
      <DialogActions>
        {onOpenMappings ? (
          <Button onClick={onOpenMappings} disabled={busy} sx={{ mr: "auto" }}>
            Сопоставления
          </Button>
        ) : null}
        <Button onClick={() => void download()} disabled={busy || !loaded}>
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
