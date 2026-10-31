from core.models import TaskStatus
from core.state import TASK_TERMINAL, TASK_TRANSITIONS, allowed_sources


def test_every_status_has_a_transition_entry():
    assert set(TASK_TRANSITIONS) == set(TaskStatus)


def test_terminal_states_have_no_exits():
    for status in TASK_TERMINAL:
        assert TASK_TRANSITIONS[status] == set()


def test_running_can_return_to_ready_for_retry():
    assert TaskStatus.RUNNING in allowed_sources(TaskStatus.READY)