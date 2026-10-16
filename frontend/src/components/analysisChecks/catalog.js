import PeopleAltOutlinedIcon from "@mui/icons-material/PeopleAltOutlined";
import PrecisionManufacturingOutlinedIcon from "@mui/icons-material/PrecisionManufacturingOutlined";
import GrassOutlinedIcon from "@mui/icons-material/GrassOutlined";
import CorporateFareOutlinedIcon from "@mui/icons-material/CorporateFareOutlined";
import FactCheckOutlinedIcon from "@mui/icons-material/FactCheckOutlined";
import EmojiEventsOutlinedIcon from "@mui/icons-material/EmojiEventsOutlined";
import TrendingDownOutlinedIcon from "@mui/icons-material/TrendingDownOutlined";
import EventBusyOutlinedIcon from "@mui/icons-material/EventBusyOutlined";
import AgricultureOutlinedIcon from "@mui/icons-material/AgricultureOutlined";
import BuildCircleOutlinedIcon from "@mui/icons-material/BuildCircleOutlined";
import LocalGasStationOutlinedIcon from "@mui/icons-material/LocalGasStationOutlined";
import SpeedOutlinedIcon from "@mui/icons-material/SpeedOutlined";
import AssessmentOutlinedIcon from "@mui/icons-material/AssessmentOutlined";
import WarehouseOutlinedIcon from "@mui/icons-material/WarehouseOutlined";
import HistoryOutlinedIcon from "@mui/icons-material/HistoryOutlined";
import FlagOutlinedIcon from "@mui/icons-material/FlagOutlined";
import LandscapeOutlinedIcon from "@mui/icons-material/LandscapeOutlined";
import LayersOutlinedIcon from "@mui/icons-material/LayersOutlined";
import CompareArrowsOutlinedIcon from "@mui/icons-material/CompareArrowsOutlined";
import AccountTreeOutlinedIcon from "@mui/icons-material/AccountTreeOutlined";
import PersonOffOutlinedIcon from "@mui/icons-material/PersonOffOutlined";
import LabelOffOutlinedIcon from "@mui/icons-material/LabelOffOutlined";
import AccessTimeOutlinedIcon from "@mui/icons-material/AccessTimeOutlined";
import GppMaybeOutlinedIcon from "@mui/icons-material/GppMaybeOutlined";

/**
 * 19 срезов по store.sqlite. Папки — только группировка внутри раздела «Проверки».
 *
 * filters: period есть всегда.
 *   group: "optional" | "recommended"; machine / field — выбор из справочника;
 *   threshold: {label, default, step, min, toParam}.
 * since: "season" — период по умолчанию с 1 января, иначе последние 30 дней.
 */
