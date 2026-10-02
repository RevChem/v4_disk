import {
  Alert,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import dayjs from "dayjs";

export default function Report27CustomDialog({
  open,
  onClose,
  dateFrom,
  dateTo,
  timeFrom,
  timeTo,
  onDateFromChange,
  onDateToChange,
  onTimeFromChange,
  onTimeToChange,
  generating,
  error,
  onSubmit,
}) {
  return (
    <Dialog
      open={open}
      onClose={() => {
        if (!generating) onClose?.();
      }}
      fullWidth
      maxWidth="xs"
    >
      <DialogTitle>Сводка 27 · Рейтинг за период</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ pt: 0.5 }}>
          {error && <Alert severity="error">{error}</Alert>}
          <Typography variant="body2" color="text.secondary">
            Разовая генерация рейтинга механизаторов за произвольный интервал. Вместе с 27
            ставится 27.1 (время остановок). PDF не создаётся — файлы появятся с кнопкой
            «Скачать Excel».
          </Typography>
          <Typography variant="subtitle2" fontWeight={600}>
            Начало периода
          </Typography>
          <Stack direction="row" spacing={1.5}>
            <TextField
              label="Дата"
              type="date"
              value={dateFrom}
              onChange={(e) => onDateFromChange?.(e.target.value)}
              InputLabelProps={{ shrink: true }}
              fullWidth
              size="small"
              disabled={generating}
            />
            <TextField
              label="Время"
              type="time"
              value={timeFrom}
              onChange={(e) => onTimeFromChange?.(e.target.value)}
              InputLabelProps={{ shrink: true }}
              fullWidth
              size="small"
              disabled={generating}
            />
          </Stack>
          <Typography variant="subtitle2" fontWeight={600}>
            Конец периода
          </Typography>
          <Stack direction="row" spacing={1.5}>
            <TextField
              label="Дата"
              type="date"
              value={dateTo}
              onChange={(e) => onDateToChange?.(e.target.value)}
              InputLabelProps={{ shrink: true }}
              fullWidth
              size="small"
              disabled={generating}
            />
            <TextField
              label="Время"
              type="time"
              value={timeTo}
              onChange={(e) => onTimeToChange?.(e.target.value)}
              InputLabelProps={{ shrink: true }}
              fullWidth
              size="small"
              disabled={generating}
            />
          </Stack>
          {dateFrom && dateTo && timeFrom && timeTo && (
            <Typography variant="caption" color="text.secondary">
              {dayjs(`${dateFrom}T${timeFrom}`).isValid() && dayjs(`${dateTo}T${timeTo}`).isValid()
                ? `${dateFrom} ${timeFrom} — ${dateTo} ${timeTo}`
                : "Проверьте дату и время"}
            </Typography>
          )}
        </Stack>
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2 }}>
        <Button onClick={onClose} disabled={generating}>
          Отмена
        </Button>
        <Button variant="contained" onClick={onSubmit} disabled={generating}>
          {generating ? "Постановка…" : "Сформировать"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
