import { useState } from "react";
import {
  Badge,
  Box,
  Button,
  Card,
  CardContent,
  Container,
  IconButton,
  Stack,
  Tooltip,
  Typography,
} from "@mui/material";
import AccountCircleOutlinedIcon from "@mui/icons-material/AccountCircleOutlined";
import LogoutRoundedIcon from "@mui/icons-material/LogoutRounded";
import PaletteOutlinedIcon from "@mui/icons-material/PaletteOutlined";
import PsychologyOutlinedIcon from "@mui/icons-material/PsychologyOutlined";

import logoUrl from "../assets/logo.png";
import UserProfileDialog from "../components/UserProfileDialog";
import { customBackgroundSx } from "../constants/themes";

const ROLE_LABELS = {
  developer: "разработчик",
  admin: "админ",
  user: "пользователь",
};

function roleLabel(role) {
  return ROLE_LABELS[role] || role;
}

export default function AppShell({
  theme,
  customBackground,
  user,
  onLogout,
  nav,
  activeNav,
  onNavChange,
  navActiveId,
  queueLabel,
  onQueueOpen,
  showQueueButton = false,
  showAiButton = false,
  onInterfaceOpen,
  onAiOpen,
  aiStatus = "none",
  aiViolationCount = 0,
  secondaryToolbar = null,
  onPasswordChanged,
  cropwise = null,
  children,
}) {
  const [profileOpen, setProfileOpen] = useState(false);

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
      <Container maxWidth="xl" disableGutters sx={{ py: 5, px: { xs: 1, sm: 1.5 } }}>
        <Stack spacing={3}>
          <Card className="hero-card" elevation={0}>
            <CardContent sx={{ p: 4 }}>
              <Stack
                direction={{ xs: "column", md: "row" }}
                justifyContent="space-between"
                alignItems="center"
                spacing={2}
              >
                <Stack direction="row" spacing={2} alignItems="center" sx={{ flex: 1, minWidth: 0 }}>
                  <Box className="logo-frame">
                    <Box
                      component="img"
                      className="logo-img"
                      src={logoUrl}
                      alt="Логотип"
                      sx={(t) => ({
                        width: 58,
                        height: 58,
                        objectFit: "contain",
                        p: 0.5,
                        filter:
                          t.palette.mode === "dark" ? "drop-shadow(0 0 10px rgba(255,255,255,0.2))" : "none",
                      })}
                    />
                  </Box>
                  <Box sx={{ minWidth: 0 }}>
                    <Typography variant="h4" color="text.primary" sx={{ fontWeight: 700 }}>
                      Центр PDF-сводок
                    </Typography>
                    <Typography variant="body1" color="text.secondary" sx={{ mt: 1 }}>
                      Ежедневные документы с быстрым доступом по дате.
                    </Typography>
                    {user && (
                      <Typography variant="caption" color="text.secondary" sx={{ mt: 0.5, display: "block" }}>
                        {user.full_name || user.email || user.username} · {roleLabel(user.role)}
                      </Typography>
                    )}
                  </Box>
                </Stack>
                <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
                  {cropwise ? (
                    <Tooltip title={cropwise.caption}>
                      <Button
                        size="small"
                        variant="contained"
                        onClick={cropwise.onToggle}
                        sx={{
                          minWidth: 0,
                          px: 1,
                          py: 0.2,
                          fontSize: "0.75rem",
                          lineHeight: 1.5,
                          textTransform: "none",
                          bgcolor: cropwise.active ? "success.main" : "error.main",
                          "&:hover": {
                            bgcolor: cropwise.active ? "success.dark" : "error.dark",
                          },
                        }}
                      >
                        Cropwise
                      </Button>
                    </Tooltip>
                  ) : null}
                  {nav?.map((item) => {
                    const btn = (
                      <Button
                        variant={(navActiveId ?? activeNav) === item.id ? "contained" : "outlined"}
                        size="small"
                        onClick={() => onNavChange(item.id)}
                      >
                        {item.label}
                      </Button>
                    );
                    const wrapped = item.badge ? (
                      <Badge key={item.id} badgeContent={item.badge} color="error" max={99}>
                        {btn}
                      </Badge>
                    ) : (
                      <span key={item.id}>{btn}</span>
                    );
                    if (item.id !== "dashboard" || !showQueueButton) {
                      return wrapped;
                    }
                    return (
                      <Stack key={item.id} direction="row" spacing={1} alignItems="center">
                        {wrapped}
                        <Button size="small" variant="outlined" onClick={onQueueOpen}>
                          {queueLabel || "Очередь"}
                        </Button>
                      </Stack>
                    );
                  })}
                  {user && (
                    <Button
                      size="small"
                      variant="outlined"
                      startIcon={<AccountCircleOutlinedIcon />}
                      onClick={() => setProfileOpen(true)}
                    >
                      Профиль
                    </Button>
                  )}
                  {onInterfaceOpen && (
                    <Button size="small" variant="outlined" startIcon={<PaletteOutlinedIcon />} onClick={onInterfaceOpen}>
                      Интерфейс
                    </Button>
                  )}
                  {showAiButton && onAiOpen && (
                    <Badge badgeContent={aiViolationCount > 0 ? aiViolationCount : null} color="error" max={99}>
                      <Button
                        size="small"
                        variant="contained"
                        color={aiStatus === "completed" ? "success" : "primary"}
                        startIcon={<PsychologyOutlinedIcon />}
                        onClick={onAiOpen}
                      >
                        AI
                      </Button>
                    </Badge>
                  )}
                  <IconButton aria-label="Выйти" onClick={onLogout} color="default">
                    <LogoutRoundedIcon />
                  </IconButton>
                </Stack>
              </Stack>
              {secondaryToolbar ? (
                <Stack
                  direction="row"
                  spacing={0.75}
                  flexWrap="wrap"
                  useFlexGap
                  sx={{
                    mt: 2,
                    pt: 1.75,
                    borderTop: 1,
                    borderColor: "divider",
                  }}
                >
                  {secondaryToolbar}
                </Stack>
              ) : null}
            </CardContent>
          </Card>
          {children}
        </Stack>
      </Container>
      {user && (
        <UserProfileDialog
          self
          open={profileOpen}
          onClose={() => setProfileOpen(false)}
          onPasswordChanged={onPasswordChanged}
        />
      )}
    </Box>
  );
}
