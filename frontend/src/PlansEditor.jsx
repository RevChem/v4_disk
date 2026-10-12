import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
import MenuItem from "@mui/material/MenuItem";
import Select from "@mui/material/Select";
import Typography from "@mui/material/Typography";
import PlansSeasonsEditor from "./PlansSeasonsEditor";
import PlansWorkGroupsEditor from "./PlansWorkGroupsEditor";
import VisibilityOffRoundedIcon from "@mui/icons-material/VisibilityOffRounded";
import VisibilityRoundedIcon from "@mui/icons-material/VisibilityRounded";
import IconButton from "@mui/material/IconButton";
import Tooltip from "@mui/material/Tooltip";
import {
  fetchPlansEditor,
  formatError,
  heartbeatPlansPresence,
  releasePlansPresence,
  releasePlansPresenceKeepalive,
  syncPlansPy,
  updatePlansEditorSeasonRange,
  updatePlansEditorValue
} from "./api";

const RANK_COL_PX = 120;

const rankStickySx = {
  position: "sticky",
  right: 0,
  zIndex: 2,
  width: RANK_COL_PX,
  minWidth: RANK_COL_PX,
  maxWidth: RANK_COL_PX,
  bgcolor: "background.paper",
  borderLeft: 1,
  borderColor: "divider",
  boxShadow: (t) =>
    t.palette.mode === "light" ? "-6px 0 8px -6px rgba(0,0,0,0.12)" : "-6px 0 8px -6px rgba(0,0,0,0.45)"
};

const modeStickySx = {
  position: "sticky",
  right: RANK_COL_PX,
  zIndex: 2,
  minWidth: 168,
  bgcolor: "background.paper",
  borderLeft: 1,
  borderColor: "divider"
};

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

