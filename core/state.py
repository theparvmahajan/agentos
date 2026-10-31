from core.models import MissionStatus, TaskStatus as T

TASK_TRANSITIONS: dict[T, set[T]] = {
    T.PENDING: {T.READY, T.SKIPPED, T.CANCELLED},
    T.READY: {T.RUNNING, T.SKIPPED, T.CANCELLED},
    T.RUNNING: {T.COMPLETED, T.FAILED, T.READY, T.CANCELLED},  # READY = retry
    T.COMPLETED: set(),
    T.FAILED: set(),
    T.SKIPPED: set(),
    T.CANCELLED: set(),
}

TASK_TERMINAL = {T.COMPLETED, T.FAILED, T.SKIPPED, T.CANCELLED}
MISSION_TERMINAL = {
    MissionStatus.COMPLETED,
    MissionStatus.FAILED,
    MissionStatus.CANCELLED,
}


class InvalidTransition(Exception):
    pass


def allowed_sources(target: T) -> list[T]:
    """Statuses a task may move FROM to reach `target`."""
    return [src for src, targets in TASK_TRANSITIONS.items() if target in targets]