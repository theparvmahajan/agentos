import time

import httpx

from core.llm.base import LLMError, LLMResponse


def _post_json(
    client: httpx.Client,
    url: str,
    payload: dict,
    headers: dict | None,
    name: str,
) -> dict:
    try:
        resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        body = " ".join(e.response.text.split())[:300] or "no response body"
        status = e.response.status_code
        # 429 (rate limited) and 5xx (the server is struggling) pass with time; other 4xx are our
        # mistake (bad key, bad request) and would fail again.
        raise LLMError(
            f"{name} returned HTTP {status}: {body}", retryable=status == 429 or status >= 500
        ) from e
    except httpx.HTTPError as e:
        # Timeouts and connection failures: the server may be restarting or briefly unreachable.
        raise LLMError(f"{name} request failed: {e}", retryable=True) from e
    except ValueError as e:
        raise LLMError(f"{name} returned invalid JSON: {e}") from e


def _messages(prompt: str, system: str | None) -> list[dict]:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return messages


class OllamaProvider:
    def __init__(
        self,
        base_url: str,
        timeout: float = 120.0,
        client: httpx.Client | None = None,
    ):
        self._url = base_url.rstrip("/") + "/api/chat"
        self._client = client or httpx.Client(timeout=timeout)

    def complete(
        self, *, prompt: str, system: str | None, model: str, json_mode: bool
    ) -> LLMResponse:
        payload = {
            "model": model,
            "messages": _messages(prompt, system),
            "stream": False,
        }
        if json_mode:
            payload["format"] = "json"

        start = time.perf_counter()
        data = _post_json(self._client, self._url, payload, None, "ollama")
        latency_ms = int((time.perf_counter() - start) * 1000)

        try:
            text = data["message"]["content"]
        except (KeyError, TypeError) as e:
            raise LLMError(f"unexpected ollama response: {e!r}") from e

        return LLMResponse(
            text=text,
            model=model,
            input_tokens=data.get("prompt_eval_count", 0),
            output_tokens=data.get("eval_count", 0),
            latency_ms=latency_ms,
        )


class OpenAICompatibleProvider:
    """Works with any API that speaks the OpenAI chat-completions format."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 120.0,
        client: httpx.Client | None = None,
    ):
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = client or httpx.Client(timeout=timeout)

    def complete(
        self, *, prompt: str, system: str | None, model: str, json_mode: bool
    ) -> LLMResponse:
        payload = {"model": model, "messages": _messages(prompt, system)}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        start = time.perf_counter()
        data = _post_json(
            self._client, self._url, payload, self._headers, "openai-compatible"
        )
        latency_ms = int((time.perf_counter() - start) * 1000)

        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"unexpected response shape: {e!r}") from e

        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            model=model,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            latency_ms=latency_ms,
        )