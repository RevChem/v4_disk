import {
  Alert,
  Box,
  Chip,
  CircularProgress,
  Link,
  Stack,
  Typography,
} from "@mui/material";

const SEV_COLOR = {
  critical: "error",
  warning: "warning",
  info: "info",
};

export default function AiReportPanel({
  aiData,
  loading = false,
  error = "",
  compact = false,
}) {
  const report = aiData?.ai_report || aiData;
  const status = aiData?.ai_status || "none";
  const findings = report?.findings || [];

  if (loading) {
    return (
      <Stack direction="row" spacing={1} alignItems="center">
        <CircularProgress size={22} />
        <Typography variant="body2">AI-разбор…</Typography>
      </Stack>
    );
  }

  return (
    <Stack spacing={2}>
      {error && <Alert severity="error">{error}</Alert>}
      {!error && status === "none" && (
        <Alert severity="info">
          AI-разбор не выполнялся. Включите галочку «AI-разбор (DeepSeek)» при запуске.
        </Alert>
      )}
      {!error && status === "running" && (
        <Alert severity="info">AI-разбор выполняется…</Alert>
      )}
      {!error && status === "failed" && (
        <Alert severity="error">
          AI-разбор завершился с ошибкой.
          {(aiData?.ai_error || report?.error)
            ? ` ${aiData?.ai_error || report?.error}`
            : " Проверьте DEEPSEEK_API_KEY в .env и логи worker."}
        </Alert>
      )}
      {report?.executive_summary && (
        <Box>
          {!compact && (
            <Typography variant="subtitle2" gutterBottom>
              Краткий вывод
            </Typography>
          )}
          <Typography variant="body2" sx={{ whiteSpace: "pre-wrap" }}>
            {report.executive_summary}
          </Typography>
        </Box>
      )}
      {findings.length > 0 && (
        <Box>
          <Typography variant="subtitle2" gutterBottom>
            Нарушения ({findings.length})
          </Typography>
          <Stack spacing={1.5}>
            {findings.map((f, idx) => (
              <Box
                key={idx}
                sx={{
                  p: 1.5,
                  border: 1,
                  borderColor: "divider",
                  borderRadius: 1,
                }}
              >
                <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
                  <Chip
                    size="small"
                    label={(f.severity || "info").toUpperCase()}
                    color={SEV_COLOR[f.severity] || "default"}
                  />
                  <Typography variant="subtitle2">{f.title}</Typography>
                </Stack>
                <Typography variant="body2" sx={{ whiteSpace: "pre-wrap" }}>
                  {f.summary}
                </Typography>
                {f.suggested_action && (
                  <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 0.5 }}>
                    Действие: {f.suggested_action}
                  </Typography>
                )}
                {(f.related_tasks || []).length > 0 && (
                  <Box sx={{ mt: 1 }}>
                    <Typography variant="caption" color="text.secondary">
                      Задания:
                    </Typography>
                    {(f.related_tasks || []).slice(0, 8).map((t, ti) => (
                      <Typography key={ti} variant="caption" display="block">
                        {t.task_url ? (
                          <Link href={t.task_url} target="_blank" rel="noopener noreferrer">
                            #{t.task_id} {t.gosnumber || ""} · {t.area_ha ?? "?"} га
                          </Link>
                        ) : (
                          `#${t.task_id} ${t.gosnumber || ""}`
                        )}
                      </Typography>
                    ))}
                  </Box>
                )}
              </Box>
            ))}
          </Stack>
        </Box>
      )}
      {report?.markdown && findings.length === 0 && status === "completed" && (
        <Typography variant="body2" sx={{ whiteSpace: "pre-wrap" }}>
          {report.markdown}
        </Typography>
      )}
    </Stack>
  );
}
