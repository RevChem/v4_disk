import { useState } from "react";
import {
  Button,
  Chip,
  Drawer,
  IconButton,
  Stack,
  Typography,
} from "@mui/material";
import CloseRoundedIcon from "@mui/icons-material/CloseRounded";

const STATUS_LABELS = {
  pending: "В очереди",
  running: "Выполняется",
  completed: "Готово",
  failed: "Ошибка",
  cancelled: "Отменено",
};

const STATUS_COLORS = {
  pending: "default",
  running: "warning",
  completed: "success",
  failed: "error",
  cancelled: "default",
};

function formatDateTime(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("ru-RU");
  } catch {
    return iso;
  }
}

function statusLabel(status, cancelRequested) {
  if (cancelRequested && (status === "pending" || status === "running")) {
    return "Отмена…";
  }
  return STATUS_LABELS[status] || status;
}

export default function GenerationQueueDrawer({ open, onClose, jobs = [], onCancelJob }) {
  const [cancellingId, setCancellingId] = useState(null);

  async function handleCancel(job, reportIds = null) {
    if (!onCancelJob) return;
    setCancellingId(job.id);
    try {
      await onCancelJob(job.id, reportIds);
    } finally {
      setCancellingId(null);
    }
  }

  return (
    <Drawer
      anchor="right"
      open={open}
      onClose={onClose}
      PaperProps={{ sx: { width: { xs: "100%", sm: 460 }, p: 2 } }}
    >
      <Stack spacing={1.5} sx={{ height: "100%" }}>
        <Stack direction="row" justifyContent="space-between" alignItems="center">
          <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>
            Очередь генерации
          </Typography>
          <IconButton size="small" onClick={onClose} aria-label="Закрыть">
            <CloseRoundedIcon fontSize="small" />
          </IconButton>
        </Stack>
        {jobs.length === 0 ? (
          <Typography variant="body2" color="text.secondary">
            Очередь пуста.
          </Typography>
        ) : (
          <Stack spacing={1}>
            {jobs.map((job) => (
              <Stack
                key={job.id}
                spacing={0.8}
                sx={{ py: 1, borderBottom: "1px dashed", borderColor: "divider" }}
              >
                <Typography variant="body2">
                  {job.summary_date} ·{" "}
                  {job.report_ids?.length ? `Сводки: ${job.report_ids.join(", ")}` : "Все сводки"}
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  {job.requested_by || "—"} · {formatDateTime(job.created_at)}
                </Typography>
                <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
                  <Chip
                    size="small"
                    color={STATUS_COLORS[job.status] || "default"}
                    label={statusLabel(job.status, job.cancel_requested)}
                  />
                  {(job.status === "completed" || job.status === "failed" || job.status === "cancelled") && (
                    <Typography variant="caption" color="text.secondary">
                      OK: {job.created_count || 0}, ошибок: {job.failed_count || 0}
                      {job.cancelled?.length ? `, отменено: ${job.cancelled.length}` : ""}
                    </Typography>
                  )}
                  {job.can_cancel && onCancelJob && (
                    <Button
                      size="small"
                      variant="outlined"
                      color="warning"
                      onClick={() => handleCancel(job)}
                      disabled={cancellingId === job.id}
                    >
                      {job.cancel_requested ? "Отмена…" : "Отменить всё"}
                    </Button>
                  )}
                </Stack>
                {job.can_cancel && job.report_ids?.length > 1 && onCancelJob && (
                  <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
                    {job.report_ids.map((rid) => {
                      const isDone = (job.completed_report_ids || []).includes(rid);
                      return (
                        <Button
                          key={rid}
                          size="small"
                          variant="text"
                          color={isDone ? "success" : "warning"}
                          sx={{ minWidth: 0, px: 0.75 }}
                          onClick={() => handleCancel(job, [rid])}
                          disabled={cancellingId === job.id || isDone}
                          title={
                            isDone
                              ? `Сводка ${rid} готова в этой задаче`
                              : `Отменить сводку ${rid}`
                          }
                        >
                          {isDone ? `✓ ${rid}` : `✕ ${rid}`}
                        </Button>
                      );
                    })}
                  </Stack>
                )}
                {job.error_message && (
                  <Typography variant="caption" color="error" sx={{ whiteSpace: "pre-wrap" }}>
                    {job.error_message}
                  </Typography>
                )}
              </Stack>
            ))}
          </Stack>
        )}
      </Stack>
    </Drawer>
  );
}
