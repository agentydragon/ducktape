"""The prompt renderer, and the template the console actually deploys."""

from __future__ import annotations

import datetime
from pathlib import Path
from uuid import UUID

import pytest
import pytest_bazel
from jinja2 import UndefinedError

from haku.console.session.system_prompt import HistoryMessage, HistorySender, SessionIntroduction, SystemPromptTemplate

SESSION = UUID("11111111-2222-4333-8444-555555555555")
CONVERSATION = UUID("66666666-7777-4888-8999-aaaaaaaaaaaa")

# The files the ConfigMap mounts. Rendering the real ones is the point: a fixture copy would let
# the shipped templates break while the test stayed green.
DEPLOYED_TEMPLATE = Path("cluster/k8s/haku/console/haku_system_prompt.md.j2")
DEPLOYED_CODER_TEMPLATE = Path("cluster/k8s/haku/console/public_coder_system_prompt.md.j2")


def introduction(*messages: HistoryMessage, earlier: tuple[UUID, ...] = ()) -> SessionIntroduction:
    return SessionIntroduction(
        session_id=SESSION,
        conversation_id=CONVERSATION,
        workspace="/workspace",
        recent_messages=messages,
        earlier_session_ids=earlier,
    )


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
    """Haku's prompt as the console loads it — the identity template `{% include %}`s the fragment."""
    return SystemPromptTemplate.from_path(DEPLOYED_TEMPLATE)


def test_deployed_coder_template_includes_the_shared_contract():
    """The other launchable Agent includes the same fragment; nothing Haku-only leaks in."""
    rendered = SystemPromptTemplate.from_path(DEPLOYED_CODER_TEMPLATE).render(introduction())
    assert "public-coder-agent" in rendered
    assert "Replies are automatic" in rendered
    assert "Haku Console MCP" in rendered
    assert str(CONVERSATION) in rendered
    assert "the start of it" in rendered
    assert "haku-state" not in rendered.lower()


def test_deployed_template_renders_a_fresh_chat(deployed: SystemPromptTemplate):
    rendered = deployed.render(introduction())
    assert str(SESSION) in rendered
    assert str(CONVERSATION) in rendered
    assert "!room:allegedly.works" not in rendered
    assert "@rai:allegedly.works" not in rendered
    assert "the start of it" in rendered
    # The re-awakening section must be gone, not present-and-empty.
    assert "Where the conversation was" not in rendered


def test_deployed_template_names_earlier_sessions_and_how_to_read_them(deployed: SystemPromptTemplate):
    """The prior-sessions block is rendered context the agent can act on: the ids the
    conversation-history tools take. Absent for a conversation this session starts."""
    earlier = UUID("99999999-8888-4777-8666-555555555555")
    rendered = deployed.render(introduction(earlier=(earlier,)))
    assert str(earlier) in rendered
    assert "read_conversation_items" in rendered
    fresh = deployed.render(introduction())
    assert str(earlier) not in fresh
    assert "earlier sessions" not in fresh


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
    assert "the start of it" not in rendered


def test_deployed_template_includes_mcp_guidance_for_clients_that_hide_server_instructions(
    deployed: SystemPromptTemplate,
):
    """The template owns the guidance prose (a deliberate second copy of the MCP server's own
    instructions, `haku/console/mcp/guidance.py`), so the render is asserted on its load-bearing
    content rather than on string identity with the server constant."""
    rendered = deployed.render(introduction())
    assert "Haku Console MCP" in rendered
    assert "pending_approval" in rendered
    assert "get_mcp_server_status" in rendered
    assert "https://github.com/agentydragon/ducktape" in rendered


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
