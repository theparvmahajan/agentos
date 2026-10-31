import json

import httpx
import pytest

from core.llm.base import LLMError
from core.llm.providers import OllamaProvider, OpenAICompatibleProvider


def client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def call(provider, **overrides):
    args = {"prompt": "hi", "system": None, "model": "m", "json_mode": False}
    args.update(overrides)
    return provider.complete(**args)


# ---------- Ollama ----------

def test_ollama_parses_text_and_tokens():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "message": {"content": "hello"},
                "prompt_eval_count": 7,
                "eval_count": 3,
            },
        )

    resp = call(OllamaProvider("http://ollama.test", client=client_for(handler)))

    assert (resp.text, resp.input_tokens, resp.output_tokens) == ("hello", 7, 3)
    assert seen[0]["stream"] is False
    assert seen[0]["messages"] == [{"role": "user", "content": "hi"}]


def test_ollama_system_prompt_and_json_mode():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": "{}"}})

    provider = OllamaProvider("http://ollama.test", client=client_for(handler))
    call(provider, system="be brief", json_mode=True)

    assert seen[0]["messages"][0] == {"role": "system", "content": "be brief"}
    assert seen[0]["format"] == "json"


def test_ollama_http_error_becomes_llm_error():
    provider = OllamaProvider(
        "http://ollama.test",
        client=client_for(lambda r: httpx.Response(500, text="boom")),
    )
    with pytest.raises(LLMError):
        call(provider)


def test_ollama_malformed_response_becomes_llm_error():
    provider = OllamaProvider(
        "http://ollama.test",
        client=client_for(lambda r: httpx.Response(200, json={"oops": 1})),
    )
    with pytest.raises(LLMError):
        call(provider)


def test_ollama_connection_failure_becomes_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    provider = OllamaProvider("http://ollama.test", client=client_for(handler))
    with pytest.raises(LLMError):
        call(provider)


# ---------- OpenAI-compatible ----------

def test_openai_compatible_parses_response_and_sends_auth():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.headers["authorization"], json.loads(request.content)))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hey"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    provider = OpenAICompatibleProvider(
        "http://api.test/v1", "secret", client=client_for(handler)
    )
    resp = call(provider, json_mode=True)

    assert (resp.text, resp.input_tokens, resp.output_tokens) == ("hey", 5, 2)
    assert seen[0][0] == "Bearer secret"
    assert seen[0][1]["response_format"] == {"type": "json_object"}


def test_openai_compatible_missing_usage_defaults_to_zero():
    provider = OpenAICompatibleProvider(
        "http://api.test/v1",
        "k",
        client=client_for(
            lambda r: httpx.Response(
                200, json={"choices": [{"message": {"content": "x"}}]}
            )
        ),
    )
    resp = call(provider)
    assert (resp.input_tokens, resp.output_tokens) == (0, 0)


def test_openai_compatible_bad_status_becomes_llm_error():
    provider = OpenAICompatibleProvider(
        "http://api.test/v1",
        "k",
        client=client_for(lambda r: httpx.Response(401, text="nope")),
    )
    with pytest.raises(LLMError):
        call(provider)