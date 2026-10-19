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
  FormControl,
  IconButton,
  InputAdornment,
  InputLabel,
  MenuItem,
  Pagination,
  Select,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import ClearRoundedIcon from "@mui/icons-material/ClearRounded";
import SearchRoundedIcon from "@mui/icons-material/SearchRounded";

import { fetchPlansEditorHistory } from "../api";

const PAGE_SIZE = 50;

function formatWhen(iso) {
  if (!iso) return "—";
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

function formatValue(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function HistoryRow({ item }) {
  const where = [item.section_label || item.kind, ...(item.keys || [])].join(" → ");
  return (
    <Box sx={{ py: 0.75, borderBottom: 1, borderColor: "divider" }}>
      <Typography variant="caption" color="text.secondary">
        {formatWhen(item.created_at)} · {item.username}
        {item.summary_date ? ` · сводка за ${item.summary_date}` : ""}
      </Typography>
      {item.has_values ? (
        <Typography variant="body2">
          {where}:{" "}
          <Box component="span" sx={{ color: "text.secondary" }}>
            {formatValue(item.old_value)}
          </Box>
          {" → "}
          <Box component="span" sx={{ fontWeight: 700 }}>
            {formatValue(item.new_value)}
          </Box>
        </Typography>
      ) : (
        <Typography variant="body2">{item.text}</Typography>
      )}
    </Box>
  );
}

export default function PlansHistoryDialog({ open, onClose }) {
  const [data, setData] = useState({ items: [], total: 0, pages: 1, page: 1, users: [], sections: [] });
  const [page, setPage] = useState(1);
  const [searchInput, setSearchInput] = useState("");
  const [query, setQuery] = useState("");
  const [section, setSection] = useState("");
  const [userId, setUserId] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const seq = useRef(0);

  const load = useCallback(async () => {
    const my = ++seq.current;
    setLoading(true);
    setError("");
    try {
      const res = await fetchPlansEditorHistory({
        page,
        pageSize: PAGE_SIZE,
        query,
        section,
        userId,
      });
      if (my !== seq.current) return;
      setData(res);
      if (res.page && res.page !== page) setPage(res.page);
    } catch (e) {
      if (my === seq.current) setError(String(e?.response?.data?.detail || e?.message || e));
    } finally {
      if (my === seq.current) setLoading(false);
    }
  }, [page, query, section, userId]);

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
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="md">
      <DialogTitle>История изменений планов</DialogTitle>
      <DialogContent dividers>
        <Stack spacing={1.5}>
          <Stack direction={{ xs: "column", sm: "row" }} spacing={1}>
            <TextField
              size="small"
              label="Поиск"
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              sx={{ flex: 1, minWidth: 200 }}
              slotProps={{
                input: {
                  startAdornment: (
                    <InputAdornment position="start">
                      <SearchRoundedIcon fontSize="small" />
                    </InputAdornment>
                  ),
                  endAdornment: searchInput ? (
                    <InputAdornment position="end">
                      <IconButton
                        size="small"
                        aria-label="Очистить поиск"
                        edge="end"
                        onClick={() => {
                          setSearchInput("");
                          setQuery("");
                          setPage(1);
                        }}
                      >
                        <ClearRoundedIcon fontSize="small" />
                      </IconButton>
                    </InputAdornment>
                  ) : null,
                },
              }}
            />
            <FormControl size="small" sx={{ minWidth: 190 }}>
              <InputLabel id="plans-history-section">Раздел</InputLabel>
              <Select
                labelId="plans-history-section"
                label="Раздел"
                value={section}
                onChange={(e) => {
                  setSection(e.target.value);
                  setPage(1);
                }}
              >
                <MenuItem value="">Все разделы</MenuItem>
                {(data.sections || []).map((s) => (
                  <MenuItem key={s.value} value={s.value}>
                    {s.label}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
            <FormControl size="small" sx={{ minWidth: 160 }}>
              <InputLabel id="plans-history-user">Пользователь</InputLabel>
              <Select
                labelId="plans-history-user"
                label="Пользователь"
                value={userId}
                onChange={(e) => {
                  setUserId(e.target.value);
                  setPage(1);
                }}
              >
                <MenuItem value="">Все</MenuItem>
                {(data.users || []).map((u) => (
                  <MenuItem key={u.id} value={String(u.id)}>
                    {u.username}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
          </Stack>

          {error && <Alert severity="error">{error}</Alert>}
          {loading && <CircularProgress size={20} />}
          {!loading && !error && data.items.length === 0 && (
            <Typography variant="body2" color="text.secondary">
              {query || section || userId ? "Ничего не найдено." : "Изменений пока не было."}
            </Typography>
          )}
          <Box>
            {data.items.map((item) => (
              <HistoryRow key={item.id} item={item} />
            ))}
          </Box>
          <Stack direction="row" justifyContent="space-between" alignItems="center">
            <Typography variant="body2" color="text.secondary">
              Всего записей: {data.total}
            </Typography>
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
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={load} disabled={loading}>
          Обновить
        </Button>
        <Button onClick={onClose}>Закрыть</Button>
      </DialogActions>
    </Dialog>
  );
}
