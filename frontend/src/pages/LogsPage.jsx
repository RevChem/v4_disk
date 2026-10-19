import { Fragment, createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  FormControl,
  IconButton,
  InputAdornment,
  InputLabel,
  Link,
  MenuItem,
  Pagination,
  Select,
  Stack,
  Tab,
  Tabs,
  TextField,
  Typography,
} from "@mui/material";
import ClearRoundedIcon from "@mui/icons-material/ClearRounded";
import SearchRoundedIcon from "@mui/icons-material/SearchRounded";
import BugReportRoundedIcon from "@mui/icons-material/BugReportRounded";
import RefreshRoundedIcon from "@mui/icons-material/RefreshRounded";

import { fetchActivityFeed, fetchActivityJob } from "../api";
import { TaskText, useTaskLinks } from "../components/TaskText";
import { formatDuration, reportLabel } from "../utils/formatDuration";

function formatWhen(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function displayMessage(ev) {
  const user = ev.username || "";
  let msg = ev.message || "";
  if (user && msg.startsWith(`${user} `)) {
    msg = msg.slice(user.length + 1);
  }
  // строка уже выводится как «время · пользователь — текст», второе тире не нужно
  return msg.replace(/^—\s+/, "");
}

const SearchTermsContext = createContext([]);

function escapeRegExp(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Подсвечивает слова поискового запроса (без учёта регистра, «ё» = «е»). */
function Highlight({ text }) {
  const terms = useContext(SearchTermsContext);
  const value = String(text ?? "");
  if (!terms.length || !value) return value;
  // «ё»→«е» не меняет длину строки, поэтому индексы нормализованной копии совпадают с исходной
  const folded = value.toLowerCase().replace(/ё/g, "е");
  const re = new RegExp(terms.map((t) => escapeRegExp(t.replace(/ё/g, "е"))).join("|"), "g");
  const parts = [];
  let last = 0;
  for (const m of folded.matchAll(re)) {
    if (!m[0]) continue;
    if (m.index > last) parts.push(value.slice(last, m.index));
    parts.push(
      <mark key={m.index} className="log-search-mark">
        {value.slice(m.index, m.index + m[0].length)}
      </mark>
    );
    last = m.index + m[0].length;
  }
  if (!parts.length) return value;
  if (last < value.length) parts.push(value.slice(last));
  return <>{parts}</>;
}

function LogEventMeta({ when, username, extra }) {
  return (
    <Box component="span" className="log-event-meta">
      {when}
      {username ? <>{" · "}<Highlight text={username} /></> : ""}
      {extra ? <>{" · "}<Highlight text={extra} /></> : ""}
    </Box>
  );
}

function ReportNumbersInline({ reports, durationSeconds, onErrorClick }) {
  const duration = formatDuration(durationSeconds);
  if (!reports?.length && !duration) return null;
  return (
    <Typography component="span" variant="body2" className="log-event-text">
      {" · "}
      {reports.map((r, i) => {
        const isErr = r.status === "error";
        const sep = i < reports.length - 1 ? ", " : "";
        return (
          <Fragment key={r.report_id}>
            {isErr && onErrorClick ? (
              <Link
                component="button"
                type="button"
                color="error"
                underline="always"
                title="Показать технический лог"
                onClick={() => onErrorClick(r.report_id)}
                sx={{ fontSize: "inherit", fontWeight: 700, verticalAlign: "baseline" }}
              >
                {r.report_id}
              </Link>
            ) : (
              <Box
                component="span"
                sx={isErr ? { color: "error.main", fontWeight: 700 } : undefined}
              >
                {r.report_id}
              </Box>
            )}
            {sep}
          </Fragment>
        );
      })}
      {duration ? ` (${duration})` : ""}
    </Typography>
  );
}

function LogEventLine({ when, username, message, extra, reports, durationSeconds, onErrorClick, className = "" }) {
  return (
    <Box className={`log-event-row${className ? ` ${className}` : ""}`}>
      <LogEventMeta when={when} username={username} extra={extra} />
      <Typography component="span" variant="body2" className="log-event-text">
        {message ? <>{" — "}<Highlight text={message} /></> : ""}
      </Typography>
      <ReportNumbersInline
        reports={reports}
        durationSeconds={durationSeconds}
        onErrorClick={onErrorClick}
      />
    </Box>
  );
}

function ReportDetailPanel({ detail, highlightReportId }) {
  const ref = useRef(null);
  const logLines = (detail?.reports || []).flatMap((r) => r.log_lines || []);
  const taskUrls = useTaskLinks(logLines);

  useEffect(() => {
    if (highlightReportId && ref.current) {
      ref.current.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, [highlightReportId, detail?.job_id]);

  if (!detail) {
    return (
      <Typography variant="body2" color="text.secondary">
        Выберите сводку с ошибкой или дождитесь загрузки деталей.
      </Typography>
    );
  }

  return (
    <Stack spacing={2}>
      <Box>
        <Typography variant="subtitle2" fontWeight={700}>
          Задание {detail.job_id?.slice(0, 8)}… · {detail.summary_date}
        </Typography>
        <Typography variant="caption" color="text.secondary">
          {detail.username} · статус: {detail.status}
        </Typography>
      </Box>
      {(detail.reports || []).map((r) => {
        const highlighted = highlightReportId && String(r.report_id) === String(highlightReportId);
        return (
          <Box
            key={r.report_id}
            ref={highlighted ? ref : undefined}
            className={`log-detail-block${highlighted ? " log-detail-block--highlight" : ""}`}
          >
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
              <Chip
                size="small"
                label={reportLabel(r.report_id, r.title)}
                color={r.status === "error" ? "error" : "success"}
              />
              {r.status === "error" && (
                <Typography variant="caption" color="error.main">
                  ошибка
                </Typography>
              )}
            </Stack>
            {r.error && (
              <Typography variant="body2" sx={{ whiteSpace: "pre-wrap", mb: 1 }}>
                {r.error}
              </Typography>
            )}
            {(r.log_lines || []).length > 0 && (
              <Box component="pre" className="log-lines-pre">
                <TaskText text={(r.log_lines || []).join("\n")} urls={taskUrls} />
              </Box>
            )}
          </Box>
        );
      })}
      {!detail.reports?.length && detail.error_message && (
        <Alert severity="error">{detail.error_message}</Alert>
      )}
    </Stack>
  );
}

const PAGE_SIZE = 50;

function UserEventList({ events, emptyText, onOpenReport }) {
  if (!events.length) {
    return (
      <Typography variant="body2" color="text.secondary">
        {emptyText}
      </Typography>
    );
  }
  return (
    <Stack spacing={0.75}>
      {events.map((ev) => (
        <Box key={ev.id} className="log-event-card log-event-card--compact">
          <LogEventLine
            when={formatWhen(ev.created_at)}
            username={ev.username}
            message={displayMessage(ev)}
            reports={ev.reports}
            durationSeconds={ev.duration_seconds}
            onErrorClick={ev.job_id ? (rid) => onOpenReport(ev.job_id, rid) : undefined}
          />
        </Box>
      ))}
    </Stack>
  );
}

export default function LogsPage({ focus, onFocusConsumed, canViewUserEvents = false }) {
  const [tab, setTab] = useState(canViewUserEvents ? "users" : "reports");
  const [page, setPage] = useState(1);
  const [feed, setFeed] = useState({ items: [], tabs: [], users: [], total: 0, pages: 1, page: 1 });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selectedJobId, setSelectedJobId] = useState(null);
  const [selectedReportId, setSelectedReportId] = useState(null);
  const [jobDetail, setJobDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [userFilter, setUserFilter] = useState("");
  const [searchInput, setSearchInput] = useState("");
  const [query, setQuery] = useState(""); // применённый (с задержкой) поисковый запрос
  const requestSeq = useRef(0);
  const jumpToHits = useRef(false); // после смены запроса перейти на первую вкладку с совпадениями

  const loadFeed = useCallback(
    async ({ refresh = false } = {}) => {
      const seq = ++requestSeq.current;
      setLoading(true);
      setError("");
      try {
        const data = await fetchActivityFeed({
          tab,
          page,
          pageSize: PAGE_SIZE,
          userId: userFilter,
          query,
          refresh,
        });
        if (seq !== requestSeq.current) return; // пришёл ответ на устаревший запрос
        setFeed(data);
        if (jumpToHits.current) {
          jumpToHits.current = false;
          // в текущей вкладке ничего не нашлось, а в другой есть — переключаемся туда
          const here = (data.tabs || []).find((t) => t.id === data.tab);
          const firstHit = (data.tabs || []).find((t) => t.count > 0);
          if (data.terms?.length && here && here.count === 0 && firstHit) {
            setTab(firstHit.id);
            setPage(1);
            return;
          }
        }
        // сервер мог поправить вкладку (нет доступа) или страницу (за пределами диапазона)
        if (data.tab && data.tab !== tab) setTab(data.tab);
        if (data.page && data.page !== page) setPage(data.page);
      } catch (e) {
        if (seq === requestSeq.current) setError(String(e.message || e));
      } finally {
        if (seq === requestSeq.current) setLoading(false);
      }
    },
    [tab, page, userFilter, query]
  );

  useEffect(() => {
    loadFeed();
  }, [loadFeed]);

  useEffect(() => {
    if (!focus) return;
    const nextTab = focus.tab === "users" && !canViewUserEvents ? "reports" : focus.tab;
    if (nextTab) {
      setTab(nextTab);
      setPage(1);
    }
    if (focus.jobId) setSelectedJobId(focus.jobId);
    if (focus.reportId) setSelectedReportId(focus.reportId);
    onFocusConsumed?.();
  }, [focus, onFocusConsumed, canViewUserEvents]);

  useEffect(() => {
    if (!selectedJobId) {
      setJobDetail(null);
      return undefined;
    }
    let cancelled = false;
    setDetailLoading(true);
    fetchActivityJob(selectedJobId)
      .then((data) => {
        if (!cancelled) setJobDetail(data);
      })
      .catch((e) => {
        if (!cancelled) setError(String(e.message || e));
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedJobId]);

  const openReport = useCallback((jobId, reportId) => {
    setTab("reports");
    setPage(1);
    setSelectedJobId(jobId);
    setSelectedReportId(reportId);
  }, []);

  const changeTab = (_, value) => {
    setTab(value);
    setPage(1);
  };

  // Запрос уходит на сервер через 400 мс после последнего нажатия клавиши.
  useEffect(() => {
    const next = searchInput.trim();
    if (next === query) return undefined;
    const timer = setTimeout(() => {
      jumpToHits.current = true;
      setQuery(next);
      setPage(1);
    }, 400);
    return () => clearTimeout(timer);
  }, [searchInput, query]);

  const clearSearch = () => {
    setSearchInput("");
    jumpToHits.current = false;
    setQuery("");
    setPage(1);
  };

  const changeUserFilter = (value) => {
    setUserFilter(value);
    setPage(1);
  };

  const userOptions = useMemo(() => {
    const opts = [{ value: "", label: "Все пользователи" }];
    if (feed.viewer_can_see_all) {
      opts.push({ value: "system", label: "Система (по расписанию)" });
    }
    const seen = new Set();
    for (const u of feed.users || []) {
      if (seen.has(u.id)) continue;
      seen.add(u.id);
      opts.push({ value: String(u.id), label: u.username });
    }
    return opts;
  }, [feed.users, feed.viewer_can_see_all]);

  const tabs = feed.tabs || [];
  const activeTabId = tabs.some((item) => item.id === tab) ? tab : tabs[0]?.id;
  const items = feed.items || [];
  const errorCount = useMemo(
    () => (tab === "reports" ? items.filter((ev) => ev.kind === "error").length : 0),
    [tab, items]
  );

  const searching = Boolean(query);
  const searchTerms = useMemo(() => feed.terms || [], [feed.terms]);
  const emptyForFilter = searching
    ? `Ничего не найдено по запросу «${query}».`
    : userFilter
      ? "Нет записей для выбранного пользователя."
      : "Пока нет записей.";
  const total = feed.total || 0;
  const from = total ? (feed.page - 1) * PAGE_SIZE + 1 : 0;
  const to = Math.min(feed.page * PAGE_SIZE, total);

  const pager =
    total > 0 ? (
      <Stack
        direction={{ xs: "column", sm: "row" }}
        spacing={1}
        alignItems="center"
        justifyContent="space-between"
      >
        <Typography variant="body2" color="text.secondary">
          Записи {from}–{to} из {total}
        </Typography>
        {feed.pages > 1 && (
          <Pagination
            count={feed.pages}
            page={feed.page}
            onChange={(_, value) => setPage(value)}
            disabled={loading}
            color="primary"
            size="small"
            showFirstButton
            showLastButton
          />
        )}
      </Stack>
    ) : null;

  return (
    <SearchTermsContext.Provider value={searchTerms}>
    <Card className="hero-card" elevation={0}>
      <CardContent sx={{ p: 2.5 }}>
        <Stack spacing={2}>
          <Stack direction="row" justifyContent="space-between" alignItems="center" flexWrap="wrap" gap={1}>
            <Stack direction="row" spacing={1} alignItems="center">
              <BugReportRoundedIcon color={errorCount > 0 ? "error" : "action"} />
              <Typography variant="h6">Логи</Typography>
            </Stack>
            <Button
              size="small"
              startIcon={<RefreshRoundedIcon />}
              onClick={() => loadFeed({ refresh: true })}
              disabled={loading}
            >
              Обновить
            </Button>
          </Stack>

          <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} alignItems={{ sm: "center" }}>
            <FormControl size="small" sx={{ minWidth: 220 }}>
              <InputLabel id="logs-user-filter-label">Пользователь</InputLabel>
              <Select
                labelId="logs-user-filter-label"
                label="Пользователь"
                value={userFilter}
                onChange={(e) => changeUserFilter(e.target.value)}
              >
                {userOptions.map((opt) => (
                  <MenuItem key={opt.value || "all"} value={opt.value}>
                    {opt.label}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
            <TextField
              size="small"
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape" && searchInput) clearSearch();
              }}
              placeholder="Поиск по логам"
              label="Поиск"
              sx={{ minWidth: 260 }}
              slotProps={{
                input: {
                  startAdornment: (
                    <InputAdornment position="start">
                      <SearchRoundedIcon fontSize="small" />
                    </InputAdornment>
                  ),
                  endAdornment: searchInput ? (
                    <InputAdornment position="end">
                      <IconButton size="small" aria-label="Очистить поиск" onClick={clearSearch} edge="end">
                        <ClearRoundedIcon fontSize="small" />
                      </IconButton>
                    </InputAdornment>
                  ) : null,
                },
              }}
            />
            <Typography variant="body2" color="text.secondary" sx={{ flex: 1 }}>
              {searching
                ? "Показаны только совпадения; в скобках у вкладок — число найденных записей. Esc — сбросить поиск."
                : canViewUserEvents
                  ? "Правки разложены по вкладкам, по 50 записей на странице. Клик по красной сводке — технический лог."
                  : "Ошибки по сводкам и несовпадения сезонов. Клик по красной сводке — технический лог."}
            </Typography>
          </Stack>

          {error && (
            <Alert severity="error" sx={{ py: 0.5 }}>
              {error}
            </Alert>
          )}

          {tabs.length > 1 ? (
            <Tabs
              value={activeTabId}
              onChange={changeTab}
              variant="scrollable"
              scrollButtons="auto"
              allowScrollButtonsMobile
              sx={{ minHeight: 40 }}
            >
              {tabs.map((item) => (
                <Tab
                  key={item.id}
                  value={item.id}
                  label={item.count > 0 ? `${item.label} (${item.count})` : item.label}
                  sx={{ minHeight: 40, py: 0.5, textTransform: "none", fontSize: "0.8125rem" }}
                />
              ))}
            </Tabs>
          ) : (
            <Typography variant="subtitle2" fontWeight={700}>
              {tabs[0]?.label || "Сводки"}
            </Typography>
          )}

          {loading && (
            <Stack direction="row" spacing={1} alignItems="center">
              <CircularProgress size={18} />
              <Typography variant="body2" color="text.secondary">
                Загрузка…
              </Typography>
            </Stack>
          )}

          {activeTabId !== "reports" && !loading && (
            <>
              <UserEventList
                events={items}
                emptyText={
                  activeTabId === "seasons" && !userFilter
                    ? "Несовпадений сезона задания и года культуры нет. Они появятся после генерации сводок."
                    : emptyForFilter
                }
                onOpenReport={openReport}
              />
              {pager}
            </>
          )}

          {activeTabId === "reports" && !loading && (
            <Stack direction={{ xs: "column", md: "row" }} spacing={2} alignItems="stretch">
              <Stack spacing={0.75} sx={{ flex: 1, minWidth: 0 }}>
                {items.length === 0 && (
                  <Typography variant="body2" color="text.secondary">
                    {searching || userFilter ? emptyForFilter : "Ошибок по сводкам нет."}
                  </Typography>
                )}
                {items.map((ev) => {
                  if (ev.kind !== "error") {
                    return (
                      <Box key={ev.id} className="log-event-card log-event-card--compact">
                        <LogEventLine
                          when={formatWhen(ev.created_at)}
                          username={ev.username}
                          message={displayMessage(ev)}
                        />
                      </Box>
                    );
                  }
                  const active =
                    selectedJobId === ev.job_id && String(selectedReportId) === String(ev.report_id);
                  const errText = (ev.error || displayMessage(ev) || "").slice(0, 280);
                  return (
                    <Box
                      key={ev.id}
                      component="button"
                      type="button"
                      className={`log-event-card log-event-card--compact log-event-card--clickable${active ? " log-event-card--active" : ""}`}
                      onClick={() => openReport(ev.job_id, ev.report_id)}
                    >
                      <LogEventLine
                        when={formatWhen(ev.created_at)}
                        username={ev.username}
                        extra={ev.summary_date}
                        message={`${reportLabel(ev.report_id, ev.title)}: ${errText}`}
                      />
                    </Box>
                  );
                })}
                {pager}
              </Stack>
              <Box sx={{ flex: 1.2, minWidth: 0 }}>
                <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 1 }}>
                  Детали задания
                </Typography>
                {detailLoading && <CircularProgress size={20} />}
                {!detailLoading && (
                  <ReportDetailPanel detail={jobDetail} highlightReportId={selectedReportId} />
                )}
              </Box>
            </Stack>
          )}
        </Stack>
      </CardContent>
    </Card>
    </SearchTermsContext.Provider>
  );
}
