import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Typography,
} from "@mui/material";
import PersonAddRoundedIcon from "@mui/icons-material/PersonAddRounded";
import RefreshRoundedIcon from "@mui/icons-material/RefreshRounded";
import BadgeOutlinedIcon from "@mui/icons-material/BadgeOutlined";

import { createManagedUser, fetchManagedUsers, updateManagedUser } from "../api";
import ChangePasswordSection from "../components/ChangePasswordSection";
import UserProfileDialog from "../components/UserProfileDialog";

const ROLE_OPTIONS = [
  { id: "user", label: "Пользователь" },
  { id: "admin", label: "Админ" },
  { id: "developer", label: "Разработчик" },
];

const COMPACT_CELL = { py: 0.75, px: 1.25, fontSize: "0.8125rem" };

function roleLabel(role) {
  return ROLE_OPTIONS.find((r) => r.id === role)?.label || role;
}

function formatLoginAt(iso) {
  if (!iso) return "не входил";
  try {
    return new Date(iso).toLocaleString("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function errorText(err) {
  const detail = err?.response?.data?.detail;
  const code = Array.isArray(detail)
    ? detail.map((item) => item?.msg || JSON.stringify(item)).join("; ")
    : detail || err?.message || String(err);
  const map = {
    email_taken: "Email уже занят",
    password_pwned: "Пароль слишком простой или был в утечках — выберите другой",
    password_not_saved: "Пароль не сохранился. Попробуйте ещё раз.",
    cannot_deactivate_self: "Нельзя отключить свою учётную запись",
    cannot_change_own_role: "Нельзя сменить свою роль",
    user_not_found: "Пользователь не найден",
  };
  return map[code] || code;
}

function UsersSelfAccountView({ currentUser, embedded, onPasswordChanged }) {
  const [profileOpen, setProfileOpen] = useState(false);

  if (!currentUser) {
    return null;
  }

  return (
    <Stack spacing={embedded ? 1.5 : 2.5}>
      {!embedded && (
        <Typography variant="h5" fontWeight={700}>
          Пользователи
        </Typography>
      )}
      <Typography variant="body2" color="text.secondary" sx={{ fontSize: "0.8125rem" }}>
        Здесь только ваша учётная запись. Управление всеми пользователями доступно роли «Разработчик».
      </Typography>
      <Card variant="outlined">
        <CardContent sx={{ py: embedded ? 1.5 : 2, px: embedded ? 1.5 : 2 }}>
          <Stack spacing={1.25}>
            <Typography variant="subtitle2" fontWeight={700}>
              {currentUser.full_name || currentUser.username}
            </Typography>
            <Stack spacing={0.25}>
              <Typography variant="body2" sx={{ fontSize: "0.8125rem" }}>
                Email: {currentUser.email || "—"}
              </Typography>
              <Typography variant="body2" sx={{ fontSize: "0.8125rem" }}>
                Логин: {currentUser.username}
              </Typography>
              <Typography variant="body2" sx={{ fontSize: "0.8125rem" }}>
                Роль: {roleLabel(currentUser.role)}
              </Typography>
            </Stack>
            <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
              <Button
                size="small"
                variant="outlined"
                startIcon={<BadgeOutlinedIcon fontSize="small" />}
                onClick={() => setProfileOpen(true)}
              >
                Профиль и активность
              </Button>
            </Stack>
          </Stack>
        </CardContent>
      </Card>
      <Card variant="outlined">
        <CardContent sx={{ py: embedded ? 1.5 : 2, px: embedded ? 1.5 : 2 }}>
          <ChangePasswordSection onChanged={onPasswordChanged} />
        </CardContent>
      </Card>
      <UserProfileDialog
        self
        open={profileOpen}
        onClose={() => setProfileOpen(false)}
        onPasswordChanged={onPasswordChanged}
      />
    </Stack>
  );
}

export default function UsersPage({
  currentUserId,
  currentUser,
  manageAll = false,
  embedded = false,
  onPasswordChanged,
}) {
  if (!manageAll) {
    return (
      <UsersSelfAccountView
        currentUser={currentUser}
        embedded={embedded}
        onPasswordChanged={onPasswordChanged}
      />
    );
  }

  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({
    email: "",
    username: "",
    fullName: "",
    password: "",
    role: "user",
  });
  const [profileUserId, setProfileUserId] = useState(null);
  const [passwordDialog, setPasswordDialog] = useState(null);
  const [newPassword, setNewPassword] = useState("");
  const [passwordError, setPasswordError] = useState("");
  const [savingPassword, setSavingPassword] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await fetchManagedUsers();
      setItems(data.items || []);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function handleCreate(e) {
    e.preventDefault();
    setCreating(true);
    setError("");
    setSuccess("");
    try {
      await createManagedUser({
        email: form.email.trim(),
        username: form.username.trim() || undefined,
        fullName: form.fullName.trim() || undefined,
        password: form.password,
        role: form.role,
      });
      setSuccess(`Пользователь ${form.email.trim()} создан`);
      setForm({ email: "", username: "", fullName: "", password: "", role: "user" });
      await load();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setCreating(false);
    }
  }

  async function handleRoleChange(user, role) {
    setError("");
    setSuccess("");
    try {
      await updateManagedUser(user.id, { role });
      setSuccess(`Роль ${user.email} → ${roleLabel(role)}`);
      await load();
    } catch (err) {
      setError(errorText(err));
    }
  }

  async function handleActiveChange(user, isActive) {
    setError("");
    setSuccess("");
    try {
      await updateManagedUser(user.id, { is_active: isActive });
      await load();
    } catch (err) {
      setError(errorText(err));
    }
  }

  function openPasswordDialog(row) {
    setPasswordDialog(row);
    setNewPassword("");
    setPasswordError("");
  }

  async function handlePasswordSave() {
    if (!passwordDialog || !newPassword) return;
    setSavingPassword(true);
    setPasswordError("");
    setError("");
    setSuccess("");
    try {
      const target = passwordDialog;
      await updateManagedUser(target.id, { password: newPassword });
      setPasswordDialog(null);
      setNewPassword("");
      if (target.id === currentUserId) {
        onPasswordChanged?.();
        return;
      }
      setSuccess(`Пароль для ${target.email} обновлён`);
    } catch (err) {
      setPasswordError(errorText(err));
    } finally {
      setSavingPassword(false);
    }
  }

  return (
    <Stack spacing={embedded ? 1.5 : 2.5}>
      {!embedded && (
        <Stack direction="row" alignItems="center" justifyContent="space-between" flexWrap="wrap" gap={1}>
          <Typography variant="h5" fontWeight={700}>
            Пользователи
          </Typography>
          <Button size="small" startIcon={<RefreshRoundedIcon />} onClick={load} disabled={loading}>
            Обновить
          </Button>
        </Stack>
      )}

      {embedded && (
        <Box sx={{ display: "flex", justifyContent: "flex-end" }}>
          <Button size="small" startIcon={<RefreshRoundedIcon />} onClick={load} disabled={loading}>
            Обновить
          </Button>
        </Box>
      )}

      {error && <Alert severity="error">{error}</Alert>}
      {success && <Alert severity="success">{success}</Alert>}

      <Card variant="outlined">
        <CardContent sx={{ py: embedded ? 1.5 : 2, px: embedded ? 1.5 : 2, "&:last-child": { pb: embedded ? 1.5 : 2 } }}>
          <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 1.5 }}>
            Новый пользователь
          </Typography>
          <Box component="form" onSubmit={handleCreate}>
            <Stack spacing={1.5}>
              <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5}>
                <TextField
                  required
                  fullWidth
                  size="small"
                  type="email"
                  label="Email (логин)"
                  value={form.email}
                  onChange={(e) => setForm((f) => ({ ...f, email: e.target.value }))}
                />
                <TextField
                  fullWidth
                  size="small"
                  label="Имя пользователя"
                  helperText="Необязательно"
                  FormHelperTextProps={{ sx: { mt: 0.25, fontSize: "0.7rem" } }}
                  value={form.username}
                  onChange={(e) => setForm((f) => ({ ...f, username: e.target.value }))}
                />
              </Stack>
              <TextField
                fullWidth
                size="small"
                label="ФИО"
                helperText="Необязательно"
                FormHelperTextProps={{ sx: { mt: 0.25, fontSize: "0.7rem" } }}
                value={form.fullName}
                onChange={(e) => setForm((f) => ({ ...f, fullName: e.target.value }))}
              />
              <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5}>
                <TextField
                  required
                  fullWidth
                  size="small"
                  type="password"
                  label="Пароль"
                  helperText="Мин. 8 символов"
                  FormHelperTextProps={{ sx: { mt: 0.25, fontSize: "0.7rem" } }}
                  value={form.password}
                  onChange={(e) => setForm((f) => ({ ...f, password: e.target.value }))}
                />
                <FormControl fullWidth size="small">
                  <InputLabel id="new-user-role">Роль</InputLabel>
                  <Select
                    labelId="new-user-role"
                    label="Роль"
                    value={form.role}
                    onChange={(e) => setForm((f) => ({ ...f, role: e.target.value }))}
                  >
                    {ROLE_OPTIONS.map((opt) => (
                      <MenuItem key={opt.id} value={opt.id}>
                        {opt.label}
                      </MenuItem>
                    ))}
                  </Select>
                </FormControl>
              </Stack>
              <Box>
                <Button
                  type="submit"
                  size="small"
                  variant="contained"
                  startIcon={creating ? <CircularProgress size={14} color="inherit" /> : <PersonAddRoundedIcon />}
                  disabled={creating}
                >
                  Создать
                </Button>
              </Box>
            </Stack>
          </Box>
        </CardContent>
      </Card>

      <Card variant="outlined">
        <CardContent sx={{ p: 0, "&:last-child": { pb: 0 } }}>
          {loading ? (
            <Box sx={{ p: 3, display: "flex", justifyContent: "center" }}>
              <CircularProgress size={28} />
            </Box>
          ) : (
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell sx={COMPACT_CELL}>Email</TableCell>
                  <TableCell sx={COMPACT_CELL}>Логин</TableCell>
                  <TableCell sx={COMPACT_CELL}>ФИО</TableCell>
                  <TableCell sx={COMPACT_CELL}>Роль</TableCell>
                  <TableCell sx={COMPACT_CELL}>Последний вход</TableCell>
                  <TableCell sx={COMPACT_CELL}>Активен</TableCell>
                  <TableCell align="right" sx={COMPACT_CELL}>
                    Профиль
                  </TableCell>
                  <TableCell align="right" sx={COMPACT_CELL}>
                    Пароль
                  </TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {items.map((row) => {
                  const isSelf = row.id === currentUserId;
                  return (
                    <TableRow key={row.id} hover>
                      <TableCell sx={COMPACT_CELL}>{row.email || "—"}</TableCell>
                      <TableCell sx={COMPACT_CELL}>{row.username}</TableCell>
                      <TableCell sx={COMPACT_CELL}>{row.full_name || "—"}</TableCell>
                      <TableCell sx={{ ...COMPACT_CELL, minWidth: 130, maxWidth: 160 }}>
                        <FormControl size="small" fullWidth disabled={isSelf}>
                          <Select
                            value={row.role}
                            onChange={(e) => handleRoleChange(row, e.target.value)}
                          >
                            {ROLE_OPTIONS.map((opt) => (
                              <MenuItem key={opt.id} value={opt.id}>
                                {opt.label}
                              </MenuItem>
                            ))}
                          </Select>
                        </FormControl>
                      </TableCell>
                      <TableCell sx={COMPACT_CELL}>{formatLoginAt(row.last_login_at)}</TableCell>
                      <TableCell sx={COMPACT_CELL}>
                        <Switch
                          checked={row.is_active}
                          disabled={isSelf}
                          onChange={(e) => handleActiveChange(row, e.target.checked)}
                          size="small"
                        />
                        {!row.is_active && (
                          <Chip size="small" label="выкл" color="warning" sx={{ ml: 0.5, height: 20, fontSize: "0.7rem" }} />
                        )}
                      </TableCell>
                      <TableCell align="right" sx={COMPACT_CELL}>
                        <Button
                          size="small"
                          startIcon={<BadgeOutlinedIcon fontSize="small" />}
                          sx={{ minWidth: 0, px: 1, fontSize: "0.8125rem" }}
                          onClick={() => setProfileUserId(row.id)}
                        >
                          Профиль
                        </Button>
                      </TableCell>
                      <TableCell align="right" sx={COMPACT_CELL}>
                        <Button size="small" sx={{ minWidth: 0, px: 1, fontSize: "0.8125rem" }} onClick={() => openPasswordDialog(row)}>
                          Сменить
                        </Button>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <UserProfileDialog
        open={profileUserId != null}
        userId={profileUserId}
        onClose={() => setProfileUserId(null)}
        onSaved={load}
      />

      <Dialog open={Boolean(passwordDialog)} onClose={() => setPasswordDialog(null)} maxWidth="xs" fullWidth>
        <DialogTitle sx={{ py: 1.5, fontSize: "1rem" }}>Новый пароль</DialogTitle>
        <DialogContent dividers sx={{ py: 1.5 }}>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5, fontSize: "0.8125rem" }}>
            {passwordDialog?.email}
          </Typography>
          {passwordError && (
            <Alert severity="error" sx={{ mb: 1.5 }}>
              {passwordError}
            </Alert>
          )}
          <TextField
            autoFocus
            fullWidth
            size="small"
            type="password"
            label="Пароль"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            helperText="Минимум 8 символов"
            FormHelperTextProps={{ sx: { fontSize: "0.7rem" } }}
          />
        </DialogContent>
        <DialogActions sx={{ py: 1 }}>
          <Button size="small" onClick={() => setPasswordDialog(null)}>
            Отмена
          </Button>
          <Button
            size="small"
            variant="contained"
            disabled={savingPassword || newPassword.length < 8}
            onClick={handlePasswordSave}
          >
            Сохранить
          </Button>
        </DialogActions>
      </Dialog>
    </Stack>
  );
}
