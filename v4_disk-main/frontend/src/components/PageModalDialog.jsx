import {
  Dialog,
  DialogContent,
  DialogTitle,
  IconButton,
  Stack,
  Typography,
} from "@mui/material";
import CloseRoundedIcon from "@mui/icons-material/CloseRounded";

export default function PageModalDialog({ open, onClose, title, children, maxWidth = "xl" }) {
  return (
    <Dialog
      open={open}
      onClose={onClose}
      fullWidth
      maxWidth={maxWidth}
      scroll="paper"
      PaperProps={{ sx: { maxHeight: "92vh" } }}
    >
      <DialogTitle sx={{ pr: 6, py: 1.5 }}>
        <Stack direction="row" alignItems="center" justifyContent="space-between">
          <Typography variant="h6" fontWeight={700}>
            {title}
          </Typography>
          <IconButton aria-label="Закрыть" onClick={onClose} sx={{ position: "absolute", right: 8, top: 8 }}>
            <CloseRoundedIcon />
          </IconButton>
        </Stack>
      </DialogTitle>
      <DialogContent dividers sx={{ p: { xs: 1.5, sm: 2 } }}>
        {children}
      </DialogContent>
    </Dialog>
  );
}
