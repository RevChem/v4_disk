/** 932.4 → «15 мин 32 сек»; 45 → «45 сек»; 3725 → «1 ч 2 мин 5 сек». Пусто, если времени нет. */
export function formatDuration(seconds) {
  if (seconds == null || !Number.isFinite(Number(seconds)) || Number(seconds) < 0) return "";
  const total = Math.round(Number(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h} ч ${m} мин ${s} сек`;
  if (m > 0) return `${m} мин ${s} сек`;
  return `${s} сек`;
}

/** «24» + «24. Техника…» → «24. Техника…» (без двойного номера). */
export function reportLabel(reportId, title) {
  const id = String(reportId ?? "");
  const t = String(title ?? "").trim();
  if (!t) return id;
  const escaped = id.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  if (new RegExp(`^${escaped}(?!\\d)\\s*\\.?`).test(t)) return t;
  return `${id}. ${t}`;
}

/** ISO → «21.09.2026, 10:15»; пусто, если даты нет. */
export function formatDateTimeRu(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
