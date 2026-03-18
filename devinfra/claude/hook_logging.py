"""Logging infrastructure for hook processes.

Configures root logger with file + optional handlers. Used by session_start
and potentially other hook entry points.
"""

import logging
import sys
from pathlib import Path


def setup_file_logging(log_file: Path, *, print_banner: bool = True) -> None:
    """Configure root logger with a file handler.

    All child loggers (proxy_setup, bazelisk_setup, etc.) inherit this config.
    Logs go to file only — stdout is reserved for structured agent context.
    """
    formatter = logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    file_handler = logging.FileHandler(log_file, mode="a")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    if print_banner:
        print(f"Setup log: {log_file}", file=sys.stderr)
