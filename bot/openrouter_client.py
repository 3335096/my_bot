import asyncio
import base64
from dataclasses import dataclass
import json
import logging
from typing import Any, AsyncGenerator

import httpx

from bot.config import settings

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}


@dataclass(slots=True)
class LLMResult:
    text: str
    model: str
    route: str
    used_web_tool: bool


class OpenRouterClient:
    def __init__(self) -> None:
        self.base_url = settings.openrouter_base_url.rstrip("/")
        self.timeout = settings.request_timeout_seconds
        self.max_retries = settings.request_max_retries
        self.retry_backoff_base_seconds = settings.request_retry_backoff_base_seconds
        self.retry_backoff_max_seconds = settings.request_retry_backoff_max_seconds

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {settings.openrouter_api_key}",
            "Content-Type": "application/json",
        }
        if settings.app_name:
            headers["X-Title"] = settings.app_name
        if settings.app_url:
            headers["HTTP-Referer"] = settings.app_url
        return headers

    def _retry_delay_seconds(self, attempt: int) -> float:
        # Exponential backoff: base * 2^attempt, clamped by max value.
        return min(
            self.retry_backoff_base_seconds * (2**attempt),
            self.retry_backoff_max_seconds,
        )

    def _should_retry_status(self, status_code: int) -> bool:
        return status_code in RETRYABLE_STATUS_CODES

    async def _post_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(
                        f"{self.base_url}/chat/completions",
                        headers=self._headers(),
                        json=payload,
                    )

                if (
                    self._should_retry_status(response.status_code)
                    and attempt < self.max_retries
                ):
                    delay = self._retry_delay_seconds(attempt)
                    logger.warning(
                        "OpenRouter temporary status=%s, retrying in %.2fs (attempt %s/%s)",
                        response.status_code,
                        delay,
                        attempt + 1,
                        self.max_retries + 1,
                    )
                    await asyncio.sleep(delay)
                    continue

                response.raise_for_status()
                return response.json()

            except (httpx.TimeoutException, httpx.RequestError) as exc:
                if attempt >= self.max_retries:
                    raise

                delay = self._retry_delay_seconds(attempt)
                logger.warning(
                    "OpenRouter request failed (%s), retrying in %.2fs (attempt %s/%s)",
                    type(exc).__name__,
                    delay,
                    attempt + 1,
                    self.max_retries + 1,
                )
                await asyncio.sleep(delay)

        raise RuntimeError("Unreachable retry loop state")

    @staticmethod
    def _extract_text(response_json: dict[str, Any]) -> str:
        choices = response_json.get("choices", [])
        if not choices:
            return "Не удалось получить ответ от модели."
        message = choices[0].get("message", {})
        content = message.get("content")
        if isinstance(content, str):
            return content.strip() or "Пустой ответ от модели."
        if isinstance(content, list):
            text_parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text", "").strip()
                    if text:
                        text_parts.append(text)
            if text_parts:
                return "\n".join(text_parts)
        return "Не удалось разобрать ответ модели."

    @staticmethod
    def _response_used_web_tool(response_json: dict[str, Any]) -> bool:
        usage = response_json.get("usage", {})
        server_tool_use = usage.get("server_tool_use", {}) if isinstance(usage, dict) else {}
        web_requests = server_tool_use.get("web_search_requests", 0)
        return bool(web_requests and web_requests > 0)

    async def chat(
        self,
        model: str,
        route: str,
        messages: list[dict[str, Any]],
        enable_web_search: bool,
    ) -> LLMResult:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        if enable_web_search:
            payload["tools"] = [
                {
                    "type": "openrouter:web_search",
                    "parameters": {"max_results": settings.openrouter_max_web_results},
                }
            ]

        response_json = await self._post_chat(payload)
        return LLMResult(
            text=self._extract_text(response_json),
            model=model,
            route=route,
            used_web_tool=self._response_used_web_tool(response_json),
        )

    async def stream_chat(
        self,
        model: str,
        route: str,
        messages: list[dict[str, Any]],
        enable_web_search: bool,
    ) -> AsyncGenerator[str, None]:
        """Stream chat completions as text chunks via SSE."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
        }
        if enable_web_search:
            payload["tools"] = [
                {
                    "type": "openrouter:web_search",
                    "parameters": {"max_results": settings.openrouter_max_web_results},
                }
            ]

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0]["delta"].get("content") or ""
                        if delta:
                            yield delta
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue

    async def transcribe_audio(self, audio_bytes: bytes, audio_format: str) -> str:
        b64_audio = base64.b64encode(audio_bytes).decode("utf-8")
        payload = {
            "model": settings.model_audio,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Please transcribe this audio file."},
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": b64_audio,
                                "format": audio_format,
                            },
                        },
                    ],
                }
            ],
        }
        response_json = await self._post_chat(payload)
        return self._extract_text(response_json)
