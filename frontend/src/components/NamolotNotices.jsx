import { useState } from "react";
import { Alert, Box, Typography } from "@mui/material";

/** Код предупреждения генерации → цвет всплывающего окна. */
const TONES = {
  namolot_plate_unparsed: {
    bg: "#c62828",
    title: "Госномер не разобран",
  },
  namolot_plate_unmatched: {
    bg: "#c62828",
    title: "Госномер не сопоставлен",
  },
  namolot_plate_without_task: {
    bg: "#4a148c",
    title: "Намолот без задания на уборку",
  },
  namolot_cache_fallback: {
    bg: "#6d1230",
    title: "Нет ответа от 1С",
  },
  namolot_tonnes_diff: {
    bg: "#e65100",
    title: "Расхождение с Namolot",
  },
};

function noticeKey(reportId, alert) {
  return `${reportId}|${alert.code}|${alert.message}`;
}

function plateNotice(message) {
  const text = String(message || "");
  const mark = "Госномера:";
  const at = text.indexOf(mark);
  if (at < 0) return { intro: text, lines: [] };
  const intro = text.slice(0, at).trim();
  const lines = text
    .slice(at + mark.length)
    .split(";")
    .map((part) => part.trim())
    .filter(Boolean);
  return { intro, lines };
}

export default function NamolotNotices({ alertsByReport }) {
  const [hidden, setHidden] = useState(() => new Set());
  const items = [];
  for (const [reportId, alerts] of Object.entries(alertsByReport || {})) {
    for (const alert of alerts || []) {
      const tone = TONES[alert.code];
      if (!tone) continue;
      items.push({ key: noticeKey(reportId, alert), reportId, alert, tone });
    }
  }
  const visible = items.filter((item) => !hidden.has(item.key));
  if (!visible.length) return null;

  function hide(key) {
    setHidden((prev) => {
      const next = new Set(prev);
      next.add(key);
      return next;
    });
  }

  return (
    <Box
      sx={{
        position: "fixed",
        top: 16,
        right: 16,
        zIndex: 1500,
        width: "min(520px, calc(100vw - 32px))",
        display: "flex",
        flexDirection: "column",
        gap: 1,
        pointerEvents: "none",
      }}
    >
      {visible.map((item) => {
        const plates = item.alert.code.startsWith("namolot_plate_") ? plateNotice(item.alert.message) : null;
        return (
        <Alert
          key={item.key}
          severity="error"
          variant="filled"
          onClose={() => hide(item.key)}
          sx={{
            pointerEvents: "auto",
            bgcolor: item.tone.bg,
            color: "#fff",
            alignItems: "flex-start",
            boxShadow: "0 8px 24px rgba(0,0,0,0.28)",
            "& .MuiAlert-icon": { color: "#fff" },
            "& .MuiAlert-action": { pt: 0, color: "#fff" },
          }}
        >
          <Typography fontWeight={700} sx={{ mb: 0.5 }}>
            {item.tone.title} · сводка {item.reportId}
          </Typography>
          {plates?.lines.length ? (
            <>
              {plates.intro ? (
                <Typography variant="body2" sx={{ mb: 0.75 }}>
                  {plates.intro}
                </Typography>
              ) : null}
              <Box component="ul" sx={{ m: 0, pl: 2.25, maxHeight: 280, overflow: "auto" }}>
                {plates.lines.map((line) => (
                  <Typography key={line} component="li" variant="body2" sx={{ fontWeight: 600 }}>
                    {line}
                  </Typography>
                ))}
              </Box>
            </>
          ) : (
            <Typography variant="body2" sx={{ whiteSpace: "pre-wrap", maxHeight: 220, overflow: "auto" }}>
              {item.alert.message}
            </Typography>
          )}
        </Alert>
        );
      })}
    </Box>
  );
}
