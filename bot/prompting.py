from __future__ import annotations

from bot.config import settings
from bot.router_logic import Intent


SYSTEM_PROMPTS: dict[Intent, str] = {
    Intent.GENERAL: (
        "You are a helpful assistant. Answer in the user's language. "
        "Be concise and practical first, then expand when requested."
    ),
    Intent.TRANSLATION: (
        "You are a professional translator. Preserve meaning, tone, and formatting. "
        "If language direction is ambiguous, infer and state assumption briefly."
    ),
    Intent.CODING: (
        "You are a senior software engineer. Prioritize correctness, "
        "concrete implementation details, and production-safe guidance."
    ),
    Intent.RESEARCH: (
        "You are a research assistant. Provide a structured answer: "
        "short summary, key findings, and actionable conclusions."
    ),
    Intent.WEB: (
        "You are a web-grounded assistant. Use recent information when needed "
        "and cite sources explicitly."
    ),
    Intent.VISION: (
        "You analyze images accurately. Describe key observations and then answer "
        "the user's concrete question. Extract visible text when useful."
    ),
    Intent.AUDIO: (
        "You are a helpful assistant. The user's message came from voice transcription. "
        "Answer clearly, preserving user intent."
    ),
}


def model_for_intent(intent: Intent) -> str:
    if intent == Intent.CODING:
        return settings.model_coding
    if intent == Intent.TRANSLATION:
        return settings.model_translation
    if intent == Intent.RESEARCH:
        return settings.model_research
    if intent == Intent.WEB:
        return settings.model_web
    if intent == Intent.VISION:
        return settings.model_vision
    if intent == Intent.AUDIO:
        return settings.model_general
    return settings.model_general


def route_name(intent: Intent) -> str:
    mapping = {
        Intent.GENERAL: "general",
        Intent.CODING: "coding",
        Intent.TRANSLATION: "translation",
        Intent.RESEARCH: "research",
        Intent.WEB: "web",
        Intent.VISION: "vision",
        Intent.AUDIO: "audio",
    }
    return mapping[intent]


def build_system_prompt(intent: Intent) -> str:
    return SYSTEM_PROMPTS[intent]


def build_badge(intent: Intent, *, model: str, use_web_search: bool) -> str:
    tools = "web" if use_web_search else "-"
    return f"🧭 {route_name(intent)} | {model} | {tools}"
