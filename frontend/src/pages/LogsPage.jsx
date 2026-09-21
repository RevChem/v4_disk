import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
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
  Typography,
} from "@mui/material";
import BugReportRoundedIcon from "@mui/icons-material/BugReportRounded";
import PersonOutlineRoundedIcon from "@mui/icons-material/PersonOutlineRounded";
import DescriptionOutlinedIcon from "@mui/icons-material/DescriptionOutlined";
import RefreshRoundedIcon from "@mui/icons-material/RefreshRounded";

import { fetchActivityFeed, fetchActivityJob } from "../api";
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

function LogEventMeta({ when, username, extra }) {
  return (
    <Box component="span" className="log-event-meta">
      {when}
      {username ? ` · ${username}` : ""}
      {extra ? ` · ${extra}` : ""}
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
        {message ? ` — ${message}` : ""}
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
                {(r.log_lines || []).join("\n")}
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

function matchesUserFilter(ev, filterUserId) {
  if (!filterUserId) return true;
  if (filterUserId === "system") return ev.user_id == null;
  return String(ev.user_id) === String(filterUserId);
}

export default function LogsPage({ focus, onFocusConsumed, canViewUserEvents = false }) {
  const [tab, setTab] = useState(canViewUserEvents ? "users" : "reports");
  const [feed, setFeed] = useState({ user_events: [], report_events: [], users: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selectedJobId, setSelectedJobId] = useState(null);
  const [selectedReportId, setSelectedReportId] = useState(null);
  const [jobDetail, setJobDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [userFilter, setUserFilter] = useState("");

  const loadFeed = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await fetchActivityFeed(80);
      setFeed(data);
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadFeed();
  }, [loadFeed]);

  useEffect(() => {
    if (!canViewUserEvents && tab === "users") {
      setTab("reports");
    }
  }, [canViewUserEvents, tab]);

  useEffect(() => {
    if (!focus) return;
    const nextTab = focus.tab === "users" && !canViewUserEvents ? "reports" : focus.tab;
    if (nextTab) setTab(nextTab);
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
    setSelectedJobId(jobId);
    setSelectedReportId(reportId);
  }, []);

  const userOptions = useMemo(() => {
    const fromApi = feed.users || [];
    const seen = new Set();
    const opts = [{ value: "", label: "Все пользователи" }];
    for (const u of fromApi) {
      if (seen.has(u.id)) continue;
      seen.add(u.id);
      opts.push({ value: String(u.id), label: u.username });
    }
    return opts;
  }, [feed.users]);

  const filteredUserEvents = useMemo(
    () => (feed.user_events || []).filter((ev) => matchesUserFilter(ev, userFilter)),
    [feed.user_events, userFilter]
  );

  const filteredReportEvents = useMemo(
    () => (feed.report_events || []).filter((ev) => matchesUserFilter(ev, userFilter)),
    [feed.report_events, userFilter]
  );

  const errorCount = useMemo(
    () => filteredReportEvents.filter((r) => r.status === "error").length,
    [filteredReportEvents]
  );

  return (
    <Card className="hero-card" elevation={0}>
      <CardContent sx={{ p: 2.5 }}>
        <Stack spacing={2}>
          <Stack direction="row" justifyContent="space-between" alignItems="center" flexWrap="wrap" gap={1}>
            <Stack direction="row" spacing={1} alignItems="center">
              <BugReportRoundedIcon color={errorCount > 0 ? "error" : "action"} />
              <Typography variant="h6">
                Логи
                {errorCount > 0 ? ` · ${errorCount} ошибок` : ""}
              </Typography>
            </Stack>
            <Button size="small" startIcon={<RefreshRoundedIcon />} onClick={loadFeed} disabled={loading}>
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
                onChange={(e) => setUserFilter(e.target.value)}
              >
                {userOptions.map((opt) => (
                  <MenuItem key={opt.value || "all"} value={opt.value}>
                    {opt.label}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
            <Typography variant="body2" color="text.secondary" sx={{ flex: 1 }}>
              Действия и ошибки по сводкам. Клик по красной сводке — технический лог.
            </Typography>
          </Stack>

          {error && (
            <Alert severity="error" sx={{ py: 0.5 }}>
              {error}
            </Alert>
          )}

          {canViewUserEvents ? (
            <Tabs value={tab} onChange={(_, v) => setTab(v)} sx={{ minHeight: 40 }}>
              <Tab
                value="users"
                icon={<PersonOutlineRoundedIcon sx={{ fontSize: 18 }} />}
                iconPosition="start"
                label="Пользователи"
                sx={{ minHeight: 40, py: 0.5 }}
              />
              <Tab
                value="reports"
                icon={<DescriptionOutlinedIcon sx={{ fontSize: 18 }} />}
                iconPosition="start"
                label="Сводки"
                sx={{ minHeight: 40, py: 0.5 }}
              />
            </Tabs>
          ) : (
            <Typography variant="subtitle2" fontWeight={700}>
              Сводки
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

          {tab === "users" && !loading && (
            <Stack spacing={0.75}>
              {filteredUserEvents.length === 0 && (
                <Typography variant="body2" color="text.secondary">
                  {userFilter ? "Нет записей для выбранного пользователя." : "Пока нет записей."}
                </Typography>
              )}
              {filteredUserEvents.map((ev) => (
                <Box key={ev.id} className="log-event-card log-event-card--compact">
                  <LogEventLine
                    when={formatWhen(ev.created_at)}
                    username={ev.username}
                    message={displayMessage(ev)}
                    reports={ev.reports}
                    durationSeconds={ev.duration_seconds}
                    onErrorClick={ev.job_id ? (rid) => openReport(ev.job_id, rid) : undefined}
                  />
                </Box>
              ))}
            </Stack>
          )}

          {tab === "reports" && !loading && (
            <Stack direction={{ xs: "column", md: "row" }} spacing={2} alignItems="stretch">
              <Stack spacing={0.75} sx={{ flex: 1, minWidth: 0 }}>
                {filteredReportEvents.length === 0 && (
                  <Typography variant="body2" color="text.secondary">
                    {userFilter ? "Нет ошибок для выбранного пользователя." : "Ошибок по сводкам нет."}
                  </Typography>
                )}
                {filteredReportEvents.map((ev) => {
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
  );
}
