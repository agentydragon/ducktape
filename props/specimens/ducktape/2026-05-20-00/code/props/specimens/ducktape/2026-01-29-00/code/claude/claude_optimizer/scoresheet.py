#!/usr/bin/env python3
"""
Abstract interface for grader scoresheets that evaluate CLAUDE.md behavior.

Each scoresheet defines a specific behavioral requirement and how to test it.
"""

import datetime
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class TestCase:
    """A single test case for evaluating a behavioral requirement."""

    id: str
    name: str
    prompt: str
    description: str
    expected_behavior: str


@dataclass
class GradeResult:
    """Result of grading a single test case."""

    test_case_id: str
    test_case_name: str
    passed: bool
    score: float  # 0.0 to 1.0
    feedback: str
    generated_code: str | None  # None if code generation failed, string if succeeded (even if empty)
    analysis_details: dict[str, Any]  # Always present - analysis is required step


@dataclass
class ScoresheetResult:
    """Complete results for a grader scoresheet."""

    scoresheet_name: str
    description: str
    overall_score: float  # 0.0 to 1.0
    total_tests: int
    passed_tests: int
    failed_tests: int
    test_results: list[GradeResult]
    summary: str

    @property
    def pass_rate(self) -> float:
        return self.passed_tests / self.total_tests if self.total_tests > 0 else 0.0


@dataclass
class ClaudeInteraction:
    """Record of a single Claude API interaction for logging."""

    interaction_id: str
    timestamp: datetime.datetime
    scoresheet_name: str
    test_case_name: str
    request_type: str  # "code_generation" | "analysis" | "other"
    claude_request: dict[str, Any]
    claude_response: dict[str, Any]
    success: bool
    error_message: str  # Empty string if no error, actual message if error occurred


class GraderScoresheet(ABC):
    """Abstract base class for grader scoresheets."""

    def __init__(self, name: str):
        self.name = name
        self.interactions: list[ClaudeInteraction] = []

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description of what this scoresheet evaluates."""

    @abstractmethod
    async def generate_test_cases(self) -> list[TestCase]:
        """Generate test cases that validate this behavioral requirement."""

    def _log_interaction(
        self,
        test_case_name: str,
        request_type: str,
        claude_request: dict[str, Any],
        claude_response: dict[str, Any],
        success: bool,
        error_message: str | None = None,
    ):
        """Log a Claude API interaction for comprehensive tracking."""
        interaction = ClaudeInteraction(
            interaction_id=str(uuid.uuid4()),
            timestamp=datetime.datetime.now(),
            scoresheet_name=self.name,
            test_case_name=test_case_name,
            request_type=request_type,
            claude_request=claude_request,
            claude_response=claude_response,
            success=success,
            error_message=error_message or "",
        )
        self.interactions.append(interaction)

    def _generate_summary(self, results: list[GradeResult], overall_score: float) -> str:
        """Generate a human-readable summary of the grading results."""
        if not results:
            return "No test cases evaluated."

        passed = sum(1 for r in results if r.passed)
        total = len(results)

        summary = f"**{self.name}** - Overall Score: {overall_score:.1%}\n"
        summary += f"Passed: {passed}/{total} test cases\n\n"

        if passed < total:
            summary += "**Failed Cases:**\n"
            for result in results:
                if not result.passed:
                    summary += f"- **{result.test_case_name}**: {result.feedback}\n"

        return summary
