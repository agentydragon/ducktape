from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class LineRange(BaseModel):
    start_line: int = Field(..., ge=1)
    end_line: int | None = Field(default=None, ge=1)

    model_config = ConfigDict(extra="forbid")


DetectorName = Annotated[str, StringConstraints(min_length=1, max_length=200)]
PropertyId = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class Detection(BaseModel):
    """One detector output item (danger signal).

    - property: candidate property id (may be heuristic); the agent confirms/relables
    - path: file path (repo-relative)
    - ranges: 1-based line ranges (closed intervals); may be single-line
    - detector: detector name/id
    - confidence: 0..1 confidence score (heuristic)
    - message: concise rationale
    - snippet: optional concise code excerpt for UX
    """

    property: PropertyId
    path: Path
    ranges: list[LineRange] = Field(default_factory=list)
    detector: DetectorName
    confidence: float = Field(ge=0.0, le=1.0, default=0.9)
    message: str | None = None
    snippet: str | None = None

    model_config = ConfigDict(extra="forbid")
