import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Autocomplete,
  Box,
  Button,
  Chip,
  CircularProgress,
  FormControl,
  InputAdornment,
  InputLabel,
  LinearProgress,
  MenuItem,
  Select,
  Stack,
  TextField,
  Typography,
  createFilterOptions,
} from "@mui/material";
import { alpha, useTheme } from "@mui/material/styles";
import { BarChart } from "@mui/x-charts/BarChart";
import { LineChart } from "@mui/x-charts/LineChart";
import PlayArrowRoundedIcon from "@mui/icons-material/PlayArrowRounded";
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";

import { fetchHubOptions, fetchHubQuery } from "../../api";
import HubTable, { fmtNum } from "./HubTable";

const POPPER = { popper: { sx: { zIndex: 1600 } } };
const limitOptions = createFilterOptions({ limit: 80 });

const pad = (n) => String(n).padStart(2, "0");
const isoOf = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

function parseIso(iso) {
  const [y, m, d] = String(iso || "").split("-").map(Number);
  return y && m && d ? new Date(y, m - 1, d) : new Date();
}

function addDays(iso, n) {
  const d = parseIso(iso);
  d.setDate(d.getDate() + n);
  return isoOf(d);
}

export function defaultPeriod(item, anchorIso) {
  const end = anchorIso || isoOf(new Date());
  if (item.since === "season") return { from: `${parseIso(end).getFullYear()}-01-01`, to: end };
  return { from: addDays(end, -29), to: end };
}

const errText = (e) => {
  const d = e?.response?.data?.detail;
  return typeof d === "string" ? d : e?.message || "Ошибка загрузки";
};

function SummaryCards({ items }) {
  const theme = useTheme();
  if (!items?.length) return null;
  return (
    <Box sx={{ display: "grid", gap: 1.25, gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))" }}>
      {items.map((s) => {
        const warn = s.tone === "warn";
        const tone = warn ? theme.palette.warning.main : theme.palette.primary.main;
        return (
          <Box
            key={s.label}
            sx={{
              p: 1.5,
              borderRadius: 2,
              border: `1px solid ${alpha(tone, warn ? 0.5 : 0.22)}`,
              bgcolor: alpha(tone, theme.palette.mode === "dark" ? 0.1 : 0.06),
            }}
          >
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", lineHeight: 1.25 }}>
              {s.label}
            </Typography>
            <Typography variant="h6" sx={{ fontWeight: 800, lineHeight: 1.3, color: warn ? "warning.main" : "text.primary" }}>
              {typeof s.value === "number" ? fmtNum(s.value, 2) : s.value}
            </Typography>
            {s.hint && (
              <Typography variant="caption" color="text.secondary">
                {s.hint}
              </Typography>
            )}
          </Box>
        );
      })}
    </Box>
  );
}

function HubChart({ spec, rows }) {
  const theme = useTheme();
  const colors = [theme.palette.primary.main, theme.palette.secondary.main, theme.palette.success.main, theme.palette.warning.main];
  const data = (spec.data || rows || []).slice(0, spec.limit || 60);
  if (!data.length) return null;
  const labels = data.map((d) => String(d[spec.x] ?? ""));
  const series = spec.series.map((s, i) => ({
    data: data.map((d) => Number(d[s.key]) || 0),
    label: s.label,
    color: colors[i % colors.length],
    valueFormatter: (v) => fmtNum(v, 2),
    ...(spec.type === "line" ? { showMark: false, curve: "monotoneX" } : null),
  }));
  return (
    <Box sx={{ border: 1, borderColor: "divider", borderRadius: 2, p: 1.5, bgcolor: "background.paper" }}>
      {spec.type === "line" ? (
        <LineChart
          height={260}
          xAxis={[{ scaleType: "point", data: labels, tickLabelStyle: { fontSize: 10 } }]}
          series={series}
          margin={{ left: 48, right: 16, top: 24, bottom: 36 }}
        />
      ) : spec.horizontal ? (
        <BarChart
          layout="horizontal"
          height={Math.max(220, Math.min(labels.length, 16) * 30 + 50)}
          yAxis={[{ scaleType: "band", data: labels, tickLabelStyle: { fontSize: 11 } }]}
          series={series}
          margin={{ left: 190, right: 16, top: 16, bottom: 30 }}
        />
      ) : (
        <BarChart
          height={280}
          xAxis={[{ scaleType: "band", data: labels, tickLabelStyle: { fontSize: 11 } }]}
          series={series}
          margin={{ left: 54, right: 16, top: 24, bottom: 56 }}
        />
      )}
    </Box>
  );
}

