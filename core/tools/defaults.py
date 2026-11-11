from pathlib import Path

from core.audit import AuditSink, PgAuditSink
from core.config import settings
from core.policy import Policy, load_policy
from core.tools.python_exec import CodeRunner, run_python_tool
from core.tools.registry import ToolRegistry
from core.tools.search import SearchProvider, get_search_provider, web_search_tool

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_configured_policy() -> Policy:
    """The policy named by POLICY_FILE (relative paths start at the project root).
    Raises PolicyError if it is missing or invalid, so a bad policy stops the process."""
    path = Path(settings.policy_file)
    return load_policy(path if path.is_absolute() else PROJECT_ROOT / path)


def build_default_registry(
    search_provider: SearchProvider | None = None,
    code_runner: CodeRunner | None = None,
    *,
    policy: Policy | None = None,
    audit: AuditSink | None = None,
) -> ToolRegistry:
    """The standard tools under the configured policy. Pass `code_runner` to choose how code
    runs (tests do); otherwise CODE_EXEC_BACKEND decides. `audit` is optional here so tests need
    no database; processes that run real agents use build_runtime_registry."""
    registry = ToolRegistry(policy=policy or load_configured_policy(), audit=audit)
    registry.register(web_search_tool(search_provider or get_search_provider()))
    registry.register(run_python_tool(code_runner))
    return registry


def build_runtime_registry(database_url: str | None = None) -> ToolRegistry:
    """What the worker uses: the standard tools, the configured policy, and the audit trail
    written to Postgres. Nothing is optional here."""
    return build_default_registry(audit=PgAuditSink(database_url))
