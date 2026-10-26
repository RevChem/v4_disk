import { useCallback, useEffect, useState } from "react";
import { Alert, Box, Button, Typography } from "@mui/material";
import { fetchFieldFirmAlerts, ignoreFieldFirmAlert } from "../api";

const POLL_MS = 5 * 60 * 1000;

function fmtDay(iso) {
  if (!iso) return "";
  const [y, m, d] = String(iso).slice(0, 10).split("-");
  return `${d}.${m}.${y}`;
}

function period(item) {
  const a = fmtDay(item.first_at);
  const b = fmtDay(item.last_at);
  if (!a) return "";
  return a === b ? a : `${a}–${b}`;
}

/**
 * Белое окно: пометка фирмы в названии поля Namolot («(АФ3)») не совпадает с папкой Cropwise.
 * Проверка идёт на сервере раз в 3 часа. Ключ окна включает run_id прогона, поэтому после
 * закрытия крестиком окно появляется снова, пока ошибка не исправлена.
 */
export default function FieldFirmNotices({ user }) {
  const [state, setState] = useState(null);
  const [hidden, setHidden] = useState(() => new Set());
  const isAdmin = user?.role === "admin";

  const load = useCallback(async () => {
    try {
      setState(await fetchFieldFirmAlerts());
    } catch {
      /* нет доступа или сеть: окно просто не показываем */
    }
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

  const items = state?.items || [];
  const runId = state?.run_id || "";
  const key = `${runId}|${items.map((i) => i.key).join(",")}`;
  if (!items.length || hidden.has(key)) return null;

  async function notAnError(item) {
    try {
      setState(await ignoreFieldFirmAlert(item.key, "помечено на сайте"));
    } catch {
      /* оставляем окно */
    }
  }

  return (
    <Box
      sx={{
        position: "fixed",
        bottom: 16,
        right: 16,
        zIndex: 1500,
        width: "min(560px, calc(100vw - 32px))",
        pointerEvents: "none",
      }}
    >
      <Alert
        severity="warning"
        variant="outlined"
        onClose={() => setHidden((prev) => new Set(prev).add(key))}
        sx={{
          pointerEvents: "auto",
          bgcolor: "#fff",
          color: "#1b1b1b",
          alignItems: "flex-start",
          borderColor: "#e0e0e0",
          borderLeft: "6px solid #e65100",
          boxShadow: "0 8px 24px rgba(0,0,0,0.28)",
          "& .MuiAlert-icon": { color: "#e65100" },
        }}
      >
        <Typography fontWeight={700} sx={{ mb: 0.5 }}>
          Поле числится за другой агрофирмой
        </Typography>
        <Typography variant="body2" sx={{ mb: 0.75 }}>
          Пометка в названии поля Namolot расходится с папкой Cropwise. Сводка 10 и кнопка Namolot
          отнесут эти тонны не той фирме. Исправьте папку в Cropwise или внесите передачу поля
          в FIELD_FIRM_TRANSFERS (plans.py).
        </Typography>
        <Box component="ul" sx={{ m: 0, pl: 2.25, maxHeight: 260, overflow: "auto" }}>
          {items.map((item) => (
            <Typography key={item.key} component="li" variant="body2" sx={{ mb: 0.5 }}>
              <b>{item.code}</b>: по пометке — {item.tag_firm}, в Cropwise — {item.cropwise_firm}.
              Затронуто {item.tonnes.toLocaleString("ru-RU")} т ({item.records} зап.
              {period(item) ? `, ${period(item)}` : ""}).
              {isAdmin ? (
                <Button size="small" sx={{ ml: 1, py: 0, minWidth: 0 }} onClick={() => notAnError(item)}>
                  Это не ошибка
                </Button>
              ) : null}
            </Typography>
          ))}
        </Box>
        <Typography variant="caption" sx={{ display: "block", mt: 0.75, color: "#616161" }}>
          Проверено: {state?.checked_at ? state.checked_at.replace("T", " ").slice(0, 16) : "—"}.
          Проверка повторяется каждые 3 часа.
        </Typography>
      </Alert>
    </Box>
  );
}
