import {
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Stack,
  TextField,
  Typography,
} from "@mui/material";

import { HARVEST_GROSS_FIRMS, sumHarvestGrossCrop } from "../api";

function formatHarvestSum(n) {
  if (n === null || n === undefined) return "—";
  const rounded = Math.round(n * 100) / 100;
  return String(rounded);
}

export default function HarvestGrossDialog({
  open,
  onClose,
  form,
  onFieldChange,
  generating,
  onSubmit,
}) {
  const crop = "Кукуруза на силос";
  const seasonSum = sumHarvestGrossCrop(form, "crops", crop, "season_t");
  const daySum = sumHarvestGrossCrop(form, "crops", crop, "day_t");

  return (
    <Dialog
      open={open}
      onClose={() => {
        if (!generating) onClose?.();
      }}
      fullWidth
      maxWidth="md"
    >
      <DialogTitle>Валовый сбор · кукуруза на силос</DialogTitle>
      <DialogContent>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Перед выгрузкой сводок 1 / 10 / 13 укажите валовый сбор (т) только по кукурузе на силос
          (сезон / сутки по агрофирмам). В ячейках остаются значения с прошлого формирования — их
          можно поправить, если за сутки что-то изменилось. Пустые ячейки не перезаписывают данные.
          Площади — из Cropwise.
        </Typography>
        <Box>
          <Stack
            direction={{ xs: "column", sm: "row" }}
            spacing={0.5}
            sx={{ mb: 1 }}
            justifyContent="space-between"
          >
            <Typography fontWeight={700}>{crop}</Typography>
            <Typography variant="body2" color="text.secondary">
              Итого: сезон {formatHarvestSum(seasonSum)} т · сутки {formatHarvestSum(daySum)} т
            </Typography>
          </Stack>
          <Box component="table" sx={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr>
                <Box component="th" sx={{ textAlign: "left", pb: 0.5 }}>
                  Агрофирма
                </Box>
                <Box component="th" sx={{ textAlign: "left", pb: 0.5, width: 140 }}>
                  Сезон, т
                </Box>
                <Box component="th" sx={{ textAlign: "left", pb: 0.5, width: 140 }}>
                  Сутки, т
                </Box>
              </tr>
            </thead>
            <tbody>
              {HARVEST_GROSS_FIRMS.map((firm) => (
                <tr key={firm}>
                  <Box component="td" sx={{ py: 0.5, pr: 1 }}>
                    {firm}
                  </Box>
                  <Box component="td" sx={{ py: 0.5, pr: 1 }}>
                    <TextField
                      size="small"
                      fullWidth
                      value={form?.crops?.[crop]?.[firm]?.season_t ?? ""}
                      onChange={(e) => onFieldChange?.("crops", crop, firm, "season_t", e.target.value)}
                      disabled={generating}
                    />
                  </Box>
                  <Box component="td" sx={{ py: 0.5 }}>
                    <TextField
                      size="small"
                      fullWidth
                      value={form?.crops?.[crop]?.[firm]?.day_t ?? ""}
                      onChange={(e) => onFieldChange?.("crops", crop, firm, "day_t", e.target.value)}
                      disabled={generating}
                    />
                  </Box>
                </tr>
              ))}
              <tr>
                <Box component="td" sx={{ py: 0.75, pr: 1, fontWeight: 700 }}>
                  Итого (холдинг)
                </Box>
                <Box component="td" sx={{ py: 0.75, pr: 1, fontWeight: 700 }}>
                  {formatHarvestSum(seasonSum)}
                </Box>
                <Box component="td" sx={{ py: 0.75, fontWeight: 700 }}>
                  {formatHarvestSum(daySum)}
                </Box>
              </tr>
            </tbody>
          </Box>
        </Box>
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2 }}>
        <Button onClick={onClose} disabled={generating}>
          Отмена
        </Button>
        <Button variant="contained" onClick={onSubmit} disabled={generating}>
          {generating ? "Постановка…" : "Сформировать"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
