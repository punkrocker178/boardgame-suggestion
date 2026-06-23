import logging


def configure_logging(level: str = "INFO") -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        force=True,
    )
    logging.getLogger("app").setLevel(log_level)

    if log_level <= logging.DEBUG:
        logging.getLogger("langchain").setLevel(logging.DEBUG)
        logging.getLogger("langchain_core").setLevel(logging.DEBUG)
