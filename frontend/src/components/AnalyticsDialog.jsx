import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogContent,
  DialogTitle,
  FormControl,
  IconButton,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  Tab,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Tabs,
  TextField,
  Typography,
} from "@mui/material";
import CloseIcon from "@mui/icons-material/Close";
import { BarChart } from "@mui/x-charts/BarChart";
import { LineChart } from "@mui/x-charts/LineChart";
import { PieChart } from "@mui/x-charts/PieChart";

import { fetchAnalytics } from "../api";

const PALETTE = ["#2563eb", "#16a34a", "#f59e0b", "#dc2626", "#7c3aed", "#0891b2", "#db2777", "#65a30d", "#ea580c", "#4338ca"];
const fmt = (v, d = 1) => (v == null ? "—" : Number(v).toLocaleString("ru-RU", { maximumFractionDigits: d }));
const errText = (e) => {
  const d = e?.response?.data?.detail;
  return typeof d === "string" ? d : e?.message || "Ошибка загрузки";
};
const iso = (d) => d.toISOString().slice(0, 10);

const TABS = [
  ["pace", "Темп работ"],
  ["machines", "Техника"],
  ["coverage", "Покрытие полей"],
  ["crops", "Культуры"],
  ["quality", "Качество данных"],
  ["restate", "Правки задним числом"],
  ["season", "Сезон к сезону"],
];

function useLoad(path, params, enabled) {
  const [state, setState] = useState({ loading: false, data: null, error: "" });
  const key = JSON.stringify(params);
  useEffect(() => {
    if (!enabled) return undefined;
    let alive = true;
    setState((s) => ({ ...s, loading: true, error: "" }));
    fetchAnalytics(path, params)
      .then((data) => alive && setState({ loading: false, data, error: "" }))
      .catch((e) => alive && setState({ loading: false, data: null, error: errText(e) }));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, key, enabled]);
  return state;
}

function Loaded({ state, children }) {
  if (state.loading) return <Box sx={{ py: 4, textAlign: "center" }}><CircularProgress size={28} /></Box>;
  if (state.error) return <Alert severity="error">{state.error}</Alert>;
  if (!state.data) return null;
  return children(state.data);
}

