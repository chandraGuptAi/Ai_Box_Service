import logging

from app.core.config import get_settings


def setup_logging() -> None:
    logging.basicConfig(
        level=get_settings().LOG_LEVEL,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
