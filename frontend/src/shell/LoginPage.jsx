import { Alert, Box, Button, Card, CardContent, Container, Stack, TextField, Typography } from "@mui/material";

import { customBackgroundSx } from "../constants/themes";

const RESET_ERRORS = {
  invalid_token: "Ссылка недействительна или устарела. Запросите новую.",
  password_pwned: "Пароль слишком простой или был в утечках — выберите другой",
  password_not_saved: "Пароль не сохранился. Попробуйте ещё раз.",
  rate_limited: "Слишком много попыток. Подождите минуту.",
  smtp_not_configured: "Почта на сервере не настроена. Обратитесь к разработчику.",
};

function recoveryError(error) {
  return RESET_ERRORS[error] || error;
}

export default function LoginPage({
  theme = "dark",
  customBackground = "",
  view = "login",
  email,
  password,
  mfaCode,
  mfaRequired,
  error,
  info,
  busy,
  resetPassword = "",
  resetConfirm = "",
  onEmailChange,
  onPasswordChange,
  onMfaCodeChange,
  onResetPasswordChange,
  onResetConfirmChange,
  onSubmit,
  onForgot,
  onBackToLogin,
}) {
  const title =
    view === "forgot" ? "Восстановление пароля" : view === "reset" ? "Новый пароль" : "Вход в Центр PDF-сводок";
  const subtitle =
    view === "forgot"
      ? "Укажите email учётной записи. Если он есть в системе, пришлём ссылку для смены пароля."
      : view === "reset"
        ? "Придумайте новый пароль. Ссылка из письма действует один час."
        : mfaRequired
          ? "Введите код из приложения аутентификации."
          : "Email и пароль вашей учётной записи.";

  return (
    <Box
      className="app-bg"
      data-disk-theme={theme}
      data-disk-custom-bg={customBackground ? "1" : "0"}
      sx={{
        minHeight: "100vh",
        ...(customBackground
          ? { ...customBackgroundSx(customBackground), bgcolor: "transparent" }
          : { bgcolor: "background.default" }),
      }}
    >
      <Container maxWidth="sm" sx={{ py: 8 }}>
        <Card className="hero-card" elevation={0}>
          <CardContent sx={{ p: 4 }}>
            <Stack spacing={2.5} component="form" onSubmit={onSubmit}>
              <Typography variant="h5" color="text.primary">
                {title}
              </Typography>
              <Typography color="text.secondary" variant="body2">
                {subtitle}
              </Typography>
              {info && <Alert severity="success">{info}</Alert>}
              {error && <Alert severity="error">{recoveryError(error)}</Alert>}
              {view === "login" && !mfaRequired && (
                <>
                  <TextField
                    label="Email"
                    type="email"
                    value={email}
                    onChange={(e) => onEmailChange(e.target.value)}
                    autoFocus
                    fullWidth
                    required
                  />
                  <TextField
                    label="Пароль"
                    type="password"
                    value={password}
                    onChange={(e) => onPasswordChange(e.target.value)}
                    autoComplete="current-password"
                    fullWidth
                    required
                  />
                </>
              )}
              {view === "login" && mfaRequired && (
                <TextField
                  label="Код MFA"
                  value={mfaCode}
                  onChange={(e) => onMfaCodeChange(e.target.value)}
                  autoFocus
                  fullWidth
                  required
                />
              )}
              {view === "forgot" && (
                <TextField
                  label="Email"
                  type="email"
                  value={email}
                  onChange={(e) => onEmailChange(e.target.value)}
                  autoFocus
                  fullWidth
                  required
                />
              )}
              {view === "reset" && (
                <>
                  <TextField
                    label="Новый пароль"
                    type="password"
                    value={resetPassword}
                    onChange={(e) => onResetPasswordChange(e.target.value)}
                    autoComplete="new-password"
                    helperText="Минимум 8 символов"
                    autoFocus
                    fullWidth
                    required
                  />
                  <TextField
                    label="Подтверждение"
                    type="password"
                    value={resetConfirm}
                    onChange={(e) => onResetConfirmChange(e.target.value)}
                    autoComplete="new-password"
                    fullWidth
                    required
                  />
                </>
              )}
              <Button type="submit" variant="contained" color="primary" size="large" fullWidth disabled={busy}>
                {busy ? "…" : view === "forgot" ? "Отправить ссылку" : view === "reset" ? "Сохранить пароль" : mfaRequired ? "Подтвердить" : "Войти"}
              </Button>
              {view === "login" && !mfaRequired && (
                <Button type="button" color="inherit" onClick={onForgot} disabled={busy}>
                  Забыли пароль?
                </Button>
              )}
              {view !== "login" && (
                <Button type="button" color="inherit" onClick={onBackToLogin} disabled={busy}>
                  Вернуться ко входу
                </Button>
              )}
            </Stack>
          </CardContent>
        </Card>
      </Container>
    </Box>
  );
}
