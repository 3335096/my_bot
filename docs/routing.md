# Smart routing policy

This document defines the task routing policy used by the bot.

## 1) Intent classes

- `general` — общий диалог
- `translation` — перевод
- `coding` — программирование и технические вопросы
- `research` — deep research (структурированный анализ)
- `web` — актуальные данные из интернета
- `vision` — анализ изображений
- `audio` — транскрибация голоса

## 2) Input-level routing

1. If message contains audio/voice → `audio` (transcribed first, then text pipeline).
2. If message contains photo/image → `vision`.
3. If message contains document → text pipeline with extracted content.
4. Otherwise route text with heuristic keyword detection:
   - translation keywords → `translation`
   - coding keywords → `coding`
   - research keywords → `research` + web search enabled
   - web keywords → `web` + web search enabled
   - fallback → `general`

## 3) Model selection

Each intent maps to a **separate model** configured via environment variables.
This allows switching providers and models without code changes.

| Intent | Env var | Default | Rationale |
|--------|---------|---------|-----------|
| `general` | `MODEL_GENERAL` | `google/gemini-2.0-flash` | Fast, cheap, multilingual |
| `coding` | `MODEL_CODING` | `anthropic/claude-sonnet-4-5` | Strong reasoning and code generation |
| `translation` | `MODEL_TRANSLATION` | `google/gemini-2.0-flash` | Fast, multilingual |
| `research` | `MODEL_RESEARCH` | `anthropic/claude-sonnet-4-5` | Deep reasoning, large context |
| `web` | `MODEL_WEB` | `google/gemini-2.0-flash` | Fast responses for fresh web data |
| `vision` | `MODEL_VISION` | `openai/gpt-4o` | Strong vision capabilities |
| `audio` (STT) | `MODEL_AUDIO` | `openai/gpt-4o-audio-preview` | input_audio API support |

> After audio transcription the resulting text runs through the standard text pipeline
> (intent re-detected from transcript, `model_general` or matching model applied).

## 4) System prompts

Each intent has a dedicated system prompt tuned for its task:

- `general` — concise helpful assistant, answers in user's language
- `translation` — professional translator, preserves meaning and tone
- `coding` — senior software engineer, production-safe guidance
- `research` — structured answer: summary → findings → conclusions
- `web` — web-grounded, cites sources explicitly
- `vision` — describes image, extracts visible text, answers user's question
- `audio` — same as general (voice input context)

## 5) Tool selection

When route requires fresh web data (`research` and `web`), request includes:

```json
{
  "tools": [
    {
      "type": "openrouter:web_search",
      "parameters": { "max_results": 5 }
    }
  ]
}
```

`max_results` is configured by `WEB_MAX_RESULTS`.

## 6) Audio normalization and STT fallback

For `audio` route the bot uses a resilient pipeline:

0. Validate configured audio limits (duration + file size).
1. Detect source format (file extension + mime type).
2. Normalize audio with `ffmpeg` to `wav` (`mono`, `16k`).
3. Send normalized audio to STT model.
4. If STT fails and source format is supported, retry once with source audio.
5. If `ffmpeg` is missing, use source format directly when supported.

Audio limits are configured with:

- `AUDIO_MAX_DURATION_SECONDS` (default 300)
- `AUDIO_MAX_FILE_SIZE_MB` (default 20)

## 7) Document support

For documents (PDF, text files):

1. Validate file size limit (`DOCUMENT_MAX_FILE_SIZE_MB`).
2. Extract text (PDF via pypdf, text files via UTF-8 decode).
3. Truncate to `DOCUMENT_MAX_EXTRACTED_CHARS` if needed.
4. Route through text pipeline using caption as the user question.

## 8) OpenRouter retry/backoff policy

The OpenRouter client uses retry with exponential backoff for transient failures.

- Retryable statuses: `408, 409, 425, 429, 500, 502, 503, 504`
- Retryable exceptions: timeout/network transport errors
- Config:
  - `REQUEST_MAX_RETRIES` (default 2)
  - `REQUEST_RETRY_BACKOFF_BASE_SECONDS` (default 1)
  - `REQUEST_RETRY_BACKOFF_MAX_SECONDS` (default 8)

> Streaming responses (`stream_chat`) do not retry mid-stream.
> Retry applies only to initial connection failures.

## 9) Routing badge UX

Badge is shown only in the first assistant reply of a new session.

Format:

```
🧭 <route> | <model> | <web|->
```

Examples:

- `🧭 coding | anthropic/claude-sonnet-4-5 | -`
- `🧭 web | google/gemini-2.0-flash | web`
- `🧭 research | anthropic/claude-sonnet-4-5 | web`

The bot stores `badge_sent` per session.

## 10) Session list and storage rules

- Recent list is exactly last 10 sessions (`RECENT_SESSIONS_LIMIT=10`).
- Saved sessions list is limited (`SAVED_SESSIONS_LIMIT`, default 50).
- Overflow policy:
  - recent: remove oldest non-saved sessions
  - saved: remove oldest saved sessions
- Forced delete from inline button removes dialog immediately (session and messages).
- Unsave button available in both recent and saved lists.
