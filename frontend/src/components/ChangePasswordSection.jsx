import { useState } from "react";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Stack,
  TextField,
  Typography,
} from "@mui/material";

import { changeMyPassword } from "../api";

const ERROR_MAP = {
  wrong_current_password: "Неверный текущий пароль",
  password_pwned: "Пароль слишком простой или был в утечках — выберите другой",
  password_not_saved: "Пароль не сохранился. Попробуйте ещё раз.",
};

function errorMessage(err) {
  const code = err?.response?.data?.detail;
  if (typeof code === "string") return ERROR_MAP[code] || code;
  if (Array.isArray(code)) return code.map((item) => item?.msg || JSON.stringify(item)).join("; ");
  return err?.message || "Не удалось сменить пароль";
}

export default function ChangePasswordSection({ onChanged, compact = false }) {
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  async function handleSubmit(e) {
    e.preventDefault();
    setError("");
    setSuccess("");
    if (newPassword.length < 8) {
      setError("Новый пароль — минимум 8 символов");
      return;
    }
    if (newPassword !== confirmPassword) {
      setError("Новый пароль и подтверждение не совпадают");
      return;
    }
    setBusy(true);
    try {
      await changeMyPassword(currentPassword, newPassword);
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      setSuccess("Пароль изменён. Войдите снова с новым паролем.");
      onChanged?.();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Stack
      component="form"
      spacing={1.25}
      onSubmit={handleSubmit}
      sx={compact ? undefined : { maxWidth: 420 }}
    >
      {!compact && (
        <Typography variant="subtitle2" fontWeight={700}>
          Смена пароля
        </Typography>
      )}
      {error && <Alert severity="error">{error}</Alert>}
      {success && <Alert severity="success">{success}</Alert>}
      <TextField
        required
        fullWidth
        size="small"
        type="password"
        autoComplete="current-password"
        label="Текущий пароль"
        value={currentPassword}
        onChange={(e) => setCurrentPassword(e.target.value)}
      />
      <TextField
        required
        fullWidth
        size="small"
        type="password"
        autoComplete="new-password"
        label="Новый пароль"
        helperText="Минимум 8 символов"
        FormHelperTextProps={{ sx: { mt: 0.25, fontSize: "0.7rem" } }}
        value={newPassword}
        onChange={(e) => setNewPassword(e.target.value)}
      />
      <TextField
        required
        fullWidth
        size="small"
        type="password"
        autoComplete="new-password"
        label="Подтверждение"
        value={confirmPassword}
        onChange={(e) => setConfirmPassword(e.target.value)}
      />
      <Box>
        <Button
          type="submit"
          size="small"
          variant="contained"
          disabled={busy || !currentPassword || newPassword.length < 8}
          startIcon={busy ? <CircularProgress size={14} color="inherit" /> : null}
        >
          Сохранить пароль
        </Button>
      </Box>
    </Stack>
  );
}
