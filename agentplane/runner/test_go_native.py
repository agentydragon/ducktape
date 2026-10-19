"""Run the shared turn and subagent scenarios against the Go runner subprocess."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest
import pytest_bazel

from agentplane.runner.test_subagents import (
    test_claude_background_child_is_raw_only as test_go_claude_background_child_raw_only,
    test_codex_child_completion_does_not_complete_parent as test_go_codex_child_does_not_complete_parent,
)
from agentplane.runner.test_turns import (
    test_a_seal_interrupts_the_active_turn_and_records_that_teardown_ended_it as test_go_seal_interrupt_metadata,
    test_claude_active_turn_model_command_has_a_native_causal_outcome as test_go_claude_active_model,
    test_claude_inputs_during_a_tool_are_confirmed_from_the_started_cohort as test_go_claude_tool_input_cohort,
    test_claude_interrupt_after_thinking_and_a_tool_call_keeps_the_thinking as test_go_interrupt_tool,
    test_claude_interrupt_after_thinking_and_some_text_keeps_the_thinking as test_go_interrupt_text,
    test_claude_interrupt_right_after_thinking_reports_it_absent_and_does_not_replay_it as test_go_interrupt_thinking_absent,
    test_codex_active_turn_model_command_takes_effect_on_its_next_turn_start as test_go_codex_active_model,
    test_codex_input_during_a_turn_joins_it as test_go_codex_input_joins_turn,
    test_effort_change_is_confirmed_and_used_by_next_model_request as test_go_effort_change_used_next_request,
    test_failed_tool_is_reported_as_failed as test_go_failed_tool,
    test_interrupt_ends_the_turn_as_interrupted as test_go_interrupt_turn,
    test_model_change_then_input_is_admitted_without_waiting_for_model_effect as test_go_model_change_then_input,
    test_model_connection_loss_through_runner as test_go_connection_loss,
    test_model_http_error_through_runner as test_go_http_error,
    test_one_turn_streams_reasoning_and_text as test_go_turn_streams_reasoning_text,
    test_the_sandbox_is_busy_while_a_turn_runs_and_idle_after_it_ends as test_go_sandbox_busy,
    test_tool_call_reports_arguments_and_result as test_go_tool_arguments_result,
)
from agentplane.runner.testing import launches
from agentplane.runner.testing.driver import RunnerDriver
from agentplane.runner.testing.gateway import Gateway
from agentplane.runner.testing.process_fixtures import RunnerProcess
from util.bazel.runfiles import get_required_path

__all__ = [
    "test_go_claude_active_model",
    "test_go_claude_background_child_raw_only",
    "test_go_claude_tool_input_cohort",
    "test_go_codex_active_model",
    "test_go_codex_child_does_not_complete_parent",
    "test_go_codex_input_joins_turn",
    "test_go_connection_loss",
    "test_go_effort_change_used_next_request",
    "test_go_failed_tool",
    "test_go_http_error",
    "test_go_interrupt_text",
    "test_go_interrupt_thinking_absent",
    "test_go_interrupt_tool",
    "test_go_interrupt_turn",
    "test_go_model_change_then_input",
    "test_go_sandbox_busy",
    "test_go_seal_interrupt_metadata",
    "test_go_tool_arguments_result",
    "test_go_turn_streams_reasoning_text",
]


@pytest.fixture
async def driver(gateway: Gateway, start_runner: Callable[..., Awaitable[RunnerProcess]]) -> RunnerDriver:
    await start_runner(runner_binary=get_required_path(launches.GO_RUNNER_BINARY))
    return RunnerDriver(gateway)


if __name__ == "__main__":
    pytest_bazel.main()
