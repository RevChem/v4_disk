import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Pagination,
  Stack,
  TextField,
  Typography,
} from "@mui/material";

import { fetchSummaryHistory } from "../api";

function formatWhen(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("ru-RU", {
      day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

/** История правок ячеек и комментариев одного документа. */
export default function DocumentHistoryDialog({ open, onClose, summaryId, title = "" }) {
  const [data, setData] = useState({ items: [], total: 0, pages: 1, page: 1 });
  const [page, setPage] = useState(1);
  const [searchInput, setSearchInput] = useState("");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const seq = useRef(0);

  const load = useCallback(async () => {
    if (!summaryId) return;
    const my = ++seq.current;
    setLoading(true);
    setError("");
    try {
      const res = await fetchSummaryHistory(summaryId, { page, query });
      if (my !== seq.current) return;
      setData(res);
      if (res.page && res.page !== page) setPage(res.page);
    } catch (e) {
      if (my === seq.current) setError(String(e?.response?.data?.detail || e?.message || e));
    } finally {
      if (my === seq.current) setLoading(false);
    }
  }, [summaryId, page, query]);

  useEffect(() => {
    if (open) load();
  }, [open, load]);

  useEffect(() => {
    const next = searchInput.trim();
    if (next === query) return undefined;
    const timer = setTimeout(() => {
      setQuery(next);
      setPage(1);
    }, 400);
    return () => clearTimeout(timer);
  }, [searchInput, query]);

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="sm">
      <DialogTitle>
        История правок документа
        {title ? (
          <Typography variant="body2" color="text.secondary">
            {title}
          </Typography>
        ) : null}
      </DialogTitle>
      <DialogContent dividers>
        <Stack spacing={1.5}>
          <TextField
            size="small"
            label="Поиск"
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
          />
          {error && <Alert severity="error">{error}</Alert>}
          {loading && <CircularProgress size={20} />}
          {!loading && !error && data.items.length === 0 && (
            <Typography variant="body2" color="text.secondary">
              {query ? "Ничего не найдено." : "Правок в этом документе пока не было."}
            </Typography>
          )}
          {data.items.map((item) => (
            <Box key={item.id} sx={{ py: 0.5, borderBottom: 1, borderColor: "divider" }}>
              <Typography variant="caption" color="text.secondary">
                {formatWhen(item.created_at)} · {item.username}
              </Typography>
              <Typography variant="body2">{item.text}</Typography>
            </Box>
          ))}
          {data.pages > 1 && (
            <Pagination
              count={data.pages}
              page={data.page}
              onChange={(_, v) => setPage(v)}
              disabled={loading}
              size="small"
              color="primary"
            />
          )}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={load} disabled={loading}>Обновить</Button>
        <Button onClick={onClose}>Закрыть</Button>
      </DialogActions>
    </Dialog>
  );
}
