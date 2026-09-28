import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  IconButton,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import EditNoteRoundedIcon from "@mui/icons-material/EditNoteRounded";
import AddRoundedIcon from "@mui/icons-material/AddRounded";
import DeleteOutlineRoundedIcon from "@mui/icons-material/DeleteOutlineRounded";

import { fetchProverkaAiPrompts, resetProverkaAiPrompts, saveProverkaAiPrompts } from "../api";

function newPromptId() {
  return `custom_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`;
}

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
    const prepared = (rules || []).map((rule) => ({
      id: String(rule.id || "").trim() || newPromptId(),
      title: String(rule.title || "").trim(),
      description: String(rule.description || "").trim(),
    }));
    const missingName = prepared.findIndex((rule) => !rule.title);
    if (missingName >= 0) {
      setSaving(false);
      setError(`Укажите название промпта ${missingName + 1}.`);
      return;
    }
    try {
      const data = await saveProverkaAiPrompts({
        prompt_context: promptContext,
        rules: prepared,
      });
      setPromptContext(data.prompt_context || "");
      setRules(data.rules || []);
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

  function addPrompt() {
    setSuccess("");
    setError("");
    setRules((prev) => [...prev, { id: newPromptId(), title: "", description: "" }]);
  }

  function removePrompt(idx) {
    setSuccess("");
    setRules((prev) => prev.filter((_, i) => i !== idx));
  }

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="md">
      <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1 }}>
        <EditNoteRoundedIcon color="primary" />
        Промпты DeepSeek
      </DialogTitle>
      <DialogContent
        dividers
        sx={{
          "& .MuiInputLabel-root": { fontSize: "0.625rem" },
          "& .MuiFormHelperText-root": { fontSize: "0.625rem" },
          "& .MuiInputBase-input": { fontSize: "0.8125rem" },
          "& .MuiTypography-body2": { fontSize: "0.8125rem" },
          "& .MuiTypography-subtitle2": { fontSize: "0.8125rem" },
          "& .MuiButton-root": { fontSize: "0.8125rem" },
        }}
      >
        <Stack spacing={2}>
          <Typography variant="body2" color="text.secondary">
            Общий контекст и промпты ниже добавляются в system prompt при AI-разборе proverka.
            У каждого промпта своё название. Доменные проверки (кукуруза/силос и т.д.) выполняются
            кодом; здесь — пояснения для нейросети.
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
              <Stack direction="row" spacing={1} alignItems="flex-start" sx={{ mb: 1 }}>
                <TextField
                  label="Название"
                  value={rule.title || ""}
                  onChange={(e) => updateRule(idx, "title", e.target.value)}
                  fullWidth
                  size="small"
                  disabled={loading || saving}
                  placeholder="Например: Проверка норм высева"
                />
                <IconButton
                  aria-label="Удалить промпт"
                  onClick={() => removePrompt(idx)}
                  disabled={loading || saving}
                  size="small"
                  sx={{ mt: 0.25 }}
                >
                  <DeleteOutlineRoundedIcon fontSize="small" />
                </IconButton>
              </Stack>
              <TextField
                label="Текст промпта"
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
          <Box>
            <Button
              startIcon={<AddRoundedIcon />}
              onClick={addPrompt}
              disabled={loading || saving}
            >
              Добавить промпт
            </Button>
          </Box>
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
