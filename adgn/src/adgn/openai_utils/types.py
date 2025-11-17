from __future__ import annotations

from enum import StrEnum
from typing import Literal, cast

from typing_extensions import TypedDict


class ReasoningEffort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


ReasoningEffortLiteral = Literal["low", "medium", "high"]


class ReasoningParams(TypedDict, total=False):
    effort: ReasoningEffortLiteral
    summary: str


def build_reasoning_params(
    effort: ReasoningEffort | None, summary: ReasoningSummary | None = None
) -> ReasoningParams | None:
    """Convert optional reasoning knobs into adapter ReasoningParams."""

    effort_value: ReasoningEffortLiteral | None = (
        cast(ReasoningEffortLiteral, effort.value) if effort is not None else None
    )
    summary_value = summary.value if summary is not None else None

    if effort_value is None and summary_value is None:
        return None

    payload: ReasoningParams = {}
    if effort_value is not None:
        payload["effort"] = effort_value
    if summary_value is not None:
        payload["summary"] = summary_value

    return payload


class ReasoningSummary(StrEnum):
    """Canonical values for Responses API reasoning summary selection."""

    auto = "auto"
    concise = "concise"
    detailed = "detailed"
