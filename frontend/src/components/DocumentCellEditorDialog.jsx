import { useCallback, useEffect, useRef, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import IconButton from "@mui/material/IconButton";
import List from "@mui/material/List";
import ListItem from "@mui/material/ListItem";
import ListItemText from "@mui/material/ListItemText";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import DeleteOutlineRoundedIcon from "@mui/icons-material/DeleteOutlineRounded";
import RefreshRoundedIcon from "@mui/icons-material/RefreshRounded";
import {
  deleteSummaryCellEdit,
  fetchSummaryCellEdits,
  getPreviewUrl,
  saveSummaryCellEdit,
  summaryIsExcel
} from "../api";
import { parsePdfCellsFromUrl } from "../utils/pdfCellParser";

/** Правка, относящаяся к этой ячейке: по ключу или по тому же месту на странице
 * (после замены в PDF у ячейки уже другой текст и другой ключ). */
function findEditForCell(edits, cell) {
  if (!cell) return null;
  const byKey = edits.find((row) => String(row.cell_key) === String(cell.cellKey));
  if (byKey) return byKey;
  const cx = Number(cell.leftPct) + Number(cell.widthPct) / 2;
  const cy = Number(cell.topPct) + Number(cell.heightPct) / 2;
  return (
    edits.find((row) => {
      if (Number(row.page) !== Number(cell.page)) return false;
      const l = Number(row.left);
      const t = Number(row.top);
      const w = Number(row.width);
      const h = Number(row.height);
      const tx = Math.max(0.003, w * 0.5);
      const ty = Math.max(0.002, h * 0.5);
      return cx >= l - tx && cx <= l + w + tx && cy >= t - ty && cy <= t + h + ty;
    }) || null
  );
}

export default function DocumentCellEditorDialog({
  open,
  onClose,
  summaryItem,
  accessToken = "",
  onSaved
}) {
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [pdfPages, setPdfPages] = useState([]);
  const [edits, setEdits] = useState([]);
  const [reportId, setReportId] = useState("");
  const [selectedCell, setSelectedCell] = useState(null);
  const [inputOpen, setInputOpen] = useState(false);
  const [replacementText, setReplacementText] = useState("");
  const loadTokenRef = useRef(0);
  const editingKeyRef = useRef("");
  const suppressBlurRef = useRef(false);

  const title = summaryItem?.title || "Сводка";

  const loadEditor = useCallback(async (silent = false) => {
    if (!summaryItem?.id) return;
    if (summaryIsExcel(summaryItem)) {
      setError("Правки ячеек доступны только для PDF.");
      setPdfPages([]);
      setEdits([]);
      return;
    }
    const loadToken = loadTokenRef.current + 1;
    loadTokenRef.current = loadToken;
    const isStale = () => loadTokenRef.current !== loadToken;

    if (!silent) {
      setLoading(true);
      setError("");
      setSuccess("");
    }
    try {
      const baseUrl = getPreviewUrl(summaryItem, accessToken);
      const previewUrl =
        !baseUrl || baseUrl === "#"
          ? baseUrl
          : `${baseUrl}${baseUrl.includes("?") ? "&" : "?"}ts=${Date.now()}`;
      const [serverState, pages] = await Promise.all([
        fetchSummaryCellEdits(summaryItem.id),
        parsePdfCellsFromUrl(previewUrl)
      ]);
      if (isStale()) return;
      setEdits(Array.isArray(serverState?.edits) ? serverState.edits : []);
      setReportId(String(serverState?.report_id || ""));
      setPdfPages(Array.isArray(pages) ? pages : []);
    } catch (e) {
      if (!isStale()) {
        setError(
          String(e?.response?.data?.detail || e?.message || "Не удалось загрузить редактор правок.")
        );
        setPdfPages([]);
        setEdits([]);
      }
    } finally {
      if (!isStale() && !silent) setLoading(false);
    }
  }, [summaryItem, accessToken]);

  useEffect(() => {
    if (!open) return;
    setSelectedCell(null);
    setInputOpen(false);
    setReplacementText("");
    loadEditor();
    return () => {
      loadTokenRef.current += 1;
    };
  }, [open, loadEditor]);

  function startEdit(cell, editsList = edits) {
    const existing = findEditForCell(editsList, cell);
    // Для уже изменённой ячейки исходное значение берём из правки, а не из текущего PDF.
    const normalized = existing ? { ...cell, cellText: existing.cell_text || cell.cellText } : cell;
    editingKeyRef.current = String(cell.cellKey);
    suppressBlurRef.current = false;
    setSelectedCell(normalized);
    setReplacementText(existing?.replacement || cell.cellText || "");
    setInputOpen(true);
    setError("");
    setSuccess("");
  }

  function closeEdit() {
    editingKeyRef.current = "";
    setInputOpen(false);
    setSelectedCell(null);
  }

  function onCellClick(cell) {
    if (saving) return;
    if (inputOpen && selectedCell && String(selectedCell.cellKey) === String(cell.cellKey)) return;
    startEdit(cell);
  }

  function openEditFromRow(row) {
    if (!row) return;
    const pageInfo = pdfPages.find((p) => Number(p.page) === Number(row.page));
    const matched = pageInfo?.cells?.find((cell) => String(cell.cellKey) === String(row.cell_key));
    const fallback = {
      cellKey: String(row.cell_key || ""),
      cellText: String(row.cell_text || ""),
      page: Number(row.page || 1),
      leftPct: Number(row.left || 0),
      topPct: Number(row.top || 0),
      widthPct: Number(row.width || 0.01),
      heightPct: Number(row.height || 0.02),
      fillColor: String(row.fill_color || ""),
      textColor: String(row.text_color || "")
    };
    const cell = matched
      ? {
          ...matched,
          fillColor: matched.fillColor || String(row.fill_color || ""),
          textColor: matched.textColor || String(row.text_color || "")
        }
      : fallback;
    startEdit(cell);
  }

  /** Соседняя ячейка в порядке чтения (по строкам, слева направо), в т.ч. на следующей странице. */
  function neighborCell(cell, dir) {
    const flat = [];
    for (const page of pdfPages) {
      const sorted = [...(page.cells || [])].sort(
        (a, b) =>
          Math.round(a.topPct * 250) - Math.round(b.topPct * 250) || a.leftPct - b.leftPct
      );
      flat.push(...sorted);
    }
    const idx = flat.findIndex((c) => String(c.cellKey) === String(cell.cellKey));
    if (idx < 0) return null;
    return flat[idx + dir] || null;
  }

  /** Сохранить значение из поля. dir: 0 — закрыть, 1/-1 — перейти к следующей/предыдущей ячейке. */
  async function commitEdit(dir = 0) {
    if (!summaryItem?.id || !selectedCell || saving) return;
    const cell = selectedCell;
    const text = String(replacementText || "").trim();
    const existing = findEditForCell(edits, cell);
    const current = String(existing ? existing.replacement : cell.cellText || "").trim();
    const next = dir ? neighborCell(cell, dir) : null;
    if (!text) {
      setError("Значение не может быть пустым.");
      return;
    }
    if (text === current) {
      if (next) startEdit(next);
      else if (editingKeyRef.current === String(cell.cellKey)) closeEdit();
      return;
    }
    setSaving(true);
    setError("");
    setSuccess("");
    try {
      const result = await saveSummaryCellEdit({
        summaryId: Number(summaryItem.id),
        page: cell.page,
        left: cell.leftPct,
        top: cell.topPct,
        width: cell.widthPct,
        height: cell.heightPct,
        cellKey: cell.cellKey,
        cellText: cell.cellText,
        replacement: text,
        fillColor: cell.fillColor || "",
        textColor: cell.textColor || ""
      });
      const newEdits = Array.isArray(result?.edits) ? result.edits : [];
      setEdits(newEdits);
      setSuccess("Сохранено, PDF обновлён.");
      if (next) startEdit(next, newEdits);
      else if (editingKeyRef.current === String(cell.cellKey)) closeEdit();
      await loadEditor(true);
      onSaved?.();
    } catch (e) {
      setError(String(e?.response?.data?.detail || e?.message || "Не удалось сохранить правку."));
    } finally {
      setSaving(false);
    }
  }

  function onInputKeyDown(event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      suppressBlurRef.current = true;
      commitEdit(0);
    } else if (event.key === "Tab") {
      event.preventDefault();
      suppressBlurRef.current = true;
      commitEdit(event.shiftKey ? -1 : 1);
    } else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      suppressBlurRef.current = true;
      closeEdit();
    }
  }

  async function handleDelete(editId) {
    if (!summaryItem?.id || !editId) return;
    setSaving(true);
    setError("");
    setSuccess("");
    try {
      const result = await deleteSummaryCellEdit({
        summaryId: Number(summaryItem.id),
        editId
      });
      setEdits(Array.isArray(result?.edits) ? result.edits : []);
      setSuccess("Правка удалена, PDF пересобран.");
      if (selectedCell) {
        const still = Boolean(findEditForCell(result?.edits || [], selectedCell));
        if (!still) {
          setInputOpen(false);
          setSelectedCell(null);
        }
      }
      await loadEditor(true);
      onSaved?.();
    } catch (e) {
      setError(String(e?.response?.data?.detail || e?.message || "Не удалось удалить правку."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <Dialog
        open={open}
        onClose={saving ? undefined : onClose}
        maxWidth="lg"
        fullWidth
        sx={{
          "& .MuiDialog-paper": {
            height: "min(920px, 94vh)",
            maxHeight: "94vh",
            display: "flex",
            flexDirection: "column"
          }
        }}
      >
        <DialogTitle sx={{ pb: 1 }}>
          <Typography variant="h6" component="span">
            Правки в документе
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.25 }}>
            {title}
            {reportId ? ` · id ${reportId}` : ""}
          </Typography>
        </DialogTitle>
        <DialogContent dividers sx={{ flex: 1, overflow: "auto", minHeight: 0 }}>
          <Stack spacing={1.5}>
            <Stack direction="row" spacing={1} alignItems="center" justifyContent="space-between">
              <Typography variant="body2" color="text.secondary">
                Кликните по значению и введите новое. Enter — сохранить, Tab — к следующей ячейке, Esc — отмена. Старое значение удаляется из PDF.
              </Typography>
              <Button
                size="small"
                startIcon={<RefreshRoundedIcon />}
                onClick={() => loadEditor()}
                disabled={loading || saving}
              >
                Обновить
              </Button>
            </Stack>

            {loading ? (
              <Stack direction="row" spacing={1} alignItems="center" sx={{ py: 4 }}>
                <CircularProgress size={22} />
                <Typography color="text.secondary">Загрузка PDF и разметки ячеек…</Typography>
              </Stack>
            ) : (
              <Stack direction={{ xs: "column", md: "row" }} spacing={2} alignItems="flex-start">
                <Stack spacing={2} sx={{ flex: 1, minWidth: 0, width: "100%" }}>
                  {pdfPages.map((page) => (
                    <Box
                      key={page.page}
                      sx={{ border: 1, borderColor: "divider", borderRadius: 1.5, p: 1 }}
                    >
                      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                        Страница {page.page}
                      </Typography>
                      <Box
                        sx={{
                          position: "relative",
                          width: "100%",
                          overflow: "auto",
                          borderRadius: 1,
                          bgcolor: "#fff"
                        }}
                      >
                        <Box
                          component="img"
                          src={page.imageDataUrl}
                          alt={`page-${page.page}`}
                          sx={{ display: "block", width: "100%", height: "auto" }}
                        />
                        <Box sx={{ position: "absolute", inset: 0, pointerEvents: "none" }}>
                          {page.cells.map((cell) => {
                            const editing =
                              inputOpen && String(selectedCell?.cellKey) === String(cell.cellKey);
                            const existing = findEditForCell(edits, cell);
                            const boxLeft = `${cell.leftPct * 100}%`;
                            const boxTop = `${cell.topPct * 100}%`;
                            if (editing) {
                              return (
                                <Box
                                  key={cell.cellKey}
                                  sx={{
                                    position: "absolute",
                                    left: boxLeft,
                                    top: boxTop,
                                    minWidth: 64,
                                    width: `${Math.max(0.6, cell.widthPct * 100)}%`,
                                    height: `${Math.max(1, cell.heightPct * 100)}%`,
                                    minHeight: 22,
                                    zIndex: 5,
                                    pointerEvents: "auto"
                                  }}
                                >
                                  <input
                                    autoFocus
                                    value={replacementText}
                                    onChange={(event) => setReplacementText(event.target.value)}
                                    onKeyDown={onInputKeyDown}
                                    onFocus={(event) => event.target.select()}
                                    onBlur={() => {
                                      if (suppressBlurRef.current) return;
                                      commitEdit(0);
                                    }}
                                    style={{
                                      width: "100%",
                                      height: "100%",
                                      boxSizing: "border-box",
                                      font: "13px/1 sans-serif",
                                      textAlign: "center",
                                      color: "#111827",
                                      background: "#ffffff",
                                      border: "2px solid #2563EB",
                                      borderRadius: 2,
                                      outline: "none",
                                      boxShadow: "0 2px 8px rgba(0,0,0,0.25)"
                                    }}
                                  />
                                </Box>
                              );
                            }
                            return (
                              <Box
                                key={cell.cellKey}
                                onClick={() => onCellClick(cell)}
                                title={
                                  existing
                                    ? `${existing.cell_text || cell.cellText} → ${existing.replacement}`
                                    : cell.cellText
                                }
                                sx={{
                                  position: "absolute",
                                  left: boxLeft,
                                  top: boxTop,
                                  width: `${Math.max(0.6, cell.widthPct * 100)}%`,
                                  height: `${Math.max(1, cell.heightPct * 100)}%`,
                                  border: "1px solid transparent",
                                  borderBottom: existing ? "2px solid #16A34A" : "1px solid transparent",
                                  cursor: "text",
                                  pointerEvents: "auto",
                                  "&::after": existing
                                    ? {
                                        content: '""',
                                        position: "absolute",
                                        top: -1,
                                        right: -1,
                                        width: 0,
                                        height: 0,
                                        borderTop: "6px solid #16A34A",
                                        borderLeft: "6px solid transparent"
                                      }
                                    : undefined,
                                  "&:hover": {
                                    border: "1px solid #2563EB",
                                    borderBottom: existing ? "2px solid #16A34A" : "1px solid #2563EB",
                                    backgroundColor: "rgba(37,99,235,0.12)"
                                  }
                                }}
                              />
                            );
                          })}
                        </Box>
                      </Box>
                    </Box>
                  ))}
                  {!pdfPages.length && !loading ? (
                    <Alert severity="info">Нет страниц для отображения.</Alert>
                  ) : null}
                </Stack>

                <Box
                  sx={{
                    width: { xs: "100%", md: 300 },
                    flexShrink: 0,
                    border: 1,
                    borderColor: "divider",
                    borderRadius: 1.5,
                    p: 1.5,
                    bgcolor: (t) =>
                      t.palette.mode === "light" ? t.palette.grey[50] : "rgba(255,255,255,0.03)"
                  }}
                >
                  <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
                    <Typography variant="subtitle2">Сохранённые правки</Typography>
                    <Chip size="small" label={edits.length} variant="outlined" />
                  </Stack>
                  {edits.length === 0 ? (
                    <Typography variant="body2" color="text.secondary">
                      Пока нет правок. Выберите ячейку на странице.
                    </Typography>
                  ) : (
                    <List dense disablePadding>
                      {edits.map((row) => (
                        <ListItem
                          key={row.id}
                          disableGutters
                          secondaryAction={
                            <IconButton
                              edge="end"
                              size="small"
                              disabled={saving}
                              onClick={() => handleDelete(row.id)}
                              aria-label="Удалить правку"
                            >
                              <DeleteOutlineRoundedIcon fontSize="small" />
                            </IconButton>
                          }
                          sx={{ alignItems: "flex-start", pr: 5 }}
                        >
                          <ListItemText
                            primary={`Стр. ${row.page}: ${row.cell_text || "—"} → ${row.replacement}`}
                            secondary="Нажмите, чтобы изменить"
                            primaryTypographyProps={{ variant: "body2" }}
                            secondaryTypographyProps={{ variant: "caption" }}
                            onClick={() => openEditFromRow(row)}
                            sx={{ cursor: "pointer", mr: 1 }}
                          />
                        </ListItem>
                      ))}
                    </List>
                  )}
                </Box>
              </Stack>
            )}
          </Stack>
        </DialogContent>
        <DialogActions sx={{ px: 2, py: 1.5 }}>
          {/* Статус внизу, чтобы страницы не «прыгали» при каждом сохранении. */}
          <Typography
            variant="body2"
            color={error ? "error" : "success.main"}
            sx={{ flex: 1, minWidth: 0 }}
          >
            {saving ? "Сохранение…" : error || success}
          </Typography>
          <Button onClick={onClose} disabled={saving}>
            Закрыть
          </Button>
        </DialogActions>
      </Dialog>

    </>
  );
}
