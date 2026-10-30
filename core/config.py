from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://agentos:agentos@localhost:5432/agentos"
    redis_url: str = "redis://localhost:6379/0"
    llm_provider: str = "ollama"
    ollama_base_url: str = "http://localhost:11434"


settings = Settings()
