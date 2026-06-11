"""Categorize violations based on their proximity to diff changes."""

from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum

from llm.claude_linter_v2.config.models import Violation
from llm.claude_linter_v2.diff.parser import ParsedDiff


class ViolationCategory(StrEnum):
    """Category of a violation based on its relationship to diff changes."""

    IN_DIFF = "in-diff"
    NEAR_DIFF = "near-diff"
    OUT_OF_DIFF = "out-of-diff"


@dataclass
class CategorizedViolation:
    """A violation categorized by its relationship to diff changes."""

    violation: Violation
    category: ViolationCategory
    distance_from_change: int | None  # For near-diff


class ViolationCategorizer:
    """Categorize violations based on their proximity to changes."""

    def __init__(self, context_distance: int = 3):
        """
        Initialize categorizer.

        Args:
            context_distance: Lines away from change to consider "near"
        """
        self.context_distance = context_distance

    def categorize_violations(
        self, violations: list[Violation], parsed_diff: ParsedDiff | None
    ) -> list[CategorizedViolation]:
        """
        Categorize violations based on their proximity to changes.

        Args:
            violations: List of violations with line numbers
            parsed_diff: Parsed diff information (None for PreToolUse)

        Returns:
            List of categorized violations
        """
        if parsed_diff is None:
            # No diff info - all violations are out-of-diff
            return [
                CategorizedViolation(violation=v, category=ViolationCategory.OUT_OF_DIFF, distance_from_change=None)
                for v in violations
            ]

        # Build set of all changed lines and their neighbors
        changed_lines = parsed_diff.added_lines
        near_lines = set()

        for line in changed_lines:
            for offset in range(-self.context_distance, self.context_distance + 1):
                if offset != 0:  # Don't include the changed line itself
                    near_line = line + offset
                    if near_line > 0:  # Line numbers start at 1
                        near_lines.add(near_line)

        categorized = []

        for violation in violations:
            category: ViolationCategory
            if violation.line in changed_lines:
                category = ViolationCategory.IN_DIFF
                distance = 0
            elif violation.line in near_lines:
                category = ViolationCategory.NEAR_DIFF
                # Calculate minimum distance to any changed line
                distance = min(abs(violation.line - changed_line) for changed_line in changed_lines)
            else:
                category = ViolationCategory.OUT_OF_DIFF
                distance = None

            categorized.append(
                CategorizedViolation(violation=violation, category=category, distance_from_change=distance)
            )

        return categorized

    def group_by_category(
        self, categorized: list[CategorizedViolation]
    ) -> defaultdict[ViolationCategory, list[CategorizedViolation]]:
        """Group categorized violations by their category."""
        groups: defaultdict[ViolationCategory, list[CategorizedViolation]] = defaultdict(list)
        for cv in categorized:
            groups[cv.category].append(cv)
        # Sort near-diff by distance
        groups[ViolationCategory.NEAR_DIFF].sort(key=lambda cv: cv.distance_from_change or 0)
        return groups

    def filter_by_priority(
        self, categorized: list[CategorizedViolation], max_violations: int = 10
    ) -> list[CategorizedViolation]:
        """
        Filter violations by priority.

        Priority order:
        1. All in-diff violations
        2. Near-diff violations (closest first)
        3. Out-of-diff violations

        Args:
            categorized: List of categorized violations
            max_violations: Maximum number to return

        Returns:
            Filtered list of violations
        """
        groups = self.group_by_category(categorized)
        return (
            groups[ViolationCategory.IN_DIFF]
            + groups[ViolationCategory.NEAR_DIFF]
            + groups[ViolationCategory.OUT_OF_DIFF]
        )[:max_violations]
