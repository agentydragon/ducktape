from __future__ import annotations

from enum import StrEnum
from typing import Literal

from typing_extensions import TypedDict


def to_reasoning_effort(value: ReasoningEffort | None) -> ReasoningEffortLiteral | None:
    if value is None:
        return None
    return value.value


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

    effort_value = to_reasoning_effort(effort)
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
