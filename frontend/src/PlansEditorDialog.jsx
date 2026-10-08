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

export default function PlansEditorDialog({ open, onClose, summaryDate = null }) {
  const [fullScreen, setFullScreen] = useState(false);

  useEffect(() => {
    if (!open) setFullScreen(false);
  }, [open]);

  useEffect(() => {
    if (!open || !fullScreen) return undefined;
    const onKey = (event) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      setFullScreen(false);
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [open, fullScreen]);

  return (
    <Dialog
      open={open}
      onClose={onClose}
      fullScreen={fullScreen}
      fullWidth
      maxWidth={false}
      disableEscapeKeyDown={fullScreen}
      sx={{
        "& .MuiDialog-paper": {
          display: "flex",
          flexDirection: "column",
          ...(fullScreen
            ? { width: "100%", height: "100%", maxWidth: "100%", maxHeight: "100%", m: 0 }
            : {
                width: "min(1920px, 98vw)",
                height: "min(1080px, 96vh)",
                maxWidth: "98vw",
                maxHeight: "96vh",
                m: 1
              })
        }
      }}
    >
      <DialogTitle sx={{ py: 1.25, px: 2, pr: 7, display: "flex", alignItems: "center" }}>
        Редактор планов (plans.py)
        <Tooltip title={fullScreen ? "Свернуть (Esc)" : "На весь экран"}>
          <IconButton
            aria-label={fullScreen ? "Выйти из полноэкранного режима" : "На весь экран"}
            onClick={() => setFullScreen((value) => !value)}
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
