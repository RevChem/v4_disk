import { useCallback, useEffect, useMemo, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import RestartAltRoundedIcon from "@mui/icons-material/RestartAltRounded";
import SaveRoundedIcon from "@mui/icons-material/SaveRounded";
import TextFormatRoundedIcon from "@mui/icons-material/TextFormatRounded";

import {
  fetchPdfHeaderLabels,
  resetPdfHeaderLabels,
  savePdfHeaderLabels,
  stripPdfHeaderGroupNumbers,
} from "../api";

function stripGroupNumbers(line) {
  const prefix = "ВИД РАБОТ: ";
  const text = String(line || "").trim();
  if (!text.startsWith(prefix)) return text;
  const body = text.slice(prefix.length).replace(/\d+\.\d+\s+/g, "").trim();
  return body ? `${prefix}${body}` : text;
}

function errorText(e) {
  const d = e?.response?.data?.detail;
  if (typeof d === "string" && d) return d;
  if (Array.isArray(d)) return d.map((x) => x?.msg || JSON.stringify(x)).join("; ");
  return String(e?.message || e);
}

export default function PdfHeaderLabelsEditor() {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [filter, setFilter] = useState("");
  const [rows, setRows] = useState([]);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await fetchPdfHeaderLabels();
      setRows(
        (data.items || []).map((item) => ({
          default_work_line: item.default_work_line,
          work_line: item.work_line,
          report_ids: item.report_ids || [],
          is_custom: Boolean(item.is_custom),
        }))
      );
    } catch (e) {
      setError(errorText(e));
      setRows([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const filteredRows = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return rows;
    return rows.filter((row) => {
      const hay = [
        row.default_work_line,
        row.work_line,
        ...(row.report_ids || []),
      ]
        .join(" ")
        .toLowerCase();
      return hay.includes(q);
    });
  }, [rows, filter]);

  function updateRow(defaultLine, value) {
    setRows((prev) =>
      prev.map((row) =>
        row.default_work_line === defaultLine ? { ...row, work_line: value } : row
      )
    );
    setSuccess("");
  }

  async function handleSave() {
    setSaving(true);
    setError("");
    setSuccess("");
    try {
      const payload = rows.map((row) => ({
        default_work_line: row.default_work_line,
        work_line:
          String(row.work_line || "").trim() === String(row.default_work_line || "").trim()
            ? null
            : String(row.work_line || "").trim(),
      }));
      const data = await savePdfHeaderLabels(payload);
      setRows(
        (data.items || []).map((item) => ({
          default_work_line: item.default_work_line,
          work_line: item.work_line,
          report_ids: item.report_ids || [],
          is_custom: Boolean(item.is_custom),
        }))
      );
      setSuccess("Подписи сохранены. Применятся при следующей генерации PDF.");
    } catch (e) {
      setError(errorText(e));
    } finally {
      setSaving(false);
    }
  }

  async function handleReset() {
    if (!window.confirm("Сбросить все пользовательские подписи PDF?")) return;
    setSaving(true);
    setError("");
    setSuccess("");
    try {
      const data = await resetPdfHeaderLabels();
      setRows(
        (data.items || []).map((item) => ({
          default_work_line: item.default_work_line,
          work_line: item.work_line,
          report_ids: item.report_ids || [],
          is_custom: Boolean(item.is_custom),
        }))
      );
      setSuccess("Подписи сброшены к значениям по умолчанию.");
    } catch (e) {
      setError(errorText(e));
    } finally {
      setSaving(false);
    }
  }

  async function handleStripNumbers() {
    setSaving(true);
    setError("");
    setSuccess("");
    try {
      const payload = filteredRows.map((row) => ({
        default_work_line: row.default_work_line,
      }));
      const data = await stripPdfHeaderGroupNumbers(payload);
      setRows(
        (data.items || []).map((item) => ({
          default_work_line: item.default_work_line,
          work_line: item.work_line,
          report_ids: item.report_ids || [],
          is_custom: Boolean(item.is_custom),
        }))
      );
      setSuccess("Номера групп убраны и сохранены. Применятся при следующей генерации PDF.");
    } catch (e) {
      setError(errorText(e));
    } finally {
      setSaving(false);
    }
  }

  function handleStripLocal(defaultLine) {
    setRows((prev) =>
      prev.map((row) =>
        row.default_work_line === defaultLine
          ? { ...row, work_line: stripGroupNumbers(row.default_work_line) }
          : row
      )
    );
    setSuccess("");
  }

  if (loading) {
    return (
      <Stack alignItems="center" justifyContent="center" sx={{ py: 6 }}>
        <CircularProgress size={28} />
      </Stack>
    );
  }

  return (
    <Stack spacing={2} sx={{ p: 2, height: "100%", minHeight: 0 }}>
      <Typography variant="body2" color="text.secondary">
        Здесь можно изменить строку «ВИД РАБОТ» в шапке PDF. Изменения сохраняются на сервере
        и применяются при следующей генерации сводок.
      </Typography>

      {error && (
        <Alert severity="error" onClose={() => setError("")}>
          {error}
        </Alert>
      )}
      {success && (
        <Alert severity="success" onClose={() => setSuccess("")}>
          {success}
        </Alert>
      )}

      <Stack direction={{ xs: "column", sm: "row" }} spacing={1} alignItems={{ sm: "center" }}>
        <TextField
          size="small"
          label="Поиск"
          placeholder="культивация, сводка 8…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          sx={{ minWidth: 260, flex: 1 }}
        />
        <Button
          size="small"
          variant="outlined"
          startIcon={<TextFormatRoundedIcon />}
          disabled={saving || filteredRows.length === 0}
          onClick={handleStripNumbers}
        >
          Убрать номера (в списке)
        </Button>
        <Button
          size="small"
          variant="outlined"
          color="warning"
          startIcon={<RestartAltRoundedIcon />}
          disabled={saving}
          onClick={handleReset}
        >
          Сбросить всё
        </Button>
        <Button
          size="small"
          variant="contained"
          startIcon={<SaveRoundedIcon />}
          disabled={saving}
          onClick={handleSave}
        >
          Сохранить
        </Button>
      </Stack>

      <TableContainer component={Box} sx={{ flex: 1, minHeight: 0, overflow: "auto", border: 1, borderColor: "divider", borderRadius: 1 }}>
        <Table size="small" stickyHeader>
          <TableHead>
            <TableRow>
              <TableCell sx={{ minWidth: 280 }}>По умолчанию</TableCell>
              <TableCell sx={{ width: 120 }}>Сводки</TableCell>
              <TableCell sx={{ minWidth: 320 }}>Показывать в PDF</TableCell>
              <TableCell sx={{ width: 120 }} />
            </TableRow>
          </TableHead>
          <TableBody>
            {filteredRows.map((row) => {
              const isChanged =
                String(row.work_line || "").trim() !== String(row.default_work_line || "").trim();
              return (
                <TableRow key={row.default_work_line} hover selected={isChanged}>
                  <TableCell>
                    <Typography variant="body2">{row.default_work_line}</Typography>
                  </TableCell>
                  <TableCell>
                    <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
                      {(row.report_ids || []).map((rid) => (
                        <Chip key={rid} size="small" label={rid} variant="outlined" />
                      ))}
                    </Stack>
                  </TableCell>
                  <TableCell>
                    <TextField
                      size="small"
                      fullWidth
                      value={row.work_line}
                      onChange={(e) => updateRow(row.default_work_line, e.target.value)}
                      placeholder={row.default_work_line}
                      inputProps={{ maxLength: 240 }}
                    />
                  </TableCell>
                  <TableCell>
                    <Button size="small" onClick={() => handleStripLocal(row.default_work_line)}>
                      Без номера
                    </Button>
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </TableContainer>
    </Stack>
  );
}
