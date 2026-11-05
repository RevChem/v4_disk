import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Autocomplete,
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogContent,
  DialogTitle,
  Stack,
  TextField,
  Typography,
} from "@mui/material";

import {
  fetchFieldSeasonState,
  fetchFieldSeasonTable,
  generateFieldSeasonSummary,
  getFieldSeasonExcelUrl,
  getFieldSeasonPdfUrl,
} from "../api";

const FIELD_SEASON_ALL_OPTION = Object.freeze({
  field_id: "all",
  field_name: "Общая (все поля)",
});

const FIELD_SEASON_PAGE_SIZE = 250;

function fieldSeasonStatePayloadEqual(prev, next) {
  const p = prev || {};
  const n = next || {};
  if (!!p.exists !== !!n.exists) return false;
  if (!!p.running !== !!n.running) return false;
  if (String(p.season_year ?? "") !== String(n.season_year ?? "")) return false;
  if (String(p.created_at ?? "") !== String(n.created_at ?? "")) return false;
  if (String(p.error ?? "") !== String(n.error ?? "")) return false;
  if (JSON.stringify(p.warnings || []) !== JSON.stringify(n.warnings || [])) return false;
  return JSON.stringify(p.fields || []) === JSON.stringify(n.fields || []);
}

function formatApiError(err, fallback) {
  const d = err?.response?.data?.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((x) => x.msg || JSON.stringify(x)).join("; ");
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

export default function FieldSeasonDialog({ open, onClose, accessToken = "", onError, onInfo }) {
  const [state, setState] = useState({ exists: false, fields: [] });
  const [selectedId, setSelectedId] = useState("all");
  const [selectedIds, setSelectedIds] = useState([]);
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [nextOffset, setNextOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const [fieldComboInput, setFieldComboInput] = useState("");
  const [multiComboInput, setMultiComboInput] = useState("");

  const nextOffsetRef = useRef(0);
  nextOffsetRef.current = nextOffset;

  const singleOptions = useMemo(
    () => [FIELD_SEASON_ALL_OPTION, ...(state?.fields || [])],
    [state?.fields]
  );

  const loadState = useCallback(async () => {
    try {
      const data = await fetchFieldSeasonState();
      const nextState = data || { exists: false, fields: [] };
      setState((prev) => (fieldSeasonStatePayloadEqual(prev, nextState) ? prev : nextState));
      if (data?.error) {
        onError?.(String(data.error));
      }
      const fields = Array.isArray(data?.fields) ? data.fields : [];
      if (!fields.length) {
        setSelectedId("all");
        setSelectedIds([]);
        setRows([]);
        setHasMore(false);
        setNextOffset(0);
        setTotal(0);
        return;
      }
      setSelectedId((prev) => {
        if (String(prev) === "all") return "all";
        if (prev && fields.some((f) => String(f.field_id) === String(prev))) return prev;
        return "all";
      });
    } catch {
      setState({ exists: false, fields: [] });
      setSelectedId("all");
      setSelectedIds([]);
      setRows([]);
      setHasMore(false);
      setNextOffset(0);
      setTotal(0);
    }
  }, [onError]);

  const loadTablePage = useCallback(async (fieldId, append = false) => {
    const offset = append ? nextOffsetRef.current : 0;
    const limit = FIELD_SEASON_PAGE_SIZE;
    setLoading(true);
    try {
      const data = await fetchFieldSeasonTable(fieldId, { offset, limit });
      const incomingRows = Array.isArray(data?.rows) ? data.rows : [];
      setRows((prev) => (append ? [...prev, ...incomingRows] : incomingRows));
      setHasMore(Boolean(data?.has_more));
      setNextOffset(Number(data?.next_offset || 0));
      setTotal(Number(data?.total || incomingRows.length));
    } catch {
      if (!append) {
        setRows([]);
      }
      setHasMore(false);
      setNextOffset(0);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }, []);

  const handleGenerate = useCallback(async () => {
    setGenerating(true);
    try {
      const next = await generateFieldSeasonSummary();
      setState(next || { exists: false, fields: [] });
      setSelectedId((prev) => (String(prev) === "all" || !prev ? "all" : prev));
      if (next?.running) {
        onInfo?.("Сводка по полям запущена, обновляем список полей...");
      } else if (next?.error) {
        onError?.(String(next.error));
      } else if (next?.exists) {
        onInfo?.("Сводка по полям сформирована.");
      } else {
        onInfo?.("Запрос на сбор сводки принят, ожидайте обновления...");
      }
    } catch (e) {
      onError?.(formatApiError(e, "Не удалось сформировать сводку по полям."));
    } finally {
      setGenerating(false);
    }
  }, [onError, onInfo]);

  useEffect(() => {
    if (!open) return;
    loadState();
  }, [open, loadState]);

  useEffect(() => {
    if (!open || !state?.running) return undefined;
    const id = window.setInterval(() => {
      loadState();
    }, 3000);
    return () => window.clearInterval(id);
  }, [open, state?.running, loadState]);

  useEffect(() => {
    if (!state?.exists) return;
    loadTablePage(selectedId, false);
  }, [selectedId, state?.exists, loadTablePage]);

  useEffect(() => {
    if (!open) return;
    const fields = state?.fields || [];
    const opt = [FIELD_SEASON_ALL_OPTION, ...fields].find(
      (item) => String(item.field_id) === String(selectedId)
    );
    if (opt) setFieldComboInput(String(opt.field_name || ""));
  }, [open, selectedId, state?.exists]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="lg">
      <DialogTitle>Сводка поля с начала сезона</DialogTitle>
      <DialogContent dividers>
        <Stack spacing={1.5}>
          {state?.warnings?.length ? (
            <Alert severity="warning">{state.warnings.join(" ")}</Alert>
          ) : null}
          <Stack direction="row" spacing={1} alignItems="center" justifyContent="space-between">
            <Typography variant="body2" color="text.secondary">
              {state?.running ? "Формирование сводки… · " : ""}
              {state?.exists
                ? `Сезон: ${state?.season_year || "—"} · Создано: ${state?.created_at || "—"}`
                : "Сводка ещё не создана"}
            </Typography>
            <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
              <Button size="small" variant="contained" onClick={handleGenerate} disabled={generating}>
                {generating ? "Запуск..." : state?.exists ? "Обновить" : "Создать"}
              </Button>
              <Button
                size="small"
                variant="outlined"
                href={getFieldSeasonPdfUrl(selectedId, accessToken, [])}
                target="_blank"
                rel="noreferrer"
                disabled={!selectedId || rows.length === 0}
              >
                {selectedId === "all" ? "PDF общая" : "PDF по полю"}
              </Button>
              <Button
                size="small"
                variant="outlined"
                href={getFieldSeasonExcelUrl(selectedId, accessToken, [])}
                target="_blank"
                rel="noreferrer"
                disabled={!selectedId || rows.length === 0}
              >
                {selectedId === "all" ? "Excel общая" : "Excel по полю"}
              </Button>
              <Button
                size="small"
                variant="outlined"
                href={getFieldSeasonPdfUrl("all", accessToken, selectedIds)}
                target="_blank"
                rel="noreferrer"
                disabled={selectedIds.length === 0}
              >
                PDF выбранные поля
              </Button>
              <Button
                size="small"
                variant="outlined"
                href={getFieldSeasonExcelUrl("all", accessToken, selectedIds)}
                target="_blank"
                rel="noreferrer"
                disabled={selectedIds.length === 0}
              >
                Excel выбранные поля
              </Button>
            </Stack>
          </Stack>
          <Autocomplete
            fullWidth
            size="small"
            disabled={!state?.exists}
            selectOnFocus={false}
            options={singleOptions}
            getOptionLabel={(option) => String(option?.field_name || "")}
            inputValue={fieldComboInput}
            onInputChange={(_, newInputValue, reason) => {
              if (reason === "input" || reason === "clear") {
                setFieldComboInput(newInputValue);
              }
            }}
            value={
              singleOptions.find((item) => String(item.field_id) === String(selectedId)) || null
            }
            onChange={(_, newValue) => {
              setSelectedId(String(newValue?.field_id || "all"));
              setFieldComboInput(newValue ? String(newValue.field_name || "") : "");
            }}
            isOptionEqualToValue={(option, value) => String(option.field_id) === String(value.field_id)}
            renderInput={(params) => <TextField {...params} label="Поле (можно вводить текст)" />}
          />
          <Autocomplete
            fullWidth
            size="small"
            multiple
            disabled={!state?.exists}
            options={state?.fields || []}
            inputValue={multiComboInput}
            onInputChange={(_, newInputValue, reason) => {
              if (reason === "input" || reason === "clear") {
                setMultiComboInput(newInputValue);
              }
            }}
            value={(state?.fields || []).filter((item) =>
              selectedIds.includes(Number(item.field_id))
            )}
            onChange={(_, newValues) => {
              const ids = (newValues || [])
                .map((v) => Number(v.field_id))
                .filter((x) => Number.isFinite(x) && x > 0);
              setSelectedIds(ids);
            }}
            getOptionLabel={(option) => String(option?.field_name || "")}
            isOptionEqualToValue={(option, value) => String(option.field_id) === String(value.field_id)}
            renderInput={(params) => (
              <TextField {...params} label="Поля для файла (можно выбрать несколько)" />
            )}
          />
          {loading ? (
            <Stack direction="row" spacing={1} alignItems="center">
              <CircularProgress size={16} />
              <Typography variant="body2" color="text.secondary">
                Загрузка таблицы...
              </Typography>
            </Stack>
          ) : rows.length > 0 ? (
            <Box>
              <Box
                sx={{
                  maxHeight: "60vh",
                  overflow: "auto",
                  border: 1,
                  borderColor: "divider",
                  borderRadius: 1,
                }}
              >
                <Box
                  component="table"
                  sx={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}
                >
                  <Box component="thead">
                    <Box component="tr">
                      {[
                        "Поле",
                        "Работа",
                        "Культура",
                        "Название ТМЦ",
                        "Урожайность",
                        "Факт внесения",
                        "Обраб. площадь",
                        "Площадь поля",
                        "Норма внесения",
                        "Дата начала",
                        "Дата окончания",
                      ].map((label, i) => (
                        <Box
                          component="th"
                          key={label}
                          sx={{
                            textAlign: i >= 4 && i <= 8 ? "right" : "left",
                            p: 0.75,
                            borderBottom: "1px solid",
                            borderColor: "divider",
                          }}
                        >
                          {label}
                        </Box>
                      ))}
                    </Box>
                  </Box>
                  <Box component="tbody">
                    {rows.map((row, idx) => (
                      <Box component="tr" key={`${row.field_id}-${idx}`}>
                        <Box
                          component="td"
                          sx={{ p: 0.75, borderBottom: "1px solid", borderColor: "divider" }}
                        >
                          {row.field_name || "—"}
                        </Box>
                        <Box
                          component="td"
                          sx={{ p: 0.75, borderBottom: "1px solid", borderColor: "divider" }}
                        >
                          {row.work_type}
                        </Box>
                        <Box
                          component="td"
                          sx={{ p: 0.75, borderBottom: "1px solid", borderColor: "divider" }}
                        >
                          {row.crop}
                        </Box>
                        <Box
                          component="td"
                          sx={{ p: 0.75, borderBottom: "1px solid", borderColor: "divider" }}
                        >
                          {row.material_name || "—"}
                        </Box>
                        <Box
                          component="td"
                          sx={{
                            p: 0.75,
                            textAlign: "right",
                            borderBottom: "1px solid",
                            borderColor: "divider",
                          }}
                        >
                          {Number(row.yield_value || 0).toFixed(2)}
                        </Box>
                        <Box
                          component="td"
                          sx={{
                            p: 0.75,
                            textAlign: "right",
                            borderBottom: "1px solid",
                            borderColor: "divider",
                          }}
                        >
                          {Number(row.fact_rate || 0).toFixed(2)}
                        </Box>
                        <Box
                          component="td"
                          sx={{
                            p: 0.75,
                            textAlign: "right",
                            borderBottom: "1px solid",
                            borderColor: "divider",
                          }}
                        >
                          {Number((row.covered_area ?? row.completed_area) || 0).toFixed(2)}
                        </Box>
                        <Box
                          component="td"
                          sx={{
                            p: 0.75,
                            textAlign: "right",
                            borderBottom: "1px solid",
                            borderColor: "divider",
                          }}
                        >
                          {Number(row.field_area || 0).toFixed(2)}
                        </Box>
                        <Box
                          component="td"
                          sx={{
                            p: 0.75,
                            textAlign: "right",
                            borderBottom: "1px solid",
                            borderColor: "divider",
                          }}
                        >
                          {Number(row.planned_rate || 0).toFixed(2)}
                        </Box>
                        <Box
                          component="td"
                          sx={{ p: 0.75, borderBottom: "1px solid", borderColor: "divider" }}
                        >
                          {row.date_start || "—"}
                        </Box>
                        <Box
                          component="td"
                          sx={{ p: 0.75, borderBottom: "1px solid", borderColor: "divider" }}
                        >
                          {row.date_end || "—"}
                        </Box>
                      </Box>
                    ))}
                  </Box>
                </Box>
              </Box>
              {(hasMore || (total > 0 && rows.length < total)) && (
                <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mt: 1 }}>
                  <Typography variant="caption" color="text.secondary">
                    Показано: {rows.length} из {total || rows.length}
                  </Typography>
                  <Button
                    size="small"
                    variant="outlined"
                    disabled={!hasMore || loading}
                    onClick={() => loadTablePage(selectedId, true)}
                  >
                    {loading ? "Загрузка..." : "Показать ещё"}
                  </Button>
                </Stack>
              )}
            </Box>
          ) : (
            <Typography variant="body2" color="text.secondary">
              {state?.exists
                ? "Для выбранного поля нет данных."
                : "Создайте сводку, чтобы выбрать поле."}
            </Typography>
          )}
        </Stack>
      </DialogContent>
    </Dialog>
  );
}
