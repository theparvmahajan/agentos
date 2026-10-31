from core.llm.base import LLMError, LLMResponse


class FakeProvider:
    """Returns scripted responses and records every call. For tests and dev."""

    def __init__(self, responses: list[str] | None = None):
        self._responses = list(responses or [])
        self.calls: list[dict] = []

    def complete(
        self, *, prompt: str, system: str | None, model: str, json_mode: bool
    ) -> LLMResponse:
        self.calls.append(
            {"prompt": prompt, "system": system, "model": model, "json_mode": json_mode}
        )
        if not self._responses:
            raise LLMError("FakeProvider has no more scripted responses")
        text = self._responses.pop(0)
        return LLMResponse(
            text=text,
            model=model,
            input_tokens=len(prompt.split()),
            output_tokens=len(text.split()),
            latency_ms=0,
        )