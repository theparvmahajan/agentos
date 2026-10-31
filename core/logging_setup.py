import logging


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx logs one INFO line per request, which drowns out our own progress lines.
    logging.getLogger("httpx").setLevel(logging.WARNING)