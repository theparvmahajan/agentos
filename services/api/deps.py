from collections.abc import Callable
from functools import lru_cache, partial
from typing import Annotated
from uuid import UUID

from fastapi import Depends

from core.pipeline import plan_and_publish
from core.queue import build_queues

Launcher = Callable[[UUID], None]


def get_database_url() -> str | None:
    """None means 'use the configured DATABASE_URL'. Tests override this."""
    return None


@lru_cache
def _default_launcher() -> Launcher:
    # Built once and shared. The API only plans: workers run the tasks, so the API process
    # needs no agents and no tools, just the LLM (for planning) and Redis (to wake the scheduler).
    return partial(plan_and_publish, queues=build_queues())


def get_launcher() -> Launcher:
    """What starts a mission in the background. Tests override this with a recorder."""
    return _default_launcher()


# Use these in endpoint signatures: `database_url: DatabaseUrl`, `launcher: LauncherDep`.
DatabaseUrl = Annotated[str | None, Depends(get_database_url)]
LauncherDep = Annotated[Launcher, Depends(get_launcher)]
