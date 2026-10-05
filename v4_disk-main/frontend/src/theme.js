import { createTheme } from "@mui/material/styles";

const VARIANTS = {
  dark: {
    isLight: false,
    primaryMain: "#818cf8",
    primaryDark: "#6366f1",
    primaryLight: "#a5b4fc",
    secondaryMain: "#a78bfa",
    bgDefault: "#0f1117",
    bgPaper: "#181a22",
    textPrimary: "#f1f0ff",
    textSecondary: "#c7c3e8",
    divider: "rgba(129, 140, 248, 0.28)",
    hover: "rgba(129, 140, 248, 0.12)",
    scrollbar: "#3730a3 #0f1117",
    cardBorder: "rgba(129, 140, 248, 0.2)",
    dialogBorder: "rgba(129, 140, 248, 0.3)",
  },
  light: {
    isLight: true,
    primaryMain: "#2563eb",
    primaryDark: "#1d4ed8",
    primaryLight: "#60a5fa",
    secondaryMain: "#64748b",
    bgDefault: "#f1f5f9",
    bgPaper: "#ffffff",
    textPrimary: "#0f172a",
    textSecondary: "#475569",
    divider: "rgba(15, 23, 42, 0.12)",
    hover: "rgba(37, 99, 235, 0.08)",
    scrollbar: "#cbd5e1 #f1f5f9",
    cardBorder: "rgba(15, 23, 42, 0.08)",
    dialogBorder: "rgba(15, 23, 42, 0.1)",
  },
  green: {
    isLight: false,
    primaryMain: "#10b981",
    primaryDark: "#059669",
    primaryLight: "#34d399",
    secondaryMain: "#2dd4bf",
    bgDefault: "#0c1210",
    bgPaper: "#141c19",
    textPrimary: "#ecfdf5",
    textSecondary: "#a7f3d0",
    divider: "rgba(16, 185, 129, 0.35)",
    hover: "rgba(16, 185, 129, 0.12)",
    scrollbar: "#14532d #0c1210",
    cardBorder: "rgba(16, 185, 129, 0.22)",
    dialogBorder: "rgba(16, 185, 129, 0.35)",
  },
  blue: {
    isLight: false,
    primaryMain: "#38bdf8",
    primaryDark: "#0ea5e9",
    primaryLight: "#7dd3fc",
    secondaryMain: "#60a5fa",
    bgDefault: "#0b1220",
    bgPaper: "#111827",
    textPrimary: "#e0f2fe",
    textSecondary: "#94a3b8",
    divider: "rgba(56, 189, 248, 0.28)",
    hover: "rgba(56, 189, 248, 0.12)",
    scrollbar: "#0c4a6e #0b1220",
    cardBorder: "rgba(56, 189, 248, 0.22)",
    dialogBorder: "rgba(56, 189, 248, 0.32)",
  },
  amber: {
    isLight: false,
    primaryMain: "#fbbf24",
    primaryDark: "#f59e0b",
    primaryLight: "#fcd34d",
    secondaryMain: "#fb923c",
    bgDefault: "#14110a",
    bgPaper: "#1c1810",
    textPrimary: "#fef3c7",
    textSecondary: "#d6d3d1",
    divider: "rgba(251, 191, 36, 0.28)",
    hover: "rgba(251, 191, 36, 0.12)",
    scrollbar: "#78350f #14110a",
    cardBorder: "rgba(251, 191, 36, 0.22)",
    dialogBorder: "rgba(251, 191, 36, 0.32)",
  },
  rose: {
    isLight: false,
    primaryMain: "#fb7185",
    primaryDark: "#f43f5e",
    primaryLight: "#fda4af",
    secondaryMain: "#f472b6",
    bgDefault: "#140c10",
    bgPaper: "#1a1116",
    textPrimary: "#ffe4e6",
    textSecondary: "#cbd5e1",
    divider: "rgba(251, 113, 133, 0.28)",
    hover: "rgba(251, 113, 133, 0.12)",
    scrollbar: "#881337 #140c10",
    cardBorder: "rgba(251, 113, 133, 0.22)",
    dialogBorder: "rgba(251, 113, 133, 0.32)",
  },
  teal: {
    isLight: false,
    primaryMain: "#2dd4bf",
    primaryDark: "#14b8a6",
    primaryLight: "#5eead4",
    secondaryMain: "#22d3ee",
    bgDefault: "#0a1214",
    bgPaper: "#111b1e",
    textPrimary: "#ecfeff",
    textSecondary: "#99f6e4",
    divider: "rgba(45, 212, 191, 0.28)",
    hover: "rgba(45, 212, 191, 0.12)",
    scrollbar: "#115e59 #0a1214",
    cardBorder: "rgba(45, 212, 191, 0.22)",
    dialogBorder: "rgba(45, 212, 191, 0.32)",
  },
  violet: {
    isLight: false,
    primaryMain: "#a78bfa",
    primaryDark: "#8b5cf6",
    primaryLight: "#c4b5fd",
    secondaryMain: "#e879f9",
    bgDefault: "#110c18",
    bgPaper: "#181022",
    textPrimary: "#f5f3ff",
    textSecondary: "#ddd6fe",
    divider: "rgba(167, 139, 250, 0.28)",
    hover: "rgba(167, 139, 250, 0.12)",
    scrollbar: "#5b21b6 #110c18",
    cardBorder: "rgba(167, 139, 250, 0.22)",
    dialogBorder: "rgba(167, 139, 250, 0.32)",
  },
};

