# Railway deployment

## 1) Create project
1. Create a new Railway project and connect your GitHub repository.
2. Add a PostgreSQL service.
3. Ensure `DATABASE_URL` from PostgreSQL is available to the bot service.

## 2) Required environment variables
Set in Railway service variables:

- `TELEGRAM_BOT_TOKEN`
- `OPENROUTER_API_KEY`
- `DATABASE_URL`

Recommended:

- `MODEL_GENERAL`
- `MODEL_CODING`
- `MODEL_TRANSLATION`
- `MODEL_RESEARCH`
- `MODEL_VISION`
- `MODEL_AUDIO`
- `RECENT_SESSIONS_LIMIT=10`
- `SAVED_SESSIONS_LIMIT=50`

## 3) Build and run
The repository includes `Dockerfile` and `railway.json`.
Railway will use:

- start command: `python -m bot.main`
- `ffmpeg` is installed in image for reliable audio normalization before STT

## 4) Validate deployment
1. Open bot in Telegram and run `/start`.
2. Send first text message in a new session and verify routing badge appears once.
3. Send second message in same session and verify no badge.
4. Open `🕘 Последние 10`, save any dialog, then verify it appears in `⭐ Сохраненные`.
5. Delete dialog from list and verify it is removed immediately.
6. Send a voice message and ensure transcription works (normal path and fallback path in logs if needed).
