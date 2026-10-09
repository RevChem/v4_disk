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
  const [configured, setConfigured] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return undefined;
    let cancelled = false;
    setCookie("");
    setError("");
    setLoading(true);
    fetchCropwiseSessionCookie()
      .then((data) => {
        if (!cancelled) setConfigured(Boolean(data?.configured));
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

  async function writeCookie(next) {
    setSaving(true);
    setError("");
    try {
      const saved = await saveCropwiseSessionCookie(next);
      setConfigured(Boolean(saved?.configured));
      setCookie("");
      await onSaved?.(saved);
      if (next.trim()) onClose?.();
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
          {configured
            ? "Cookie задана. Посмотреть или скопировать её нельзя: можно только удалить или вставить новую."
            : "Cookie не задана. Вставьте свою — она запишется в .env и будет использоваться для проверок и DeepSeek."}
        </Typography>
        <TextField
          label="Новая cookie"
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
          helperText="Только вставка новой cookie. Текущее значение на экран не выводится."
        />
        {error ? (
          <Alert severity="error" sx={{ mt: 2 }}>
            {error}
          </Alert>
        ) : null}
      </DialogContent>
      <DialogActions>
        <Button color="error" onClick={() => void writeCookie("")} disabled={loading || saving || !configured}>
          Удалить
        </Button>
        <Button onClick={onClose} disabled={saving}>
          Отмена
        </Button>
        <Button
          variant="contained"
          onClick={() => void writeCookie(cookie)}
          disabled={loading || saving || !cookie.trim()}
        >
          {saving ? "Сохранение…" : "Сохранить"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
