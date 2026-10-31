from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://agentos:agentos@localhost:5432/agentos"
    redis_url: str = "redis://localhost:6379/0"

    llm_provider: str = "ollama"  # "ollama" or "openai_compatible"
    llm_model: str = "qwen2.5:7b "
    llm_timeout_seconds: float = 120.0
    ollama_base_url: str = "http://localhost:11434"
    openai_base_url: str = ""
    openai_api_key: str = ""


settings = Settings()