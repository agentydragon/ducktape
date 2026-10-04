"""Tests for Twenty Questions OpenAI Agents SDK result extraction."""

from unittest.mock import MagicMock

import pytest_bazel
from agents import ToolCallOutputItem

from skills.info_gathering.evals.twenty_questions.x.openai_agents.twenty_questions import _run_sim_and_extract


def _make_tool_output_item(output: str) -> ToolCallOutputItem:
    return ToolCallOutputItem(agent=MagicMock(), raw_item={"output": output}, output=output)


def test_extract_answer() -> None:
    result = MagicMock()
    result.new_items = [_make_tool_output_item("Answered: yes")]
    response, is_correct, is_invalid, _reason = _run_sim_and_extract(result)
    assert response == "yes"
    assert not is_correct
    assert not is_invalid


def test_extract_correct() -> None:
    result = MagicMock()
    result.new_items = [_make_tool_output_item("Correct!")]
    _response, is_correct, is_invalid, _reason = _run_sim_and_extract(result)
    assert is_correct
    assert not is_invalid


def test_extract_invalid() -> None:
    result = MagicMock()
    result.new_items = [_make_tool_output_item("Invalid: not a question")]
    _response, is_correct, is_invalid, reason = _run_sim_and_extract(result)
    assert is_invalid
    assert reason == "not a question"
    assert not is_correct


if __name__ == "__main__":
    pytest_bazel.main()
