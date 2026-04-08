# Smart routing policy

This document defines the task routing policy used by the bot.

## 1) Intent classes

- `general`
- `translation`
- `coding`
- `research`
- `web`
- `vision`
- `audio`

## 2) Input-level routing

1. If message contains audio/voice -> `audio` (audio is transcribed first).
2. If message contains photo/image -> `vision`.
3. Otherwise route text with heuristic detection:
   - translation keywords -> `translation`
   - coding keywords -> `coding`
   - research keywords -> `research` + web search enabled
   - web keywords -> `web` + web search enabled
   - fallback -> `general`

## 3) Model selection

The model is selected by route using environment variables:

- `MODEL_GENERAL`
- `MODEL_CODING`
- `MODEL_TRANSLATION`
- `MODEL_RESEARCH`
- `MODEL_VISION`
- `MODEL_AUDIO` (for transcription)

This allows switching providers/models without code changes.

## 4) Tool selection

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

## 5) Routing badge UX

Badge is shown only in the first assistant reply of a new session.

Format:

```
🧭 <route> | <model> | <web|->
```

Examples:

- `🧭 coding | openai/gpt-4.1 | -`
- `🧭 web | openai/gpt-4.1 | web`

The bot stores `badge_sent` per session.

## 6) Session list and storage rules

- Recent list is exactly last 10 sessions (`RECENT_SESSIONS_LIMIT=10`).
- Saved sessions list is limited (`SAVED_SESSIONS_LIMIT`, default 50).
- Overflow policy:
  - recent: remove oldest non-saved sessions
  - saved: remove oldest saved sessions
- Forced delete from inline button removes dialog immediately (session and messages).

