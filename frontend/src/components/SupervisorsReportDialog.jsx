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

import { downloadSupervisorsReport, fetchSupervisorsWorkGroups } from "../api";

const DEFAULT_FROM = "2026-11-01T06:00";
const DEFAULT_TO = "2026-11-30T05:59";

function moscowStamp(value) {
  const text = String(value || "").trim();
  if (text.length === 16) return `${text}:00+03:00`;
  if (text.length === 19) return `${text}+03:00`;
  return text;
}

export default function SupervisorsReportDialog({ open, onClose }) {
  const [start, setStart] = useState(DEFAULT_FROM);
  const [end, setEnd] = useState(DEFAULT_TO);
  const [groups, setGroups] = useState([]);
  const [selected, setSelected] = useState([]);
  const [groupsError, setGroupsError] = useState("");
  const [error, setError] = useState("");
  const [generating, setGenerating] = useState(false);

  useEffect(() => {
    if (!open) return;
    setStart(DEFAULT_FROM);
    setEnd(DEFAULT_TO);
    setError("");
    setGroupsError("");
    let cancelled = false;
    fetchSupervisorsWorkGroups()
      .then((items) => {
        if (cancelled) return;
        const list = Array.isArray(items) ? items : [];
        setGroups(list);
        setSelected(list.map((item) => item.id));
      })
      .catch((err) => {
        if (cancelled) return;
        setGroups([]);
        setSelected([]);
        setGroupsError(err?.message || "Не удалось загрузить типы работ.");
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  const allChecked = groups.length > 0 && selected.length === groups.length;

  function toggleAll() {
    setSelected(allChecked ? [] : groups.map((item) => item.id));
  }

  function toggleOne(id) {
    setSelected((prev) => (prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id]));
  }

  async function submit() {
    if (!start || !end) {
      setError("Укажите начало и конец периода.");
      return;
    }
    if (new Date(start).getTime() >= new Date(end).getTime()) {
      setError("Конец периода должен быть позже начала.");
      return;
    }
    if (groups.length && selected.length === 0) {
      setError("Выберите хотя бы один тип работ.");
      return;
    }
    setGenerating(true);
    setError("");
    try {
      const workGroupIds = allChecked ? [] : selected;
      await downloadSupervisorsReport({
        start: moscowStamp(start),
        end: moscowStamp(end),
        workGroupIds,
      });
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
      <DialogTitle>Отсутствие данных по топливу</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ pt: 0.5 }}>
          {error && <Alert severity="error">{error}</Alert>}
          {groupsError && <Alert severity="warning">{groupsError}</Alert>}
          <Typography variant="body2" color="text.secondary">
            Задания машин за выбранный интервал. Время московское. Файл делится на листы agri,
            transport и наемники. Сбор за месяц может занять несколько минут.
          </Typography>
          <TextField
            label="Начало"
            type="datetime-local"
            value={start}
            onChange={(event) => setStart(event.target.value)}
            InputLabelProps={{ shrink: true }}
            size="small"
            disabled={generating}
            fullWidth
          />
          <TextField
            label="Конец"
            type="datetime-local"
            value={end}
            onChange={(event) => setEnd(event.target.value)}
            InputLabelProps={{ shrink: true }}
            size="small"
            disabled={generating}
            fullWidth
          />
          <Typography variant="subtitle2">Тип работ</Typography>
          <FormGroup>
            <FormControlLabel
              control={
                <Checkbox
                  size="small"
                  checked={allChecked}
                  indeterminate={selected.length > 0 && !allChecked}
                  onChange={toggleAll}
                  disabled={generating || !groups.length}
                />
              }
              label="Все"
            />
            {groups.map((item) => (
              <FormControlLabel
                key={item.id}
                control={
                  <Checkbox
                    size="small"
                    checked={selected.includes(item.id)}
                    onChange={() => toggleOne(item.id)}
                    disabled={generating}
                  />
                }
                label={item.name}
              />
            ))}
          </FormGroup>
        </Stack>
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2 }}>
        <Button onClick={onClose} disabled={generating}>
          Отмена
        </Button>
        <Button variant="contained" onClick={() => void submit()} disabled={generating}>
          {generating ? <CircularProgress size={18} color="inherit" /> : "Сформировать"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
