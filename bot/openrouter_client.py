import base64
from dataclasses import dataclass
from typing import Any

import httpx

from bot.config import settings


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

    async def _post_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            )
            response.raise_for_status()
            return response.json()

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
