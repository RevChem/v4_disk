import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogContentText from "@mui/material/DialogContentText";
import DialogTitle from "@mui/material/DialogTitle";
import IconButton from "@mui/material/IconButton";
import List from "@mui/material/List";
import ListItem from "@mui/material/ListItem";
import ListItemText from "@mui/material/ListItemText";
import Stack from "@mui/material/Stack";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import DeleteOutlineRoundedIcon from "@mui/icons-material/DeleteOutlineRounded";
import FitScreenRoundedIcon from "@mui/icons-material/FitScreenRounded";
import RefreshRoundedIcon from "@mui/icons-material/RefreshRounded";
import HistoryRoundedIcon from "@mui/icons-material/HistoryRounded";
import DocumentHistoryDialog from "./DocumentHistoryDialog";
import SaveRoundedIcon from "@mui/icons-material/SaveRounded";
import UndoRoundedIcon from "@mui/icons-material/UndoRounded";
import ZoomInRoundedIcon from "@mui/icons-material/ZoomInRounded";
import ZoomOutRoundedIcon from "@mui/icons-material/ZoomOutRounded";
import {
  fetchSummaryCellEdits,
  getPreviewUrl,
  saveSummaryCellEditsBatch,
  summaryIsExcel
} from "../api";
import { parsePdfCellsFromUrl } from "../utils/pdfCellParser";
import {
  CHANGED_TINT,
  applyDraftValue,
  buildBatchPayload,
  cellFontCqw,
  clampZoom,
  draftCount,
  findCellForEdit,
  findEditForCell,
  isCellChanged,
  mergeEditRows,
  originalText,
  readingNeighbor,
  stepZoom,
  verticalNeighbor
} from "../utils/cellEditDraft";

/** Чёткость картинки страницы: выше обычной, чтобы при приближении текст не «мылился». */
const IMAGE_SCALE = 2;
const DRAFT_OUTLINE = "1px dashed rgba(22, 163, 74, 0.85)";
const REVERT_OUTLINE = "1px dashed rgba(100, 116, 139, 0.9)";

