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
import TextField from "@mui/material/TextField";
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

  const title = summaryItem?.title || "Сводка";

  const loadEditor = useCallback(async () => {
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

    setLoading(true);
    setError("");
    setSuccess("");
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
      if (!isStale()) setLoading(false);
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

  function onCellClick(cell) {
    setSelectedCell(cell);
    const existing = edits.find((row) => String(row.cell_key) === String(cell.cellKey));
    setReplacementText(existing?.replacement || cell.cellText || "");
    setInputOpen(true);
    setError("");
    setSuccess("");
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
    setSelectedCell(cell);
    setReplacementText(String(row.replacement || cell.cellText || ""));
    setInputOpen(true);
  }

  async function handleSave() {
    if (!summaryItem?.id || !selectedCell) return;
    const text = String(replacementText || "").trim();
    if (!text) {
      setError("Введите новое значение для ячейки.");
      return;
    }
    setSaving(true);
    setError("");
    setSuccess("");
    try {
      const result = await saveSummaryCellEdit({
        summaryId: Number(summaryItem.id),
        page: selectedCell.page,
        left: selectedCell.leftPct,
        top: selectedCell.topPct,
        width: selectedCell.widthPct,
        height: selectedCell.heightPct,
        cellKey: selectedCell.cellKey,
        cellText: selectedCell.cellText,
        replacement: text,
        fillColor: selectedCell.fillColor || "",
        textColor: selectedCell.textColor || ""
      });
      setEdits(Array.isArray(result?.edits) ? result.edits : []);
      setSuccess("Правка сохранена, PDF обновлён.");
      setInputOpen(false);
      setSelectedCell(null);
      await loadEditor();
      onSaved?.();
    } catch (e) {
      setError(String(e?.response?.data?.detail || e?.message || "Не удалось сохранить правку."));
    } finally {
      setSaving(false);
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
        const still = (result?.edits || []).some(
          (row) => String(row.cell_key) === String(selectedCell.cellKey)
        );
        if (!still) {
          setInputOpen(false);
          setSelectedCell(null);
        }
      }
      await loadEditor();
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
                Кликните по значению на странице и введите замену — исходный текст будет перекрыт новым.
              </Typography>
              <Button
                size="small"
                startIcon={<RefreshRoundedIcon />}
                onClick={loadEditor}
                disabled={loading || saving}
              >
                Обновить
              </Button>
            </Stack>

            {error ? <Alert severity="error">{error}</Alert> : null}
            {success ? <Alert severity="success">{success}</Alert> : null}

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
                            const selected = selectedCell?.cellKey === cell.cellKey;
                            const existing = edits.find(
                              (row) => String(row.cell_key) === String(cell.cellKey)
                            );
                            return (
                              <Box
                                key={cell.cellKey}
                                onClick={() => onCellClick(cell)}
                                title={
                                  existing
                                    ? `${cell.cellText} → ${existing.replacement}`
                                    : cell.cellText
                                }
                                sx={{
                                  position: "absolute",
                                  left: `${cell.leftPct * 100}%`,
                                  top: `${cell.topPct * 100}%`,
                                  width: `${Math.max(0.6, cell.widthPct * 100)}%`,
                                  height: `${Math.max(1, cell.heightPct * 100)}%`,
                                  border: selected
                                    ? "2px solid #22D3EE"
                                    : existing
                                      ? "2px solid #16A34A"
                                      : "1.5px solid rgba(250, 204, 21, 0.88)",
                                  backgroundColor: selected
                                    ? "rgba(34,211,238,0.3)"
                                    : existing
                                      ? "rgba(22,163,74,0.22)"
                                      : "rgba(250, 204, 21, 0.2)",
                                  cursor: "pointer",
                                  pointerEvents: "auto"
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
          <Button onClick={onClose} disabled={saving}>
            Закрыть
          </Button>
        </DialogActions>
      </Dialog>

      <Dialog
        open={Boolean(open && inputOpen && selectedCell)}
        onClose={() => {
          if (saving) return;
          setInputOpen(false);
        }}
        maxWidth="sm"
        fullWidth
      >
        <DialogTitle>Замена значения</DialogTitle>
        <DialogContent dividers>
          <Stack spacing={1.5} sx={{ pt: 0.5 }}>
            <Typography variant="body2" color="text.secondary">
              Страница {selectedCell?.page}. Исходное значение:
            </Typography>
            <Alert severity="info" sx={{ py: 0.5 }}>
              {selectedCell?.cellText || "—"}
            </Alert>
            <TextField
              label="Новое значение"
              value={replacementText}
              onChange={(event) => setReplacementText(event.target.value)}
              fullWidth
              autoFocus
              multiline
              minRows={2}
              disabled={saving}
            />
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button
            color="inherit"
            disabled={saving}
            onClick={() => {
              setInputOpen(false);
              setSelectedCell(null);
            }}
          >
            Отмена
          </Button>
          {edits.some((row) => String(row.cell_key) === String(selectedCell?.cellKey)) ? (
            <Button
              color="error"
              disabled={saving}
              onClick={() => {
                const row = edits.find(
                  (item) => String(item.cell_key) === String(selectedCell?.cellKey)
                );
                if (row?.id) handleDelete(row.id);
              }}
            >
              Удалить правку
            </Button>
          ) : null}
          <Button variant="contained" disabled={saving} onClick={handleSave}>
            {saving ? "Сохранение…" : "Сохранить в PDF"}
          </Button>
        </DialogActions>
      </Dialog>
    </>
  );
}
