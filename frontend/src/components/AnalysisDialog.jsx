import { useEffect, useState } from "react";
import { Box, ButtonBase, Dialog, IconButton, Stack, Tooltip, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import AgricultureOutlinedIcon from "@mui/icons-material/AgricultureOutlined";
import CloseFullscreenRoundedIcon from "@mui/icons-material/CloseFullscreenRounded";
import CloseRoundedIcon from "@mui/icons-material/CloseRounded";
import OpenInFullRoundedIcon from "@mui/icons-material/OpenInFullRounded";
import FactCheckOutlinedIcon from "@mui/icons-material/FactCheckOutlined";
import InsightsOutlinedIcon from "@mui/icons-material/InsightsOutlined";
import LocalGasStationOutlinedIcon from "@mui/icons-material/LocalGasStationOutlined";
import QuestionAnswerOutlinedIcon from "@mui/icons-material/QuestionAnswerOutlined";
import AssignmentOutlinedIcon from "@mui/icons-material/AssignmentOutlined";
import QueryStatsRoundedIcon from "@mui/icons-material/QueryStatsRounded";

import ChecksPanel from "./analysisChecks/ChecksPanel";
import AnalyticsPanel from "./AnalyticsPanel";
import DriverWorkPanel from "./DriverWorkPanel";
import FuelPanel from "./FuelPanel";
import TaskFactsPanel from "./TaskFactsPanel";
import TasksLookupPanel from "./TasksLookupPanel";

const SECTIONS = [
  {
    id: "analytics",
    title: "Аналитика",
    hint: "Темп, техника, покрытие, культуры",
    icon: InsightsOutlinedIcon,
  },
  {
    id: "fuel",
    title: "Топливо",
    hint: "Поле и дорога, аномалии расхода",
    icon: LocalGasStationOutlinedIcon,
  },
  {
    id: "driver",
    title: "Работа исполнителя",
    hint: "Гектары по дням, полям и заданиям",
    icon: AgricultureOutlinedIcon,
  },
  {
    id: "facts",
    title: "Фильтрация",
    hint: "Срез заданий за смену: вчера 06:00 — сегодня 05:59",
    icon: QuestionAnswerOutlinedIcon,
  },
  {
    id: "checks",
    title: "Проверки",
    hint: "19 срезов по людям, технике и полям",
    icon: FactCheckOutlinedIcon,
  },
  {
    id: "tasks",
    title: "Задания",
    hint: "Карточка задания Cropwise по номеру",
    icon: AssignmentOutlinedIcon,
  },
];

/** Панели, которым нужна вся высота окна и собственная прокрутка внутри. */
const FILL = new Set(["facts"]);

/** Разделы анализа, которые видит администратор. Аналитика и проверки остаются у разработчика. */
const WORK_IDS = new Set(["fuel", "driver", "facts", "tasks"]);

function isComputerFullscreen() {
  return document.fullscreenElement === document.documentElement;
}

export default function AnalysisDialog({
  open,
  onClose,
  initialSection = "analytics",
  summaryDate = "",
  scope = "all",
}) {
  const sections = scope === "all" ? SECTIONS : SECTIONS.filter((item) => WORK_IDS.has(item.id));
  const [section, setSection] = useState(initialSection);
  const [visited, setVisited] = useState(() => new Set([initialSection]));
  const [driverPreset, setDriverPreset] = useState(null);
  const [fullScreen, setFullScreen] = useState(false);

  useEffect(() => {
    const sync = () => setFullScreen(isComputerFullscreen());
    document.addEventListener("fullscreenchange", sync);
    return () => document.removeEventListener("fullscreenchange", sync);
  }, []);

  useEffect(() => {
    if (open) return undefined;
    if (document.fullscreenElement) {
      document.exitFullscreen().catch(() => {});
    }
    return undefined;
  }, [open]);

  useEffect(() => {
    if (!open || !fullScreen) return undefined;
    const onKey = (event) => {
      if (event.key !== "Escape" || !isComputerFullscreen()) return;
      const menuOpen = document.querySelector(".MuiMenu-root, .MuiPopover-root");
      if (menuOpen) {
        event.preventDefault();
        return;
      }
      event.stopPropagation();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [open, fullScreen]);

  async function toggleComputerFullscreen() {
    try {
      if (document.fullscreenElement) {
        await document.exitFullscreen();
      } else {
        await document.documentElement.requestFullscreen();
      }
    } catch {
      // Браузер может запретить полноэкранный режим без жеста пользователя.
    }
  }

  // При каждом открытии начинаем с нужного раздела; панели грузят данные только после первого показа.
  useEffect(() => {
    if (!open) return;
    const next = scope === "all" || WORK_IDS.has(initialSection) ? initialSection : "facts";
    setSection(next);
    // Уже открытые разделы не размонтируем: «Фильтрация» хранит последний срез.
    setVisited((prev) => (prev.has(next) ? prev : new Set(prev).add(next)));
    setDriverPreset(null);
  }, [open, initialSection, scope]);

  function pick(id) {
    setSection(id);
    setVisited((prev) => (prev.has(id) ? prev : new Set(prev).add(id)));
  }

  const current = sections.find((s) => s.id === section) || sections[0];

  return (
    <Dialog
      open={open}
      onClose={onClose}
      fullScreen={fullScreen}
      fullWidth
      maxWidth={false}
      scroll="paper"
      PaperProps={{
        sx: fullScreen
          ? {
              display: "flex",
              flexDirection: "column",
              width: "100%",
              height: "100%",
              maxWidth: "100%",
              maxHeight: "100%",
              m: 0,
              borderRadius: 0,
              overflow: "hidden",
            }
          : {
              width: "min(1600px, 97vw)",
              height: "94vh",
              maxHeight: "94vh",
              m: { xs: 0.5, sm: 2 },
              display: "flex",
              flexDirection: "column",
              overflow: "hidden",
            },
      }}
    >
      <Stack direction="row" alignItems="center" spacing={1.25} sx={{ px: 2.5, pt: 1.75, pb: 1.25 }}>
        <QueryStatsRoundedIcon color="primary" />
        <Typography variant="h6" fontWeight={700}>
          Анализ
        </Typography>
        <Typography variant="body2" color="text.secondary" sx={{ display: { xs: "none", sm: "block" } }}>
          · {current.title}
        </Typography>
        <Box sx={{ flex: 1 }} />
        <Tooltip title={fullScreen ? "Выйти из полноэкранного режима (Esc)" : "На весь экран компьютера"}>
          <IconButton
            aria-label={fullScreen ? "Выйти из полноэкранного режима" : "На весь экран компьютера"}
            onClick={() => void toggleComputerFullscreen()}
          >
            {fullScreen ? <CloseFullscreenRoundedIcon /> : <OpenInFullRoundedIcon />}
          </IconButton>
        </Tooltip>
        <IconButton aria-label="Закрыть" onClick={onClose}>
          <CloseRoundedIcon />
        </IconButton>
      </Stack>

      <Box
        sx={{
          display: "grid",
          gap: 1.25,
          px: 2.5,
          pb: 1.75,
          gridTemplateColumns: { xs: "repeat(2, minmax(0, 1fr))", md: "repeat(3, minmax(0, 1fr))", xl: "repeat(6, minmax(0, 1fr))" },
          borderBottom: 1,
          borderColor: "divider",
        }}
      >
        {sections.map(({ id, title, hint, icon: Icon }) => {
          const on = id === section;
          return (
            <ButtonBase
              key={id}
              onClick={() => pick(id)}
              aria-pressed={on}
              sx={(theme) => ({
                display: "flex",
                alignItems: "center",
                justifyContent: "flex-start",
                gap: { xs: 1, sm: 1.5 },
                textAlign: "left",
                px: { xs: 1.25, sm: 1.75 },
                py: 1.25,
                borderRadius: 2,
                border: 1,
                borderColor: on ? "primary.main" : "divider",
                bgcolor: on ? alpha(theme.palette.primary.main, theme.palette.mode === "dark" ? 0.18 : 0.08) : "transparent",
                transition: "background-color .15s, border-color .15s, transform .15s",
                "&:hover": {
                  bgcolor: alpha(theme.palette.primary.main, theme.palette.mode === "dark" ? 0.14 : 0.06),
                  borderColor: "primary.main",
                },
                "&:focus-visible": { outline: `2px solid ${theme.palette.primary.main}`, outlineOffset: 2 },
              })}
            >
              <Box
                sx={(theme) => ({
                  display: "grid",
                  placeItems: "center",
                  width: 40,
                  height: 40,
                  flexShrink: 0,
                  borderRadius: 1.5,
                  color: on ? "primary.contrastText" : "primary.main",
                  bgcolor: on ? "primary.main" : alpha(theme.palette.primary.main, 0.12),
                })}
              >
                <Icon />
              </Box>
              <Box sx={{ minWidth: 0 }}>
                <Typography
                  variant="subtitle2"
                  fontWeight={700}
                  sx={{ lineHeight: 1.25, whiteSpace: { xs: "normal", sm: "nowrap" }, overflow: "hidden", textOverflow: "ellipsis" }}
                >
                  {title}
                </Typography>
                <Typography
                  variant="caption"
                  color="text.secondary"
                  sx={{ display: { xs: "none", sm: "block" }, lineHeight: 1.25 }}
                  noWrap
                >
                  {hint}
                </Typography>
              </Box>
            </ButtonBase>
          );
        })}
      </Box>

      <Box sx={{ flex: 1, minHeight: 0, position: "relative" }}>
        {sections.map(({ id }) =>
          visited.has(id) ? (
            <Box
              key={id}
              role="tabpanel"
              hidden={id !== section}
              sx={{
                display: id === section ? "block" : "none",
                height: "100%",
                overflow: FILL.has(id) ? "hidden" : "auto",
                p: { xs: 1.5, sm: 2.5 },
              }}
            >
              {id === "analytics" && <AnalyticsPanel active={section === id} />}
              {id === "fuel" && <FuelPanel active={section === id} summaryDate={summaryDate} />}
              {id === "driver" && <DriverWorkPanel summaryDate={summaryDate} preset={driverPreset} />}
              {id === "facts" && (
                <TaskFactsPanel active={section === id} summaryDate={summaryDate} />
              )}
              {id === "tasks" && <TasksLookupPanel />}
              {id === "checks" && (
                <ChecksPanel
                  active={section === id}
                  summaryDate={summaryDate}
                  onOpenDriver={(spec) => {
                    setDriverPreset({ ...spec, token: Date.now() });
                    pick("driver");
                  }}
                />
              )}
            </Box>
          ) : null,
        )}
      </Box>
    </Dialog>
  );
}
