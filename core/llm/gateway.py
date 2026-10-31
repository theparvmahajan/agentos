from enum import Enum

from core.config import settings
from core.llm.base import LLMError, LLMProvider, LLMResponse
from core.llm.providers import OllamaProvider, OpenAICompatibleProvider


class TaskType(str, Enum):
    GENERAL = "general"
    PLANNING = "planning"
    RESEARCH = "research"
    CODING = "coding"
    REVIEW = "review"


# Future router seam: map a task type to a specific model here.
MODEL_OVERRIDES: dict[TaskType, str] = {}


def select_model(task_type: TaskType) -> str:
    return MODEL_OVERRIDES.get(task_type, settings.llm_model)


def get_provider() -> LLMProvider:
    name = settings.llm_provider
    if name == "ollama":
        return OllamaProvider(settings.ollama_base_url, settings.llm_timeout_seconds)
    if name == "openai_compatible":
        if not settings.openai_base_url or not settings.openai_api_key:
            raise LLMError("OPENAI_BASE_URL and OPENAI_API_KEY must be set")
        return OpenAICompatibleProvider(
            settings.openai_base_url,
            settings.openai_api_key,
            settings.llm_timeout_seconds,
        )
    raise LLMError(f"unknown LLM provider: {name}")


def generate(
    prompt: str,
    task_type: TaskType = TaskType.GENERAL,
    *,
    system: str | None = None,
    json_mode: bool = False,
    provider: LLMProvider | None = None,
) -> LLMResponse:
    provider = provider or get_provider()
    return provider.complete(
        prompt=prompt,
        system=system,
        model=select_model(task_type),
        json_mode=json_mode,
    )