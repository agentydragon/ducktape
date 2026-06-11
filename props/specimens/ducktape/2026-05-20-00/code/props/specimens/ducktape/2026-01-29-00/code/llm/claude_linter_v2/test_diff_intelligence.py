"""Tests for diff intelligence module."""

from collections import defaultdict
from pathlib import Path

import pytest_bazel

from llm.claude_code_api import EditOperation, EditToolCall, MultiEditToolCall
from llm.claude_linter_v2.config.models import Violation
from llm.claude_linter_v2.diff.categorizer import CategorizedViolation, ViolationCategorizer, ViolationCategory
from llm.claude_linter_v2.diff.intelligence import DiffIntelligence
from llm.claude_linter_v2.diff.parser import ParsedDiff, parse_tool_response

TEST_FILE = Path("/test.py")


class TestDiffParser:
    """Test diff parsing functionality."""

    def test_parse_edit_tool(self):
        """Test parsing Edit tool response."""
        tool_call = EditToolCall(file_path=TEST_FILE, old_string="def foo():", new_string="def bar():")
        tool_response = {
            "structuredPatch": [
                {"oldStart": 10, "oldLines": 1, "newStart": 10, "newLines": 1, "lines": ["-def foo():", "+def bar():"]}
            ]
        }

        parsed = parse_tool_response(tool_call, tool_response)

        assert parsed is not None
        assert parsed.added_lines == {10}
        assert len(parsed.hunks) == 1
        assert parsed.hunks[0].new_start == 10

    def test_parse_multiedit_tool(self):
        """Test parsing MultiEdit tool with multiple hunks."""
        tool_call = MultiEditToolCall(
            file_path=TEST_FILE,
            edits=[
                EditOperation(old_string="foo", new_string="bar"),
                EditOperation(old_string="baz", new_string="qux"),
            ],
        )
        tool_response = {
            "structuredPatch": [
                {"oldStart": 10, "oldLines": 1, "newStart": 10, "newLines": 1, "lines": ["-foo", "+bar"]},
                {"oldStart": 20, "oldLines": 1, "newStart": 20, "newLines": 1, "lines": ["-baz", "+qux"]},
            ]
        }

        parsed = parse_tool_response(tool_call, tool_response)

        assert parsed is not None
        assert parsed.added_lines == {10, 20}
        assert len(parsed.hunks) == 2

    def test_parse_missing_structured_patch(self):
        """Test that missing structuredPatch field returns None."""
        tool_call = EditToolCall(file_path=TEST_FILE, old_string="foo", new_string="bar")
        tool_response = {"someOtherField": "value"}

        assert parse_tool_response(tool_call, tool_response) is None


class TestViolationCategorizer:
    """Test violation categorization."""

    def test_categorize_in_diff(self):
        """Test categorizing violations in changed lines."""
        categorizer = ViolationCategorizer(context_distance=3)

        violations = [
            Violation(rule="E722", line=10, column=0, message="Bare except"),
            Violation(rule="E722", line=20, column=0, message="Bare except"),
        ]

        parsed_diff = ParsedDiff(
            file_path=TEST_FILE, hunks=[], added_lines={10}, removed_lines=set(), context_lines=set()
        )

        categorized = categorizer.categorize_violations(violations, parsed_diff)

        assert len(categorized) == 2
        assert categorized[0].category == ViolationCategory.IN_DIFF
        assert categorized[0].distance_from_change == 0
        assert categorized[1].category == ViolationCategory.OUT_OF_DIFF
        assert categorized[1].distance_from_change is None

    def test_categorize_near_diff(self):
        """Test categorizing violations near changed lines."""
        categorizer = ViolationCategorizer(context_distance=3)

        violations = [
            Violation(rule="E722", line=8, column=0, message="Near change"),
            Violation(rule="E722", line=13, column=0, message="Also near"),
            Violation(rule="E722", line=20, column=0, message="Far away"),
        ]

        parsed_diff = ParsedDiff(
            file_path=TEST_FILE, hunks=[], added_lines={10}, removed_lines=set(), context_lines=set()
        )

        categorized = categorizer.categorize_violations(violations, parsed_diff)

        assert categorized[0].category == ViolationCategory.NEAR_DIFF
        assert categorized[0].distance_from_change == 2
        assert categorized[1].category == ViolationCategory.NEAR_DIFF
        assert categorized[1].distance_from_change == 3
        assert categorized[2].category == ViolationCategory.OUT_OF_DIFF

    def test_filter_by_priority(self):
        """Test filtering violations by priority."""
        categorizer = ViolationCategorizer()

        categorized = [
            CategorizedViolation(
                violation=Violation(rule="E1", line=1, column=0, message="Out"),
                category=ViolationCategory.OUT_OF_DIFF,
                distance_from_change=None,
            ),
            CategorizedViolation(
                violation=Violation(rule="E2", line=10, column=0, message="In"),
                category=ViolationCategory.IN_DIFF,
                distance_from_change=0,
            ),
            CategorizedViolation(
                violation=Violation(rule="E3", line=8, column=0, message="Near"),
                category=ViolationCategory.NEAR_DIFF,
                distance_from_change=2,
            ),
        ]

        filtered = categorizer.filter_by_priority(categorized, max_violations=2)

        assert len(filtered) == 2
        assert filtered[0].category == ViolationCategory.IN_DIFF
        assert filtered[1].category == ViolationCategory.NEAR_DIFF


class TestDiffIntelligence:
    """Test the main diff intelligence module."""

    def test_format_violations_by_category(self):
        """Test formatting categorized violations."""
        di = DiffIntelligence()

        groups: defaultdict[ViolationCategory, list[CategorizedViolation]] = defaultdict(list)
        groups[ViolationCategory.IN_DIFF] = [
            CategorizedViolation(
                violation=Violation(rule="E722", line=10, column=0, message="Bare except"),
                category=ViolationCategory.IN_DIFF,
                distance_from_change=0,
            )
        ]
        groups[ViolationCategory.NEAR_DIFF] = [
            CategorizedViolation(
                violation=Violation(rule="W293", line=8, column=0, message="Trailing whitespace"),
                category=ViolationCategory.NEAR_DIFF,
                distance_from_change=2,
            )
        ]

        formatted = di.format_violations_by_category(groups)

        assert "Issues in code you just added:" in formatted
        assert "Line 10: Bare except" in formatted
        assert "Issues near your changes:" in formatted
        assert "Line 8 (2 lines away): Trailing whitespace" in formatted


if __name__ == "__main__":
    pytest_bazel.main()
