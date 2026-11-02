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
import AssignmentOutlinedIcon from "@mui/icons-material/AssignmentOutlined";

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

function renderNavItem(item, activeId, onNavChange) {
  const btn = (
    <Button
      variant={activeId === item.id ? "contained" : "outlined"}
      size="small"
      onClick={() => onNavChange(item.id)}
    >
      {item.label}
    </Button>
  );
  if (item.badge) {
    return (
      <Badge key={item.id} badgeContent={item.badge} color="error" max={99}>
        {btn}
      </Badge>
    );
  }
  return <span key={item.id}>{btn}</span>;
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
  equipment = null,
  dataErrors = null,
  report2Period = null,
  onCookieOpen = null,
  onNamolotOpen = null,
  onFieldOwnershipOpen = null,
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
                    <Stack spacing={0.5} alignItems="stretch">
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
                      <Tooltip title={cropwise.seasonCaption}>
                        <Button
                          size="small"
                          variant="contained"
                          onClick={cropwise.onSeason}
                          sx={{
                            minWidth: 0,
                            px: 1,
                            py: 0.2,
                            fontSize: "0.75rem",
                            lineHeight: 1.5,
                            textTransform: "none",
                            bgcolor: cropwise.seasonActive ? "success.main" : "error.main",
                            "&:hover": {
                              bgcolor: cropwise.seasonActive ? "success.dark" : "error.dark",
                            },
                          }}
                        >
                          Cropwise за сезон
                        </Button>
                      </Tooltip>
                    </Stack>
                  ) : null}
                  {onCookieOpen || onNamolotOpen || onFieldOwnershipOpen ? (
                    <Stack spacing={0.5} alignItems="stretch">
                      {onCookieOpen ? (
                        <Tooltip title="Cookie Cropwise: записать в .env и использовать во всех проверках">
                          <Button
                            size="small"
                            variant="outlined"
                            onClick={onCookieOpen}
                            sx={{
                              minWidth: 0,
                              px: 1,
                              py: 0.2,
                              fontSize: "0.75rem",
                              lineHeight: 1.5,
                              textTransform: "none",
                            }}
                          >
                            Cookie
                          </Button>
                        </Tooltip>
                      ) : null}
                      {onNamolotOpen ? (
                        <Tooltip title="Намолот из API 1С за период, по агрофирмам">
                          <Button
                            size="small"
                            variant="outlined"
                            onClick={onNamolotOpen}
                            sx={{
                              minWidth: 0,
                              px: 1,
                              py: 0.2,
                              fontSize: "0.75rem",
                              lineHeight: 1.5,
                              textTransform: "none",
                            }}
                          >
                            Namolot
                          </Button>
                        </Tooltip>
                      ) : null}
                      {onFieldOwnershipOpen ? (
                        <Tooltip title="Поля, у которых с выбранной даты сменилась принадлежность агрофирме">
                          <Button
                            size="small"
                            variant="outlined"
                            onClick={onFieldOwnershipOpen}
                            sx={{
                              minWidth: 0,
                              px: 1,
                              py: 0.2,
                              fontSize: "0.75rem",
                              lineHeight: 1.5,
                              textTransform: "none",
                            }}
                          >
                            Принадлежность полей
                          </Button>
                        </Tooltip>
                      ) : null}
                    </Stack>
                  ) : null}
                  {equipment || dataErrors || report2Period ? (
                    <Stack spacing={0.5} alignItems="stretch">
                      {equipment ? (
                        <Tooltip title={equipment.caption}>
                          <Button
                            size="small"
                            variant="outlined"
                            onClick={equipment.onClick}
                            sx={{
                              minWidth: 0,
                              px: 1,
                              py: 0.2,
                              fontSize: "0.75rem",
                              lineHeight: 1.5,
                              textTransform: "none",
                            }}
                          >
                            Оборудование
                          </Button>
                        </Tooltip>
                      ) : null}
                      {dataErrors ? (
                        <Tooltip title={dataErrors.active ? "Ошибки данных включены" : "Показать ошибки данных в сводках"}>
                          <Button
                            size="small"
                            variant={dataErrors.active ? "contained" : "outlined"}
                            color={dataErrors.active ? "success" : "primary"}
                            onClick={dataErrors.onToggle}
                            sx={{
                              minWidth: 0,
                              px: 1,
                              py: 0.2,
                              fontSize: "0.75rem",
                              lineHeight: 1.5,
                              textTransform: "none",
                            }}
                          >
                            Ошибки
                          </Button>
                        </Tooltip>
                      ) : null}
                      {report2Period ? (
                        <Tooltip title="Сводка 2 за период: даты, агрооперации и сезоны">
                          <Button
                            size="small"
                            variant="outlined"
                            onClick={report2Period.onOpen}
                            sx={{
                              minWidth: 0,
                              px: 1,
                              py: 0.2,
                              fontSize: "0.75rem",
                              lineHeight: 1.5,
                              textTransform: "none",
                            }}
                          >
                            Сводка 2
                          </Button>
                        </Tooltip>
                      ) : null}
                    </Stack>
                  ) : null}
                  {(nav || [])
                    .filter((item) => item.id === "users")
                    .map((item) => renderNavItem(item, navActiveId ?? activeNav, onNavChange))}
                  {showQueueButton ? (
                    <Button size="small" variant="outlined" onClick={onQueueOpen}>
                      {queueLabel || "Очередь"}
                    </Button>
                  ) : null}
                  {(nav || [])
                    .filter((item) => item.id !== "users")
                    .map((item) => renderNavItem(item, navActiveId ?? activeNav, onNavChange))}
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
                        startIcon={<AssignmentOutlinedIcon />}
                        onClick={onAiOpen}
                      >
                        Задания
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
