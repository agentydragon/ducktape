"""environments Action presentation examples."""

import pytest
import pytest_bazel

from agentplane.app.frontend.actions.testing.action_presentation_cases import (
    EXAMPLE_ENVIRONMENT,
    ActionExample,
    ActionPresentationCases,
)

pytest_plugins = ("testing.frontend_visual.fixtures", "agentplane.app.frontend.testing.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")

ENVIRONMENT_VIEW = {
    "target": EXAMPLE_ENVIRONMENT,
    "configuration": {"template": "python"},
    "observed_state": {"operating_mode": "OPERATING_MODE_RUNNING", "readiness": {"state": "READINESS_STATE_READY"}},
}


class TestActionPresentations(ActionPresentationCases):
    examples = (
        ActionExample("environments", "list", {"limit": 12}, {"items": [ENVIRONMENT_VIEW]}, "Template python"),
        ActionExample("environments", "get", {"target": EXAMPLE_ENVIRONMENT}, ENVIRONMENT_VIEW, "Template python"),
        ActionExample(
            "environments",
            "resume",
            {"target": EXAMPLE_ENVIRONMENT, "resume_token": "recorded-token"},
            {"target": EXAMPLE_ENVIRONMENT, "outcome": "RESUME_OUTCOME_REQUESTED"},
            "Resume requested",
        ),
    )


if __name__ == "__main__":
    pytest_bazel.main()
