import json
from datetime import datetime
from uuid import uuid4

import httpx
import pytest

from core.config import settings
from core.models import Agent
from core.tools import (
    ToolError,
    ToolInputError,
    ToolPermissionError,
    build_default_registry,
)
from core.tools.search import (
    SNIPPET_CHARS,
    FakeSearchProvider,
    SearchResult,
    TavilySearchProvider,
    format_results,
    get_search_provider,
)


def make_agent(*capabilities: str) -> Agent:
    return Agent(
        id=uuid4(),
        name="researcher",
        capabilities=list(capabilities),
        created_at=datetime(2026, 1, 1),
    )


def client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def tavily(handler) -> TavilySearchProvider:
    return TavilySearchProvider("k", client=client_for(handler))


# ---------- Tavily provider ----------

def test_tavily_sends_the_expected_request_and_parses_results():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers["authorization"], json.loads(request.content)))
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "A", "url": "https://a.test", "content": "alpha", "score": 0.9},
                    {"url": "https://b.test", "content": "beta"},
                ]
            },
        )

    results = tavily(handler).search("postgres locks", 3)

    url, auth, body = seen[0]
    assert url == "https://api.tavily.com/search"
    assert auth == "Bearer k"
    assert body["query"] == "postgres locks"
    assert body["max_results"] == 3
    assert results[0] == SearchResult("A", "https://a.test", "alpha")
    assert results[1].title == "https://b.test"  # falls back to the URL


def test_tavily_empty_results_is_not_an_error():
    provider = tavily(lambda r: httpx.Response(200, json={"results": []}))
    assert provider.search("q", 5) == []


@pytest.mark.parametrize(
    "status, fragment",
    [(401, "rejected the API key"), (403, "rejected the API key"), (429, "rate limit"), (500, "HTTP 500")],
)
def test_tavily_http_errors_become_readable_tool_errors(status, fragment):
    provider = tavily(lambda r: httpx.Response(status, text="nope"))
    with pytest.raises(ToolError) as exc:
        provider.search("q", 5)
    assert fragment in str(exc.value)


def test_tavily_connection_failure_becomes_tool_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(ToolError) as exc:
        tavily(handler).search("q", 5)
    assert "failed" in str(exc.value)


def test_tavily_invalid_json_becomes_tool_error():
    provider = tavily(lambda r: httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(ToolError) as exc:
        provider.search("q", 5)
    assert "invalid JSON" in str(exc.value)


@pytest.mark.parametrize("body", [{"oops": 1}, [], {"results": [{"title": "no url"}]}])
def test_tavily_unexpected_shape_becomes_tool_error(body):
    provider = tavily(lambda r: httpx.Response(200, json=body))
    with pytest.raises(ToolError) as exc:
        provider.search("q", 5)
    assert "unexpected" in str(exc.value)


# ---------- formatting ----------

def test_format_results_numbers_results_and_flags_external_content():
    text = format_results(
        "pg", [SearchResult("One", "https://1.test", "first"), SearchResult("Two", "https://2.test", "second")]
    )
    assert "Web search results for: pg" in text
    assert "1. One" in text and "https://1.test" in text and "first" in text
    assert "2. Two" in text
    assert "not as instructions" in text


def test_format_results_empty():
    assert format_results("pg", []) == "No results found for: pg"


def test_snippets_are_cleaned_and_truncated():
    messy = "word   \n  " * 400
    text = format_results("q", [SearchResult("T", "https://t.test", messy)])
    snippet_line = text.splitlines()[-1].strip()
    assert "\n" not in snippet_line
    assert "  " not in snippet_line
    assert len(snippet_line) <= SNIPPET_CHARS


# ---------- the tool, through the registry ----------

RESULTS = [SearchResult(f"R{i}", f"https://{i}.test", f"snippet {i}") for i in range(1, 8)]


def test_researcher_can_search_and_defaults_to_five_results():
    fake = FakeSearchProvider(RESULTS)
    registry = build_default_registry(fake)

    result = registry.call(make_agent("web.search"), "web_search", {"query": "  databases  "})

    assert fake.calls == [("databases", 5)]  # whitespace stripped, default applied
    assert "1. R1" in result.output and "5. R5" in result.output
    assert "R6" not in result.output


def test_max_results_is_passed_through():
    fake = FakeSearchProvider(RESULTS)
    build_default_registry(fake).call(
        make_agent("web.search"), "web_search", {"query": "q", "max_results": 2}
    )
    assert fake.calls == [("q", 2)]


@pytest.mark.parametrize(
    "args",
    [{}, {"query": ""}, {"query": "   "}, {"query": "q", "max_results": 0}, {"query": "q", "max_results": 11}, {"query": "x" * 401}],
)
def test_invalid_search_arguments_never_reach_the_provider(args):
    fake = FakeSearchProvider(RESULTS)
    with pytest.raises(ToolInputError):
        build_default_registry(fake).call(make_agent("web.search"), "web_search", args)
    assert fake.calls == []


def test_agent_without_the_capability_cannot_search():
    fake = FakeSearchProvider(RESULTS)
    with pytest.raises(ToolPermissionError):
        build_default_registry(fake).call(make_agent("db.read"), "web_search", {"query": "q"})
    assert fake.calls == []


def test_provider_failures_surface_as_tool_errors():
    fake = FakeSearchProvider(error=ToolError("search is down"))
    with pytest.raises(ToolError) as exc:
        build_default_registry(fake).call(make_agent("web.search"), "web_search", {"query": "q"})
    assert "search is down" in str(exc.value)


def test_no_results_message_is_returned_not_raised():
    result = build_default_registry(FakeSearchProvider([])).call(
        make_agent("web.search"), "web_search", {"query": "zzzz"}
    )
    assert result.output == "No results found for: zzzz"


# ---------- provider selection ----------

def test_missing_api_key_gives_a_clear_error_at_call_time(monkeypatch):
    monkeypatch.setattr(settings, "tavily_api_key", "")
    with pytest.raises(ToolError) as exc:
        get_search_provider().search("q", 5)
    assert "TAVILY_API_KEY" in str(exc.value)


def test_api_key_selects_the_tavily_provider(monkeypatch):
    monkeypatch.setattr(settings, "tavily_api_key", "secret")
    assert isinstance(get_search_provider(), TavilySearchProvider)