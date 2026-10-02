import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Autocomplete,
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
  IconButton,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import AddRoundedIcon from "@mui/icons-material/AddRounded";
import CloseRoundedIcon from "@mui/icons-material/CloseRounded";
import DeleteOutlineRoundedIcon from "@mui/icons-material/DeleteOutlineRounded";
import EmailOutlinedIcon from "@mui/icons-material/EmailOutlined";
import PlayCircleOutlineRoundedIcon from "@mui/icons-material/PlayCircleOutlineRounded";
import ScheduleRoundedIcon from "@mui/icons-material/ScheduleRounded";

import { fetchReportCatalog, fetchSchedule, updateSchedule } from "../api";

function newId(prefix) {
  return `${prefix}-${Math.random().toString(36).slice(2, 10)}`;
}

function formatTime(hour, minute) {
  return `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
}

function toTimeValue(hour, minute) {
  return formatTime(hour ?? 0, minute ?? 0);
}

function fromTimeValue(value) {
  const [h, m] = String(value || "00:00").split(":");
  return {
    hour: Math.min(23, Math.max(0, Number(h) || 0)),
    minute: Math.min(59, Math.max(0, Number(m) || 0)),
  };
}

function parseEmails(text) {
  return String(text || "")
    .split(/[,;\s]+/)
    .map((x) => x.trim())
    .filter(Boolean);
}

function reportLabel(catalogById, id) {
  const item = catalogById[id];
  return item ? `${id}. ${item.title}` : id;
}

function reportShort(catalogById, id) {
  return catalogById[id]?.title || id;
}

function emptyTask() {
  return {
    id: newId("task"),
    label: "",
    description: "",
    enabled: true,
    hour: 8,
    minute: 0,
    report_ids: [],
    emails: [],
    subject: "",
  };
}

function configToTasks(generation, mail) {
  const mailById = Object.fromEntries((mail || []).map((m) => [m.id, m]));
  const usedMail = new Set();
  const tasks = (generation || []).map((g) => {
    const m = mailById[g.id];
    if (m) usedMail.add(m.id);
    return {
      id: g.id,
      label: g.label || "",
      description: g.description || "",
      enabled: g.enabled !== false,
      hour: g.hour ?? 8,
      minute: g.minute ?? 0,
      report_ids: g.report_ids || [],
      emails: m?.emails || [],
      subject: m?.subject || "",
    };
  });
  for (const m of mail || []) {
    if (usedMail.has(m.id)) continue;
    if (tasks.some((t) => t.id === m.id)) continue;
    tasks.push({
      id: m.id,
      label: m.label || "",
      description: m.description || "",
      enabled: m.enabled !== false,
      hour: m.hour ?? 9,
      minute: m.minute ?? 0,
      report_ids: m.report_ids || [],
      emails: m.emails || [],
      subject: m.subject || "",
    });
  }
  return tasks.sort(
    (a, b) =>
      formatTime(a.hour, a.minute).localeCompare(formatTime(b.hour, b.minute))
      || String(a.label).localeCompare(String(b.label))
  );
}

function tasksToConfig(tasks) {
  const generation = tasks.map((t) => ({
    id: t.id,
    label: t.label?.trim() || null,
    description: t.description?.trim() || null,
    enabled: Boolean(t.enabled),
    hour: t.hour,
    minute: t.minute,
    report_ids: t.report_ids || [],
  }));
  const mail = tasks
    .filter((t) => (t.emails || []).length > 0)
    .map((t) => ({
      id: t.id,
      label: t.label?.trim() || null,
      description: t.description?.trim() || null,
      enabled: Boolean(t.enabled),
      hour: t.hour,
      minute: t.minute,
      report_ids: t.report_ids || [],
      emails: t.emails || [],
      subject: t.subject?.trim() || null,
    }));
  return { generation, mail };
}

function tasksPersistable(taskList) {
  return taskList.every((t) => (t.report_ids || []).length > 0);
}

function tasksSignature(taskList) {
  return JSON.stringify(tasksToConfig(taskList));
}

function cardPreview(task, catalogById) {
  if (task.description?.trim()) return task.description.trim();
  const reports = (task.report_ids || []).map((id) => reportShort(catalogById, id)).join(", ");
  if (reports) return `Сводки: ${reports}`;
  return "Нажмите, чтобы настроить расписание";
}

function ReportPicker({ catalog, value, onChange, placeholder }) {
  const options = catalog.map((c) => c.id);
  const catalogById = Object.fromEntries(catalog.map((c) => [c.id, c]));
  return (
    <Autocomplete
      multiple
      size="small"
      options={options}
      value={value || []}
      onChange={(_, ids) => onChange(ids)}
      getOptionLabel={(id) => reportLabel(catalogById, id)}
      renderTags={(ids, getTagProps) =>
        ids.map((id, index) => (
          <Chip
            {...getTagProps({ index })}
            key={id}
            size="small"
            label={catalogById[id]?.title || id}
          />
        ))
      }
      renderInput={(params) => (
        <TextField {...params} placeholder={placeholder} label="Сводки" />
      )}
    />
  );
}

function ScheduleTaskCard({ task, catalogById, onClick }) {
  const enabled = Boolean(task.enabled);
  const hasMail = (task.emails || []).length > 0;
  const title = task.label?.trim() || "Без названия";
  const preview = cardPreview(task, catalogById);

  return (
    <Box
      component="button"
      type="button"
      className={`schedule-card${enabled ? "" : " schedule-card--off"}`}
      onClick={onClick}
    >
      <Stack spacing={1.25} sx={{ height: "100%", textAlign: "left" }}>
        <Stack direction="row" justifyContent="space-between" alignItems="flex-start" spacing={1}>
          <Chip
            size="small"
            icon={<ScheduleRoundedIcon sx={{ fontSize: "14px !important" }} />}
            label={formatTime(task.hour, task.minute)}
            className="schedule-card__time"
          />
          <Chip
            size="small"
            label={enabled ? "Вкл" : "Выкл"}
            className={enabled ? "schedule-card__badge-on" : "schedule-card__badge-off"}
          />
        </Stack>

        <Box sx={{ flex: 1 }}>
          <Typography variant="subtitle2" fontWeight={700} lineHeight={1.3} gutterBottom>
            {title}
          </Typography>
          <Typography
            variant="caption"
            color="text.secondary"
            sx={{
              display: "-webkit-box",
              WebkitLineClamp: 3,
              WebkitBoxOrient: "vertical",
              overflow: "hidden",
            }}
          >
            {preview}
          </Typography>
        </Box>

        <Stack direction="row" spacing={0.75} flexWrap="wrap" useFlexGap>
          <Chip
            size="small"
            variant="outlined"
            icon={<PlayCircleOutlineRoundedIcon sx={{ fontSize: "14px !important" }} />}
            label={`${(task.report_ids || []).length} св.`}
          />
          {hasMail && (
            <Chip
              size="small"
              variant="outlined"
              icon={<EmailOutlinedIcon sx={{ fontSize: "14px !important" }} />}
              label={`${task.emails.length} email`}
            />
          )}
        </Stack>
      </Stack>
    </Box>
  );
}

function ScheduleTaskDialog({ open, task, catalog, onClose, onChange, onDelete }) {
  const [draft, setDraft] = useState(task);
  const [localError, setLocalError] = useState("");

  useEffect(() => {
    if (open && task) {
      setDraft({ ...task });
      setLocalError("");
    }
  }, [open, task?.id]);

  if (!draft) return null;

  const enabled = Boolean(draft.enabled);

  function apply(next) {
    setLocalError("");
    if (!(next.report_ids || []).length) {
      setLocalError("Выберите хотя бы одну сводку.");
      return;
    }
    setDraft(next);
    onChange(next);
  }

  function patch(fields) {
    apply({ ...draft, ...fields });
  }

  function handleToggle() {
    apply({ ...draft, enabled: !enabled });
  }

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth className="schedule-dialog">
      <DialogTitle sx={{ pr: 6 }}>
        <Typography variant="h6" component="span" fontWeight={700}>
          {draft.label?.trim() || "Настройка расписания"}
        </Typography>
        <IconButton
          aria-label="Закрыть"
          onClick={onClose}
          sx={{ position: "absolute", right: 12, top: 12 }}
        >
          <CloseRoundedIcon />
        </IconButton>
      </DialogTitle>

      <DialogContent dividers>
        <Stack spacing={2.5}>
          {localError && (
            <Alert severity="warning" sx={{ py: 0.5 }}>
              {localError}
            </Alert>
          )}
          <Stack direction="row" alignItems="center" spacing={2}>
            <Button
              variant="contained"
              size="large"
              onClick={handleToggle}
              className={enabled ? "schedule-toggle schedule-toggle--off" : "schedule-toggle schedule-toggle--on"}
              sx={{ minWidth: 120, fontWeight: 700 }}
            >
              {enabled ? "Выкл" : "Вкл"}
            </Button>
            <Typography variant="body2" color="text.secondary">
              {enabled
                ? "Задача активна — запуск в указанное время (MSK)."
                : "Задача отключена — Celery её пропустит."}
            </Typography>
          </Stack>

          <TextField
            label="Название"
            placeholder="Обеденная сводка"
            value={draft.label || ""}
            onChange={(e) => patch({ label: e.target.value })}
            fullWidth
          />

          <TextField
            label="Описание"
            placeholder="Ежедневная выгрузка обеденной сводки…"
            value={draft.description || ""}
            onChange={(e) => patch({ description: e.target.value })}
            fullWidth
            multiline
            minRows={2}
          />

          <TextField
            label="Время (MSK)"
            type="time"
            value={toTimeValue(draft.hour, draft.minute)}
            onChange={(e) => patch(fromTimeValue(e.target.value))}
            inputProps={{ step: 60 }}
            fullWidth
            InputLabelProps={{ shrink: true }}
          />

          <ReportPicker
            catalog={catalog}
            value={draft.report_ids}
            onChange={(ids) => patch({ report_ids: ids })}
            placeholder="Выберите сводки…"
          />

          <TextField
            label="Email для рассылки"
            placeholder="a@example.com, b@example.com"
            value={(draft.emails || []).join(", ")}
            onChange={(e) => patch({ emails: parseEmails(e.target.value) })}
            fullWidth
            helperText="Необязательно. PDF должны быть готовы к указанному времени."
          />

          {(draft.emails || []).length > 0 && (
            <TextField
              label="Тема письма"
              placeholder="Сводки за …"
              value={draft.subject || ""}
              onChange={(e) => patch({ subject: e.target.value })}
              fullWidth
            />
          )}
        </Stack>
      </DialogContent>

      <DialogActions sx={{ px: 3, py: 2, justifyContent: "space-between" }}>
        <Button color="error" startIcon={<DeleteOutlineRoundedIcon />} onClick={onDelete}>
          Удалить
        </Button>
        <Button onClick={onClose}>Закрыть</Button>
      </DialogActions>
    </Dialog>
  );
}

function AddScheduleCard({ onClick }) {
  return (
    <Box component="button" type="button" className="schedule-card schedule-card--add" onClick={onClick}>
      <Stack alignItems="center" justifyContent="center" spacing={1} sx={{ height: "100%", color: "text.secondary" }}>
        <AddRoundedIcon sx={{ fontSize: 32, opacity: 0.7 }} />
        <Typography variant="body2" fontWeight={600}>
          Добавить задачу
        </Typography>
      </Stack>
    </Box>
  );
}

export default function SchedulePage() {
  const [tasks, setTasks] = useState([]);
  const [catalog, setCatalog] = useState([]);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(true);
  const [dialogTaskId, setDialogTaskId] = useState(null);
  const skipPersistRef = useRef(true);
  const saveTimerRef = useRef(null);
  const lastPersistedRef = useRef(null);

  const catalogById = useMemo(
    () => Object.fromEntries(catalog.map((c) => [c.id, c])),
    [catalog]
  );

  const dialogTask = useMemo(
    () => tasks.find((t) => t.id === dialogTaskId) || null,
    [tasks, dialogTaskId]
  );

  const persistTasks = useCallback(async (taskList) => {
    if (!tasksPersistable(taskList)) return;
    const sig = tasksSignature(taskList);
    if (sig === lastPersistedRef.current) return;
    setSaving(true);
    setSaved(false);
    setError("");
    try {
      const data = await updateSchedule(tasksToConfig(taskList));
      const merged = configToTasks(data.config?.generation || [], data.config?.mail || []);
      const mergedSig = tasksSignature(merged);
      skipPersistRef.current = true;
      setTasks(merged);
      lastPersistedRef.current = mergedSig;
      setSaved(true);
      skipPersistRef.current = false;
    } catch (e) {
      setError(String(e.message || e));
      setSaved(false);
    } finally {
      setSaving(false);
    }
  }, []);

  const load = useCallback(async () => {
    setError("");
    try {
      const [scheduleData, catalogData] = await Promise.all([fetchSchedule(), fetchReportCatalog()]);
      skipPersistRef.current = true;
      const loaded = configToTasks(scheduleData.config?.generation || [], scheduleData.config?.mail || []);
      setTasks(loaded);
      setCatalog(catalogData.items || []);
      lastPersistedRef.current = tasksSignature(loaded);
      setSaved(true);
      skipPersistRef.current = false;
    } catch (e) {
      setError(String(e.message || e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (skipPersistRef.current || !tasksPersistable(tasks)) return undefined;
    const sig = tasksSignature(tasks);
    if (sig === lastPersistedRef.current) {
      setSaved(true);
      return undefined;
    }
    setSaved(false);
    clearTimeout(saveTimerRef.current);
    saveTimerRef.current = setTimeout(() => {
      persistTasks(tasks);
    }, 600);
    return () => clearTimeout(saveTimerRef.current);
  }, [tasks, persistTasks]);

  function upsertTask(next) {
    setTasks((prev) => {
      const idx = prev.findIndex((t) => t.id === next.id);
      if (idx < 0) {
        return [...prev, next].sort((a, b) =>
          formatTime(a.hour, a.minute).localeCompare(formatTime(b.hour, b.minute))
        );
      }
      return prev.map((t, i) => (i === idx ? next : t));
    });
  }

  function removeTask(id) {
    setTasks((prev) => {
      const next = prev.filter((t) => t.id !== id);
      if (tasksPersistable(next)) {
        persistTasks(next);
      }
      return next;
    });
    setDialogTaskId(null);
  }

  const activeCount = tasks.filter((t) => t.enabled).length;

  return (
    <Card className="hero-card" elevation={0}>
      <CardContent sx={{ p: 2, "&:last-child": { pb: 2 } }}>
        <Stack spacing={2.5}>
          <Stack direction="row" justifyContent="space-between" alignItems="baseline" flexWrap="wrap" gap={1}>
            <Box>
              <Typography variant="subtitle1" fontWeight={600}>
                Расписание · MSK
              </Typography>
              <Typography variant="caption" color="text.secondary" component="div">
                {activeCount} из {tasks.length} задач включено
                {saving ? (
                  <>
                    {" · "}
                    <CircularProgress size={10} sx={{ verticalAlign: "middle", mr: 0.5 }} />
                    сохранение…
                  </>
                ) : saved && tasks.length > 0 ? (
                  <Box component="span" className="schedule-saved-badge">
                    {" · сохранено"}
                  </Box>
                ) : null}
              </Typography>
              <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 0.25 }}>
                Изменения сохраняются автоматически. Celery Beat сверяет время каждую минуту и запускает
                задачи ровно в указанный час:минуту.
              </Typography>
            </Box>
          </Stack>

          {error && (
            <Alert severity="error" sx={{ py: 0.5 }}>
              {error}
            </Alert>
          )}

          <Box className="schedule-grid">
            {tasks.map((task) => (
              <ScheduleTaskCard
                key={task.id}
                task={task}
                catalogById={catalogById}
                onClick={() => setDialogTaskId(task.id)}
              />
            ))}
            <AddScheduleCard
              onClick={() => {
                const t = emptyTask();
                setTasks((prev) => [...prev, t]);
                setDialogTaskId(t.id);
              }}
            />
          </Box>
        </Stack>
      </CardContent>

      <ScheduleTaskDialog
        open={Boolean(dialogTask)}
        task={dialogTask}
        catalog={catalog}
        onClose={() => setDialogTaskId(null)}
        onChange={upsertTask}
        onDelete={() => dialogTaskId && removeTask(dialogTaskId)}
      />
    </Card>
  );
}
