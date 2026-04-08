from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    telegram_bot_token: str = Field(alias="TELEGRAM_BOT_TOKEN")
    openrouter_api_key: str = Field(alias="OPENROUTER_API_KEY")
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1", alias="OPENROUTER_BASE_URL"
    )
    app_name: str = Field(default="telegram-openrouter-agent", alias="APP_NAME")
    app_url: str = Field(default="", alias="APP_URL")

    database_url: str = Field(alias="DATABASE_URL")

    model_general: str = Field(default="openai/gpt-4o-mini", alias="MODEL_GENERAL")
    model_coding: str = Field(default="openai/gpt-4.1", alias="MODEL_CODING")
    model_translation: str = Field(
        default="openai/gpt-4o-mini", alias="MODEL_TRANSLATION"
    )
    model_research: str = Field(default="openai/gpt-4.1", alias="MODEL_RESEARCH")
    model_vision: str = Field(default="openai/gpt-4o-mini", alias="MODEL_VISION")
    model_audio: str = Field(
        default="openai/gpt-4o-audio-preview", alias="MODEL_AUDIO"
    )

    recent_sessions_limit: int = Field(default=10, alias="RECENT_SESSIONS_LIMIT")
    saved_sessions_limit: int = Field(default=50, alias="SAVED_SESSIONS_LIMIT")
    max_context_messages: int = Field(default=16, alias="MAX_CONTEXT_MESSAGES")
    request_timeout_seconds: float = Field(default=60.0, alias="REQUEST_TIMEOUT_SECONDS")
    openrouter_max_web_results: int = Field(default=5, alias="WEB_MAX_RESULTS")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
