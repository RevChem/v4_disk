import { useCallback, useMemo, useState } from "react";
import { Alert, Box, Chip, CircularProgress, Grid, IconButton, Paper, Stack, Typography } from "@mui/material";
import CloseIcon from "@mui/icons-material/Close";
import RestartAltIcon from "@mui/icons-material/RestartAlt";
import { PieChart } from "@mui/x-charts/PieChart";
import { BarChart } from "@mui/x-charts/BarChart";
import { LineChart } from "@mui/x-charts/LineChart";

// Палитра — независима от темы (тёмная/светлая/зелёная/синяя), чтобы цвета серий
// оставались узнаваемыми при переключении темы приложения.
const PALETTE = [
  "#2563eb",
  "#16a34a",
  "#f59e0b",
  "#dc2626",
  "#7c3aed",
  "#0891b2",
  "#db2777",
  "#65a30d",
  "#ea580c",
  "#4338ca",
  "#0d9488",
  "#b91c1c",
  "#9333ea",
  "#ca8a04",
];
const REST_COLOR = "#94a3b8";
const HIDDEN_CHARTS_STORAGE_KEY = "works_stats_hidden_charts_v1";
const WEEKDAY_LABELS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];

function loadHiddenFromStorage() {
  try {
    const raw = window.localStorage.getItem(HIDDEN_CHARTS_STORAGE_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(arr) ? arr : []);
  } catch {
    return new Set();
  }
}

function saveHiddenToStorage(set) {
  try {
    window.localStorage.setItem(HIDDEN_CHARTS_STORAGE_KEY, JSON.stringify([...set]));
  } catch {
    /* localStorage недоступен — просто не сохраняем состояние между сессиями */
  }
}

function round1(v) {
  return Math.round((Number(v) || 0) * 10) / 10;
}

function fmtArea(v) {
  const n = Number(v) || 0;
  return `${n.toLocaleString("ru-RU", { maximumFractionDigits: 1 })} га`;
}

function shortLabel(s, max = 26) {
  const clean = String(s || "—").replace(/^\d+(\.\d+)*\s*/, "");
  return clean.length > max ? `${clean.slice(0, max - 1)}…` : clean;
}

function sortDesc(map) {
  return [...map.entries()].sort((a, b) => b[1] - a[1]);
}

function topNWithRest(sortedEntries, n, restLabel = "Прочее") {
  if (sortedEntries.length <= n) return sortedEntries;
  const head = sortedEntries.slice(0, n);
  const restSum = sortedEntries.slice(n).reduce((s, [, v]) => s + v, 0);
  return restSum > 0 ? [...head, [restLabel, restSum]] : head;
}

function toRanked(entries, limit) {
  const sliced = entries.slice(0, limit);
  return {
    labels: sliced.map(([k]) => k),
    values: sliced.map(([, v]) => v),
    truncatedCount: Math.max(0, entries.length - limit),
  };
}

function fmtDayLabel(dateStr) {
  const d = new Date(`${dateStr}T00:00:00`);
  if (Number.isNaN(d.getTime())) return dateStr;
  return d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" });
}

function mondayOf(dateStr) {
  const d = new Date(`${dateStr}T00:00:00`);
  const day = (d.getDay() + 6) % 7; // 0 = понедельник
  d.setDate(d.getDate() - day);
  return d;
}

function fmtWeekLabel(mondayDate) {
  const sunday = new Date(mondayDate);
  sunday.setDate(sunday.getDate() + 6);
  const f = (d) => d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" });
  return `${f(mondayDate)}–${f(sunday)}`;
}

function pieSeriesData(entries) {
  return entries.map(([label, value], i) => ({
    id: label,
    value: round1(value),
    label: shortLabel(label),
    color: label === "Прочее" ? REST_COLOR : PALETTE[i % PALETTE.length],
  }));
}

const LEGEND_RIGHT = {
  direction: "vertical",
  position: { vertical: "middle", horizontal: "end" },
  itemMarkWidth: 10,
  itemMarkHeight: 10,
  labelStyle: { fontSize: 11 },
};
const LEGEND_TOP = {
  position: { vertical: "top", horizontal: "end" },
  labelStyle: { fontSize: 10 },
};

