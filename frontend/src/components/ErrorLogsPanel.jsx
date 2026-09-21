import {
  Alert,
  Box,
  Stack,
  Typography,
} from "@mui/material";

function filterErrorLines(lines) {
  if (!lines?.length) return [];
  const re = /error|ошибк|failed|exception|traceback|fatal|blank|UI error/i;
  return lines.filter((line) => re.test(String(line)));
}

export default function ErrorLogsPanel({ proverkaLogs = [], generationLogs = [], failedItems = [] }) {
  const genErrors = filterErrorLines(generationLogs);
  const proverka = proverkaLogs || [];
  const failed = failedItems || [];
  const total = proverka.length + genErrors.length + failed.length;

  if (total === 0) {
    return <Alert severity="success">Ошибок нет.</Alert>;
  }

  return (
    <Stack spacing={2}>
      {failed.map((item, idx) => (
        <Alert key={`f-${item.report_id}-${idx}`} severity="error" variant="outlined">
          <Typography variant="subtitle2" sx={{ fontWeight: 600 }}>
            Генерация · сводка {item.report_id}
            {item.title ? ` — ${item.title}` : ""}
          </Typography>
          <Typography variant="body2" sx={{ mt: 0.5, whiteSpace: "pre-wrap" }}>
            {item.error || "Неизвестная ошибка"}
          </Typography>
        </Alert>
      ))}

      {proverka.map((entry, idx) => (
        <Alert key={`p-${idx}`} severity="error" variant="outlined">
          <Typography variant="subtitle2" sx={{ fontWeight: 600 }}>
            Проверка · {entry.source}
          </Typography>
          <Typography variant="body2" sx={{ mt: 0.5, whiteSpace: "pre-wrap" }}>
            {entry.message}
          </Typography>
        </Alert>
      ))}

      {genErrors.length > 0 && (
        <Box>
          <Typography variant="subtitle2" sx={{ mb: 1 }}>
            Генерация (фрагменты лога)
          </Typography>
          <Box
            component="pre"
            sx={{
              m: 0,
              p: 1.5,
              borderRadius: 1,
              bgcolor: "action.hover",
              fontSize: 12,
              whiteSpace: "pre-wrap",
              maxHeight: 360,
              overflow: "auto",
            }}
          >
            {genErrors.join("\n")}
          </Box>
        </Box>
      )}
    </Stack>
  );
}