function PickerField({ label, value, onChange, options, loading, helper }) {
  return (
    <Autocomplete
      size="small"
      sx={{ minWidth: 260, flex: "1 1 260px", maxWidth: 420 }}
      options={options}
      value={value}
      loading={loading}
      onChange={(_, v) => onChange(v)}
      filterOptions={limitOptions}
      getOptionLabel={(o) => o.label || ""}
      isOptionEqualToValue={(a, b) => a.id === b.id}
      slotProps={POPPER}
      noOptionsText="Ничего не найдено"
      renderOption={(props, o) => (
        <li {...props} key={o.id}>
          <Stack direction="row" spacing={1} sx={{ width: "100%" }} justifyContent="space-between">
            <span>{o.label}</span>
            {o.hint ? (
              <Typography variant="caption" color="text.secondary">
                {o.hint}
              </Typography>
            ) : null}
          </Stack>
        </li>
      )}
      renderInput={(params) => (
        <TextField
          {...params}
          label={label}
          helperText={helper}
          InputProps={{
            ...params.InputProps,
            endAdornment: (
              <>
                {loading ? <CircularProgress size={16} /> : null}
                {params.InputProps.endAdornment}
              </>
            ),
          }}
        />
      )}
    />
  );
}

/**
 * Фильтры + результат одного среза.
 * saved/onSave хранят выбранные фильтры между переключениями подтем.
 */
