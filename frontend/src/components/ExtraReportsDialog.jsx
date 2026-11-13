import { Box, Typography } from "@mui/material";

import PageModalDialog from "./PageModalDialog";

const REPORTS = [
  {
    id: "fuel",
    title: "Отсутствие данных по топливу",
    description:
      "Задания машин за любой период: путь, расход топлива и ссылка на задание. Наёмники вынесены на отдельный лист.",
  },
  {
    id: "harvest",
    title: "Валовый сбор",
    description:
      "По действующим полям сезона: культура, сорт, валовый сбор, урожайность, даты сева и уборки.",
  },
];

export default function ExtraReportsDialog({ open, onClose, onOpen }) {
  return (
    <PageModalDialog open={open} onClose={onClose} title="Доп. отчеты">
      <Box className="schedule-grid">
        {REPORTS.map((item) => (
          <Box
            key={item.id}
            component="button"
            type="button"
            className="schedule-card"
            onClick={() => onOpen?.(item.id)}
          >
            <Typography variant="subtitle2" fontWeight={700} lineHeight={1.3} gutterBottom>
              {item.title}
            </Typography>
            <Typography
              variant="caption"
              color="text.secondary"
              sx={{
                display: "-webkit-box",
                WebkitLineClamp: 3,
                WebkitBoxOrient: "vertical",
                overflow: "hidden",
              }}
            >
              {item.description}
            </Typography>
          </Box>
        ))}
      </Box>
    </PageModalDialog>
  );
}
