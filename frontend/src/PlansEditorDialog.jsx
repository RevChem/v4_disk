import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Button from "@mui/material/Button";
import PlansEditor from "./PlansEditor";

export default function PlansEditorDialog({ open, onClose, summaryDate = null }) {
  return (
    <Dialog
      open={open}
      onClose={onClose}
      fullWidth
      maxWidth={false}
      sx={{
        "& .MuiDialog-paper": {
          width: "min(1920px, 98vw)",
          height: "min(1080px, 96vh)",
          maxWidth: "98vw",
          maxHeight: "96vh",
          m: 1,
          display: "flex",
          flexDirection: "column"
        }
      }}
    >
      <DialogTitle sx={{ py: 1.25, px: 2 }}>Редактор планов (plans.py)</DialogTitle>
      <DialogContent dividers sx={{ flex: 1, overflow: "hidden", minHeight: 0, p: 0 }}>
        {open ? <PlansEditor summaryDate={summaryDate} /> : null}
      </DialogContent>
      <DialogActions sx={{ px: 2, py: 1 }}>
        <Button onClick={onClose}>Закрыть</Button>
      </DialogActions>
    </Dialog>
  );
}
