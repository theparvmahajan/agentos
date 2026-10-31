import json
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core import repository as repo
from core.dag import CycleError, topological_order
from core.db import get_conn
from core.jsonutil import JSONExtractError, extract_json
from core.llm import LLMProvider, TaskType, generate
from core.models import Agent, MissionStatus, Task

MAX_TASKS = 10


class PlanValidationError(Exception):
    """The model's output was not a usable plan. Safe to ask the model to fix it."""


class PlanningError(Exception):
    """Planning failed for good (e.g. invalid output after every repair attempt)."""


class PlanTask(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    key: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    agent: str = Field(min_length=1)
    instruction: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)


class Plan(BaseModel):
    tasks: list[PlanTask] = Field(min_length=1, max_length=MAX_TASKS)


def build_system_prompt(agents: list[Agent]) -> str:
    agent_lines = "\n".join(
        f"- {a.name}: {a.description or 'no description'}"
        + (f" (capabilities: {', '.join(a.capabilities)})" if a.capabilities else "")
        for a in agents
    )
    example = json.dumps(
        {
            "tasks": [
                {"key": "t1", "agent": agents[0].name, "instruction": "...", "depends_on": []},
                {"key": "t2", "agent": agents[0].name, "instruction": "...", "depends_on": ["t1"]},
            ]
        },
        indent=2,
    )
    return (
        "You are the planning component of a multi-agent runtime. "
        "Break the user's objective into a small set of tasks and assign each "
        "task to exactly one agent.\n\n"
        f"Available agents:\n{agent_lines}\n\n"
        "Respond with ONLY a JSON object in exactly this shape:\n"
        f"{example}\n\n"
        "Rules:\n"
        "- key: a short unique id such as t1, t2, t3.\n"
        "- agent: must be exactly one of the agent names listed above.\n"
        "- instruction: a complete, self-contained description of the work. "
        "The agent sees only this text and the outputs of its dependencies.\n"
        "- depends_on: keys of tasks that must finish first. Only add a "
        "dependency when a task needs another task's output.\n"
        "- Tasks that do not need each other's output must NOT depend on each "
        "other, so they can run in parallel.\n"
        f"- Use between 1 and {MAX_TASKS} tasks. No cycles.\n"
        "- Output the JSON object only: no markdown, no commentary."
    )


def parse_plan(text: str, agent_names: set[str]) -> Plan:
    """Parse and fully validate model output. Returns tasks in dependency order."""
    try:
        data = extract_json(text)
    except JSONExtractError as e:
        raise PlanValidationError("output was not valid JSON") from e

    try:
        plan = Plan.model_validate(data)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()
        )
        raise PlanValidationError(f"schema errors: {problems}") from e

    keys = [t.key for t in plan.tasks]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    if dupes:
        raise PlanValidationError(f"duplicate task keys: {', '.join(dupes)}")

    for t in plan.tasks:
        if t.agent not in agent_names:
            raise PlanValidationError(
                f"task {t.key} uses unknown agent '{t.agent}'; "
                f"valid agents: {', '.join(sorted(agent_names))}"
            )
        if t.key in t.depends_on:
            raise PlanValidationError(f"task {t.key} depends on itself")
        unknown = [d for d in t.depends_on if d not in keys]
        if unknown:
            raise PlanValidationError(
                f"task {t.key} depends on unknown task(s): {', '.join(unknown)}"
            )

    by_key = {t.key: t for t in plan.tasks}
    try:
        order = topological_order({t.key: set(t.depends_on) for t in plan.tasks})
    except CycleError as e:
        raise PlanValidationError(str(e)) from e
    return Plan(tasks=[by_key[k] for k in order])


def plan_mission(
    objective: str,
    agents: list[Agent],
    *,
    provider: LLMProvider | None = None,
    max_repairs: int = 1,
) -> Plan:
    """Ask the LLM for a plan. If the output is invalid, show it the error and ask again.

    This is a repair loop for bad *output*. It is not a retry for failed *calls*:
    an LLMError propagates immediately (retrying calls is the scheduler's job).
    """
    if not agents:
        raise PlanningError("no agents are registered")

    names = {a.name for a in agents}
    system = build_system_prompt(agents)
    base_prompt = f"Objective:\n{objective}"
    prompt = base_prompt
    last_error = ""

    for _ in range(max_repairs + 1):
        response = generate(
            prompt, TaskType.PLANNING, system=system, json_mode=True, provider=provider
        )
        try:
            return parse_plan(response.text, names)
        except PlanValidationError as e:
            last_error = str(e)
            prompt = (
                f"{base_prompt}\n\n"
                f"Your previous answer:\n{response.text[:2000]}\n\n"
                f"It was rejected: {last_error}\n"
                "Return the corrected JSON object only."
            )

    raise PlanningError(
        f"planner output invalid after {max_repairs + 1} attempts: {last_error}"
    )


def store_plan(conn, mission_id: UUID, plan: Plan, agents: list[Agent]) -> list[Task]:
    """Write a validated plan to Postgres. Runs inside the caller's transaction."""
    agent_by_name = {a.name: a for a in agents}
    task_ids: dict[str, UUID] = {}
    created: list[Task] = []
    # Plan tasks are in dependency order, so each dependency already exists.
    for pt in plan.tasks:
        task = repo.create_task(
            conn,
            mission_id,
            pt.key,
            agent_by_name[pt.agent].id,
            pt.instruction,
            depends_on=[task_ids[k] for k in pt.depends_on],
        )
        task_ids[pt.key] = task.id
        created.append(task)
    return created


def run_planner(
    mission_id: UUID,
    *,
    provider: LLMProvider | None = None,
    database_url: str | None = None,
) -> list[Task]:
    """pending -> planning -> (tasks stored) -> running, or -> failed."""
    with get_conn(database_url) as conn:
        mission = repo.get_mission(conn, mission_id)
        if mission is None:
            raise LookupError(f"mission {mission_id} not found")
        # Simple check; two simultaneous callers could both pass it.
        # Fine while one caller exists; hardened with proper claiming in M4.
        if mission.status != MissionStatus.PENDING:
            raise ValueError(f"mission {mission_id} is {mission.status.value}, expected pending")
        repo.set_mission_status(conn, mission_id, MissionStatus.PLANNING)
        agents = repo.list_agents(conn)
    # The block above committed. No connection is held open during the slow LLM call.

    try:
        plan = plan_mission(mission.objective, agents, provider=provider)
        with get_conn(database_url) as conn:  # tasks + status change commit together
            tasks = store_plan(conn, mission_id, plan, agents)
            repo.set_mission_status(conn, mission_id, MissionStatus.RUNNING)
    except Exception as e:
        with get_conn(database_url) as conn:
            repo.set_mission_status(
                conn, mission_id, MissionStatus.FAILED, error=f"planning failed: {e}"
            )
        raise
    return tasks