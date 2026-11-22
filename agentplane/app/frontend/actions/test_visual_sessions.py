"""sessions Action presentation examples."""

import pytest
import pytest_bazel

from agentplane.app.frontend.actions.testing.action_presentation_cases import (
    EXAMPLE_COMMAND,
    EXAMPLE_ENVIRONMENT,
    EXAMPLE_SESSION,
    ActionExample,
    ActionPresentationCases,
)

pytest_plugins = ("testing.frontend_visual.fixtures", "agentplane.app.frontend.testing.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")


class TestActionPresentations(ActionPresentationCases):
    examples = (
        ActionExample(
            "sessions",
            "create",
            {
                "target": EXAMPLE_ENVIRONMENT,
                "session_id": EXAMPLE_SESSION,
                "spec": {"harness": "HARNESS_CODEX", "model": "gpt-5.4", "cwd": "/workspace"},
                "additional_instructions": "Use the project changelog for context.",
                "setup_script": "git status --short",
            },
            {
                "session": {
                    "session_id": EXAMPLE_SESSION,
                    "status": {"materialization": "SESSION_MATERIALIZATION_WAITING"},
                }
            },
            "Session created · Waiting",
        ),
        ActionExample(
            "sessions",
            "get",
            {"session_id": EXAMPLE_SESSION},
            {
                "session_id": EXAMPLE_SESSION,
                "session": {"source": "dispatcher", "kind": "observed"},
                "history": {"source": "history", "kind": "unavailable", "error": {"message": "History unavailable"}},
            },
            "History unavailable",
            compact_card_expands=False,
        ),
        ActionExample(
            "sessions",
            "submit_input",
            {
                "target": EXAMPLE_ENVIRONMENT,
                "session_id": EXAMPLE_SESSION,
                "command_id": EXAMPLE_COMMAND,
                "text": "Review the deployment notes.",
            },
            {"dispatch": {"session_id": EXAMPLE_SESSION, "state": "DELIVERY_STATE_QUEUED_IN_DISPATCHER"}},
            "Queued for delivery",
        ),
        ActionExample(
            "sessions",
            "observe_input",
            {"session_id": EXAMPLE_SESSION, "command_id": EXAMPLE_COMMAND, "wait_seconds": 15},
            {
                "session_id": EXAMPLE_SESSION,
                "dispatch": {"source": "dispatcher", "kind": "observed"},
                "observation": {"source": "history", "kind": "input_confirmed", "gaps": []},
            },
            "Input confirmed",
        ),
        ActionExample(
            "sessions",
            "cancel_input",
            {"session_id": EXAMPLE_SESSION, "command_id": EXAMPLE_COMMAND},
            {"dispatch": {"session_id": EXAMPLE_SESSION, "state": "DELIVERY_STATE_CANCELLED_IN_DISPATCHER"}},
            "Cancelled before delivery",
        ),
        ActionExample(
            "sessions",
            "interrupt_turn",
            {
                "target": EXAMPLE_ENVIRONMENT,
                "session_id": EXAMPLE_SESSION,
                "command_id": EXAMPLE_COMMAND,
                "turn_id": "turn-5",
            },
            {"dispatch": {"session_id": EXAMPLE_SESSION, "state": "DELIVERY_STATE_RUNNER_ADMITTED"}},
            "Recorded by the runner",
        ),
        ActionExample(
            "sessions",
            "read_turns",
            {"session_id": EXAMPLE_SESSION, "count": 5, "page_cursor": "recorded-next-page"},
            {
                "session_id": EXAMPLE_SESSION,
                "source": "history",
                "gaps": [{"first": "3", "last": "4"}],
                "turns": [
                    {
                        "turn_id": "turn-5",
                        "input": [{"text": "Review the deployment notes."}],
                        "tool_calls": [{"name": "get_file_contents"}],
                        "status": "TURN_STATUS_SUCCEEDED",
                        "final_assistant_message": "The readiness checks are documented.",
                    }
                ],
            },
            "Some history is missing.",
            compact_card_expands=False,
        ),
    )


if __name__ == "__main__":
    pytest_bazel.main()
