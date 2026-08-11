"""The prompt renderer, and the template the console actually deploys."""

from __future__ import annotations

import datetime
from pathlib import Path
from uuid import UUID

import pytest
import pytest_bazel
from jinja2 import UndefinedError

from haku.console.x.system_prompt import HistoryMessage, SessionIntroduction, SystemPromptTemplate

SESSION = UUID("11111111-2222-4333-8444-555555555555")

# The file the ConfigMap mounts. Rendering the real one is the point: a fixture copy would
# let the shipped template break while the test stayed green.
DEPLOYED_TEMPLATE = Path("cluster/k8s/haku/console/matrix_system_prompt.md.j2")


def introduction(*messages: HistoryMessage) -> SessionIntroduction:
    return SessionIntroduction(
        session_id=SESSION,
        room_id="!room:allegedly.works",
        operator_user_id="@rai:allegedly.works",
        workspace="/workspace",
        recent_messages=messages,
    )


def history(sender: str, body: str, event_id: str = "$event") -> HistoryMessage:
    return HistoryMessage(
        sender=sender, body=body, event_id=event_id, sent_at=datetime.datetime(2026, 8, 11, 3, 14, tzinfo=datetime.UTC)
    )


def test_names_the_session_and_room():
    rendered = SystemPromptTemplate("session {{ session_id }} in {{ room_id }}").render(introduction())
    assert rendered == f"session {SESSION} in !room:allegedly.works"


def test_a_name_the_renderer_does_not_supply_raises():
    """A silently-dropped paragraph is the failure mode this guards against."""
    with pytest.raises(UndefinedError):
        SystemPromptTemplate("{{ nonexistent }}").render(introduction())


def test_message_bodies_are_not_html_escaped():
    rendered = SystemPromptTemplate("{{ recent_messages[0].body }}").render(
        introduction(history("@rai:allegedly.works", 'does 3 < 5 & "quoting" survive?'))
    )
    assert rendered == 'does 3 < 5 & "quoting" survive?'


@pytest.fixture
def deployed() -> SystemPromptTemplate:
    return SystemPromptTemplate.from_path(DEPLOYED_TEMPLATE)


def test_deployed_template_renders_a_fresh_room(deployed: SystemPromptTemplate):
    rendered = deployed.render(introduction())
    assert str(SESSION) in rendered
    assert "!room:allegedly.works" in rendered
    assert "@rai:allegedly.works" in rendered
    assert "no earlier conversation" in rendered
    # The re-awakening section must be gone, not present-and-empty.
    assert "Where the conversation was" not in rendered


def test_deployed_template_carries_both_sides_of_the_history(deployed: SystemPromptTemplate):
    rendered = deployed.render(
        introduction(
            history("@rai:allegedly.works", "did the OA thing happen?", "$first"),
            history("@haku:allegedly.works", "you booked it for Monday", "$second"),
        )
    )
    assert "Where the conversation was" in rendered
    # Haku's own replies are context, not noise: a prompt with only Rai's half reads as a
    # monologue and invites the agent to answer questions it already answered.
    assert "you booked it for Monday" in rendered
    assert "$first" in rendered
    assert "$second" in rendered
    assert "no earlier conversation" not in rendered


def test_deployed_template_keeps_a_multiline_body_inside_its_bullet(deployed: SystemPromptTemplate):
    """An unindented continuation line would read as prompt text rather than as quoted input."""
    rendered = deployed.render(introduction(history("@rai:allegedly.works", "first line\nsecond line")))
    assert "  second line" in rendered


if __name__ == "__main__":
    pytest_bazel.main()
