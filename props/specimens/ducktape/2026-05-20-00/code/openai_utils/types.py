from __future__ import annotations

from enum import StrEnum
from typing import Literal

from typing_extensions import TypedDict


class ReasoningEffort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


ReasoningEffortLiteral = Literal["low", "medium", "high"]


class ReasoningSummary(StrEnum):
    """Canonical values for Responses API reasoning summary selection."""

    AUTO = "auto"
    CONCISE = "concise"
    DETAILED = "detailed"


ReasoningSummaryLiteral = Literal["auto", "concise", "detailed"]


class ReasoningParams(TypedDict, total=False):
    effort: ReasoningEffort | ReasoningEffortLiteral
    summary: ReasoningSummary | ReasoningSummaryLiteral


def build_reasoning_params(
    effort: ReasoningEffort | None, summary: ReasoningSummary | None = None
) -> ReasoningParams | None:
    """Convert optional reasoning knobs into adapter ReasoningParams."""
    if effort is None and summary is None:
        return None

    payload: ReasoningParams = {}
    if effort is not None:
        payload["effort"] = effort
    if summary is not None:
        payload["summary"] = summary

    return payload
