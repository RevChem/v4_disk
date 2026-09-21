import { useCallback, useEffect, useMemo, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Divider from "@mui/material/Divider";
import Grid from "@mui/material/Grid";
import List from "@mui/material/List";
import ListItemButton from "@mui/material/ListItemButton";
import ListItemText from "@mui/material/ListItemText";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Tab from "@mui/material/Tab";
import Tabs from "@mui/material/Tabs";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import PlansSeasonsEditor from "./PlansSeasonsEditor";
import PlansWorkGroupsEditor from "./PlansWorkGroupsEditor";
import VisibilityOffRoundedIcon from "@mui/icons-material/VisibilityOffRounded";
import VisibilityRoundedIcon from "@mui/icons-material/VisibilityRounded";
import IconButton from "@mui/material/IconButton";
import Tooltip from "@mui/material/Tooltip";
import {
  fetchPlansEditor,
  resetPlansEditor,
  updatePlansEditorSeasonRange,
  updatePlansEditorValue
} from "./api";

function isEdited(base, effective, keys) {
  let b = base;
  let e = effective;
  for (const k of keys) {
    b = b?.[k];
    e = e?.[k];
  }
  if (b === undefined && e === undefined) return false;
  if (typeof b === "number" || typeof e === "number") {
    return Math.abs(Number(b ?? 0) - Number(e ?? 0)) > 1e-9;
  }
  return b !== e;
}

function getNested(obj, keys) {
  let cur = obj;
  for (const k of keys) {
    if (cur == null) return undefined;
    cur = cur[k];
  }
  return cur;
}

function formatPlanInputValue(value) {
  if (value == null || value === "") return "";
  return String(value);
}

function editedFieldSx(edited) {
  return edited
    ? {
        "& .MuiOutlinedInput-root": {
          bgcolor: (t) => (t.palette.mode === "light" ? "warning.50" : "rgba(255, 193, 7, 0.12)")
        }
      }
    : undefined;
}

function PlanNumberField({
  label,
  relativeKeys,
  effVal,
  baseVal,
  edited,
  saving,
  onSave
}) {
  const [localValue, setLocalValue] = useState(() => formatPlanInputValue(effVal));
  const [focused, setFocused] = useState(false);

  useEffect(() => {
    if (!focused) {
      setLocalValue(formatPlanInputValue(effVal));
    }
  }, [effVal, focused]);

  return (
    <TextField
      size="small"
      fullWidth
      label={label}
      type="text"
      inputMode="decimal"
      value={localValue}
      onFocus={() => setFocused(true)}
      onChange={(e) => setLocalValue(e.target.value)}
      onBlur={async (e) => {
        setFocused(false);
        const ok = await onSave(relativeKeys, e.target.value);
        if (!ok) {
          setLocalValue(formatPlanInputValue(effVal));
        }
      }}
      disabled={saving}
      sx={editedFieldSx(edited)}
      helperText={edited ? `База: ${baseVal}` : undefined}
    />
  );
}

function PlanMatrixNumberField({ relativeKeys, effVal, baseVal, edited, saving, onSave }) {
  const [localValue, setLocalValue] = useState(() => formatPlanInputValue(effVal));
  const [focused, setFocused] = useState(false);

  useEffect(() => {
    if (!focused) {
      setLocalValue(formatPlanInputValue(effVal));
    }
  }, [effVal, focused]);

  return (
    <TextField
      size="small"
      type="text"
      inputMode="decimal"
      value={localValue}
      onFocus={() => setFocused(true)}
      onChange={(e) => setLocalValue(e.target.value)}
      onBlur={async (e) => {
        setFocused(false);
        const ok = await onSave(relativeKeys, e.target.value);
        if (!ok) {
          setLocalValue(formatPlanInputValue(effVal));
        }
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          e.currentTarget.blur();
        }
      }}
      disabled={saving}
      inputProps={{ style: { textAlign: "center" } }}
      sx={{
        width: 88,
        flexShrink: 0,
        ...editedFieldSx(edited)
      }}
      title={edited ? `База: ${baseVal}` : undefined}
    />
  );
}

const EMPTY_WORK_GROUPS = [];

function yearsFromSeasonMeta(meta) {
  if (!meta) return [];
  if (Array.isArray(meta.season_years)) {
    return meta.season_years.map((y) => Number(y)).filter((y) => Number.isFinite(y));
  }
  if (meta.season_year != null) {
    const y = Number(meta.season_year);
    return Number.isFinite(y) ? [y] : [];
  }
  return [];
}

