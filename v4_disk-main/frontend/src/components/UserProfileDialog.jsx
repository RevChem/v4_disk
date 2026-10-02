import { Fragment, useCallback, useEffect, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Collapse,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  IconButton,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import ExpandMoreRoundedIcon from "@mui/icons-material/ExpandMoreRounded";

import { fetchMyProfile, fetchUserProfile, updateManagedUser } from "../api";
import ChangePasswordSection from "./ChangePasswordSection";
import { formatDuration } from "../utils/formatDuration";

const ROLE_LABELS = { developer: "Разработчик", admin: "Админ", user: "Пользователь" };

const JOB_STATUS = {
  completed: { label: "готово", color: "success" },
  running: { label: "выполняется", color: "info" },
  pending: { label: "в очереди", color: "default" },
  failed: { label: "ошибка", color: "error" },
  cancelled: { label: "отменено", color: "warning" },
};

function formatDateTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatIsoDate(iso) {
  if (!iso) return "—";
  const [y, m, d] = String(iso).split("-");
  return y && m && d ? `${d}.${m}.${y}` : iso;
}

const AREA_COLORS = {
  "Редактор планов": "primary",
  "Правки ячеек": "secondary",
  "Комментарии": "info",
  "Порядок страниц": "success",
  "Подписи PDF": "warning",
  "Расписание": "default",
};

function ChangesList({ changes }) {
  const [area, setArea] = useState("");
  const areas = Array.from(new Set(changes.items.map((c) => c.area)));
  const items = area ? changes.items.filter((c) => c.area === area) : changes.items;

  return (
    <Stack spacing={0.75}>
      {areas.length > 1 && (
        <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
          <Chip
            size="small"
            label="Все"
            color={area === "" ? "primary" : "default"}
            variant={area === "" ? "filled" : "outlined"}
            onClick={() => setArea("")}
            sx={{ height: 22, fontSize: "0.7rem" }}
          />
          {areas.map((a) => (
            <Chip
              key={a}
              size="small"
              label={a}
              color={area === a ? AREA_COLORS[a] || "primary" : "default"}
              variant={area === a ? "filled" : "outlined"}
              onClick={() => setArea(area === a ? "" : a)}
              sx={{ height: 22, fontSize: "0.7rem" }}
            />
          ))}
        </Stack>
      )}
      <Box
        sx={{
          maxHeight: 280,
          overflowY: "auto",
          border: 1,
          borderColor: "divider",
          borderRadius: 1.5,
          px: 1.25,
          py: 0.5,
        }}
      >
        {items.map((c) => (
          <Stack
            key={c.id}
            direction="row"
            spacing={1.25}
            alignItems="flex-start"
            sx={{ fontSize: "0.8125rem", py: 0.5, "& + &": { borderTop: 1, borderColor: "divider" } }}
          >
            <Box sx={{ width: 118, flexShrink: 0, color: "text.secondary" }}>
              {formatDateTime(c.at)}
            </Box>
            <Chip
              size="small"
              label={c.area}
              color={AREA_COLORS[c.area] || "default"}
              variant="outlined"
              sx={{ height: 20, fontSize: "0.68rem", flexShrink: 0 }}
            />
            <Box sx={{ minWidth: 0, wordBreak: "break-word" }}>{c.text}</Box>
          </Stack>
        ))}
        {changes.total > changes.items.length && !area && (
          <Typography variant="caption" color="text.secondary">
            Показаны последние {changes.items.length} из {changes.total}
          </Typography>
        )}
      </Box>
    </Stack>
  );
}

function Section({ title, hint, children }) {
  return (
    <Box>
      <Stack direction="row" spacing={1} alignItems="baseline" sx={{ mb: 0.75 }}>
        <Typography variant="subtitle2" fontWeight={700}>
          {title}
        </Typography>
        {hint && (
          <Typography variant="caption" color="text.secondary">
            {hint}
          </Typography>
        )}
      </Stack>
      {children}
    </Box>
  );
}

function InfoRow({ label, children }) {
  return (
    <Stack direction="row" spacing={1} sx={{ fontSize: "0.8125rem" }}>
      <Box sx={{ width: 130, flexShrink: 0, color: "text.secondary" }}>{label}</Box>
      <Box sx={{ minWidth: 0, wordBreak: "break-word" }}>{children}</Box>
    </Stack>
  );
}

/** Номера сводок задания: «1, 2, 3, …, 24»; сводки с ошибкой — красным. */
function NumbersInline({ reports }) {
  return (
    <>
      {reports.map((r, i) => (
        <Fragment key={r.report_id}>
          <Box
            component="span"
            sx={r.status === "error" ? { color: "error.main", fontWeight: 700 } : undefined}
          >
            {r.report_id}
          </Box>
          {i < reports.length - 1 ? ", " : ""}
        </Fragment>
      ))}
    </>
  );
}

function reportStatusText(status) {
  if (status === "ok") return "готово";
  if (status === "error") return "ошибка";
  return JOB_STATUS[status]?.label || status;
}

function JobCard({ job }) {
  const [open, setOpen] = useState(false);
  const st = JOB_STATUS[job.status] || { label: job.status, color: "default" };
  const duration = formatDuration(job.total_duration_seconds);
  const hasReports = (job.reports || []).length > 0;

  return (
    <Box sx={{ border: 1, borderColor: "divider", borderRadius: 1.5, px: 1.25, py: 0.75 }}>
      <Stack direction="row" spacing={1} alignItems="flex-start">
        <Box sx={{ flex: 1, minWidth: 0, fontSize: "0.8125rem", color: "text.secondary" }}>
          {formatDateTime(job.launched_at)} · сводки за {formatIsoDate(job.summary_date)}
          {" · "}
          {hasReports ? <NumbersInline reports={job.reports} /> : "—"}
          {duration ? ` (${duration})` : ""}
        </Box>
        <Chip
          size="small"
          label={job.failed_count > 0 ? `${st.label}, ошибок: ${job.failed_count}` : st.label}
          color={job.failed_count > 0 && job.status === "completed" ? "warning" : st.color}
          variant="outlined"
          sx={{ height: 22, fontSize: "0.7rem" }}
        />
        {hasReports && (
          <IconButton
            size="small"
            aria-label={open ? "Свернуть" : "Время по каждой сводке"}
            onClick={() => setOpen((v) => !v)}
            sx={{
              p: 0.25,
              transition: "transform .15s",
              transform: open ? "rotate(180deg)" : "none",
            }}
          >
            <ExpandMoreRoundedIcon fontSize="small" />
          </IconButton>
        )}
      </Stack>
      <Collapse in={open} unmountOnExit>
        <Stack spacing={0.25} sx={{ mt: 0.75, pt: 0.75, borderTop: 1, borderColor: "divider" }}>
          {job.reports.map((r) => (
            <Stack
              key={r.report_id}
              direction="row"
              spacing={1}
              sx={{ fontSize: "0.78rem" }}
            >
              <Box sx={{ width: 34, flexShrink: 0, fontWeight: 600 }}>{r.report_id}</Box>
              <Box sx={{ flex: 1, minWidth: 0 }}>{r.title}</Box>
              <Box
                sx={{
                  flexShrink: 0,
                  color: r.status === "error" ? "error.main" : "text.secondary",
                }}
              >
                {r.status === "ok" && r.duration_seconds != null
                  ? formatDuration(r.duration_seconds)
                  : reportStatusText(r.status)}
              </Box>
            </Stack>
          ))}
        </Stack>
      </Collapse>
    </Box>
  );
}

/**
 * Профиль пользователя.
 * self — свой профиль (данные из /users/me/profile, ФИО только для чтения);
 * иначе — профиль userId для разработчика (ФИО можно менять).
 */
export default function UserProfileDialog({
  open,
  userId,
  onClose,
  onSaved,
  self = false,
  onPasswordChanged,
}) {
  const [profile, setProfile] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [fullName, setFullName] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  const load = useCallback(async () => {
    if (!self && !userId) return;
    setLoading(true);
    setError("");
    try {
      const data = self ? await fetchMyProfile() : await fetchUserProfile(userId);
      setProfile(data);
      setFullName(data.user.full_name || "");
    } catch (err) {
      setError(err?.response?.data?.detail || err?.message || String(err));
    } finally {
      setLoading(false);
    }
  }, [userId, self]);

  useEffect(() => {
    if (open) {
      setProfile(null);
      setSaved(false);
      load();
    }
  }, [open, load]);

  async function handleSave() {
    setSaving(true);
    setError("");
    setSaved(false);
    try {
      await updateManagedUser(userId, { full_name: fullName.trim() });
      setSaved(true);
      await load();
      onSaved?.();
    } catch (err) {
      setError(err?.response?.data?.detail || err?.message || String(err));
    } finally {
      setSaving(false);
    }
  }

  const user = profile?.user;
  const logins = profile?.logins;
  const gens = profile?.generations;
  const changes = profile?.changes;
  const dirty = user ? fullName.trim() !== (user.full_name || "") : false;

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth scroll="paper">
      <DialogTitle sx={{ py: 1.5, fontSize: "1rem" }}>
        {self ? "Мой профиль" : "Профиль"}
        {user ? ` · ${user.full_name || user.username}` : ""}
      </DialogTitle>
      <DialogContent dividers sx={{ py: 1.5 }}>
        {error && (
          <Alert severity="error" sx={{ mb: 1.5 }}>
            {error}
          </Alert>
        )}
        {loading && !profile && (
          <Box sx={{ p: 3, display: "flex", justifyContent: "center" }}>
            <CircularProgress size={28} />
          </Box>
        )}
        {user && (
          <Stack spacing={2.5}>
            <Section title="Основное">
              <Stack spacing={1.25}>
                {self ? (
                  <InfoRow label="ФИО">
                    {user.full_name || "не указано (укажет разработчик)"}
                  </InfoRow>
                ) : (
                <Stack direction={{ xs: "column", sm: "row" }} spacing={1}>
                  <TextField
                    fullWidth
                    size="small"
                    label="ФИО"
                    placeholder="Иванов Иван Иванович"
                    value={fullName}
                    onChange={(e) => {
                      setFullName(e.target.value);
                      setSaved(false);
                    }}
                    inputProps={{ maxLength: 255 }}
                  />
                  <Button
                    variant="contained"
                    size="small"
                    onClick={handleSave}
                    disabled={saving || !dirty}
                    sx={{ flexShrink: 0, minWidth: 110 }}
                  >
                    {saving ? <CircularProgress size={16} color="inherit" /> : "Сохранить"}
                  </Button>
                </Stack>
                )}
                {!self && saved && !dirty && (
                  <Typography variant="caption" color="success.main">
                    ФИО сохранено
                  </Typography>
                )}
                <Stack spacing={0.25}>
                  <InfoRow label="Email">{user.email || "—"}</InfoRow>
                  <InfoRow label="Логин">{user.username}</InfoRow>
                  <InfoRow label="Роль">{ROLE_LABELS[user.role] || user.role}</InfoRow>
                  <InfoRow label="Статус">{user.is_active ? "активен" : "отключён"}</InfoRow>
                  <InfoRow label="Создан">{formatDateTime(user.created_at)}</InfoRow>
                </Stack>
              </Stack>
            </Section>

            {self && (
              <Section title="Пароль">
                <ChangePasswordSection compact onChanged={onPasswordChanged} />
              </Section>
            )}

            <Section
              title="Входы в систему"
              hint={
                logins.total > 0
                  ? `всего ${logins.total}, последний — ${formatDateTime(logins.last_at)}`
                  : ""
              }
            >
              {logins.items.length === 0 ? (
                <Typography variant="body2" color="text.secondary">
                  Входов пока не было.
                </Typography>
              ) : (
                <Box
                  sx={{
                    maxHeight: 190,
                    overflowY: "auto",
                    border: 1,
                    borderColor: "divider",
                    borderRadius: 1.5,
                    px: 1.25,
                    py: 0.5,
                  }}
                >
                  {logins.items.map((l, i) => (
                    <Stack
                      key={`${l.at}-${i}`}
                      direction="row"
                      spacing={2}
                      sx={{ fontSize: "0.8125rem", py: 0.25 }}
                    >
                      <Box sx={{ width: 140 }}>{formatDateTime(l.at)}</Box>
                      <Box sx={{ color: "text.secondary" }}>{l.ip || ""}</Box>
                    </Stack>
                  ))}
                  {logins.total > logins.items.length && (
                    <Typography variant="caption" color="text.secondary">
                      Показаны последние {logins.items.length} из {logins.total}
                    </Typography>
                  )}
                </Box>
              )}
            </Section>

            <Section
              title="Запуски сводок"
              hint={
                gens.total > 0
                  ? `всего заданий: ${gens.total}. Стрелка — время по каждой сводке`
                  : ""
              }
            >
              {gens.items.length === 0 ? (
                <Typography variant="body2" color="text.secondary">
                  Сводки этим пользователем не запускались.
                </Typography>
              ) : (
                <Stack spacing={0.75}>
                  {gens.items.map((job) => (
                    <JobCard key={job.job_id} job={job} />
                  ))}
                  {gens.total > gens.items.length && (
                    <Typography variant="caption" color="text.secondary">
                      Показаны последние {gens.items.length} из {gens.total}
                    </Typography>
                  )}
                </Stack>
              )}
            </Section>

            <Section
              title="Внесённые изменения"
              hint={
                changes?.total > 0
                  ? `всего ${changes.total}: планы, правки ячеек, комментарии, порядок страниц`
                  : ""
              }
            >
              {!changes || changes.items.length === 0 ? (
                <Typography variant="body2" color="text.secondary">
                  Изменений пока не вносилось.
                </Typography>
              ) : (
                <ChangesList changes={changes} />
              )}
            </Section>
          </Stack>
        )}
      </DialogContent>
      <DialogActions sx={{ py: 1 }}>
        <Button size="small" onClick={onClose}>
          Закрыть
        </Button>
      </DialogActions>
    </Dialog>
  );
}
