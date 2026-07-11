"""Logging configuration for wildcard_manager."""

import logging
import logging.handlers
from pathlib import Path


def setup_logging(app_dir: Path) -> None:
    """Initialize logging with rotating file handler."""
    log_dir = app_dir / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "app.log"

    root_logger = logging.getLogger("wildcard_manager")
    root_logger.setLevel(logging.DEBUG)

    handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8"
    )
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)
