import { useEffect, useMemo, useState } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import { updatePlansEditorCalendarSeasons } from "./api";

function formatRu(iso) {
  if (!iso) return "—";
  const [y, m, d] = String(iso).split("-");
  if (!y || !m || !d) return iso;
  return `${d}.${m}.${y}`;
}

export default function PlansSeasonsEditor({
  calendarSeasons = {},
  reportDate,
  summaryDate,
  onSaved,
  saving,
  setSaving,
  setError,
  setSuccess,
}) {
  const [rows, setRows] = useState([]);

  const yearKeys = useMemo(() => {
    const keys = Object.keys(calendarSeasons || {});
    if (keys.length) return keys.sort((a, b) => Number(a) - Number(b));
    const y = reportDate ? new Date(reportDate).getFullYear() : new Date().getFullYear();
    return [String(y - 1), String(y), String(y + 1), String(y + 2)];
  }, [calendarSeasons, reportDate]);

  useEffect(() => {
    setRows(
      yearKeys.map((year) => {
        const entry = calendarSeasons?.[year] || {};
        const y = Number(year);
        return {
          year,
          period_start: entry.period_start || (Number.isFinite(y) ? `${y - 1}-07-01` : ""),
          period_end: entry.period_end || (Number.isFinite(y) ? `${y}-07-01` : ""),
        };
      })
    );
  }, [calendarSeasons, yearKeys]);

  function updateRow(idx, field, value) {
    setRows((prev) => prev.map((r, i) => (i === idx ? { ...r, [field]: value } : r)));
  }

  async function handleSave() {
    setSaving(true);
    setError("");
    setSuccess("");
    try {
      const calendar_seasons = {};
      for (const row of rows) {
        if (!row.year?.trim()) continue;
        calendar_seasons[row.year.trim()] = {
          period_start: row.period_start,
          period_end: row.period_end,
        };
      }
      const data = await updatePlansEditorCalendarSeasons({ calendar_seasons }, summaryDate);
      onSaved?.(data);
      setSuccess("Календарь сезонов сохранён");
    } catch (e) {
      setError(e?.response?.data?.detail ?? e?.message ?? "Ошибка сохранения");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Stack spacing={2}>
      <Alert severity="info">
        Календарные границы сезонов. Для группы работ выбираются годы сезона — диапазоны{" "}
        <strong>объединяются</strong> и обрезаются датой отчёта.
        <br />
        Пример: 2026 + 2027 при отчёте {formatRu(reportDate)} → от{" "}
        {rows[0]?.period_start ? formatRu(rows[0].period_start) : "01.07.2025"} до{" "}
        {formatRu(reportDate)}.
      </Alert>

      <Paper variant="outlined">
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>Сезон (год)</TableCell>
              <TableCell>Начало</TableCell>
              <TableCell>Конец (граница сезона)</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {rows.map((row, idx) => (
              <TableRow key={row.year}>
                <TableCell>
                  <Typography variant="body2" fontWeight={600}>
                    {row.year}
                  </Typography>
                </TableCell>
                <TableCell>
                  <TextField
                    size="small"
                    type="date"
                    value={row.period_start}
                    onChange={(e) => updateRow(idx, "period_start", e.target.value)}
                    disabled={saving}
                    InputLabelProps={{ shrink: true }}
                  />
                </TableCell>
                <TableCell>
                  <TextField
                    size="small"
                    type="date"
                    value={row.period_end}
                    onChange={(e) => updateRow(idx, "period_end", e.target.value)}
                    disabled={saving}
                    InputLabelProps={{ shrink: true }}
                  />
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </Paper>

      <Button variant="contained" disabled={saving} onClick={handleSave}>
        Сохранить календарь сезонов
      </Button>

      <Typography variant="caption" color="text.secondary">
        Файл: season_ranges.json → calendar_seasons. Даты групп работ задаются только выбором сезонов на
        вкладках планов.
      </Typography>
    </Stack>
  );
}
