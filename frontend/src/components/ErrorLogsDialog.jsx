import {
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Stack,
  Typography,
  Button,
} from "@mui/material";
import BugReportRoundedIcon from "@mui/icons-material/BugReportRounded";

import ErrorLogsPanel from "./ErrorLogsPanel";

export default function ErrorLogsDialog({ open, onClose, proverkaLogs = [], generationLogs = [], failedItems = [] }) {
  const total =
    (proverkaLogs?.length || 0) +
    (generationLogs || []).filter((line) =>
      /error|ошибк|failed|exception|traceback|fatal|blank|UI error/i.test(String(line))
    ).length +
    (failedItems?.length || 0);

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="sm">
      <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1 }}>
        <BugReportRoundedIcon color={total > 0 ? "error" : "action"} />
        Логи ошибок
        {total > 0 && (
          <Typography component="span" variant="body2" color="error.main" sx={{ ml: 0.5 }}>
            ({total})
          </Typography>
        )}
      </DialogTitle>
      <DialogContent dividers>
        <Stack spacing={2}>
          <ErrorLogsPanel
            proverkaLogs={proverkaLogs}
            generationLogs={generationLogs}
            failedItems={failedItems}
          />
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Закрыть</Button>
      </DialogActions>
    </Dialog>
  );
}
