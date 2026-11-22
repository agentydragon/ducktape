"""orchestration_v1 Action presentation examples."""

import pytest
import pytest_bazel

from agentplane.app.frontend.actions.testing.action_presentation_cases import (
    EXAMPLE_COMMAND,
    EXAMPLE_SESSION,
    ActionExample,
    ActionPresentationCases,
)

pytest_plugins = ("testing.frontend_visual.fixtures", "agentplane.app.frontend.testing.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")

ORCHESTRATION_SANDBOX = {"name": "review-box", "uid": "81000000-0000-4000-8000-000000000003"}

ORCHESTRATION_SESSION: dict[str, object] = {"sandbox": ORCHESTRATION_SANDBOX, "session_id": EXAMPLE_SESSION}

ORCHESTRATION_COMMAND = {**ORCHESTRATION_SESSION, "command_id": EXAMPLE_COMMAND}

QUEUED = {"session_id": EXAMPLE_SESSION, "state": "DELIVERY_STATE_QUEUED_IN_DISPATCHER"}

ORCHESTRATION_SPEC = {"harness": "HARNESS_CODEX", "model": "gpt-5.4", "cwd": "/workspace"}


class TestActionPresentations(ActionPresentationCases):
    examples = (
        ActionExample(
            "orchestration_v1",
            "launch_sandbox",
            {
                "name": "review-box",
                "template": "python",
                "launcher_grants": ["send_message"],
                "policy_sets": ["docs-read"],
                "wake_on_send": True,
                "idle_suspend_after_seconds": 1200,
            },
            {"sandbox": ORCHESTRATION_SANDBOX, "launcher_grants": ["send_message"]},
            "Send inbox messages",
        ),
        ActionExample(
            "orchestration_v1",
            "list_launch_presets",
            {},
            {
                "presets": {
                    "docs": {
                        "title": "Documentation reviewer",
                        "launch": {"template": "python"},
                        "create_session": {"spec": ORCHESTRATION_SPEC},
                    }
                }
            },
            "Documentation reviewer",
            compact_card_expands=False,
        ),
        ActionExample(
            "orchestration_v1",
            "create_session",
            {**ORCHESTRATION_SESSION, "spec": {**ORCHESTRATION_SPEC, "instructions": "Check the changelog."}},
            {"session_id": EXAMPLE_SESSION, "status": {"materialization": "SESSION_MATERIALIZATION_WAITING"}},
            "Session created · Waiting",
        ),
        ActionExample(
            "orchestration_v1",
            "get_session",
            ORCHESTRATION_SESSION,
            {
                "sandbox_ready": {"status": "True", "reason": "PodReady"},
                "session": {
                    "session_id": EXAMPLE_SESSION,
                    "runner": {"harness": "HARNESS_CODEX", "active_turn_id": "turn-5"},
                },
                "dispatches": [],
            },
            "Turn active",
            compact_card_expands=False,
        ),
        ActionExample(
            "orchestration_v1",
            "list_sessions",
            {"sandbox": ORCHESTRATION_SANDBOX},
            {
                "sessions": [
                    {
                        "session_id": EXAMPLE_SESSION,
                        "runner": {"harness": "HARNESS_CODEX"},
                        "last_turn_status": "TURN_STATUS_SUCCEEDED",
                    }
                ]
            },
            "Last turn · Succeeded",
            compact_card_expands=False,
        ),
        ActionExample(
            "orchestration_v1",
            "change_model",
            {**ORCHESTRATION_COMMAND, "model": "gpt-5.4-mini"},
            QUEUED,
            "Queued for delivery",
        ),
        ActionExample(
            "orchestration_v1",
            "change_reasoning_effort",
            {**ORCHESTRATION_COMMAND, "effort": "high"},
            QUEUED,
            "Queued for delivery",
        ),
        ActionExample(
            "orchestration_v1",
            "submit_input",
            {**ORCHESTRATION_COMMAND, "text": "Review the deployment notes."},
            QUEUED,
            "Queued for delivery",
        ),
        ActionExample(
            "orchestration_v1",
            "interrupt_turn",
            {**ORCHESTRATION_COMMAND, "turn_id": "turn-5"},
            QUEUED,
            "Queued for delivery",
        ),
        ActionExample(
            "orchestration_v1",
            "read_turns",
            {**ORCHESTRATION_SESSION, "count": 5},
            {
                "turns": [
                    {
                        "turn_id": "turn-5",
                        "input": [{"text": "Check the deployment notes."}],
                        "tool_calls": [],
                        "final_assistant_message": "Checked the deployment notes.",
                    }
                ],
                "resume_token": "recorded-token",
            },
            "Checked the deployment notes.",
            compact_card_expands=False,
        ),
        ActionExample(
            "orchestration_v1",
            "wait_for_turn",
            {**ORCHESTRATION_SESSION, "resume_token": "recorded-token", "wait_seconds": 20},
            {"completed": False, "resume_token": "next-token"},
            "No completion observed before the wait ended",
        ),
    )


if __name__ == "__main__":
    pytest_bazel.main()
