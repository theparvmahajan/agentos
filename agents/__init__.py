from agents.coder import run_coder
from agents.researcher import run_researcher
from agents.reviewer import run_reviewer
from core.runner import Runner
from agents.sleeper import run_sleeper

def build_default_runners() -> dict[str, Runner]:
    """Maps an agent's registry name to the code that does its work."""
    return {
        "researcher": run_researcher,
        "coder": run_coder,
        "reviewer": run_reviewer,
        "sleeper": run_sleeper,
    }