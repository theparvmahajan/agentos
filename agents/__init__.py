from agents.researcher import run_researcher
from core.runner import Runner


def build_default_runners() -> dict[str, Runner]:
    """Maps an agent's registry name to the code that does its work."""
    return {"researcher": run_researcher}