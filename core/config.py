from pydantic import field_validator, model_validator
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
    # Where agent-written code runs: "docker" (isolated container, the default) or "local"
    # (a plain subprocess on THIS machine, no isolation; only for when you accept that risk).
    code_exec_backend: str = "docker"
    code_exec_image: str = "python:3.12-slim"
    # Browser origins allowed to call the API (comma separated). The dashboard's address.
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    # A running task is held under a lease that its worker renews (a "heartbeat"). If the worker
    # dies, the lease runs out after `task_lease_seconds` and the reaper recovers the task.
    # Keep the lease well above the heartbeat so one late beat does not look like a death.
    task_lease_seconds: float = 30.0
    lease_heartbeat_seconds: float = 10.0
    reaper_interval_seconds: float = 10.0
    # Retrying a failed attempt: wait min(cap, base * 2**(n-1)) seconds, half of it random.
    retry_base_seconds: float = 2.0
    retry_max_seconds: float = 60.0
    # How often the sweeper publishes READY tasks that are due (a finished backoff, or a message
    # that was lost), and how long a published message is trusted before it is published again.
    sweep_interval_seconds: float = 2.0
    ready_republish_seconds: float = 60.0
    # Multiplies every agent's timeout_seconds (1.0 = as stored). Raise it for a slow local model.
    task_timeout_scale: float = 1.0

    @model_validator(mode="after")
    def _heartbeat_inside_lease(self) -> "Settings":
        if self.lease_heartbeat_seconds * 2 > self.task_lease_seconds:
            raise ValueError(
                "LEASE_HEARTBEAT_SECONDS must be at most half of TASK_LEASE_SECONDS, "
                "otherwise one late heartbeat expires a healthy worker's lease"
            )
        for name in (
            "reaper_interval_seconds",
            "retry_base_seconds",
            "retry_max_seconds",
            "sweep_interval_seconds",
            "ready_republish_seconds",
            "task_timeout_scale",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name.upper()} must be positive")
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("RETRY_MAX_SECONDS must not be smaller than RETRY_BASE_SECONDS")
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.cors_origins.split(",") if o.strip()]

    @field_validator(
        "llm_provider", "llm_model", "code_exec_backend", "code_exec_image", mode="before"
    )
    @classmethod
    def _strip(cls, value: object) -> object:
        """A stray space in .env (or a default) breaks model lookups; remove it."""
        return value.strip() if isinstance(value, str) else value


settings = Settings()
