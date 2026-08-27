"""The prompt renderer, and the template the console actually deploys."""

from __future__ import annotations

import datetime
from pathlib import Path
from uuid import UUID

import pytest
import pytest_bazel
from jinja2 import Environment, StrictUndefined, UndefinedError

from haku.console.mcp_guidance import SERVER_INSTRUCTIONS
from haku.console.x.system_prompt import HistoryMessage, HistorySender, SessionIntroduction, SystemPromptTemplate

SESSION = UUID("11111111-2222-4333-8444-555555555555")

# The file the ConfigMap mounts. Rendering the real one is the point: a fixture copy would let the
# shipped template break while the test stayed green.
DEPLOYED_TEMPLATE = Path("cluster/k8s/haku/console/chat_system_prompt.md.j2")


def introduction(*messages: HistoryMessage) -> SessionIntroduction:
    return SessionIntroduction(session_id=SESSION, workspace="/workspace", recent_messages=messages)


def history(sender: HistorySender, body: str) -> HistoryMessage:
    return HistoryMessage(sender=sender, body=body, sent_at=datetime.datetime(2026, 8, 11, 3, 14, tzinfo=datetime.UTC))


def test_names_the_session_without_a_channel_address():
    rendered = SystemPromptTemplate("session {{ session_id }}").render(introduction())
    assert rendered == f"session {SESSION}"


def test_a_name_the_renderer_does_not_supply_raises():
    """A silently-dropped paragraph is the failure mode this guards against."""
    with pytest.raises(UndefinedError):
        SystemPromptTemplate("{{ nonexistent }}").render(introduction())


def test_message_bodies_are_not_html_escaped():
    rendered = SystemPromptTemplate("{{ recent_messages[0].body }}").render(
        introduction(history(HistorySender.OPERATOR, 'does 3 < 5 & "quoting" survive?'))
    )
    assert rendered == 'does 3 < 5 & "quoting" survive?'


@pytest.fixture
def deployed() -> SystemPromptTemplate:
    return SystemPromptTemplate.from_path(DEPLOYED_TEMPLATE)


def test_deployed_template_renders_a_fresh_chat(deployed: SystemPromptTemplate):
    rendered = deployed.render(introduction())
    assert str(SESSION) in rendered
    assert "!room:allegedly.works" not in rendered
    assert "@rai:allegedly.works" not in rendered
    assert "no earlier conversation" in rendered
    # The re-awakening section must be gone, not present-and-empty.
    assert "Where the conversation was" not in rendered


def test_deployed_template_carries_both_sides_of_the_history(deployed: SystemPromptTemplate):
    rendered = deployed.render(
        introduction(
            history(HistorySender.OPERATOR, "did the OA thing happen?"),
            history(HistorySender.ASSISTANT, "you booked it for Monday"),
        )
    )
    assert "Where the conversation was" in rendered
    assert "did the OA thing happen?" in rendered
    # Haku's own replies are context, not noise: a prompt with only Rai's half reads as a
    # monologue and invites the agent to answer questions it already answered.
    assert "you booked it for Monday" in rendered
    assert "no earlier conversation" not in rendered


def test_deployed_template_includes_mcp_guidance_for_clients_that_hide_server_instructions(
    deployed: SystemPromptTemplate,
):
    rendered = deployed.render(introduction())
    guidance = SERVER_INSTRUCTIONS
    assert guidance in rendered
    assert "pending_approval" in rendered
    assert "get_mcp_server_status" in rendered
    assert "https://github.com/agentydragon/ducktape" in rendered


def test_deployed_template_tolerates_the_previous_renderer_during_config_rollout() -> None:
    """The ConfigMap may reach current replicas before the follow-up image supplies guidance."""
    rendered = (
        Environment(undefined=StrictUndefined)
        .from_string(DEPLOYED_TEMPLATE.read_text())
        .render(session_id=SESSION, workspace="/workspace", recent_messages=[])
    )
    assert "Haku Console MCP" not in rendered
    assert SERVER_INSTRUCTIONS not in rendered


def test_deployed_template_points_at_the_index_whether_or_not_history_was_replayed(deployed: SystemPromptTemplate):
    """Recall is the standing instruction: mentioning it only where history was replayed would teach
    it as a special case of re-awakening."""
    for rendered in (
        deployed.render(introduction()),
        deployed.render(introduction(history(HistorySender.OPERATOR, "did the OA thing happen?"))),
    ):
        assert "haku_index" in rendered


def test_deployed_template_keeps_a_multiline_body_inside_its_bullet(deployed: SystemPromptTemplate):
    """An unindented continuation line would read as prompt text rather than as quoted input."""
    rendered = deployed.render(introduction(history(HistorySender.OPERATOR, "first line\nsecond line")))
    assert "  second line" in rendered


if __name__ == "__main__":
    pytest_bazel.main()
