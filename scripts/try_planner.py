import sys

from core import repository as repo
from core.db import get_conn
from core.planner import run_planner

DEFAULT = "Compare PostgreSQL and MySQL for a small startup and summarize the tradeoffs"


def main() -> None:
    objective = " ".join(sys.argv[1:]) or DEFAULT
    with get_conn() as conn:
        mission = repo.create_mission(conn, objective)
    print(f"mission {mission.id}\nobjective: {objective}\n")

    tasks = run_planner(mission.id)

    key_by_id = {t.id: t.key for t in tasks}
    for t in tasks:
        deps = [key_by_id[d] for d in t.depends_on]
        print(f"{t.key}  depends_on={deps}\n    {t.instruction}")


if __name__ == "__main__":
    main()