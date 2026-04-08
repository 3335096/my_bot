from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Intent(str, Enum):
    GENERAL = "general"
    CODING = "coding"
    TRANSLATION = "translation"
    RESEARCH = "research"
    WEB = "web"
    VISION = "vision"
    AUDIO = "audio"


@dataclass(slots=True)
class RouteDecision:
    intent: Intent
    use_web_search: bool = False

    def badge(self, model_slug: str) -> str:
        tools = "web" if self.use_web_search else "-"
        return f"🧭 {self.intent.value} | {model_slug} | {tools}"


TRANSLATION_HINTS = (
    "переведи",
    "translate",
    "translation",
    "на англий",
    "на русский",
    "на немец",
    "на француз",
)

CODING_HINTS = (
    "python",
    "javascript",
    "typescript",
    "debug",
    "ошибка",
    "код",
    "api",
    "sql",
    "bug",
    "refactor",
)

RESEARCH_HINTS = (
    "deep research",
    "исследуй",
    "подробно",
    "сравни",
    "обзор",
)

WEB_HINTS = (
    "сегодня",
    "последние",
    "latest",
    "news",
    "в интернете",
    "найди в веб",
    "источники",
    "актуаль",
)


def detect_intent(text: str | None, *, has_photo: bool, has_audio: bool) -> RouteDecision:
    if has_audio:
        return RouteDecision(intent=Intent.AUDIO, use_web_search=False)
    if has_photo:
        return RouteDecision(intent=Intent.VISION, use_web_search=False)

    normalized = (text or "").strip().lower()
    if not normalized:
        return RouteDecision(intent=Intent.GENERAL, use_web_search=False)

    if any(h in normalized for h in TRANSLATION_HINTS):
        return RouteDecision(intent=Intent.TRANSLATION, use_web_search=False)
    if any(h in normalized for h in CODING_HINTS):
        return RouteDecision(intent=Intent.CODING, use_web_search=False)
    if any(h in normalized for h in RESEARCH_HINTS):
        return RouteDecision(intent=Intent.RESEARCH, use_web_search=True)
    if any(h in normalized for h in WEB_HINTS):
        return RouteDecision(intent=Intent.WEB, use_web_search=True)

    return RouteDecision(intent=Intent.GENERAL, use_web_search=False)

