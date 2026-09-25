import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import PsychologyOutlinedIcon from "@mui/icons-material/PsychologyOutlined";
import EditNoteOutlinedIcon from "@mui/icons-material/EditNoteOutlined";
import SummarizeOutlinedIcon from "@mui/icons-material/SummarizeOutlined";
import QuestionAnswerOutlinedIcon from "@mui/icons-material/QuestionAnswerOutlined";

import { fetchProverkaAiSettings, saveProverkaAiSettings } from "../api";

export default function AiMenuDialog({
  open,
  onClose,
  onOpenReport,
  onOpenPrompts,
  onOpenTaskFacts,
  isDeveloper = true,
}) {
  const [settings, setSettings] = useState(null);
  const [model, setModel] = useState("deepseek-flash");
  const [apiKey, setApiKey] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await fetchProverkaAiSettings();
      setSettings(data);
      setModel(data.model || "deepseek-flash");
      setApiKey("");
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) load();
  }, [open, load]);

  async function handleSaveSettings() {
    setSaving(true);
    setError("");
    setSuccess("");
    try {
      const payload = { model };
      if (apiKey.trim()) payload.api_key = apiKey.trim();
      const data = await saveProverkaAiSettings(payload);
      setSettings(data);
      setApiKey("");
      setSuccess("Настройки сохранены.");
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1 }}>
        <PsychologyOutlinedIcon color="primary" />
        AI
      </DialogTitle>
      <DialogContent dividers>
        <Stack spacing={2}>
          <Stack direction={{ xs: "column", sm: "row" }} spacing={1}>
            <Button
              variant="contained"
              fullWidth
              startIcon={<SummarizeOutlinedIcon />}
              onClick={() => {
                onClose();
                onOpenReport?.();
              }}
            >
              Отчёт AI
            </Button>
            <Button
              variant="outlined"
              fullWidth
              startIcon={<EditNoteOutlinedIcon />}
              onClick={() => {
                onClose();
                onOpenPrompts?.();
              }}
            >
              Промпты AI
            </Button>
          </Stack>

          {isDeveloper && (
            <Button
              variant="outlined"
              fullWidth
              startIcon={<QuestionAnswerOutlinedIcon />}
              onClick={() => {
                onClose();
                onOpenTaskFacts?.();
              }}
            >
              Вопросы по заданиям
            </Button>
          )}

          <Box sx={{ pt: 1, borderTop: 1, borderColor: "divider" }}>
            <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 1.5 }}>
              DeepSeek
            </Typography>
            {error && <Alert severity="error" sx={{ mb: 1.5, py: 0.5 }}>{error}</Alert>}
            {success && <Alert severity="success" sx={{ mb: 1.5, py: 0.5 }}>{success}</Alert>}
            <Stack spacing={1.5}>
              <FormControl fullWidth size="small" disabled={loading}>
                <InputLabel id="ai-model-label">Модель</InputLabel>
                <Select
                  labelId="ai-model-label"
                  label="Модель"
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                >
                  {(settings?.models || ["deepseek-flash", "deepseek-chat", "deepseek-reasoner"]).map((m) => (
                    <MenuItem key={m} value={m}>
                      {m}
                    </MenuItem>
                  ))}
                </Select>
              </FormControl>
              <TextField
                label="Токен доступа API"
                type="password"
                size="small"
                fullWidth
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={settings?.api_key_set ? `Задан: ${settings.api_key_masked}` : "sk-…"}
                helperText={
                  settings?.api_key_source === "env"
                    ? "Сейчас используется ключ из .env. Новый ключ сохранится в uploads."
                    : "Оставьте пустым, чтобы не менять текущий ключ."
                }
              />
              <Button variant="outlined" onClick={handleSaveSettings} disabled={saving || loading}>
                {saving ? "Сохранение…" : "Сохранить настройки"}
              </Button>
            </Stack>
          </Box>
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Закрыть</Button>
      </DialogActions>
    </Dialog>
  );
}
