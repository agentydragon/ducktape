"""A loopback model in the vocabulary the runner tests script against.

A test says what the model does (`Text`, `Reasoning`, `ShellCall`) and reads what the harness sent
(`ModelRequest`); the harness subclass owns the wire dialect. That split is what lets one test body
run against both harnesses.
"""

from __future__ import annotations

import abc
import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from agentplane.native.testing.model_endpoint import JsonResponse, ModelExchange, SseEvent, StreamableRequest


@dataclass(frozen=True)
class Text:
    text: str


@dataclass(frozen=True)
class Reasoning:
    text: str


@dataclass(frozen=True)
class ShellCall:
    call_id: str
    command: str


Item = Text | Reasoning | ShellCall


@dataclass(frozen=True)
class ToolOutput:
    call_id: str
    # The result's text blocks in order, as the harness sent them; a plain-string result is one block.
    blocks: list[str]

    def contains(self, text: str) -> bool:
        return any(text in block for block in self.blocks)


@dataclass(frozen=True)
class ModelRequest[RequestT: StreamableRequest]:
    """What the harness sent upstream, as native-protocol-neutral markers."""

    _exchange: ModelExchange[RequestT]
    model: str
    effort: str | None
    # The instruction text the model sees outside the conversation: the harness's system prompt,
    # and any developer preamble it sends alongside.
    system_text: str
    user_texts: list[str]
    assistant_texts: list[str]
    reasoning_texts: list[str]
    tool_outputs: list[ToolOutput]
    tool_calls: list[str]
    streaming: bool

    @property
    def native(self) -> RequestT:
        """The request in the harness's own wire dialect."""
        return self._exchange.request


class ScriptedModel[RequestT: StreamableRequest](abc.ABC):
    def __init__(self, *, model: str) -> None:
        self.model = model
        self.request_count = 0
        self._held_client_closures: list[asyncio.Task[None]] = []

    async def request(self) -> ModelRequest[RequestT]:
        """The next typed model request the harness sends."""
        self.request_count += 1
        return self.parse(await self.next_exchange())

    async def reply(self, request: ModelRequest[RequestT], *items: Item) -> None:
        async with request._exchange as exchange:
            await exchange.send(*self.stream(request, list(items)))

    async def http_error(self, request: ModelRequest[RequestT], *, status: int = 502) -> None:
        """Return an HTTP error before streaming any model content."""
        async with request._exchange as exchange:
            await exchange.respond(
                JsonResponse(
                    b'{"type":"error","error":{"type":"api_error","message":"scripted upstream failure"}}',
                    status=status,
                )
            )

    async def drop_connection(self, request: ModelRequest[RequestT], *, after_text: str | None = None) -> None:
        """Break a model response before content or after its first visible text delta."""
        async with request._exchange as exchange:
            if after_text is not None:
                stream = self.stream(request, [Text(after_text)])
                delta = next(
                    i for i, event in enumerate(stream) if event.kind in ("text_delta", "response.output_text.delta")
                )
                await exchange.send(*stream[: delta + 1])
            await exchange.abort()

    async def hold(self, request: ModelRequest[RequestT]) -> None:
        """Begin an answer and never finish it, so the turn stays in flight until interrupted."""
        await request._exchange.send(*self.opened_stream())
        self._hold_until_client_closes(request)

    async def hold_after_first_delta(self, request: ModelRequest[RequestT], item: Item) -> None:
        """Send one streamed fragment, then leave the model response open until the client closes."""
        await self.hold_through(request, [item], lambda event: event.kind.endswith("delta"))

    async def hold_through(
        self, request: ModelRequest[RequestT], items: list[Item], stop: Callable[[SseEvent], bool]
    ) -> None:
        """Send the answer through its first event matching `stop`, then leave it open until the client closes."""
        events = self.stream(request, items)
        end = next(index for index, event in enumerate(events) if stop(event))
        await request._exchange.send(*events[: end + 1])
        self._hold_until_client_closes(request)

    def _hold_until_client_closes(self, request: ModelRequest[RequestT]) -> None:
        # The endpoint remains strict at fixture teardown: the runner must close this held stream
        # when it stops its harness, and this task records that explicit client-side closure.
        self._held_client_closures.append(asyncio.create_task(request._exchange.wait_client_closed()))

    @abc.abstractmethod
    async def next_exchange(self) -> ModelExchange[RequestT]: ...

    @abc.abstractmethod
    def parse(self, exchange: ModelExchange[RequestT]) -> ModelRequest[RequestT]: ...

    @abc.abstractmethod
    def stream(self, request: ModelRequest[RequestT], items: list[Item]) -> tuple[SseEvent, ...]:
        """The answer to `request`, in the tools it offers: Codex offers its shell directly or through code mode."""

    @abc.abstractmethod
    def opened_stream(self) -> tuple[SseEvent, ...]: ...
