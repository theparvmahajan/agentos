from core import repository as repo
from core.db import get_conn

AGENTS = [
    {
        "name": "researcher",
        "description": "Searches the web and summarizes findings into concise notes.",
        "capabilities": ["web.search"],
        "timeout_seconds": 90,
    },
    {
        "name": "coder",
        "description": (
            "Writes and runs small self-contained Python programs (standard library only, "
            "no internet) and reports the code with its verified output. "
            "Use it for calculations, data processing and small algorithms."
        ),
        "capabilities": ["code.execute"],
        "timeout_seconds": 120,
    },
    {
        "name": "reviewer",
        "description": (
            "Critically checks the output of earlier tasks (research notes, code and its "
            "output) and answers VERDICT: APPROVE or REJECT with concrete findings. "
            "Has no tools, so it MUST depend on the task(s) it reviews. Use it last."
        ),
        "capabilities": [],
        "timeout_seconds": 60,
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
