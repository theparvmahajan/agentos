from core.tools.python_exec import CodeRunner, run_python_tool
from core.tools.registry import ToolRegistry
from core.tools.search import SearchProvider, get_search_provider, web_search_tool


def build_default_registry(
    search_provider: SearchProvider | None = None,
    code_runner: CodeRunner | None = None,
) -> ToolRegistry:
    """Pass `code_runner` to choose how code runs (tests do). Otherwise CODE_EXEC_BACKEND decides."""
    registry = ToolRegistry()
    registry.register(web_search_tool(search_provider or get_search_provider()))
    registry.register(run_python_tool(code_runner))
    return registry