export function createAppTheme(variant) {
  const cfg = VARIANTS[variant] || VARIANTS.dark;
  const isLight = cfg.isLight;

  return createTheme({
    palette: {
      mode: isLight ? "light" : "dark",
      primary: {
        main: cfg.primaryMain,
        dark: cfg.primaryDark,
        light: cfg.primaryLight,
        contrastText: "#ffffff",
      },
      secondary: { main: cfg.secondaryMain },
      background: {
        default: cfg.bgDefault,
        paper: cfg.bgPaper,
      },
      text: {
        primary: cfg.textPrimary,
        secondary: cfg.textSecondary,
      },
      divider: cfg.divider,
      action: { hover: cfg.hover },
    },
    shape: { borderRadius: 12 },
    typography: {
      fontFamily: '"Inter", "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
      h4: { fontWeight: 700, letterSpacing: "-0.02em" },
      h5: { fontWeight: 700 },
      h6: { fontWeight: 600 },
      button: { fontWeight: 600, textTransform: "none" },
    },
    components: {
      MuiCssBaseline: {
        styleOverrides: {
          body: {
            scrollbarColor: cfg.scrollbar,
            color: cfg.textPrimary,
            backgroundColor: cfg.bgDefault,
          },
        },
      },
      MuiPaper: { styleOverrides: { root: { backgroundImage: "none" } } },
      MuiCard: {
        styleOverrides: {
          root: {
            borderRadius: 16,
            border: `1px solid ${cfg.cardBorder}`,
            boxShadow: isLight
              ? "0 4px 24px rgba(15, 23, 42, 0.08)"
              : "0 8px 32px rgba(0, 0, 0, 0.35)",
          },
        },
      },
      MuiDialog: {
        styleOverrides: {
          paper: {
            borderRadius: 16,
            border: `1px solid ${cfg.dialogBorder}`,
            boxShadow: isLight ? "0 24px 48px rgba(15, 23, 42, 0.15)" : "0 24px 56px rgba(0, 0, 0, 0.55)",
          },
        },
      },
      MuiDialogTitle: {
        styleOverrides: {
          root: ({ theme }) => ({
            fontWeight: 700,
            borderBottom: `1px solid ${theme.palette.divider}`,
            paddingTop: theme.spacing(2.5),
            paddingBottom: theme.spacing(2),
          }),
        },
      },
      MuiButton: {
        defaultProps: { disableElevation: false },
        styleOverrides: {
          root: { borderRadius: 10 },
          containedPrimary: {
            boxShadow: "none",
            "&:hover": { boxShadow: "0 6px 20px rgba(0,0,0,0.22)" },
          },
        },
      },
      MuiChip: {
        styleOverrides: {
          root: { fontWeight: 600 },
          colorPrimary: { border: "1px solid", borderColor: "rgba(255,255,255,0.25)" },
        },
      },
      MuiTextField: { defaultProps: { variant: "outlined", size: "small" } },
      MuiAlert: { styleOverrides: { root: { borderRadius: 12 } } },
    },
  });
}
