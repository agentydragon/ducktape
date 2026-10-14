"""The `log_levels` setting every service entry point takes."""

from enum import StrEnum
from typing import Annotated

from pydantic import Field


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


LogLevels = Annotated[
    dict[str, LogLevel],
    Field(
        default_factory=dict,
        description="Logger name to level, `root` for the root logger; loggers not named stay at `INFO`. "
        "`kubernetes_asyncio.client.rest` never goes below `INFO`: its DEBUG output carries bearer tokens.",
    ),
]
