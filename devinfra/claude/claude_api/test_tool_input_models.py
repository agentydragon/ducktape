"""Tests for Claude Code tool input models.

Upstream Claude Code adds fields to tool inputs between releases; the parsers must
tolerate unknown keys rather than choke on them.
"""

import pytest_bazel

from devinfra.claude.claude_api.tool_input_models import BashInput, GrepInput


def test_bash_input_extra_fields_allowed() -> None:
    m = BashInput.model_validate({"command": "ls", "futureField": 42})
    assert m.command == "ls"


def test_grep_input_extra_fields_allowed() -> None:
    m = GrepInput.model_validate({"pattern": "x", "newUpstreamFlag": True})
    assert m.pattern == "x"


if __name__ == "__main__":
    pytest_bazel.main()