export const CHECK_FOLDERS = [
  {
    id: "people",
    title: "Люди",
    tagline: "Кто и сколько сделал",
    icon: PeopleAltOutlinedIcon,
    color: "primary",
    items: [
      {
        id: "people_rating",
        icon: EmojiEventsOutlinedIcon,
        title: "Рейтинг исполнителей",
        desc: "Топ и аутсайдеры по выработке: гектары, смены, выработка за смену, основная операция.",
        filters: { group: "recommended" },
      },
      {
        id: "people_low",
        icon: TrendingDownOutlinedIcon,
        title: "Малая выработка",
        desc: "Смены, где человек отработал меньше порога. Видно, кто, на какой машине и когда.",
        filters: { group: "optional", threshold: { label: "Порог, га за смену", default: 10, step: 1, min: 1 } },
      },
      {
        id: "people_load",
        icon: EventBusyOutlinedIcon,
        title: "Нагрузка и пустые смены",
        desc: "Смены подряд без выходных, пропуски внутри периода, разброс выработки.",
        filters: { group: "optional" },
      },
    ],
  },
  {
    id: "machines",
    title: "Техника",
    tagline: "Как работает парк",
    icon: PrecisionManufacturingOutlinedIcon,
    color: "secondary",
    items: [
      {
        id: "machine_work",
        icon: AgricultureOutlinedIcon,
        title: "Работа машины",
        desc: "Одна машина: смены, поля, культуры, исполнители и гектары по дням.",
        filters: { group: "optional", machine: true },
      },
      {
        id: "machine_idle",
        icon: BuildCircleOutlinedIcon,
        title: "Простои техники",
        desc: "Дни без работы между сменами и, если есть телеметрия, часы остановок в поле и на дороге.",
        filters: { group: "recommended" },
      },
      {
        id: "fuel",
        icon: LocalGasStationOutlinedIcon,
        title: "Литры на гектар",
        desc: "Сравнение расхода по машине и операции. Подробные аномалии поля и дороги — в разделе «Топливо».",
        filters: { group: "recommended" },
      },
      {
        id: "norm_vs_fact",
        icon: AssessmentOutlinedIcon,
        title: "Факт против нормы",
        desc: "Фактические га/ч против нормы выработки. Кто систематически не дотягивает.",
        filters: { group: "recommended" },
      },
      {
        id: "fleet_load",
        icon: WarehouseOutlinedIcon,
        title: "Загрузка парка",
        desc: "Сколько машин и людей работало в каждый день и сколько гектаров пришлось на машину.",
        filters: { group: "optional" },
      },
      {
        id: "speed",
        icon: SpeedOutlinedIcon,
        title: "Средняя скорость",
        desc: "Скорость выше или ниже типичной для операции: помогает находить нарушения технологии.",
        filters: { group: "recommended" },
      },
    ],
  },
  {
    id: "fields",
    title: "Поля",
    tagline: "Что и где сделано",
    icon: GrassOutlinedIcon,
    color: "success",
    items: [
      {
        id: "field_history",
        icon: HistoryOutlinedIcon,
        title: "История поля",
        desc: "Все операции на одном поле за сезон по порядку: что, когда, кем и сколько гектаров.",
        since: "season",
        filters: { group: "optional", field: true },
      },
      {
        id: "plan_progress",
        icon: FlagOutlinedIcon,
        title: "Прогресс против плана",
        desc: "Факт к плану по операции, фирме и культуре, остаток и прогноз окончания по текущему темпу.",
        since: "season",
        filters: { group: "optional" },
      },
      {
        id: "fields_idle",
        icon: LandscapeOutlinedIcon,
        title: "Поля без работ",
        desc: "Поля, где в периоде не было ни одной операции, и когда там работали в последний раз.",
        filters: { group: "optional" },
      },
      {
        id: "repeat_passes",
        icon: LayersOutlinedIcon,
        title: "Повторные проходы",
        desc: "Операции, где обработано больше площади поля: дубли заданий и перекрытия.",
        since: "season",
        filters: {
          group: "recommended",
          threshold: { label: "Порог, % площади поля", default: 115, step: 5, min: 100, toParam: (v) => v / 100 },
        },
      },
    ],
  },
  {
    id: "firms",
    title: "Агрофирмы",
    tagline: "Сравнение и итоги",
    icon: CorporateFareOutlinedIcon,
    color: "warning",
    items: [
      {
        id: "firm_compare",
        icon: CompareArrowsOutlinedIcon,
        title: "Сравнение агрофирм",
        desc: "Одна операция по всем фирмам: гектары, поля, машины, люди и выработка на машино-смену.",
        filters: { group: "recommended" },
      },
      {
        id: "holding_totals",
        icon: AccountTreeOutlinedIcon,
        title: "Итоги по холдингу",
        desc: "Дерево: фирма, культура, операция. Раскрывайте уровни и смотрите доли.",
        filters: { group: "optional" },
      },
    ],
  },
  {
    id: "quality",
    title: "Качество данных",
    tagline: "Где цифры могут врать",
    icon: FactCheckOutlinedIcon,
    color: "error",
    items: [
      {
        id: "q_no_driver",
        icon: PersonOffOutlinedIcon,
        title: "Задания без исполнителя",
        desc: "Гектары есть, человека нет: они не попадают в рейтинги и в «Работу исполнителя».",
        filters: { group: "optional" },
      },
      {
        id: "q_no_crop_agro",
        icon: LabelOffOutlinedIcon,
        title: "Без культуры или агрооперации",
        desc: "Строки площади, у которых не нашлась культура, и задания без привязанной агрооперации.",
        filters: { group: "optional" },
      },
      {
        id: "q_no_hourly",
        icon: AccessTimeOutlinedIcon,
        title: "Га без почасовой разбивки",
        desc: "Где нет почасовой телеметрии и площадь относится ко дню окончания задания.",
        filters: { group: "optional" },
      },
      {
        id: "q_suspicious",
        icon: GppMaybeOutlinedIcon,
        title: "Подозрительные задания",
        desc: "Нереально высокая выработка за смену и возможные дубли заданий.",
        filters: { group: "optional", threshold: { label: "Множитель к медиане", default: 3, step: 0.5, min: 1.5 } },
      },
    ],
  },
];

export const CHECK_ITEMS = CHECK_FOLDERS.flatMap((folder) =>
  folder.items.map((item) => ({ ...item, folderId: folder.id })),
);

export function findCheck(id) {
  return CHECK_ITEMS.find((item) => item.id === id) || null;
}
