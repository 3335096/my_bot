# Railway deployment

## 1) Подготовка репозитория

Убедитесь, что код запушен на GitHub — Railway деплоит напрямую из репозитория.

## 2) Создание проекта

1. Зайдите на [railway.com](https://railway.com) → **New Project**
2. Выберите **Deploy from GitHub repo** → выберите репозиторий
3. Railway автоматически обнаружит `Dockerfile` и начнёт сборку

## 3) PostgreSQL

1. В проекте нажмите **Add Service → Database → PostgreSQL**
2. Перейдите в настройки бота → вкладка **Variables**
3. Добавьте переменную:
   ```
   DATABASE_URL=${{Postgres.DATABASE_URL}}
   ```
   Railway подставит реальный URL автоматически через reference.

## 4) Переменные окружения

Добавьте в **Variables** бота:

### Обязательные

```
TELEGRAM_BOT_TOKEN=       # токен от @BotFather
OPENROUTER_API_KEY=       # ключ с openrouter.ai/keys
DATABASE_URL=${{Postgres.DATABASE_URL}}
```

### Модели (рекомендуется задать явно)

```
MODEL_GENERAL=google/gemini-2.0-flash
MODEL_CODING=anthropic/claude-sonnet-4-5
MODEL_TRANSLATION=google/gemini-2.0-flash
MODEL_RESEARCH=anthropic/claude-sonnet-4-5
MODEL_WEB=google/gemini-2.0-flash
MODEL_VISION=openai/gpt-4o
MODEL_AUDIO=openai/gpt-4o-audio-preview
```

Полный список доступных моделей: https://openrouter.ai/models

### Опциональные (дефолты уже в коде)

```
RECENT_SESSIONS_LIMIT=10
SAVED_SESSIONS_LIMIT=50
MAX_CONTEXT_MESSAGES=16
REQUEST_TIMEOUT_SECONDS=60
REQUEST_MAX_RETRIES=2
REQUEST_RETRY_BACKOFF_BASE_SECONDS=1
REQUEST_RETRY_BACKOFF_MAX_SECONDS=8
WEB_MAX_RESULTS=5
AUDIO_MAX_DURATION_SECONDS=300
AUDIO_MAX_FILE_SIZE_MB=20
DOCUMENT_MAX_FILE_SIZE_MB=20
DOCUMENT_MAX_EXTRACTED_CHARS=50000
APP_NAME=telegram-openrouter-agent
APP_URL=
```

## 5) Деплой

После добавления переменных Railway автоматически деплоит.
При каждом `git push` в ветку `main` деплой запускается заново.

Образ собирается из `Dockerfile`:
- базовый образ: `python:3.11-slim`
- `ffmpeg` установлен для нормализации аудио перед транскрибацией
- запуск: `python -m bot.main`
- политика перезапуска: `ON_FAILURE`, до 10 попыток (`railway.json`)
- схема БД создаётся автоматически при старте (`db.init_schema()`)

## 6) Валидация после деплоя

1. `/start` → бот отвечает, появляется reply-клавиатура
2. Текстовое сообщение → streaming-ответ с routing badge в первом сообщении сессии
3. Второе сообщение в той же сессии → badge не показывается
4. Голосовое сообщение → typing... → блок транскрипции + streaming-ответ
5. Фото → streaming-анализ изображения
6. PDF-документ → typing... → краткое изложение содержимого
7. `🕘 Последние 10` → список диалогов с кнопками Сохранить / Открепить / Удалить
8. Сохранить диалог → появляется в `⭐ Сохраненные` с кнопкой Открепить
9. Удалить диалог → немедленно исчезает из списка
