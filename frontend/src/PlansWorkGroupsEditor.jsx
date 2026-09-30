import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Alert from "@mui/material/Alert";
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
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import AddRoundedIcon from "@mui/icons-material/AddRounded";
import DeleteOutlineRoundedIcon from "@mui/icons-material/DeleteOutlineRounded";
import { fetchPlansEditorWorkTypes, updatePlansEditorWorkGroups } from "./api";

function normalizeGroups(raw) {
  return (raw || []).map((g, index) => {
    const ids = Array.isArray(g?.ids) ? g.ids.map((x) => Number(x)).filter((x) => Number.isFinite(x)) : [];
    return {
      rowId: g?.rowId || `wg-${index}`,
      name: String(g?.name || "").trim(),
      ids,
      idsText: idsToText(ids)
    };
  });
}

function idsToText(ids) {
  return (ids || []).join(", ");
}

function textToIds(text) {
  return String(text || "")
    .split(/[,\s;]+/)
    .map((x) => x.trim())
    .filter(Boolean)
    .map((x) => Number(x))
    .filter((x) => Number.isFinite(x));
}

export default function PlansWorkGroupsEditor({
  groups,
  summaryDate = null,
  onSaved,
  saving,
  readOnly = false,
  setSaving,
  setError,
  setSuccess
}) {
  const [draft, setDraft] = useState([]);
  const [workTypes, setWorkTypes] = useState([]);
  const [typesLoading, setTypesLoading] = useState(false);
  const [filter, setFilter] = useState("");
  const groupsSnapshotRef = useRef("");
  const nextRowIdRef = useRef(1);

  useEffect(() => {
    const nextSnapshot = JSON.stringify(groups ?? []);
    if (nextSnapshot === groupsSnapshotRef.current) return;
    groupsSnapshotRef.current = nextSnapshot;
    setDraft(normalizeGroups(groups));
  }, [groups]);

  const loadWorkTypes = useCallback(async () => {
    setTypesLoading(true);
    try {
      const data = await fetchPlansEditorWorkTypes();
      setWorkTypes(Array.isArray(data?.items) ? data.items : []);
    } catch (e) {
      setError(e?.response?.data?.detail ?? e?.message ?? "Не удалось загрузить справочник work_types.");
    } finally {
      setTypesLoading(false);
    }
  }, [setError]);

  useEffect(() => {
    loadWorkTypes();
  }, [loadWorkTypes]);

  const typeMap = useMemo(() => {
    const m = new Map();
    for (const wt of workTypes) {
      m.set(Number(wt.id), String(wt.name || ""));
    }
    return m;
  }, [workTypes]);

  const filteredTypes = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return workTypes;
    return workTypes.filter(
      (wt) => String(wt.id).includes(q) || String(wt.name || "").toLowerCase().includes(q)
    );
  }, [workTypes, filter]);

  function updateRow(index, patch) {
    setDraft((prev) => prev.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  }

  function addRow() {
    const rowId = `wg-new-${nextRowIdRef.current++}`;
    setDraft((prev) => [...prev, { rowId, name: "Новая группа", ids: [], idsText: "" }]);
  }

  function deleteRow(index) {
    setDraft((prev) => prev.filter((_, i) => i !== index));
  }

  async function handleSave() {
    if (readOnly) {
      setError("Сейчас планы редактирует другой пользователь. Параллельные правки недоступны.");
      return;
    }
    const normalized = normalizeGroups(draft)
      .filter((g) => g.name)
      .map(({ name, ids }) => ({ name, ids }));
    if (!normalized.length) {
      setError("Добавьте хотя бы одну группу с названием.");
      return;
    }
    setSaving(true);
    setError("");
    setSuccess("");
    try {
      const data = await updatePlansEditorWorkGroups(normalized, summaryDate);
      onSaved(data);
      setSuccess("WORK_GROUPS сохранены.");
    } catch (e) {
      setError(e?.response?.data?.detail ?? e?.message ?? "Не удалось сохранить WORK_GROUPS.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Grid container spacing={2}>
      <Grid item xs={12} lg={8}>
        <Stack spacing={1.5}>
          <Alert severity="info" sx={{ py: 0.5 }}>
            Каждая строка — блок в сводке 22. ID — это <strong>work_type</strong> из Cropwise API
            (<code>/work_types</code>). Несколько ID объединяются в одну группу.
          </Alert>
          <TableContainer component={Paper} variant="outlined">
            <Table size="small" stickyHeader>
              <TableHead>
                <TableRow>
                  <TableCell sx={{ minWidth: 220, fontWeight: 700 }}>Название группы</TableCell>
                  <TableCell sx={{ minWidth: 160, fontWeight: 700 }}>work_type ID</TableCell>
                  <TableCell sx={{ fontWeight: 700 }}>Расшифровка</TableCell>
                  <TableCell width={48} />
                </TableRow>
              </TableHead>
              <TableBody>
                {draft.map((row, index) => (
                  <TableRow key={row.rowId} hover>
                    <TableCell sx={{ verticalAlign: "top" }}>
                      <TextField
                        size="small"
                        fullWidth
                        value={row.name}
                        onChange={(e) => updateRow(index, { name: e.target.value })}
                        disabled={saving || readOnly}
                      />
                    </TableCell>
                    <TableCell sx={{ verticalAlign: "top" }}>
                      <TextField
                        size="small"
                        fullWidth
                        placeholder="55, 103, 81"
                        value={row.idsText ?? idsToText(row.ids)}
                        onChange={(e) => {
                          const idsText = e.target.value;
                          updateRow(index, { idsText, ids: textToIds(idsText) });
                        }}
                        disabled={saving || readOnly}
                      />
                    </TableCell>
                    <TableCell sx={{ verticalAlign: "top" }}>
                      <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
                        {(row.ids || []).map((id) => (
                          <Chip
                            key={`${index}-${id}`}
                            size="small"
                            label={`${id}: ${typeMap.get(Number(id)) || "?"}`}
                            variant={typeMap.has(Number(id)) ? "outlined" : "filled"}
                            color={typeMap.has(Number(id)) ? "default" : "warning"}
                          />
                        ))}
                      </Stack>
                    </TableCell>
                    <TableCell sx={{ verticalAlign: "top" }}>
                      <IconButton
                        size="small"
                        aria-label="Удалить группу"
                        onClick={() => deleteRow(index)}
                        disabled={saving || readOnly || draft.length <= 1}
                      >
                        <DeleteOutlineRoundedIcon fontSize="small" />
                      </IconButton>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
          <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
            <Button startIcon={<AddRoundedIcon />} onClick={addRow} disabled={saving || readOnly}>
              Добавить группу
            </Button>
            <Button variant="contained" onClick={handleSave} disabled={saving || readOnly}>
              {saving ? "Сохранение..." : "Сохранить WORK_GROUPS"}
            </Button>
          </Stack>
        </Stack>
      </Grid>
      <Grid item xs={12} lg={4}>
        <Paper variant="outlined" sx={{ p: 1.5, height: "100%", minHeight: 320 }}>
          <Stack spacing={1}>
            <Typography variant="subtitle2">Справочник work_types (Cropwise API)</Typography>
            <TextField
              size="small"
              placeholder="Поиск по ID или названию"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
            {typesLoading ? (
              <Stack direction="row" spacing={1} alignItems="center">
                <CircularProgress size={16} />
                <Typography variant="body2" color="text.secondary">
                  Загрузка…
                </Typography>
              </Stack>
            ) : (
              <Box sx={{ maxHeight: 520, overflow: "auto" }}>
                <Table size="small">
                  <TableHead>
                    <TableRow>
                      <TableCell width={56}>ID</TableCell>
                      <TableCell>Название</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {filteredTypes.map((wt) => (
                      <TableRow key={wt.id} hover>
                        <TableCell>{wt.id}</TableCell>
                        <TableCell>{wt.name}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </Box>
            )}
            <Button size="small" onClick={loadWorkTypes} disabled={typesLoading}>
              Обновить справочник
            </Button>
          </Stack>
        </Paper>
      </Grid>
    </Grid>
  );
}
