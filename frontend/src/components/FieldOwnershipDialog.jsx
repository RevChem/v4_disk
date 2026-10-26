import { useState } from "react";
import {
  Alert,
  Button,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  MenuItem,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Typography,
} from "@mui/material";

import { fetchFieldOwnership } from "../api";

const PRESETS = [
  { id: "2025-06-01", label: "01.06.2025" },
  { id: "2026-01-01", label: "01.01.2026" },
  { id: "2026-06-01", label: "01.06.2026" },
  { id: "2026-06-10", label: "10.06.2026" },
  { id: "custom", label: "Другая дата" },
];

function fmtDay(value) {
  const text = String(value || "");
  const match = text.match(/^(\d{4})-(\d{2})-(\d{2})/);
  return match ? `${match[3]}.${match[2]}.${match[1]}` : text || "—";
}

function verdict(row) {
  if (row.tag_changed && row.folder_differs) return "Пометка менялась и не совпадает с папкой";
  if (row.tag_changed) return "Пометка менялась";
  return "Пометка не совпадает с папкой";
}

export default function FieldOwnershipDialog({ open, onClose }) {
  const [preset, setPreset] = useState("2025-06-01");
  const [customDate, setCustomDate] = useState("2025-06-01");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [report, setReport] = useState(null);

  const since = preset === "custom" ? customDate : preset;

  async function search() {
    if (!since) return;
    setLoading(true);
    setError("");
    setReport(null);
    try {
      setReport(await fetchFieldOwnership({ since }));
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setLoading(false);
    }
  }

  return (
    <Dialog open={open} onClose={loading ? undefined : onClose} fullWidth maxWidth="lg">
      <DialogTitle>Принадлежность полей</DialogTitle>
      <DialogContent>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          С выбранной даты ищутся поля, у которых в Namolot менялась пометка агрофирмы
          («(АФ3)», «АФ5-») или эта пометка не совпадает с текущей папкой поля в Cropwise.
        </Typography>
        <Stack direction={{ xs: "column", sm: "row" }} spacing={1} sx={{ mb: 2 }} alignItems={{ sm: "center" }}>
          <TextField
            select
            label="С даты"
            size="small"
            value={preset}
            onChange={(event) => setPreset(event.target.value)}
            disabled={loading}
            sx={{ minWidth: 220 }}
          >
            {PRESETS.map((item) => (
              <MenuItem key={item.id} value={item.id}>
                {item.label}
              </MenuItem>
            ))}
          </TextField>
          {preset === "custom" ? (
            <TextField
              label="Дата"
              type="date"
              size="small"
              value={customDate}
              onChange={(event) => setCustomDate(event.target.value)}
              disabled={loading}
              InputLabelProps={{ shrink: true }}
            />
          ) : null}
          <Button variant="contained" onClick={() => void search()} disabled={loading || !since}>
            {loading ? "Сверка…" : "Показать"}
          </Button>
        </Stack>
        {loading ? (
          <Alert severity="info">
            Сверяются записи Namolot и папки Cropwise. Если дата раньше кэша, период дочитывается из 1С и это может занять несколько минут.
          </Alert>
        ) : null}
        {error ? <Alert severity="error">{error}</Alert> : null}
        {report ? (
          <Stack spacing={1.5}>
            {report.records ? (
              <Typography variant="body2" color="text.secondary">
                Просмотрены записи {fmtDay(report.covered_from)} — {fmtDay(report.covered_to)}
                {report.source ? ` (${report.source})` : ""}. Записей Namolot: {report.records}. Полей со сменой: {report.items?.length || 0}.
              </Typography>
            ) : null}
            {report.warning ? <Alert severity="warning">{report.warning}</Alert> : null}
            {report.items?.length ? (
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>Поле</TableCell>
                    <TableCell>Папка Cropwise</TableCell>
                    <TableCell>Пометки с даты</TableCell>
                    <TableCell>Что изменилось</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {report.items.map((row) => (
                    <TableRow key={row.code}>
                      <TableCell sx={{ whiteSpace: "nowrap" }}>
                        {row.code}
                        {row.known_transfer ? (
                          <Chip size="small" label="уже в передачах" sx={{ ml: 1 }} />
                        ) : null}
                      </TableCell>
                      <TableCell>{row.cropwise_firm || "—"}</TableCell>
                      <TableCell>
                        {(row.periods || []).map((period) => (
                          <div key={`${row.code}-${period.firm}`}>
                            {period.firm}: {fmtDay(period.from)}
                            {period.to !== period.from ? ` — ${fmtDay(period.to)}` : ""}, {period.tonnes} т
                          </div>
                        ))}
                      </TableCell>
                      <TableCell>{verdict(row)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <Typography variant="body2">С этой даты смена принадлежности не найдена.</Typography>
            )}
          </Stack>
        ) : null}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={loading}>Закрыть</Button>
      </DialogActions>
    </Dialog>
  );
}
