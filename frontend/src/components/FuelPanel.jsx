import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  FormControl,
  InputLabel,
  Link,
  MenuItem,
  Select,
  Stack,
  Tab,
  Tabs,
  TextField,
  ToggleButton,
  ToggleButtonGroup,
  Typography,
} from "@mui/material";
import DownloadOutlinedIcon from "@mui/icons-material/DownloadOutlined";
import RefreshIcon from "@mui/icons-material/Refresh";
import { BarChart } from "@mui/x-charts/BarChart";

import { fetchFuel } from "../api";
import { KpiCard, SortableTable, downloadCsv, fmt, fmtDate, isoDay, monthBounds } from "./analysis/common";

const KIND_LABEL = {
  over_field: "Перерасход в поле",
  over_distance: "Перерасход на км",
  over_road: "Перерасход на дороге",
  idle_burn: "Расход без выработки",
  road_no_motion: "Дорога без движения",
  machine_systemic: "Систематический перерасход",
};

const KIND_HINT = {
  over_field: "л/га задания выше нормы того же вида работ",
  over_distance: "л/км задания выше нормы того же вида работ",
  over_road: "на дороге л/ч выше нормы того же вида работ",
  idle_burn: "топливо потрачено, а площади и пути нет",
  road_no_motion: "топливо на дороге есть, а времени в пути почти нет",
  machine_systemic: "у машины за период л/га стабильно выше парка",
};

function shiftDays(iso, delta) {
  const d = new Date(`${iso}T12:00:00`);
  d.setDate(d.getDate() + delta);
  return isoDay(d);
}

/** Подсветка л/га и л/ч: красным, если сильно выше медианы по таблице. */
function toneBy(rows, key, minHa = 20) {
  const vals = rows.filter((r) => r[key] != null && (r.ha ?? minHa) >= minHa).map((r) => r[key]).sort((a, b) => a - b);
  if (vals.length < 5) return () => undefined;
  const med = vals[Math.floor(vals.length / 2)];
  return (r) => {
    if (r[key] == null || (r.ha ?? minHa) < minHa) return undefined;
    if (r[key] > med * 1.35) return "bad";
    if (r[key] > med * 1.2) return "warn";
    return undefined;
  };
}

function breakdownColumns(nameKey, nameLabel, nameFn, rows) {
  const toneHa = toneBy(rows, "l_ha");
  const toneRoad = toneBy(rows, "road_l_h", 0);
  const pct = (v) => (v == null ? null : Math.round(v * 100));
  return [
    { key: nameKey, label: nameLabel, render: nameFn, sort: (r) => String(nameFn(r)), csv: nameFn, wrap: true },
    { key: "tasks", label: "Заданий", align: "right", render: (r) => fmt(r.tasks, 0) },
    { key: "ha", label: "Га", align: "right", render: (r) => fmt(r.ha, 1) },
    { key: "fuel_total", label: "Топливо, л", align: "right", render: (r) => fmt(r.fuel_total, 0) },
    { key: "fuel_field", label: "В поле, л", align: "right", render: (r) => fmt(r.fuel_field, 0) },
    { key: "fuel_road", label: "На дороге, л", align: "right", render: (r) => fmt(r.fuel_road, 0) },
    { key: "road_share", label: "Дорога, %", align: "right", render: (r) => (r.road_share == null ? "—" : `${pct(r.road_share)}%`), sort: (r) => r.road_share, csv: (r) => pct(r.road_share) },
    { key: "l_ha", label: "л/га", align: "right", render: (r) => fmt(r.l_ha, 1), tone: toneHa },
    { key: "l_km", label: "л/км", align: "right", render: (r) => fmt(r.l_km, 2) },
    { key: "road_l_h", label: "Дорога, л/ч", align: "right", render: (r) => fmt(r.road_l_h, 1), tone: toneRoad },
    { key: "fuel_share", label: "С данными", align: "right", render: (r) => `${pct(r.fuel_share)}%`, sort: (r) => r.fuel_share, csv: (r) => pct(r.fuel_share) },
  ];
}

function TaskLink({ row }) {
  return row.task_url ? (
    <Link href={row.task_url} target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()}>
      #{row.task_id}
    </Link>
  ) : (
    `#${row.task_id}`
  );
}

function ExportButton({ onClick, disabled }) {
  return (
    <Button size="small" startIcon={<DownloadOutlinedIcon fontSize="small" />} onClick={onClick} disabled={disabled}>
      CSV
    </Button>
  );
}

