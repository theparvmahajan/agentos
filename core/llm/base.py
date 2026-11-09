from dataclasses import dataclass
from typing import Protocol


class LLMError(Exception):
    """Any failure talking to an LLM provider.

    `retryable` says whether trying again could plausibly help (the provider was busy, down or
    unreachable) as opposed to never helping (a bad key, a malformed request or reply). The
    default is False: only a failure we positively recognise as transient is retried.
    """

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


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