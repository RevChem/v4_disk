import { Box, Typography } from "@mui/material";

import PageModalDialog from "./PageModalDialog";

const REPORTS = [
  {
    id: "fuel",
    tone: "amber",
    mark: "Топливо",
    title: "Отсутствие данных по топливу",
    description:
      "Задания машин за выбранный период. По каждому полю — километры в работе и за всё задание, общий расход в литрах и ссылка на карточку. Нулевой расход остаётся в строке, наёмники собраны отдельно.",
  },
  {
    id: "harvest",
    tone: "green",
    mark: "Урожай",
    title: "Валовый сбор",
    description:
      "Действующие поля сезона: учётные гектары, культура, сорт и сколько реально посеяно. Дальше даты сева и уборки, валовый сбор, урожай в тоннах и урожайность с карточки поля. Архивные поля не входят.",
  },
];

export default function ExtraReportsDialog({ open, onClose, onOpen }) {
  return (
    <PageModalDialog open={open} onClose={onClose} title="Доп. отчеты">
      <Box className="extra-reports-grid">
        {REPORTS.map((item) => (
          <Box
            key={item.id}
            component="button"
            type="button"
            className={`extra-report-card extra-report-card--${item.tone}`}
            onClick={() => onOpen?.(item.id)}
          >
            <span className="extra-report-card__mark">{item.mark}</span>
            <Typography variant="subtitle1" fontWeight={700} lineHeight={1.3}>
              {item.title}
            </Typography>
            <Typography variant="body2" className="extra-report-card__text">
              {item.description}
            </Typography>
          </Box>
        ))}
      </Box>
    </PageModalDialog>
  );
}
