import pytest
from pydantic import ValidationError

from core.config import Settings


def test_model_name_whitespace_is_stripped():
    s = Settings(_env_file=None, llm_model="  qwen2.5:7b ", llm_provider=" ollama ")
    assert s.llm_model == "qwen2.5:7b"
    assert s.llm_provider == "ollama"


def test_no_secret_defaults(monkeypatch):
    for name in ("TAVILY_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    s = Settings(_env_file=None)
    assert s.tavily_api_key == ""
    assert s.openai_api_key == ""


def test_code_runs_in_docker_unless_you_choose_otherwise():
    s = Settings(_env_file=None)
    assert s.code_exec_backend == "docker"
    assert Settings(_env_file=None, code_exec_backend=" local ").code_exec_backend == "local"


def test_lease_defaults_leave_room_for_a_missed_heartbeat():
    s = Settings(_env_file=None)
    assert s.task_lease_seconds >= 2 * s.lease_heartbeat_seconds


@pytest.mark.parametrize(
    "lease, heartbeat",
    [(10, 10), (10, 6), (10, 5.1)],  # a heartbeat slower than half the lease expires healthy workers
)
def test_a_heartbeat_that_is_too_slow_for_the_lease_is_refused_at_startup(lease, heartbeat):
    with pytest.raises(ValidationError, match="at most half"):
        Settings(_env_file=None, task_lease_seconds=lease, lease_heartbeat_seconds=heartbeat)


def test_a_heartbeat_of_exactly_half_the_lease_is_accepted():
    s = Settings(_env_file=None, task_lease_seconds=10, lease_heartbeat_seconds=5)
    assert s.lease_heartbeat_seconds == 5


def test_the_reaper_interval_must_be_positive():
    with pytest.raises(ValidationError, match="REAPER_INTERVAL_SECONDS"):
        Settings(_env_file=None, reaper_interval_seconds=0)
