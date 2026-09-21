import { useState } from "react";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
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
import ReportProblemRoundedIcon from "@mui/icons-material/ReportProblemRounded";

const CROPWISE_WORK_URL = "https://operations.cropwise.com/machines/reports/work";

export default function ViolationsDialog({
  open,
  onClose,
  monitoring,
  onEnable,
  enabling,
  error,
}) {
  const [cookie, setCookie] = useState("");

  const violations = monitoring?.violations || [];
  const count = monitoring?.cropwise_diff_count ?? monitoring?.violation_count ?? 0;
  const canEnableWithoutCookie =
    monitoring?.cookie_valid && !monitoring?.monitoring_enabled && !cookie.trim();

  async function handleSubmit() {
    await onEnable?.(cookie.trim() || null);
    setCookie("");
  }

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="sm">
      <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1 }}>
        <ReportProblemRoundedIcon color="error" />
        Проверка Cropwise
      </DialogTitle>
      <DialogContent dividers>
        <Stack spacing={2}>
          <Typography variant="body2" color="text.secondary">
            Введите cookie из браузера после входа в{" "}
            <Link href={CROPWISE_WORK_URL} target="_blank" rel="noopener noreferrer">
              Cropwise work report
            </Link>
            . Cookie хранится на сервере 4 часа и видна только вам. После включения каждая
            созданная сводка автоматически сверяется с Cropwise.
          </Typography>

          {error && <Alert severity="error">{error}</Alert>}

          <TextField
            label="Cookie (_cropio_ses=…)"
            value={cookie}
            onChange={(e) => setCookie(e.target.value)}
            fullWidth
            multiline
            minRows={2}
            disabled={enabling}
            placeholder="Скопируйте из DevTools → Application → Cookies"
          />

          {monitoring?.cookie_valid && !monitoring?.monitoring_enabled && (
            <Alert severity="info">
              Cookie ещё действует до{" "}
              {monitoring.cookie_expires_at
                ? new Date(monitoring.cookie_expires_at).toLocaleString("ru-RU")
                : "—"}
              . Можно включить проверку без повторного ввода.
            </Alert>
          )}

          {violations.length > 0 && (
            <Box sx={{ overflowX: "auto" }}>
              <Typography variant="subtitle2" sx={{ mb: 1 }}>
                Нарушения ({count})
              </Typography>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>Сводка</TableCell>
                    <TableCell>Детали</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {violations.map((v, idx) => (
                    <TableRow key={idx}>
                      <TableCell>
                        {v.report_id}. {v.title || "—"}
                      </TableCell>
                      <TableCell sx={{ whiteSpace: "normal" }}>{v.detail}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Box>
          )}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={enabling}>
          Отмена
        </Button>
        <Button
          variant="contained"
          color="success"
          onClick={handleSubmit}
          disabled={enabling || (!cookie.trim() && !canEnableWithoutCookie)}
          startIcon={enabling ? <CircularProgress size={16} /> : null}
        >
          {cookie.trim() ? "Сохранить и включить" : "Включить проверку"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
