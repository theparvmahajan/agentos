"""Failures we have PROVEN we do not survive yet. Each is an expected failure (xfail, strict).

They stay in the suite on purpose: the gap is documented by a test that fails today, and the day
it is fixed the test starts passing, which strict=True turns into a loud reminder to remove the
marker and update docs/RELIABILITY.md.
"""

import contextlib
import threading

import pytest

from core.db import get_conn
from tests.chaos.proxy import BLACKHOLE, RESET, through


@pytest.mark.xfail(
    strict=True,
    reason="a Postgres connection that stays open but never answers (a middlebox that swallows "
    "traffic) hangs the caller: only connecting and a vanished network are bounded",
)
def test_a_silent_database_connection_does_not_hang_the_caller_forever(test_db_url, db_proxy):
    url = through(test_db_url, db_proxy)
    finished = threading.Event()

    def query():
        try:
            with contextlib.suppress(Exception), get_conn(url) as conn:  # any error is fine:
                conn.execute("SELECT pg_sleep(0.5)")  # starts while the connection is healthy
                db_proxy.set_mode(BLACKHOLE)
                conn.execute("SELECT 1")  # the reply to this one never arrives
        finally:
            finished.set()

    thread = threading.Thread(target=query, daemon=True)
    thread.start()
    try:
        assert finished.wait(8), "the call is still hanging after 8 s"
    finally:
        db_proxy.set_mode(RESET)  # cuts the connection, which finally frees the stuck thread
        thread.join(5)
