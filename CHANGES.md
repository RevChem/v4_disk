# Исправления (ревизия кода сводок)

## Раунд 1
1. report_runner.py — гонка записи прогресса/длительностей: RLock + уникальный tmp + os.replace;
   запись статистики больше не роняет уже готовую сводку.
2. svodki_task_store.py / svodki_daily_facts_area.py — flat_facts получил фильтр season.
3. report_runner.py — _save_artifact атомарный (tmp .part -> os.replace, потом чистка старых).
4. report_runner.py — generate_reports_to_disk: одна пачка за раз в процессе (ожидание отменяемо).
5. report_generation.py — семафор PDF: создание под локом, release того же объекта.

## Раунд 2 (углублённая проверка)
6. NEW backend/app/legacy/json_store_io.py + применено в cell_edits_store, comments_store, plans_editor,
   manual_overrides, interface_settings_store, pdf_header_labels, plans_edit_lock:
   - атомарная запись (уникальный tmp, fsync, os.replace); manual_overrides писался прямо в файл;
   - битый JSON больше не читается молча как {} (следующее сохранение затирало ВСЕ правки):
     файл уходит в бэкап *.corrupt-<ts> и пишется error в лог;
   - read-modify-write под store_rmw_lock (RLock + flock между процессами): summaries.py (6 эндпоинтов),
     plans_editor.set_plan_value/set_work_groups, manual_overrides.reset.../set_sync_exclusion.
7. task_area_shared.item_area_in_window — если почасовые данные есть, но вся площадь вне окна смены,
   возвращалась ПОЛНАЯ covered_area строки (фирма получала площадь, отработанную в другие сутки).
   Теперь 0; откат к covered_area только если почасовых данных нет/нули/нечитаемы.
   Уточнение (раунд 3): напрямую окно считает сводка 6 — там цифры изменились; в сводках 1/2
   task_cov_by_firm использует окно только как веса фирм, итог масштабируется к task.covered_area,
   поэтому на суммы сводок 1/2 исходная правка влияла слабо (см. раунд 3, п. 12).
8. report_generation._extract_saved_xlsx_path — имя файла после «Сохранено:» резалось по первому пробелу
   (выбирался чужой самый свежий xlsx). Теперь берётся всё до «.xlsx».

Тесты: backend/tests/test_json_store_io.py, backend/tests/test_report_runner_state.py,
svodki/plan/tools/test_item_area_window.py.

## Раунд 3
9. Культивация: единый набор work_type_id = 10, 50, 63, 96 везде (plans.WORK_GROUPS, cropwise/*,
   сводка 8 — убран вид 51, proverka/work_groups.py, backend/works_summary.py, тест test_work_groups).
10. Сводка 1.18 удалена полностью (каталог, report_generation, report_runner, редакторы порядка/макета,
    manual_overrides, pdf_header_labels, svodki_prefetch). Файла 1.18_main_ammonium__1__day.py не было.
11. proverka/cropwise_crops.seasons_missing_from_ui: при пустом плане возвращает [] (как в docstring и
    тесте); убрана вводящая в заблуждение строка `_ = seasons_in_shift`.
    ВНИМАНИЕ: при пустом плане и пустой вкладке UI культуры из API v3 больше не добавляются.
12. task_area_shared: item_area_in_window больше не откатывается к полной covered_area при нулевых или
    нечитаемых почасовых данных (откат только если почасовых данных нет вовсе или окно не задано).
    task_cov_by_firm: если почасовые данные есть, но в окне площади нет — возвращает {} вместо
    деления полной covered_area поровну между фирмами. Меняет цифры в сводках 1/2/6 для таких заданий.
13. test_task_store_sql_season_param больше не ходит в сеть (хранилище помечено свежим; границы окна
    форматируются в поясе аккаунта через ts.fmt_ts; _put_task вызывается после установки cov_lo).
14. json_store_io:
    - OSError при чтении (права, I/O) — 3 повтора, затем JsonStoreReadError; здоровый файл больше не
      переименовывается в *.corrupt-* и не подменяется {};
    - JSON не-объект (список и т.п.) уходит в бэкап *.corrupt-<ts> с записью в лог;
    - межпроцессная блокировка на Windows через msvcrt.locking (не проверялось на Windows);
    - блокировки по имени хранилища (plans_edits, manual_overrides, summaries) вместо одного общего lock-файла.
15. Исправлены пропущенные импорты (NameError при импорте): manual_overrides.py
    (serialized_rmw, read_json_dict, atomic_write_text) и comments_store.py (read_json_dict, atomic_write_text).
    В исходном архиве импорт report_generation падал из-за этого.

## Найдено, но НЕ менялось (нужно ваше решение)
- season_unified: year_plus_one/plow_year_plus_one = report_date.year+1 без учёта месяца. Без сохранённого
  диапазона в редакторе планов с 01.01.2027 по 30.06.2027 «Посев/Опрыскивание/Вспашка» уйдут на сезон 2028
  (с июля снова верно). Тот же допущение зашито в 5_main_fixed (SEASON_TASK_YEAR_STR),
  2_main_ideal_compact (~стр. 1560) и 9_main_fixed (~стр. 162). Формула: год+1 при месяце >= 7, иначе год.
- Повтор упавших сводок (report_runner) перезапускает и детерминированные ошибки.
- «2026» в названии сводки 5 (report_catalog) и жёсткий REPORT_DATE по умолчанию в 5_main_fixed; N+1.

## Раунд 4 (редактор планов, проверка сводок, фронтенд)
- proverka/service.py: status.json пишется атомарно; read-modify-write (отмена, прогресс, AI) под общей блокировкой,
  чтение устойчиво к недописанному файлу; job_id валидируется (UUID); params.json (с cookie) стирается сразу
  после чтения и при ранней отмене; история/индекс AI пишутся атомарно под блокировкой.
- plans_editor.py: можно снять скрытие культуры, заданное в plans.py (хранится явный 0); база plans.py кэшируется по mtime;
  set_plan_value/set_work_groups не грузят план дважды; дубли названий групп отклоняются, ID дедуплицируются.
- season_meta.py: season_ranges.json — атомарная запись, блокировка RMW, битый файл уходит в *.corrupt-<ts>;
  season_unified кэшируется, store не перечитывается на каждую группу.
- Фронтенд: раздельные счётчики загрузки/записи в PlansEditor (нет «вечной» загрузки); ошибки через formatError
  (422 больше не роняет интерфейс), ErrorBoundary в main.jsx; пустое поле не сохраняется как 0;
  Grid переведён на API MUI v7 (size); таймауты запросов редактора планов; в редакторе групп — детект конфликта
  с чужими правками, проверка дублей/некорректных ID, предупреждение об «осиротевших» правках;
  ProverkaDialog: сброс старого jobId перед запуском, onComplete через ref, устойчивость к единичным сбоям опроса.
- Удалено: папка cropwise/ (код чужого приложения, импортировал несуществующие app.cropwise/app.users),
  __pycache__, *.pyc, violations_sync.py.save.
