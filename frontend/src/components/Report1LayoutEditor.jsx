import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Alert from "@mui/material/Alert";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import FormControl from "@mui/material/FormControl";
import InputLabel from "@mui/material/InputLabel";
import MenuItem from "@mui/material/MenuItem";
import Select from "@mui/material/Select";
import TextField from "@mui/material/TextField";
import List from "@mui/material/List";
import ListItemButton from "@mui/material/ListItemButton";
import ListItemText from "@mui/material/ListItemText";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Grid from "@mui/material/Grid";
import IconButton from "@mui/material/IconButton";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import DownloadRoundedIcon from "@mui/icons-material/DownloadRounded";
import AddRoundedIcon from "@mui/icons-material/AddRounded";
import CloseRoundedIcon from "@mui/icons-material/CloseRounded";
import DragIndicatorRoundedIcon from "@mui/icons-material/DragIndicatorRounded";
import RestartAltRoundedIcon from "@mui/icons-material/RestartAltRounded";
import SaveRoundedIcon from "@mui/icons-material/SaveRounded";
import VisibilityOffRoundedIcon from "@mui/icons-material/VisibilityOffRounded";
import VisibilityRoundedIcon from "@mui/icons-material/VisibilityRounded";
import {
  downloadReport1PageOrderPreview,
  fetchReport1PageOrder,
  fetchSummariesByDate,
  getPageOrderSourcePdfUrl,
  getPreviewUrl,
  reportIdFromSummaryTitle,
  resetReport1PageOrder,
  saveReport1PageOrder,
  summaryIsExcel
} from "../api";
import { loadPdfPageThumbnails, loadSinglePdfPageThumbnail, getPdfPageCount } from "./pdfPageThumbnails";

function reorderByPageOrder(thumbnails, pageOrder) {
  if (!Array.isArray(thumbnails) || thumbnails.length === 0) return [];
  if (!Array.isArray(pageOrder) || pageOrder.length !== thumbnails.length) {
    return thumbnails.map((item) => ({ ...item, originalPage: item.page }));
  }
  const byPage = new Map(thumbnails.map((item) => [item.page, item]));
  return pageOrder
    .map((pageNum) => {
      const src = byPage.get(pageNum);
      if (!src) return null;
      return { ...src, originalPage: pageNum };
    })
    .filter(Boolean);
}

function pageOrderFromItems(items) {
  return items.filter((item) => item.kind !== "graft").map((item) => item.originalPage);
}

function slotsFromItems(items) {
  return items.map((item) => {
    if (item.kind === "graft") {
      return {
        kind: "graft",
        donor_report: item.donorReport,
        donor_page: item.donorPage,
        ...(item.label ? { label: item.label } : {})
      };
    }
    return { kind: "own", page: item.originalPage };
  });
}

function buildSlotsFromState(savedState) {
  if (Array.isArray(savedState?.page_slots) && savedState.page_slots.length) {
    return savedState.page_slots;
  }
  if (Array.isArray(savedState?.page_order) && savedState.page_order.length) {
    return savedState.page_order.map((page) => ({ kind: "own", page }));
  }
  return null;
}

function slotsEqual(a, b) {
  if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
  return a.every((slot, index) => JSON.stringify(slot) === JSON.stringify(b[index]));
}

function itemKey(item, index) {
  if (item.kind === "graft") {
    return `graft-${item.donorReport}-${item.donorPage}-${index}`;
  }
  return `own-${item.originalPage}`;
}

async function buildItemsFromSlots(slots, thumbnailByPage, summaries) {
  const items = [];
  for (const slot of slots) {
    if (slot.kind === "own") {
      const src = thumbnailByPage.get(slot.page);
      if (src) {
        items.push({ ...src, kind: "own", originalPage: slot.page });
      } else {
        items.push({
          kind: "own",
          originalPage: slot.page,
          page: slot.page,
          label: `Страница ${slot.page}`,
          imageDataUrl: "",
          loading: true
        });
      }
      continue;
    }
    if (slot.kind !== "graft") continue;
    const donorReport = String(slot.donor_report || "");
    const donorPage = Number(slot.donor_page) || 1;
    const donor = (summaries || []).find(
      (row) =>
        reportIdFromSummaryTitle(row?.title) === donorReport &&
        row?.visible !== false &&
        !summaryIsExcel(row)
    );
    items.push({
      kind: "graft",
      donorReport,
      donorPage,
      label: slot.label || `Сводка ${donorReport}, стр. ${donorPage}`,
      imageDataUrl: "",
      originalPage: null,
      donorAvailable: Boolean(donor),
      loading: Boolean(donor)
    });
  }
  return items;
}

function placeholderOwnItems(count) {
  return Array.from({ length: count }, (_, index) => {
    const page = index + 1;
    return {
      kind: "own",
      page,
      originalPage: page,
      label: `Страница ${page}`,
      imageDataUrl: "",
      loading: true
    };
  });
}

