import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CssBaseline, ThemeProvider } from "@mui/material";

import AiMenuDialog from "./components/AiMenuDialog";
import AiPromptsDialog from "./components/AiPromptsDialog";
import AiReportDialog from "./components/AiReportDialog";
import TaskFactsDialog from "./components/TaskFactsDialog";
import DriverWorkDialog from "./components/DriverWorkDialog";
import GenerationQueueDrawer from "./components/GenerationQueueDrawer";
import InterfaceMenuDialog from "./components/InterfaceMenuDialog";
import PageModalDialog from "./components/PageModalDialog";
import {
  cancelGenerationJob,
  clearStoredTokens,
  fetchCropwiseCookie,
  fetchGenerationJobs,
  fetchInterfaceBackground,
  fetchInterfaceSettings,
  fetchMe,
  fetchProverkaAiReport,
  fetchProverkaMonitoring,
  getStoredTokens,
  login,
  logout,
  requestPasswordReset,
  resetPasswordWithToken,
  runProverkaAiReport,
  setStoredTokens,
  verifyMfaLogin,
} from "./api";
import { DEFAULT_THEME, isValidTheme } from "./constants/themes";
import DashboardPage from "./pages/DashboardPage";
import LogsPage from "./pages/LogsPage";
import SchedulePage from "./pages/SchedulePage";
import UsersPage from "./pages/UsersPage";
import AppShell from "./shell/AppShell";
import LoginPage from "./shell/LoginPage";
import { createAppTheme } from "./theme";
import logoUrl from "./assets/logo.png";

function todayIso() {
  return new Date().toISOString().slice(0, 10);
}

function formatQueueTime(iso) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  } catch {
    return "";
  }
}