export default function FuelPanel({ active, summaryDate = "" }) {
  const initial = useMemo(() => monthBounds(summaryDate), [summaryDate]);
  const [dateFrom, setDateFrom] = useState(initial.from);
  const [dateTo, setDateTo] = useState(initial.to);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [tab, setTab] = useState("anomalies");
  const [severity, setSeverity] = useState("all");
  const [kind, setKind] = useState("all");
  const requested = useRef(false);

  const load = useCallback(async (from, to) => {
    setLoading(true);
    setError("");
    try {
      const res = await fetchFuel({ dateFrom: from, dateTo: to });
      if (!res.available) {
        setData(null);
        setError(res.detail || "Хранилище заданий недоступно");
        return;
      }
      setData(res);
    } catch (e) {
      setData(null);
      setError(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (active && !requested.current) {
      requested.current = true;
      load(initial.from, initial.to);
    }
  }, [active, load, initial]);

  function applyPreset(days) {
    const end = summaryDate || isoDay(new Date());
    const start = shiftDays(end, -(days - 1));
    setDateFrom(start);
    setDateTo(end);
    load(start, end);
  }

  function applyMonth() {
    setDateFrom(initial.from);
    setDateTo(initial.to);
    load(initial.from, initial.to);
  }

  const totals = data?.totals;
  const cov = data?.coverage;
  const anomalies = data?.anomalies || [];
  const counts = data?.anomaly_counts || {};
  const critical = anomalies.filter((a) => a.severity === "critical").length;

  const shownAnomalies = useMemo(
    () =>
      anomalies.filter(
        (a) => (severity === "all" || a.severity === severity) && (kind === "all" || a.kind === kind),
      ),
    [anomalies, severity, kind],
  );

  const anomalyColumns = useMemo(
    () => [
      {
        key: "severity",
        label: "Уровень",
        sort: (r) => (r.severity === "critical" ? 2 : 1),
        csv: (r) => (r.severity === "critical" ? "критично" : "внимание"),
        render: (r) => (
          <Chip size="small" color={r.severity === "critical" ? "error" : "warning"} label={r.severity === "critical" ? "Критично" : "Внимание"} />
        ),
      },
      { key: "kind", label: "Тип", wrap: true, render: (r) => KIND_LABEL[r.kind] || r.kind, csv: (r) => KIND_LABEL[r.kind] || r.kind },
      { key: "date", label: "Дата", render: (r) => fmtDate(r.date), sort: (r) => r.date },
      { key: "machine", label: "Машина", wrap: true },
      { key: "driver", label: "Исполнитель", render: (r) => r.driver || (r.driver_id ? `id ${r.driver_id}` : "—"), wrap: true },
      { key: "operation", label: "Вид работ", render: (r) => r.operation || (r.wt ? `вид ${r.wt}` : "—"), wrap: true },
      { key: "text", label: "Что не так", wrap: true, minWidth: 280 },
      { key: "excess_l", label: "Лишних, л", align: "right", render: (r) => fmt(r.excess_l, 0) },
      { key: "task_id", label: "Задание", render: (r) => <TaskLink row={r} />, csv: (r) => r.task_url },
    ],
    [],
  );

  const machineRows = data?.by_machine || [];
  const driverRows = data?.by_driver || [];
  const operationRows = data?.by_operation || [];
  const firmRows = data?.by_firm || [];
  const dayRows = data?.by_day || [];
  const taskRows = data?.tasks || [];

  const machineCols = useMemo(() => breakdownColumns("machine", "Машина", (r) => r.machine || `id ${r.machine_id ?? "—"}`, machineRows), [machineRows]);
  const driverCols = useMemo(() => breakdownColumns("driver", "Исполнитель", (r) => r.driver || "Без исполнителя", driverRows), [driverRows]);
  const operationCols = useMemo(() => breakdownColumns("operation", "Вид работ", (r) => r.operation || "Без вида работ", operationRows), [operationRows]);
  const firmCols = useMemo(() => breakdownColumns("firm", "Агрофирма", (r) => r.firm || "Поле не определено", firmRows), [firmRows]);
  const dayCols = useMemo(
    () => breakdownColumns("date", "День", (r) => fmtDate(r.date), dayRows).map((c, i) => (i === 0 ? { ...c, sort: (r) => r.date } : c)),
    [dayRows],
  );

  const taskCols = useMemo(
    () => [
      { key: "date", label: "Дата", render: (r) => fmtDate(r.date), sort: (r) => r.date },
      { key: "machine", label: "Машина", wrap: true },
      { key: "driver", label: "Исполнитель", render: (r) => r.driver || (r.driver_id ? `id ${r.driver_id}` : "—"), wrap: true },
      { key: "operation", label: "Вид работ", render: (r) => r.operation || (r.wt ? `вид ${r.wt}` : "—"), wrap: true },
      { key: "firm", label: "Агрофирма", render: (r) => r.firm || "—", wrap: true },
      { key: "ha", label: "Га", align: "right", render: (r) => fmt(r.ha, 1) },
      { key: "fuel_total", label: "Топливо, л", align: "right", render: (r) => fmt(r.fuel_total, 0) },
      { key: "fuel_field", label: "В поле, л", align: "right", render: (r) => fmt(r.fuel_field, 0) },
      { key: "fuel_road", label: "На дороге, л", align: "right", render: (r) => fmt(r.fuel_road, 0) },
      { key: "l_ha", label: "л/га", align: "right", render: (r) => fmt(r.l_ha, 1) },
      { key: "road_l_h", label: "Дорога, л/ч", align: "right", render: (r) => fmt(r.road_l_h, 1) },
      { key: "task_id", label: "Задание", render: (r) => <TaskLink row={r} />, csv: (r) => r.task_url },
    ],
    [],
  );

  const noFuelCols = useMemo(
    () => [
      { key: "machine", label: "Машина", wrap: true },
      { key: "tasks", label: "Заданий", align: "right" },
      { key: "ha", label: "Га", align: "right", render: (r) => fmt(r.ha, 1) },
    ],
    [],
  );
  const partialCols = useMemo(
    () => [
      { key: "machine", label: "Машина", wrap: true },
      { key: "area_tasks", label: "Заданий с площадью", align: "right" },
      { key: "missing", label: "Без топлива", align: "right" },
      { key: "missing_share", label: "Доля", align: "right", render: (r) => `${Math.round(r.missing_share * 100)}%`, sort: (r) => r.missing_share },
      { key: "missing_ha", label: "Га без топлива", align: "right", render: (r) => fmt(r.missing_ha, 1) },
    ],
    [],
  );

  const exportCurrent = () => {
    const stamp = `${dateFrom}_${dateTo}`;
    const map = {
      anomalies: [anomalyColumns, shownAnomalies, "аномалии"],
      machines: [machineCols, machineRows, "машины"],
      drivers: [driverCols, driverRows, "исполнители"],
      operations: [operationCols, operationRows, "виды_работ"],
      firms: [firmCols, firmRows, "агрофирмы"],
      days: [dayCols, dayRows, "дни"],
      tasks: [taskCols, taskRows, "задания"],
    };
    const entry = map[tab];
    if (entry) downloadCsv(`fuel_${entry[2]}_${stamp}.csv`, entry[0], entry[1]);
  };

  const coveragePct = cov && cov.tasks ? Math.round((cov.with_fuel / cov.tasks) * 100) : null;
  const periodDays = data ? Math.round((new Date(data.date_to) - new Date(data.date_from)) / 86400000) + 1 : 0;

  return (
    <Stack spacing={2} sx={{ minHeight: 0 }}>
      <Stack direction={{ xs: "column", md: "row" }} spacing={1.5} alignItems={{ md: "center" }} flexWrap="wrap" useFlexGap>
        <TextField label="С" type="date" size="small" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} slotProps={{ inputLabel: { shrink: true } }} />
        <TextField label="По" type="date" size="small" value={dateTo} onChange={(e) => setDateTo(e.target.value)} slotProps={{ inputLabel: { shrink: true } }} />
        <Button variant="contained" onClick={() => load(dateFrom, dateTo)} disabled={loading || !dateFrom || !dateTo} startIcon={loading ? <CircularProgress size={16} color="inherit" /> : <RefreshIcon />}>
          {loading ? "Считаю…" : "Показать"}
        </Button>
        <Stack direction="row" spacing={0.75}>
          <Chip size="small" variant="outlined" label="7 дней" onClick={() => applyPreset(7)} disabled={loading} />
          <Chip size="small" variant="outlined" label="30 дней" onClick={() => applyPreset(30)} disabled={loading} />
          <Chip size="small" variant="outlined" label="Месяц сводки" onClick={applyMonth} disabled={loading} />
        </Stack>
        <Box sx={{ flex: 1 }} />
        <Typography variant="caption" color="text.secondary">
          Топливо и расход по заданиям из хранилища. Модель не используется: 0 токенов.
        </Typography>
      </Stack>

      {error && <Alert severity="warning">{error}</Alert>}
      {data?.names_note && <Alert severity="info">{data.names_note}</Alert>}

      {!data && !loading && !error && (
        <Typography variant="body2" color="text.secondary" sx={{ py: 6, textAlign: "center" }}>
          Выберите период и нажмите «Показать».
        </Typography>
      )}

      {data && (
        <>
          <Box sx={{ display: "grid", gap: 1.25, gridTemplateColumns: { xs: "repeat(2, 1fr)", md: "repeat(3, 1fr)", lg: "repeat(6, 1fr)" } }}>
            <KpiCard label="Топливо всего" value={`${fmt(totals.fuel_total, 0)} л`} sub={`${periodDays} дн. · ${fmt(totals.ha, 0)} га`} />
            <KpiCard label="В поле" value={`${fmt(totals.fuel_field, 0)} л`} sub={totals.l_ha != null ? `${fmt(totals.l_ha, 1)} л/га` : "нет площади"} />
            <KpiCard label="На дороге" value={`${fmt(totals.fuel_road, 0)} л`} sub={totals.road_share != null ? `${Math.round(totals.road_share * 100)}% от расхода` : "нет разбивки"} />
            <KpiCard label="Дорога, л/ч" value={totals.road_l_h != null ? fmt(totals.road_l_h, 1) : "—"} sub={totals.road_h ? `${fmt(totals.road_h, 0)} ч в пути` : "нет времени в пути"} />
            <KpiCard
              label="Заданий с топливом"
              value={coveragePct == null ? "—" : `${coveragePct}%`}
              sub={`${fmt(cov.with_fuel, 0)} из ${fmt(cov.tasks, 0)} · ${cov.machines_no_fuel_count} машин без датчика`}
              tone={coveragePct != null && coveragePct < 60 ? "warn" : "default"}
            />
            <KpiCard
              label="Аномалий"
              value={fmt(data.anomalies_total ?? anomalies.length, 0)}
              sub={`критичных ${critical}`}
              tone={critical ? "bad" : anomalies.length ? "warn" : "good"}
            />
          </Box>

          <Tabs value={tab} onChange={(_, v) => setTab(v)} variant="scrollable" scrollButtons="auto">
            <Tab value="anomalies" label={`Аномалии (${anomalies.length})`} />
            <Tab value="machines" label="Техника" />
            <Tab value="drivers" label="Исполнители" />
            <Tab value="operations" label="Виды работ" />
            <Tab value="firms" label="Агрофирмы" />
            <Tab value="days" label="По дням" />
            <Tab value="tasks" label="Задания" />
            <Tab value="coverage" label={`Датчики (${(cov.machines_no_fuel_count || 0) + (cov.machines_partial_count || 0)})`} />
          </Tabs>

          {tab === "anomalies" && (
            <Stack spacing={1.5}>
              <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} alignItems={{ sm: "center" }} flexWrap="wrap" useFlexGap>
                <ToggleButtonGroup size="small" exclusive value={severity} onChange={(_, v) => v && setSeverity(v)}>
                  <ToggleButton value="all">Все</ToggleButton>
                  <ToggleButton value="critical">Критичные</ToggleButton>
                  <ToggleButton value="warning">Внимание</ToggleButton>
                </ToggleButtonGroup>
                <FormControl size="small" sx={{ minWidth: 260 }}>
                  <InputLabel>Тип</InputLabel>
                  <Select label="Тип" value={kind} onChange={(e) => setKind(e.target.value)} MenuProps={{ sx: { zIndex: 1600 } }}>
                    <MenuItem value="all">Все типы</MenuItem>
                    {Object.entries(KIND_LABEL).map(([k, label]) => (
                      <MenuItem key={k} value={k}>
                        {label}
                        {counts[k] ? ` · ${counts[k]}` : ""}
                      </MenuItem>
                    ))}
                  </Select>
                </FormControl>
                <Box sx={{ flex: 1 }} />
                <ExportButton onClick={exportCurrent} disabled={!shownAnomalies.length} />
              </Stack>
              {kind !== "all" && (
                <Typography variant="caption" color="text.secondary">
                  {KIND_LABEL[kind]}: {KIND_HINT[kind]}.
                </Typography>
              )}
              {data.anomalies_total > anomalies.length && (
                <Alert severity="info">Показаны первые {anomalies.length} из {data.anomalies_total}, самые крупные по лишним литрам.</Alert>
              )}
              <SortableTable
                columns={anomalyColumns}
                rows={shownAnomalies}
                rowKey={(r, i) => `${r.kind}-${r.task_id}-${r.machine_id}-${i}`}
                defaultSort="excess_l"
                emptyText={anomalies.length ? "По выбранному фильтру аномалий нет" : "Аномалий не найдено. Расход укладывается в норму аналогов."}
              />
              <Typography variant="caption" color="text.secondary">
                Норма считается по медиане аналогов того же вида работ за выбранный период; выбросом считается расход выше медианы
                на {Math.round((data.thresholds?.rel_field ?? 0.35) * 100)}% и больше, но не меньше {data.thresholds?.min_excess_l ?? 10} л лишнего.
                Если аналогов меньше {data.thresholds?.min_peers ?? 8}, нормы нет и проверка молчит. Это признак для проверки, а не доказательство слива.
              </Typography>
            </Stack>
          )}

          {tab === "machines" && (
            <TableBlock cols={machineCols} rows={machineRows} sort="fuel_total" onExport={exportCurrent} rowKey={(r) => r.machine_id ?? "none"} />
          )}
          {tab === "drivers" && (
            <TableBlock cols={driverCols} rows={driverRows} sort="fuel_total" onExport={exportCurrent} rowKey={(r) => r.driver_id ?? "none"} />
          )}
          {tab === "operations" && (
            <TableBlock cols={operationCols} rows={operationRows} sort="fuel_total" onExport={exportCurrent} rowKey={(r) => r.wt ?? "none"} />
          )}
          {tab === "firms" && (
            <TableBlock cols={firmCols} rows={firmRows} sort="fuel_total" onExport={exportCurrent} rowKey={(r) => r.firm || "none"} />
          )}
          {tab === "days" && (
            <Stack spacing={1.5}>
              {dayRows.length > 0 && (
                <Box sx={{ border: 1, borderColor: "divider", borderRadius: 1.5, p: 1, bgcolor: "background.paper" }}>
                  <BarChart
                    height={300}
                    xAxis={[{ scaleType: "band", data: dayRows.map((r) => fmtDate(r.date).slice(0, 5)) }]}
                    series={[
                      { data: dayRows.map((r) => r.fuel_field), label: "В поле, л", stack: "fuel", color: "#2563eb" },
                      { data: dayRows.map((r) => r.fuel_road), label: "На дороге, л", stack: "fuel", color: "#f59e0b" },
                    ]}
                  />
                </Box>
              )}
              <TableBlock cols={dayCols} rows={dayRows} sort="date" dir="asc" onExport={exportCurrent} rowKey={(r) => r.date} />
            </Stack>
          )}
          {tab === "tasks" && (
            <Stack spacing={1.5}>
              {data.tasks_truncated && (
                <Alert severity="info">Показаны {taskRows.length} заданий с наибольшим расходом. Итоги в остальных вкладках посчитаны по всем.</Alert>
              )}
              <TableBlock cols={taskCols} rows={taskRows} sort="fuel_total" onExport={exportCurrent} rowKey={(r, i) => `${r.task_id}-${i}`} />
            </Stack>
          )}
          {tab === "coverage" && (
            <Stack spacing={2}>
              <Typography variant="body2" color="text.secondary">
                Датчики топлива и пути есть не у всей техники. Без данных техника не попадает в проверку расхода, поэтому
                её стоит смотреть отдельно: слив легче скрыть там, где датчика нет или он работает урывками.
                Заданий с путём: {fmt(cov.with_distance, 0)}, с разбивкой поле/дорога: {fmt(cov.with_road_split, 0)}, с исполнителем: {fmt(cov.with_driver, 0)} из {fmt(cov.tasks, 0)}.
              </Typography>
              <Box>
                <Typography variant="subtitle2" sx={{ mb: 0.75 }}>
                  Датчик пропадает периодами ({cov.machines_partial_count})
                </Typography>
                <SortableTable columns={partialCols} rows={cov.machines_partial || []} rowKey={(r) => r.machine_id ?? "none"} defaultSort="missing_ha" emptyText="Нет машин с пропусками" />
              </Box>
              <Box>
                <Typography variant="subtitle2" sx={{ mb: 0.75 }}>
                  Нет данных о топливе ({cov.machines_no_fuel_count})
                </Typography>
                <SortableTable columns={noFuelCols} rows={cov.machines_no_fuel || []} rowKey={(r) => r.machine_id ?? "none"} defaultSort="ha" emptyText="У всей техники есть данные о топливе" />
              </Box>
            </Stack>
          )}
        </>
      )}
    </Stack>
  );
}

function TableBlock({ cols, rows, sort, dir = "desc", onExport, rowKey }) {
  return (
    <Stack spacing={1}>
      <Stack direction="row" justifyContent="flex-end">
        <ExportButton onClick={onExport} disabled={!rows.length} />
      </Stack>
      <SortableTable columns={cols} rows={rows} rowKey={rowKey} defaultSort={sort} defaultDir={dir} />
    </Stack>
  );
}
