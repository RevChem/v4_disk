import { useEffect, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  IconButton,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import DeleteOutlineIcon from "@mui/icons-material/DeleteOutline";

import { fetchPlateMappings, savePlateMappings } from "../api";

function emptyRow() {
  return { namolot: "", cropwise: "" };
}

export default function PlateMappingsDialog({ open, onClose }) {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (!open) return undefined;
    let cancelled = false;
    setError("");
    setSaved(false);
    setLoading(true);
    fetchPlateMappings()
      .then((data) => {
        if (!cancelled) setRows(Array.isArray(data?.items) ? data.items.map((item) => ({ ...item })) : []);
      })
      .catch((err) => {
        if (!cancelled) setError(String(err.message || err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  function updateRow(index, field, value) {
    setSaved(false);
    setRows((prev) => prev.map((row, i) => (i === index ? { ...row, [field]: value } : row)));
  }

  function removeRow(index) {
    setSaved(false);
    setRows((prev) => prev.filter((_, i) => i !== index));
  }

  async function save() {
    setSaving(true);
    setError("");
    setSaved(false);
    try {
      const payload = rows
        .map((row) => ({
          namolot: String(row.namolot || "").trim(),
          cropwise: String(row.cropwise || "").trim(),
        }))
        .filter((row) => row.namolot || row.cropwise);
      const data = await savePlateMappings(payload);
      setRows(Array.isArray(data?.items) ? data.items : payload);
      setSaved(true);
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onClose={saving ? undefined : onClose} fullWidth maxWidth="md">
      <DialogTitle>Сопоставления госномеров</DialogTitle>
      <DialogContent>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Namolot — как номер записан в 1С. Cropwise — госномер этой же машины в Cropwise.
          Следующая сводка 14 возьмёт сохранённый список. Пробелы и раскладка при поиске не мешают.
        </Typography>
        {error ? (
          <Alert severity="error" sx={{ mb: 2 }}>
            {error}
          </Alert>
        ) : null}
        {saved ? (
          <Alert severity="success" sx={{ mb: 2 }}>
            Сохранено. Новая сводка 14 уже использует этот список.
          </Alert>
        ) : null}
        <Stack spacing={1}>
          {rows.map((row, index) => (
            <Stack key={index} direction="row" spacing={1} alignItems="center">
              <TextField
                label="Namolot"
                value={row.namolot}
                onChange={(event) => updateRow(index, "namolot", event.target.value)}
                size="small"
                fullWidth
                disabled={loading || saving}
                placeholder="АЕ 2121 16"
              />
              <TextField
                label="Cropwise"
                value={row.cropwise}
                onChange={(event) => updateRow(index, "cropwise", event.target.value)}
                size="small"
                fullWidth
                disabled={loading || saving}
                placeholder="2121АЕ"
              />
              <IconButton
                aria-label="Удалить сопоставление"
                onClick={() => removeRow(index)}
                disabled={loading || saving}
              >
                <DeleteOutlineIcon />
              </IconButton>
            </Stack>
          ))}
          {!rows.length && !loading ? (
            <Typography variant="body2" color="text.secondary">
              Пар пока нет. Добавьте строку, если автоматическое сравнение номера не сходится.
            </Typography>
          ) : null}
        </Stack>
        <Box sx={{ mt: 2 }}>
          <Button
            onClick={() => {
              setSaved(false);
              setRows((prev) => [...prev, emptyRow()]);
            }}
            disabled={loading || saving}
          >
            Добавить
          </Button>
        </Box>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={saving}>
          Закрыть
        </Button>
        <Button variant="contained" onClick={save} disabled={loading || saving}>
          Сохранить
        </Button>
      </DialogActions>
    </Dialog>
  );
}
