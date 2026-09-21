import { Alert, Box, Button, Card, CardContent, Container, Stack, TextField, Typography } from "@mui/material";

import { customBackgroundSx } from "../constants/themes";

export default function LoginPage({
  theme = "dark",
  customBackground = "",
  email,
  password,
  mfaCode,
  mfaRequired,
  error,
  busy,
  onEmailChange,
  onPasswordChange,
  onMfaCodeChange,
  onSubmit,
}) {
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
                Вход в Центр PDF-сводок
              </Typography>
              <Typography color="text.secondary" variant="body2">
                {mfaRequired
                  ? "Введите код из приложения аутентификации."
                  : "Email и пароль учётной записи администратора (задаются в .env: BOOTSTRAP_ADMIN_EMAIL)."}
              </Typography>
              {error && <Alert severity="error">{error}</Alert>}
              {!mfaRequired && (
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
                    fullWidth
                    required
                  />
                </>
              )}
              {mfaRequired && (
                <TextField
                  label="Код MFA"
                  value={mfaCode}
                  onChange={(e) => onMfaCodeChange(e.target.value)}
                  autoFocus
                  fullWidth
                  required
                />
              )}
              <Button type="submit" variant="contained" color="primary" size="large" fullWidth disabled={busy}>
                {busy ? "…" : mfaRequired ? "Подтвердить" : "Войти"}
              </Button>
            </Stack>
          </CardContent>
        </Card>
      </Container>
    </Box>
  );
}
