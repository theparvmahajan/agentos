import os
import signal
import socket
import threading

from agents import build_default_runners
from core.logging_setup import configure_logging
from core.queue import build_queues
from core.tools import build_runtime_registry
from core.worker import run_worker


def main() -> None:
    configure_logging()
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    # Stable names mean a restarted worker picks up the messages it never finished.
    # To run several processes on one machine, give each its own WORKER_NAME.
    base = os.environ.get("WORKER_NAME") or f"worker@{socket.gethostname()}"
    concurrency = int(os.environ.get("WORKER_CONCURRENCY", "1"))

    queues = build_queues()
    runners = build_default_runners()
    tools = build_runtime_registry()  # policy + audit trail: a bad policy file stops the worker here

    threads = [
        threading.Thread(
            target=run_worker,
            args=(queues,),
            kwargs={"consumer": f"{base}-{i}", "stop": stop, "runners": runners, "tools": tools},
            name=f"worker-{i}",
        )
        for i in range(1, concurrency + 1)
    ]
    for t in threads:
        t.start()
    while not stop.wait(0.5):  # polling, so Ctrl+C is noticed on every platform
        pass
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()