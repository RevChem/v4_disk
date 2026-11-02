import { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControlLabel,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import dayjs from "dayjs";

import { fetchPlansEditor } from "../api";

const REPORT2_GROUPS = [
  "1.1 Внесение минеральных удобрений",
  "2.1 Боронование",
  "2.9 Дискование",
  "2.4 Вспашка",
  "2.11 Культивация",
  "4.0 Посев",
  "4.1 Посев (овощи)",
  "3.1 Опрыскивание",
  "3.2 Опрыскивание (овощи)",
  "2.12 Прикатывание",
  "2.7 Гребнеобразование",
  "2.6 Фрезерование",
  "2.8 Глубокорыхление",
  "5.0 Уборка",
  "5.4 Уборка картофеля, лука",
];

function seasonYearOf(iso) {
  const value = dayjs(iso);
  if (!value.isValid()) return null;
  return value.year() + (value.month() >= 6 ? 1 : 0);
}

function yearChoices(dateFrom, dateTo, extra = []) {
  const start = dayjs(dateFrom);
  const end = dayjs(dateTo);
  const years = new Set();
  if (start.isValid() && end.isValid()) {
    const lo = start.year() - 1;
    const hi = seasonYearOf(dateTo) ?? end.year();
    for (let year = lo; year <= hi; year += 1) years.add(year);
  }
  for (const year of extra) {
    const value = Number(year);
    if (Number.isFinite(value)) years.add(value);
  }
  if (!years.size) {
    years.add(2025);
    years.add(2026);
    years.add(2027);
  }
  return [...years].sort((a, b) => a - b);
}

function defaultYears(choices, saved) {
  const picked = (saved || []).map(Number).filter((year) => choices.includes(year));
  if (picked.length) return picked;
  if (choices.length <= 2) return [...choices];
  return choices.slice(-2);
}

export default function Report2PeriodDialog({
  open,
  summaryDate,
  onClose,
  onSubmit,
  generating,
  error,
}) {
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [enabled, setEnabled] = useState(() => Object.fromEntries(REPORT2_GROUPS.map((name) => [name, true])));
  const [seasons, setSeasons] = useState({});
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");

  useEffect(() => {
    if (!open) return undefined;
    const end = summaryDate || dayjs().format("YYYY-MM-DD");
    const endDate = dayjs(end);
    const start = endDate.month() >= 1
      ? endDate.month(1).date(14).format("YYYY-MM-DD")
      : endDate.subtract(1, "year").month(1).date(14).format("YYYY-MM-DD");
    const initialFrom = dayjs(start).isAfter(endDate) ? end : start;
    setDateFrom(initialFrom);
    setDateTo(end);
    setLoadError("");
    setEnabled(Object.fromEntries(REPORT2_GROUPS.map((name) => [name, true])));
    let cancelled = false;
    setLoading(true);
    fetchPlansEditor(end)
      .then((data) => {
        if (cancelled) return;
        const byGroup = data?.season_meta?.by_group || {};
        const next = {};
        for (const name of REPORT2_GROUPS) {
          const meta = byGroup[name] || {};
          const saved = meta.season_years || meta.default_season_years || [];
          const extra = yearChoices(initialFrom, end, saved);
          next[name] = defaultYears(extra, saved);
        }
        setSeasons(next);
      })
      .catch((e) => {
        if (cancelled) return;
        const choices = yearChoices(initialFrom, end);
        const next = {};
        for (const name of REPORT2_GROUPS) next[name] = defaultYears(choices, []);
        setSeasons(next);
        setLoadError(String(e?.message || e || "Не удалось прочитать сезоны редактора"));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, summaryDate]);

  const choices = useMemo(
    () => yearChoices(dateFrom, dateTo, Object.values(seasons).flat()),
    [dateFrom, dateTo, seasons],
  );
  const selectedNames = REPORT2_GROUPS.filter((name) => enabled[name]);
  const allOn = selectedNames.length === REPORT2_GROUPS.length;

  function toggleGroup(name) {
    setEnabled((prev) => ({ ...prev, [name]: !prev[name] }));
  }

  function toggleYear(name, year) {
    setSeasons((prev) => {
      const current = prev[name] || [];
      const next = current.includes(year)
        ? current.filter((item) => item !== year)
        : [...current, year].sort((a, b) => a - b);
      return { ...prev, [name]: next };
    });
  }

  function handleSubmit() {
    const seasonsPayload = {};
    for (const name of selectedNames) {
      seasonsPayload[name] = (seasons[name] || []).filter((year) => choices.includes(year));
    }
    onSubmit?.({
      dateFrom,
      dateTo,
      groups: selectedNames,
      seasons: seasonsPayload,
    });
  }

  return (
    <Dialog
      open={open}
      onClose={() => {
        if (!generating) onClose?.();
      }}
      fullWidth
      maxWidth="xl"
    >
      <DialogTitle>Сводка 2 за период</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ pt: 0.5 }}>
          {error ? <Alert severity="error">{error}</Alert> : null}
          {loadError ? <Alert severity="warning">{loadError}. Сезоны можно отметить вручную.</Alert> : null}
          <Typography variant="body2" color="text.secondary">
            Разовая сводка 2 за выбранные дни. В файл попадают смены с 06:00 первого дня
            по 06:00 следующего дня после конца, только отмеченные агрооперации и их сезоны.
            PDF не создаётся — появится кнопка «Скачать Excel». Обычная сводка за дату не заменяется.
          </Typography>
          <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} sx={{ maxWidth: 520 }}>
            <TextField
              label="С"
              type="date"
              value={dateFrom}
              onChange={(e) => setDateFrom(e.target.value)}
              InputLabelProps={{ shrink: true }}
              fullWidth
              size="small"
              disabled={generating}
            />
            <TextField
              label="По"
              type="date"
              value={dateTo}
              onChange={(e) => setDateTo(e.target.value)}
              InputLabelProps={{ shrink: true }}
              fullWidth
              size="small"
              disabled={generating}
            />
          </Stack>
          <Stack direction="row" justifyContent="space-between" alignItems="center">
            <Typography variant="subtitle2">Агрооперации и сезоны</Typography>
            <FormControlLabel
              control={
                <Checkbox
                  size="small"
                  checked={allOn}
                  indeterminate={selectedNames.length > 0 && !allOn}
                  onChange={(e) => {
                    const on = e.target.checked;
                    setEnabled(Object.fromEntries(REPORT2_GROUPS.map((name) => [name, on])));
                  }}
                  disabled={generating}
                />
              }
              label="Все операции"
            />
          </Stack>
          {loading ? (
            <Box sx={{ display: "flex", justifyContent: "center", py: 3 }}>
              <CircularProgress size={28} />
            </Box>
          ) : (
            <Box
              sx={{
                display: "grid",
                gridTemplateColumns: {
                  xs: "1fr",
                  sm: "repeat(2, minmax(0, 1fr))",
                  lg: "repeat(5, minmax(0, 1fr))",
                },
                gap: 1.25,
                alignItems: "start",
              }}
            >
              {REPORT2_GROUPS.map((name) => (
                <Box
                  key={name}
                  sx={{
                    border: 1,
                    borderColor: "divider",
                    borderRadius: 1,
                    px: 1.25,
                    py: 0.75,
                  }}
                >
                  <FormControlLabel
                    sx={{ alignItems: "flex-start", mr: 0 }}
                    control={
                      <Checkbox
                        size="small"
                        checked={Boolean(enabled[name])}
                        onChange={() => toggleGroup(name)}
                        disabled={generating}
                      />
                    }
                    label={<Typography variant="body2" sx={{ pt: 0.75 }}>{name}</Typography>}
                  />
                  {enabled[name] ? (
                    <Stack direction="row" spacing={0.75} useFlexGap flexWrap="wrap" sx={{ pl: 4, pb: 0.5 }}>
                      {choices.map((year) => {
                        const selected = (seasons[name] || []).includes(year);
                        return (
                          <Chip
                            key={year}
                            size="small"
                            label={String(year)}
                            clickable
                            color={selected ? "primary" : "default"}
                            variant={selected ? "filled" : "outlined"}
                            disabled={generating}
                            onClick={() => toggleYear(name, year)}
                          />
                        );
                      })}
                    </Stack>
                  ) : null}
                </Box>
              ))}
            </Box>
          )}
        </Stack>
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2 }}>
        <Button onClick={onClose} disabled={generating}>
          Отмена
        </Button>
        <Button variant="contained" onClick={handleSubmit} disabled={generating || loading}>
          {generating ? "Постановка…" : "Сформировать"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
