"""Connections must be bounded: nothing waits forever for a server that has gone quiet."""

import psycopg

from core import db
from core.config import settings
from core.queue import make_redis


def test_database_connections_have_a_connect_timeout_and_tcp_keepalives(monkeypatch):
    seen = {}

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(psycopg, "connect", lambda url, **kwargs: seen.update(kwargs) or FakeConn())
    with db.get_conn("postgresql://x"):
        pass

    assert seen["connect_timeout"] == settings.db_connect_timeout_seconds
    assert seen["keepalives"] == 1
    assert seen["tcp_user_timeout"] == int(settings.db_network_timeout_seconds * 1000)
    assert seen["keepalives_count"] * seen["keepalives_interval"] <= settings.db_network_timeout_seconds


def test_the_redis_client_has_timeouts_and_no_hidden_retries():
    kwargs = make_redis("redis://localhost:6379/0").get_connection_kwargs()
    assert kwargs["socket_timeout"] == settings.redis_socket_timeout_seconds
    assert kwargs["socket_connect_timeout"] == settings.redis_connect_timeout_seconds
    assert kwargs["retry"]._retries == 0  # our own loops retry; a second layer only delays them


def test_the_read_timeout_is_longer_than_the_longest_blocking_read():
    # XREADGROUP blocks on the server for block_ms (default 1000 ms); the client must outwait it.
    assert settings.redis_socket_timeout_seconds > 2
