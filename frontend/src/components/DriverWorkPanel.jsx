import { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Autocomplete,
  Box,
  Button,
  Link,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Typography,
} from "@mui/material";
import DownloadOutlinedIcon from "@mui/icons-material/DownloadOutlined";

import { fetchDriverWork, fetchDriverWorkPeople } from "../api";

const POPPER_PROPS = { popper: { sx: { zIndex: 1600 } } };

function monthBounds(iso) {
  const [y, m] = String(iso || "").split("-").map(Number);
  if (!y || !m) {
    const now = new Date();
    return monthBounds(`${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-01`);
  }
  const last = new Date(y, m, 0).getDate();
  const mm = String(m).padStart(2, "0");
  return { from: `${y}-${mm}-01`, to: `${y}-${mm}-${String(last).padStart(2, "0")}` };
}

function fmtDate(iso) {
  const [y, m, d] = String(iso || "").split("-");
  return y && m && d ? `${d}.${m}.${y}` : iso || "";
}

function csvEscape(value) {
  const s = value == null ? "" : String(value);
  return /[";\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

const CSV_COLUMNS = [
  ["date", "Дата"],
  ["driver", "ФИО"],
  ["firm", "Агрофирма"],
  ["field", "Поле"],
  ["operation", "Операция"],
  ["agro_operation", "Агрооперация"],
  ["ha", "Га"],
  ["task_id", "Задание"],
  ["task_url", "Ссылка"],
];

function rowsToCsv(rows) {
  const lines = [CSV_COLUMNS.map(([, label]) => csvEscape(label)).join(";")];
  for (const row of rows) {
    lines.push(CSV_COLUMNS.map(([key]) => csvEscape(key === "date" ? fmtDate(row[key]) : row[key])).join(";"));
  }
  return `\uFEFF${lines.join("\r\n")}`;
}

export default function DriverWorkPanel({ summaryDate = "", preset = null }) {
  const initial = useMemo(() => monthBounds(summaryDate), [summaryDate]);
  const [dateFrom, setDateFrom] = useState(initial.from);
  const [dateTo, setDateTo] = useState(initial.to);
  const [people, setPeople] = useState([]);
  const [person, setPerson] = useState(null);
  const [namesNote, setNamesNote] = useState("");
  const [result, setResult] = useState(null);
  const [loadingPeople, setLoadingPeople] = useState(false);
  const [loadingRows, setLoadingRows] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const bounds = monthBounds(summaryDate);
    setDateFrom(bounds.from);
    setDateTo(bounds.to);
    setPeople([]);
    setPerson(null);
    setResult(null);
    setNamesNote("");
    setError("");
  }, [summaryDate]);

  useEffect(() => {
    if (!preset?.person?.id) return undefined;
    const from = preset.dateFrom;
    const to = preset.dateTo;
    setDateFrom(from);
    setDateTo(to);
    setPeople([preset.person]);
    setPerson(preset.person);
    setResult(null);
    setError("");
    setNamesNote("");
    let alive = true;
    setLoadingRows(true);
    fetchDriverWork({ dateFrom: from, dateTo: to, driverId: preset.person.id })
      .then((data) => {
        if (!alive) return;
        if (!data.available) {
          setResult(null);
          setError(data.detail || "Хранилище заданий недоступно");
          return;
        }
        setResult(data);
        setNamesNote(data.names_note || "");
      })
      .catch((e) => {
        if (alive) setError(String(e.message || e));
      })
      .finally(() => {
        if (alive) setLoadingRows(false);
      });
    return () => {
      alive = false;
    };
  }, [preset]);

  async function loadPeople() {
    setLoadingPeople(true);
    setError("");
    setPerson(null);
    setResult(null);
    try {
      const data = await fetchDriverWorkPeople({ dateFrom, dateTo });
      if (!data.available) {
        setPeople([]);
        setError(data.detail || "Хранилище заданий недоступно");
        return;
      }
      setPeople(data.people || []);
      setNamesNote(data.names_note || "");
      if (!(data.people || []).length) {
        setError("За этот период в хранилище нет гектаров с исполнителем.");
      }
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setLoadingPeople(false);
    }
  }

  async function loadRows(next) {
    if (!next?.id) return;
    setLoadingRows(true);
    setError("");
    try {
      const data = await fetchDriverWork({ dateFrom, dateTo, driverId: next.id });
      if (!data.available) {
        setResult(null);
        setError(data.detail || "Хранилище заданий недоступно");
        return;
      }
      setResult(data);
      setNamesNote(data.names_note || "");
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setLoadingRows(false);
    }
  }

  function downloadCsv() {
    const rows = result?.rows || [];
    if (!rows.length) return;
    const blob = new Blob([rowsToCsv(rows)], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `driver_${person?.id || "work"}_${dateFrom}_${dateTo}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  const rows = result?.rows || [];

  return (
    <Box>
        <Stack spacing={2}>
          <Typography variant="body2" color="text.secondary">
            Гектары из хранилища заданий по сменным дням (с 06:00). ФИО подставляется из Cropwise по номеру исполнителя.
            Расход топлива по исполнителям смотрите во вкладке «Топливо».
          </Typography>
          <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} alignItems={{ sm: "center" }}>
            <TextField
              label="С"
              type="date"
              size="small"
              value={dateFrom}
              onChange={(e) => {
                setDateFrom(e.target.value);
                setPeople([]);
                setPerson(null);
                setResult(null);
              }}
              InputLabelProps={{ shrink: true }}
            />
            <TextField
              label="По"
              type="date"
              size="small"
              value={dateTo}
              onChange={(e) => {
                setDateTo(e.target.value);
                setPeople([]);
                setPerson(null);
                setResult(null);
              }}
              InputLabelProps={{ shrink: true }}
            />
            <Button variant="contained" onClick={loadPeople} disabled={loadingPeople || !dateFrom || !dateTo}>
              {loadingPeople ? "Поиск…" : "Найти исполнителей"}
            </Button>
          </Stack>
          <Autocomplete
            options={people}
            value={person}
            onChange={(_, value) => {
              setPerson(value);
              setResult(null);
              if (value) loadRows(value);
            }}
            getOptionLabel={(option) => `${option.name} · ${Number(option.ha).toFixed(2)} га · ${option.tasks} зад.`}
            isOptionEqualToValue={(a, b) => a.id === b.id}
            slotProps={POPPER_PROPS}
            disabled={!people.length}
            renderInput={(params) => (
              <TextField {...params} label="Исполнитель" size="small" placeholder="Иванов Иван Иванович" />
            )}
          />
          {namesNote && <Alert severity="info">{namesNote}</Alert>}
          {error && <Alert severity="warning">{error}</Alert>}
          {result && (
            <Stack direction="row" spacing={2} alignItems="center">
              <Typography variant="body2">
                {result.driver || person?.name}: {Number(result.ha_total || 0).toFixed(2)} га, заданий {result.tasks || 0}
              </Typography>
              <Box sx={{ flex: 1 }} />
              <Button
                size="small"
                startIcon={<DownloadOutlinedIcon />}
                onClick={downloadCsv}
                disabled={!rows.length}
              >
                CSV
              </Button>
            </Stack>
          )}
          {loadingRows && <Typography variant="body2">Считаю строки…</Typography>}
          {result && !rows.length && !loadingRows && (
            <Typography variant="body2">За период у этого исполнителя нет гектаров.</Typography>
          )}
          {result?.truncated && (
            <Alert severity="info">Показаны первые 5000 строк. Сумма гектаров посчитана по всем.</Alert>
          )}
          {rows.length > 0 && (
            <Box sx={{ maxHeight: "calc(92vh - 430px)", minHeight: 240, overflow: "auto" }}>
              <Table size="small" stickyHeader>
                <TableHead>
                  <TableRow>
                    <TableCell>Дата</TableCell>
                    <TableCell>Агрофирма</TableCell>
                    <TableCell>Поле</TableCell>
                    <TableCell>Операция</TableCell>
                    <TableCell>Агрооперация</TableCell>
                    <TableCell align="right">Га</TableCell>
                    <TableCell>Задание</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {rows.map((row) => (
                    <TableRow key={`${row.date}-${row.task_id}-${row.field_id}`}>
                      <TableCell>{fmtDate(row.date)}</TableCell>
                      <TableCell>{row.firm || "—"}</TableCell>
                      <TableCell>{row.field || "—"}</TableCell>
                      <TableCell>{row.operation || "—"}</TableCell>
                      <TableCell>{row.agro_operation || "—"}</TableCell>
                      <TableCell align="right">{Number(row.ha).toFixed(2)}</TableCell>
                      <TableCell>
                        {row.task_url ? (
                          <Link href={row.task_url} target="_blank" rel="noreferrer">
                            {row.task_id}
                          </Link>
                        ) : (
                          row.task_id
                        )}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Box>
          )}
        </Stack>
    </Box>
  );
}