function PlanMatrixNumberField({ relativeKeys, effVal, baseVal, edited, saving, onSave, onPasteText }) {
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
      onPaste={(e) => {
        if (!onPasteText) return;
        const text = e.clipboardData?.getData("text") ?? "";
        if (!text.includes("\t")) return;
        e.preventDefault();
        onPasteText(text);
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

function newPlansEditToken() {
  const cryptoObj = globalThis.crypto;
  if (cryptoObj?.randomUUID) return cryptoObj.randomUUID();
  if (cryptoObj?.getRandomValues) {
    const bytes = cryptoObj.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  }
  return `plans${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`.replace(/[^A-Za-z0-9_-]/g, "");
}

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

function defaultYearRange(year, calendarSeasons) {
  const y = Number(year);
  const row = calendarSeasons?.[String(y)] || {};
  return {
    period_start: row.period_start || `${y - 1}-07-01`,
    period_end: row.period_end || `${y}-07-01`,
  };
}

function rangesForYears(years, meta, calendarSeasons) {
  const stored = meta?.year_ranges || {};
  const out = {};
  for (const y of years) {
    const key = String(y);
    const row = stored[key] || calendarSeasons?.[key] || {};
    out[key] = {
      period_start: row.period_start || `${y - 1}-07-01`,
      period_end: row.period_end || `${y}-07-01`,
    };
  }
  return out;
}

function operationNames(state) {
  const names = [];
  const seen = new Set();
  const push = (name) => {
    const text = String(name || "").trim();
    if (!text || seen.has(text)) return;
    seen.add(text);
    names.push(text);
  };
  for (const group of state?.effective?.WORK_GROUPS || state?.base?.WORK_GROUPS || []) {
    push(group?.name);
  }
  for (const sectionKey of ["HARDCODED_PLAN", "AVAILABLE_OWN", "CROP_DISPLAY_RANK", "CROP_REPORT_MODE", "CROP_FIRM_HIDDEN"]) {
    for (const name of Object.keys(state?.effective?.[sectionKey] || {})) push(name);
  }
  for (const name of Object.keys(state?.effective?.PLAN_RATE_PER_GROUP || {})) push(name);
  for (const name of Object.keys(state?.season_meta?.by_group || {})) push(name);
  return names;
}

function downloadAgroOperationsJson(state, localSeasons) {
  const groups = state?.effective?.WORK_GROUPS || state?.base?.WORK_GROUPS || [];
  const idsByName = new Map(
    groups.map((group) => [String(group?.name || "").trim(), group?.ids || []])
  );
  const dirty = localSeasons?.dirtySeasonGroups;
  const operations = {};
  for (const name of operationNames(state)) {
    const meta = state?.season_meta?.by_group?.[name] || {};
    const unsaved = Boolean(dirty?.has?.(name));
    operations[name] = {
      work_type_ids: idsByName.get(name) || [],
      season_years: unsaved
        ? localSeasons.localRangeYearsByGroup?.[name] ?? meta.season_years ?? []
        : meta.season_years ?? [],
      year_ranges: unsaved
        ? localSeasons.localYearRangesByGroup?.[name] ?? meta.year_ranges ?? {}
        : meta.year_ranges ?? {},
      HARDCODED_PLAN: state?.effective?.HARDCODED_PLAN?.[name] || {},
      AVAILABLE_OWN: state?.effective?.AVAILABLE_OWN?.[name] || {},
      PLAN_RATE_PER_GROUP: state?.effective?.PLAN_RATE_PER_GROUP?.[name] ?? null,
      CROP_DISPLAY_RANK: state?.effective?.CROP_DISPLAY_RANK?.[name] || {},
      CROP_REPORT_MODE: state?.effective?.CROP_REPORT_MODE?.[name] || {},
      CROP_FIRM_HIDDEN: state?.effective?.CROP_FIRM_HIDDEN?.[name] || {},
      OTHER_CROPS_LAYOUT: Number(state?.effective?.OTHER_CROPS_LAYOUT?.[name] ?? 0) === 1 ? 1 : 0,
    };
  }
  const payload = {
    report_date: state?.season_meta?.report_date || null,
    calendar_seasons: state?.season_meta?.calendar_seasons || {},
    operations,
  };
  const blob = new Blob([`${JSON.stringify(payload, null, 2)}\n`], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "agro_operations.json";
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

export default function PlansEditor({ summaryDate = null }) {
  const [state, setState] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [sectionKey, setSectionKey] = useState("CALENDAR_SEASONS");
  const [workGroup, setWorkGroup] = useState("");
  const [dragCrop, setDragCrop] = useState(null);
  const [otherEditors, setOtherEditors] = useState([]);
  // loadGen — «последний загрузчик побеждает» только среди reload() (смена даты);
  // writeGen растёт при каждом применении ответа записи: опрос, начатый до записи, отбрасывается.
  const loadGen = useRef(0);
  const writeGen = useRef(0);
  const reloadInFlight = useRef(false);
  const pollInFlight = useRef(false);
  const savingRef = useRef(false);

  const setSavingTracked = useCallback((value) => {
    const next = Boolean(value);
    savingRef.current = next;
    setSaving(next);
  }, []);

  /** Ответ записи — самое свежее состояние: применяем и отменяем устаревшие опросы. */
  const applyWriteState = useCallback((data) => {
    writeGen.current += 1;
    setState(data);
  }, []);

  const saveCropMode = useCallback(
    async (crop, mode) => {
      if (!workGroup || savingRef.current) return;
      setSavingTracked(true);
      setError("");
      setSuccess("");
      try {
        const data = await updatePlansEditorValue({
          section_key: "CROP_REPORT_MODE",
          keys: [workGroup, crop],
          value: mode,
          summaryDate
        });
        applyWriteState(data);
        setSuccess(
          mode === 1
            ? `«${crop}» входит в «Иные культуры».`
            : mode === 2
              ? `«${crop}» в сводках не учитывается.`
              : `«${crop}» снова учитывается отдельно.`
        );
      } catch (e) {
        setError(formatError(e, "Не удалось сохранить учёт культуры"));
      } finally {
        setSavingTracked(false);
        setDragCrop(null);
      }
    },
    [workGroup, summaryDate, applyWriteState, setSavingTracked]
  );

  const saveOtherLayout = useCallback(
    async (mode) => {
      if (!workGroup || savingRef.current) return;
      setSavingTracked(true);
      setError("");
      setSuccess("");
      try {
        const data = await updatePlansEditorValue({
          section_key: "OTHER_CROPS_LAYOUT",
          keys: [workGroup],
          value: mode,
          summaryDate
        });
        applyWriteState(data);
        setSuccess(
          mode === 1
            ? "В сводках культуры из «Иные культуры» выводятся отдельно, по агрофирмам и культурам."
            : "В сводках «Иные культуры» — одна строка, только по агрофирмам."
        );
      } catch (e) {
        setError(formatError(e, "Не удалось сохранить вывод иных культур"));
      } finally {
        setSavingTracked(false);
      }
    },
    [workGroup, summaryDate, applyWriteState, setSavingTracked]
  );

  useEffect(() => {
    const token = newPlansEditToken();
    let stopped = false;
    const controller = new AbortController();

    const sync = async () => {
      try {
        const data = await heartbeatPlansPresence(token, controller.signal);
        if (!stopped) setOtherEditors(Array.isArray(data?.others) ? data.others : []);
      } catch (e) {
        if (stopped || e?.code === "ERR_CANCELED" || e?.name === "CanceledError") return;
      }
    };

    sync();
    const timer = window.setInterval(sync, 5000);
    const releaseOnHide = () => releasePlansPresenceKeepalive(token);
    window.addEventListener("pagehide", releaseOnHide);

    return () => {
      stopped = true;
      controller.abort();
      window.clearInterval(timer);
      window.removeEventListener("pagehide", releaseOnHide);
      releasePlansPresence(token).catch(() => {});
      window.setTimeout(() => {
        releasePlansPresence(token).catch(() => {});
      }, 2000);
    };
  }, []);

  const reload = useCallback(async () => {
    const gen = ++loadGen.current;
    const wGen = writeGen.current;
    reloadInFlight.current = true;
    setLoading(true);
    setError("");
    try {
      const data = await fetchPlansEditor(summaryDate);
      if (gen !== loadGen.current) return;
      // если за время загрузки уже применён ответ записи — он новее
      if (wGen === writeGen.current) setState(data);
    } catch (e) {
      if (gen !== loadGen.current) return;
      setError(formatError(e, "Не удалось загрузить планы"));
    } finally {
      if (gen === loadGen.current) {
        reloadInFlight.current = false;
        setLoading(false);
      }
    }
  }, [summaryDate]);

  const refreshSilent = useCallback(async () => {
    // не конкурируем ни с записью, ни с явной загрузкой, ни с предыдущим опросом
    if (savingRef.current || reloadInFlight.current || pollInFlight.current) return;
    pollInFlight.current = true;
    const gen = loadGen.current;
    const wGen = writeGen.current;
    try {
      const data = await fetchPlansEditor(summaryDate);
      if (gen !== loadGen.current || wGen !== writeGen.current || savingRef.current) return;
      setState(data);
    } catch {
      // Фоновое обновление не затирает уже показанные планы.
    } finally {
      pollInFlight.current = false;
    }
  }, [summaryDate]);

  useEffect(() => {
    reload();
  }, [reload]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      refreshSilent();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [refreshSilent]);

  const seasonMeta = useMemo(() => state?.season_meta?.by_group?.[workGroup] || null, [state, workGroup]);
  const seasonLabel = seasonMeta?.label || "";
  const serverRangeYears = useMemo(() => yearsFromSeasonMeta(seasonMeta), [seasonMeta]);
  const [localRangeYearsByGroup, setLocalRangeYearsByGroup] = useState({});
  const [localYearRangesByGroup, setLocalYearRangesByGroup] = useState({});
  const [dirtySeasonGroups, setDirtySeasonGroups] = useState(() => new Set());
  const seasonSelectionDirty = dirtySeasonGroups.has(workGroup);
  const rangeYears = seasonSelectionDirty
    ? localRangeYearsByGroup[workGroup] ?? serverRangeYears
    : serverRangeYears;
  const calendarSeasons = state?.season_meta?.calendar_seasons || {};
  const serverYearRanges = useMemo(
    () => rangesForYears(serverRangeYears, seasonMeta, calendarSeasons),
    [serverRangeYears, seasonMeta, calendarSeasons]
  );
  const yearRanges = seasonSelectionDirty
    ? localYearRangesByGroup[workGroup] ?? serverYearRanges
    : serverYearRanges;

  const seasonYearOptions = useMemo(() => {
    const fromMeta = Array.isArray(seasonMeta?.season_year_options)
      ? seasonMeta.season_year_options.map((y) => Number(y)).filter((y) => Number.isFinite(y))
      : [];
    return [...new Set([...fromMeta, ...rangeYears])].sort((a, b) => a - b);
  }, [seasonMeta, rangeYears]);

  const toggleSeasonYear = (year) => {
    const y = Number(year);
    if (!Number.isFinite(y) || !workGroup) return;
    const currentYears = localRangeYearsByGroup[workGroup] ?? serverRangeYears;
    const set = new Set(currentYears);
    if (set.has(y)) {
      if (set.size <= 1) return;
      set.delete(y);
    } else {
      set.add(y);
    }
    const nextYears = [...set].sort((a, b) => a - b);
    const nextRanges = { ...(localYearRangesByGroup[workGroup] ?? serverYearRanges) };
    const key = String(y);
    if (set.has(y)) {
      if (!nextRanges[key]) nextRanges[key] = defaultYearRange(y, calendarSeasons);
    } else {
      delete nextRanges[key];
    }
    for (const rangeKey of Object.keys(nextRanges)) {
      if (!set.has(Number(rangeKey))) delete nextRanges[rangeKey];
    }
    setDirtySeasonGroups((prev) => new Set(prev).add(workGroup));
    setLocalRangeYearsByGroup((prev) => ({ ...prev, [workGroup]: nextYears }));
    setLocalYearRangesByGroup((prev) => ({ ...prev, [workGroup]: nextRanges }));
  };

  const updateYearRange = (year, field, value) => {
    if (!workGroup) return;
    const key = String(year);
    const nextRanges = { ...(localYearRangesByGroup[workGroup] ?? serverYearRanges) };
    nextRanges[key] = {
      ...(nextRanges[key] || defaultYearRange(year, calendarSeasons)),
      [field]: value,
    };
    setDirtySeasonGroups((prev) => new Set(prev).add(workGroup));
    setLocalRangeYearsByGroup((prev) =>
      prev[workGroup] ? prev : { ...prev, [workGroup]: serverRangeYears }
    );
    setLocalYearRangesByGroup((prev) => ({ ...prev, [workGroup]: nextRanges }));
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
    setLocalYearRangesByGroup((prev) => {
      if (!(groupName in prev)) return prev;
      const next = { ...prev };
      delete next[groupName];
      return next;
    });
  };

  const saveSeasonRange = async ({ clear = false } = {}) => {
    if (!workGroup) return;
    setSavingTracked(true);
    setError("");
    setSuccess("");
    try {
      const rangesPayload = {};
      if (!clear) {
        for (const y of rangeYears) {
          const row = yearRanges[String(y)] || defaultYearRange(y, calendarSeasons);
          rangesPayload[String(y)] = {
            period_start: row.period_start,
            period_end: row.period_end,
          };
        }
      }
      const data = await updatePlansEditorSeasonRange(
        {
          group_name: workGroup,
          season_years: clear ? null : rangeYears,
          year_ranges: clear ? null : rangesPayload,
          clear,
        },
        summaryDate
      );
      applyWriteState(data);
      clearSeasonSelectionDraft(workGroup);
      setSuccess(clear ? "Диапазон сброшен к расчётному." : "Диапазон дат и сезоны сохранены.");
    } catch (e) {
      setError(formatError(e, "Не удалось сохранить диапазон дат"));
    } finally {
      setSavingTracked(false);
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

  const { countedCrops, otherCrops, skippedCrops } = useMemo(() => {
    if (!state || !workGroup || section?.kind !== "nested_3") {
      return { countedCrops: [], otherCrops: [], skippedCrops: [] };
    }
    const crops = new Set(state.crops || []);
    const present = new Set();
    const planRoot = state.effective?.[sectionKey] || state.base?.[sectionKey] || {};
    for (const firms of Object.values(planRoot)) {
      if (!firms || typeof firms !== "object") continue;
      for (const firmCrops of Object.values(firms)) {
        if (!firmCrops || typeof firmCrops !== "object") continue;
        Object.keys(firmCrops).forEach((c) => crops.add(c));
      }
    }
    for (const firmName of firmsForGroup) {
      Object.keys(state.effective?.[sectionKey]?.[workGroup]?.[firmName] || {}).forEach((c) => {
        crops.add(c);
        present.add(c);
      });
      Object.keys(state.base?.[sectionKey]?.[workGroup]?.[firmName] || {}).forEach((c) => {
        crops.add(c);
        present.add(c);
      });
    }
    Object.keys(state.effective?.CROP_REPORT_MODE?.[workGroup] || {}).forEach((c) => crops.add(c));
    crops.delete("Иные культуры");
    present.delete("Иные культуры");
    const ranks = state.effective?.CROP_DISPLAY_RANK?.[workGroup] || {};
    const mode = state.effective?.CROP_REPORT_MODE?.[workGroup] || {};
    const sorted = [...crops].sort((a, b) => {
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
    const countedCrops = [];
    const otherCrops = [];
    const skippedCrops = [];
    for (const crop of sorted) {
      const raw = mode[crop];
      // Нет явного режима и культуры нет в плане этой операции — «Не считать».
      const flag = raw == null || raw === "" ? (present.has(crop) ? 0 : 2) : Number(raw);
      if (flag === 2) skippedCrops.push(crop);
      else if (flag === 1) otherCrops.push(crop);
      else countedCrops.push(crop);
    }
    return { countedCrops, otherCrops, skippedCrops };
  }, [state, sectionKey, workGroup, section, firmsForGroup]);

  const otherLayout = Number(state?.effective?.OTHER_CROPS_LAYOUT?.[workGroup] ?? 0) === 1 ? 1 : 0;

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
    const text = String(rawValue ?? "").replace(",", ".").replace(/\s+/g, "").trim();
    if (text === "") {
      // пустое поле не сохраняем как 0: просто возвращаем прежнее значение
      return false;
    }
    if (!/^[+-]?(\d+(\.\d*)?|\.\d+)$/.test(text)) {
      setError("Введите число");
      return false;
    }
    const num = Number(text);
    if (!Number.isFinite(num)) {
      setError("Введите число");
      return false;
    }
    const fullPath = [sectionKey, ...relativeKeys];
    const current = getNested(state?.effective, fullPath);
    if (typeof current === "number" && Math.abs(current - num) <= 1e-9) {
      return true;
    }
    setSavingTracked(true);
    setError("");
    setSuccess("");
    try {
      const data = await updatePlansEditorValue({
        section_key: sectionKey,
        keys: relativeKeys,
        value: num,
        summaryDate
      });
      applyWriteState(data);
      const note = data?.plans_py_note;
      if (note && String(note).startsWith("Не удалось")) {
        setError(note);
      } else {
        setSuccess(note || "Сохранено");
      }
      return true;
    } catch (e) {
      setError(formatError(e, "Не удалось сохранить"));
      return false;
    } finally {
      setSavingTracked(false);
    }
  };

  const renderOtherCulturesRow = () => (
    <TableRow>
      <TableCell
        title={
          otherLayout
            ? "Свой план, га. Культуры этого блока выводятся отдельными строками по агрофирмам. Строку чисел можно вставить из таблицы."
            : "Свой план, га. Пока одна строка, культуры блока в сводке отдельно не считаются: их гектары сохранены и войдут в строки, когда вернёте вывод по культурам. Строку чисел можно вставить из таблицы."
        }
        sx={{
          fontWeight: 700,
          position: "sticky",
          left: 0,
          zIndex: 2,
          bgcolor: "background.paper"
        }}
      >
        Иные культуры
      </TableCell>
      {firmsForGroup.map((firmName, firmIndex) => {
        const relativeKeys = [workGroup, firmName, "Иные культуры"];
        const fullPath = [sectionKey, ...relativeKeys];
        const effVal = getNested(state?.effective, fullPath);
        const baseVal = getNested(state?.base, fullPath);
        const edited = isEdited(
          state?.base?.[sectionKey]?.[workGroup],
          state?.effective?.[sectionKey]?.[workGroup],
          [firmName, "Иные культуры"]
        );
        const plan = state?.effective?.HARDCODED_PLAN?.[workGroup]?.[firmName] || {};
        let folded = 0;
        for (const name of otherCrops) {
          folded += Number(plan[name] || 0);
        }
        const own = Number(effVal || 0);
        const reportTotal = own + folded;
        return (
          <TableCell key={`other-${firmName}`} align="center" sx={{ p: 0.75, minWidth: 148 }}>
            <PlanMatrixNumberField
              relativeKeys={relativeKeys}
              effVal={effVal}
              baseVal={baseVal}
              edited={edited}
              saving={saving}
              onSave={saveValue}
              onPasteText={async (text) => {
                const cells = text
                  .replace(/\r/g, "")
                  .split("\n")[0]
                  .split("\t")
                  .map((part) => part.trim());
                if (cells.length < 2) return;
                for (let offset = 0; offset < cells.length; offset += 1) {
                  const firm = firmsForGroup[firmIndex + offset];
                  if (!firm || cells[offset] === "") continue;
                  const ok = await saveValue([workGroup, firm, "Иные культуры"], cells[offset]);
                  if (!ok) break;
                }
              }}
            />
            {otherLayout ? (
              <Typography variant="caption" color="text.secondary" display="block">
                в сводке отдельно
              </Typography>
            ) : folded ? (
              <Typography variant="caption" color="text.secondary" display="block">
                в сводке {reportTotal.toLocaleString("ru-RU", { maximumFractionDigits: 2 })}
              </Typography>
            ) : null}
          </TableCell>
        );
      })}
      <TableCell align="center" sx={{ ...modeStickySx, color: "text.secondary" }}>
        свой план
      </TableCell>
      <TableCell sx={rankStickySx} />
    </TableRow>
  );

  const renderModeDivider = (kind) => {
    const targetMode = !dragCrop ? null : kind === "other" ? (dragCrop.mode === 1 ? 0 : 1) : dragCrop.mode === 2 ? 1 : 2;
    const active = Boolean(dragCrop) && targetMode !== dragCrop.mode;
    const idleLabel = kind === "other" ? "Иные культуры" : "Не учитываются";
    const hint =
      targetMode === 0
        ? "Отпустите здесь — культура будет учитываться"
        : targetMode === 1
          ? "Отпустите здесь — культура войдёт в «Иные культуры»"
          : "Отпустите здесь — культура не будет учитываться";
    return (
      <TableRow
        onDragOver={(event) => {
          if (!active) return;
          event.preventDefault();
        }}
        onDrop={(event) => {
          event.preventDefault();
          if (!dragCrop || targetMode == null || targetMode === dragCrop.mode) return;
          saveCropMode(dragCrop.name, targetMode);
        }}
      >
        <TableCell
          colSpan={firmsForGroup.length + 3}
          sx={{
            py: active ? 1.25 : 0.75,
            borderTop: "2px solid",
            borderBottom: "2px solid",
            borderColor: active ? "primary.main" : kind === "other" ? "info.main" : "divider",
            bgcolor: (t) =>
              active
                ? t.palette.mode === "light"
                  ? "rgba(25, 118, 210, 0.12)"
                  : "rgba(144, 202, 249, 0.16)"
                : t.palette.mode === "light"
                  ? "grey.100"
                  : "rgba(255,255,255,0.06)"
          }}
        >
          <Typography variant="caption" color="text.secondary">
            {active ? hint : idleLabel}
          </Typography>
        </TableCell>
      </TableRow>
    );
  };

  const renderCropRow = (crop, sectionMode) => {
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
        onDragOver={(event) => {
          if (!dragCrop || dragCrop.mode === sectionMode) return;
          event.preventDefault();
        }}
        onDrop={(event) => {
          event.preventDefault();
          if (!dragCrop || dragCrop.name === crop || dragCrop.mode === sectionMode) return;
          saveCropMode(dragCrop.name, sectionMode);
        }}
        sx={
          hidden
            ? {
                opacity: dragCrop?.name === crop ? 0.35 : 0.55,
                bgcolor: (t) =>
                  t.palette.mode === "light" ? "action.hover" : "rgba(255,255,255,0.04)"
              }
            : dragCrop?.name === crop
              ? { opacity: 0.35 }
              : undefined
        }
      >
        <TableCell
          draggable={!saving}
          onDragStart={(event) => {
            event.dataTransfer.setData("text/plain", crop);
            event.dataTransfer.effectAllowed = "move";
            setDragCrop({ name: crop, mode: sectionMode });
          }}
          onDragEnd={() => setDragCrop(null)}
          title="Перетащите через черту в другой блок"
          sx={{
            fontWeight: 500,
            position: "sticky",
            left: 0,
            zIndex: 2,
            bgcolor: "background.paper",
            whiteSpace: "nowrap",
            cursor: saving ? "default" : "grab"
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
                        setSavingTracked(true);
                        setError("");
                        setSuccess("");
                        try {
                          const data = await updatePlansEditorValue({
                            section_key: "CROP_FIRM_HIDDEN",
                            keys: [workGroup, firmName, crop],
                            value: firmHidden ? 0 : 1,
                            summaryDate
                          });
                          applyWriteState(data);
                          setSuccess(
                            firmHidden
                              ? `«${crop}» снова видна для ${firmName}.`
                              : `«${crop}» скрыта для ${firmName}.`
                          );
                        } catch (e) {
                          setError(formatError(e, "Не удалось сохранить скрытие по АФ"));
                        } finally {
                          setSavingTracked(false);
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
        <TableCell align="center" sx={{ ...modeStickySx, p: 0.5 }}>
          <Select
            size="small"
            value={sectionMode}
            disabled={saving}
            onChange={(event) => {
              saveCropMode(crop, Number(event.target.value));
            }}
            MenuProps={{ disableScrollLock: true }}
            sx={{ minWidth: 148, fontSize: 13 }}
          >
            <MenuItem value={0}>Учитываются</MenuItem>
            <MenuItem value={1}>Иные культуры</MenuItem>
            <MenuItem value={2}>Не учитываются</MenuItem>
          </Select>
        </TableCell>
        <TableCell align="center" sx={{ ...rankStickySx, p: 0.75 }}>
          <PlanMatrixNumberField
            relativeKeys={[workGroup, crop]}
            effVal={rankVal ?? ""}
            baseVal={state?.base?.CROP_DISPLAY_RANK?.[workGroup]?.[crop]}
            edited={rankEdited}
            saving={saving}
            onSave={async (keys, raw) => {
              const trimmed = String(raw).replace(",", ".").trim();
              if (trimmed === "") {
                return false;
              }
              const num = Number(trimmed);
              if (!Number.isFinite(num) || num < 0) {
                setError("Ранг: целое число ≥ 0 (0 = скрыть)");
                return false;
              }
              setSavingTracked(true);
              setError("");
              setSuccess("");
              try {
                const data = await updatePlansEditorValue({
                  section_key: "CROP_DISPLAY_RANK",
                  keys,
                  value: Math.trunc(num),
                  summaryDate
                });
                applyWriteState(data);
                setSuccess("Ранг культуры сохранён.");
                return true;
              } catch (e) {
                setError(formatError(e, "Не удалось сохранить ранг"));
                return false;
              } finally {
                setSavingTracked(false);
              }
            }}
          />
        </TableCell>
      </TableRow>
    );
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

          {otherEditors.length > 0 && (
            <Alert severity="warning" sx={{ py: 0.5 }}>
              Планы одновременно правит <strong>{otherEditors.map((person) => person.name).join(", ")}</strong>.
              Сохранённые изменения подтягиваются каждые 5 секунд.
            </Alert>
          )}
          {error && <Alert severity="error">{error}</Alert>}
          {success && <Alert severity="success">{success}</Alert>}

          <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
            <Button variant="text" size="small" disabled={loading || saving} onClick={reload}>
              Обновить
            </Button>
            <Button
              variant="outlined"
              size="small"
              disabled={loading || saving}
              onClick={async () => {
                setSavingTracked(true);
                setError("");
                setSuccess("");
                try {
                  const data = await syncPlansPy(summaryDate);
                  applyWriteState(data);
                  const note = data?.plans_py_note || "plans.py синхронизирован";
                  if (String(note).startsWith("Не удалось")) setError(note);
                  else setSuccess(note);
                } catch (e) {
                  setError(formatError(e, "Не удалось синхронизировать plans.py"));
                } finally {
                  setSavingTracked(false);
                }
              }}
            >
              Синхронизация plans.py
            </Button>
            <Button
              variant="outlined"
              size="small"
              disabled={loading || !state}
              onClick={() =>
                downloadAgroOperationsJson(state, {
                  dirtySeasonGroups,
                  localRangeYearsByGroup,
                  localYearRangesByGroup,
                })
              }
            >
              Скачать json
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
              onSaved={applyWriteState}
              saving={saving}
              setSaving={setSavingTracked}
              setError={setError}
              setSuccess={setSuccess}
            />
          ) : section?.kind === "work_groups" ? (
            <PlansWorkGroupsEditor
              groups={state?.effective?.WORK_GROUPS ?? state?.base?.WORK_GROUPS ?? EMPTY_WORK_GROUPS}
              lockedNames={(state?.base?.WORK_GROUPS || []).map((g) => g?.name).filter(Boolean)}
              summaryDate={summaryDate}
              onSaved={applyWriteState}
              saving={saving}
              setSaving={setSavingTracked}
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
          {section?.kind === "nested_3" && workGroup && countedCrops.length + otherCrops.length + skippedCrops.length > 0 && (
            <Stack spacing={1} sx={{ mb: 1 }}>
              <Typography variant="caption" color="text.secondary" display="block">
                Учитываются: {countedCrops.length}. Иные культуры: {otherCrops.length}. Не учитываются:{" "}
                {skippedCrops.length}. Название можно перетащить через черту.
              </Typography>
              <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
                <Typography variant="caption" color="text.secondary">
                  Иные культуры в сводках
                </Typography>
                <Select
                  size="small"
                  value={otherLayout}
                  disabled={saving}
                  onChange={(event) => {
                    saveOtherLayout(Number(event.target.value));
                  }}
                  MenuProps={{ disableScrollLock: true }}
                  sx={{ minWidth: 280, fontSize: 13 }}
                >
                  <MenuItem value={0}>Одной строкой, по агрофирмам</MenuItem>
                  <MenuItem value={1}>По культурам и агрофирмам</MenuItem>
                </Select>
              </Stack>
            </Stack>
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
                </Stack>
                <Stack
                  direction={{ xs: "column", md: "row" }}
                  spacing={2}
                  alignItems="flex-start"
                >
                  <Stack spacing={0.75} sx={{ flex: "1 1 auto" }}>
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
                  <Stack spacing={1} sx={{ flex: "0 0 auto" }}>
                    {[...rangeYears].sort((a, b) => a - b).map((y) => {
                      const row = yearRanges[String(y)] || defaultYearRange(y, calendarSeasons);
                      return (
                        <Stack key={y} direction="row" spacing={1} alignItems="center">
                          <Typography variant="body2" fontWeight={700} sx={{ width: 52 }}>
                            {y}
                          </Typography>
                          <TextField
                            size="small"
                            type="date"
                            label="Начало"
                            value={row.period_start || ""}
                            onChange={(e) => updateYearRange(y, "period_start", e.target.value)}
                            disabled={saving}
                            InputLabelProps={{ shrink: true }}
                            sx={{ width: 168 }}
                          />
                          <TextField
                            size="small"
                            type="date"
                            label="Конец"
                            value={row.period_end || ""}
                            onChange={(e) => updateYearRange(y, "period_end", e.target.value)}
                            disabled={saving}
                            InputLabelProps={{ shrink: true }}
                            sx={{ width: 168 }}
                          />
                        </Stack>
                      );
                    })}
                  </Stack>
                </Stack>
                <Typography variant="caption" color="text.secondary">
                  У каждого выбранного сезона свой диапазон дат. Если его ещё не сохраняли, даты берутся
                  из вкладки «Сезоны». По умолчанию сезоны:{" "}
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
                  <Grid key={key} size={{ xs: 12, sm: 6, md: 4, lg: 3 }}>
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
                  <Grid key={firmName} size={{ xs: 12, sm: 6, md: 4, lg: 3 }}>
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
                sx={{
                  minWidth: Math.max(960, firmsForGroup.length * 168 + 280),
                  "& .MuiTableCell-head": { zIndex: 4, bgcolor: "background.paper" }
                }}
              >
                <TableHead>
                  <TableRow>
                    <TableCell
                      sx={{
                        fontWeight: 700,
                        minWidth: 180,
                        position: "sticky",
                        left: 0,
                        top: 0,
                        zIndex: 5,
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
                      sx={{ ...modeStickySx, top: 0, zIndex: 5, fontWeight: 700 }}
                      title="Одной строкой — в сводке только «Иные культуры» по агрофирмам. По культурам — отдельные строки. Не учитываются — гектары в сводки не входят."
                    >
                      Учёт в сводках
                    </TableCell>
                    <TableCell
                      align="center"
                      sx={{ ...rankStickySx, top: 0, zIndex: 6, fontWeight: 700 }}
                      title="0 — скрыть культуру; 1 — первая; 2 — вторая…"
                    >
                      Ранг
                    </TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {countedCrops.map((crop) => renderCropRow(crop, 0))}
                  {renderModeDivider("other")}
                  {otherCrops.map((crop) => renderCropRow(crop, 1))}
                  {renderOtherCulturesRow()}
                  {renderModeDivider("skip")}
                  {skippedCrops.map((crop) => renderCropRow(crop, 2))}
                </TableBody>
              </Table>
            </TableContainer>
          )}

          {section?.kind === "nested_3" && workGroup && countedCrops.length + otherCrops.length + skippedCrops.length === 0 && (
            <Alert severity="info">Для выбранной группы работ нет данных.</Alert>
          )}
            </>
          )}
        </Box>
      </Box>
    </Box>
  );
}
