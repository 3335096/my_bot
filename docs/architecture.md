# Архитектура

## Обзор

Бот построен как асинхронное приложение на `aiogram` и состоит из следующих слоев:

1. **Telegram Layer (`bot/handlers.py`)**
   - Прием пользовательских событий (text, voice/audio, photo, commands, callbacks).
   - Управление UX: кнопки, вывод последних и сохраненных диалогов.
2. **Routing Layer (`bot/router_logic.py`, `bot/prompting.py`)**
   - Определение intent по сообщению.
   - Выбор модели, system prompt и флага web-search.
3. **LLM Gateway (`bot/openrouter_client.py`)**
   - OpenRouter Chat Completions вызовы.
   - Подключение server tool `openrouter:web_search`.
   - Транскрибация аудио через `input_audio`.
4. **Audio Normalization (`bot/audio_pipeline.py`)**
   - Определение формата аудио по расширению/mime.
   - Нормализация через `ffmpeg` в `wav mono 16k`.
   - Fallback-план (повторная транскрибация на исходном формате).
5. **Persistence (`bot/db.py`)**
   - PostgreSQL хранение пользователей, сессий, сообщений.
   - Активная сессия пользователя, сохраненные/последние диалоги.

## Поток запроса (text)

1. Пользователь отправляет текст.
2. Бот поднимает/обновляет пользователя и получает активную сессию.
3. Router определяет intent.
4. Формируется контекст: system prompt + история диалога + новый вопрос.
5. OpenRouter возвращает ответ модели.
6. Бот сохраняет user/assistant сообщения в БД.
7. Если это первая реплика новой сессии — добавляет routing badge.
8. Отправляет итоговый ответ в Telegram.

## Поток запроса (voice)

1. Бот скачивает голосовой файл из Telegram.
2. Приводит аудио к `wav mono 16k` через `ffmpeg`.
3. Передает нормализованное аудио в OpenRouter как `input_audio`.
4. При ошибке делает fallback-попытку транскрибации исходного формата.
5. Транскрипт идет в text pipeline как обычный user prompt.
6. Пользователь получает ответ + блок транскрипции.

## Поток запроса (image)

1. Бот скачивает изображение.
2. Кодирует в base64 data URL.
3. Отправляет в модель с multimodal `content` (`text` + `image_url`).
4. Сохраняет ответ и отдает его пользователю.

