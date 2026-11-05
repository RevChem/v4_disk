import { useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  IconButton,
  MenuItem,
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
  Tooltip,
  Typography,
} from "@mui/material";
import CloseFullscreenRoundedIcon from "@mui/icons-material/CloseFullscreenRounded";
import OpenInFullRoundedIcon from "@mui/icons-material/OpenInFullRounded";

import { downloadNamolotExcel, fetchNamolotSeason } from "../api";
import { ColumnFilter, EMPTY_COLUMNS, applyColumnFilters, distinctColumnValues } from "./ColumnFilter";
import { filterRows, rankRows, sortRecordRows } from "../utils/namolotFilter";
import { NAMOLOT_CROPS as CROPS, NAMOLOT_FIRMS as FIRM_ORDER } from "../utils/namolotOptions";
import { readView, useViewSnapshot } from "../utils/viewMemory";

function isComputerFullscreen() {
  return document.fullscreenElement === document.documentElement;
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

export default function NamolotDialog({ open, onClose, summaryDate, onOpenMappings = null }) {
  const reportDate = summaryDate || new Date().toISOString().slice(0, 10);
  const bootRef = useRef(undefined);
  if (bootRef.current === undefined) {
    bootRef.current = readView(`namolot:${reportDate}`) || {};
  }
  const boot = bootRef.current;
  const [rows, setRows] = useState(() => (Array.isArray(boot.rows) ? boot.rows : []));
  const [windows, setWindows] = useState(boot.windows || null);
  const [applied, setApplied] = useState(boot.applied ?? null);
  const [loaded, setLoaded] = useState(Boolean(boot.loaded));
  const [loading, setLoading] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState(boot.error || "");
  const [firm, setFirm] = useState(boot.firm || "");
  const [crop, setCrop] = useState(boot.crop || "");
  const [dateFrom, setDateFrom] = useState(boot.dateFrom || "");
  const [timeFrom, setTimeFrom] = useState(boot.timeFrom ?? "06:00");
  const [dateTo, setDateTo] = useState(boot.dateTo || "");
  const [timeTo, setTimeTo] = useState(boot.timeTo ?? "05:59");
  const [plate, setPlate] = useState(boot.plate || "");
  const [surname, setSurname] = useState(boot.surname || "");
  const [columns, setColumns] = useState(boot.columns || EMPTY_COLUMNS);
  const [mode, setMode] = useState(boot.mode || "rows");
  const [sortKey, setSortKey] = useState(boot.sortKey || "tonnes");
  const [sortDir, setSortDir] = useState(boot.sortDir || "desc");
  const [page, setPage] = useState(Number.isFinite(boot.page) ? boot.page : 0);
  const [pageSize, setPageSize] = useState(boot.pageSize || 50);
  const [fullScreen, setFullScreen] = useState(false);

  function restore(saved) {
    const data = saved || {};
    setRows(Array.isArray(data.rows) ? data.rows : []);
    setWindows(data.windows || null);
    setApplied(data.applied ?? null);
    setLoaded(Boolean(data.loaded));
    setError(data.error || "");
    setFirm(data.firm || "");
    setCrop(data.crop || "");
    setDateFrom(data.dateFrom || "");
    setTimeFrom(data.timeFrom ?? "06:00");
    setDateTo(data.dateTo || "");
    setTimeTo(data.timeTo ?? "05:59");
    setPlate(data.plate || "");
    setSurname(data.surname || "");
    setColumns(data.columns || EMPTY_COLUMNS);
    setMode(data.mode || "rows");
    setSortKey(data.sortKey || "tonnes");
    setSortDir(data.sortDir || "desc");
    setPage(Number.isFinite(data.page) ? data.page : 0);
    setPageSize(data.pageSize || 50);
  }

  const memoryKey = `namolot:${reportDate}`;
  const snapshot = useMemo(
    () => ({
      rows,
      windows,
      applied,
      loaded,
      error,
      firm,
      crop,
      dateFrom,
      timeFrom,
      dateTo,
      timeTo,
      plate,
      surname,
      columns,
      mode,
      sortKey,
      sortDir,
      page,
      pageSize,
    }),
    [
      rows,
      windows,
      applied,
      loaded,
      error,
      firm,
      crop,
      dateFrom,
      timeFrom,
      dateTo,
      timeTo,
      plate,
      surname,
      columns,
      mode,
      sortKey,
      sortDir,
      page,
      pageSize,
    ],
  );
  useViewSnapshot(memoryKey, snapshot, restore);

  useEffect(() => {
    const sync = () => setFullScreen(isComputerFullscreen());
    document.addEventListener("fullscreenchange", sync);
    return () => document.removeEventListener("fullscreenchange", sync);
  }, []);

  useEffect(() => {
    if (open) return undefined;
    if (document.fullscreenElement) {
      document.exitFullscreen().catch(() => {});
    }
    return undefined;
  }, [open]);

  useEffect(() => {
    if (!open || !fullScreen) return undefined;
    const onKey = (event) => {
      if (event.key !== "Escape" || !isComputerFullscreen()) return;
      const menuOpen = document.querySelector(".MuiMenu-root, .MuiPopover-root");
      if (menuOpen) {
        event.preventDefault();
        return;
      }
      event.stopPropagation();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [open, fullScreen]);

  async function toggleComputerFullscreen() {
    try {
      if (document.fullscreenElement) {
        await document.exitFullscreen();
      } else {
        await document.documentElement.requestFullscreen();
      }
    } catch {
      // Браузер может запретить полноэкранный режим без жеста пользователя.
    }
  }

  const ranged = useMemo(() => {
    if (!applied) return [];
    return filterRows(rows, {
      firms: applied.firm ? [applied.firm] : [],
      crops: applied.crop ? [applied.crop] : [],
      dateFrom: applied.dateFrom,
      timeFrom: applied.timeFrom,
      dateTo: applied.dateTo,
      timeTo: applied.timeTo,
      plate: applied.plate,
      surname: applied.surname,
    });
  }, [rows, applied]);

  const columnOptions = useMemo(() => distinctColumnValues(ranged), [ranged]);

  const visible = useMemo(() => {
    const picked = applyColumnFilters(ranged, columns);
    if (mode === "rows") return sortRecordRows(picked, sortKey, sortDir);
    return rankRows(picked, mode, sortDir);
  }, [ranged, columns, mode, sortKey, sortDir]);

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

  function pickColumn(key, value) {
    setPage(0);
    setColumns((current) => ({ ...current, [key]: value }));
  }

  async function form({ force = false } = {}) {
    const next = { firm, crop, dateFrom, timeFrom, dateTo, timeTo, plate, surname };
    setColumns(EMPTY_COLUMNS);
    setApplied(next);
    setPage(0);
    // Сохранённая таблица только на экране. «Сформировать» снова запрашивает Namolot.
    if (loading) return;
    if (!force && loaded) return;
    const hadRows = loaded;
    setLoading(true);
    setError("");
    try {
      const data = await fetchNamolotSeason({ reportDate });
      setWindows(data);
      setRows(Array.isArray(data?.rows) ? data.rows : []);
      setLoaded(true);
    } catch (err) {
      if (!hadRows) {
        setRows([]);
        setWindows(null);
        setLoaded(false);
      }
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
    setColumns(EMPTY_COLUMNS);
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
    <Dialog
      open={open}
      onClose={busy ? undefined : onClose}
      fullScreen={fullScreen}
      fullWidth
      maxWidth="lg"
      scroll="paper"
      slotProps={{
        paper: {
          sx: fullScreen
            ? {
                display: "flex",
                flexDirection: "column",
                width: "100%",
                height: "100%",
                maxWidth: "100%",
                maxHeight: "100%",
                m: 0,
                borderRadius: 0,
              }
            : undefined,
        },
      }}
    >
      <DialogTitle sx={{ py: 1.25, pr: 7, position: "relative" }}>
        Namolot
        <Tooltip title={fullScreen ? "Выйти из полноэкранного режима (Esc)" : "На весь экран компьютера"}>
          <IconButton
            aria-label={fullScreen ? "Выйти из полноэкранного режима" : "На весь экран компьютера"}
            onClick={() => void toggleComputerFullscreen()}
            sx={{ position: "absolute", right: 8, top: 6 }}
          >
            {fullScreen ? <CloseFullscreenRoundedIcon /> : <OpenInFullRoundedIcon />}
          </IconButton>
        </Tooltip>
      </DialogTitle>
      <DialogContent sx={fullScreen ? { flex: 1, minHeight: 0 } : undefined}>
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
          <Button variant="contained" onClick={() => void form({ force: true })} disabled={loading}>
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
        {applied && loaded ? (
          <>
            <Typography variant="body2" sx={{ mb: 1 }}>
              {mode === "rows" ? "Записей" : "Строк"} {visible.length.toLocaleString("ru-RU")}
              {" · "}
              {fmtTonnes(shownTonnes)} т
              {visible.length !== rows.length ? ` из ${rows.length.toLocaleString("ru-RU")}` : ""}
              . Сортировка по намолоту {sortDir === "desc" ? "убывает" : "растёт"}.
            </Typography>
            {visible.length ? (
              <Box sx={{ maxHeight: fullScreen ? "calc(100vh - 460px)" : 460, overflow: "auto" }}>
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
      <TableCell>
        <ColumnFilter label="Госномер" values={options.plate} selected={columns.plate} onSelect={(value) => onColumn("plate", value)} />
      </TableCell>
      <TableCell>
        <ColumnFilter label="Механизатор" values={options.operator} selected={columns.operator} onSelect={(value) => onColumn("operator", value)} />
      </TableCell>
      <TableCell>
        <ColumnFilter label="Поле" values={options.field} selected={columns.field} onSelect={(value) => onColumn("field", value)} />
      </TableCell>
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
