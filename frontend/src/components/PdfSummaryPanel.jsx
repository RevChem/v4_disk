import { useMemo, useState } from "react";
import {
  Box,
  Button,
  Card,
  CardContent,
  CircularProgress,
  Collapse,
  Dialog,
  DialogContent,
  DialogTitle,
  IconButton,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";
import ChatBubbleOutlineRoundedIcon from "@mui/icons-material/ChatBubbleOutlineRounded";
import DeleteOutlineRoundedIcon from "@mui/icons-material/DeleteOutlineRounded";
import DownloadRoundedIcon from "@mui/icons-material/DownloadRounded";
import EditNoteRoundedIcon from "@mui/icons-material/EditNoteRounded";
import VisibilityRoundedIcon from "@mui/icons-material/VisibilityRounded";

import { deleteSummary, downloadGeneratedFile, startGeneratedFileDownload, summaryIsExcel } from "../api";

function formatAdded(modifiedAt, generationSeconds) {
  if (!modifiedAt) return "—";
  const ts = Number(modifiedAt);
  if (!Number.isFinite(ts)) return "—";
  const when = new Date(ts * 1000).toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
  const seconds = Number(generationSeconds);
  if (Number.isFinite(seconds) && seconds > 0) {
    const label = Number.isInteger(seconds) ? String(seconds) : seconds.toFixed(1);
    return `${when} · ${label} с`;
  }
  return when;
}

export default function PdfSummaryPanel({
  summaryDate,
  files = [],
  loading = false,
  onError,
  canCellEdit = false,
  onCellEdit,
  canComments = false,
  onComment,
  canDelete = false,
  onDeleted,
  cropwiseByReport = {},
  cropwiseSeasonByReport = {},
}) {
  const [previewFilename, setPreviewFilename] = useState(null);
  const [previewUrl, setPreviewUrl] = useState(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [deletingId, setDeletingId] = useState(null);
  const [diffReportId, setDiffReportId] = useState("");
  const [seasonDiffReportId, setSeasonDiffReportId] = useState("");

  const sortedFiles = useMemo(() => {
    const key = (f) => {
      const rid = f.report_id || String(f.title || f.filename).match(/^(\d+(?:\.\d+)?)/)?.[1];
      if (!rid) return [9999, 0];
      return rid.split(".").map((x) => parseInt(x, 10) || 0);
    };
    return [...files].sort((a, b) => {
      const ka = key(a);
      const kb = key(b);
      for (let i = 0; i < Math.max(ka.length, kb.length); i += 1) {
        const d = (kb[i] || 0) - (ka[i] || 0);
        if (d) return d;
      }
      return 0;
    });
  }, [files]);

  function handleDownload(filename) {
    startGeneratedFileDownload(summaryDate, filename);
  }

  async function handleDelete(item) {
    const summaryId = Number(item.summary_id);
    if (!summaryId) return;
    const label = item.title || item.filename || "сводку";
    if (!window.confirm(`Удалить «${label}»? Файл будет удалён с диска.`)) {
      return;
    }
    setDeletingId(summaryId);
    try {
      await deleteSummary(summaryId);
      if (previewFilename === item.filename) {
        if (previewUrl) URL.revokeObjectURL(previewUrl);
        setPreviewFilename(null);
        setPreviewUrl(null);
      }
      onDeleted?.();
    } catch (e) {
      onError?.(String(e.message || e));
    } finally {
      setDeletingId(null);
    }
  }

  async function togglePreview(filename) {
    if (previewFilename === filename) {
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      setPreviewFilename(null);
      setPreviewUrl(null);
      return;
    }
    setPreviewLoading(true);
    try {
      const blob = await downloadGeneratedFile(summaryDate, filename);
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      const url = URL.createObjectURL(blob);
      setPreviewFilename(filename);
      setPreviewUrl(url);
    } catch (e) {
      onError?.(String(e.message || e));
    } finally {
      setPreviewLoading(false);
    }
  }

  return (
    <Card elevation={0} sx={{ border: 1, borderColor: "divider" }}>
      <CardContent sx={{ p: 3 }}>
        <Typography variant="h6" fontWeight={600} sx={{ mb: 2 }}>
          Сводки за выбранную дату
        </Typography>

        {loading ? (
          <Stack direction="row" alignItems="center" spacing={1}>
            <CircularProgress size={18} />
            <Typography color="text.secondary">Загрузка…</Typography>
          </Stack>
        ) : sortedFiles.length === 0 ? (
          <Typography color="text.secondary">На эту дату пока нет загруженных сводок.</Typography>
        ) : (
          <Stack spacing={1.5}>
            {sortedFiles.map((f) => {
              const open = previewFilename === f.filename;
              const isExcel = summaryIsExcel(f);
              const reportKey =
                f.report_id || String(f.title || f.filename).match(/^(\d+(?:\.\d+)?)/)?.[1] || "";
              const diffs = cropwiseByReport[String(reportKey)] || [];
              const seasonDiffs = cropwiseSeasonByReport[String(reportKey)] || [];
              return (
                <Box key={f.filename}>
                  <Box
                    className="summary-row"
                    sx={(th) => ({
                      display: "flex",
                      flexWrap: { xs: "wrap", sm: "nowrap" },
                      alignItems: { xs: "stretch", sm: "center" },
                      gap: 1,
                      p: 1.5,
                      borderRadius: 2,
                      border: `1px solid ${th.palette.divider}`,
                      bgcolor:
                        th.palette.mode === "light"
                          ? "rgba(255,255,255,0.92)"
                          : th.palette.background.paper,
                      boxShadow:
                        th.palette.mode === "light"
                          ? "0 4px 16px rgba(15,23,42,0.07)"
                          : "0 8px 28px rgba(0,0,0,0.32)",
                    })}
                  >
                    <Box sx={{ flex: 1, minWidth: 0, display: "flex", alignItems: "center", gap: 1 }}>
                      {seasonDiffs.length > 0 ? (
                        <IconButton
                          aria-label="Различия с Cropwise за сезон"
                          title="Различия с Cropwise за сезон (больше 10 га)"
                          onClick={() => setSeasonDiffReportId(String(reportKey))}
                          sx={{ p: 0.5, flexShrink: 0 }}
                        >
                          <Box
                            sx={{
                              width: 10,
                              height: 10,
                              borderRadius: "50%",
                              bgcolor: "error.main",
                            }}
                          />
                        </IconButton>
                      ) : null}
                      {diffs.length > 0 ? (
                        <IconButton
                          aria-label="Различия с Cropwise"
                          title="Различия с Cropwise"
                          onClick={() => setDiffReportId(String(reportKey))}
                          sx={{ p: 0.5, flexShrink: 0 }}
                        >
                          <Box
                            sx={{
                              width: 10,
                              height: 10,
                              borderRadius: "50%",
                              bgcolor: "error.main",
                            }}
                          />
                        </IconButton>
                      ) : null}
                      <Box sx={{ minWidth: 0 }}>
                      <Typography fontWeight={600}>{f.title || f.filename}</Typography>
                      <Typography variant="body2" color="text.secondary">
                        Добавлено: {formatAdded(f.modified_at, f.generation_seconds)}
                        {isExcel ? " · Excel" : ""}
                      </Typography>
                      </Box>
                    </Box>
                    <Stack direction="row" spacing={1} sx={{ flexShrink: 0, flexWrap: "wrap" }}>
                      {!isExcel && (
                        <Button
                          variant={open ? "contained" : "outlined"}
                          size="small"
                          className="pressable"
                          startIcon={<VisibilityRoundedIcon />}
                          onClick={() => togglePreview(f.filename)}
                          disabled={previewLoading && !open}
                        >
                          {open ? "Скрыть" : "Предпросмотр"}
                        </Button>
                      )}
                      <Button
                        variant="contained"
                        size="small"
                        className="pressable"
                        startIcon={<DownloadRoundedIcon />}
                        onClick={() => handleDownload(f.filename)}
                      >
                        {isExcel ? "Скачать Excel" : "Скачать"}
                      </Button>
                      {canCellEdit && f.summary_id && !isExcel && (
                        <Button
                          variant="outlined"
                          size="small"
                          className="pressable"
                          startIcon={<EditNoteRoundedIcon />}
                          onClick={() =>
                            onCellEdit?.({
                              id: Number(f.summary_id),
                              title: f.title || f.filename,
                              summary_date: summaryDate,
                              filename: f.filename,
                              report_id: f.report_id,
                            })
                          }
                        >
                          Правки в документе
                        </Button>
                      )}
                      {canComments && f.summary_id && !isExcel && (
                        <Button
                          variant="outlined"
                          size="small"
                          className="pressable"
                          startIcon={<ChatBubbleOutlineRoundedIcon />}
                          onClick={() =>
                            onComment?.({
                              id: Number(f.summary_id),
                              title: f.title || f.filename,
                              summary_date: summaryDate,
                              filename: f.filename,
                            })
                          }
                        >
                          Комментарий
                        </Button>
                      )}
                      {canDelete && f.summary_id && (
                        <Button
                          variant="contained"
                          color="error"
                          size="small"
                          className="pressable summary-delete-btn"
                          startIcon={<DeleteOutlineRoundedIcon />}
                          onClick={() => handleDelete(f)}
                          disabled={deletingId === Number(f.summary_id)}
                          sx={{ minWidth: 0, px: 1.25 }}
                        >
                          Удалить
                        </Button>
                      )}
                    </Stack>
                  </Box>
                  <Collapse in={Boolean(open && previewUrl && !isExcel)} timeout={{ enter: 420, exit: 280 }}>
                    <Box
                      sx={{
                        mt: 1,
                        borderRadius: 2,
                        overflow: "hidden",
                        border: 1,
                        borderColor: "divider",
                        height: { xs: 360, md: 520 },
                      }}
                    >
                      <Box
                        component="iframe"
                        src={previewUrl || undefined}
                        title={f.title || f.filename}
                        sx={{ width: "100%", height: "100%", border: 0 }}
                      />
                    </Box>
                  </Collapse>
                </Box>
              );
            })}
          </Stack>
        )}
      </CardContent>
      <CropwiseDiffDialog
        rows={cropwiseByReport[diffReportId] || []}
        onClose={() => setDiffReportId("")}
      />
      <CropwiseDiffDialog
        season
        rows={cropwiseSeasonByReport[seasonDiffReportId] || []}
        onClose={() => setSeasonDiffReportId("")}
      />
    </Card>
  );
}

function formatHa(value) {
  if (value == null || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return number.toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fileCell(row) {
  if (row.status === "missing") return "нет в файле";
  if (row.status === "unread") return "файл не прочитан";
  return formatHa(row.file_ha);
}

function columnCaption(row, side) {
  if (row.source !== "sheets") return null;
  return side === "left" ? "итого листа" : "сумма строк";
}

function CropwiseDiffDialog({ rows, onClose, season = false }) {
  const title = rows[0]?.title || (rows[0]?.report_id ? `Сводка ${rows[0].report_id}` : "Cropwise");
  const sheetsOnly = rows.length > 0 && rows.every((row) => row.source === "sheets");
  return (
    <Dialog open={rows.length > 0} onClose={onClose} fullWidth maxWidth="sm">
      <DialogTitle>
        {sheetsOnly ? "Листы" : season ? "Cropwise за сезон" : "Cropwise"} · {title}
      </DialogTitle>
      <DialogContent>
        {season ? (
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
            Сезонные гектары из файла сводки и work.json Cropwise. Показаны расхождения больше 10 га.
          </Typography>
        ) : null}
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>Операция</TableCell>
              <TableCell align="right">{sheetsOnly ? "Итого листа, га" : "Файл, га"}</TableCell>
              <TableCell align="right">{sheetsOnly ? "Сумма строк, га" : "Cropwise, га"}</TableCell>
              <TableCell align="right">Разница, га</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {rows.map((row, index) => (
              <TableRow key={`${row.group}-${index}`}>
                <TableCell>
                  {row.group || "Сводка"}
                  {row.note ? (
                    <Typography variant="caption" color="text.secondary" display="block">
                      {row.note}
                    </Typography>
                  ) : null}
                </TableCell>
                <TableCell align="right">
                  {fileCell(row)}
                  {columnCaption(row, "left") ? (
                    <Typography variant="caption" color="text.secondary" display="block">
                      {columnCaption(row, "left")}
                    </Typography>
                  ) : null}
                </TableCell>
                <TableCell align="right">
                  {formatHa(row.cropwise_ha)}
                  {columnCaption(row, "right") ? (
                    <Typography variant="caption" color="text.secondary" display="block">
                      {columnCaption(row, "right")}
                    </Typography>
                  ) : null}
                </TableCell>
                <TableCell align="right">{formatHa(row.delta)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </DialogContent>
    </Dialog>
  );
}
