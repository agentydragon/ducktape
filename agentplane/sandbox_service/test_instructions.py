"""Operational prompt construction without the integration app or its database."""

import pytest
import pytest_bazel

from agentplane.sandbox_service.instructions import combine_instructions, resolved_agent_instructions


def test_image_owned_agent_instructions_render_deployment_service_urls() -> None:
    instructions = resolved_agent_instructions(
        None, egress_api_url="http://egress.test.invalid", actions_service_url="http://actions.test.invalid:8080"
    )

    assert "http://egress.test.invalid/v1/rules" in instructions
    assert "http://egress.test.invalid/openapi.json" in instructions
    assert "http://actions.test.invalid:8080/openapi.json" in instructions
    assert "### Waiting efficiently for Actions" not in instructions


@pytest.mark.parametrize("configured", [None, "", "Custom deployment instructions"])
def test_notification_workflow_and_examples_augment_default_or_custom_prompt(configured: str | None) -> None:
    instructions = resolved_agent_instructions(
        configured,
        egress_api_url="http://egress.test.invalid" if configured is None else None,
        actions_service_url="http://actions.test.invalid:8080" if configured is None else None,
        notifications_service_url="http://notifications.test.invalid:8080",
    )

    assert instructions.count("### Waiting efficiently for Actions") == 1
    assert instructions.index("Choose how to wait:") < instructions.index("### Subscribe")
    assert "You may subscribe immediately; you do not need to time out first." in instructions
    assert "end your turn rather than occupying it with" in instructions
    assert "http://notifications.test.invalid:8080/v1/subscriptions" in instructions
    assert "http://notifications.test.invalid:8080/v1/inboxes/INBOX_ID/acknowledgement" in instructions
    assert '"idempotency_key": "follow-REAL_REQUEST_ID"' in instructions
    assert '"client_key"' not in instructions
    assert '"source": {"provider": "actions", "request_id": "REAL_REQUEST_ID", "after_sequence": 0}' in instructions
    assert "event.request_id and event.sequence" in instructions
    assert '"through_cursor": LAST_HANDLED_CURSOR' in instructions
    assert "Reads and runner delivery receipts never acknowledge." in instructions
    assert "unsubscribing is not withdrawal." in instructions
    assert "No notification-triggered harness/sandbox startup or wake-up is available." in instructions
    if configured:
        assert instructions.startswith(configured)
    elif configured is None:
        assert "http://actions.test.invalid:8080/openapi.json" in instructions


@pytest.mark.parametrize("configured", ["", "Custom deployment instructions"])
def test_explicit_instructions_do_not_require_service_urls(configured: str) -> None:
    assert resolved_agent_instructions(configured, egress_api_url=None, actions_service_url=None) == configured


@pytest.mark.parametrize(
    ("egress", "actions"), [(None, None), ("http://egress.test", None), (None, "http://actions.test")]
)
def test_bundled_instructions_require_both_service_urls(egress: str | None, actions: str | None) -> None:
    with pytest.raises(ValueError, match="require"):
        resolved_agent_instructions(None, egress_api_url=egress, actions_service_url=actions)


def test_task_instructions_augment_platform_guidance() -> None:
    assert combine_instructions("  Platform  ", "  Task  ") == "Platform\n\nTask"
    assert combine_instructions("", "Task") == "Task"
    assert combine_instructions("Platform", "") == "Platform"


if __name__ == "__main__":
    pytest_bazel.main()