const STATUS_LABEL = {
  saved: "Нажмите, чтобы изменить",
  modified: "Изменена · не сохранено",
  new: "Новая · не сохранено",
  revert: "Будет удалена при сохранении"
};

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
  const [historyOpen, setHistoryOpen] = useState(false);
  const [replacementText, setReplacementText] = useState("");
  /** Черновик: правки копятся здесь и уходят на сервер одним запросом. */
  const [draft, setDraft] = useState({});
  const [zoom, setZoom] = useState(1);
  const [confirm, setConfirm] = useState(null); // null | "close" | "discard"
  const [scroller, setScroller] = useState(null);

  const draftRef = useRef({});
  const zoomRef = useRef(1);
  const zoomAnchorRef = useRef(null);
  const loadTokenRef = useRef(0);
  const editingKeyRef = useRef("");
  const suppressBlurRef = useRef(false);
  const saveDraftRef = useRef(null);

  zoomRef.current = zoom;
  const title = summaryItem?.title || "Сводка";
  const dirtyCount = draftCount(draft);

  const updateDraft = useCallback((updater) => {
    draftRef.current = updater(draftRef.current);
    setDraft(draftRef.current);
  }, []);

  const draftByEditId = useMemo(() => {
    const map = new Map();
    for (const entry of Object.values(draft)) {
      if (entry.editId) map.set(String(entry.editId), entry);
    }
    return map;
  }, [draft]);

  const rows = useMemo(() => mergeEditRows(edits, draft), [edits, draft]);

  /** Запись черновика для ячейки (по ключу или по сохранённой правке в этом месте). */
  const draftEntryFor = useCallback(
    (cell, existing) =>
      draft[String(cell.cellKey)] || (existing ? draftByEditId.get(String(existing.id)) : undefined),
    [draft, draftByEditId]
  );

  const loadEditor = useCallback(
    async (silent = false) => {
      if (!summaryItem?.id) return false;
      if (summaryIsExcel(summaryItem)) {
        setError("Правки ячеек доступны только для PDF.");
        setPdfPages([]);
        setEdits([]);
        return false;
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
          parsePdfCellsFromUrl(previewUrl, { imageScale: IMAGE_SCALE })
        ]);
        if (isStale()) return false;
        setEdits(Array.isArray(serverState?.edits) ? serverState.edits : []);
        setReportId(String(serverState?.report_id || ""));
        setPdfPages(Array.isArray(pages) ? pages : []);
        return true;
      } catch (e) {
        if (!isStale()) {
          setError(
            String(e?.response?.data?.detail || e?.message || "Не удалось загрузить редактор правок.")
          );
          if (!silent) {
            setPdfPages([]);
            setEdits([]);
          }
        }
        return false;
      } finally {
        if (!isStale() && !silent) setLoading(false);
      }
    },
    [summaryItem, accessToken]
  );

  // Новое открытие диалога / другая сводка — чистый черновик и масштаб.
  useEffect(() => {
    if (!open) return;
    draftRef.current = {};
    setDraft({});
    setZoom(1);
    setConfirm(null);
  }, [open, summaryItem?.id]);

  useEffect(() => {
    if (!open) return undefined;
    setSelectedCell(null);
    setInputOpen(false);
    setReplacementText("");
    loadEditor();
    return () => {
      loadTokenRef.current += 1;
    };
  }, [open, loadEditor]);

  // Не даём случайно закрыть вкладку с несохранёнными правками.
  useEffect(() => {
    if (!open || dirtyCount === 0) return undefined;
    const handler = (event) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [open, dirtyCount]);

  // Ctrl/Cmd+S — сохранить правки.
  useEffect(() => {
    if (!open) return undefined;
    const handler = (event) => {
      if ((event.ctrlKey || event.metaKey) && String(event.key).toLowerCase() === "s") {
        event.preventDefault();
        saveDraftRef.current?.();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open]);

  // ---- масштаб ----------------------------------------------------------------------------
  const changeZoom = useCallback(
    (next, anchor) => {
      const target = clampZoom(next);
      const current = zoomRef.current;
      if (target === current) return;
      const el = scroller;
      zoomAnchorRef.current = {
        ratio: target / current,
        x: anchor?.x ?? (el ? el.clientWidth / 2 : 0),
        y: anchor?.y ?? (el ? el.clientHeight / 2 : 0)
      };
      zoomRef.current = target;
      setZoom(target);
    },
    [scroller]
  );

  // После смены масштаба удерживаем под курсором (или по центру) то же место страницы.
  useLayoutEffect(() => {
    const info = zoomAnchorRef.current;
    if (!info || !scroller) return;
    zoomAnchorRef.current = null;
    // При 100% и меньше страница целиком помещается по ширине — горизонтальный сдвиг не нужен.
    scroller.scrollLeft = zoom <= 1 ? 0 : (scroller.scrollLeft + info.x) * info.ratio - info.x;
    scroller.scrollTop = (scroller.scrollTop + info.y) * info.ratio - info.y;
  }, [zoom, scroller]);

  // Ctrl/Cmd + колесо (в т.ч. щипок на трекпаде) — масштаб. Нужен не-passive слушатель.
  useEffect(() => {
    if (!scroller) return undefined;
    const onWheel = (event) => {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      const rect = scroller.getBoundingClientRect();
      changeZoom(zoomRef.current * Math.exp(-event.deltaY * 0.002), {
        x: event.clientX - rect.left,
        y: event.clientY - rect.top
      });
    };
    scroller.addEventListener("wheel", onWheel, { passive: false });
    return () => scroller.removeEventListener("wheel", onWheel);
  }, [scroller, changeZoom]);

  // ---- редактирование ---------------------------------------------------------------------
  function startEdit(cell) {
    const existing = findEditForCell(edits, cell);
    // Для уже изменённой ячейки исходное значение берём из правки, а не из текущего PDF.
    const normalized = existing ? { ...cell, cellText: existing.cell_text || cell.cellText } : cell;
    const entry =
      draftRef.current[String(cell.cellKey)] ||
      (existing
        ? Object.values(draftRef.current).find((d) => String(d.editId) === String(existing.id))
        : undefined);
    editingKeyRef.current = String(cell.cellKey);
    suppressBlurRef.current = false;
    setSelectedCell(normalized);
    setReplacementText(entry ? entry.replacement : existing?.replacement || cell.cellText || "");
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
    if (saving) return;
    const cell = row.edit
      ? findCellForEdit(pdfPages, row.edit)
      : draftRef.current[row.draftKey]?.cell;
    if (cell) startEdit(cell);
  }

  /** Записать значение из поля в черновик (на сервер ничего не уходит).
   * move: none — закрыть поле; down/up — по столбцу (как Enter в Excel); next/prev — по порядку чтения. */
  function commitEdit(move = "none") {
    if (!selectedCell) return;
    const cell = selectedCell;
    const text = String(replacementText || "").trim();
    if (!text) {
      setError("Значение не может быть пустым.");
      return;
    }
    updateDraft((prev) => applyDraftValue(prev, edits, cell, text));
    setError("");
    setSuccess("");
    let target = null;
    if (move === "down") target = verticalNeighbor(pdfPages, cell, 1);
    else if (move === "up") target = verticalNeighbor(pdfPages, cell, -1);
    else if (move === "next") target = readingNeighbor(pdfPages, cell, 1);
    else if (move === "prev") target = readingNeighbor(pdfPages, cell, -1);
    if (target) startEdit(target);
    else if (editingKeyRef.current === String(cell.cellKey)) closeEdit();
  }

  function onInputKeyDown(event) {
    if ((event.ctrlKey || event.metaKey) && String(event.key).toLowerCase() === "s") {
      event.preventDefault();
      event.stopPropagation();
      suppressBlurRef.current = true;
      commitEdit("none");
      saveDraftRef.current?.();
    } else if (event.key === "Enter") {
      event.preventDefault();
      suppressBlurRef.current = true;
      commitEdit(event.shiftKey ? "up" : "down");
    } else if (event.key === "Tab") {
      event.preventDefault();
      suppressBlurRef.current = true;
      commitEdit(event.shiftKey ? "prev" : "next");
    } else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      suppressBlurRef.current = true;
      closeEdit();
    }
  }

  /** Убрать запись из черновика (вернуть сохранённое значение). */
  function discardDraftEntry(draftKey) {
    updateDraft((prev) => {
      const next = { ...prev };
      delete next[draftKey];
      return next;
    });
  }

  /** Отменить уже сохранённую правку: в черновик ложится исходное значение, на сервере правка
   * удалится при «Сохранить правки». */
  function cancelSavedEdit(row) {
    const cell = findCellForEdit(pdfPages, row.edit);
    if (!cell) return;
    updateDraft((prev) => applyDraftValue(prev, edits, cell, row.edit.cell_text));
    setError("");
    setSuccess("");
  }

  function discardAll() {
    draftRef.current = {};
    setDraft({});
    closeEdit();
    setError("");
    setSuccess("");
  }

  /** Сохранить весь черновик одним запросом. Возвращает true при успехе. */
  async function saveDraft() {
    if (!summaryItem?.id || saving) return false;
    const current = draftRef.current;
    const { edits: upserts, deleteIds } = buildBatchPayload(current, edits);
    if (!upserts.length && !deleteIds.length) return true;
    setSaving(true);
    setError("");
    setSuccess("");
    try {
      const result = await saveSummaryCellEditsBatch({
        summaryId: Number(summaryItem.id),
        edits: upserts,
        deleteIds
      });
      closeEdit();
      // Черновик убираем только после перезагрузки PDF — пока она идёт, новые значения видны.
      const reloaded = await loadEditor(true);
      if (!reloaded && Array.isArray(result?.edits)) setEdits(result.edits);
      draftRef.current = {};
      setDraft({});
      const parts = [];
      if (upserts.length) parts.push(`изменено: ${upserts.length}`);
      if (deleteIds.length) parts.push(`отменено: ${deleteIds.length}`);
      setSuccess(
        reloaded
          ? `Сохранено (${parts.join(", ")}), PDF обновлён.`
          : `Сохранено (${parts.join(", ")}), но превью не обновилось — нажмите «Обновить».`
      );
      onSaved?.();
      return true;
    } catch (e) {
      // Черновик остаётся нетронутым — можно повторить.
      setError(String(e?.response?.data?.detail || e?.message || "Не удалось сохранить правки."));
      return false;
    } finally {
      setSaving(false);
    }
  }
  saveDraftRef.current = saveDraft;

  function requestClose() {
    if (saving) return;
    if (draftCount(draftRef.current) > 0) {
      setConfirm("close");
      return;
    }
    onClose?.();
  }

  async function saveAndClose() {
    setConfirm(null);
    if (await saveDraft()) onClose?.();
  }

  function discardAndClose() {
    setConfirm(null);
    discardAll();
    onClose?.();
  }

  // ---- отрисовка ячеек --------------------------------------------------------------------
  function renderCell(cell, aspect) {
    const key = String(cell.cellKey);
    const existing = findEditForCell(edits, cell);
    const entry = draftEntryFor(cell, existing);
    const changed = isCellChanged(cell, existing, entry);
    const editing = inputOpen && String(selectedCell?.cellKey) === key;
    const geom = {
      left: `${cell.leftPct * 100}%`,
      top: `${cell.topPct * 100}%`,
      width: `${Math.max(0.6, cell.widthPct * 100)}%`,
      height: `${Math.max(1, cell.heightPct * 100)}%`
    };
    const fontSize = `max(7px, ${cellFontCqw(cell, aspect).toFixed(3)}cqw)`;

    if (editing) {
      // Ячейка узкая, а поле не может быть меньше ~36px. Расти оно должно от центра
      // цифры: если оставить левый край на месте, текст с text-align:center уезжает вправо.
      const cx = (cell.leftPct + cell.widthPct / 2) * 100;
      const cy = (cell.topPct + cell.heightPct / 2) * 100;
      return (
        <div
          key={key}
          style={{
            position: "absolute",
            left: `${cx}%`,
            top: `${cy}%`,
            transform: "translate(-50%, -50%)",
            width: `max(${geom.width}, 36px)`,
            height: `max(${geom.height}, 18px)`,
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
              commitEdit("none");
            }}
            style={{
              width: "100%",
              height: "100%",
              boxSizing: "border-box",
              margin: 0,
              padding: 0,
              font: `${fontSize}/1 Arial, Helvetica, sans-serif`,
              fontVariantNumeric: "tabular-nums",
              textAlign: "center",
              color: "#111827",
              background: "#ffffff",
              border: "2px solid #2563EB",
              borderRadius: 2,
              outline: "none",
              boxShadow: "0 2px 8px rgba(0,0,0,0.25)"
            }}
          />
        </div>
      );
    }

    if (entry) {
      // Несохранённое значение: показываем его поверх страницы сразу, до обращения к серверу.
      const cx = (cell.leftPct + cell.widthPct / 2) * 100;
      return (
        <div
          key={key}
          className={`cell-draft ${changed ? "cell-draft-changed" : "cell-draft-reverted"}`}
          onClick={() => onCellClick(cell)}
          title={`${originalText(cell, existing)} → ${entry.replacement} (не сохранено)`}
          style={{
            position: "absolute",
            left: `${cx}%`,
            top: geom.top,
            transform: "translateX(-50%)",
            minWidth: geom.width,
            width: "max-content",
            height: geom.height,
            fontSize,
            color: cell.textColor || "#111827",
            backgroundColor: cell.fillColor || "#ffffff"
          }}
        >
          {entry.replacement}
        </div>
      );
    }

    return (
      <div
        key={key}
        className={`cell-hit${changed ? " cell-changed" : ""}`}
        onClick={() => onCellClick(cell)}
        title={changed ? `${originalText(cell, existing)} → ${currentValueText(cell, existing)}` : cell.cellText}
        style={{ ...geom, position: "absolute" }}
      />
    );
  }

  function currentValueText(cell, existing) {
    return existing ? existing.replacement : cell.cellText;
  }

  const zoomPercent = `${Math.round(zoom * 100)}%`;
  const statusColor = error ? "error" : success ? "success.main" : dirtyCount ? "warning.main" : "text.secondary";
  const statusText = saving
    ? "Сохранение…"
    : error || success || (dirtyCount ? `Несохранённых правок: ${dirtyCount}` : "");

  return (
    <>
      <Dialog
        open={open}
        onClose={requestClose}
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

        <Box
          sx={{
            px: 3,
            py: 1,
            borderTop: 1,
            borderColor: "divider",
            display: "flex",
            flexWrap: "wrap",
            alignItems: "center",
            gap: 1.5
          }}
        >
          <Stack direction="row" spacing={0.25} alignItems="center">
            <Tooltip title="Уменьшить">
              <span>
                <IconButton
                  size="small"
                  onClick={() => changeZoom(stepZoom(zoom, -1))}
                  disabled={loading || zoom <= 0.5}
                  aria-label="Уменьшить"
                >
                  <ZoomOutRoundedIcon fontSize="small" />
                </IconButton>
              </span>
            </Tooltip>
            <Typography
              variant="body2"
              sx={{ minWidth: 44, textAlign: "center", fontVariantNumeric: "tabular-nums" }}
            >
              {zoomPercent}
            </Typography>
            <Tooltip title="Увеличить">
              <span>
                <IconButton
                  size="small"
                  onClick={() => changeZoom(stepZoom(zoom, 1))}
                  disabled={loading || zoom >= 3}
                  aria-label="Увеличить"
                >
                  <ZoomInRoundedIcon fontSize="small" />
                </IconButton>
              </span>
            </Tooltip>
            <Tooltip title="По ширине окна (100%)">
              <span>
                <IconButton
                  size="small"
                  onClick={() => changeZoom(1)}
                  disabled={loading || zoom === 1}
                  aria-label="По ширине окна"
                >
                  <FitScreenRoundedIcon fontSize="small" />
                </IconButton>
              </span>
            </Tooltip>
          </Stack>

          <Stack direction="row" spacing={1.5} alignItems="center" sx={{ flex: 1, minWidth: 0 }}>
            <Stack direction="row" spacing={0.5} alignItems="center">
              <Box
                sx={{
                  width: 14,
                  height: 14,
                  borderRadius: 0.5,
                  bgcolor: CHANGED_TINT,
                  border: "1px solid rgba(22,163,74,0.45)"
                }}
              />
              <Typography variant="caption" color="text.secondary">
                изменено
              </Typography>
            </Stack>
            <Stack direction="row" spacing={0.5} alignItems="center">
              <Box
                sx={{
                  width: 14,
                  height: 14,
                  borderRadius: 0.5,
                  bgcolor: CHANGED_TINT,
                  border: DRAFT_OUTLINE
                }}
              />
              <Typography variant="caption" color="text.secondary">
                не сохранено
              </Typography>
            </Stack>
          </Stack>

          <Button
            size="small"
            startIcon={<RefreshRoundedIcon />}
            onClick={() => loadEditor()}
            disabled={loading || saving}
          >
            Обновить
          </Button>
        </Box>

        <DialogContent
          dividers
          sx={{
            flex: 1,
            minHeight: 0,
            p: 0,
            overflow: "hidden",
            display: "flex",
            flexDirection: { xs: "column", md: "row" }
          }}
        >
          <Box
            ref={setScroller}
            sx={{
              flex: "1 1 0",
              minWidth: 0,
              minHeight: 0,
              overflow: "auto",
              p: 2,
              // Подсветка и состояния ячеек: стили заданы один раз, а не на каждую ячейку.
              "& .cell-hit": { cursor: "text", pointerEvents: "auto", boxSizing: "border-box" },
              "& .cell-hit:hover": {
                outline: "1px solid #2563EB",
                backgroundColor: "rgba(37,99,235,0.12)"
              },
              "& .cell-changed": {
                backgroundColor: CHANGED_TINT,
                boxShadow: "0 0 0 1px rgba(34,197,94,0.14)"
              },
              "& .cell-draft": {
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                boxSizing: "border-box",
                padding: 0,
                lineHeight: 1,
                whiteSpace: "nowrap",
                fontFamily: "Arial, Helvetica, sans-serif",
                fontVariantNumeric: "tabular-nums",
                cursor: "text",
                pointerEvents: "auto",
                zIndex: 2
              },
              "& .cell-draft-changed": {
                backgroundImage: `linear-gradient(${CHANGED_TINT}, ${CHANGED_TINT})`,
                outline: DRAFT_OUTLINE
              },
              "& .cell-draft-reverted": { outline: REVERT_OUTLINE },
              "& .cell-draft:hover": { outline: "1px solid #2563EB" }
            }}
          >
            {loading ? (
              <Stack direction="row" spacing={1} alignItems="center" sx={{ py: 4 }}>
                <CircularProgress size={22} />
                <Typography color="text.secondary">Загрузка PDF и разметки ячеек…</Typography>
              </Stack>
            ) : (
              <>
                <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
                  Кликните по значению и введите новое. Enter — вниз, Tab — вправо, Esc — отмена.
                  Изменения копятся в черновике и применяются кнопкой «Сохранить правки» (Ctrl+S).
                  Ctrl + колесо мыши — масштаб.
                </Typography>
                <Stack
                  spacing={2}
                  sx={{ width: `${zoom * 100}%`, mx: zoom < 1 ? "auto" : 0 }}
                >
                  {pdfPages.map((page) => {
                    const aspect = page.width > 0 ? page.height / page.width : 1.414;
                    return (
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
                            borderRadius: 1,
                            bgcolor: "#fff",
                            containerType: "inline-size"
                          }}
                        >
                          <Box
                            component="img"
                            src={page.imageDataUrl}
                            alt={`page-${page.page}`}
                            sx={{ display: "block", width: "100%", height: "auto" }}
                          />
                          <Box sx={{ position: "absolute", inset: 0, pointerEvents: "none" }}>
                            {page.cells.map((cell) => renderCell(cell, aspect))}
                          </Box>
                        </Box>
                      </Box>
                    );
                  })}
                </Stack>
                {!pdfPages.length ? <Alert severity="info">Нет страниц для отображения.</Alert> : null}
              </>
            )}
          </Box>

          <Box
            sx={{
              flex: { xs: "0 0 auto", md: "0 0 300px" },
              maxHeight: { xs: "38%", md: "none" },
              overflow: "auto",
              borderLeft: { md: 1 },
              borderTop: { xs: 1, md: 0 },
              borderColor: "divider",
              p: 1.5,
              bgcolor: (t) =>
                t.palette.mode === "light" ? t.palette.grey[50] : "rgba(255,255,255,0.03)"
            }}
          >
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }} flexWrap="wrap">
              <Typography variant="subtitle2">Правки</Typography>
              <Chip size="small" label={rows.length} variant="outlined" />
              {dirtyCount ? (
                <Chip size="small" color="warning" variant="outlined" label={`не сохранено: ${dirtyCount}`} />
              ) : null}
            </Stack>
            {rows.length === 0 ? (
              <Typography variant="body2" color="text.secondary">
                Пока нет правок. Выберите ячейку на странице.
              </Typography>
            ) : (
              <List dense disablePadding>
                {rows.map((row) => {
                  const unsaved = row.status !== "saved";
                  return (
                    <ListItem
                      key={row.id}
                      disableGutters
                      secondaryAction={
                        row.status === "saved" ? (
                          <Tooltip title="Отменить правку (применится при сохранении)">
                            <span>
                              <IconButton
                                edge="end"
                                size="small"
                                disabled={saving}
                                onClick={() => cancelSavedEdit(row)}
                                aria-label="Отменить правку"
                              >
                                <DeleteOutlineRoundedIcon fontSize="small" />
                              </IconButton>
                            </span>
                          </Tooltip>
                        ) : (
                          <Tooltip title={row.status === "revert" ? "Вернуть правку" : "Сбросить изменение"}>
                            <span>
                              <IconButton
                                edge="end"
                                size="small"
                                disabled={saving}
                                onClick={() => discardDraftEntry(row.draftKey)}
                                aria-label="Сбросить изменение"
                              >
                                <UndoRoundedIcon fontSize="small" />
                              </IconButton>
                            </span>
                          </Tooltip>
                        )
                      }
                      sx={{ alignItems: "flex-start", pr: 5 }}
                    >
                      <ListItemText
                        primary={`Стр. ${row.page}: ${row.from || "—"} → ${row.to}`}
                        secondary={STATUS_LABEL[row.status]}
                        primaryTypographyProps={{
                          variant: "body2",
                          sx: row.status === "revert" ? { textDecoration: "line-through" } : undefined
                        }}
                        secondaryTypographyProps={{
                          variant: "caption",
                          color: unsaved ? "warning.main" : "text.secondary"
                        }}
                        onClick={() => openEditFromRow(row)}
                        sx={{ cursor: "pointer", mr: 1 }}
                      />
                    </ListItem>
                  );
                })}
              </List>
            )}
          </Box>
        </DialogContent>

        <DialogActions sx={{ px: 2, py: 1.5, gap: 1 }}>
          {/* Статус внизу, чтобы страницы не «прыгали» при каждом сохранении. */}
          <Typography variant="body2" color={statusColor} sx={{ flex: 1, minWidth: 0 }}>
            {statusText}
          </Typography>
          <Button
            color="inherit"
            startIcon={<HistoryRoundedIcon />}
            onClick={() => setHistoryOpen(true)}
          >
            История
          </Button>
          <Button
            color="inherit"
            startIcon={<UndoRoundedIcon />}
            onClick={() => setConfirm("discard")}
            disabled={saving || dirtyCount === 0}
          >
            Сбросить
          </Button>
          <Button onClick={requestClose} disabled={saving}>
            Закрыть
          </Button>
          <Button
            variant="contained"
            startIcon={
              saving ? <CircularProgress size={16} color="inherit" /> : <SaveRoundedIcon />
            }
            onClick={() => saveDraft()}
            disabled={saving || dirtyCount === 0}
          >
            {dirtyCount ? `Сохранить правки (${dirtyCount})` : "Сохранить правки"}
          </Button>
        </DialogActions>
      </Dialog>

      <DocumentHistoryDialog
        open={historyOpen}
        onClose={() => setHistoryOpen(false)}
        summaryId={summaryItem?.id ? Number(summaryItem.id) : null}
        title={title}
      />

      <Dialog open={confirm !== null} onClose={() => setConfirm(null)} maxWidth="xs" fullWidth>
        <DialogTitle>
          {confirm === "discard" ? "Сбросить изменения?" : "Есть несохранённые правки"}
        </DialogTitle>
        <DialogContent>
          <DialogContentText>
            {confirm === "discard"
              ? `Несохранённые изменения (${dirtyCount}) будут отброшены. Сохранённые ранее правки не изменятся.`
              : `Несохранённых правок: ${dirtyCount}. Сохранить их перед закрытием?`}
          </DialogContentText>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2, flexWrap: "wrap", gap: 1 }}>
          <Button onClick={() => setConfirm(null)}>Продолжить редактирование</Button>
          {confirm === "discard" ? (
            <Button
              color="error"
              onClick={() => {
                setConfirm(null);
                discardAll();
              }}
            >
              Сбросить
            </Button>
          ) : (
            <>
              <Button color="error" onClick={discardAndClose}>
                Не сохранять
              </Button>
              <Button variant="contained" onClick={saveAndClose}>
                Сохранить и закрыть
              </Button>
            </>
          )}
        </DialogActions>
      </Dialog>
    </>
  );
}
