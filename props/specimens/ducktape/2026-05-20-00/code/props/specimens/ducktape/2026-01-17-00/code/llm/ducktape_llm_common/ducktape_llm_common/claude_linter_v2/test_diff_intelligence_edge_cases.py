"""Edge case tests for diff intelligence module."""

from collections import defaultdict
from pathlib import Path

from ducktape_llm_common.claude_code_api import EditOperation, EditToolCall, MultiEditToolCall
from ducktape_llm_common.claude_linter_v2.config.models import Violation
from ducktape_llm_common.claude_linter_v2.diff.categorizer import CategorizedViolation, ViolationCategory
from ducktape_llm_common.claude_linter_v2.diff.intelligence import DiffIntelligence
from ducktape_llm_common.claude_linter_v2.diff.parser import parse_tool_response

TEST_FILE = Path("/test.py")


class TestDiffParserEdgeCases:
    """Test edge cases in diff parsing."""

    def test_parse_edit_with_context_lines(self):
        """Test parsing Edit tool with context lines."""
        tool_call = EditToolCall(file_path=TEST_FILE, old_string="foo", new_string="bar")
        tool_response = {
            "structuredPatch": [
                {
                    "oldStart": 10,
                    "oldLines": 5,
                    "newStart": 10,
                    "newLines": 5,
                    "lines": [
                        " def setup():",  # Context line
                        "     config = load_config()",
                        "-    return config",
                        "+    return validate_config(config)",
                        "     # End of setup",  # Context line
                    ],
                }
            ]
        }

        parsed = parse_tool_response(tool_call, tool_response)

        assert parsed is not None
        assert parsed.added_lines == {12}  # Only the + line
        assert parsed.context_lines == {10, 11, 13}  # The unchanged lines

    def test_parse_multiedit_with_line_shifts(self):
        """Test MultiEdit where second edit is affected by first edit's line changes."""
        tool_call = MultiEditToolCall(file_path=TEST_FILE, edits=[EditOperation(old_string="foo", new_string="bar")])
        tool_response = {
            "structuredPatch": [
                {
                    "oldStart": 10,
                    "oldLines": 1,
                    "newStart": 10,
                    "newLines": 3,  # Added 2 lines
                    "lines": ["-def foo():", "+def foo():", "+    # Added comment", "+    pass"],
                },
                {
                    "oldStart": 20,
                    "oldLines": 1,
                    "newStart": 22,  # Shifted by 2 due to first edit
                    "newLines": 1,
                    "lines": ["-old", "+new"],
                },
            ]
        }

        parsed = parse_tool_response(tool_call, tool_response)

        assert parsed is not None
        assert parsed.added_lines == {10, 11, 12, 22}

    def test_parse_empty_structured_patch(self):
        """Test handling empty structuredPatch."""
        tool_call = EditToolCall(file_path=TEST_FILE, old_string="foo", new_string="bar")
        tool_response: dict[str, list] = {"structuredPatch": []}
        parsed = parse_tool_response(tool_call, tool_response)
        assert parsed is not None
        assert len(parsed.hunks) == 0
        assert len(parsed.added_lines) == 0

    def test_parse_no_structured_patch_field(self):
        """Test handling missing structuredPatch field."""
        tool_call = EditToolCall(file_path=TEST_FILE, old_string="foo", new_string="bar")
        tool_response = {"someOtherField": "value"}
        parsed = parse_tool_response(tool_call, tool_response)
        assert parsed is None

    def test_parse_special_diff_markers(self):
        """Test handling special diff markers."""
        tool_call = EditToolCall(file_path=TEST_FILE, old_string="old line", new_string="new line")
        tool_response = {
            "structuredPatch": [
                {
                    "oldStart": 10,
                    "oldLines": 2,
                    "newStart": 10,
                    "newLines": 2,
                    "lines": [
                        "-old line",
                        "+new line",
                        "\\ No newline at end of file",  # Special marker
                    ],
                }
            ]
        }

        parsed = parse_tool_response(tool_call, tool_response)

        assert parsed is not None
        assert parsed.added_lines == {10}
        # Special marker should be ignored


class TestDiffIntelligenceEdgeCases:
    """Test edge cases in diff intelligence."""

    def test_no_violations(self):
        """Test handling when there are no violations."""
        di = DiffIntelligence()

        tool_call = EditToolCall(file_path=TEST_FILE, old_string="old", new_string="new")
        tool_response = {
            "structuredPatch": [
                {"oldStart": 10, "oldLines": 1, "newStart": 10, "newLines": 1, "lines": ["-old", "+new"]}
            ]
        }

        groups = di.analyze(
            tool_call=tool_call,
            tool_response=tool_response,
            violations=[],  # No violations
        )

        assert groups[ViolationCategory.IN_DIFF] == []
        assert groups[ViolationCategory.NEAR_DIFF] == []
        assert groups[ViolationCategory.OUT_OF_DIFF] == []

    def test_overlapping_near_regions(self):
        """Test when multiple changes create overlapping near-diff regions."""
        di = DiffIntelligence(context_distance=3)

        tool_call = EditToolCall(file_path=TEST_FILE, old_string="line10", new_string="new10")
        tool_response = {
            "structuredPatch": [
                {
                    "oldStart": 10,
                    "oldLines": 2,
                    "newStart": 10,
                    "newLines": 2,
                    "lines": ["-line10", "+new10", "-line15", "+new15"],
                }
            ]
        }

        violations = [Violation(rule="E1", line=12, column=0, message="Between changes")]

        groups = di.analyze(tool_call=tool_call, tool_response=tool_response, violations=violations)

        # Line 12 is within 3 lines of both line 10 and line 11
        assert len(groups[ViolationCategory.NEAR_DIFF]) == 1
        assert groups[ViolationCategory.NEAR_DIFF][0].distance_from_change == 1  # Closest distance

    def test_format_many_violations(self):
        """Test formatting with many violations doesn't overwhelm output."""
        di = DiffIntelligence()

        # Create many out-of-diff violations
        groups = defaultdict(list)
        groups[ViolationCategory.OUT_OF_DIFF] = [
            CategorizedViolation(
                violation=Violation(rule=f"E{i}", line=i * 10, column=0, message=f"Error {i}"),
                category=ViolationCategory.OUT_OF_DIFF,
                distance_from_change=None,
            )
            for i in range(1, 11)  # 10 violations
        ]
        formatted = di.format_violations_by_category(groups)

        # Should only show first 3 out-of-diff
        assert "Line 10:" in formatted
        assert "Line 20:" in formatted
        assert "Line 30:" in formatted
        assert "... and 7 more" in formatted
        assert "Line 40:" not in formatted  # Should be truncated
