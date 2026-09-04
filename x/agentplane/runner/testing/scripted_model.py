"""A loopback model in the vocabulary the runner tests script against.

A test says what the model does (`Text`, `Reasoning`, `ShellCall`) and reads what the harness sent
(`ModelRequest`); the provider subclass owns the wire dialect. That split is what lets one test body
run against both harnesses.
"""

from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass

from x.agentplane.harness_tests.scripted_upstream import ScriptedUpstream, Stream, UpstreamRequest


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
    text: str


@dataclass(frozen=True)
class ModelRequest:
    """What the harness sent upstream, as provider-neutral markers."""

    raw: UpstreamRequest
    # The instruction text the model sees outside the conversation: the harness's system prompt,
    # and any developer preamble it sends alongside.
    system_text: str
    user_texts: list[str]
    assistant_texts: list[str]
    reasoning_texts: list[str]
    tool_outputs: list[ToolOutput]
    streaming: bool


class ScriptedModel(abc.ABC):
    def __init__(self, upstream: ScriptedUpstream, *, model: str) -> None:
        self.upstream = upstream
        self.model = model

    async def request(self, *, timeout_s: float = 30) -> ModelRequest:
        """The next request the harness sends; blocks until it arrives."""
        return self.parse(await asyncio.to_thread(self.upstream.next_request, timeout=timeout_s))

    def reply(self, request: ModelRequest, *items: Item) -> None:
        self.upstream.respond(request.raw, self.stream(list(items)))

    def hold(self, request: ModelRequest) -> None:
        """Begin an answer and never finish it, so the turn stays in flight until interrupted."""
        self.upstream.respond(request.raw, self.opened_stream())

    def assert_quiescent(self) -> None:
        self.upstream.assert_quiescent()

    @abc.abstractmethod
    def parse(self, raw: UpstreamRequest) -> ModelRequest: ...

    @abc.abstractmethod
    def stream(self, items: list[Item]) -> Stream: ...

    @abc.abstractmethod
    def opened_stream(self) -> Stream: ...
