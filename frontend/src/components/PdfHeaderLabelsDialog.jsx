import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Button from "@mui/material/Button";
import PdfHeaderLabelsEditor from "./PdfHeaderLabelsEditor";

export default function PdfHeaderLabelsDialog({ open, onClose }) {
  return (
    <Dialog
      open={open}
      onClose={onClose}
      fullWidth
      maxWidth="lg"
      sx={{
        "& .MuiDialog-paper": {
          width: "min(1200px, 96vw)",
          height: "min(820px, 92vh)",
          maxWidth: "96vw",
          maxHeight: "92vh",
          m: 1,
          display: "flex",
          flexDirection: "column",
        },
      }}
    >
      <DialogTitle sx={{ py: 1.25, px: 2 }}>Подписи PDF — «ВИД РАБОТ»</DialogTitle>
      <DialogContent dividers sx={{ flex: 1, overflow: "hidden", minHeight: 0, p: 0 }}>
        {open ? <PdfHeaderLabelsEditor /> : null}
      </DialogContent>
      <DialogActions sx={{ px: 2, py: 1 }}>
        <Button onClick={onClose}>Закрыть</Button>
      </DialogActions>
    </Dialog>
  );
}
