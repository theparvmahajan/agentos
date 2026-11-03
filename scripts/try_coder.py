"""Run the Coder agent once against your real LLM. No database or queue needed.

    python -m scripts.try_coder "Compute the 20th Fibonacci number"

Costs a few LLM calls (up to 8). It really executes the code the model writes, on THIS
machine, so read the printed code. run_python is not a sandbox (see core/tools/python_exec.py).
"""
import sys
from datetime import datetime
from uuid import uuid4

from agents import run_coder
from core.logging_setup import configure_logging
from core.models import Agent
from core.tools import build_default_registry

DEFAULT = "Compute the 20th Fibonacci number and print it."


def main() -> None:
    configure_logging()
    instruction = " ".join(sys.argv[1:]) or DEFAULT
    agent = Agent(
        id=uuid4(), name="coder", capabilities=["code.execute"], created_at=datetime.now()
    )
    result = run_coder(agent, instruction, [], tools=build_default_registry())
    print(f"\n--- coder finished: {result.steps} steps, {result.tool_calls} tool calls, "
          f"{result.input_tokens}+{result.output_tokens} tokens, {result.llm_latency_ms} ms in LLM ---\n")
    print(result.output)


if __name__ == "__main__":
    main()
