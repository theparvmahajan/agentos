import random

import httpx
import pytest

from core.config import settings
from core.llm.base import LLMError
from core.llm.providers import OllamaProvider, OpenAICompatibleProvider
from core.retry import Backoff, TaskTimeout, is_retryable

# ---------- the delay ----------

def test_the_delay_doubles_each_attempt_until_the_cap():
    b = Backoff(base=2, cap=60)
    # The ceiling for attempts 1..8 is 2, 4, 8, 16, 32, 60, 60, 60
    for attempt, ceiling in enumerate([2, 4, 8, 16, 32, 60, 60, 60], start=1):
        rng = random.Random(attempt)
        for _ in range(200):
            assert ceiling / 2 <= b.delay(attempt, rng) <= ceiling


def test_a_retry_is_always_really_delayed():
    """Equal jitter: at least half the ceiling is fixed, so the delay can never be about zero."""
    rng = random.Random(0)
    assert min(Backoff(10, 100).delay(1, rng) for _ in range(1000)) >= 5


def test_the_random_half_spreads_simultaneous_retries_apart():
    rng = random.Random(0)
    delays = {round(Backoff(10, 100).delay(3, rng), 3) for _ in range(100)}
    assert len(delays) > 90  # not all the same


def test_the_same_seed_gives_the_same_delays():
    assert Backoff(2, 60).delay(4, random.Random(7)) == Backoff(2, 60).delay(4, random.Random(7))


@pytest.mark.parametrize("attempt", [0, -1])
def test_attempt_numbers_start_at_one(attempt):
    with pytest.raises(ValueError):
        Backoff(2, 60).delay(attempt)


def test_a_huge_attempt_number_does_not_overflow():
    assert Backoff(2, 60).delay(10_000) <= 60  # 2**9999 is fine for Python ints, and capped


def test_the_defaults_come_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "retry_base_seconds", 3.0)
    monkeypatch.setattr(settings, "retry_max_seconds", 9.0)
    assert Backoff.from_settings() == Backoff(3.0, 9.0)


# ---------- which failures are retried ----------

def test_only_failures_that_say_they_are_transient_are_retried():
    assert is_retryable(TaskTimeout("slow"))
    assert is_retryable(LLMError("busy", retryable=True))
    assert not is_retryable(LLMError("bad key"))  # the default is: do not retry
    assert not is_retryable(ValueError("a bug"))
    assert not is_retryable(KeyError("also a bug"))


def test_the_flag_must_be_exactly_true():
    class Odd(Exception):
        retryable = "yes"

    assert not is_retryable(Odd())


# ---------- provider errors are classified ----------

def ollama_answering(status=None, error=None, content=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if error:
            raise error
        return httpx.Response(status, content=content or b"{}", request=request)

    return OllamaProvider("http://ollama.test", client=httpx.Client(transport=httpx.MockTransport(handler)))


def failure_of(provider):
    with pytest.raises(LLMError) as caught:
        provider.complete(prompt="hi", system=None, model="m", json_mode=False)
    return caught.value


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_rate_limits_and_server_errors_are_retryable(status):
    assert failure_of(ollama_answering(status)).retryable is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_our_own_mistakes_are_not_retryable(status):
    assert failure_of(ollama_answering(status)).retryable is False


@pytest.mark.parametrize("error", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
def test_connection_failures_and_timeouts_are_retryable(error):
    assert failure_of(ollama_answering(error=error)).retryable is True


def test_an_unreadable_reply_is_not_retryable():
    assert failure_of(ollama_answering(200, content=b"<html>not json")).retryable is False


def test_the_openai_compatible_provider_classifies_the_same_way():
    def handler(request):
        return httpx.Response(503, content=b"overloaded", request=request)

    provider = OpenAICompatibleProvider(
        "http://api.test", "key", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert failure_of(provider).retryable is True
