# proverka — сверка сводок с Cropwise UI

Сравнивает **area_today** из утренних сводок с отчётом Cropwise Operations  
[«Отчёт об агро работах машин»](https://operations.cropwise.com/machines/reports/work).

## Настройка

1. **API-токен** — `CROPWISE_API_TOKEN` в `.env` (корень проекта).
2. **Cookie браузера** — `CROPWISE_SESSION_COOKIE` в `.env` или в начале `reconcile.py`.  
   Скопируйте **целую строку Cookie** из DevTools → Network → Headers (не только Value).

## Запуск

```bash
cd svodki/plan
python3 proverka/reconcile.py --report 5
python3 proverka/reconcile.py --report 4 --report 7
python3 proverka/reconcile.py --all                    # все сводки + таблица
python3 proverka/reconcile.py --all --excel            # + Excel-файл
REPORT_DATE=2026-09-11 python3 proverka/reconcile.py --all --excel
```

Docker:

```bash
docker exec -e REPORT_DATE=2026-09-11 v3_disk-api-1 \
  python3 /app/svodki/plan/proverka/reconcile.py --all
```

### Сводная таблица (все сводки vs Cropwise)

После `--all` создаётся `reconcile_all_{дата}.json`. Таблица:

```bash
python3 proverka/summary.py --date 2026-09-11
python3 proverka/summary.py --date 2026-09-11 --firms    # по агрофирмам
python3 proverka/summary.py --date 2026-09-11 --csv svodka_vs_cropwise.csv
```

Docker:

```bash
docker exec v3_disk-api-1 \
  python3 /app/svodki/plan/proverka/summary.py --date 2026-09-11 --firms
```

### Excel (расхождения + детализация)

Листы: **Итоги**, **Фирмы**, **Сводки между собой**, **Детали сводок** (машины),
**Детали Cropwise** (путь/задание из [work.json](https://operations.cropwise.com/machines/reports/work)),
**Расхождения** (фирмы с diff + детализация).

```bash
# вместе с прогоном
docker exec -e REPORT_DATE=2026-09-11 v3_disk-api-1 \
  python3 /app/svodki/plan/proverka/reconcile.py --all --excel

# из готового JSON
docker exec v3_disk-api-1 \
  python3 /app/svodki/plan/proverka/export_excel.py --date 2026-09-11
```

Файл: `proverka_{дата}.xlsx` в корне проекта (`/app/` в Docker).

### Кнопка на сайте сводок

На главной странице («Сводки») → **Проверка сводок** (для developer / sitcentr / admin):

1. Введите **cookie** (для режима Cropwise)
2. Выберите режим: **Сводки ↔ Cropwise** или **только сводки между собой**
3. Отметьте сводки → **Запустить проверку** → **Скачать Excel**

Колонки: **Сводка** (PDF/Excel логика) | **Cropwise** (UI work.json) | **Diff** | статус.

Сравнение **«между собой»** — по **операциям** (work group): для «4.0 Посев» сводки **1, 2 и 5**
должны дать один и тот же факт за смену; для «1.1 МУ» — **1, 2 и 3**, и т.д.

| Операция | Сводки для сверки |
|----------|-------------------|
| 1.1 МУ | 1, 2, 3 |
| 2.1 Боронование | 1, 2, 4 |
| 4.0 Посев | 1, 2, 5 |
| 5.0 Уборка (га) | 1, 2, 10, 11, 13 |
| 5.0 Уборка (машины) | 1, 2, 11, 14 |
| … | … |

Сверка берёт **локальный факт каждой сводки** (как в её Excel), а не подставляет
эталон из тематической. Для 5.0: сводка 10 — «Факт за сутки» из «Общий намолот»,
11 — grain_area_today, 13 — «Убрано за сутки» на листе «Зерновые культуры».

Сводки **1** и **2** не сравниваются с Cropwise UI (это зонтики), только друг с другом и с 3–13.
Сводка **1** в proverka считается в режиме `REPORT1_STANDALONE=1` (все группы из API).

## Сводки и сезоны

Факт (`area_today`) считается **модулем сводки** — с теми же правилами, что PDF/Excel.

| ID | Работа | machine_task.season | history_items (поля) |
|----|--------|---------------------|----------------------|
| 3 | Минеральные удобрения | **2026+2027** (`machine_task.season`) | 2026+2027 (`history_items`) |
| 4 | Боронование | все | 2026 |
| 5 | **только 2027** | 2026+2027, карта посева — 2027 |
| 6 | Опрыскивание | все | 2026 |
| 7 | Дискование | все | 2026+2027 (в сводке 2; факт — по полям) |
| 8 | Культивация | все | 2026 |
| 9 | **вспашка 2027**, глубокорыхление — все | вспашка — 2027 |
| 11 | Уборка | все | 2026 |

Таблица выше — как **сами сводки** режут `machine_task.season` и `history_items`. Для суточного факта проверки она не фильтрует запрос в Cropwise.

**Cropwise UI** (`work.json`) отдаёт только сезон, выбранный в сессии. Проверка забирает два сезона — прошлый и текущий (сезон Y идёт с 01.07.(Y-1) по 01.07.Y; осенью 2026 и весной 2027 это 2026 и 2027) — и складывает их. Суточный факт от сезона не зависит; сезон нужен для сезонных итогов и для культуры. В результате хранится разбивка по сезонам: гектары, источник `work_json` и статус (`loaded`, `empty`, `not_loaded`, `unstable`).

Если сезон не загрузился, итог помечается «неполный, сезон N не проверен». API v3 в колонку «Cropwise» не подставляется; его можно смотреть отдельной справочной цифрой. Для сводок, которые сами режут сезон (внесение, посев, вспашка), гектары другого сезона не входят в расхождение: если там больше 0, пишется предупреждение «есть задания сезона N — проверьте сезон заданий в Cropwise».

Переопределения: `uploads/season_ranges.json` + `uploads/plans_edits.json` (Редактор планов).

### Платформа (сезоны + культуры + листы Excel)

| Задача | Где настраивать |
|--------|-----------------|
| Сезон посева 2026 / 2026+2027 | Редактор планов → группа «4.0 Посев» → season_years; или proverka «Сезоны заданий» |
| Закрыть культуру (рапс на МУ) | Редактор планов → `CROP_FIRM_HIDDEN`: группа + фирма + культура = 1 |
| Ранг / скрыть в одной группе | `CROP_DISPLAY_RANK`: ранг 0 = скрыть, 1+ = порядок строк |
| Имена листов Excel = PDF | `firm_sheet_names.py` + постобработка при генерации |

Проверка:

```bash
docker exec -e REPORT_DATE=2026-09-11 v3_disk-api-1 \\
  python3 /app/svodki/plan/proverka/verify_platform.py --season-seeding 2027
```

Окно смены: **06:00 — 06:00** (МСК), как в сводках.

## Что сравнивается

- **Сводка** — `area_today` по агрофирмам (machine_tasks + mtfmi).
- **Cropwise UI** — колонка факта за период (например «Посев, га»), итог из строки **«Всего»**.  
  Колонка «Площадь, га» — это не факт за смену.

Результаты пишутся в JSON: `reconcile_report{N}_{date}.json`.
