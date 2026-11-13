import { Box, Chip, Stack, Typography } from "@mui/material";

import PageModalDialog from "./PageModalDialog";

const REPORTS = [
  {
    id: "fuel",
    mark: "Топливо",
    title: "Отсутствие данных по топливу",
    description:
      "Задания машин за выбранный период. По каждому полю — километры в работе и за всё задание, общий расход в литрах и ссылка на карточку. Нулевой расход остаётся в строке, наёмники собраны отдельно.",
  },
  {
    id: "harvest",
    mark: "Урожай",
    title: "Валовый сбор",
    description:
      "Действующие поля сезона: учётные гектары, культура, сорт и сколько реально посеяно. Дальше даты сева и уборки, валовый сбор, урожай в тоннах и урожайность с карточки поля. Архивные поля не входят.",
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
            className="schedule-card schedule-card--green-outline"
            onClick={() => onOpen?.(item.id)}
          >
            <Stack spacing={1.25} sx={{ height: "100%", textAlign: "left" }}>
              <Chip size="small" variant="outlined" label={item.mark} sx={{ alignSelf: "flex-start" }} />
              <Box sx={{ flex: 1 }}>
                <Typography variant="subtitle2" fontWeight={700} lineHeight={1.3} gutterBottom>
                  {item.title}
                </Typography>
                <Typography
                  variant="caption"
                  color="text.secondary"
                  sx={{
                    display: "-webkit-box",
                    WebkitLineClamp: 4,
                    WebkitBoxOrient: "vertical",
                    overflow: "hidden",
                  }}
                >
                  {item.description}
                </Typography>
              </Box>
            </Stack>
          </Box>
        ))}
      </Box>
    </PageModalDialog>
  );
}
