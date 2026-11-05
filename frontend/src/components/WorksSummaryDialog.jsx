import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Autocomplete,
  Box,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogContent,
  DialogTitle,
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  Tab,
  Tabs,
  TextField,
  Typography,
} from "@mui/material";

import {
  downloadWorksSummaryExcel,
  fetchAllWorksSummaryRows,
  fetchWorksSummaryGrid,
  fetchWorksSummaryState,
  generateWorksSummary,
} from "../api";
import WorksStatsCharts from "./WorksStatsCharts";

function formatApiError(err, fallback) {
  const d = err?.response?.data?.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((x) => x.msg || JSON.stringify(x)).join("; ");
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

function formatWorksSheetLabel(s) {
  if (!s) return "";
  const fromApi = String(s.display_title || "").trim();
  if (fromApi) return fromApi;
  const full = `${String(s.group || "").trim()} ${String(s.crop || "").trim()}`.trim();
  return full || String(s.sheet_title || "").trim();
}

export default function WorksSummaryDialog({ open, onClose, onError, onInfo }) {
  const [worksState, setWorksState] = useState({ exists: false, groups: [], crops: [], firms: [] });
  const [worksGrid, setWorksGrid] = useState(null);
  const [sheetIdx, setSheetIdx] = useState(0);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [selectedYears, setSelectedYears] = useState([]);
  const [selectedGroups, setSelectedGroups] = useState([]);
  const [selectedCrops, setSelectedCrops] = useState([]);
  const [selectedFirms, setSelectedFirms] = useState([]);
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [tableError, setTableError] = useState("");
  const [excelDownloading, setExcelDownloading] = useState(false);
  const [viewTab, setViewTab] = useState("table"); // "table" | "charts"
  const [chartRows, setChartRows] = useState([]);
  const [chartsLoading, setChartsLoading] = useState(false);
  const [chartsError, setChartsError] = useState("");

  const prevRunningRef = useRef(false);
  const tableReqId = useRef(0);
  const chartsReqId = useRef(0);

  const groupOptions = useMemo(() => {
    const fromState = Array.isArray(worksState?.groups) ? worksState.groups : [];
    const fromGrid = (worksGrid?.sheets || []).map((s) => s.group).filter(Boolean);
    return [...new Set([...fromState, ...fromGrid])].sort((a, b) =>
      String(a).localeCompare(String(b), "ru")
    );
  }, [worksState?.groups, worksGrid]);

  const cropOptions = useMemo(() => {
    const fromState = Array.isArray(worksState?.crops) ? worksState.crops : [];
    const fromGrid = (worksGrid?.sheets || []).map((s) => s.crop).filter(Boolean);
    return [...new Set([...fromState, ...fromGrid])].sort((a, b) =>
      String(a).localeCompare(String(b), "ru")
    );
  }, [worksState?.crops, worksGrid]);

  const firmOptions = useMemo(() => {
    const fromState = Array.isArray(worksState?.firms) ? worksState.firms : [];
    const fromGrid = [];
    for (const s of worksGrid?.sheets || []) {
      for (const b of s.blocks || []) {
        for (const fr of b.firms || []) {
          if (fr?.firm) fromGrid.push(fr.firm);
        }
      }
    }
    return [...new Set([...fromState, ...fromGrid])].sort((a, b) =>
      String(a).localeCompare(String(b), "ru")
    );
  }, [worksState?.firms, worksGrid]);

  const groupOptionsMerged = useMemo(
    () =>
      [...new Set([...groupOptions, ...selectedGroups])].sort((a, b) =>
        String(a).localeCompare(String(b), "ru")
      ),
    [groupOptions, selectedGroups]
  );

  const cropOptionsMerged = useMemo(
    () =>
      [...new Set([...cropOptions, ...selectedCrops])].sort((a, b) =>
        String(a).localeCompare(String(b), "ru")
      ),
    [cropOptions, selectedCrops]
  );

  const firmOptionsMerged = useMemo(
    () =>
      [...new Set([...firmOptions, ...selectedFirms])].sort((a, b) =>
        String(a).localeCompare(String(b), "ru")
      ),
    [firmOptions, selectedFirms]
  );

  const yearOptionsMerged = useMemo(() => {
    const fromState = [worksState?.season_year, worksState?.prev_year]
      .map((x) => Number(x))
      .filter((x) => Number.isFinite(x) && x > 0);
    const merged = [
      ...new Set([
        ...fromState,
        ...selectedYears.map((x) => Number(x)).filter((x) => Number.isFinite(x) && x > 0),
      ]),
    ];
    return merged.sort((a, b) => a - b);
  }, [worksState?.season_year, worksState?.prev_year, selectedYears]);

  const activeSheetIndex = useMemo(() => {
    const n = worksGrid?.sheets?.length || 0;
    if (!n) return 0;
    return Math.min(sheetIdx, n - 1);
  }, [worksGrid, sheetIdx]);

  const activeSheet = useMemo(() => {
    const sheets = worksGrid?.sheets;
    if (!sheets?.length) return null;
    return sheets[activeSheetIndex] || null;
  }, [worksGrid, activeSheetIndex]);

  const filterParams = useCallback(
    (extra = {}) => ({
      years: selectedYears,
      groups: selectedGroups,
      crops: selectedCrops,
      firms: selectedFirms,
      date_from: dateFrom || undefined,
      date_to: dateTo || undefined,
      ...extra,
    }),
    [selectedYears, selectedGroups, selectedCrops, selectedFirms, dateFrom, dateTo]
  );

  const loadState = useCallback(async () => {
    try {
      const data = await fetchWorksSummaryState();
      setWorksState(data || { exists: false, groups: [], crops: [], firms: [] });
      if (data?.error) {
        onError?.(String(data.error));
      }
      const y1 = Number(data?.season_year || 0);
      const y2 = Number(data?.prev_year || 0);
      const years = [y1, y2].filter((x) => Number.isFinite(x) && x > 0);
      setSelectedYears((prev) => (prev.length ? prev : years));
    } catch {
      setWorksState({ exists: false, groups: [], crops: [], firms: [] });
    }
  }, [onError]);

  const loadGrid = useCallback(async () => {
    tableReqId.current += 1;
    const reqId = tableReqId.current;
    setLoading(true);
    try {
      const data = await fetchWorksSummaryGrid(filterParams());
      if (reqId !== tableReqId.current) return;
      setTableError("");
      setWorksGrid(data || null);
      setSheetIdx(0);
      void loadState();
    } catch (e) {
      if (reqId !== tableReqId.current) return;
      setWorksGrid(null);
      setTableError(formatApiError(e, "Не удалось загрузить таблицу сводки по работам."));
    } finally {
      if (reqId === tableReqId.current) setLoading(false);
    }
  }, [filterParams, loadState]);

  const loadCharts = useCallback(async () => {
    chartsReqId.current += 1;
    const reqId = chartsReqId.current;
    setChartsLoading(true);
    setChartsError("");
    try {
      const rows = await fetchAllWorksSummaryRows(filterParams());
      if (reqId !== chartsReqId.current) return;
      setChartRows(rows);
      void loadState();
    } catch (e) {
      if (reqId !== chartsReqId.current) return;
      setChartRows([]);
      setChartsError(formatApiError(e, "Не удалось загрузить данные для графиков."));
    } finally {
      if (reqId === chartsReqId.current) setChartsLoading(false);
    }
  }, [filterParams, loadState]);

  const handleGenerate = useCallback(async () => {
    setGenerating(true);
    try {
      const next = await generateWorksSummary();
      setWorksState(next || { exists: false, groups: [], crops: [], firms: [] });
      if (next?.queue_job_id) {
        onInfo?.(
          `Сводка по работам поставлена в очередь (задача ${next.queue_job_id}). Откройте «Очередь генерации».`
        );
      } else if (next?.running) {
        onInfo?.("Сводка по работам запущена, ожидайте обновления...");
      } else if (next?.exists) {
        onInfo?.("Сводка по работам сформирована.");
      } else if (next?.error) {
        onError?.(String(next.error));
      } else {
        onInfo?.("Запрос на сбор сводки принят.");
      }
    } catch (e) {
      onError?.(formatApiError(e, "Не удалось сформировать сводку по работам."));
    } finally {
      setGenerating(false);
    }
  }, [onError, onInfo]);

  const handleDownloadExcel = useCallback(async () => {
    setTableError("");
    setExcelDownloading(true);
    try {
      await downloadWorksSummaryExcel(filterParams());
    } catch (e) {
      setTableError(formatApiError(e, "Не удалось скачать Excel."));
    } finally {
      setExcelDownloading(false);
    }
  }, [filterParams]);

  const resetFilters = useCallback(() => {
    const y1 = Number(worksState?.season_year || 0);
    const y2 = Number(worksState?.prev_year || 0);
    const years = [y1, y2].filter((x) => Number.isFinite(x) && x > 0);
    setSelectedYears(years);
    setSelectedGroups([]);
    setSelectedCrops([]);
    setSelectedFirms([]);
    setDateFrom("");
    setDateTo("");
  }, [worksState?.season_year, worksState?.prev_year]);

  useEffect(() => {
    if (!open) return;
    loadState();
  }, [open, loadState]);

  useEffect(() => {
    if (!open || viewTab !== "table") return;
    loadGrid();
  }, [
    open,
    viewTab,
    loadGrid,
    selectedYears.join(","),
    selectedGroups.join(","),
    selectedCrops.join(","),
    selectedFirms.join(","),
    dateFrom,
    dateTo,
  ]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!open || viewTab !== "charts") return;
    loadCharts();
  }, [
    open,
    viewTab,
    loadCharts,
    selectedYears.join(","),
    selectedGroups.join(","),
    selectedCrops.join(","),
    selectedFirms.join(","),
    dateFrom,
    dateTo,
  ]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!open || !worksState?.running) return undefined;
    const id = window.setInterval(() => {
      loadState();
    }, 3000);
    return () => window.clearInterval(id);
  }, [open, worksState?.running, loadState]);

  useEffect(() => {
    const wasRunning = Boolean(prevRunningRef.current);
    const isRunning = Boolean(worksState?.running);
    const finishedNow = wasRunning && !isRunning;
    prevRunningRef.current = isRunning;
    if (!finishedNow || !open) return;
    if (viewTab === "charts") loadCharts();
    else loadGrid();
  }, [worksState?.running, open, viewTab, loadGrid, loadCharts]);

  const dialogTitle = useMemo(() => {
    const y1 = worksState?.prev_year;
    const y2 = worksState?.season_year;
    if (y1 && y2) return `Сводка по работам ${y1}/${y2}`;
    return "Сводка по работам";
  }, [worksState?.prev_year, worksState?.season_year]);

  return (
    <Dialog
      open={open}
      onClose={() => {
        onClose?.();
        setTableError("");
      }}
      fullWidth
      maxWidth="lg"
    >
      <DialogTitle>{dialogTitle}</DialogTitle>
      <DialogContent dividers>
        <Stack spacing={1.5}>
          {tableError ? (
            <Alert severity="warning" onClose={() => setTableError("")}>
              {tableError}
            </Alert>
          ) : null}
          {worksState?.warnings?.length ? (
            <Alert severity="warning">{worksState.warnings.join(" ")}</Alert>
          ) : null}
          <Stack direction="row" spacing={1} alignItems="center" justifyContent="space-between">
            <Typography variant="body2" color="text.secondary">
              {worksState?.running ? "Формирование сводки… · " : ""}
              {worksState?.exists
                ? `Период: ${worksState?.prev_year || "—"} / ${worksState?.season_year || "—"} · Создано: ${worksState?.created_at || "—"}`
                : "Сводка ещё не создана"}
            </Typography>
            <Stack direction="row" spacing={1}>
              <Button size="small" variant="contained" onClick={handleGenerate} disabled={generating}>
                {generating ? "Запуск..." : worksState?.exists ? "Обновить" : "Создать"}
              </Button>
              <Button
                size="small"
                variant="outlined"
                disabled={generating || excelDownloading || worksState?.running}
                onClick={() => void handleDownloadExcel()}
              >
                {excelDownloading ? "Excel…" : "Excel"}
              </Button>
            </Stack>
          </Stack>
          <Stack direction={{ xs: "column", md: "row" }} spacing={1}>
            <Autocomplete
              size="small"
              multiple
              options={yearOptionsMerged}
              value={selectedYears}
              onChange={(_, v) =>
                setSelectedYears(
                  (v || []).map((x) => Number(x)).filter((x) => Number.isFinite(x) && x > 0)
                )
              }
              isOptionEqualToValue={(a, b) => Number(a) === Number(b)}
              slotProps={{ popper: { sx: { zIndex: 1600 } } }}
              renderInput={(params) => <TextField {...params} label="Годы" />}
              sx={{ minWidth: 180 }}
            />
            <Autocomplete
              size="small"
              multiple
              freeSolo
              forcePopupIcon
              options={groupOptionsMerged}
              value={selectedGroups}
              onChange={(_, v) =>
                setSelectedGroups(
                  (v || []).map((x) => String(x || "").trim()).filter(Boolean)
                )
              }
              isOptionEqualToValue={(a, b) => String(a) === String(b)}
              filterSelectedOptions={false}
              slotProps={{ popper: { sx: { zIndex: 1600 } } }}
              noOptionsText="Нет данных. Можно ввести вручную."
              renderTags={(value, getTagProps) =>
                value.map((option, index) => (
                  <Chip
                    size="small"
                    label={option}
                    {...getTagProps({ index })}
                    key={`${option}-${index}`}
                  />
                ))
              }
              renderInput={(params) => <TextField {...params} label="Виды работ" />}
              sx={{ minWidth: 240 }}
            />
            <Autocomplete
              size="small"
              multiple
              freeSolo
              forcePopupIcon
              options={cropOptionsMerged}
              value={selectedCrops}
              onChange={(_, v) =>
                setSelectedCrops(
                  (v || []).map((x) => String(x || "").trim()).filter(Boolean)
                )
              }
              isOptionEqualToValue={(a, b) => String(a) === String(b)}
              filterSelectedOptions={false}
              slotProps={{ popper: { sx: { zIndex: 1600 } } }}
              noOptionsText="Нет данных. Можно ввести вручную."
              renderTags={(value, getTagProps) =>
                value.map((option, index) => (
                  <Chip
                    size="small"
                    label={option}
                    {...getTagProps({ index })}
                    key={`${option}-${index}`}
                  />
                ))
              }
              renderInput={(params) => <TextField {...params} label="Культуры" />}
              sx={{ minWidth: 220 }}
            />
            <Autocomplete
              size="small"
              multiple
              freeSolo
              forcePopupIcon
              options={firmOptionsMerged}
              value={selectedFirms}
              onChange={(_, v) =>
                setSelectedFirms(
                  (v || []).map((x) => String(x || "").trim()).filter(Boolean)
                )
              }
              isOptionEqualToValue={(a, b) => String(a) === String(b)}
              filterSelectedOptions={false}
              slotProps={{ popper: { sx: { zIndex: 1600 } } }}
              noOptionsText="Нет данных. Можно ввести вручную."
              renderTags={(value, getTagProps) =>
                value.map((option, index) => (
                  <Chip
                    size="small"
                    label={option}
                    {...getTagProps({ index })}
                    key={`${option}-${index}`}
                  />
                ))
              }
              renderInput={(params) => <TextField {...params} label="Исполнители" />}
              sx={{ minWidth: 260 }}
            />
          </Stack>
          <Stack direction={{ xs: "column", sm: "row" }} spacing={1}>
            <TextField
              size="small"
              type="date"
              label="Дата с"
              InputLabelProps={{ shrink: true }}
              value={dateFrom}
              onChange={(e) => setDateFrom(e.target.value)}
            />
            <TextField
              size="small"
              type="date"
              label="Дата по"
              InputLabelProps={{ shrink: true }}
              value={dateTo}
              onChange={(e) => setDateTo(e.target.value)}
            />
            <Button size="small" variant="text" onClick={resetFilters}>
              Сбросить фильтры
            </Button>
          </Stack>
          <Tabs
            value={viewTab}
            onChange={(_, v) => setViewTab(v)}
            sx={{ minHeight: 36, "& .MuiTab-root": { minHeight: 36, py: 0.5 } }}
          >
            <Tab value="table" label="Таблица" />
            <Tab value="charts" label="Графики" />
          </Tabs>
          {viewTab === "charts" ? (
            <WorksStatsCharts rows={chartRows} loading={chartsLoading} error={chartsError} />
          ) : loading ? (
            <Stack direction="row" spacing={1} alignItems="center">
              <CircularProgress size={16} />
              <Typography variant="body2" color="text.secondary">
                Загрузка таблицы...
              </Typography>
            </Stack>
          ) : activeSheet ? (
            <Box>
              <Stack spacing={1} sx={{ mb: 1 }}>
                <Typography variant="caption" color="text.secondary">
                  Листов (как в Excel): {worksGrid?.sheet_count ?? 0}
                  {worksGrid?.effective_date_from && worksGrid?.effective_date_to
                    ? ` · Дни по колонкам: ${worksGrid.effective_date_from} — ${worksGrid.effective_date_to}`
                    : ""}
                </Typography>
                {(worksGrid?.sheets || []).length > 0 ? (
                  <FormControl size="small" fullWidth className="works-sheet-select" sx={{ width: "100%" }}>
                    <InputLabel id="works-sheet-select-label">Лист</InputLabel>
                    <Select
                      labelId="works-sheet-select-label"
                      label="Лист"
                      value={activeSheetIndex}
                      onChange={(e) => setSheetIdx(Number(e.target.value))}
                      renderValue={(idx) => {
                        const s = (worksGrid.sheets || [])[Number(idx)];
                        const text = formatWorksSheetLabel(s);
                        return (
                          <Box
                            component="span"
                            title={text}
                            sx={{
                              display: "block",
                              width: "100%",
                              whiteSpace: "normal",
                              wordBreak: "break-word",
                              overflow: "visible",
                              textOverflow: "clip",
                              lineHeight: 1.35,
                              py: 0.25,
                              pr: 3,
                            }}
                          >
                            {text}
                          </Box>
                        );
                      }}
                      MenuProps={{
                        PaperProps: {
                          sx: {
                            maxHeight: 360,
                            maxWidth: 720,
                            zIndex: 1700,
                          },
                        },
                      }}
                    >
                      {(worksGrid.sheets || []).map((s, idx) => (
                        <MenuItem
                          key={`works-sheet-${idx}`}
                          value={idx}
                          sx={{
                            whiteSpace: "normal",
                            wordBreak: "break-word",
                            lineHeight: 1.35,
                            alignItems: "flex-start",
                          }}
                        >
                          {formatWorksSheetLabel(s)}
                        </MenuItem>
                      ))}
                    </Select>
                  </FormControl>
                ) : null}
              </Stack>
              <Box
                sx={{
                  maxHeight: "60vh",
                  overflow: "auto",
                  border: 1,
                  borderColor: "divider",
                  borderRadius: 1,
                }}
              >
                {(activeSheet.blocks || []).map((block, bi) => (
                  <Box key={`blk-${block.header_year}-${bi}`}>
                    {bi > 0 ? <Box sx={{ height: 8, bgcolor: "#F2F2F2" }} /> : null}
                    <Box
                      sx={{
                        px: 1,
                        py: 0.75,
                        bgcolor: block.is_current_season ? "#FFFF00" : "#E2EFDA",
                        borderBottom: "1px solid",
                        borderColor: "divider",
                      }}
                    >
                      <Typography variant="subtitle2" sx={{ fontWeight: 700, color: "#111827" }}>
                        {block.title}
                      </Typography>
                    </Box>
                    <Box sx={{ overflowX: "auto" }}>
                      <Box
                        component="table"
                        sx={{
                          width: "100%",
                          borderCollapse: "collapse",
                          fontSize: 11,
                          minWidth: 560,
                        }}
                      >
                        <Box component="thead">
                          <Box component="tr">
                            {["Агрофирма", "Культура", "Вид работ"].map((label) => (
                              <Box
                                component="th"
                                key={label}
                                sx={{
                                  textAlign: "center",
                                  p: 0.5,
                                  borderBottom: "1px solid",
                                  borderColor: "divider",
                                  bgcolor: "#FF0000",
                                  color: "#fff",
                                  fontWeight: 700,
                                }}
                              >
                                {label}
                              </Box>
                            ))}
                            {(block.date_headers || []).map((dh) => (
                              <Box
                                component="th"
                                key={dh.date}
                                sx={{
                                  textAlign: "center",
                                  p: 0.5,
                                  borderBottom: "1px solid",
                                  borderColor: "divider",
                                  bgcolor: "#FF0000",
                                  color: "#fff",
                                  fontWeight: 700,
                                  whiteSpace: "nowrap",
                                }}
                              >
                                {dh.label}
                              </Box>
                            ))}
                            <Box
                              component="th"
                              sx={{
                                textAlign: "center",
                                p: 0.5,
                                borderBottom: "1px solid",
                                borderColor: "divider",
                                bgcolor: "#FF0000",
                                color: "#fff",
                                fontWeight: 700,
                              }}
                            >
                              ИТОГО
                            </Box>
                          </Box>
                        </Box>
                        <Box component="tbody">
                          {(block.firms || []).map((fr, fi) => (
                            <Box component="tr" key={`${fr.firm}-${fi}`}>
                              <Box
                                component="td"
                                sx={{ p: 0.5, borderBottom: "1px solid", borderColor: "divider" }}
                              >
                                {fr.firm}
                              </Box>
                              <Box
                                component="td"
                                sx={{ p: 0.5, borderBottom: "1px solid", borderColor: "divider" }}
                              >
                                {activeSheet.crop}
                              </Box>
                              <Box
                                component="td"
                                sx={{ p: 0.5, borderBottom: "1px solid", borderColor: "divider" }}
                              >
                                {activeSheet.group}
                              </Box>
                              {(fr.daily || []).map((v, vi) => (
                                <Box
                                  component="td"
                                  key={`d-${vi}`}
                                  sx={{
                                    p: 0.5,
                                    borderBottom: "1px solid",
                                    borderColor: "divider",
                                    textAlign: "center",
                                  }}
                                >
                                  {Number(v).toFixed(1)}
                                </Box>
                              ))}
                              <Box
                                component="td"
                                sx={{
                                  p: 0.5,
                                  borderBottom: "1px solid",
                                  borderColor: "divider",
                                  textAlign: "center",
                                  fontWeight: 600,
                                  bgcolor: "#BDD7EE",
                                }}
                              >
                                {Number(fr.total ?? 0).toFixed(1)}
                              </Box>
                            </Box>
                          ))}
                          <Box component="tr">
                            <Box
                              component="td"
                              colSpan={3 + (block.date_headers || []).length}
                              sx={{
                                p: 0.5,
                                borderBottom: "1px solid",
                                borderColor: "divider",
                                bgcolor: "#D9E1F2",
                                textAlign: "right",
                                fontWeight: 700,
                              }}
                            >
                              СУММА
                            </Box>
                            <Box
                              component="td"
                              sx={{
                                p: 0.5,
                                borderBottom: "1px solid",
                                borderColor: "divider",
                                textAlign: "center",
                                fontWeight: 700,
                                bgcolor: "#D9E1F2",
                              }}
                            >
                              {Number(block.grand_total ?? 0).toFixed(1)}
                            </Box>
                          </Box>
                        </Box>
                      </Box>
                    </Box>
                  </Box>
                ))}
              </Box>
            </Box>
          ) : (
            <Typography variant="body2" color="text.secondary">
              {worksState?.exists
                ? "Для выбранных фильтров нет данных."
                : "Создайте сводку, чтобы увидеть данные."}
            </Typography>
          )}
        </Stack>
      </DialogContent>
    </Dialog>
  );
}