export default function HubResult({ item, anchorDate, groups, saved, onSave, optionsCache, onDrill }) {
  const theme = useTheme();
  const f = item.filters || {};
  const initial = useMemo(() => {
    const per = defaultPeriod(item, anchorDate);
    return {
      from: saved?.from || per.from,
      to: saved?.to || per.to,
      group: saved?.group || "",
      machine: saved?.machine || null,
      field: saved?.field || null,
      threshold: saved?.threshold ?? (f.threshold ? String(f.threshold.default) : ""),
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [item.id]);

  const [draft, setDraft] = useState(initial);
  const [state, setState] = useState({ loading: false, data: null, error: "" });
  const [opts, setOpts] = useState({ machines: optionsCache.current.machines || [], fields: optionsCache.current.fields || [] });
  const [optsLoading, setOptsLoading] = useState(false);
  const reqId = useRef(0);

  const needMachine = Boolean(f.machine) && !draft.machine;
  const needField = Boolean(f.field) && !draft.field;
  const needGroup = f.group === "required" && !draft.group;
  const periodBad = !draft.from || !draft.to || draft.to < draft.from;
  const blocked = needMachine || needField || needGroup || periodBad;

  useEffect(() => {
    const kind = f.machine ? "machines" : f.field ? "fields" : null;
    if (!kind || optionsCache.current[kind]) return undefined;
    let alive = true;
    setOptsLoading(true);
    fetchHubOptions(kind, { date_from: draft.from, date_to: draft.to })
      .then((items) => {
        optionsCache.current[kind] = items;
        if (alive) setOpts((o) => ({ ...o, [kind]: items }));
      })
      .catch(() => {})
      .finally(() => alive && setOptsLoading(false));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [item.id]);

  const run = useCallback(
    async (d) => {
      const id = ++reqId.current;
      setState((s) => ({ ...s, loading: true, error: "" }));
      const th = f.threshold && d.threshold !== "" ? Number(d.threshold) : undefined;
      try {
        const data = await fetchHubQuery(item.id, {
          date_from: d.from,
          date_to: d.to,
          group: d.group || undefined,
          machine_id: d.machine?.id,
          field_id: d.field?.id,
          threshold: th !== undefined && Number.isFinite(th) ? (f.threshold.toParam ? f.threshold.toParam(th) : th) : undefined,
        });
        if (id === reqId.current) setState({ loading: false, data, error: "" });
      } catch (e) {
        if (id === reqId.current) setState({ loading: false, data: null, error: errText(e) });
      }
    },
    [item.id, f.threshold],
  );

  // первый запуск при открытии подтемы, если ничего выбирать не нужно
  useEffect(() => {
    if (!blocked) run(initial);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [item.id]);

  function apply() {
    if (blocked) return;
    onSave?.(draft);
    run(draft);
  }

  function setPreset(days) {
    const to = anchorDate || isoOf(new Date());
    setDraft((d) => ({ ...d, from: addDays(to, -(days - 1)), to }));
  }

  function setSeason() {
    const to = anchorDate || isoOf(new Date());
    setDraft((d) => ({ ...d, from: `${parseIso(to).getFullYear()}-01-01`, to }));
  }

  const { data, loading, error } = state;
  const hint = needMachine ? "Выберите машину" : needField ? "Выберите поле" : needGroup ? "Выберите группу работ" : periodBad ? "Проверьте период" : "";
  const filename = `${item.id}_${draft.from}_${draft.to}`;

  return (
    <Stack spacing={2}>
      <Box
        sx={{
          p: 1.75,
          borderRadius: 2.5,
          border: 1,
          borderColor: "divider",
          background: `linear-gradient(135deg, ${alpha(theme.palette.primary.main, 0.07)}, transparent 70%)`,
        }}
      >
        <Stack direction="row" spacing={1.25} useFlexGap sx={{ flexWrap: "wrap", rowGap: 1.25 }} alignItems="flex-start">
          <TextField
            size="small"
            type="date"
            label="С"
            value={draft.from}
            onChange={(e) => setDraft((d) => ({ ...d, from: e.target.value }))}
            InputLabelProps={{ shrink: true }}
            sx={{ width: 160 }}
          />
          <TextField
            size="small"
            type="date"
            label="По (включительно)"
            value={draft.to}
            onChange={(e) => setDraft((d) => ({ ...d, to: e.target.value }))}
            InputLabelProps={{ shrink: true }}
            sx={{ width: 190 }}
          />
          {f.group && (
            <FormControl size="small" sx={{ minWidth: 240, flex: "1 1 240px", maxWidth: 360 }}>
              <InputLabel>Группа работ</InputLabel>
              <Select
                label="Группа работ"
                value={draft.group}
                onChange={(e) => setDraft((d) => ({ ...d, group: e.target.value }))}
                MenuProps={{ sx: { zIndex: 1600 } }}
              >
                {f.group !== "required" && <MenuItem value="">Все группы</MenuItem>}
                {groups.map((g) => (
                  <MenuItem key={g} value={g}>
                    {g}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
          )}
          {f.machine && (
            <PickerField label="Машина" value={draft.machine} onChange={(v) => setDraft((d) => ({ ...d, machine: v }))} options={opts.machines} loading={optsLoading} />
          )}
          {f.field && (
            <PickerField label="Поле" value={draft.field} onChange={(v) => setDraft((d) => ({ ...d, field: v }))} options={opts.fields} loading={optsLoading} />
          )}
          {f.threshold && (
            <TextField
              size="small"
              type="number"
              label={f.threshold.label}
              value={draft.threshold}
              onChange={(e) => setDraft((d) => ({ ...d, threshold: e.target.value }))}
              inputProps={{ min: f.threshold.min, step: f.threshold.step }}
              sx={{ width: 210 }}
              onKeyDown={(e) => e.key === "Enter" && apply()}
            />
          )}
        </Stack>

        <Stack direction="row" spacing={1} alignItems="center" sx={{ mt: 1.5, flexWrap: "wrap", rowGap: 1 }}>
          {[
            ["Один день", () => setDraft((d) => ({ ...d, from: anchorDate || isoOf(new Date()), to: anchorDate || isoOf(new Date()) }))],
            ["7 дней", () => setPreset(7)],
            ["30 дней", () => setPreset(30)],
            ["С начала года", setSeason],
          ].map(([label, fn]) => (
            <Chip key={label} size="small" label={label} variant="outlined" onClick={fn} />
          ))}
          <Box sx={{ flex: 1 }} />
          {hint && (
            <Typography variant="caption" color="warning.main">
              {hint}
            </Typography>
          )}
          <Button
            variant="contained"
            size="small"
            startIcon={loading ? <CircularProgress size={14} color="inherit" /> : <PlayArrowRoundedIcon />}
            onClick={apply}
            disabled={blocked || loading}
          >
            {loading ? "Считаю…" : "Показать"}
          </Button>
        </Stack>
      </Box>

      {loading && <LinearProgress sx={{ borderRadius: 1 }} />}
      {error && <Alert severity="error">{error}</Alert>}

      {data && (
        <Stack spacing={2} sx={{ opacity: loading ? 0.55 : 1, transition: "opacity .15s" }}>
          <SummaryCards items={data.summary} />
          {data.note && (
            <Stack
              direction="row"
              spacing={1}
              sx={{
                p: 1.25,
                borderRadius: 2,
                borderLeft: `3px solid ${theme.palette.info.main}`,
                bgcolor: alpha(theme.palette.info.main, 0.08),
              }}
            >
              <InfoOutlinedIcon fontSize="small" color="info" sx={{ mt: 0.15 }} />
              <Typography variant="body2" color="text.secondary" sx={{ whiteSpace: "pre-line" }}>
                {data.note}
              </Typography>
            </Stack>
          )}
          {data.chart && data.rows.length > 0 && <HubChart spec={data.chart} rows={data.rows} />}
          {data.rows.length > 0 ? (
            <HubTable
              key={`${item.id}-${data.date_from}-${data.date_to}-${data.total_rows}`}
              columns={data.columns}
              rows={data.rows}
              tree={data.tree}
              truncated={data.truncated}
              totalRows={data.total_rows}
              filename={filename}
              onDrill={onDrill ? (kind, row) => onDrill(kind, row, data) : undefined}
            />
          ) : (
            !data.note && <Alert severity="info">За выбранный период ничего не найдено.</Alert>
          )}
        </Stack>
      )}
    </Stack>
  );
}
