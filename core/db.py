from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from core.config import settings


@contextmanager
def get_conn(database_url: str | None = None) -> Iterator[psycopg.Connection]:
    with psycopg.connect(database_url or settings.database_url, row_factory=dict_row) as conn:
        yield conn