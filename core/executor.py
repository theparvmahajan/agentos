import logging
from uuid import UUID

from core import repository as repo
from core.dag import topological_order
from core.db import get_conn
from core.llm import LLMProvider
from core.models import Mission, MissionStatus, Task, TaskStatus
from core.readiness import nodes_from_tasks, plan_next
from core.results import compose_result, describe_failure
from core.runner import DependencyOutput, Runner
from core.tools import ToolRegistry

log = logging.getLogger(__name__)

ERROR_CHARS = 300


def _clip(text: str, limit: int = ERROR_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


def _set_task(database_url: str | None, task_id: UUID, status: TaskStatus, **kwargs) -> Task:
    # One short transaction per change: progress is committed (and visible) immediately.
    with get_conn(database_url) as conn:
        return repo.set_task_status(conn, task_id, status, **kwargs)


def _finish(
    database_url: str | None, mission_id: UUID, status: MissionStatus, **kwargs
) -> Mission:
    with get_conn(database_url) as conn:
        return repo.set_mission_status(conn, mission_id, status, **kwargs)


def execute_mission(
    mission_id: UUID,
    *,
    runners: dict[str, Runner],
    tools: ToolRegistry,
    provider: LLMProvider | None = None,
    database_url: str | None = None,
) -> Mission:
    """Run a planned mission's tasks one at a time, in dependency order."""
    with get_conn(database_url) as conn:
        mission = repo.get_mission(conn, mission_id)
        if mission is None:
            raise LookupError(f"mission {mission_id} not found")
        if mission.status != MissionStatus.RUNNING:
            raise ValueError(f"mission {mission_id} is {mission.status.value}, expected running")
        tasks = repo.get_tasks(conn, mission_id)
        agents = {a.id: a for a in repo.list_agents(conn)}

    if not tasks:
        return _finish(database_url, mission_id, MissionStatus.FAILED, error="mission has no tasks")
    if any(t.status != TaskStatus.PENDING for t in tasks):
        raise ValueError("mission has tasks that already started; resuming is not supported yet")

    by_id = {t.id: t for t in tasks}
    # Do not trust the order tasks come back from the database: derive it from the edges.
    graph = {str(t.id): {str(d) for d in t.depends_on} for t in tasks}
    order = [by_id[UUID(node)] for node in topological_order(graph)]

    status: dict[UUID, TaskStatus] = {t.id: TaskStatus.PENDING for t in tasks}
    outputs: dict[UUID, str] = {}
    errors: dict[UUID, str] = {}

    while True:
        decision = plan_next(nodes_from_tasks(tasks, status))

        for task_id in decision.skip:
            _set_task(database_url, task_id, TaskStatus.SKIPPED)
            status[task_id] = TaskStatus.SKIPPED
            log.info("task %s skipped (a dependency did not complete)", by_id[task_id].key)

        if decision.finished:
            break
        if not decision.ready:  # cannot happen: nothing is in flight between iterations
            raise RuntimeError("no task is ready but the mission is not finished")

        task = by_id[decision.ready[0]]  # sequential: one task at a time
        _set_task(database_url, task.id, TaskStatus.READY)
        _set_task(database_url, task.id, TaskStatus.RUNNING)
        log.info("task %s running", task.key)

        agent = agents[task.agent_id]
        try:
            runner = runners.get(agent.name)
            if runner is None:
                raise LookupError(f"no runner registered for agent '{agent.name}'")
            dependencies = [
                DependencyOutput(by_id[d].key, by_id[d].instruction, outputs[d])
                for d in task.depends_on
            ]
            result = runner(agent, task.instruction, dependencies, tools=tools, provider=provider)
        except Exception as e:  # noqa: BLE001 - any runner failure is recorded, not fatal
            message = f"{type(e).__name__}: {e}"
            _set_task(database_url, task.id, TaskStatus.FAILED, error=message)
            status[task.id] = TaskStatus.FAILED
            errors[task.id] = message
            log.warning("task %s failed: %s", task.key, _clip(message))
            continue

        _set_task(database_url, task.id, TaskStatus.COMPLETED, output=result.output)
        status[task.id] = TaskStatus.COMPLETED
        outputs[task.id] = result.output
        log.info(
            "task %s completed (%d steps, %d tool calls, %d+%d tokens)",
            task.key,
            result.steps,
            result.tool_calls,
            result.input_tokens,
            result.output_tokens,
        )

    failed = [t for t in order if status[t.id] == TaskStatus.FAILED]
    skipped = [t for t in order if status[t.id] == TaskStatus.SKIPPED]

    if not failed:  # tasks are only ever skipped after a failure, so none were skipped either
        log.info("mission %s completed", mission_id)
        return _finish(
            database_url, mission_id, MissionStatus.COMPLETED, result=compose_result(order, outputs)
        )

    message = describe_failure(failed, skipped, errors)
    log.warning("mission %s failed: %s", mission_id, message)
    return _finish(database_url, mission_id, MissionStatus.FAILED, error=message)