"""The prompt renderer, and the template the console actually deploys."""

from __future__ import annotations

import datetime
from uuid import UUID

import pytest
import pytest_bazel
from jinja2 import UndefinedError

from haku.console.session.system_prompt import HistoryMessage, HistorySender, SessionIntroduction, SystemPromptTemplate

SESSION = UUID("11111111-2222-4333-8444-555555555555")
CONVERSATION = UUID("66666666-7777-4888-8999-aaaaaaaaaaaa")


def introduction(*messages: HistoryMessage, earlier: tuple[UUID, ...] = ()) -> SessionIntroduction:
    return SessionIntroduction(
        session_id=SESSION,
        conversation_id=CONVERSATION,
        workspace="/test/workspace",
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


if __name__ == "__main__":
    pytest_bazel.main()