function ChartCard({ id, title, subtitle, onHide, children }) {
  return (
    <Paper variant="outlined" sx={{ p: 1.5, height: "100%", display: "flex", flexDirection: "column" }}>
      <Stack direction="row" alignItems="flex-start" justifyContent="space-between" spacing={1}>
        <Box sx={{ minWidth: 0 }}>
          <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
            {title}
          </Typography>
          {subtitle ? (
            <Typography variant="caption" color="text.secondary">
              {subtitle}
            </Typography>
          ) : null}
        </Box>
        <IconButton size="small" onClick={() => onHide(id)} title="Скрыть этот график">
          <CloseIcon fontSize="small" />
        </IconButton>
      </Stack>
      <Box sx={{ flex: 1, mt: 0.5 }}>{children}</Box>
    </Paper>
  );
}

export default function WorksStatsCharts({ rows, loading, error }) {
  const [hidden, setHidden] = useState(() => loadHiddenFromStorage());

  const hide = useCallback((id) => {
    setHidden((prev) => {
      const next = new Set(prev);
      next.add(id);
      saveHiddenToStorage(next);
      return next;
    });
  }, []);

  const restore = useCallback((id) => {
    setHidden((prev) => {
      const next = new Set(prev);
      next.delete(id);
      saveHiddenToStorage(next);
      return next;
    });
  }, []);

  const restoreAll = useCallback(() => {
    setHidden(new Set());
    saveHiddenToStorage(new Set());
  }, []);

  // --- Базовая агрегация сырых строк по всем нужным разрезам за один проход ---
  const stats = useMemo(() => {
    const byGroup = new Map();
    const byFirm = new Map();
    const byCrop = new Map();
    const byDate = new Map();
    const byDateGroup = new Map();
    const byWeekday = new Array(7).fill(0);
    const byFirmGroup = new Map();
    const byYearGroup = new Map();
    const years = new Set();
    let total = 0;

    for (const r of rows || []) {
      const area = Number(r.area) || 0;
      if (area <= 0) continue;
      const group = String(r.group || "—");
      const firm = String(r.firm || "—");
      const crop = String(r.crop || "—");
      const dateStr = String(r.date || "");
      const year = Number(r.year) || 0;

      total += area;
      byGroup.set(group, (byGroup.get(group) || 0) + area);
      byFirm.set(firm, (byFirm.get(firm) || 0) + area);
      byCrop.set(crop, (byCrop.get(crop) || 0) + area);
      if (year) years.add(year);

      if (dateStr) {
        byDate.set(dateStr, (byDate.get(dateStr) || 0) + area);
        if (!byDateGroup.has(dateStr)) byDateGroup.set(dateStr, new Map());
        const dg = byDateGroup.get(dateStr);
        dg.set(group, (dg.get(group) || 0) + area);

        const d = new Date(`${dateStr}T00:00:00`);
        if (!Number.isNaN(d.getTime())) {
          byWeekday[(d.getDay() + 6) % 7] += area;
        }
      }

      if (!byFirmGroup.has(firm)) byFirmGroup.set(firm, new Map());
      byFirmGroup.get(firm).set(group, (byFirmGroup.get(firm).get(group) || 0) + area);

      if (year) {
        if (!byYearGroup.has(year)) byYearGroup.set(year, new Map());
        byYearGroup.get(year).set(group, (byYearGroup.get(year).get(group) || 0) + area);
      }
    }

    return { byGroup, byFirm, byCrop, byDate, byDateGroup, byWeekday, byFirmGroup, byYearGroup, years, total };
  }, [rows]);

  // --- Временная шкала: по дням, если период короткий, иначе по неделям (иначе ось не читается) ---
  const dateEntries = useMemo(
    () => [...stats.byDate.entries()].sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0)),
    [stats]
  );
  const useWeekly = dateEntries.length > 40;

  const timeBuckets = useMemo(() => {
    if (!dateEntries.length) return { keys: [], labels: [], totals: [] };
    if (!useWeekly) {
      return {
        keys: dateEntries.map(([d]) => d),
        labels: dateEntries.map(([d]) => fmtDayLabel(d)),
        totals: dateEntries.map(([, v]) => v),
      };
    }
    const weekMap = new Map();
    for (const [d, v] of dateEntries) {
      const monday = mondayOf(d);
      const key = monday.toISOString().slice(0, 10);
      if (!weekMap.has(key)) weekMap.set(key, { label: fmtWeekLabel(monday), total: 0 });
      weekMap.get(key).total += v;
    }
    const sortedKeys = [...weekMap.keys()].sort();
    return {
      keys: sortedKeys,
      labels: sortedKeys.map((k) => weekMap.get(k).label),
      totals: sortedKeys.map((k) => weekMap.get(k).total),
    };
  }, [dateEntries, useWeekly]);

  const cumulativeTotals = useMemo(() => {
    let running = 0;
    return timeBuckets.totals.map((v) => (running += v));
  }, [timeBuckets]);

  const topGroupsForStack = useMemo(() => sortDesc(stats.byGroup).slice(0, 6).map(([g]) => g), [stats]);

  const stackedByDate = useMemo(() => {
    if (!timeBuckets.keys.length) return { series: [] };
    const bucketOf = (dateStr) => (useWeekly ? mondayOf(dateStr).toISOString().slice(0, 10) : dateStr);
    const keyIndex = new Map(timeBuckets.keys.map((k, i) => [k, i]));
    const perGroup = new Map(topGroupsForStack.map((g) => [g, new Array(timeBuckets.keys.length).fill(0)]));
    const restArr = new Array(timeBuckets.keys.length).fill(0);
    for (const [dateStr, groupMap] of stats.byDateGroup.entries()) {
      const idx = keyIndex.get(bucketOf(dateStr));
      if (idx === undefined) continue;
      for (const [group, val] of groupMap.entries()) {
        if (perGroup.has(group)) perGroup.get(group)[idx] += val;
        else restArr[idx] += val;
      }
    }
    const series = [...perGroup.entries()].map(([group, data], i) => ({
      data: data.map(round1),
      label: shortLabel(group),
      stack: "total",
      color: PALETTE[i % PALETTE.length],
      valueFormatter: fmtArea,
    }));
    const restTotal = restArr.reduce((s, v) => s + v, 0);
    if (restTotal > 0) {
      series.push({ data: restArr.map(round1), label: "Прочее", stack: "total", color: REST_COLOR, valueFormatter: fmtArea });
    }
    return { series };
  }, [timeBuckets, stats, topGroupsForStack, useWeekly]);

  const matrixData = useMemo(() => {
    const topFirms = sortDesc(stats.byFirm).slice(0, 6).map(([f]) => f);
    const topGroups = sortDesc(stats.byGroup).slice(0, 5).map(([g]) => g);
    const series = topGroups.map((group, i) => ({
      label: shortLabel(group),
      color: PALETTE[i % PALETTE.length],
      data: topFirms.map((firm) => round1(stats.byFirmGroup.get(firm)?.get(group) || 0)),
      valueFormatter: fmtArea,
    }));
    return { categories: topFirms, series };
  }, [stats]);

  const seasonComparison = useMemo(() => {
    const years = [...stats.years].sort((a, b) => a - b);
    if (years.length < 2) return null;
    const topGroups = sortDesc(stats.byGroup).slice(0, 8).map(([g]) => g);
    const series = years.map((year, i) => ({
      label: String(year),
      color: PALETTE[(i + 3) % PALETTE.length],
      data: topGroups.map((g) => round1(stats.byYearGroup.get(year)?.get(g) || 0)),
      valueFormatter: fmtArea,
    }));
    return { categories: topGroups.map((g) => shortLabel(g)), years, series };
  }, [stats]);

  const groupSorted = useMemo(() => sortDesc(stats.byGroup), [stats]);
  const firmSorted = useMemo(() => sortDesc(stats.byFirm), [stats]);
  const cropSorted = useMemo(() => sortDesc(stats.byCrop), [stats]);

  const groupPie = useMemo(() => topNWithRest(groupSorted, 8), [groupSorted]);
  const firmPie = useMemo(() => topNWithRest(firmSorted, 8), [firmSorted]);
  const cropPie = useMemo(() => topNWithRest(cropSorted, 7), [cropSorted]);

  const groupRanked = useMemo(() => toRanked(groupSorted, 14), [groupSorted]);
  const firmRanked = useMemo(() => toRanked(firmSorted, 12), [firmSorted]);
  const cropRanked = useMemo(() => toRanked(cropSorted, 12), [cropSorted]);

  const charts = useMemo(() => {
    const list = [];

    if (groupPie.length) {
      list.push({
        id: "group-pie",
        title: "Доля площади по видам работ",
        render: () => (
          <PieChart
            height={300}
            series={[
              {
                data: pieSeriesData(groupPie),
                innerRadius: 40,
                paddingAngle: 1,
                cornerRadius: 3,
                highlightScope: { faded: "global", highlighted: "item" },
                valueFormatter: (item) => fmtArea(item.value),
              },
            ]}
            slotProps={{ legend: LEGEND_RIGHT }}
          />
        ),
      });
    }

    if (firmPie.length) {
      list.push({
        id: "firm-pie",
        title: "Доля площади по агрофирмам",
        render: () => (
          <PieChart
            height={300}
            series={[
              {
                data: pieSeriesData(firmPie),
                innerRadius: 40,
                paddingAngle: 1,
                cornerRadius: 3,
                highlightScope: { faded: "global", highlighted: "item" },
                valueFormatter: (item) => fmtArea(item.value),
              },
            ]}
            slotProps={{ legend: LEGEND_RIGHT }}
          />
        ),
      });
    }

    if (cropPie.length) {
      list.push({
        id: "crop-pie",
        title: "Доля площади по культурам",
        subtitle: cropSorted.length > 7 ? `Топ-7 из ${cropSorted.length}, остальное — «Прочее»` : undefined,
        render: () => (
          <PieChart
            height={300}
            series={[
              {
                data: pieSeriesData(cropPie),
                innerRadius: 40,
                paddingAngle: 1,
                cornerRadius: 3,
                highlightScope: { faded: "global", highlighted: "item" },
                valueFormatter: (item) => fmtArea(item.value),
              },
            ]}
            slotProps={{ legend: LEGEND_RIGHT }}
          />
        ),
      });
    }

    if (firmRanked.labels.length) {
      list.push({
        id: "firm-rank",
        title: "Рейтинг агрофирм по площади",
        subtitle: firmRanked.truncatedCount ? `Показаны первые ${firmRanked.labels.length}` : undefined,
        render: () => (
          <BarChart
            height={Math.max(260, firmRanked.labels.length * 30 + 40)}
            layout="horizontal"
            yAxis={[{ scaleType: "band", data: firmRanked.labels.map((l) => shortLabel(l, 22)), tickLabelStyle: { fontSize: 11 } }]}
            series={[{ data: firmRanked.values.map(round1), label: "Площадь, га", color: PALETTE[0], valueFormatter: fmtArea }]}
            margin={{ left: 150 }}
          />
        ),
      });
    }

    if (groupRanked.labels.length) {
      list.push({
        id: "group-rank",
        title: "Рейтинг видов работ по площади",
        subtitle: groupRanked.truncatedCount ? `Показаны первые ${groupRanked.labels.length}` : undefined,
        render: () => (
          <BarChart
            height={Math.max(260, groupRanked.labels.length * 30 + 40)}
            layout="horizontal"
            yAxis={[{ scaleType: "band", data: groupRanked.labels.map((l) => shortLabel(l, 26)), tickLabelStyle: { fontSize: 11 } }]}
            series={[{ data: groupRanked.values.map(round1), label: "Площадь, га", color: PALETTE[1], valueFormatter: fmtArea }]}
            margin={{ left: 180 }}
          />
        ),
      });
    }

    if (cropRanked.labels.length) {
      list.push({
        id: "crop-rank",
        title: "Рейтинг культур по площади",
        subtitle: cropRanked.truncatedCount ? `Показаны первые ${cropRanked.labels.length}` : undefined,
        render: () => (
          <BarChart
            height={Math.max(260, cropRanked.labels.length * 30 + 40)}
            layout="horizontal"
            yAxis={[{ scaleType: "band", data: cropRanked.labels.map((l) => shortLabel(l, 22)), tickLabelStyle: { fontSize: 11 } }]}
            series={[{ data: cropRanked.values.map(round1), label: "Площадь, га", color: PALETTE[2], valueFormatter: fmtArea }]}
            margin={{ left: 150 }}
          />
        ),
      });
    }

    if (timeBuckets.labels.length) {
      list.push({
        id: "trend-line",
        title: "Динамика площади по датам",
        subtitle: useWeekly ? "Группировка по неделям (диапазон слишком широкий для дней)" : "По дням",
        render: () => (
          <LineChart
            height={300}
            xAxis={[{ scaleType: "point", data: timeBuckets.labels, tickLabelStyle: { fontSize: 10 } }]}
            series={[
              {
                data: timeBuckets.totals.map(round1),
                label: "Площадь, га",
                color: PALETTE[3],
                area: true,
                showMark: timeBuckets.labels.length <= 30,
                valueFormatter: fmtArea,
              },
            ]}
          />
        ),
      });

      list.push({
        id: "cumulative-line",
        title: "Площадь нарастающим итогом",
        subtitle: useWeekly ? "По неделям" : "По дням",
        render: () => (
          <LineChart
            height={300}
            xAxis={[{ scaleType: "point", data: timeBuckets.labels, tickLabelStyle: { fontSize: 10 } }]}
            series={[
              {
                data: cumulativeTotals.map(round1),
                label: "Нарастающим итогом, га",
                color: PALETTE[4],
                showMark: false,
                valueFormatter: fmtArea,
              },
            ]}
          />
        ),
      });
    }

    if (stackedByDate.series.length) {
      list.push({
        id: "stacked-date-group",
        title: "Виды работ по датам (стек)",
        subtitle: useWeekly ? "Топ-6 видов работ, по неделям" : "Топ-6 видов работ, по дням",
        render: () => (
          <BarChart
            height={320}
            xAxis={[
              {
                scaleType: "band",
                data: timeBuckets.labels,
                tickLabelStyle: { fontSize: 10, angle: timeBuckets.labels.length > 20 ? -45 : 0 },
              },
            ]}
            series={stackedByDate.series}
            slotProps={{ legend: LEGEND_TOP }}
          />
        ),
      });
    }

    if (matrixData.categories.length && matrixData.series.length) {
      list.push({
        id: "matrix-firm-group",
        title: "Виды работ по агрофирмам",
        subtitle: "Топ-6 агрофирм × топ-5 видов работ",
        render: () => (
          <BarChart
            height={320}
            xAxis={[{ scaleType: "band", data: matrixData.categories.map((f) => shortLabel(f, 16)), tickLabelStyle: { fontSize: 10 } }]}
            series={matrixData.series}
            slotProps={{ legend: LEGEND_TOP }}
          />
        ),
      });
    }

    if (seasonComparison) {
      list.push({
        id: "season-comparison",
        title: "Сравнение по сезонам",
        subtitle: `${seasonComparison.years.join(" vs ")} · топ-8 видов работ`,
        render: () => (
          <BarChart
            height={320}
            xAxis={[{ scaleType: "band", data: seasonComparison.categories, tickLabelStyle: { fontSize: 10, angle: -20 } }]}
            series={seasonComparison.series}
            slotProps={{ legend: LEGEND_TOP }}
          />
        ),
      });
    }

    if (stats.byWeekday.some((v) => v > 0)) {
      list.push({
        id: "weekday-bar",
        title: "Площадь по дням недели",
        render: () => (
          <BarChart
            height={280}
            xAxis={[{ scaleType: "band", data: WEEKDAY_LABELS }]}
            series={[{ data: stats.byWeekday.map(round1), label: "Площадь, га", color: PALETTE[5], valueFormatter: fmtArea }]}
          />
        ),
      });
    }

    return list;
  }, [
    groupPie,
    firmPie,
    cropPie,
    cropSorted.length,
    firmRanked,
    groupRanked,
    cropRanked,
    timeBuckets,
    useWeekly,
    cumulativeTotals,
    stackedByDate,
    matrixData,
    seasonComparison,
    stats.byWeekday,
  ]);

  const visibleCharts = useMemo(() => charts.filter((c) => !hidden.has(c.id)), [charts, hidden]);
  const hiddenCharts = useMemo(() => charts.filter((c) => hidden.has(c.id)), [charts, hidden]);

  if (loading) {
    return (
      <Stack direction="row" spacing={1} alignItems="center" sx={{ py: 3 }}>
        <CircularProgress size={16} />
        <Typography variant="body2" color="text.secondary">
          Загрузка данных для графиков...
        </Typography>
      </Stack>
    );
  }

  if (error) {
    return <Alert severity="warning">{error}</Alert>;
  }

  if (!rows?.length) {
    return (
      <Typography variant="body2" color="text.secondary" sx={{ py: 2 }}>
        Для выбранных фильтров нет данных.
      </Typography>
    );
  }

  return (
    <Stack spacing={1.5}>
      <Typography variant="body2" color="text.secondary">
        Строк: {rows.length.toLocaleString("ru-RU")} · Суммарная площадь: {fmtArea(stats.total)}
      </Typography>

      {hiddenCharts.length ? (
        <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
          <Typography variant="caption" color="text.secondary">
            Скрыто ({hiddenCharts.length}):
          </Typography>
          {hiddenCharts.map((c) => (
            <Chip
              key={c.id}
              size="small"
              variant="outlined"
              label={c.title}
              onClick={() => restore(c.id)}
              onDelete={() => restore(c.id)}
              deleteIcon={<RestartAltIcon fontSize="small" />}
            />
          ))}
          <Chip size="small" color="primary" variant="outlined" label="Показать всё" onClick={restoreAll} />
        </Stack>
      ) : null}

      <Grid container spacing={1.5}>
        {visibleCharts.map((c) => (
          <Grid key={c.id} size={{ xs: 12, md: 6 }}>
            <ChartCard id={c.id} title={c.title} subtitle={c.subtitle} onHide={hide}>
              {c.render()}
            </ChartCard>
          </Grid>
        ))}
      </Grid>
    </Stack>
  );
}
