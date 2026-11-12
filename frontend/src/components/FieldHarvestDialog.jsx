import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControlLabel,
  FormGroup,
  Stack,
  TextField,
  Typography,
} from "@mui/material";

import { downloadFieldHarvest } from "../api";

const FIRMS = [
  "АФ Чистопольская",
  "АгроРаздолье",
  "Аксу-Агро",
  "Актай",
  "Дрожжаное Рассвет Агро",
  "Лениногорская Птицефабрика",
  "НурлатАгро",
];

export default function FieldHarvestDialog({ open, onClose }) {
  const [year, setYear] = useState(String(new Date().getFullYear()));
  const [firms, setFirms] = useState(FIRMS);
  const [error, setError] = useState("");
  const [generating, setGenerating] = useState(false);

  useEffect(() => {
    if (!open) return;
    setYear(String(new Date().getFullYear()));
    setFirms(FIRMS);
    setError("");
  }, [open]);

  function toggleFirm(name) {
    setFirms((current) =>
      current.includes(name) ? current.filter((item) => item !== name) : [...current, name]
    );
  }

  async function submit() {
    const season = Number(year);
    if (!Number.isInteger(season) || season < 2000 || season > 2100) {
      setError("Укажите год сезона от 2000 до 2100.");
      return;
    }
    if (!firms.length) {
      setError("Выберите хотя бы одну агрофирму.");
      return;
    }
    setGenerating(true);
    setError("");
    try {
      await downloadFieldHarvest(season, firms);
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
      maxWidth="sm"
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
          <FormGroup>
            <Typography variant="body2" sx={{ mb: 0.5 }}>
              Агрофирмы
            </Typography>
            {FIRMS.map((name) => (
              <FormControlLabel
                key={name}
                control={
                  <Checkbox
                    checked={firms.includes(name)}
                    onChange={() => toggleFirm(name)}
                    disabled={generating}
                  />
                }
                label={name}
              />
            ))}
          </FormGroup>
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
