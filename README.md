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

Файлы сохраняются в `uploads/YYYY-MM-DD/`.
