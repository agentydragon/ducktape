from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AsyncExitStack

import httpx2
import pytest
from openai import AsyncOpenAI

from agentplane.x.authored_conversation.agent import Agent
from agentplane.x.authored_conversation.settings import EnvironmentCredential, Settings
from agentplane.x.authored_conversation.store import Message, Store, open_store
from agentplane.x.authored_conversation.trace import Trace


@pytest.fixture
def settings(tmp_path) -> Settings:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return Settings(
        base_url="https://model.test/v1",
        model="test-model",
        credential=EnvironmentCredential(kind="environment", name="EXPERIMENT_TEST_KEY"),
        workspace=workspace,
        state_directory=tmp_path / "state",
    )


@pytest.fixture
def store(settings: Settings) -> Iterator[Store]:
    with open_store(settings.state_directory / "thread.sqlite", max_message_chars=settings.max_message_chars) as store:
        yield store


@pytest.fixture
def trace(settings: Settings, store: Store) -> Iterator[Trace]:
    with (settings.state_directory / "trace.jsonl").open("a") as stream:
        yield Trace(stream)


@pytest.fixture
def published() -> list[Message]:
    return []


@pytest.fixture
async def make_agent(
    settings: Settings, store: Store, trace: Trace, published: list[Message]
) -> AsyncIterator[
    Callable[[Callable[[httpx2.Request], httpx2.Response | Awaitable[httpx2.Response]]], Awaitable[Agent]]
]:
    async with AsyncExitStack() as stack:

        async def make(handler) -> Agent:
            http = await stack.enter_async_context(httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
            client = await stack.enter_async_context(
                AsyncOpenAI(api_key="test-credential", base_url=settings.base_url, http_client=http, max_retries=0)
            )
            return Agent(settings, client, store, trace, published.append)

        yield make
