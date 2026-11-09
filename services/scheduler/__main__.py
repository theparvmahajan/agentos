import os
import signal
import socket
import threading

from core.config import settings
from core.logging_setup import configure_logging
from core.queue import build_queues
from core.scheduler import run_scheduler


def main() -> None:
    configure_logging()
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    # A stable name means a restarted scheduler picks up the events it never acknowledged.
    # To run several on one machine, give each its own SCHEDULER_NAME.
    consumer = os.environ.get("SCHEDULER_NAME") or f"scheduler@{socket.gethostname()}"
    run_scheduler(
        build_queues(),
        consumer=consumer,
        stop=stop,
        reap_interval=settings.reaper_interval_seconds,
        sweep_interval=settings.sweep_interval_seconds,
    )


if __name__ == "__main__":
    main()