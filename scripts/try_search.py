import sys

from core import repository as repo
from core.db import get_conn
from core.tools import ToolError, ToolPermissionError, build_default_registry

DEFAULT = "What is SKIP LOCKED in PostgreSQL used for?"


def main() -> None:
    query = " ".join(sys.argv[1:]) or DEFAULT

    with get_conn() as conn:
        agent = repo.get_agent_by_name(conn, "researcher")
    if agent is None:
        raise SystemExit("researcher agent not found: run `make seed` first")

    registry = build_default_registry()

    print(f"tools available to '{agent.name}': {[t.name for t in registry.tools_for(agent)]}\n")
    try:
        result = registry.call(agent, "web_search", {"query": query, "max_results": 3})
    except ToolError as e:
        raise SystemExit(f"search failed: {e}") from e
    print(result.output)
    print(f"\n[{result.tool}] {result.latency_ms}ms")

    print("\n--- same call, but with the agent's capabilities removed ---")
    powerless = agent.model_copy(update={"capabilities": []})
    try:
        registry.call(powerless, "web_search", {"query": query})
    except ToolPermissionError as e:
        print(f"blocked: {e}")


if __name__ == "__main__":
    main()