export default function App() {
  const interfaceReqRef = useRef(0);
  const backgroundVersionRef = useRef(null);
  const [theme, setTheme] = useState(DEFAULT_THEME);
  const [customBackground, setCustomBackground] = useState("");
  const [interfaceSettingsLoaded, setInterfaceSettingsLoaded] = useState(false);
  const [user, setUser] = useState(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [mfaCode, setMfaCode] = useState("");
  const [mfaRequired, setMfaRequired] = useState(false);
  const [mfaPendingToken, setMfaPendingToken] = useState("");
  const [authError, setAuthError] = useState("");
  const [authInfo, setAuthInfo] = useState("");
  const [authView, setAuthView] = useState(() =>
    new URLSearchParams(window.location.search).get("reset_token") ? "reset" : "login",
  );
  const [resetToken, setResetToken] = useState(
    () => new URLSearchParams(window.location.search).get("reset_token") || "",
  );
  const [resetPassword, setResetPassword] = useState("");
  const [resetConfirm, setResetConfirm] = useState("");
  const [authBusy, setAuthBusy] = useState(false);
  const [activeNav, setActiveNav] = useState("dashboard");
  const [generationQueue, setGenerationQueue] = useState([]);
  const [queueDrawerOpen, setQueueDrawerOpen] = useState(false);
  const [queueTick, setQueueTick] = useState(0);
  const [summaryDate, setSummaryDate] = useState(todayIso);
  const [monitoring, setMonitoring] = useState(null);
  const [aiReportOpen, setAiReportOpen] = useState(false);
  const [aiPromptsOpen, setAiPromptsOpen] = useState(false);
  const [aiMenuOpen, setAiMenuOpen] = useState(false);
  const [taskFactsOpen, setTaskFactsOpen] = useState(false);
  const [taskFactsJobId, setTaskFactsJobId] = useState("");
  const [driverWorkOpen, setDriverWorkOpen] = useState(false);
  const [interfaceOpen, setInterfaceOpen] = useState(false);
  const [aiReport, setAiReport] = useState(null);
  const [aiReportLoading, setAiReportLoading] = useState(false);
  const [aiReportError, setAiReportError] = useState("");
  const [aiRefreshing, setAiRefreshing] = useState(false);
  const [genFailedItems, setGenFailedItems] = useState([]);
  const [genErrorLogs, setGenErrorLogs] = useState([]);
  const [logsFocus, setLogsFocus] = useState(null);
  const [logsModalOpen, setLogsModalOpen] = useState(false);
  const [scheduleModalOpen, setScheduleModalOpen] = useState(false);
  const [usersModalOpen, setUsersModalOpen] = useState(false);
  const [dashboardHeaderTools, setDashboardHeaderTools] = useState(null);

  const POLL_IDLE_MS = 30_000;
  const POLL_ACTIVE_MS = 2_000;
  const prevQueueRef = useRef([]);

  const muiTheme = useMemo(() => createAppTheme(theme), [theme]);

  useEffect(() => {
    let favicon = document.querySelector("link[rel='icon']");
    if (!favicon) {
      favicon = document.createElement("link");
      favicon.setAttribute("rel", "icon");
      document.head.appendChild(favicon);
    }
    favicon.setAttribute("href", logoUrl);
  }, []);

  const isDeveloper = user?.role === "developer";
  const openTaskFacts = useCallback((jobId) => {
    setTaskFactsJobId(jobId || "");
    setTaskFactsOpen(true);
  }, []);
  const canGenerate =
    user?.role === "developer" || user?.role === "admin" || user?.role === "user";
  const [cropwiseOn, setCropwiseOn] = useState(
    () => window.localStorage.getItem("svodki.cropwiseCheck") === "1",
  );
  const [cropwiseCookie, setCropwiseCookie] = useState(false);
  const [equipmentRequest, setEquipmentRequest] = useState(0);
  const cropwiseActive = cropwiseOn && cropwiseCookie;
  const cropwiseCaption = !cropwiseCookie
    ? "На сервере нет cookie Cropwise — сводки создаются без проверки"
    : cropwiseActive
      ? "После создания сводок сверим их с work.json"
      : "Сводки создаются без проверки Cropwise";

  useEffect(() => {
    if (!canGenerate) return undefined;
    let cancelled = false;
    fetchCropwiseCookie()
      .then((available) => {
        if (!cancelled) setCropwiseCookie(available);
      })
      .catch(() => {
        if (!cancelled) setCropwiseCookie(false);
      });
    return () => {
      cancelled = true;
    };
  }, [canGenerate]);

  function toggleCropwise() {
    if (!cropwiseCookie) {
      setCropwiseOn(false);
      window.localStorage.setItem("svodki.cropwiseCheck", "0");
      return;
    }
    setCropwiseOn((prev) => {
      const next = !prev;
      window.localStorage.setItem("svodki.cropwiseCheck", next ? "1" : "0");
      return next;
    });
  }

  // Сброс интерфейса к умолчанию (выход, смена пользователя, ошибка загрузки).
  // Увеличение счётчика делает недействительными все уже запущенные загрузки.
  const resetInterface = useCallback(() => {
    interfaceReqRef.current += 1;
    backgroundVersionRef.current = null;
    setTheme(DEFAULT_THEME);
    setCustomBackground("");
  }, []);

  const applyInterfaceSettings = useCallback(async (settings) => {
    const reqId = ++interfaceReqRef.current;
    const nextTheme = isValidTheme(settings?.theme) ? settings.theme : DEFAULT_THEME;
    setTheme(nextTheme);
    if (!settings?.has_custom_background) {
      backgroundVersionRef.current = null;
      setCustomBackground("");
      return;
    }
    const version = settings.background_version || 0;
    if (backgroundVersionRef.current === version) {
      // Фон не менялся (например, сменили только тему) — не скачиваем заново.
      return;
    }
    try {
      const blobUrl = await fetchInterfaceBackground(version);
      if (reqId !== interfaceReqRef.current) {
        // За время загрузки пользователь сменился или пришли новые настройки.
        URL.revokeObjectURL(blobUrl);
        return;
      }
      backgroundVersionRef.current = version;
      setCustomBackground(blobUrl);
    } catch {
      if (reqId === interfaceReqRef.current) {
        backgroundVersionRef.current = null;
        setCustomBackground("");
      }
    }
  }, []);

  const loadInterfaceSettings = useCallback(async () => {
    if (!getStoredTokens().access) {
      resetInterface();
      setInterfaceSettingsLoaded(true);
      return;
    }
    const startedAt = interfaceReqRef.current;
    try {
      const settings = await fetchInterfaceSettings();
      if (startedAt !== interfaceReqRef.current) return; // пользователь сменился
      await applyInterfaceSettings(settings);
    } catch {
      if (startedAt !== interfaceReqRef.current) return;
      resetInterface();
    }
    setInterfaceSettingsLoaded(true);
  }, [applyInterfaceSettings, resetInterface]);

  useEffect(() => {
    if (!authChecked) return;
    // Другой пользователь — начинаем с чистого интерфейса, чужой фон не показываем.
    resetInterface();
    setInterfaceSettingsLoaded(false);
    loadInterfaceSettings();
  }, [authChecked, user?.id, loadInterfaceSettings, resetInterface]);

  // Blob-URL фона освобождается, когда он заменён новым или компонент удалён:
  // старый адрес живёт, пока новый не отрисован (без «мигания»).
  useEffect(() => {
    if (!customBackground) return undefined;
    return () => URL.revokeObjectURL(customBackground);
  }, [customBackground]);

  useEffect(() => {
    document.documentElement.dataset.diskTheme = theme;
  }, [theme]);

  const loadUser = useCallback(async () => {
    const { access } = getStoredTokens();
    if (!access) {
      setUser(null);
      setAuthChecked(true);
      return;
    }
    try {
      const me = await fetchMe();
      setUser(me);
    } catch {
      clearStoredTokens();
      setUser(null);
    } finally {
      setAuthChecked(true);
    }
  }, []);

  const loadGenerationQueue = useCallback(async () => {
    if (!canGenerate) {
      setGenerationQueue([]);
      return;
    }
    try {
      const data = await fetchGenerationJobs();
      setGenerationQueue(data.items || []);
    } catch {
      setGenerationQueue([]);
    }
  }, [canGenerate]);

  const handleCancelGenerationJob = useCallback(
    async (jobId, reportIds = null) => {
      await cancelGenerationJob(jobId, reportIds);
      await loadGenerationQueue();
    },
    [loadGenerationQueue]
  );

  useEffect(() => {
    loadUser();
  }, [loadUser]);

  const hasActiveGenerationJobs = useMemo(
    () =>
      generationQueue.some((job) => job.status === "pending" || job.status === "running"),
    [generationQueue]
  );

  useEffect(() => {
    if (!user) return undefined;
    loadGenerationQueue();
    const pollMs = hasActiveGenerationJobs ? POLL_ACTIVE_MS : POLL_IDLE_MS;
    const id = setInterval(loadGenerationQueue, pollMs);
    return () => clearInterval(id);
  }, [user, loadGenerationQueue, queueTick, hasActiveGenerationJobs, POLL_ACTIVE_MS, POLL_IDLE_MS]);

  useEffect(() => {
    const prev = prevQueueRef.current;
    let queueChanged = false;
    for (const job of generationQueue) {
      const old = prev.find((item) => item.id === job.id);
      if (!old) {
        if (job.status !== "pending") queueChanged = true;
        continue;
      }
      if (old.status !== job.status) queueChanged = true;
      if (old.updated_at !== job.updated_at) queueChanged = true;
      const oldDone = old.completed_report_ids?.length ?? 0;
      const newDone = job.completed_report_ids?.length ?? 0;
      if (newDone > oldDone) queueChanged = true;
    }
    prevQueueRef.current = generationQueue;
    if (queueChanged) {
      setQueueTick((tick) => tick + 1);
    }
  }, [generationQueue]);

  const loadMonitoring = useCallback(async () => {
    if (!isDeveloper) return;
    try {
      const data = await fetchProverkaMonitoring(summaryDate);
      setMonitoring(data);
    } catch {
      setMonitoring(null);
    }
  }, [isDeveloper, summaryDate]);

  const loadAiReport = useCallback(async () => {
    if (!isDeveloper) return;
    try {
      const data = await fetchProverkaAiReport(summaryDate);
      setAiReport(data);
    } catch {
      setAiReport(null);
    }
  }, [isDeveloper, summaryDate]);

  useEffect(() => {
    if (!user || !isDeveloper) return undefined;
    loadMonitoring();
    loadAiReport();
    const pollMs = hasActiveGenerationJobs ? POLL_ACTIVE_MS : POLL_IDLE_MS;
    const id = setInterval(() => {
      loadMonitoring();
      loadAiReport();
    }, pollMs);
    return () => clearInterval(id);
  }, [
    user,
    isDeveloper,
    summaryDate,
    loadMonitoring,
    loadAiReport,
    hasActiveGenerationJobs,
    POLL_ACTIVE_MS,
    POLL_IDLE_MS,
  ]);

  useEffect(() => {
    if (!isDeveloper) return;
    const recentDone = generationQueue.some(
      (j) =>
        j.status === "completed" &&
        j.updated_at &&
        Date.now() - new Date(j.updated_at).getTime() < 60_000
    );
    if (recentDone) loadMonitoring();
  }, [generationQueue, isDeveloper, loadMonitoring]);

  const aiStatus = aiReport?.ai_status || "none";
  const aiViolationCount = aiReport?.ai_violation_count ?? 0;
  const queueLabel = useMemo(() => {
    const active = generationQueue.filter(
      (j) => j.status === "pending" || j.status === "running"
    );
    if (!active.length) return "Очередь";
    const latest = [...active].sort(
      (a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0)
    )[0];
    const time = formatQueueTime(latest?.created_at);
    if (time) return `Очередь · ${time}`;
    return `Очередь · ${active.length}`;
  }, [generationQueue]);

  async function handleAiReportClick() {
    setAiReportError("");
    setAiReportLoading(true);
    setAiReportOpen(true);
    try {
      const data = await fetchProverkaAiReport(summaryDate);
      setAiReport(data);
    } catch (e) {
      setAiReportError(String(e.message || e));
    } finally {
      setAiReportLoading(false);
    }
  }

  async function handleAiReportRefresh() {
    setAiRefreshing(true);
    setAiReportError("");
    try {
      await runProverkaAiReport(summaryDate);
      setTimeout(async () => {
        try {
          const data = await fetchProverkaAiReport(summaryDate);
          setAiReport(data);
        } catch (e) {
          setAiReportError(String(e.message || e));
        } finally {
          setAiRefreshing(false);
        }
      }, 3000);
    } catch (e) {
      setAiReportError(String(e.message || e));
      setAiRefreshing(false);
    }
  }

  const navActiveId = logsModalOpen
    ? "logs"
    : scheduleModalOpen
      ? "schedule"
      : usersModalOpen
        ? "users"
        : activeNav;

  function handleNavChange(id) {
    if (id === "users") {
      setUsersModalOpen(true);
      return;
    }
    if (id === "logs") {
      setLogsModalOpen(true);
      return;
    }
    if (id === "schedule") {
      setScheduleModalOpen(true);
      return;
    }
    setActiveNav(id);
  }

  const nav = useMemo(() => {
    const items = [];
    if (user) {
      items.push({ id: "users", label: "Пользователи" });
    }
    items.push({ id: "dashboard", label: "Сводки" });
    if (canGenerate) {
      items.push({ id: "logs", label: "Логи" });
    }
    if (user?.role === "developer" || user?.role === "admin") {
      items.push({ id: "schedule", label: "Расписание" });
    }
    return items;
  }, [user, isDeveloper, canGenerate]);

  function clearResetTokenFromUrl() {
    const url = new URL(window.location.href);
    url.searchParams.delete("reset_token");
    window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
    setResetToken("");
  }

  function backToLogin() {
    clearResetTokenFromUrl();
    setAuthView("login");
    setAuthError("");
    setResetPassword("");
    setResetConfirm("");
  }

  async function handleAuthSubmit(e) {
    e.preventDefault();
    setAuthError("");
    if (authView === "forgot") {
      setAuthBusy(true);
      setAuthInfo("");
      try {
        const res = await requestPasswordReset(email.trim());
        setAuthInfo(res?.detail || "Если такой email есть в системе, мы отправили письмо со ссылкой.");
        setAuthView("login");
      } catch (err) {
        setAuthError(String(err.message || err));
      } finally {
        setAuthBusy(false);
      }
      return;
    }
    if (authView === "reset") {
      if (resetPassword.length < 8) {
        setAuthError("Новый пароль — минимум 8 символов");
        return;
      }
      if (resetPassword !== resetConfirm) {
        setAuthError("Новый пароль и подтверждение не совпадают");
        return;
      }
      setAuthBusy(true);
      setAuthInfo("");
      try {
        const res = await resetPasswordWithToken(resetToken, resetPassword);
        clearResetTokenFromUrl();
        setResetPassword("");
        setResetConfirm("");
        setPassword("");
        await logout();
        setUser(null);
        setAuthView("login");
        setAuthInfo(res?.detail || "Пароль обновлён. Войдите с новым паролем.");
      } catch (err) {
        setAuthError(String(err.message || err));
      } finally {
        setAuthBusy(false);
      }
      return;
    }
    setAuthBusy(true);
    setAuthInfo("");
    try {
      if (mfaRequired) {
        const pair = await verifyMfaLogin(mfaPendingToken, mfaCode);
        setStoredTokens(pair.access_token, pair.refresh_token);
        setMfaRequired(false);
        setMfaPendingToken("");
        setMfaCode("");
      } else {
        const res = await login(email, password);
        if (res.mfa_required) {
          setMfaRequired(true);
          setMfaPendingToken(res.mfa_pending_token);
          return;
        }
        setStoredTokens(res.access_token, res.refresh_token);
      }
      await loadUser();
    } catch (err) {
      setAuthError(String(err.message || err));
    } finally {
      setAuthBusy(false);
    }
  }

  async function handlePasswordChanged() {
    await handleLogout();
    setAuthInfo("Пароль изменён. Войдите с новым паролем.");
  }

  async function handleLogout() {
    await logout();
    // Всё, что относится к вышедшему пользователю, сбрасываем сразу,
    // чтобы страница входа и следующий пользователь не увидели чужие данные.
    resetInterface();
    setUser(null);
    setActiveNav("dashboard");
    setGenerationQueue([]);
    setMonitoring(null);
    setAiReport(null);
    setAiReportError("");
    setAiReportOpen(false);
    setAiPromptsOpen(false);
    setAiMenuOpen(false);
    setInterfaceOpen(false);
    setGenFailedItems([]);
    setGenErrorLogs([]);
    setLogsFocus(null);
    setLogsModalOpen(false);
    setScheduleModalOpen(false);
    setUsersModalOpen(false);
    setQueueDrawerOpen(false);
    setDashboardHeaderTools(null);
    setEmail("");
    setPassword("");
    setMfaCode("");
    setMfaRequired(false);
    setMfaPendingToken("");
    setAuthError("");
  }

  if (!authChecked || !interfaceSettingsLoaded) {
    return null;
  }

  if (!user || authView === "reset") {
    return (
      <ThemeProvider theme={muiTheme}>
        <CssBaseline />
        <LoginPage
          theme={theme}
          customBackground={customBackground}
          view={authView}
          email={email}
          password={password}
          mfaCode={mfaCode}
          mfaRequired={mfaRequired}
          error={authError}
          info={authInfo}
          busy={authBusy}
          resetPassword={resetPassword}
          resetConfirm={resetConfirm}
          onEmailChange={setEmail}
          onPasswordChange={setPassword}
          onMfaCodeChange={setMfaCode}
          onResetPasswordChange={setResetPassword}
          onResetConfirmChange={setResetConfirm}
          onSubmit={handleAuthSubmit}
          onForgot={() => {
            setAuthView("forgot");
            setAuthError("");
            setAuthInfo("");
          }}
          onBackToLogin={backToLogin}
        />
      </ThemeProvider>
    );
  }

  return (
    <ThemeProvider theme={muiTheme}>
      <CssBaseline />
      <AppShell
        theme={theme}
        customBackground={customBackground}
        user={user}
        onLogout={handleLogout}
        onPasswordChanged={handlePasswordChanged}
        nav={nav}
        activeNav={activeNav}
        navActiveId={navActiveId}
        onNavChange={handleNavChange}
        queueLabel={queueLabel}
        onQueueOpen={() => setQueueDrawerOpen(true)}
        showQueueButton={canGenerate}
        cropwise={
          canGenerate
            ? { active: cropwiseActive, caption: cropwiseCaption, onToggle: toggleCropwise }
            : null
        }
        equipment={
          isDeveloper
            ? {
                caption:
                  "Сверить число техники в готовых Excel за выбранную дату со сводкой 2",
                onClick: () => setEquipmentRequest((n) => n + 1),
              }
            : null
        }
        showAiButton={isDeveloper}
        onInterfaceOpen={() => setInterfaceOpen(true)}
        onAiOpen={() => setAiMenuOpen(true)}
        aiStatus={aiStatus}
        aiViolationCount={aiViolationCount}
        secondaryToolbar={navActiveId === "dashboard" ? dashboardHeaderTools : null}
      >
        <DashboardPage
          user={user}
          generationQueue={generationQueue}
          summaryDate={summaryDate}
          onSummaryDateChange={setSummaryDate}
          onHeaderToolsChange={setDashboardHeaderTools}
          onGenerationDiagnostics={({ failedItems, logs }) => {
            setGenFailedItems(failedItems || []);
            setGenErrorLogs(logs || []);
          }}
          onProverkaComplete={() => {
            loadMonitoring();
            loadAiReport();
          }}
          onGenerationQueued={() => {
            setQueueTick((x) => x + 1);
            setQueueDrawerOpen(true);
          }}
          onOpenLogs={(focus) => {
            setLogsFocus(focus);
            setLogsModalOpen(true);
          }}
          onOpenTaskFacts={isDeveloper ? openTaskFacts : undefined}
          compareCropwise={cropwiseActive}
          equipmentRequest={equipmentRequest}
        />
      </AppShell>
      <GenerationQueueDrawer
        open={queueDrawerOpen && canGenerate}
        onClose={() => setQueueDrawerOpen(false)}
        jobs={generationQueue}
        onCancelJob={handleCancelGenerationJob}
      />
      <InterfaceMenuDialog
        open={interfaceOpen}
        onClose={() => setInterfaceOpen(false)}
        theme={theme}
        onThemeChange={setTheme}
        customBackground={customBackground}
        onSettingsSaved={applyInterfaceSettings}
      />
      <AiMenuDialog
        open={aiMenuOpen}
        onClose={() => setAiMenuOpen(false)}
        onOpenReport={handleAiReportClick}
        onOpenPrompts={() => setAiPromptsOpen(true)}
        onOpenTaskFacts={() => openTaskFacts()}
        onOpenDriverWork={() => setDriverWorkOpen(true)}
        isDeveloper={isDeveloper}
        summaryDate={summaryDate}
      />
      {user && (
        <PageModalDialog
          open={usersModalOpen}
          onClose={() => setUsersModalOpen(false)}
          title="Пользователи"
        >
          <UsersPage
            currentUserId={user.id}
            currentUser={user}
            manageAll={isDeveloper}
            embedded
            onPasswordChanged={handlePasswordChanged}
          />
        </PageModalDialog>
      )}
      {canGenerate && (
        <PageModalDialog
          open={logsModalOpen}
          onClose={() => setLogsModalOpen(false)}
          title="Логи"
        >
          <LogsPage
            focus={logsFocus}
            onFocusConsumed={() => setLogsFocus(null)}
            canViewUserEvents={isDeveloper}
          />
        </PageModalDialog>
      )}
      {canGenerate && (user?.role === "developer" || user?.role === "admin") && (
        <PageModalDialog
          open={scheduleModalOpen}
          onClose={() => setScheduleModalOpen(false)}
          title="Расписание"
        >
          <SchedulePage />
        </PageModalDialog>
      )}
      <AiReportDialog
        open={aiReportOpen && isDeveloper}
        onClose={() => setAiReportOpen(false)}
        summaryDate={summaryDate}
        aiData={aiReport}
        loading={aiReportLoading}
        error={aiReportError}
        onRefresh={handleAiReportRefresh}
        refreshing={aiRefreshing}
        onLoadReport={(data) => {
          if (data?.error) {
            setAiReportError(data.error);
          } else {
            setAiReport(data);
            setAiReportError("");
          }
        }}
      />
      <AiPromptsDialog
        open={aiPromptsOpen && isDeveloper}
        onClose={() => setAiPromptsOpen(false)}
      />
      <TaskFactsDialog
        open={taskFactsOpen && isDeveloper}
        onClose={() => setTaskFactsOpen(false)}
        summaryDate={summaryDate}
        initialJobId={taskFactsJobId}
      />
      <DriverWorkDialog
        open={driverWorkOpen && isDeveloper}
        onClose={() => setDriverWorkOpen(false)}
        summaryDate={summaryDate}
      />
    </ThemeProvider>
  );
}
