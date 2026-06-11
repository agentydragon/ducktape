"""Target metric mode for prompt optimizer."""

from enum import StrEnum


class TargetMetric(StrEnum):
    """Prompt optimizer terminal metric mode.

    Determines which validation examples are used for optimization:
    - WHOLE_REPO: Only full-snapshot validation examples (black-box validation)
    - TARGETED: Both per-file and full-snapshot validation examples (allows iteration)
    """

    WHOLE_REPO = "whole-repo"
    TARGETED = "targeted"
