export const THEME_IDS = ["dark", "light", "green", "blue", "amber", "rose", "teal", "violet"];

export const THEMES = [
  { id: "dark", label: "Тёмная" },
  { id: "light", label: "Светлая" },
  { id: "green", label: "Зелёная" },
  { id: "blue", label: "Синяя" },
  { id: "amber", label: "Янтарная" },
  { id: "rose", label: "Розовая" },
  { id: "teal", label: "Бирюзовая" },
  { id: "violet", label: "Фиолетовая" },
];

export const DEFAULT_THEME = "dark";

export function isValidTheme(value) {
  return THEME_IDS.includes(value);
}


/** CSS-переменная для фона (обходит конфликт с theme background в styles.css). */
export function customBackgroundSx(imageUrl) {
  if (!imageUrl) return {};
  return {
    "--disk-custom-bg-image": `linear-gradient(rgba(0,0,0,0.4), rgba(0,0,0,0.4)), url("${imageUrl}")`,
  };
}
