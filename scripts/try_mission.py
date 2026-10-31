import sys

from agents import build_default_runners
from core import repository as repo
from core.db import get_conn
from core.executor import execute_mission
from core.logging_setup import configure_logging
from core.planner import run_planner
from core.tools import build_default_registry

DEFAULT = "Compare PostgreSQL and MySQL for a small startup and summarize the tradeoffs"


def main() -> None:
    configure_logging()
    objective = " ".join(sys.argv[1:]) or DEFAULT

    with get_conn() as conn:
        mission = repo.create_mission(conn, objective)
    print(f"mission {mission.id}\nobjective: {objective}\n")

    run_planner(mission.id)
    mission = execute_mission(
        mission.id, runners=build_default_runners(), tools=build_default_registry()
    )

    with get_conn() as conn:
        tasks = repo.get_tasks(conn, mission.id)
    key_by_id = {t.id: t.key for t in tasks}

    print("\n--- tasks ---")
    for t in sorted(tasks, key=lambda t: t.started_at or t.created_at):
        deps = [key_by_id[d] for d in t.depends_on]
        print(f"{t.key}  {t.status.value}  attempts={t.attempts}  depends_on={deps}")
        if t.last_error:
            print(f"    error: {t.last_error}")

    print(f"\n--- mission {mission.status.value} ---")
    print(mission.result or mission.error)


if __name__ == "__main__":
    main()