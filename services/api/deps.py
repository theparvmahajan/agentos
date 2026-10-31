from collections.abc import Callable
from functools import lru_cache, partial
from typing import Annotated
from uuid import UUID

from fastapi import Depends

from agents import build_default_runners
from core.pipeline import process_mission
from core.tools import build_default_registry

Processor = Callable[[UUID], None]


def get_database_url() -> str | None:
    """None means 'use the configured DATABASE_URL'. Tests override this."""
    return None


@lru_cache
def _default_processor() -> Processor:
    # Built once and shared: the tool registry holds an HTTP client that is safe to reuse.
    return partial(
        process_mission, runners=build_default_runners(), tools=build_default_registry()
    )


def get_processor() -> Processor:
    """What runs a mission in the background. Tests override this with a recorder."""
    return _default_processor()


# Use these in endpoint signatures: `database_url: DatabaseUrl`, `processor: ProcessorDep`.
DatabaseUrl = Annotated[str | None, Depends(get_database_url)]
ProcessorDep = Annotated[Processor, Depends(get_processor)]