async function hydrateGraftThumbnails(items, summaries, accessToken, shouldCancel) {
  const updates = [...items];
  let changed = false;
  for (let index = 0; index < updates.length; index += 1) {
    if (shouldCancel?.()) return updates;
    const item = updates[index];
    if (item.kind !== "graft" || !item.donorAvailable || item.imageDataUrl) continue;
    const donor = (summaries || []).find(
      (row) =>
        reportIdFromSummaryTitle(row?.title) === item.donorReport &&
        row?.visible !== false &&
        !summaryIsExcel(row)
    );
    if (!donor) continue;
    try {
      const thumb = await loadSinglePdfPageThumbnail(
        getPreviewUrl(donor, accessToken),
        item.donorPage,
        { scale: 0.32, withLabels: false }
      );
      updates[index] = {
        ...item,
        imageDataUrl: thumb.imageDataUrl,
        label: item.label || thumb.label,
        loading: false
      };
      changed = true;
    } catch {
      updates[index] = { ...item, loading: false };
      changed = true;
    }
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  return changed ? updates : items;
}

function hiddenPagesFromItems(items) {
  return items.map((item) => item.originalPage).sort((a, b) => a - b);
}

function sameIntList(a, b) {
  if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
  return a.every((value, index) => value === b[index]);
}

function moveItem(list, fromIndex, toIndex) {
  if (fromIndex === toIndex || fromIndex < 0 || toIndex < 0 || fromIndex >= list.length || toIndex >= list.length) {
    return list;
  }
  const next = [...list];
  const [removed] = next.splice(fromIndex, 1);
  next.splice(toIndex, 0, removed);
  return next;
}

function PageContentsTable({ items, highlightIndex, onSelectIndex, onHideIndex, onRemoveIndex }) {
  return (
    <TableContainer
      sx={{
        height: "100%",
        maxHeight: "100%",
        border: 1,
        borderColor: "divider",
        borderRadius: 1,
        bgcolor: (t) =>
          t.palette.mode === "light" ? t.palette.background.paper : "rgba(255,255,255,0.04)"
      }}
    >
      <Table size="small" stickyHeader>
        <TableHead>
          <TableRow>
            <TableCell
              width={72}
              sx={{ fontWeight: 600, bgcolor: (t) => t.palette.background.paper }}
            >
              Позиция
            </TableCell>
            <TableCell
              width={88}
              sx={{ fontWeight: 600, bgcolor: (t) => t.palette.background.paper }}
            >
              Исходная стр.
            </TableCell>
            <TableCell sx={{ fontWeight: 600, bgcolor: (t) => t.palette.background.paper }}>
              Содержание страницы
            </TableCell>
            {onHideIndex || onRemoveIndex ? (
              <TableCell
                width={48}
                sx={{ fontWeight: 600, bgcolor: (t) => t.palette.background.paper }}
              />
            ) : null}
          </TableRow>
        </TableHead>
        <TableBody>
          {items.map((item, index) => {
            const isGraft = item.kind === "graft";
            const moved = !isGraft && item.originalPage !== index + 1;
            return (
              <TableRow
                key={itemKey(item, index)}
                hover
                selected={highlightIndex === index}
                onClick={() => onSelectIndex?.(index)}
                sx={{ cursor: onSelectIndex ? "pointer" : "default" }}
              >
                <TableCell>
                  <Chip
                    size="small"
                    label={index + 1}
                    color={isGraft ? "info" : moved ? "warning" : "primary"}
                    variant={isGraft || moved ? "filled" : "outlined"}
                  />
                </TableCell>
                <TableCell>
                  <Typography variant="body2" fontWeight={600}>
                    {isGraft ? `св.${item.donorReport} стр.${item.donorPage}` : item.originalPage}
                  </Typography>
                </TableCell>
                <TableCell>
                  <Typography variant="body2" noWrap title={item.label}>
                    {item.label}
                  </Typography>
                </TableCell>
                {onHideIndex || onRemoveIndex ? (
                  <TableCell align="center" onClick={(event) => event.stopPropagation()}>
                    {isGraft ? (
                      <Tooltip title="Убрать подстановку">
                        <IconButton size="small" onClick={() => onRemoveIndex?.(index)}>
                          <CloseRoundedIcon fontSize="small" />
                        </IconButton>
                      </Tooltip>
                    ) : (
                      <Tooltip title="Скрыть страницу">
                        <IconButton size="small" onClick={() => onHideIndex?.(index)}>
                          <VisibilityOffRoundedIcon fontSize="small" />
                        </IconButton>
                      </Tooltip>
                    )}
                  </TableCell>
                ) : null}
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </TableContainer>
  );
}

function PageThumb({ item, index, highlighted, onDragStart, onDragOver, onDrop, onDragEnd, onHide, onRemove }) {
  const isGraft = item.kind === "graft";
  const moved = !isGraft && item.originalPage !== index + 1;
  return (
    <Paper
      variant="outlined"
      draggable
      onDragStart={(event) => onDragStart(event, index)}
      onDragOver={(event) => onDragOver(event, index)}
      onDrop={(event) => onDrop(event, index)}
      onDragEnd={onDragEnd}
      sx={{
        position: "relative",
        cursor: "grab",
        overflow: "hidden",
        borderColor: highlighted
          ? "primary.main"
          : isGraft
            ? "info.main"
            : moved
              ? "warning.main"
              : "divider",
        borderWidth: highlighted ? 2 : 1,
        bgcolor: (t) =>
          isGraft
            ? t.palette.mode === "light"
              ? "rgba(33, 150, 243, 0.08)"
              : "rgba(33, 150, 243, 0.14)"
            : moved
              ? t.palette.mode === "light"
                ? "warning.50"
                : "rgba(251, 191, 36, 0.12)"
              : t.palette.background.paper,
        "&:active": { cursor: "grabbing" }
      }}
    >
      <Box
        sx={{
          position: "absolute",
          top: 0,
          left: 0,
          right: 0,
          zIndex: 1,
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 0.5,
          px: 0.75,
          py: 0.5,
          bgcolor: (t) =>
            t.palette.mode === "light" ? "rgba(255,255,255,0.92)" : "rgba(24, 26, 34, 0.92)",
          borderBottom: 1,
          borderColor: "divider"
        }}
      >
        <Stack direction="row" spacing={0.5} alignItems="center" useFlexGap flexWrap="wrap">
          <Chip size="small" color={isGraft ? "info" : moved ? "warning" : "primary"} label={`Поз. ${index + 1}`} />
          {isGraft ? (
            <Chip
              size="small"
              variant="outlined"
              color="info"
              label={`из св.${item.donorReport} стр.${item.donorPage}`}
              sx={{ bgcolor: "background.paper" }}
            />
          ) : (
            <Chip
              size="small"
              variant="outlined"
              label={`Исходная ${item.originalPage}`}
              sx={{ bgcolor: "background.paper" }}
            />
          )}
        </Stack>
        <Stack direction="row" spacing={0.25} alignItems="center">
          {isGraft && onRemove ? (
            <Tooltip title="Убрать подстановку">
              <IconButton
                size="small"
                onClick={(event) => {
                  event.stopPropagation();
                  onRemove(index);
                }}
              >
                <CloseRoundedIcon fontSize="small" />
              </IconButton>
            </Tooltip>
          ) : null}
          {!isGraft && onHide ? (
            <Tooltip title="Скрыть страницу">
              <IconButton
                size="small"
                onClick={(event) => {
                  event.stopPropagation();
                  onHide(index);
                }}
              >
                <VisibilityOffRoundedIcon fontSize="small" />
              </IconButton>
            </Tooltip>
          ) : null}
          <DragIndicatorRoundedIcon fontSize="small" sx={{ color: "text.secondary", flexShrink: 0 }} />
        </Stack>
      </Box>
      <Box sx={{ pt: 4.5, minHeight: 120 }}>
        {item.imageDataUrl ? (
          <Box
            component="img"
            src={item.imageDataUrl}
            alt={item.label}
            draggable={false}
            sx={{ display: "block", width: "100%", height: "auto", userSelect: "none" }}
          />
        ) : (
          <Box
            sx={{
              minHeight: 120,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              px: 2,
              bgcolor: (t) => (t.palette.mode === "light" ? t.palette.grey[100] : "rgba(255,255,255,0.04)")
            }}
          >
            <Typography variant="caption" color="text.secondary" align="center">
              {isGraft ? "PDF донора недоступен на эту дату" : "Нет превью"}
            </Typography>
          </Box>
        )}
      </Box>
      <Box
        sx={{
          px: 1,
          py: 0.75,
          borderTop: 1,
          borderColor: "divider",
          bgcolor: (t) =>
            t.palette.mode === "light" ? t.palette.grey[50] : "rgba(255,255,255,0.04)"
        }}
      >
        <Typography variant="caption" color="text.secondary" display="block" lineHeight={1.2}>
          {isGraft ? `Подстановка из сводки ${item.donorReport}` : `Исходная стр. ${item.originalPage}`}
        </Typography>
        <Typography
          variant="body2"
          fontWeight={500}
          sx={{
            mt: 0.25,
            display: "-webkit-box",
            WebkitLineClamp: 2,
            WebkitBoxOrient: "vertical",
            overflow: "hidden"
          }}
          title={item.label}
        >
          {item.label}
        </Typography>
      </Box>
    </Paper>
  );
}

function HiddenPageThumb({ item, onShow }) {
  return (
    <Paper
      variant="outlined"
      sx={{
        overflow: "hidden",
        borderColor: "divider",
        opacity: 0.72,
        bgcolor: (t) =>
          t.palette.mode === "light" ? t.palette.action.hover : "rgba(255,255,255,0.04)"
      }}
    >
      <Box
        sx={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 0.5,
          px: 0.75,
          py: 0.5,
          borderBottom: 1,
          borderColor: "divider"
        }}
      >
        <Chip size="small" variant="outlined" color="default" label={`Исходная ${item.originalPage}`} />
        <Tooltip title="Показать страницу">
          <IconButton size="small" onClick={() => onShow(item.originalPage)}>
            <VisibilityRoundedIcon fontSize="small" />
          </IconButton>
        </Tooltip>
      </Box>
      <Box sx={{ pt: 0.5 }}>
        <Box
          component="img"
          src={item.imageDataUrl}
          alt={`Скрытая страница ${item.originalPage}: ${item.label}`}
          draggable={false}
          sx={{
            display: "block",
            width: "100%",
            height: "auto",
            userSelect: "none",
            filter: "grayscale(0.85)"
          }}
        />
      </Box>
      <Box sx={{ px: 1, py: 0.75, borderTop: 1, borderColor: "divider" }}>
        <Typography variant="body2" fontWeight={500} noWrap title={item.label}>
          {item.label}
        </Typography>
      </Box>
    </Paper>
  );
}

function AddGraftDialog({ open, onClose, donorCatalog, onInsert, summaries = [], accessToken = "" }) {
  const [donorId, setDonorId] = useState("");
  const [customPage, setCustomPage] = useState("1");
  const [donorPageCount, setDonorPageCount] = useState(null);
  const selectedDonor = (donorCatalog || []).find((row) => row.id === donorId);

  useEffect(() => {
    if (!open) return;
    const first = (donorCatalog || [])[0];
    setDonorId(first?.id || "");
    setCustomPage("1");
  }, [open, donorCatalog]);

  useEffect(() => {
    let cancelled = false;
    async function loadCount() {
      setDonorPageCount(null);
      if (!donorId) return;
      const donor = (summaries || []).find(
        (row) =>
          reportIdFromSummaryTitle(row?.title) === donorId &&
          row?.visible !== false &&
          !summaryIsExcel(row)
      );
      if (!donor) return;
      try {
        const count = await getPdfPageCount(getPreviewUrl(donor, accessToken));
        if (!cancelled) setDonorPageCount(count);
      } catch {
        if (!cancelled) setDonorPageCount(null);
      }
    }
    loadCount();
    return () => {
      cancelled = true;
    };
  }, [donorId, summaries, accessToken]);

  function handlePickPage(pageRow) {
    if (!selectedDonor) return;
    onInsert({
      donorReport: selectedDonor.id,
      donorPage: pageRow.page,
      label: pageRow.desc || `Сводка ${selectedDonor.id}, стр. ${pageRow.page}`
    });
    onClose();
  }

  function handleCustomInsert() {
    if (!selectedDonor) return;
    const page = Math.max(1, Number.parseInt(customPage, 10) || 1);
    if (donorPageCount != null && page > donorPageCount) {
      return;
    }
    onInsert({
      donorReport: selectedDonor.id,
      donorPage: page,
      label: `Сводка ${selectedDonor.id}, стр. ${page}`
    });
    onClose();
  }

  const catalogPages = selectedDonor?.pages || [{ page: 1, desc: "Первая страница" }];
  const dynamicPages =
    donorPageCount != null && donorPageCount > 0
      ? Array.from({ length: donorPageCount }, (_, index) => {
          const page = index + 1;
          const hinted = catalogPages.find((row) => Number(row.page) === page);
          return {
            page,
            desc: hinted?.desc || `Страница ${page}`,
            sheet: hinted?.sheet || ""
          };
        })
      : catalogPages;

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>Добавить страницу из другой сводки</DialogTitle>
      <DialogContent dividers>
        <Stack spacing={2}>
          <Alert severity="info" sx={{ py: 0.5 }}>
            Страница попадёт в итоговую сводку только если соответствующая сводка включена в пачку
            генерации.
          </Alert>
          <FormControl fullWidth size="small">
            <InputLabel id="graft-donor-label">Сводка-донор</InputLabel>
            <Select
              labelId="graft-donor-label"
              label="Сводка-донор"
              value={donorId}
              onChange={(event) => setDonorId(event.target.value)}
            >
              {(donorCatalog || []).map((row) => (
                <MenuItem key={row.id} value={row.id}>
                  {row.title || row.label || row.id}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          <Typography variant="subtitle2">Страница</Typography>
          <List dense disablePadding sx={{ maxHeight: 280, overflow: "auto" }}>
            {dynamicPages.map((pageRow) => (
              <ListItemButton key={`${donorId}-${pageRow.page}`} onClick={() => handlePickPage(pageRow)}>
                <ListItemText
                  primary={`Стр. ${pageRow.page}${pageRow.desc ? ` — ${pageRow.desc}` : ""}`}
                  secondary={pageRow.sheet || null}
                />
              </ListItemButton>
            ))}
          </List>
          {donorPageCount == null ? (
            <Stack direction="row" spacing={1} alignItems="center">
              <TextField
                size="small"
                type="number"
                label="№ стр."
                value={customPage}
                onChange={(event) => setCustomPage(event.target.value)}
                inputProps={{ min: 1, max: 200 }}
                sx={{ width: 110 }}
              />
              <Button variant="outlined" onClick={handleCustomInsert} disabled={!selectedDonor}>
                Добавить стр. {customPage || "?"}
              </Button>
            </Stack>
          ) : null}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Отмена</Button>
      </DialogActions>
    </Dialog>
  );
}

export default function Report1LayoutEditor({
  reportId = "1",
  summaryDate,
  accessToken = "",
  onSavingChange,
  generationBatchReportIds = []
}) {
  const masterReportId = String(reportId || "1");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [summary, setSummary] = useState(null);
  const [savedState, setSavedState] = useState(null);
  const [pageItems, setPageItems] = useState([]);
  const [hiddenItems, setHiddenItems] = useState([]);
  const [totalPageCount, setTotalPageCount] = useState(0);
  const [dragIndex, setDragIndex] = useState(null);
  const [highlightIndex, setHighlightIndex] = useState(null);
  const [busy, setBusy] = useState(false);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [addGraftOpen, setAddGraftOpen] = useState(false);
  const [donorCatalog, setDonorCatalog] = useState([]);
  const [summariesCache, setSummariesCache] = useState([]);
  const cardRefs = useRef([]);
  const loadTokenRef = useRef(0);

  const currentSlots = useMemo(() => slotsFromItems(pageItems), [pageItems]);
  const currentHidden = useMemo(() => hiddenPagesFromItems(hiddenItems), [hiddenItems]);
  const graftDonorIds = useMemo(
    () => [...new Set(pageItems.filter((item) => item.kind === "graft").map((item) => item.donorReport))],
    [pageItems]
  );
  const missingBatchDonors = useMemo(
    () =>
      graftDonorIds.filter(
        (donorId) => !(generationBatchReportIds || []).map(String).includes(String(donorId))
      ),
    [graftDonorIds, generationBatchReportIds]
  );
  const missingPdfDonors = useMemo(
    () =>
      graftDonorIds.filter(
        (donorId) =>
          !(summariesCache || []).some(
            (row) =>
              reportIdFromSummaryTitle(row?.title) === donorId &&
              row?.visible !== false &&
              !summaryIsExcel(row)
          )
      ),
    [graftDonorIds, summariesCache]
  );
  const isDirty = useMemo(() => {
    const savedSlots = buildSlotsFromState(savedState);
    const savedHidden = [...(savedState?.hidden_pages || [])].sort((a, b) => a - b);
    const slotsDirty = savedSlots ? !slotsEqual(currentSlots, savedSlots) : currentSlots.length > 0;
    const hiddenDirty = !sameIntList(currentHidden, savedHidden);
    return slotsDirty || hiddenDirty;
  }, [savedState, currentSlots, currentHidden]);

  const loadEditor = useCallback(async () => {
    const loadToken = loadTokenRef.current + 1;
    loadTokenRef.current = loadToken;
    const isStale = () => loadTokenRef.current !== loadToken;

    setLoading(true);
    setError("");
    setSuccess("");
    try {
      const [summaries, orderState] = await Promise.all([
        fetchSummariesByDate(summaryDate),
        fetchReport1PageOrder(masterReportId)
      ]);
      if (isStale()) return;

      const masterSummary = (summaries || []).find(
        (item) =>
          reportIdFromSummaryTitle(item?.title) === masterReportId &&
          item?.visible !== false &&
          !summaryIsExcel(item)
      );
      if (!masterSummary) {
        setSummary(null);
        setPageItems([]);
        setHiddenItems([]);
        setTotalPageCount(0);
        setSavedState(orderState);
        setDonorCatalog(orderState?.donor_catalog || []);
        setSummariesCache(summaries || []);
        setError(
          `На дату ${summaryDate} нет опубликованной PDF-сводки ${masterReportId}. Сначала сгенерируйте сводку.`
        );
        return;
      }

      setSummary(masterSummary);
      setSavedState(orderState);
      setDonorCatalog(orderState?.donor_catalog || []);
      setSummariesCache(summaries || []);

      // Для разметки ячеек берём исходный PDF (до перестановки), иначе слоты «поплывут».
      const previewUrl = (() => {
        const sourceUrl = getPageOrderSourcePdfUrl(masterReportId, masterSummary.id, accessToken);
        const sep = sourceUrl.includes("?") ? "&" : "?";
        return `${sourceUrl}${sep}ts=${Date.now()}`;
      })();
      const pageCount = await getPdfPageCount(previewUrl);
      if (isStale()) return;

      setTotalPageCount(pageCount);
      const savedSlots = buildSlotsFromState(orderState);
      const savedHidden = orderState?.hidden_pages || [];
      const expectedTotal =
        orderState?.source_page_count ||
        (savedSlots
          ? savedSlots.filter((slot) => slot.kind === "own").length + savedHidden.length
          : pageCount);

      let initialVisible = placeholderOwnItems(pageCount);
      let initialHidden = [];

      if (Array.isArray(savedSlots) && savedSlots.length > 0 && expectedTotal === pageCount) {
        initialVisible = await buildItemsFromSlots(savedSlots, new Map(), summaries);
        const hiddenPages = new Set(savedHidden);
        initialHidden = Array.from({ length: pageCount }, (_, index) => index + 1)
          .filter((pageNum) => hiddenPages.has(pageNum))
          .map((pageNum) => ({
            kind: "own",
            page: pageNum,
            originalPage: pageNum,
            label: `Страница ${pageNum}`,
            imageDataUrl: "",
            loading: true
          }));
      }

      setPageItems(initialVisible);
      setHiddenItems(initialHidden);
      setLoading(false);

      const thumbnailByPage = new Map();
      await loadPdfPageThumbnails(previewUrl, {
        scale: 0.32,
        withLabels: true,
        shouldCancel: isStale,
        onPage: (thumb) => {
          if (isStale()) return;
          thumbnailByPage.set(thumb.page, thumb);
          setPageItems((prev) =>
            prev.map((item) => {
              if (item.kind !== "own" || item.originalPage !== thumb.page) return item;
              return { ...item, ...thumb, kind: "own", originalPage: thumb.page, loading: false };
            })
          );
          setHiddenItems((prev) =>
            prev.map((item) => {
              if (item.originalPage !== thumb.page) return item;
              return { ...item, ...thumb, kind: "own", originalPage: thumb.page, loading: false };
            })
          );
        }
      });
      if (isStale()) return;

      if (Array.isArray(savedSlots) && savedSlots.length > 0 && expectedTotal === pageCount) {
        const hydrated = await hydrateGraftThumbnails(initialVisible, summaries, accessToken, isStale);
        if (!isStale()) {
          setPageItems(hydrated);
        }
      }
    } catch (e) {
      if (!isStale()) {
        setError(String(e?.response?.data?.detail || e?.message || "Не удалось загрузить редактор."));
        setPageItems([]);
        setHiddenItems([]);
      }
    } finally {
      if (!isStale()) {
        setLoading(false);
      }
    }
  }, [summaryDate, accessToken, masterReportId]);

  useEffect(() => {
    loadEditor();
    return () => {
      loadTokenRef.current += 1;
    };
  }, [loadEditor]);

  useEffect(() => {
    onSavingChange?.(busy || previewBusy);
  }, [busy, previewBusy, onSavingChange]);

  function scrollToCard(index) {
    setHighlightIndex(index);
    const node = cardRefs.current[index];
    if (node?.scrollIntoView) {
      node.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }

  function handleDragStart(event, index) {
    setDragIndex(index);
    setHighlightIndex(index);
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", String(index));
  }

  function handleDragOver(event, index) {
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    if (dragIndex == null || dragIndex === index) return;
    setPageItems((prev) => moveItem(prev, dragIndex, index));
    setDragIndex(index);
    setHighlightIndex(index);
  }

  function handleDrop(event) {
    event.preventDefault();
    setDragIndex(null);
  }

  function handleDragEnd() {
    setDragIndex(null);
  }

  function hidePage(index) {
    const ownCount = pageItems.filter((item) => item.kind !== "graft").length;
    if (ownCount <= 1) {
      setError(`Нельзя скрыть все страницы сводки ${masterReportId} — должна остаться хотя бы одна.`);
      return;
    }
    setError("");
    setPageItems((prev) => {
      const item = prev[index];
      if (!item || item.kind === "graft") return prev;
      setHiddenItems((hidden) =>
        [...hidden, item].sort((a, b) => a.originalPage - b.originalPage)
      );
      if (highlightIndex === index) {
        setHighlightIndex(null);
      } else if (highlightIndex != null && highlightIndex > index) {
        setHighlightIndex(highlightIndex - 1);
      }
      return prev.filter((_, idx) => idx !== index);
    });
  }

  function showPage(originalPage) {
    setError("");
    setHiddenItems((prev) => {
      const item = prev.find((row) => row.originalPage === originalPage);
      if (!item) return prev;
      setPageItems((visible) => [...visible, item]);
      return prev.filter((row) => row.originalPage !== originalPage);
    });
  }

  function removeGraft(index) {
    setError("");
    setPageItems((prev) => prev.filter((_, idx) => idx !== index));
    if (highlightIndex === index) {
      setHighlightIndex(null);
    } else if (highlightIndex != null && highlightIndex > index) {
      setHighlightIndex(highlightIndex - 1);
    }
  }

  async function insertGraft({ donorReport, donorPage, label }) {
    setError("");
    const donor = (summariesCache || []).find(
      (row) =>
        reportIdFromSummaryTitle(row?.title) === donorReport &&
        row?.visible !== false &&
        !summaryIsExcel(row)
    );
    let thumb = null;
    if (donor) {
      try {
        thumb = await loadSinglePdfPageThumbnail(getPreviewUrl(donor, accessToken), donorPage, {
          scale: 0.32,
          withLabels: false
        });
      } catch {
        thumb = null;
      }
    }
    const item = {
      kind: "graft",
      donorReport,
      donorPage,
      label: label || thumb?.label || `Сводка ${donorReport}, стр. ${donorPage}`,
      imageDataUrl: thumb?.imageDataUrl || "",
      originalPage: null,
      donorAvailable: Boolean(donor)
    };
    setPageItems((prev) => {
      if (highlightIndex != null && highlightIndex >= 0) {
        const next = [...prev];
        next.splice(highlightIndex + 1, 0, item);
        return next;
      }
      return [...prev, item];
    });
  }

  async function handleSave() {
    if (!summary || pageItems.length === 0) return;
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      const slots = slotsFromItems(pageItems);
      const hidden = hiddenPagesFromItems(hiddenItems);
      const state = await saveReport1PageOrder({
        reportId: masterReportId,
        pageSlots: slots,
        hiddenPages: hidden,
        referenceSummaryId: summary.id,
        pageCount: totalPageCount || pageOrderFromItems(pageItems).length + hiddenItems.length
      });
      setSavedState(state);
      const hiddenNote = hidden.length ? ` Скрыто страниц: ${hidden.length}.` : "";
      const graftNote = graftDonorIds.length ? ` Подстановок: ${slots.filter((s) => s.kind === "graft").length}.` : "";
      const layersNote = state?.layers_note ? ` ${state.layers_note}` : "";
      if (state?.apply_error) {
        // Порядок записан, но к PDF не применён — говорим причину, а не «применится при генерации».
        setError(`Порядок страниц сохранён, но не применён к PDF: ${state.apply_error}`);
      } else {
        setSuccess(
          state?.applied_now
            ? `Порядок страниц сохранён и применён к PDF сводки ${masterReportId}.${hiddenNote}${graftNote}${layersNote}`
            : `Порядок страниц сохранён.${hiddenNote}${graftNote} Будет применён при следующей генерации сводки ${masterReportId}.`
        );
      }
    } catch (e) {
      setError(String(e?.response?.data?.detail || e?.message || "Не удалось сохранить порядок."));
    } finally {
      setBusy(false);
    }
  }

  async function handleReset() {
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      const state = await resetReport1PageOrder(masterReportId);
      setSavedState(state);
      setPageItems((prev) => {
        const ownOnly = prev.filter((item) => item.kind !== "graft");
        const all = [...ownOnly, ...hiddenItems].sort((a, b) => a.originalPage - b.originalPage);
        return all.map((item) => ({ ...item, kind: "own" }));
      });
      setHiddenItems([]);
      if (state?.apply_error) {
        setError(`Порядок сброшен, но PDF не восстановлен: ${state.apply_error}`);
      } else {
        setSuccess(`Порядок сброшен к исходному.${state?.layers_note ? ` ${state.layers_note}` : ""}`);
      }
    } catch (e) {
      setError(String(e?.response?.data?.detail || e?.message || "Не удалось сбросить порядок."));
    } finally {
      setBusy(false);
    }
  }

  async function handleDownloadPreview() {
    if (!summary || pageItems.length === 0) return;
    setPreviewBusy(true);
    setError("");
    try {
      const blob = await downloadReport1PageOrderPreview(
        summary.id,
        slotsFromItems(pageItems),
        hiddenPagesFromItems(hiddenItems),
        masterReportId
      );
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `svodka${masterReportId.replace(/\./g, "_")}_preview_${summaryDate}.pdf`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      setSuccess("Предпросмотр PDF скачан.");
    } catch (e) {
      setError(String(e?.response?.data?.detail || e?.message || "Не удалось выгрузить предпросмотр."));
    } finally {
      setPreviewBusy(false);
    }
  }

  if (loading) {
    return (
      <Stack alignItems="center" justifyContent="center" sx={{ py: 8 }}>
        <CircularProgress size={36} />
        <Typography variant="body2" color="text.secondary" sx={{ mt: 2 }}>
          Загрузка страниц и подписей…
        </Typography>
      </Stack>
    );
  }

  return (
    <Box sx={{ height: "100%", display: "flex", flexDirection: "column", minHeight: 0 }}>
      <Stack spacing={1.5} sx={{ px: 2, py: 1.5, borderBottom: 1, borderColor: "divider" }}>
        <Stack direction={{ xs: "column", sm: "row" }} spacing={1} alignItems={{ sm: "center" }} justifyContent="space-between">
          <Box>
            <Typography variant="subtitle1">Порядок страниц PDF</Typography>
            <Typography variant="body2" color="text.secondary">
              {summary
                ? `${summary.title} · ${pageItems.length} поз. (${pageItems.filter((i) => i.kind !== "graft").length} своих, ${pageItems.filter((i) => i.kind === "graft").length} подстановок) · дата ${summaryDate}`
                : `Дата ${summaryDate}`}
            </Typography>
          </Box>
          <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
            <Button
              variant="outlined"
              startIcon={<AddRoundedIcon />}
              disabled={!summary || busy || previewBusy || donorCatalog.length === 0}
              onClick={() => setAddGraftOpen(true)}
            >
              Добавить из другой сводки
            </Button>
            <Button
              variant="contained"
              startIcon={<SaveRoundedIcon />}
              disabled={!summary || busy || previewBusy || !isDirty}
              onClick={handleSave}
            >
              Сохранить
            </Button>
            <Button
              variant="outlined"
              startIcon={previewBusy ? <CircularProgress size={16} /> : <DownloadRoundedIcon />}
              disabled={!summary || busy || previewBusy || pageItems.length === 0}
              onClick={handleDownloadPreview}
            >
              Скачать предпросмотр
            </Button>
            <Button
              variant="text"
              startIcon={<RestartAltRoundedIcon />}
              disabled={busy || previewBusy}
              onClick={handleReset}
            >
              Сбросить
            </Button>
          </Stack>
        </Stack>
        {savedState?.has_custom_order ? (
          <Chip
            size="small"
            color="info"
            label={
              [
                savedState?.graft_count ? `подстановок ${savedState.graft_count}` : null,
                savedState?.hidden_pages?.length ? `скрыто ${savedState.hidden_pages.length}` : null
              ]
                .filter(Boolean)
                .join(", ") || "Сохранён пользовательский порядок для генерации"
            }
            sx={{ alignSelf: "flex-start" }}
          />
        ) : null}
        {missingBatchDonors.length > 0 ? (
          <Alert severity="warning" sx={{ py: 0.5 }}>
            Для подстановок из сводок <strong>{missingBatchDonors.join(", ")}</strong> включите их
            галочкой в пачке генерации — иначе при следующей генерации страницы не попадут в PDF.
          </Alert>
        ) : null}
        {missingPdfDonors.length > 0 ? (
          <Alert severity="warning" sx={{ py: 0.5 }}>
            На дату {summaryDate} нет опубликованных PDF сводок{" "}
            <strong>{missingPdfDonors.join(", ")}</strong> — миниатюра и предпросмотр могут быть
            недоступны до генерации.
          </Alert>
        ) : null}
        {error ? <Alert severity="error">{error}</Alert> : null}
        {success ? <Alert severity="success">{success}</Alert> : null}
        {summary ? (
          <Alert severity="info" sx={{ py: 0.5 }}>
            <strong>Позиция</strong> — место в итоговой сводке. Синие карточки — страницы из других
            сводок. Перетащите миниатюры, скройте ненужные или добавьте страницу донора.
          </Alert>
        ) : null}
      </Stack>

      <AddGraftDialog
        open={addGraftOpen}
        onClose={() => setAddGraftOpen(false)}
        donorCatalog={donorCatalog}
        summaries={summariesCache}
        accessToken={accessToken}
        onInsert={insertGraft}
      />

      <Box
        sx={{
          flex: 1,
          display: "flex",
          flexDirection: { xs: "column", sm: "row" },
          minHeight: 0,
          overflow: "hidden"
        }}
      >
        <Box sx={{ flex: 1, overflow: "auto", p: 2, minWidth: 0 }}>
          {summary && pageItems.length > 0 ? (
            <Grid container spacing={1.5}>
              {pageItems.map((item, index) => (
                <Grid
                  item
                  xs={6}
                  sm={6}
                  md={4}
                  lg={3}
                  key={itemKey(item, index)}
                  ref={(node) => {
                    cardRefs.current[index] = node;
                  }}
                >
                  <PageThumb
                    item={item}
                    index={index}
                    highlighted={highlightIndex === index}
                    onDragStart={handleDragStart}
                    onDragOver={handleDragOver}
                    onDrop={handleDrop}
                    onDragEnd={handleDragEnd}
                    onHide={hidePage}
                    onRemove={removeGraft}
                  />
                </Grid>
              ))}
            </Grid>
          ) : null}

          {summary && hiddenItems.length > 0 ? (
            <Box sx={{ mt: 3 }}>
              <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1.5 }}>
                <Typography variant="subtitle2">Скрытые страницы</Typography>
                <Chip size="small" label={hiddenItems.length} variant="outlined" />
              </Stack>
              <Grid container spacing={1.5}>
                {hiddenItems.map((item) => (
                  <Grid item xs={6} sm={6} md={4} lg={3} key={`hidden-${item.originalPage}`}>
                    <HiddenPageThumb item={item} onShow={showPage} />
                  </Grid>
                ))}
              </Grid>
            </Box>
          ) : null}
        </Box>

        {summary && pageItems.length > 0 ? (
          <Box
            sx={{
              width: { xs: "100%", sm: 300, md: 340 },
              flexShrink: 0,
              display: "flex",
              flexDirection: "column",
              minHeight: 0,
              borderLeft: { sm: 1 },
              borderTop: { xs: 1, sm: 0 },
              borderColor: "divider",
              bgcolor: (t) =>
                t.palette.mode === "light" ? t.palette.grey[50] : t.palette.background.default
            }}
          >
            <Box
              sx={{
                px: 1.5,
                py: 1.25,
                borderBottom: 1,
                borderColor: "divider",
                bgcolor: "background.paper"
              }}
            >
              <Typography variant="subtitle2">Содержание сводки</Typography>
              <Typography variant="caption" color="text.secondary">
                {pageItems.length} поз.
                {pageItems.filter((i) => i.kind === "graft").length
                  ? ` · ${pageItems.filter((i) => i.kind === "graft").length} подстановок`
                  : ""}
                {hiddenItems.length ? ` · ${hiddenItems.length} скрыто` : ""}
              </Typography>
            </Box>
            <Box sx={{ flex: 1, overflow: "auto", p: 1.5, minHeight: { xs: 200, sm: 0 } }}>
              <PageContentsTable
                items={pageItems}
                highlightIndex={highlightIndex}
                onSelectIndex={scrollToCard}
                onHideIndex={hidePage}
                onRemoveIndex={removeGraft}
              />
            </Box>
          </Box>
        ) : null}
      </Box>
    </Box>
  );
}
