from core.tools.registry import ToolRegistry
from core.tools.search import SearchProvider, get_search_provider, web_search_tool


def build_default_registry(search_provider: SearchProvider | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(web_search_tool(search_provider or get_search_provider()))
    return registry