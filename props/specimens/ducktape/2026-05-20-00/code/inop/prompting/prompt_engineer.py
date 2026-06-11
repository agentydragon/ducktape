from __future__ import annotations

import math
import statistics
from enum import Enum
from typing import Protocol

from inop.engine.models import GradedRollout
from inop.io.logging_utils import DualOutputLogging

logger = DualOutputLogging.get_logger()

SUBMIT_PROMPT_FUNCTION_NAME = "submit_prompt"


class FeedbackMode(Enum):
    """Processing mode for prompt optimization."""

    FULL_ROLLOUTS = "full_rollouts"
    SUMMARY = "summary"
    STATS_ONLY = "stats_only"


class FeedbackProvider(Protocol):
    async def provide_feedback(self, rollouts: list[GradedRollout]) -> str:
        """Provide feedback string based on a batch of rollouts."""
        ...

    def verbal_description(self) -> str:
        """Return a human-language description of the feedback provider."""
        ...


class FullRolloutsFeedbackProvider(FeedbackProvider):
    async def provide_feedback(self, rollouts: list[GradedRollout]) -> str:
        summaries: list[str] = []
        for graded_code in rollouts:
            # Use Pydantic's model_dump_json which will use our custom serializers
            summary = graded_code.model_dump_json(indent=2)
            summaries.append(summary)
        return "\n\n".join(summaries)

    def verbal_description(self) -> str:
        return "Full rollouts feedback provider, providing full task rollouts with messages and tool use."


class StatsOnlyFeedbackProvider(FeedbackProvider):
    async def provide_feedback(self, rollouts: list[GradedRollout]) -> str:
        """Provide feedback string based on a batch of rollouts."""
        if len(rollouts) < 2:
            return "Insufficient data: need at least 2 rollouts for statistics."
        overall_scores = [graded_code.grade.overall_score for graded_code in rollouts]
        n = len(overall_scores)
        mean_score = sum(overall_scores) / n
        std_err = statistics.stdev(overall_scores) / math.sqrt(n)
        return f"Mean overall score: {mean_score:.2f} (standard error {std_err:.2f})"

    def verbal_description(self) -> str:
        return "Mean overall score (point estimate and 95% CI)"