export default function PlansEditor({ summaryDate = null }) {
  const [state, setState] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [sectionKey, setSectionKey] = useState("CALENDAR_SEASONS");
  const [workGroup, setWorkGroup] = useState("");

  const reload = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await fetchPlansEditor(summaryDate);
      setState(data);
    } catch (e) {
      setError(e?.response?.data?.detail ?? e?.message ?? "Не удалось загрузить планы");
    } finally {
      setLoading(false);
    }
  }, [summaryDate]);

  useEffect(() => {
    reload();
  }, [reload]);

  const seasonMeta = useMemo(() => state?.season_meta?.by_group?.[workGroup] || null, [state, workGroup]);
  const seasonLabel = seasonMeta?.label || "";
  const serverRangeYears = useMemo(() => yearsFromSeasonMeta(seasonMeta), [seasonMeta]);
  const [localRangeYearsByGroup, setLocalRangeYearsByGroup] = useState({});
  const [dirtySeasonGroups, setDirtySeasonGroups] = useState(() => new Set());
  const seasonSelectionDirty = dirtySeasonGroups.has(workGroup);
  const rangeYears = seasonSelectionDirty
    ? localRangeYearsByGroup[workGroup] ?? serverRangeYears
    : serverRangeYears;

  const seasonYearOptions = useMemo(() => {
    const fromMeta = Array.isArray(seasonMeta?.season_year_options)
      ? seasonMeta.season_year_options.map((y) => Number(y)).filter((y) => Number.isFinite(y))
      : [];
    return [...new Set([...fromMeta, ...rangeYears])].sort((a, b) => a - b);
  }, [seasonMeta, rangeYears]);

  const toggleSeasonYear = (year) => {
    const y = Number(year);
    if (!Number.isFinite(y) || !workGroup) return;
    setDirtySeasonGroups((prev) => new Set(prev).add(workGroup));
    setLocalRangeYearsByGroup((prev) => {
      const current = prev[workGroup] ?? serverRangeYears;
      const set = new Set(current);
      if (set.has(y)) {
        if (set.size <= 1) return prev;
        set.delete(y);
      } else {
        set.add(y);
      }
      return { ...prev, [workGroup]: [...set].sort((a, b) => a - b) };
    });
  };

  const clearSeasonSelectionDraft = (groupName) => {
    if (!groupName) return;
    setDirtySeasonGroups((prev) => {
      const next = new Set(prev);
      next.delete(groupName);
      return next;
    });
    setLocalRangeYearsByGroup((prev) => {
      if (!(groupName in prev)) return prev;
      const next = { ...prev };
      delete next[groupName];
      return next;
    });
  };

  const saveSeasonRange = async ({ clear = false } = {}) => {
    if (!workGroup) return;
    setSaving(true);
    setError("");
    setSuccess("");
    try {
      const data = await updatePlansEditorSeasonRange(
        {
          group_name: workGroup,
          season_years: clear ? null : rangeYears,
          clear,
        },
        summaryDate
      );
      setState(data);
      clearSeasonSelectionDraft(workGroup);
      setSuccess(clear ? "Диапазон сброшен к расчётному." : "Диапазон дат и сезоны сохранены.");
    } catch (e) {
      setError(e?.response?.data?.detail ?? e?.message ?? "Не удалось сохранить диапазон дат");
    } finally {
      setSaving(false);
    }
  };

  const section = useMemo(
    () => state?.sections?.find((s) => s.key === sectionKey) ?? null,
    [state, sectionKey]
  );

  const groupsForSection = useMemo(() => {
    if (!state?.effective?.[sectionKey]) return state?.work_groups ?? [];
    if (section?.kind === "nested_3" || section?.kind === "nested_2") {
      const fromData = Object.keys(state.effective[sectionKey] || {});
      const ordered = (state?.work_groups ?? []).filter((g) => fromData.includes(g));
      const rest = fromData.filter((g) => !ordered.includes(g)).sort();
      return [...ordered, ...rest];
    }
    return state?.work_groups ?? [];
  }, [state, sectionKey, section]);

  useEffect(() => {
    if (groupsForSection.length && !groupsForSection.includes(workGroup)) {
      setWorkGroup(groupsForSection[0]);
    }
  }, [groupsForSection, workGroup]);

  const firmsForGroup = useMemo(() => {
    if (!state || !workGroup || section?.kind !== "nested_3") return [];
    const fromData = new Set(Object.keys(state.effective?.[sectionKey]?.[workGroup] || {}));
    Object.keys(state.base?.[sectionKey]?.[workGroup] || {}).forEach((f) => fromData.add(f));
    const ordered = (state.firms ?? []).filter((f) => fromData.has(f));
    const rest = [...fromData].filter((f) => !ordered.includes(f)).sort();
    return [...ordered, ...rest];
  }, [state, sectionKey, workGroup, section]);

  const cropsForMatrix = useMemo(() => {
    if (!state || !workGroup || section?.kind !== "nested_3") return [];
    const crops = new Set();
    for (const firmName of firmsForGroup) {
      Object.keys(state.effective?.[sectionKey]?.[workGroup]?.[firmName] || {}).forEach((c) =>
        crops.add(c)
      );
      Object.keys(state.base?.[sectionKey]?.[workGroup]?.[firmName] || {}).forEach((c) => crops.add(c));
    }
    const ranks = state.effective?.CROP_DISPLAY_RANK?.[workGroup] || {};
    return [...crops].sort((a, b) => {
      const raRaw = ranks[a];
      const rbRaw = ranks[b];
      const ra = raRaw === "" || raRaw == null ? null : Number(raRaw);
      const rb = rbRaw === "" || rbRaw == null ? null : Number(rbRaw);
      const aHas = ra != null && Number.isFinite(ra);
      const bHas = rb != null && Number.isFinite(rb);
      const aHidden = aHas && ra <= 0;
      const bHidden = bHas && rb <= 0;
      if (aHidden !== bHidden) return aHidden ? 1 : -1;
      if (aHas && bHas && !aHidden && !bHidden && ra !== rb) return ra - rb;
      if (aHas && !aHidden && (!bHas || bHidden)) return -1;
      if (bHas && !bHidden && (!aHas || aHidden)) return 1;
      return a.localeCompare(b, "ru");
    });
  }, [state, sectionKey, workGroup, section, firmsForGroup]);

  const flatRows = useMemo(() => {
    if (!state || section?.kind !== "flat") return [];
    const eff = state.effective?.[sectionKey] ?? {};
    const base = state.base?.[sectionKey] ?? {};
    return Object.keys({ ...base, ...eff }).sort((a, b) => a.localeCompare(b, "ru"));
  }, [state, section, sectionKey]);

  const firmRows = useMemo(() => {
    if (!state || section?.kind !== "nested_2" || !workGroup) return [];
    const eff = state.effective?.[sectionKey]?.[workGroup] ?? {};
    const base = state.base?.[sectionKey]?.[workGroup] ?? {};
    const fromData = Object.keys({ ...base, ...eff });
    const ordered = (state.firms ?? []).filter((f) => fromData.includes(f));
    const rest = fromData.filter((f) => !ordered.includes(f)).sort();
    return [...ordered, ...rest];
  }, [state, section, sectionKey, workGroup]);

  const saveValue = async (relativeKeys, rawValue) => {
    const num = Number(String(rawValue).replace(",", ".").trim());
    if (!Number.isFinite(num)) {
      setError("Введите число");
      return false;
    }
    const fullPath = [sectionKey, ...relativeKeys];
    const current = getNested(state?.effective, fullPath);
    if (typeof current === "number" && Math.abs(current - num) <= 1e-9) {
      return true;
    }
    setSaving(true);
    setError("");
    setSuccess("");
    try {
      const data = await updatePlansEditorValue({
        section_key: sectionKey,
        keys: relativeKeys,
        value: num,
        summaryDate
      });
      setState(data);
      setSuccess("Сохранено");
      return true;
    } catch (e) {
      setError(e?.response?.data?.detail ?? e?.message ?? "Не удалось сохранить");
      return false;
    } finally {
      setSaving(false);
    }
  };

  const handleReset = async (onlySection = false) => {
    setSaving(true);
    setError("");
    setSuccess("");
    try {
      const data = await resetPlansEditor(onlySection ? sectionKey : null, summaryDate);
      setState(data);
      setSuccess(onlySection ? "Секция сброшена к plans.py" : "Все правки сброшены к plans.py");
    } catch (e) {
      setError(e?.response?.data?.detail ?? e?.message ?? "Не удалось сбросить");
    } finally {
      setSaving(false);
    }
  };

  if (loading && !state) {
    return (
      <Stack direction="row" spacing={1} alignItems="center" sx={{ p: 2 }}>
        <CircularProgress size={18} />
        <Typography variant="body2" color="text.secondary">
          Загрузка планов из plans.py…
        </Typography>
      </Stack>
    );
  }

  return (
    <Box sx={{ display: "flex", flexDirection: { xs: "column", md: "row" }, minHeight: 0, height: "100%" }}>
      <Box
        sx={{
          width: { xs: "100%", md: 280 },
          flexShrink: 0,
          borderRight: { md: 1 },
          borderBottom: { xs: 1, md: 0 },
          borderColor: "divider",
          bgcolor: (t) => (t.palette.mode === "light" ? "grey.50" : "rgba(255,255,255,0.02)")
        }}
      >
        <Typography variant="subtitle2" color="text.secondary" sx={{ px: 2, pt: 1.5, pb: 0.5 }}>
          Разделы планов
        </Typography>
        <List dense disablePadding sx={{ maxHeight: { xs: 160, md: "none" }, overflow: { xs: "auto", md: "visible" } }}>
          {(state?.sections ?? []).map((s) => (
            <ListItemButton
              key={s.key}
              selected={s.key === sectionKey}
              onClick={() => {
                setSectionKey(s.key);
              }}
            >
              <ListItemText
                primary={s.label}
                secondary={
                  s.kind === "work_groups"
                    ? "API: work_type ID"
                    : s.kind === "flat"
                      ? "список"
                      : s.kind === "calendar_seasons"
                      ? "диапазоны дат"
                      : s.kind === "nested_2"
                        ? "группа → фирма"
                        : "группа → фирма → культура"
                }
                primaryTypographyProps={{ variant: "body2", fontWeight: s.key === sectionKey ? 600 : 400 }}
                secondaryTypographyProps={{ variant: "caption" }}
              />
            </ListItemButton>
          ))}
        </List>
      </Box>

      <Box sx={{ flex: 1, minWidth: 0, minHeight: 0, display: "flex", flexDirection: "column" }}>
        <Stack spacing={1.5} sx={{ p: 2, pb: 1, flexShrink: 0 }}>
          <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap alignItems="center">
            <Typography variant="subtitle1" sx={{ flex: 1, minWidth: 200 }}>
              {section?.label || sectionKey}
            </Typography>
            {saving && <Chip size="small" label="Сохранение…" color="primary" variant="outlined" />}
          </Stack>

          <Alert severity="info" sx={{ py: 0.5 }}>
            База — <strong>plans.py</strong>, сохраняются только отличия. Жёлтая подсветка — изменено.
          </Alert>
          {state?.has_edits && (
            <Alert severity="warning" sx={{ py: 0.5 }}>
              Есть сохранённые правки ({state.path})
            </Alert>
          )}
          {error && <Alert severity="error">{error}</Alert>}
          {success && <Alert severity="success">{success}</Alert>}

          <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
            <Button variant="outlined" size="small" disabled={saving} onClick={() => handleReset(true)}>
              Сбросить секцию
            </Button>
            <Button variant="outlined" color="warning" size="small" disabled={saving} onClick={() => handleReset(false)}>
              Сбросить все правки
            </Button>
            <Button variant="text" size="small" disabled={loading || saving} onClick={reload}>
              Обновить
            </Button>
          </Stack>
        </Stack>

        <Divider />

        <Box sx={{ flex: 1, minHeight: 0, overflow: "auto", p: 2 }}>
          {section?.kind === "calendar_seasons" ? (
            <PlansSeasonsEditor
              calendarSeasons={state?.season_meta?.calendar_seasons || {}}
              reportDate={state?.season_meta?.report_date}
              summaryDate={summaryDate}
              onSaved={setState}
              saving={saving}
              setSaving={setSaving}
              setError={setError}
              setSuccess={setSuccess}
            />
          ) : section?.kind === "work_groups" ? (
            <PlansWorkGroupsEditor
              groups={state?.effective?.WORK_GROUPS ?? state?.base?.WORK_GROUPS ?? EMPTY_WORK_GROUPS}
              summaryDate={summaryDate}
              onSaved={(data) => {
                setState(data);
              }}
              saving={saving}
              setSaving={setSaving}
              setError={setError}
              setSuccess={setSuccess}
            />
          ) : (
            <>
          {(section?.kind === "nested_3" || section?.kind === "nested_2") && groupsForSection.length > 0 && (
            <Tabs
              value={workGroup || false}
              onChange={(_, value) => {
                setWorkGroup(String(value));
              }}
              variant="scrollable"
              scrollButtons="auto"
              sx={{ mb: 1, borderBottom: 1, borderColor: "divider" }}
            >
              {groupsForSection.map((g) => (
                <Tab key={g} value={g} label={g} sx={{ textTransform: "none", maxWidth: 360 }} />
              ))}
            </Tabs>
          )}

          {seasonMeta && (section?.kind === "nested_3" || section?.kind === "nested_2") && (
            <Paper variant="outlined" sx={{ p: 1.5, mb: 2 }}>
              <Stack spacing={1.25}>
                <Alert
                  severity={seasonSelectionDirty ? "info" : seasonMeta.has_override ? "warning" : "success"}
                  sx={{ py: 0.5 }}
                >
                  {seasonLabel}
                  {seasonMeta.has_override ? " (ручная правка)" : ""}
                  {seasonSelectionDirty ? " · несохранённый выбор сезонов" : ""}
                </Alert>
                <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
                  <Button
                    variant="contained"
                    size="small"
                    disabled={saving || !workGroup || rangeYears.length === 0}
                    onClick={() => saveSeasonRange()}
                  >
                    Сохранить сезоны
                  </Button>
                  <Button
                    variant="outlined"
                    size="small"
                    disabled={saving || !workGroup || !seasonMeta.has_override}
                    onClick={() => saveSeasonRange({ clear: true })}
                  >
                    Сбросить
                  </Button>
                </Stack>
                <Stack spacing={0.75}>
                  <Typography variant="caption" color="text.secondary">
                    Сезоны Cropwise (можно несколько, напр. посев — 2026 и 2027):
                  </Typography>
                  <Stack direction="row" spacing={0.75} useFlexGap flexWrap="wrap">
                    {seasonYearOptions.map((y) => {
                      const selected = rangeYears.includes(y);
                      return (
                        <Chip
                          key={y}
                          label={String(y)}
                          clickable
                          color={selected ? "primary" : "default"}
                          variant={selected ? "filled" : "outlined"}
                          disabled={saving}
                          onClick={() => toggleSeasonYear(y)}
                        />
                      );
                    })}
                  </Stack>
                </Stack>
                <Typography variant="caption" color="text.secondary">
                  Диапазон считается из вкладки «Сезоны» + выбранные годы. По умолчанию сезоны:{" "}
                  {(seasonMeta.default_season_years || [seasonMeta.default_season_year]).join(", ")}.
                  {seasonSelectionDirty
                    ? " Нажмите «Сохранить сезоны» — «Обновить» не сбрасывает несохранённый выбор."
                    : ""}
                </Typography>
              </Stack>
            </Paper>
          )}
          {section?.kind === "flat" && (
            <Grid container spacing={1.5}>
              {flatRows.map((key) => {
                const relativeKeys = [key];
                const fullPath = [sectionKey, ...relativeKeys];
                const effVal = getNested(state?.effective, fullPath);
                const baseVal = getNested(state?.base, fullPath);
                const edited = isEdited(state?.base?.[sectionKey], state?.effective?.[sectionKey], relativeKeys);
                const seasonHint = state?.season_meta?.by_group?.[key]?.label || "";
                return (
                  <Grid item xs={12} sm={6} md={4} lg={3} key={key}>
                    <PlanNumberField
                      label={key}
                      relativeKeys={relativeKeys}
                      effVal={effVal}
                      baseVal={baseVal}
                      edited={edited}
                      saving={saving}
                      onSave={saveValue}
                    />
                    {seasonHint ? (
                      <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 0.5 }}>
                        {seasonHint}
                      </Typography>
                    ) : null}
                  </Grid>
                );
              })}
            </Grid>
          )}

          {section?.kind === "nested_2" && workGroup && (
            <Grid container spacing={1.5}>
              {firmRows.map((firmName) => {
                const relativeKeys = [workGroup, firmName];
                const fullPath = [sectionKey, ...relativeKeys];
                const effVal = getNested(state?.effective, fullPath);
                const baseVal = getNested(state?.base, fullPath);
                const edited = isEdited(state?.base?.[sectionKey], state?.effective?.[sectionKey], relativeKeys);
                return (
                  <Grid item xs={12} sm={6} md={4} lg={3} key={firmName}>
                    <Paper variant="outlined" sx={{ p: 1.5, height: "100%" }}>
                      <Typography variant="caption" color="text.secondary" display="block" sx={{ mb: 1 }}>
                        {workGroup}
                      </Typography>
                      <PlanNumberField
                        label={firmName}
                        relativeKeys={relativeKeys}
                        effVal={effVal}
                        baseVal={baseVal}
                        edited={edited}
                        saving={saving}
                        onSave={saveValue}
                      />
                    </Paper>
                  </Grid>
                );
              })}
            </Grid>
          )}

          {section?.kind === "nested_3" && workGroup && (
            <TableContainer component={Paper} variant="outlined" sx={{ maxHeight: "100%", overflow: "auto" }}>
              <Table
                stickyHeader
                size="small"
                sx={{ minWidth: Math.max(960, firmsForGroup.length * 168 + 280) }}
              >
                <TableHead>
                  <TableRow>
                    <TableCell
                      sx={{
                        fontWeight: 700,
                        minWidth: 180,
                        position: "sticky",
                        left: 0,
                        zIndex: 4,
                        bgcolor: "background.paper"
                      }}
                    >
                      Культура
                    </TableCell>
                    {firmsForGroup.map((firmName) => (
                      <TableCell key={firmName} align="center" sx={{ fontWeight: 600, minWidth: 148 }}>
                        {firmName}
                      </TableCell>
                    ))}
                    <TableCell
                      align="center"
                      sx={{
                        fontWeight: 700,
                        minWidth: 104,
                        position: "sticky",
                        right: 0,
                        zIndex: 4,
                        bgcolor: "background.paper",
                        borderLeft: 1,
                        borderColor: "divider",
                        boxShadow: (t) =>
                          t.palette.mode === "light"
                            ? "-6px 0 8px -6px rgba(0,0,0,0.18)"
                            : "-6px 0 8px -6px rgba(0,0,0,0.55)"
                      }}
                      title="0 — скрыть культуру; 1 — первая; 2 — вторая…"
                    >
                      Ранг
                    </TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {cropsForMatrix.map((crop) => {
                    const rankVal = state?.effective?.CROP_DISPLAY_RANK?.[workGroup]?.[crop];
                    const rankNum = rankVal === "" || rankVal == null ? null : Number(rankVal);
                    const hidden = rankNum != null && Number.isFinite(rankNum) && rankNum <= 0;
                    const rankEdited = isEdited(
                      state?.base?.CROP_DISPLAY_RANK?.[workGroup],
                      state?.effective?.CROP_DISPLAY_RANK?.[workGroup],
                      [crop]
                    );
                    return (
                      <TableRow
                        key={crop}
                        hover
                        sx={
                          hidden
                            ? {
                                opacity: 0.55,
                                bgcolor: (t) =>
                                  t.palette.mode === "light" ? "action.hover" : "rgba(255,255,255,0.04)"
                              }
                            : undefined
                        }
                      >
                        <TableCell
                          sx={{
                            fontWeight: 500,
                            position: "sticky",
                            left: 0,
                            zIndex: 2,
                            bgcolor: "background.paper",
                            whiteSpace: "nowrap"
                          }}
                        >
                          {crop}
                          {hidden ? (
                            <Typography component="span" variant="caption" color="text.secondary" sx={{ ml: 1 }}>
                              скрыта
                            </Typography>
                          ) : null}
                        </TableCell>
                        {firmsForGroup.map((firmName) => {
                          const relativeKeys = [workGroup, firmName, crop];
                          const fullPath = [sectionKey, ...relativeKeys];
                          const effVal = getNested(state?.effective, fullPath);
                          const baseVal = getNested(state?.base, fullPath);
                          const edited = isEdited(
                            state?.base?.[sectionKey]?.[workGroup],
                            state?.effective?.[sectionKey]?.[workGroup],
                            [firmName, crop]
                          );
                          const firmHidden = Number(
                            state?.effective?.CROP_FIRM_HIDDEN?.[workGroup]?.[firmName]?.[crop] || 0
                          ) > 0;
                          return (
                            <TableCell
                              key={`${firmName}-${crop}`}
                              align="center"
                              sx={{
                                p: 0.75,
                                minWidth: 148,
                                bgcolor: firmHidden
                                  ? (t) =>
                                      t.palette.mode === "light"
                                        ? "rgba(211, 47, 47, 0.08)"
                                        : "rgba(244, 67, 54, 0.12)"
                                  : undefined
                              }}
                            >
                              <Stack
                                direction="row"
                                spacing={0.5}
                                alignItems="center"
                                justifyContent="center"
                                sx={{ minWidth: 132 }}
                              >
                                <PlanMatrixNumberField
                                  relativeKeys={relativeKeys}
                                  effVal={firmHidden ? "" : effVal}
                                  baseVal={baseVal}
                                  edited={edited}
                                  saving={saving || firmHidden}
                                  onSave={saveValue}
                                />
                                <Tooltip
                                  title={
                                    firmHidden
                                      ? "Показать культуру для этой АФ"
                                      : "Скрыть культуру только для этой АФ"
                                  }
                                >
                                  <span>
                                    <IconButton
                                      size="small"
                                      disabled={saving || hidden}
                                      color={firmHidden ? "error" : "default"}
                                      aria-label={firmHidden ? "Показать для АФ" : "Скрыть для АФ"}
                                      sx={{ flexShrink: 0 }}
                                      onClick={async () => {
                                        setSaving(true);
                                        setError("");
                                        setSuccess("");
                                        try {
                                          const data = await updatePlansEditorValue({
                                            section_key: "CROP_FIRM_HIDDEN",
                                            keys: [workGroup, firmName, crop],
                                            value: firmHidden ? 0 : 1,
                                            summaryDate
                                          });
                                          setState(data);
                                          setSuccess(
                                            firmHidden
                                              ? `«${crop}» снова видна для ${firmName}.`
                                              : `«${crop}» скрыта для ${firmName}.`
                                          );
                                        } catch (e) {
                                          setError(
                                            e?.response?.data?.detail ??
                                              e?.message ??
                                              "Не удалось сохранить скрытие по АФ"
                                          );
                                        } finally {
                                          setSaving(false);
                                        }
                                      }}
                                    >
                                      {firmHidden ? (
                                        <VisibilityOffRoundedIcon sx={{ fontSize: 16 }} />
                                      ) : (
                                        <VisibilityRoundedIcon sx={{ fontSize: 16 }} />
                                      )}
                                    </IconButton>
                                  </span>
                                </Tooltip>
                              </Stack>
                            </TableCell>
                          );
                        })}
                        <TableCell
                          align="center"
                          sx={{
                            p: 0.75,
                            minWidth: 104,
                            position: "sticky",
                            right: 0,
                            zIndex: 2,
                            bgcolor: "background.paper",
                            borderLeft: 1,
                            borderColor: "divider",
                            boxShadow: (t) =>
                              t.palette.mode === "light"
                                ? "-6px 0 8px -6px rgba(0,0,0,0.12)"
                                : "-6px 0 8px -6px rgba(0,0,0,0.45)"
                          }}
                        >
                          <PlanMatrixNumberField
                            relativeKeys={[workGroup, crop]}
                            effVal={rankVal ?? ""}
                            baseVal={state?.base?.CROP_DISPLAY_RANK?.[workGroup]?.[crop]}
                            edited={rankEdited}
                            saving={saving}
                            onSave={async (keys, raw) => {
                              const trimmed = String(raw).replace(",", ".").trim();
                              if (trimmed === "") {
                                return true;
                              }
                              const num = Number(trimmed);
                              if (!Number.isFinite(num) || num < 0) {
                                setError("Ранг: целое число ≥ 0 (0 = скрыть)");
                                return false;
                              }
                              setSaving(true);
                              setError("");
                              setSuccess("");
                              try {
                                const data = await updatePlansEditorValue({
                                  section_key: "CROP_DISPLAY_RANK",
                                  keys,
                                  value: Math.trunc(num),
                                  summaryDate
                                });
                                setState(data);
                                setSuccess("Ранг культуры сохранён.");
                                return true;
                              } catch (e) {
                                setError(
                                  e?.response?.data?.detail ?? e?.message ?? "Не удалось сохранить ранг"
                                );
                                return false;
                              } finally {
                                setSaving(false);
                              }
                            }}
                          />
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </TableContainer>
          )}

          {section?.kind === "nested_3" && workGroup && cropsForMatrix.length === 0 && (
            <Alert severity="info">Для выбранной группы работ нет данных.</Alert>
          )}
            </>
          )}
        </Box>
      </Box>
    </Box>
  );
}
