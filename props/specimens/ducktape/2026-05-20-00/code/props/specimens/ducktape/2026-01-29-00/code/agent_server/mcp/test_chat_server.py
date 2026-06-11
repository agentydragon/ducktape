import asyncio
from typing import Any

import pytest
import pytest_bazel
from fastmcp.client import Client
from hamcrest import anything, assert_that, empty, has_properties
from hamcrest.core.matcher import Matcher

from agent_server.mcp.chat.server import (
    ChatAuthor,
    ChatMessage,
    ChatServer,
    ChatStore,
    PostInput,
    PostResult,
    ReadPendingInput,
)
from agent_server.testing.chat_stubs import ChatServerStub
from mcp_infra.stubs.typed_stubs import TypedClient
from mcp_infra.testing.fixtures import ResourceUpdatedCapture
from mcp_utils.resources import extract_single_text_content


@pytest.fixture
def chat_servers() -> tuple[ChatStore, Any, Any]:
    """Create and wire up chat servers with shared store."""
    store = ChatStore()
    human = ChatServer(author=ChatAuthor.USER, store=store)
    assistant = ChatServer(author=ChatAuthor.ASSISTANT, store=store)
    store.register_servers(human=human, assistant=assistant)
    return store, human, assistant


@pytest.fixture
async def human_session(chat_servers):
    """Open human client session."""
    _store, human, _assistant = chat_servers
    async with Client(human) as sess:
        yield human, sess


@pytest.fixture
async def assistant_session(chat_servers):
    """Open assistant client session."""
    _store, _human, assistant = chat_servers
    async with Client(assistant) as sess:
        yield assistant, sess


def user_markdown_message(content: str, *, id: Matcher[int] | int | None = None) -> Matcher[ChatMessage]:
    """Matcher for user markdown messages."""
    if id is None:
        id = anything()
    return has_properties(author=ChatAuthor.USER, mime="text/markdown", content=content, id=id)


def assistant_markdown_message(content: str, *, id: Matcher[int] | int | None = None) -> Matcher[ChatMessage]:
    """Matcher for assistant markdown messages."""
    if id is None:
        id = anything()
    return has_properties(author=ChatAuthor.ASSISTANT, mime="text/markdown", content=content, id=id)


async def test_chat_flow_user_to_agent_then_agent_to_user(human_session, assistant_session) -> None:
    human, human_sess = human_session
    assistant, assistant_sess = assistant_session

    h = ChatServerStub.from_server(human, human_sess)
    a = ChatServerStub.from_server(assistant, assistant_sess)

    # Initially, assistant has nothing pending
    page0 = await a.read_pending_messages(ReadPendingInput(limit=100))
    assert_that(page0.messages, empty())

    # Human posts two messages
    for txt in ("hello", "world"):
        p = await h.post(PostInput(mime="text/markdown", content=txt))
        assert p.id

    # Assistant reads pending (should get both user messages once)
    page = await a.read_pending_messages(ReadPendingInput(limit=100))
    messages = page.messages
    assert len(messages) == 2
    assert messages[0].author == ChatAuthor.USER
    assert messages[0].mime == "text/markdown"
    assert messages[0].content == "hello"
    assert messages[1].author == ChatAuthor.USER
    assert messages[1].mime == "text/markdown"
    assert messages[1].content == "world"

    # Second read should be empty (HWM advanced)
    page2 = await a.read_pending_messages(ReadPendingInput(limit=100))
    assert_that(page2.messages, empty())

    # Assistant replies; human reads it pending
    reply = await a.post(PostInput(mime="text/markdown", content="roger"))
    assert reply.id is not None
    hpage = await h.read_pending_messages(ReadPendingInput(limit=100))
    assert len(hpage.messages) == 1
    assert_that(hpage.messages[0], assistant_markdown_message("roger"))


async def test_chat_head_notifications_other_participant(chat_servers) -> None:
    _store, human, assistant = chat_servers

    # Assistant notifications on human posts
    cap_assist = ResourceUpdatedCapture()
    async with Client(assistant, message_handler=cap_assist) as assist_sess, Client(human) as human_sess:
        h_post = TypedClient.from_server(human, human_sess).stub("post", PostResult)
        await h_post(PostInput(mime="text/markdown", content="hello"))
        await asyncio.sleep(0.05)
        assert assistant.head_resource.uri in cap_assist.updated, cap_assist.updated

    # Human notifications on assistant posts
    cap_human = ResourceUpdatedCapture()
    async with Client(human, message_handler=cap_human) as human_sess, Client(assistant) as assist_sess:
        a_post = TypedClient.from_server(assistant, assist_sess).stub("post", PostResult)
        await a_post(PostInput(mime="text/markdown", content="roger"))
        await asyncio.sleep(0.05)
        assert human.head_resource.uri in cap_human.updated, cap_human.updated


async def test_chat_last_read_updates_with_read_pending(chat_servers) -> None:
    """Test last-read updates with message handler (can't use session fixtures)."""
    _store, human, assistant = chat_servers

    # Attach a capture handler to the assistant server where read_pending is called
    cap_assist = ResourceUpdatedCapture()
    async with Client(assistant, message_handler=cap_assist) as assistant_sess, Client(human) as human_sess:
        h = ChatServerStub.from_server(human, human_sess)
        a = ChatServerStub.from_server(assistant, assistant_sess)

        # Human posts one message; assistant HWM should advance on read and emit last-read update
        await h.post(PostInput(mime="text/markdown", content="one"))
        cap_assist.updated.clear()
        await a.read_pending_messages(ReadPendingInput(limit=100))
        await asyncio.sleep(0.05)
        assert assistant.last_read_resource.uri in cap_assist.updated, cap_assist.updated

        # Reading again without new messages should not advance HWM or emit another last-read update
        cap_assist.updated.clear()
        await a.read_pending_messages(ReadPendingInput(limit=100))
        await asyncio.sleep(0.05)
        assert not cap_assist.updated


async def test_chat_resources_return_ints_directly(human_session, assistant_session) -> None:
    """Verify that head and last_read resources return int | None directly, not wrapped in dicts."""
    human, human_sess = human_session
    assistant, assistant_sess = assistant_session

    h = ChatServerStub.from_server(human, human_sess)
    a = ChatServerStub.from_server(assistant, assistant_sess)

    # Initially, head should be None (no messages)
    head_contents = await human_sess.read_resource(human.head_resource.uri)
    head_text = extract_single_text_content(head_contents)
    assert head_text == "null"

    # Post a message
    result = await h.post(PostInput(mime="text/markdown", content="test"))
    msg_id = result.id

    # Now head should return the message ID as an integer
    head_contents = await human_sess.read_resource(human.head_resource.uri)
    head_text = extract_single_text_content(head_contents)
    assert head_text == str(msg_id)

    # Last-read should initially be None
    last_read_contents = await assistant_sess.read_resource(assistant.last_read_resource.uri)
    last_read_text = extract_single_text_content(last_read_contents)
    assert last_read_text == "null"

    # After reading, last-read should be the message ID
    await a.read_pending_messages(ReadPendingInput(limit=100))
    last_read_contents = await assistant_sess.read_resource(assistant.last_read_resource.uri)
    last_read_text = extract_single_text_content(last_read_contents)
    assert last_read_text == str(msg_id)


if __name__ == "__main__":
    pytest_bazel.main()
