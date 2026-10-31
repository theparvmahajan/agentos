from uuid import UUID

from core.models import TaskStatus as T
from core.readiness import TaskNode, plan_next

# a -> b -> d
# a -> c -> d
#      c -> e
DEPS = {"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"], "e": ["c"]}
ID = {name: UUID(int=i + 1) for i, name in enumerate(DEPS)}
NAME = {v: k for k, v in ID.items()}
status = {name: T.PENDING for name in DEPS}


def show(event: str) -> None:
    nodes = [TaskNode(ID[n], status[n], frozenset(ID[d] for d in deps)) for n, deps in DEPS.items()]
    d = plan_next(nodes)
    ready = [NAME[i] for i in d.ready]
    skip = [NAME[i] for i in d.skip]
    states = " ".join(f"{n}={status[n].value[:4]}" for n in DEPS)
    print(f"{event:<26} {states}\n{'':<26} -> ready={ready} skip={skip} finished={d.finished}\n")
    for i in d.skip:  # apply the skips, as a scheduler would
        status[NAME[i]] = T.SKIPPED


show("start")
status["a"] = T.COMPLETED
show("a completes")
status["b"] = T.RUNNING
status["c"] = T.RUNNING
show("b and c are running")
status["c"] = T.FAILED
show("c FAILS")
status["b"] = T.COMPLETED
show("b completes")