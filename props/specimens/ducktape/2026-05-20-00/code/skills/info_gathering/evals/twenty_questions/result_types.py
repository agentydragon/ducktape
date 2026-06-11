"""Result types for Twenty Questions eval runs."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from skills.info_gathering.evals.harness import RunSummary as _BaseRunSummary


class Correct(BaseModel):
    kind: Literal["correct"] = "correct"
    turns: int


class Timeout(BaseModel):
    kind: Literal["timeout"] = "timeout"
    limit: int


Result = Annotated[Correct | Timeout, Field(discriminator="kind")]


class LogEntry(BaseModel):
    timestamp: datetime
    player: Literal["guesser", "simulator"]
    model: str | None = None
    content: str
    tool_calls: list[dict[str, object]] = Field(default_factory=list)


class RunSummary(_BaseRunSummary[Result]):
    invalid_input_count: int = 0


__all__ = ["Correct", "LogEntry", "Result", "RunSummary", "Timeout"]
