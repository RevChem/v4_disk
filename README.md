# Svodki: планы и PDF

Минимальный сервис для:

- редактирования планов (`svodki/plan/plans.py` + JSON-правки);
- запуска отчётных скриптов из `svodki/plan/`;
- конвертации Excel → PDF (LibreOffice + шапки).

## Структура

```
backend/app/     API и оркестрация
frontend/        UI: редактор планов, генерация, Excel→PDF
svodki/plan/     Скрипты сводок и plans.py
deploy/          nginx + Dockerfile для фронта
```

## Запуск (Docker)

```bash
cp .env.example .env   # задайте CROPWISE_API_TOKEN
docker compose up -d --build
```

- UI: http://localhost:8080
- API: http://localhost:6757

## Запуск (локально)

```bash
# backend
cd backend && uv pip install -r requirements.txt
uvicorn app.main:app --reload --port 6757

# frontend
cd frontend && npm install && npm run dev
```

## API

| Маршрут | Описание |
|---------|----------|
| `GET /plans-editor` | Состояние редактора планов |
| `PUT /plans-editor/*` | Правки планов |
| `POST /reports/generate` | Скрипт → Excel → PDF |
| `GET /reports/files/{date}` | Список файлов за дату |
| `POST /convert/xlsx-to-pdf` | Готовый Excel → PDF |
| `GET /proverka/field-firm-alerts` | Последняя проверка «пометка фирмы в поле Namolot ≠ папка Cropwise» |
| `POST /proverka/field-firm-alerts/ignore` | «Это не ошибка» для пары пометка/папка (только admin) |
| `POST /proverka/field-firm-alerts/run` | Внеочередной прогон проверки (только admin) |

Файлы сохраняются в `uploads/YYYY-MM-DD/`.

## Передача полей между агрофирмами

Cropwise хранит только текущую папку поля. Если поле передали другой агрофирме посреди сезона, сводка 10 и кнопка Namolot иначе отдадут тонны до передачи новому владельцу.

Уже известные передачи пишутся в `svodki/plan/plans.py`, словарь `FIELD_FIRM_TRANSFERS`: код поля → список `(дата, фирма до, фирма с этой даты)`. Запись раньше 00:00 МСК даты передачи остаётся у прежней фирмы. Сейчас там `РВ-01-61`: АгроРаздолье до 02.09.2026, затем АФ Чистопольская. Та же дата применяется в сводке 10 и в `namolot_view.record_firm`.

Отдельно Celery-задача `check_field_firm_mismatch` (по умолчанию раз в 3 часа) сравнивает пометку в названии поля Namolot — `(АФ3)` или префикс `АФ5-` — с папкой Cropwise. Расхождения пишутся в `uploads/.field_firm_check.json` и показываются белым окном на дашборде (`FieldFirmNotices`). Окно закрывается крестиком и появляется снова после следующего прогона, пока расхождение не исчезнет. Кнопка «Это не ошибка» (admin) пишет пару в `uploads/.field_firm_ignore.json`. Поле из `FIELD_FIRM_TRANSFERS` ошибкой не считается, если пометка совпадает с фирмой на дату записи.

В `.env`:

- `FIELD_FIRM_CHECK_INTERVAL_SEC=10800` — интервал проверки, секунды (не меньше 300).
- `FIELD_FIRM_ALERT_ADMIN_ONLY=false` — при `true` список видит только администратор.
