import { useEffect, useState } from "react";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Button from "@mui/material/Button";
import IconButton from "@mui/material/IconButton";
import Tooltip from "@mui/material/Tooltip";
import CloseFullscreenRoundedIcon from "@mui/icons-material/CloseFullscreenRounded";
import OpenInFullRoundedIcon from "@mui/icons-material/OpenInFullRounded";
import PlansEditor from "./PlansEditor";

function isComputerFullscreen() {
  return document.fullscreenElement === document.documentElement;
}

export default function PlansEditorDialog({ open, onClose, summaryDate = null }) {
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
      // Список «Считать» и другие меню — тоже на Escape. Сначала закрывается меню.
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
        // На весь документ, а не на бумагу диалога: меню MUI и календарь
        // рисуются в body и иначе оказываются вне полноэкранного элемента.
        await document.documentElement.requestFullscreen();
      }
    } catch {
      // Браузер может запретить полноэкранный режим без жеста пользователя.
    }
  }

  return (
    <Dialog
      open={open}
      onClose={onClose}
      fullScreen={fullScreen}
      fullWidth
      maxWidth={false}
      slotProps={{
        paper: {
          sx: fullScreen
            ? {
                display: "flex",
                flexDirection: "column",
                width: "100%",
                height: "100%",
                maxWidth: "100%",
                maxHeight: "100%",
                m: 0,
                borderRadius: 0
              }
            : {
                display: "flex",
                flexDirection: "column",
                width: "min(1920px, 98vw)",
                height: "min(1080px, 96vh)",
                maxWidth: "98vw",
                maxHeight: "96vh",
                m: 1
              }
        }
      }}
    >
      <DialogTitle sx={{ py: 1.25, px: 2, pr: 7, display: "flex", alignItems: "center" }}>
        Редактор планов (plans.py)
        <Tooltip title={fullScreen ? "Выйти из полноэкранного режима (Esc)" : "На весь экран компьютера"}>
          <IconButton
            aria-label={fullScreen ? "Выйти из полноэкранного режима" : "На весь экран компьютера"}
            onClick={toggleComputerFullscreen}
            sx={{ position: "absolute", right: 8, top: 6 }}
          >
            {fullScreen ? <CloseFullscreenRoundedIcon /> : <OpenInFullRoundedIcon />}
          </IconButton>
        </Tooltip>
      </DialogTitle>
      <DialogContent dividers sx={{ flex: 1, overflow: "hidden", minHeight: 0, p: 0 }}>
        {open ? <PlansEditor summaryDate={summaryDate} /> : null}
      </DialogContent>
      <DialogActions sx={{ px: 2, py: 1 }}>
        <Button onClick={onClose}>Закрыть</Button>
      </DialogActions>
    </Dialog>
  );
}
