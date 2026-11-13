import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import CircularProgress from "@mui/material/CircularProgress";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Divider from "@mui/material/Divider";
import FormControl from "@mui/material/FormControl";
import InputLabel from "@mui/material/InputLabel";
import MenuItem from "@mui/material/MenuItem";
import Select from "@mui/material/Select";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import RefreshRoundedIcon from "@mui/icons-material/RefreshRounded";
import {
  deleteSummaryComment,
  fetchSummaryComments,
  getPreviewUrl,
  saveSummaryComment,
  toggleSummaryCommentFuture
} from "../api";
import { parsePdfCellsFromUrl } from "../utils/pdfCellParser";
import { findCommentForCell } from "../utils/commentCellMatch";

export function clampCommentNorm(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return 0;
  return Math.max(0, Math.min(1, n));
}

export function defaultCommentAnchorYForCell(cell, pdfPages) {
  if (!cell) return null;
  const pageInfo = pdfPages.find((p) => p.page === cell.page);
  if (!pageInfo || !Array.isArray(pageInfo.cells) || pageInfo.cells.length === 0) {
    return null;
  }
  const top = cell.topPct;
  const candidates = pageInfo.cells.filter(
    (c) =>
      c.topPct >= Math.max(0, top - 0.03) &&
      c.topPct <= Math.min(1, top + 0.4) &&
      c.widthPct > 0.01
  );
  if (!candidates.length) return null;
  const bottom = Math.max(...candidates.map((c) => c.topPct + c.heightPct));
  return Math.max(0, Math.min(1, bottom + 0.015));
}

export function commentAnchorYForRow(row) {
  const raw = row?.comment_anchor_y;
  if (raw != null && raw !== "" && Number.isFinite(Number(raw))) {
    return clampCommentNorm(raw);
  }
  return clampCommentNorm(row?.y);
}

