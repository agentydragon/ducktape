"""sandbox Action presentation examples."""

import pytest
import pytest_bazel

from agentplane.app.frontend.actions.testing.action_presentation_cases import ActionExample, ActionPresentationCases

pytest_plugins = ("testing.frontend_visual.fixtures", "agentplane.app.frontend.testing.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")

SANDBOX = {"name": "review-sandbox", "template": "python", "phase": "Running", "conditions": []}


class TestActionPresentations(ActionPresentationCases):
    examples = (
        ActionExample(
            "sandbox",
            "exec",
            {
                "name": "review-sandbox",
                "script": "printf '%s\\n' 'ready for review'",
                "timeout_seconds": 60,
                "max_output_bytes": 4096,
                "cwd": "/workspace",
            },
            {
                "exit": {"kind": "exited", "exit_code": 0},
                "stdout": "ready for review\n",
                "stderr": "",
                "duration_seconds": 0.1,
            },
            "ready for review",
        ),
        ActionExample(
            "sandbox",
            "create",
            {"name": "review-sandbox", "template": "python"},
            SANDBOX,
            "Running",
            compact_card_expands=False,
        ),
        ActionExample("sandbox", "get", {"name": "review-sandbox"}, SANDBOX, "Running", compact_card_expands=False),
        ActionExample(
            "sandbox",
            "dispose",
            {"name": "review-sandbox"},
            {"name": "review-sandbox", "existed": True},
            "disposed",
            compact_card_expands=False,
        ),
        ActionExample("sandbox", "list", {}, {"sandboxes": [SANDBOX]}, "Running", compact_card_expands=False),
        ActionExample(
            "sandbox",
            "get_template",
            {"template": "python"},
            {
                "spec": {
                    "containers": [{"name": "workspace", "image": "example.com/sandbox/python:3.13"}],
                    "volumes": [{"name": "work"}],
                }
            },
            "example.com/sandbox/python:3.13",
            compact_card_expands=False,
        ),
    )


if __name__ == "__main__":
    pytest_bazel.main()
