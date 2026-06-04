# Railway Deploy

Деплой делаем через сайт Railway и GitHub-репозиторий.

## Start command

В репозитории есть `railway.json`. Railway будет запускать:

```powershell
python run_telegram.py
```

## Environment variables

В Railway нужно добавить переменные:

```env
TELEGRAM_BOT_TOKEN=
OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.aitunnel.ru/v1/
OPENAI_MODEL=gpt-4o-mini
OPENAI_VISION_MODEL=gpt-4o-mini
GOOGLE_SHEETS_SPREADSHEET_ID=
GOOGLE_SHEETS_WORKSHEET_NAME=Расчеты
GOOGLE_APPLICATION_CREDENTIALS_JSON=
```

`OPENAI_MODEL` можно переключать между `deepseek-v4-pro` и `gpt-4o-mini`.
`OPENAI_VISION_MODEL` используется для обработки скриншотов и фото; для нее лучше оставить модель с поддержкой изображений, например `gpt-4o-mini`.

`GOOGLE_APPLICATION_CREDENTIALS_JSON` должен содержать полный JSON service account.
Локальный путь `GOOGLE_APPLICATION_CREDENTIALS` на Railway не нужен.

## GitHub flow

1. Открыть Railway.
2. Войти через GitHub.
3. New Project.
4. Deploy from GitHub repo.
5. Выбрать `sp1908storage/Roundtrip-Profit-Calculator`.
6. Добавить переменные окружения.
7. Запустить deploy.