export default function SummaryCommentEditorDialog({
  open,
  onClose,
  items = [],
  initialSummaryId = null,
  accessToken = "",
  onSaved
}) {
  const [summaryId, setSummaryId] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [commentRows, setCommentRows] = useState([]);
  const [reportId, setReportId] = useState("");
  const [pdfPages, setPdfPages] = useState([]);
  const [selectedCell, setSelectedCell] = useState(null);
  const [inputOpen, setInputOpen] = useState(false);
  const [inputText, setInputText] = useState("");
  const [dragPreview, setDragPreview] = useState(null);
  const [draftAnchorY, setDraftAnchorY] = useState(null);
  const pageOverlayRefs = useRef({});
  const loadTokenRef = useRef(0);

  const selectedItem = useMemo(
    () => items.find((row) => String(row.id) === String(summaryId)) || null,
    [items, summaryId]
  );

  const selectedCellComment = findCommentForCell(selectedCell, commentRows);

  const loadEditor = useCallback(
    async (summaryIdValue, { keepMessages = false } = {}) => {
      const sid = String(summaryIdValue || "");
      const selected = items.find((row) => String(row.id) === sid);
      if (!selected) return;

      const loadToken = loadTokenRef.current + 1;
      loadTokenRef.current = loadToken;
      const isStale = () => loadTokenRef.current !== loadToken;

      setLoading(true);
      if (!keepMessages) {
        // После сохранения сообщение об успехе/ошибке не стираем.
        setError("");
        setSuccess("");
      }
      try {
        const baseUrl = getPreviewUrl(selected, accessToken);
        const previewUrl =
          !baseUrl || baseUrl === "#"
            ? baseUrl
            : `${baseUrl}${baseUrl.includes("?") ? "&" : "?"}ts=${Date.now()}`;
        const [serverState, pages] = await Promise.all([
          fetchSummaryComments(selected.id),
          parsePdfCellsFromUrl(previewUrl)
        ]);
        if (isStale()) return;
        setCommentRows(Array.isArray(serverState?.comments) ? serverState.comments : []);
        setReportId(String(serverState?.report_id || ""));
        setPdfPages(Array.isArray(pages) ? pages : []);
      } catch (e) {
        if (!isStale()) {
          setError(
            String(
              e?.response?.data?.detail ||
                e?.message ||
                "Не удалось загрузить редактор комментариев."
            )
          );
          setCommentRows([]);
          setPdfPages([]);
        }
      } finally {
        if (!isStale()) setLoading(false);
      }
    },
    [items, accessToken]
  );

  useEffect(() => {
    if (!open) return;
    setSelectedCell(null);
    setInputOpen(false);
    setInputText("");
    setDragPreview(null);
    setDraftAnchorY(null);
    setError("");
    setSuccess("");

    const initial =
      initialSummaryId != null && items.some((row) => String(row.id) === String(initialSummaryId))
        ? String(initialSummaryId)
        : items.length
          ? String(items[0].id)
          : "";
    setSummaryId(initial);
    if (initial) {
      loadEditor(initial);
    } else {
      setCommentRows([]);
      setPdfPages([]);
      setReportId("");
    }
    return () => {
      loadTokenRef.current += 1;
    };
  }, [open, initialSummaryId, items, loadEditor]);

  async function handleSummaryChange(nextSummaryId) {
    const sid = String(nextSummaryId || "");
    setSummaryId(sid);
    setSelectedCell(null);
    setInputOpen(false);
    setInputText("");
    setDragPreview(null);
    setDraftAnchorY(null);
    if (!sid) {
      setCommentRows([]);
      setPdfPages([]);
      return;
    }
    await loadEditor(sid);
  }

  function onCellClick(cell) {
    setSelectedCell(cell);
    const existing = findCommentForCell(cell, commentRows);
    setInputText(existing?.comment || "");
    setDraftAnchorY(null);
    setDragPreview(null);
    setInputOpen(true);
  }

  function openEditorForRow(row) {
    if (!row) return;
    const pageInfo = pdfPages.find((p) => Number(p.page) === Number(row.page));
    const matchedCell = pageInfo?.cells?.find(
      (cell) => String(cell.cellKey) === String(row.cell_key)
    );
    const fallbackCell = {
      cellKey: String(row.cell_key || ""),
      cellText: String(row.cell_text || row.cell_key || ""),
      page: Number(row.page || 1),
      leftPct: Number(row.x || 0),
      topPct: Number(row.y || 0),
      widthPct: 0.01,
      heightPct: 0.02
    };
    setSelectedCell(matchedCell || fallbackCell);
    setInputText(String(row.comment || ""));
    setDraftAnchorY(null);
    setDragPreview(null);
    setInputOpen(true);
  }

  async function submitSave(applyToFuture = false) {
    if (!summaryId || !selectedCell) return;
    const text = String(inputText || "").trim();
    if (!text) {
      setError("Введите текст комментария.");
      return;
    }
    setSaving(true);
    setError("");
    setSuccess("");
    try {
      const payload = {
        summaryId: Number(summaryId),
        page: selectedCell.page,
        x: Math.max(
          0,
          Math.min(1, selectedCell.leftPct + selectedCell.widthPct * 0.96)
        ),
        y: Math.max(
          0,
          Math.min(1, selectedCell.topPct + selectedCell.heightPct * 0.52)
        ),
        commentAnchorY: (() => {
          if (typeof draftAnchorY === "number" && Number.isFinite(draftAnchorY)) {
            return clampCommentNorm(draftAnchorY);
          }
          return defaultCommentAnchorYForCell(selectedCell, pdfPages);
        })(),
        cellKey: selectedCell.cellKey,
        cellText: selectedCell.cellText,
        comment: text,
        applyToFuture: Boolean(applyToFuture)
      };
      const result = await saveSummaryComment(payload);
      setCommentRows(Array.isArray(result?.comments) ? result.comments : []);
      if (result?.apply_error) {
        setError(`Комментарий сохранён, но PDF не обновлён: ${result.apply_error}`);
      } else {
        setSuccess(
          applyToFuture
            ? "Комментарий сохранён и будет применяться к будущим сводкам."
            : "Комментарий сохранён, PDF обновлён."
        );
      }
      setInputOpen(false);
      setInputText("");
      setDraftAnchorY(null);
      setDragPreview(null);
      await loadEditor(summaryId, { keepMessages: true });
      onSaved?.();
    } catch (e) {
      setError(String(e?.response?.data?.detail || "Не удалось сохранить комментарий."));
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete(commentId) {
    if (!summaryId || !commentId) return;
    setSaving(true);
    setError("");
    setSuccess("");
    try {
      const result = await deleteSummaryComment({
        summaryId: Number(summaryId),
        commentId
      });
      setCommentRows(Array.isArray(result?.comments) ? result.comments : []);
      if (result?.apply_error) {
        setError(`Комментарий удалён, но PDF не пересобран: ${result.apply_error}`);
      } else {
        setSuccess("Комментарий удалён, PDF пересобран.");
      }
      await loadEditor(summaryId, { keepMessages: true });
      onSaved?.();
    } catch (e) {
      setError(String(e?.response?.data?.detail || "Не удалось удалить комментарий."));
    } finally {
      setSaving(false);
    }
  }

  async function handleToggleFutureForCellKey(cellKey, enabled) {
    if (!summaryId || !cellKey) return;
    setSaving(true);
    setError("");
    setSuccess("");
    try {
      const result = await toggleSummaryCommentFuture({
        summaryId: Number(summaryId),
        cellKey: String(cellKey),
        enabled: Boolean(enabled)
      });
      setCommentRows(Array.isArray(result?.comments) ? result.comments : []);
      setSuccess(
        enabled
          ? "Применение к будущим сводкам включено."
          : "Применение к будущим сводкам отключено."
      );
      await loadEditor(summaryId, { keepMessages: true });
    } catch (e) {
      setError(
        String(
          e?.response?.data?.detail ||
            e?.message ||
            "Не удалось обновить настройку для будущих сводок."
        )
      );
    } finally {
      setSaving(false);
    }
  }

  async function handleToggleFutureForCell(enabled) {
    if (!selectedCell) return;
    await handleToggleFutureForCellKey(selectedCell.cellKey, enabled);
  }

  async function persistAnchorY(rowSnapshot, newAnchorY) {
    if (!summaryId || !rowSnapshot?.cell_key) return;
    const text = String(rowSnapshot.comment || "").trim();
    if (!text) {
      setError("Нет текста комментария для сохранения позиции.");
      return;
    }
    setSaving(true);
    setError("");
    setSuccess("");
    try {
      const result = await saveSummaryComment({
        summaryId: Number(summaryId),
        page: Number(rowSnapshot.page || 1),
        x: clampCommentNorm(rowSnapshot.x),
        y: clampCommentNorm(rowSnapshot.y),
        commentAnchorY: clampCommentNorm(newAnchorY),
        cellKey: String(rowSnapshot.cell_key),
        cellText: String(rowSnapshot.cell_text || ""),
        comment: text,
        applyToFuture: Boolean(rowSnapshot.apply_to_future)
      });
      setCommentRows(Array.isArray(result?.comments) ? result.comments : []);
      if (result?.apply_error) {
        setError(`Положение сохранено, но PDF не обновлён: ${result.apply_error}`);
      } else {
        setSuccess("Положение текста комментария обновлено, PDF пересобран.");
      }
      await loadEditor(summaryId, { keepMessages: true });
      onSaved?.();
    } catch (e) {
      setError(String(e?.response?.data?.detail || "Не удалось сохранить положение текста."));
    } finally {
      setSaving(false);
    }
  }

  function beginTextAnchorPointerDown(ev, row) {
    ev.preventDefault();
    ev.stopPropagation();
    if (saving) return;
    const pageNum = Number(row.page || 1);
    const overlayEl = pageOverlayRefs.current[pageNum];
    if (!overlayEl) return;
    const snapshot = {
      id: row.id,
      page: pageNum,
      cell_key: row.cell_key,
      cell_text: row.cell_text,
      comment: row.comment,
      x: Number(row.x),
      y: Number(row.y),
      comment_anchor_y: row.comment_anchor_y,
      apply_to_future: row.apply_to_future
    };
    const rect0 = overlayEl.getBoundingClientRect();
    const initialAnchor = commentAnchorYForRow(row);
    let lastAnchor = clampCommentNorm((ev.clientY - rect0.top) / rect0.height);
    setDragPreview({ id: row.id, page: pageNum, anchorY: lastAnchor });

    const onMove = (e) => {
      const rect = overlayEl.getBoundingClientRect();
      lastAnchor = clampCommentNorm((e.clientY - rect.top) / rect.height);
      setDragPreview({ id: row.id, page: pageNum, anchorY: lastAnchor });
    };

    const finish = async () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", finish);
      window.removeEventListener("pointercancel", finish);
      setDragPreview(null);
      if (Math.abs(lastAnchor - initialAnchor) < 0.004) return;
      await persistAnchorY(snapshot, lastAnchor);
    };

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", finish);
    window.addEventListener("pointercancel", finish);
  }

  function beginDraftTextAnchorPointerDown(ev, pageNum) {
    ev.preventDefault();
    ev.stopPropagation();
    if (saving || !selectedCell) return;
    const overlayEl = pageOverlayRefs.current[pageNum];
    if (!overlayEl) return;
    const rowEditing = findCommentForCell(selectedCell, commentRows);
    const autoY = defaultCommentAnchorYForCell(selectedCell, pdfPages);
    const initialAnchor =
      typeof draftAnchorY === "number" && Number.isFinite(draftAnchorY)
        ? clampCommentNorm(draftAnchorY)
        : rowEditing
          ? commentAnchorYForRow(rowEditing)
          : clampCommentNorm(
              autoY ?? selectedCell.topPct + selectedCell.heightPct * 0.88
            );

    const rect0 = overlayEl.getBoundingClientRect();
    let lastAnchor = clampCommentNorm((ev.clientY - rect0.top) / rect0.height);
    setDragPreview({ id: "__draft__", page: pageNum, anchorY: lastAnchor });

    const onMove = (e) => {
      const rect = overlayEl.getBoundingClientRect();
      lastAnchor = clampCommentNorm((e.clientY - rect.top) / rect.height);
      setDragPreview({ id: "__draft__", page: pageNum, anchorY: lastAnchor });
    };

    const finish = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", finish);
      window.removeEventListener("pointercancel", finish);
      setDragPreview(null);
      if (Math.abs(lastAnchor - initialAnchor) < 0.004) return;
      setDraftAnchorY(lastAnchor);
    };

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", finish);
    window.addEventListener("pointercancel", finish);
  }

  function handleClose() {
    if (saving) return;
    setDragPreview(null);
    setDraftAnchorY(null);
    onClose?.();
  }

  return (
    <>
      <Dialog
        open={open}
        onClose={handleClose}
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
            Комментарии к готовой сводке
          </Typography>
        </DialogTitle>
        <DialogContent dividers sx={{ flex: 1, overflow: "auto", minHeight: 0 }}>
          <Stack spacing={1.5}>
            <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5}>
              <FormControl size="small" fullWidth>
                <InputLabel id="comment-summary-label">Сводка</InputLabel>
                <Select
                  labelId="comment-summary-label"
                  label="Сводка"
                  value={summaryId}
                  onChange={(e) => handleSummaryChange(String(e.target.value))}
                  disabled={!items.length || loading || saving}
                >
                  {items.map((row) => (
                    <MenuItem key={row.id} value={String(row.id)}>
                      {row.title}
                    </MenuItem>
                  ))}
                </Select>
              </FormControl>
              <Button
                variant="outlined"
                startIcon={<RefreshRoundedIcon />}
                onClick={() => summaryId && loadEditor(summaryId)}
                disabled={!summaryId || loading || saving}
              >
                Обновить
              </Button>
            </Stack>

            {error ? <Alert severity="error">{error}</Alert> : null}
            {success ? <Alert severity="success">{success}</Alert> : null}
            {reportId ? (
              <Typography variant="caption" color="text.secondary">
                ID сводки: {reportId}. Кликните по ячейке, чтобы задать текст. Синюю полосу «Текст
                комментария» можно тянуть до сохранения и после — положение блока текста в PDF;
                красная звёздочка — привязка к ячейке (координаты задаются ячейкой).
              </Typography>
            ) : null}

            {!items.length ? (
              <Alert severity="info">На выбранную дату нет PDF-сводок для комментариев.</Alert>
            ) : loading ? (
              <Stack direction="row" spacing={1} alignItems="center" sx={{ py: 4 }}>
                <CircularProgress size={22} />
                <Typography color="text.secondary">Загрузка PDF и разметки ячеек…</Typography>
              </Stack>
            ) : (
              <Stack spacing={2}>
                {pdfPages.map((page) => (
                  <Box
                    key={page.page}
                    sx={{ border: 1, borderColor: "divider", borderRadius: 1.5, p: 1 }}
                  >
                    <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                      Страница {page.page}
                    </Typography>
                    <Box
                      ref={(el) => {
                        pageOverlayRefs.current[page.page] = el;
                      }}
                      sx={{
                        position: "relative",
                        width: "100%",
                        maxWidth: "100%",
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
                          const existing = findCommentForCell(cell, commentRows);
                          return (
                            <Box
                              key={cell.cellKey}
                              className="comment-cell-hotspot"
                              onClick={() => onCellClick(cell)}
                              title={cell.cellText}
                              sx={{
                                position: "absolute",
                                left: `${cell.leftPct * 100}%`,
                                top: `${cell.topPct * 100}%`,
                                width: `${Math.max(0.6, cell.widthPct * 100)}%`,
                                height: `${Math.max(1, cell.heightPct * 100)}%`,
                                border: selected
                                  ? "2px solid #22D3EE"
                                  : existing
                                    ? "2px solid #EF4444"
                                    : "1.5px solid rgba(250, 204, 21, 0.88)",
                                backgroundColor: selected
                                  ? "rgba(34,211,238,0.3)"
                                  : existing
                                    ? "rgba(239,68,68,0.22)"
                                    : "rgba(250, 204, 21, 0.2)",
                                cursor: "pointer",
                                pointerEvents: "auto"
                              }}
                            />
                          );
                        })}
                        {commentRows
                          .filter((crow) => Number(crow.page) === Number(page.page))
                          .map((crow) => {
                            const editingThisCell =
                              Boolean(selectedCellComment) &&
                              String(crow.id) === String(selectedCellComment.id);
                            const live =
                              dragPreview && String(dragPreview.id) === String(crow.id)
                                ? dragPreview
                                : null;
                            const anchorY = clampCommentNorm(
                              live != null && typeof live.anchorY === "number"
                                ? live.anchorY
                                : commentAnchorYForRow(crow)
                            );
                            const px = clampCommentNorm(crow.x);
                            const py = clampCommentNorm(crow.y);
                            return (
                              <Box
                                key={`comment-overlay-${crow.id}`}
                                sx={{ position: "absolute", inset: 0 }}
                              >
                                <Box
                                  title="Позиция звёздочки в PDF (меняется при сохранении из ячейки)"
                                  sx={{
                                    position: "absolute",
                                    left: `${px * 100}%`,
                                    top: `${py * 100}%`,
                                    transform: "translate(-50%, -50%)",
                                    width: 24,
                                    height: 24,
                                    borderRadius: "50%",
                                    bgcolor: "#B91C1C",
                                    color: "#fff",
                                    display: "flex",
                                    alignItems: "center",
                                    justifyContent: "center",
                                    fontSize: 12,
                                    fontWeight: 700,
                                    pointerEvents: "none",
                                    boxShadow: 1,
                                    zIndex: 2,
                                    opacity: 0.92
                                  }}
                                >
                                  *
                                </Box>
                                {!editingThisCell ? (
                                  <Box
                                    onPointerDown={(e) => beginTextAnchorPointerDown(e, crow)}
                                    title="Перетащите вверх или вниз — блок текста комментария в PDF"
                                    sx={{
                                      position: "absolute",
                                      left: "3%",
                                      width: "94%",
                                      top: `${anchorY * 100}%`,
                                      transform: "translateY(-50%)",
                                      minHeight: 28,
                                      py: 0.25,
                                      px: 0.5,
                                      borderRadius: 1,
                                      bgcolor: "rgba(14, 165, 233, 0.28)",
                                      border: "2px solid #0284c7",
                                      display: "flex",
                                      alignItems: "center",
                                      justifyContent: "center",
                                      cursor: saving ? "wait" : "ns-resize",
                                      pointerEvents: "auto",
                                      zIndex: 3,
                                      userSelect: "none",
                                      touchAction: "none"
                                    }}
                                  >
                                    <Typography
                                      variant="caption"
                                      sx={{ fontWeight: 600, color: "#0c4a6e" }}
                                    >
                                      Текст комментария — тянуть ↑↓
                                    </Typography>
                                  </Box>
                                ) : null}
                              </Box>
                            );
                          })}
                        {open &&
                          selectedCell &&
                          Number(selectedCell.page) === Number(page.page) ? (
                          <Box
                            sx={{
                              position: "absolute",
                              inset: 0,
                              zIndex: 4,
                              pointerEvents: "none"
                            }}
                          >
                            {(() => {
                              const rowEditing = findCommentForCell(selectedCell, commentRows);
                              const autoY = defaultCommentAnchorYForCell(
                                selectedCell,
                                pdfPages
                              );
                              const anchorBase =
                                typeof draftAnchorY === "number" &&
                                Number.isFinite(draftAnchorY)
                                  ? clampCommentNorm(draftAnchorY)
                                  : rowEditing
                                    ? commentAnchorYForRow(rowEditing)
                                    : clampCommentNorm(
                                        autoY ??
                                          selectedCell.topPct +
                                            selectedCell.heightPct * 0.88
                                      );
                              const liveDraft =
                                dragPreview?.id === "__draft__" &&
                                Number(dragPreview.page) === Number(page.page)
                                  ? dragPreview
                                  : null;
                              const anchorY = clampCommentNorm(
                                liveDraft != null && typeof liveDraft.anchorY === "number"
                                  ? liveDraft.anchorY
                                  : anchorBase
                              );
                              const cpx = clampCommentNorm(
                                selectedCell.leftPct + selectedCell.widthPct * 0.96
                              );
                              const cpy = clampCommentNorm(
                                selectedCell.topPct + selectedCell.heightPct * 0.52
                              );
                              return (
                                <>
                                  {!rowEditing ? (
                                    <Box
                                      title="Новый комментарий: позиция звёздочки по ячейке"
                                      sx={{
                                        position: "absolute",
                                        left: `${cpx * 100}%`,
                                        top: `${cpy * 100}%`,
                                        transform: "translate(-50%, -50%)",
                                        width: 24,
                                        height: 24,
                                        borderRadius: "50%",
                                        bgcolor: "#B91C1C",
                                        color: "#fff",
                                        display: "flex",
                                        alignItems: "center",
                                        justifyContent: "center",
                                        fontSize: 12,
                                        fontWeight: 700,
                                        pointerEvents: "none",
                                        boxShadow: 1,
                                        zIndex: 2,
                                        opacity: 0.85
                                      }}
                                    >
                                      *
                                    </Box>
                                  ) : null}
                                  <Box
                                    onPointerDown={(e) =>
                                      beginDraftTextAnchorPointerDown(e, page.page)
                                    }
                                    title="Положение текста до сохранения (и при редактировании ячейки)"
                                    sx={{
                                      position: "absolute",
                                      left: "3%",
                                      width: "94%",
                                      top: `${anchorY * 100}%`,
                                      transform: "translateY(-50%)",
                                      minHeight: 28,
                                      py: 0.25,
                                      px: 0.5,
                                      borderRadius: 1,
                                      bgcolor: "rgba(14, 165, 233, 0.22)",
                                      border: "2px dashed #0369a1",
                                      display: "flex",
                                      alignItems: "center",
                                      justifyContent: "center",
                                      cursor: saving ? "wait" : "ns-resize",
                                      pointerEvents: "auto",
                                      zIndex: 3,
                                      userSelect: "none",
                                      touchAction: "none"
                                    }}
                                  >
                                    <Typography
                                      variant="caption"
                                      sx={{ fontWeight: 600, color: "#0c4a6e" }}
                                    >
                                      Текст комментария (черновик) — тянуть ↑↓
                                    </Typography>
                                  </Box>
                                </>
                              );
                            })()}
                          </Box>
                        ) : null}
                      </Box>
                    </Box>
                  </Box>
                ))}
              </Stack>
            )}

            <Divider />
            <Typography variant="subtitle2">Сохранённые комментарии</Typography>
            {commentRows.length === 0 ? (
              <Typography variant="body2" color="text.secondary">
                Пока нет комментариев для этой сводки.
              </Typography>
            ) : (
              <Stack spacing={0.75}>
                {commentRows.map((row) => (
                  <Stack
                    key={row.id}
                    direction={{ xs: "column", sm: "row" }}
                    spacing={1}
                    sx={{ p: 1, border: 1, borderColor: "divider", borderRadius: 1 }}
                  >
                    <Typography variant="body2" sx={{ flex: 1 }}>
                      <strong>Стр. {row.page}</strong>: {row.cell_text || row.cell_key} —{" "}
                      {row.comment}
                    </Typography>
                    <Button
                      size="small"
                      variant="outlined"
                      onClick={() => openEditorForRow(row)}
                      disabled={saving}
                    >
                      Редактировать
                    </Button>
                    <Button
                      size="small"
                      color={row.apply_to_future ? "warning" : "primary"}
                      variant="outlined"
                      onClick={() =>
                        handleToggleFutureForCellKey(row.cell_key, !row.apply_to_future)
                      }
                      disabled={saving}
                    >
                      {row.apply_to_future ? "Отменить для будущих" : "Добавить в будущие"}
                    </Button>
                    <Button
                      size="small"
                      color="error"
                      variant="outlined"
                      onClick={() => handleDelete(row.id)}
                      disabled={saving}
                    >
                      Удалить
                    </Button>
                  </Stack>
                ))}
              </Stack>
            )}
          </Stack>
        </DialogContent>
        <DialogActions sx={{ px: 2, py: 1.5 }}>
          <Button onClick={handleClose} disabled={saving}>
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
        <DialogTitle>Комментарий к ячейке</DialogTitle>
        <DialogContent dividers>
          <Stack spacing={1.5} sx={{ pt: 0.5 }}>
            <Typography variant="body2" color="text.secondary">
              Сводка: {selectedItem?.title || "—"}
            </Typography>
            <Typography variant="body2">
              Ячейка: {selectedCell?.cellText || selectedCell?.cellKey || "—"}
            </Typography>
            <TextField
              label="Комментарий"
              value={inputText}
              onChange={(e) => setInputText(e.target.value)}
              fullWidth
              multiline
              minRows={3}
              disabled={saving}
              autoFocus
            />
          </Stack>
        </DialogContent>
        <DialogActions sx={{ flexWrap: "wrap", gap: 0.5, px: 2, py: 1.5 }}>
          <Button
            variant="outlined"
            color="error"
            onClick={async () => {
              if (!selectedCellComment) return;
              await handleDelete(selectedCellComment.id);
              setInputOpen(false);
            }}
            disabled={saving || !selectedCellComment}
          >
            Удалить комментарий
          </Button>
          <Button variant="outlined" onClick={() => setInputOpen(false)} disabled={saving}>
            Отмена
          </Button>
          <Button variant="outlined" onClick={() => submitSave(true)} disabled={saving}>
            Сохранить и добавить на все дни
          </Button>
          <Button
            variant="outlined"
            color="warning"
            onClick={() => handleToggleFutureForCell(false)}
            disabled={saving || !selectedCellComment?.apply_to_future}
          >
            Отменить для будущих сводок
          </Button>
          <Button variant="contained" onClick={() => submitSave(false)} disabled={saving}>
            {saving ? "Сохранение…" : "Сохранить"}
          </Button>
        </DialogActions>
      </Dialog>
    </>
  );
}
