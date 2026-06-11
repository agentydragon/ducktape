from __future__ import annotations

import logging


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(datefmt="%Y-%m-%dT%H:%M:%S%z", format="%(asctime)s [%(levelname)s] %(message)s", level=level)
