import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  TextField,
  Typography,
} from "@mui/material";

import { fetchCropwiseSessionCookie, saveCropwiseSessionCookie } from "../api";

export default function CropwiseCookieDialog({ open, onClose, onSaved }) {
  const [cookie, setCookie] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return undefined;
    let cancelled = false;
    setError("");
    setLoading(true);
    fetchCropwiseSessionCookie()
      .then((data) => {
        if (!cancelled) setCookie(String(data?.cookie || ""));
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

  async function handleSave() {
    setSaving(true);
    setError("");
    try {
      const saved = await saveCropwiseSessionCookie(cookie);
      setCookie(String(saved?.cookie || ""));
      await onSaved?.(saved);
      onClose?.();
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onClose={saving ? undefined : onClose} fullWidth maxWidth="sm">
      <DialogTitle>Cookie</DialogTitle>
      <DialogContent>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Значение записывается в .env как CROPWISE_SESSION_COOKIE и заменяет прежнюю cookie.
          Проверки сводок, сверка с Cropwise и запросы DeepSeek берут её оттуда.
        </Typography>
        <TextField
          label="Cookie Cropwise"
          value={cookie}
          onChange={(event) => setCookie(event.target.value)}
          multiline
          minRows={4}
          maxRows={10}
          fullWidth
          disabled={loading || saving}
          autoComplete="off"
          spellCheck={false}
          placeholder="_cropio_ses=...; другие_куки=..."
          helperText="Вставьте заголовок Cookie из DevTools. Пустое поле очищает cookie. Несколько строк склеиваются через «;»."
        />
        {error ? (
          <Alert severity="error" sx={{ mt: 2 }}>
            {error}
          </Alert>
        ) : null}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={saving}>
          Отмена
        </Button>
        <Button variant="contained" onClick={() => void handleSave()} disabled={loading || saving}>
          {saving ? "Сохранение…" : "Сохранить"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
