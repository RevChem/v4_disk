import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import EditNoteRoundedIcon from "@mui/icons-material/EditNoteRounded";

import { fetchProverkaAiPrompts, resetProverkaAiPrompts, saveProverkaAiPrompts } from "../api";

export default function AiPromptsDialog({ open, onClose }) {
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [promptContext, setPromptContext] = useState("");
  const [rules, setRules] = useState([]);
  const [isCustom, setIsCustom] = useState(false);
  const [updatedAt, setUpdatedAt] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    setSuccess("");
    try {
      const data = await fetchProverkaAiPrompts();
      setPromptContext(data.prompt_context || "");
      setRules(data.rules || []);
      setIsCustom(Boolean(data.is_custom));
      setUpdatedAt(data.updated_at || null);
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) load();
  }, [open, load]);

  async function handleSave() {
    setSaving(true);
    setError("");
    setSuccess("");
    try {
      const data = await saveProverkaAiPrompts({
        prompt_context: promptContext,
        rules,
      });
      setIsCustom(Boolean(data.is_custom));
      setUpdatedAt(data.updated_at || null);
      setSuccess("Сохранено. Будет использовано при следующем AI-разборе.");
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setSaving(false);
    }
  }

  async function handleReset() {
    setSaving(true);
    setError("");
    setSuccess("");
    try {
      const data = await resetProverkaAiPrompts();
      setPromptContext(data.prompt_context || "");
      setRules(data.rules || []);
      setIsCustom(false);
      setUpdatedAt(null);
      setSuccess("Сброшено к значениям по умолчанию.");
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setSaving(false);
    }
  }

  function updateRule(idx, field, value) {
    setRules((prev) =>
      prev.map((r, i) => (i === idx ? { ...r, [field]: value } : r))
    );
  }

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="md">
      <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1 }}>
        <EditNoteRoundedIcon color="primary" />
        Промпты DeepSeek
      </DialogTitle>
      <DialogContent dividers>
        <Stack spacing={2}>
          <Typography variant="body2" color="text.secondary">
            Этот текст добавляется в system prompt при AI-разборе proverka. Доменные проверки
            (кукуруза/силос и т.д.) выполняются кодом; здесь — пояснения для нейросети.
          </Typography>
          {isCustom && (
            <Alert severity="info">
              Используются сохранённые промпты
              {updatedAt ? ` (обновлено ${new Date(updatedAt).toLocaleString("ru-RU")})` : ""}.
            </Alert>
          )}
          {error && <Alert severity="error">{error}</Alert>}
          {success && <Alert severity="success">{success}</Alert>}
          <TextField
            label="Общий контекст (prompt_context)"
            value={promptContext}
            onChange={(e) => setPromptContext(e.target.value)}
            multiline
            minRows={5}
            fullWidth
            disabled={loading || saving}
            placeholder="Роль ассистента, приоритеты, ограничения…"
          />
          {(rules || []).map((rule, idx) => (
            <Box
              key={rule.id || idx}
              sx={{ p: 1.5, border: 1, borderColor: "divider", borderRadius: 1 }}
            >
              <Typography variant="subtitle2" gutterBottom>
                {rule.title || rule.id || `Правило ${idx + 1}`}
              </Typography>
              <TextField
                label="Описание для LLM"
                value={rule.description || ""}
                onChange={(e) => updateRule(idx, "description", e.target.value)}
                multiline
                minRows={3}
                fullWidth
                size="small"
                disabled={loading || saving}
              />
            </Box>
          ))}
        </Stack>
      </DialogContent>
      <DialogActions sx={{ px: 2, py: 1.5, justifyContent: "space-between" }}>
        <Button onClick={handleReset} disabled={loading || saving} color="inherit">
          Сбросить по умолчанию
        </Button>
        <Stack direction="row" spacing={1}>
          <Button onClick={onClose} disabled={saving}>
            Закрыть
          </Button>
          <Button variant="contained" onClick={handleSave} disabled={loading || saving}>
            {saving ? "Сохранение…" : "Сохранить"}
          </Button>
        </Stack>
      </DialogActions>
    </Dialog>
  );
}
