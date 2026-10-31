from dataclasses import dataclass
from typing import Protocol


class LLMError(Exception):
    """Any failure talking to an LLM provider."""


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int


class LLMProvider(Protocol):
    def complete(
        self,
        *,
        prompt: str,
        system: str | None,
        model: str,
        json_mode: bool,
    ) -> LLMResponse: ...