import { useMemo, useState } from "react";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Button from "@mui/material/Button";
import FormControl from "@mui/material/FormControl";
import InputLabel from "@mui/material/InputLabel";
import MenuItem from "@mui/material/MenuItem";
import Select from "@mui/material/Select";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import Report1LayoutEditor from "./Report1LayoutEditor";

export default function Report1LayoutEditorDialog({
  open,
  onClose,
  summaryDate,
  accessToken = "",
  generationBatchReportIds = [],
  catalog = [],
  initialReportId = "1"
}) {
  const [saving, setSaving] = useState(false);
  const [reportId, setReportId] = useState(initialReportId || "1");

  const reportOptions = useMemo(() => catalog, [catalog]);
  const selectedTitle =
    reportOptions.find((row) => row.id === reportId)?.title || `Сводка ${reportId}`;

  return (
    <Dialog
      open={open}
      onClose={saving ? undefined : onClose}
      fullWidth
      maxWidth="xl"
      TransitionProps={{
        onEnter: () => setReportId(initialReportId || "1")
      }}
      sx={{
        "& .MuiDialog-paper": {
          height: "min(920px, 94vh)",
          maxHeight: "94vh",
          display: "flex",
          flexDirection: "column"
        }
      }}
    >
      <DialogTitle sx={{ pb: 0.5 }}>
        <Stack
          direction={{ xs: "column", sm: "row" }}
          spacing={1.5}
          alignItems={{ sm: "center" }}
          justifyContent="space-between"
        >
          <Stack spacing={0.25} sx={{ minWidth: 0 }}>
            <Typography variant="h6" component="span">
              Порядок страниц сводки
            </Typography>
            <Typography variant="body2" color="text.secondary">
              Скрывайте, переставляйте и добавляйте страницы из других сводок
            </Typography>
          </Stack>
          <FormControl size="small" sx={{ minWidth: { xs: "100%", sm: 320 } }}>
            <InputLabel id="page-order-report-label">Сводка</InputLabel>
            <Select
              labelId="page-order-report-label"
              label="Сводка"
              value={reportId}
              disabled={saving}
              onChange={(event) => setReportId(String(event.target.value))}
            >
              {reportOptions.map((row) => (
                <MenuItem key={row.id} value={row.id}>
                  {row.title}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
        </Stack>
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
          Редактируется: {selectedTitle}
        </Typography>
      </DialogTitle>
      <DialogContent dividers sx={{ flex: 1, overflow: "hidden", minHeight: 0, p: 0 }}>
        {open ? (
          <Report1LayoutEditor
            key={reportId}
            reportId={reportId}
            summaryDate={summaryDate}
            accessToken={accessToken}
            generationBatchReportIds={generationBatchReportIds}
            onSavingChange={setSaving}
          />
        ) : null}
      </DialogContent>
      <DialogActions sx={{ px: 2, py: 1.5 }}>
        <Button onClick={onClose} disabled={saving}>
          Закрыть
        </Button>
      </DialogActions>
    </Dialog>
  );
}
