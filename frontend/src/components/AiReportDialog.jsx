import { useCallback, useEffect, useState } from "react";
import {
  Box,
  Button,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  List,
  ListItemButton,
  ListItemText,
  Stack,
  Typography,
} from "@mui/material";

import AiReportPanel from "./AiReportPanel";
import { fetchProverkaAiReportByJob, fetchProverkaAiReportHistory } from "../api";

function formatWhen(iso) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

export default function AiReportDialog({
  open,
  onClose,
  summaryDate,
  aiData,
  loading,
  error,
  onRefresh,
  refreshing,
  onLoadReport,
}) {
  const [history, setHistory] = useState([]);
  const [selectedJobId, setSelectedJobId] = useState(null);
  const [historyLoading, setHistoryLoading] = useState(false);

  const loadHistory = useCallback(async () => {
    setHistoryLoading(true);
    try {
      const data = await fetchProverkaAiReportHistory(50);
      setHistory(data.items || []);
    } catch {
      setHistory([]);
    } finally {
      setHistoryLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) loadHistory();
  }, [open, loadHistory]);

  useEffect(() => {
    if (aiData?.job_id) setSelectedJobId(aiData.job_id);
  }, [aiData?.job_id]);

  async function selectHistoryItem(item) {
    setSelectedJobId(item.job_id);
    try {
      const data = await fetchProverkaAiReportByJob(item.job_id);
      onLoadReport?.(data);
    } catch (e) {
      onLoadReport?.({ error: String(e.message || e) });
    }
  }

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="lg" scroll="paper">
      <DialogTitle>Отчёт AI</DialogTitle>
      <DialogContent dividers sx={{ p: 0 }}>
        <Stack direction={{ xs: "column", md: "row" }} sx={{ minHeight: 420 }}>
          <Box
            sx={{
              width: { xs: "100%", md: 300 },
              borderRight: { md: 1 },
              borderBottom: { xs: 1, md: 0 },
              borderColor: "divider",
              maxHeight: { md: 520 },
              overflow: "auto",
            }}
          >
            <Box sx={{ px: 2, py: 1.5, borderBottom: 1, borderColor: "divider" }}>
              <Typography variant="subtitle2" fontWeight={700}>
                История отчётов
              </Typography>
              <Typography variant="caption" color="text.secondary">
                Сохранённые AI-разборы
              </Typography>
            </Box>
            {historyLoading && (
              <Typography variant="body2" color="text.secondary" sx={{ p: 2 }}>
                Загрузка…
              </Typography>
            )}
            {!historyLoading && history.length === 0 && (
              <Typography variant="body2" color="text.secondary" sx={{ p: 2 }}>
                Пока нет сохранённых отчётов. Запустите проверку с галочкой AI.
              </Typography>
            )}
            <List dense disablePadding>
              {history.map((item) => {
                const active = selectedJobId === item.job_id;
                return (
                  <ListItemButton
                    key={item.job_id}
                    selected={active}
                    onClick={() => selectHistoryItem(item)}
                    sx={{ alignItems: "flex-start", py: 1.25 }}
                  >
                    <ListItemText
                      primary={
                        <Stack direction="row" spacing={0.75} alignItems="center" flexWrap="wrap">
                          <Typography variant="body2" fontWeight={600}>
                            {item.report_date}
                          </Typography>
                          {item.ai_violation_count > 0 && (
                            <Chip size="small" color="error" label={item.ai_violation_count} />
                          )}
                        </Stack>
                      }
                      secondary={
                        <>
                          <Typography variant="caption" color="text.secondary" display="block">
                            {formatWhen(item.updated_at)}
                          </Typography>
                          {item.executive_summary && (
                            <Typography
                              variant="caption"
                              display="block"
                              sx={{
                                mt: 0.5,
                                display: "-webkit-box",
                                WebkitLineClamp: 2,
                                WebkitBoxOrient: "vertical",
                                overflow: "hidden",
                              }}
                            >
                              {item.executive_summary}
                            </Typography>
                          )}
                        </>
                      }
                    />
                  </ListItemButton>
                );
              })}
            </List>
          </Box>
          <Box sx={{ flex: 1, p: 2, minWidth: 0 }}>
            <Typography variant="caption" color="text.secondary" display="block" sx={{ mb: 1 }}>
              Дата на дашборде: {summaryDate}
            </Typography>
            <AiReportPanel aiData={aiData} loading={loading} error={error} />
          </Box>
        </Stack>
      </DialogContent>
      <DialogActions>
        {onRefresh && (
          <Button onClick={onRefresh} disabled={refreshing}>
            {refreshing ? "Запуск…" : "Обновить отчёт"}
          </Button>
        )}
        <Button onClick={onClose}>Закрыть</Button>
      </DialogActions>
    </Dialog>
  );
}
