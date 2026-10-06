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
import { fetchPlansEditorWorkTypes, formatError, updatePlansEditorWorkGroups } from "./api";

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

function splitIdTokens(text) {
  return String(text || "")
    .split(/[,\s;]+/)
    .map((x) => x.trim())
    .filter(Boolean);
}

function textToIds(text) {
  return splitIdTokens(text)
    .filter((x) => /^\d+$/.test(x))
    .map((x) => Number(x));
}

function groupsKey(list) {
  return JSON.stringify(
    (list || []).map((g) => ({ name: String(g?.name || "").trim(), ids: g?.ids || [] }))
  );
}

export default function PlansWorkGroupsEditor({
  groups,
  summaryDate = null,
  onSaved,
  saving,
  setSaving,
  setError,
  setSuccess
}) {
  const [draft, setDraft] = useState([]);
  const [workTypes, setWorkTypes] = useState([]);
  const [typesLoading, setTypesLoading] = useState(false);
  const [filter, setFilter] = useState("");
  const [conflict, setConflict] = useState(false);
  // JSON серверной версии, с которой синхронизирован черновик ("" — ещё не загружали)
  const baseKeyRef = useRef("");
  const nextRowIdRef = useRef(1);

  const serverNormalized = useMemo(() => normalizeGroups(groups), [groups]);
  const serverKey = useMemo(() => groupsKey(serverNormalized), [serverNormalized]);
  const draftKey = useMemo(() => groupsKey(draft), [draft]);

  useEffect(() => {
    if (serverKey === baseKeyRef.current) return;
    const isDirty = baseKeyRef.current !== "" && draftKey !== baseKeyRef.current;
    if (!isDirty) {
      baseKeyRef.current = serverKey;
      setDraft(serverNormalized);
      setConflict(false);
    } else {
      // Пока у нас несохранённый черновик, группы изменил кто-то другой.
      setConflict(true);
    }
    // draftKey намеренно не в зависимостях: реагируем только на смену серверной версии
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverKey, serverNormalized]);

  function reloadFromServer() {
    baseKeyRef.current = serverKey;
    setDraft(serverNormalized);
    setConflict(false);
  }

  const loadWorkTypes = useCallback(async () => {
    setTypesLoading(true);
    try {
      const data = await fetchPlansEditorWorkTypes();
      setWorkTypes(Array.isArray(data?.items) ? data.items : []);
    } catch (e) {
      setError(formatError(e, "Не удалось загрузить справочник work_types."));
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
    const rows = draft.map((row) => ({
      name: String(row.name || "").trim(),
      ids: row.idsText !== undefined ? textToIds(row.idsText) : row.ids || [],
      bad: row.idsText !== undefined
        ? splitIdTokens(row.idsText).filter((x) => !/^\d+$/.test(x))
        : [],
    }));
    const named = rows.filter((g) => g.name);
    if (!named.length) {
      setError("Добавьте хотя бы одну группу с названием.");
      return;
    }
    const badRow = named.find((g) => g.bad.length);
    if (badRow) {
      setError(`Группа «${badRow.name}»: ID должны быть целыми числами, не подходит: ${badRow.bad.join(", ")}.`);
      return;
    }
    const seen = new Set();
    for (const g of named) {
      const k = g.name.toLocaleLowerCase("ru");
      if (seen.has(k)) {
        setError(`Название группы повторяется: «${g.name}».`);
        return;
      }
      seen.add(k);
    }
    const normalized = named.map(({ name, ids }) => ({ name, ids: [...new Set(ids)] }));

    // Переименование/удаление группы «осиротит» правки планов, привязанные к её названию.
    const newNames = new Set(normalized.map((g) => g.name));
    const lost = serverNormalized.map((g) => g.name).filter((n) => n && !newNames.has(n));
    if (lost.length) {
      const ok = window.confirm(
        `Группы будут удалены или переименованы: ${lost.join("; ")}.\n\n` +
          "Площади, агрегаты, ранги и сезоны, заданные под старыми названиями, перестанут применяться. Продолжить?"
      );
      if (!ok) return;
    }
    if (conflict) {
      const ok = window.confirm(
        "Пока вы редактировали, группы работ изменил другой пользователь. Его изменения будут перезаписаны. Продолжить?"
      );
      if (!ok) return;
    }

    setSaving(true);
    setError("");
    setSuccess("");
    try {
      const data = await updatePlansEditorWorkGroups(normalized, summaryDate);
      baseKeyRef.current = groupsKey(normalized);
      setDraft(normalizeGroups(normalized));
      setConflict(false);
      onSaved(data);
      setSuccess("WORK_GROUPS сохранены.");
    } catch (e) {
      setError(formatError(e, "Не удалось сохранить WORK_GROUPS."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Grid container spacing={2}>
      <Grid size={{ xs: 12, lg: 8 }}>
        <Stack spacing={1.5}>
          {conflict && (
            <Alert
              severity="warning"
              action={
                <Button color="inherit" size="small" onClick={reloadFromServer} disabled={saving}>
                  Загрузить серверную версию
                </Button>
              }
            >
              Группы работ изменил другой пользователь, пока у вас открыт несохранённый черновик.
              Сохранение перезапишет его изменения.
            </Alert>
          )}
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
                        disabled={saving}
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
                        disabled={saving}
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
                        disabled={saving || draft.length <= 1}
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
            <Button startIcon={<AddRoundedIcon />} onClick={addRow} disabled={saving}>
              Добавить группу
            </Button>
            <Button variant="contained" onClick={handleSave} disabled={saving}>
              {saving ? "Сохранение..." : "Сохранить WORK_GROUPS"}
            </Button>
          </Stack>
        </Stack>
      </Grid>
      <Grid size={{ xs: 12, lg: 4 }}>
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
