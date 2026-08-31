"""Contract tests for the prompt templates projected into the Haku Console ConfigMap."""

from __future__ import annotations

import datetime
from pathlib import Path
from uuid import UUID

import pytest
import pytest_bazel

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


@pytest.fixture
def deployed(k8s_dir: Path) -> SystemPromptTemplate:
    return SystemPromptTemplate.from_path(k8s_dir / "haku/console/haku_system_prompt.md.j2")


def test_deployed_coder_template_includes_the_shared_contract(k8s_dir: Path) -> None:
    rendered = SystemPromptTemplate.from_path(k8s_dir / "haku/console/public_coder_system_prompt.md.j2").render(
        introduction()
    )

    assert "public-coder-agent" in rendered
    assert "Replies are automatic" in rendered
    assert "Haku Console MCP" in rendered
    assert "proxy-github-placeholder" in rendered
    assert "Do repository work from the shell" in rendered
    assert "yours until CI is green" in rendered
    assert str(CONVERSATION) in rendered
    assert "the start of it" in rendered
    assert "haku-state" not in rendered.lower()


def test_deployed_template_renders_a_fresh_chat(deployed: SystemPromptTemplate) -> None:
    rendered = deployed.render(introduction())

    assert str(SESSION) in rendered
    assert str(CONVERSATION) in rendered
    assert "!room:allegedly.works" not in rendered
    assert "@rai:allegedly.works" not in rendered
    assert "the start of it" in rendered
    assert "Where the conversation was" not in rendered


def test_deployed_template_names_earlier_sessions_and_how_to_read_them(deployed: SystemPromptTemplate) -> None:
    earlier = UUID("99999999-8888-4777-8666-555555555555")
    rendered = deployed.render(introduction(earlier=(earlier,)))

    assert str(earlier) in rendered
    assert "read_conversation_items" in rendered
    fresh = deployed.render(introduction())
    assert str(earlier) not in fresh
    assert "earlier sessions" not in fresh


def test_deployed_template_carries_both_sides_of_the_history(deployed: SystemPromptTemplate) -> None:
    rendered = deployed.render(
        introduction(
            history(HistorySender.OPERATOR, "did the OA thing happen?"),
            history(HistorySender.ASSISTANT, "you booked it for Monday"),
        )
    )

    assert "Where the conversation was" in rendered
    assert "did the OA thing happen?" in rendered
    assert "you booked it for Monday" in rendered
    assert "the start of it" not in rendered


def test_deployed_template_includes_mcp_guidance_for_clients_that_hide_server_instructions(
    deployed: SystemPromptTemplate,
) -> None:
    rendered = deployed.render(introduction())

    assert "Haku Console MCP" in rendered
    assert "pending_approval" in rendered
    assert "get_mcp_server_status" in rendered
    assert "https://github.com/agentydragon/ducktape" in rendered


def test_deployed_template_points_at_the_index_with_or_without_history(deployed: SystemPromptTemplate) -> None:
    for rendered in (
        deployed.render(introduction()),
        deployed.render(introduction(history(HistorySender.OPERATOR, "did the OA thing happen?"))),
    ):
        assert "haku_index" in rendered


def test_deployed_template_keeps_a_multiline_body_inside_its_bullet(deployed: SystemPromptTemplate) -> None:
    rendered = deployed.render(introduction(history(HistorySender.OPERATOR, "first line\nsecond line")))

    assert "  second line" in rendered


if __name__ == "__main__":
    pytest_bazel.main()
