"""ssh Action presentation examples."""

import pytest
import pytest_bazel

from agentplane.app.frontend.actions.testing.action_presentation_cases import ActionExample, ActionPresentationCases

pytest_plugins = ("testing.frontend_visual.fixtures", "agentplane.app.frontend.testing.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")


class TestActionPresentations(ActionPresentationCases):
    examples = (
        ActionExample(
            "ssh",
            "list_targets",
            {},
            [
                {"user": "backup", "host": "archive.example.com", "available": True},
                {
                    "user": "build",
                    "host": "builder.example.com",
                    "available": False,
                    "error": {"message": "Configured SSH identity is unavailable."},
                },
            ],
            "backup@archive.example.com",
            compact_card_expands=False,
        ),
        ActionExample(
            "ssh",
            "exec",
            {"host": "archive.example.com", "user": "backup", "command": "ls -lh /srv/backups", "timeout_seconds": 30},
            {
                "host": "archive.example.com",
                "user": "backup",
                "exit_code": 0,
                "stdout": "-rw-r--r-- 1 backup backup 1.5M Oct 12 03:00 daily.tar.zst\n",
                "stderr": "",
                "stdout_truncated": False,
                "stderr_truncated": False,
            },
            "daily.tar.zst",
        ),
    )


if __name__ == "__main__":
    pytest_bazel.main()
