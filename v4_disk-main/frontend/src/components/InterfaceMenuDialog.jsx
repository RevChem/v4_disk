import { useRef, useState } from "react";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogContent,
  DialogTitle,
  Stack,
  ToggleButton,
  ToggleButtonGroup,
  Typography,
} from "@mui/material";
import PaletteOutlinedIcon from "@mui/icons-material/PaletteOutlined";
import ImageOutlinedIcon from "@mui/icons-material/ImageOutlined";

import { saveInterfaceSettings } from "../api";
import { THEMES } from "../constants/themes";

export default function InterfaceMenuDialog({
  open,
  onClose,
  theme,
  onThemeChange,
  customBackground,
  onSettingsSaved,
}) {
  const fileRef = useRef(null);
  const [bgError, setBgError] = useState("");
  const [saveError, setSaveError] = useState("");
  const [busy, setBusy] = useState(false);

  async function persistSettings({ nextTheme = theme, nextBackground = undefined }) {
    setSaveError("");
    setBusy(true);
    try {
      const saved = await saveInterfaceSettings({
        theme: nextTheme,
        customBackground: nextBackground,
      });
      onThemeChange?.(saved.theme);
      await onSettingsSaved?.(saved);
    } catch (err) {
      setSaveError(String(err.message || err));
    } finally {
      setBusy(false);
    }
  }

  async function handleThemeChange(_, value) {
    if (!value || value === theme) return;
    await persistSettings({ nextTheme: value });
  }

  function handleBackgroundFile(file) {
    if (!file) return;
    setBgError("");
    if (!file.type.startsWith("image/")) {
      setBgError("Выберите файл изображения (PNG, JPG, WebP).");
      return;
    }
    if (file.size > 4 * 1024 * 1024) {
      setBgError("Файл слишком большой (макс. 4 МБ).");
      return;
    }
    const reader = new FileReader();
    reader.onload = async () => {
      const dataUrl = String(reader.result || "");
      await persistSettings({ nextBackground: dataUrl });
    };
    reader.onerror = () => setBgError("Не удалось прочитать файл.");
    reader.readAsDataURL(file);
  }

  async function clearBackground() {
    setBgError("");
    await persistSettings({ nextBackground: "" });
  }

  return (
    <Dialog open={open} onClose={onClose} maxWidth="xs" fullWidth>
      <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1 }}>
        <PaletteOutlinedIcon color="primary" />
        Интерфейс
      </DialogTitle>
      <DialogContent dividers>
        <Stack spacing={2.5}>
          {saveError && (
            <Alert severity="error" sx={{ py: 0.5 }}>
              {saveError}
            </Alert>
          )}

          <Box>
            <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 1 }}>
              Тема оформления
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
              Сохраняется для вашей учётной записи на сервере.
            </Typography>
            <ToggleButtonGroup
              size="small"
              exclusive
              value={theme}
              onChange={handleThemeChange}
              disabled={busy}
              sx={{ flexWrap: "wrap", gap: 0.5 }}
            >
              {THEMES.map((t) => (
                <ToggleButton key={t.id} value={t.id} sx={{ px: 1.5 }}>
                  {t.label}
                </ToggleButton>
              ))}
            </ToggleButtonGroup>
          </Box>

          <Box>
            <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 1 }}>
              Собственный фон
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
              Личный фон — видите только вы после входа в систему.
            </Typography>
            <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap alignItems="center">
              <Button
                variant="outlined"
                size="small"
                startIcon={<ImageOutlinedIcon />}
                disabled={busy}
                onClick={() => fileRef.current?.click()}
              >
                Выбрать файл
              </Button>
              {customBackground && (
                <Button
                  variant="text"
                  size="small"
                  color="inherit"
                  disabled={busy}
                  onClick={clearBackground}
                >
                  Убрать фон
                </Button>
              )}
              {busy && <CircularProgress size={18} />}
            </Stack>
            <input
              ref={fileRef}
              type="file"
              hidden
              accept="image/png,image/jpeg,image/webp,image/gif"
              onChange={(e) => handleBackgroundFile(e.target.files?.[0])}
            />
            {bgError && (
              <Typography variant="caption" color="error.main" display="block" sx={{ mt: 1 }}>
                {bgError}
              </Typography>
            )}
            {customBackground && (
              <Box
                sx={{
                  mt: 1.5,
                  height: 96,
                  borderRadius: 2,
                  border: 1,
                  borderColor: "divider",
                  backgroundImage: `url(${customBackground})`,
                  backgroundSize: "cover",
                  backgroundPosition: "center",
                }}
              />
            )}
          </Box>
        </Stack>
      </DialogContent>
    </Dialog>
  );
}
