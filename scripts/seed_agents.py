from core import repository as repo
from core.db import get_conn

AGENTS = [
    {
        "name": "researcher",
        "description": "Searches the web and summarizes findings into concise notes.",
        "capabilities": ["web.search"],
        "timeout_seconds": 90,
    },
]


def main() -> None:
    with get_conn() as conn:
        for spec in AGENTS:
            if repo.get_agent_by_name(conn, spec["name"]) is None:
                repo.create_agent(conn, **spec)
                print(f"created agent: {spec['name']}")
            else:
                print(f"already exists: {spec['name']}")


if __name__ == "__main__":
    main()