function SimpleTable({ cols, rows, max = 200 }) {
  return (
    <Box sx={{ overflow: "auto", maxHeight: 420, border: 1, borderColor: "divider", borderRadius: 1 }}>
      <Table size="small" stickyHeader>
        <TableHead>
          <TableRow>
            {cols.map((c) => (
              <TableCell key={c.key} align={c.num ? "right" : "left"}>{c.title}</TableCell>
            ))}
          </TableRow>
        </TableHead>
        <TableBody>
          {rows.slice(0, max).map((r, i) => (
            <TableRow key={i} hover>
              {cols.map((c) => (
                <TableCell key={c.key} align={c.num ? "right" : "left"}>
                  {c.render ? c.render(r) : r[c.key] ?? "—"}
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </Box>
  );
}

function PaceTab({ range, group }) {
  const st = useLoad("pace", { date_from: range.from, date_to: range.to, groups: group || "" }, true);
  const [mode, setMode] = useState("daily");
  return (
    <Loaded state={st}>
      {(d) => {
        const series = d.series.slice(0, 8);
        if (!series.length) return <Alert severity="info">Нет данных за период.</Alert>;
        return (
          <Stack spacing={2}>
            <Stack direction="row" spacing={1}>
              {[["daily", "По дням"], ["avg7", "Среднее за 7 дн."], ["cumulative", "Накопительно"]].map(([k, t]) => (
                <Chip key={k} label={t} color={mode === k ? "primary" : "default"} onClick={() => setMode(k)} />
              ))}
            </Stack>
            <LineChart
              height={340}
              xAxis={[{ scaleType: "point", data: d.days, tickLabelStyle: { fontSize: 10 } }]}
              series={series.map((s, i) => ({
                data: s[mode], label: s.group, showMark: false, color: PALETTE[i % PALETTE.length],
                valueFormatter: (v) => `${fmt(v)} га`,
              }))}
            />
            <SimpleTable
              cols={[
                { key: "group", title: "Группа работ" },
                { key: "total", title: "Итого, га", num: true, render: (r) => fmt(r.total) },
                { key: "last7_avg", title: "Темп за 7 дней, га/сут", num: true, render: (r) => fmt(r.last7_avg) },
              ]}
              rows={d.series}
            />
          </Stack>
        );
      }}
    </Loaded>
  );
}

function MachinesTab({ range, group }) {
  const st = useLoad("machines", { group, date_from: range.from, date_to: range.to }, !!group);
  if (!group) return <Alert severity="info">Выберите группу работ сверху.</Alert>;
  return (
    <Loaded state={st}>
      {(d) => (
        <Stack spacing={2}>
          <Typography variant="body2" color="text.secondary">
            Машин: {d.n_machines}. Средняя выработка за машино-смену: {fmt(d.avg_ha_per_machine_day)} га.
            «Простой» — дни без работы между первой и последней сменой; «слабых смен» — выработка меньше 10 га.
          </Typography>
          <BarChart
            layout="horizontal"
            height={Math.max(260, Math.min(d.rows.length, 20) * 28 + 40)}
            yAxis={[{ scaleType: "band", data: d.rows.slice(0, 20).map((r) => r.machine), tickLabelStyle: { fontSize: 11 } }]}
            series={[{ data: d.rows.slice(0, 20).map((r) => r.ha), label: "Площадь, га", color: PALETTE[0], valueFormatter: (v) => fmt(v) }]}
            margin={{ left: 170 }}
          />
          <SimpleTable
            cols={[
              { key: "machine", title: "Машина" },
              { key: "ha", title: "Га", num: true, render: (r) => fmt(r.ha) },
              { key: "days_worked", title: "Смен", num: true },
              { key: "avg_ha_per_day", title: "Га/смену", num: true, render: (r) => fmt(r.avg_ha_per_day) },
              { key: "best_day_ha", title: "Лучшая смена", num: true, render: (r) => fmt(r.best_day_ha) },
              { key: "idle_days", title: "Простой, дн.", num: true },
              { key: "low_days", title: "Слабых смен", num: true },
              { key: "last_day", title: "Последняя смена" },
            ]}
            rows={d.rows}
          />
        </Stack>
      )}
    </Loaded>
  );
}

function CoverageTab({ range, groups }) {
  const st = useLoad("coverage", { date_from: range.from, date_to: range.to }, true);
  const [grp, setGrp] = useState("");
  const [doneIn, setDoneIn] = useState("");
  const miss = useLoad("fields-missing", { group: grp, done_in: doneIn, date_from: range.from, date_to: range.to }, !!grp);
  return (
    <Stack spacing={2}>
      <Loaded state={st}>
        {(d) => (
          <>
            <Typography variant="body2" color="text.secondary">
              Всего полей в справочнике: {d.total_fields ?? "н/д"}. Считаются поля с работой в выбранном периоде.
            </Typography>
            <BarChart
              layout="horizontal"
              height={Math.max(260, d.rows.length * 28 + 40)}
              yAxis={[{ scaleType: "band", data: d.rows.map((r) => r.group), tickLabelStyle: { fontSize: 11 } }]}
              series={[{ data: d.rows.map((r) => r.fields_done), label: "Полей обработано", color: PALETTE[1] }]}
              margin={{ left: 210 }}
            />
          </>
        )}
      </Loaded>
      <Typography variant="subtitle2">Где операция не сделана, хотя другая уже есть</Typography>
      <Stack direction="row" spacing={1}>
        <FormControl size="small" sx={{ minWidth: 220 }}>
          <InputLabel>Нет операции</InputLabel>
          <Select label="Нет операции" value={grp} onChange={(e) => setGrp(e.target.value)}>
            {groups.map((g) => <MenuItem key={g} value={g}>{g}</MenuItem>)}
          </Select>
        </FormControl>
        <FormControl size="small" sx={{ minWidth: 220 }}>
          <InputLabel>Но есть</InputLabel>
          <Select label="Но есть" value={doneIn} onChange={(e) => setDoneIn(e.target.value)}>
            <MenuItem value="">любая операция</MenuItem>
            {groups.map((g) => <MenuItem key={g} value={g}>{g}</MenuItem>)}
          </Select>
        </FormControl>
      </Stack>
      {grp && (
        <Loaded state={miss}>
          {(d) => (
            <>
              <Typography variant="body2">Полей: {d.count}</Typography>
              <SimpleTable
                cols={[
                  { key: "field", title: "Поле" },
                  { key: "last_day_other", title: "Последняя работа" },
                  { key: "ha_other", title: "Га", num: true, render: (r) => fmt(r.ha_other) },
                ]}
                rows={d.rows}
              />
            </>
          )}
        </Loaded>
      )}
    </Stack>
  );
}

function CropsTab({ range, group }) {
  const st = useLoad("crops", { group, date_from: range.from, date_to: range.to }, true);
  return (
    <Loaded state={st}>
      {(d) => (d.error ? <Alert severity="warning">{d.error}</Alert> : (
        <Stack spacing={2}>
          <PieChart
            height={320}
            series={[{
              data: d.rows.slice(0, 12).map((r, i) => ({ id: i, value: r.ha, label: r.crop })),
              innerRadius: 40, paddingAngle: 1, cornerRadius: 3,
              valueFormatter: (it) => `${fmt(it.value)} га`,
            }]}
          />
          <SimpleTable
            cols={[
              { key: "crop", title: "Культура" },
              { key: "ha", title: "Га", num: true, render: (r) => fmt(r.ha) },
              { key: "pct", title: "Доля, %", num: true, render: (r) => fmt(r.pct) },
            ]}
            rows={d.rows}
          />
        </Stack>
      ))}
    </Loaded>
  );
}

function QualityTab() {
  const st = useLoad("quality", {}, true);
  return (
    <Loaded state={st}>
      {(d) => (
        <Stack spacing={2}>
          <Typography variant="body2" color="text.secondary">
            Заданий: {fmt(d.tasks, 0)}, строк площади: {fmt(d.area_rows, 0)}, всего {fmt(d.area_ha, 0)} га.
            Площадь без почасовых данных: {fmt(d.ha_without_hourly, 0)} га ({fmt(d.ha_without_hourly_pct)}%).
          </Typography>
          <SimpleTable
            cols={[
              { key: "title", title: "Проверка" },
              { key: "count", title: "Кол-во", num: true, render: (r) => fmt(r.count, 0) },
              { key: "pct", title: "Доля, %", num: true, render: (r) => fmt(r.pct, 2) },
            ]}
            rows={d.items}
          />
          {d.tasks_without_mtfmi_by_wt?.length > 0 && (
            <>
              <Typography variant="subtitle2">Задания без строк площади — по типам работ (топ-10)</Typography>
              <SimpleTable
                cols={[
                  { key: "group", title: "Группа" },
                  { key: "wt", title: "Тип работ", num: true },
                  { key: "count", title: "Заданий", num: true },
                ]}
                rows={d.tasks_without_mtfmi_by_wt}
              />
            </>
          )}
        </Stack>
      )}
    </Loaded>
  );
}

function RestateTab() {
  const [dayFrom, setDayFrom] = useState(iso(new Date(Date.now() - 30 * 86400000)));
  const [dayTo, setDayTo] = useState(iso(new Date(Date.now() + 86400000)));
  const st = useLoad("restatements", { day_from: dayFrom, day_to: dayTo }, true);
  const [a, setA] = useState("");
  const [b, setB] = useState("");
  const snaps = useMemo(() => [...(st.data?.snapshots || [])].sort(), [st.data]);
  useEffect(() => {
    if (snaps.length >= 2 && !a && !b) {
      setA(snaps[snaps.length - 2]);
      setB(snaps[snaps.length - 1]);
    }
  }, [snaps, a, b]);
  const diff = useLoad("changes", { snap_a: a, snap_b: b }, !!a && !!b && a !== b);
  return (
    <Stack spacing={2}>
      <Typography variant="body2" color="text.secondary">
        Ежедневные снимки итогов (tools/analytics_snapshot.py) показывают, как менялась площадь за прошлые сменные
        дни после их закрытия: поздние правки заданий и пересчёты почасовой площади.
      </Typography>
      <Stack direction="row" spacing={1}>
        <TextField size="small" type="date" label="Сменные дни с" value={dayFrom} onChange={(e) => setDayFrom(e.target.value)} InputLabelProps={{ shrink: true }} />
        <TextField size="small" type="date" label="по (не включая)" value={dayTo} onChange={(e) => setDayTo(e.target.value)} InputLabelProps={{ shrink: true }} />
      </Stack>
      <Loaded state={st}>
        {(d) => {
          if (d.note) return <Alert severity="info">{d.note}</Alert>;
          if (snaps.length < 2) return <Alert severity="info">Снимков пока {snaps.length}: нужно минимум два (по одному в сутки).</Alert>;
          const rows = d.series.filter((r) => Math.abs(r.delta) > 0.05);
          return (
            <Stack spacing={2}>
              <Typography variant="subtitle2">Суточная площадь: первый и последний снимок ({snaps[0]} … {snaps[snaps.length - 1]})</Typography>
              <LineChart
                height={300}
                xAxis={[{ scaleType: "point", data: d.days, tickLabelStyle: { fontSize: 10 } }]}
                series={[
                  { data: d.series.map((r) => r.first), label: "первый снимок", showMark: false, color: PALETTE[5] },
                  { data: d.series.map((r) => r.last), label: "последний снимок", showMark: false, color: PALETTE[0] },
                ]}
              />
              <Typography variant="subtitle2">Дней, цифры которых менялись: {rows.length}</Typography>
              <SimpleTable
                cols={[
                  { key: "day", title: "Сменный день" },
                  { key: "first", title: "Было, га", num: true, render: (r) => fmt(r.first) },
                  { key: "last", title: "Стало, га", num: true, render: (r) => fmt(r.last) },
                  { key: "delta", title: "Δ, га", num: true, render: (r) => fmt(r.delta) },
                ]}
                rows={rows}
              />
            </Stack>
          );
        }}
      </Loaded>
      {snaps.length >= 2 && (
        <>
          <Typography variant="subtitle2">Сравнить два снимка</Typography>
          <Stack direction="row" spacing={1}>
            {[["Снимок A", a, setA], ["Снимок B", b, setB]].map(([t, v, set]) => (
              <FormControl key={t} size="small" sx={{ minWidth: 160 }}>
                <InputLabel>{t}</InputLabel>
                <Select label={t} value={v} onChange={(e) => set(e.target.value)}>
                  {snaps.map((s) => <MenuItem key={s} value={s}>{s}</MenuItem>)}
                </Select>
              </FormControl>
            ))}
          </Stack>
          {a && b && a !== b && (
            <Loaded state={diff}>
              {(d) => (
                <>
                  <Typography variant="body2">Изменений: {d.count}, суммарно {fmt(d.total_delta)} га</Typography>
                  <SimpleTable
                    cols={[
                      { key: "day", title: "Сменный день" },
                      { key: "group", title: "Группа" },
                      { key: "was", title: "Было, га", num: true, render: (r) => fmt(r.was) },
                      { key: "now", title: "Стало, га", num: true, render: (r) => fmt(r.now) },
                      { key: "delta", title: "Δ, га", num: true, render: (r) => fmt(r.delta) },
                    ]}
                    rows={d.rows}
                  />
                </>
              )}
            </Loaded>
          )}
        </>
      )}
    </Stack>
  );
}

function SeasonTab({ group }) {
  const st = useLoad("season-compare", { group }, true);
  return (
    <Loaded state={st}>
      {(d) => (
        <Stack spacing={2}>
          <Typography variant="body2" color="text.secondary">
            Накопительная площадь по неделям с 1 января ({d.group}). Если в хранилище нет данных за прошлый год,
            его линия будет нулевой.
          </Typography>
          <LineChart
            height={340}
            xAxis={[{ scaleType: "point", data: d.weeks }]}
            series={d.series.map((s, i) => ({ data: s.cumulative, label: `${s.year}: ${fmt(s.total, 0)} га`, showMark: false, color: PALETTE[i] }))}
          />
        </Stack>
      )}
    </Loaded>
  );
}

export default function AnalyticsDialog({ open, onClose }) {
  const [tab, setTab] = useState("pace");
  const [overview, setOverview] = useState(null);
  const [err, setErr] = useState("");
  const [from, setFrom] = useState(iso(new Date(Date.now() - 60 * 86400000)));
  const [to, setTo] = useState(iso(new Date(Date.now() + 86400000)));
  const [group, setGroup] = useState("");

  const load = useCallback(() => {
    setErr("");
    fetchAnalytics("overview")
      .then(setOverview)
      .catch((e) => setErr(errText(e)));
  }, []);
  useEffect(() => {
    if (open) load();
  }, [open, load]);

  const groups = overview?.groups || [];
  const range = useMemo(() => ({ from, to }), [from, to]);
  const needsGroup = tab === "machines";
  const showGroup = tab === "pace" || tab === "machines" || tab === "crops" || tab === "season";

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="xl">
      <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1 }}>
        <Box sx={{ flex: 1 }}>Аналитика по хранилищу заданий</Box>
        <IconButton onClick={onClose} size="small"><CloseIcon /></IconButton>
      </DialogTitle>
      <DialogContent dividers>
        {err && <Alert severity="error" sx={{ mb: 2 }}>{err}</Alert>}
        {overview && (
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1 }}>
            Заданий: {fmt(overview.tasks, 0)} ({overview.first_task_date} … {overview.last_task_date}); база обновлена{" "}
            {overview.store_updated_at}. Цифры — текущее состояние данных, как в сводках.
          </Typography>
        )}
        <Stack direction="row" spacing={1} sx={{ mb: 2, flexWrap: "wrap", rowGap: 1 }}>
          <TextField size="small" type="date" label="С" value={from} onChange={(e) => setFrom(e.target.value)} InputLabelProps={{ shrink: true }} />
          <TextField size="small" type="date" label="По (не включая)" value={to} onChange={(e) => setTo(e.target.value)} InputLabelProps={{ shrink: true }} />
          {showGroup && (
            <FormControl size="small" sx={{ minWidth: 260 }}>
              <InputLabel>Группа работ</InputLabel>
              <Select label="Группа работ" value={group} onChange={(e) => setGroup(e.target.value)}>
                {!needsGroup && <MenuItem value="">Все группы</MenuItem>}
                {groups.map((g) => <MenuItem key={g} value={g}>{g}</MenuItem>)}
              </Select>
            </FormControl>
          )}
          <Button size="small" onClick={load}>Обновить</Button>
        </Stack>
        <Tabs value={tab} onChange={(_, v) => setTab(v)} variant="scrollable" scrollButtons="auto" sx={{ mb: 2 }}>
          {TABS.map(([k, t]) => <Tab key={k} value={k} label={t} />)}
        </Tabs>
        {overview && tab === "pace" && <PaceTab range={range} group={group} />}
        {overview && tab === "machines" && <MachinesTab range={range} group={group} />}
        {overview && tab === "coverage" && <CoverageTab range={range} groups={groups} />}
        {overview && tab === "crops" && <CropsTab range={range} group={group} />}
        {overview && tab === "quality" && <QualityTab />}
        {overview && tab === "restate" && <RestateTab />}
        {overview && tab === "season" && <SeasonTab group={group} />}
      </DialogContent>
    </Dialog>
  );
}
