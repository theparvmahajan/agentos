from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://agentos:agentos@localhost:5432/agentos"
    redis_url: str = "redis://localhost:6379/0"
    queue_prefix: str = "agentos"
    llm_provider: str = "ollama"  # "ollama" or "openai_compatible"
    llm_model: str = "qwen2.5:7b"
    llm_timeout_seconds: float = 120.0
    ollama_base_url: str = "http://localhost:11434"
    openai_base_url: str = ""
    openai_api_key: str = ""
    tavily_api_key: str = ""  # secrets live in .env only, never as defaults here

    @field_validator("llm_provider", "llm_model", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        """A stray space in .env (or a default) breaks model lookups; remove it."""
        return value.strip() if isinstance(value, str) else value


settings = Settings()