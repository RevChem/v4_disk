import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Stack,
  TextField,
  Typography,
} from "@mui/material";

import { downloadFieldHarvest } from "../api";

export default function FieldHarvestDialog({ open, onClose }) {
  const [year, setYear] = useState(String(new Date().getFullYear()));
  const [error, setError] = useState("");
  const [generating, setGenerating] = useState(false);

  useEffect(() => {
    if (!open) return;
    setYear(String(new Date().getFullYear()));
    setError("");
  }, [open]);

  async function submit() {
    const season = Number(year);
    if (!Number.isInteger(season) || season < 2000 || season > 2100) {
      setError("Укажите год сезона от 2000 до 2100.");
      return;
    }
    setGenerating(true);
    setError("");
    try {
      await downloadFieldHarvest(season);
      onClose?.();
    } catch (err) {
      setError(err?.message || "Не удалось сформировать отчёт.");
    } finally {
      setGenerating(false);
    }
  }

  return (
    <Dialog
      open={open}
      onClose={() => {
        if (!generating) onClose?.();
      }}
      fullWidth
      maxWidth="xs"
    >
      <DialogTitle>Валовый сбор</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ pt: 0.5 }}>
          {error && <Alert severity="error">{error}</Alert>}
          <Typography variant="body2" color="text.secondary">
            По каждому действующему полю сезона: название, учётные гектары, культура, сорт,
            физический валовый сбор и урожай в тоннах, урожайность из карточки поля, дата сева
            и дата уборки. Архивные поля не входят: в сезоне нет записи о доступности поля.
            Колонка «Посеяно, га» — факт посева, на листе сортов она суммируется по сорту.
          </Typography>
          <TextField
            label="Сезон"
            type="number"
            value={year}
            onChange={(event) => setYear(event.target.value)}
            disabled={generating}
            inputProps={{ min: 2000, max: 2100 }}
          />
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={() => onClose?.()} disabled={generating}>
          Закрыть
        </Button>
        <Button variant="contained" onClick={submit} disabled={generating}>
          {generating ? <CircularProgress size={18} color="inherit" /> : "Скачать Excel"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
