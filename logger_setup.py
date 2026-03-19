"""Logging configuration for the OptionsBot.

On Railway/cloud: logs to stdout only (Railway captures stdout).
Locally: logs to both stdout and bot.log file.
"""

import logging
import os
import sys
from pathlib import Path


def setup_logger(name: str = "optionsbot", log_file: str = "bot.log") -> logging.Logger:
    """Configure and return a logger.

    Detects Railway environment automatically and skips file logging.
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler (INFO and above)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler only when running locally (not on Railway)
    if not os.getenv("RAILWAY_ENVIRONMENT"):
        try:
            log_path = Path(__file__).parent / log_file
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except (OSError, PermissionError):
            pass  # Skip file logging if we can't write

    return logger
