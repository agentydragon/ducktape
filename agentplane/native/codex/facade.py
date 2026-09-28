"""Typed Codex app-server operations over a lossless native transport.

The facade owns JSON-RPC request ids and typed request/response matching.  It does not consume
notifications or project them into conversation items; that remains the runner session and its
adapter's responsibility.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from agentplane.native.codex import driver, wire
from agentplane.native.transport import NativeTransport


@dataclass(frozen=True)
class CodexReceipt:
    response: wire.Response
    sequence: int


class CodexHarness:
    """Codex-native JSON-RPC operations over an append-only transport."""

    def __init__(self, transport: NativeTransport, *, request_prefix: str) -> None:
        self.transport = transport
        self._request_ids = (f"{request_prefix}-{n}" for n in itertools.count(1))

    async def initialize(self, *, experimental_api: bool = False) -> CodexReceipt:
        response = await self.request(driver.initialize(self._request_id(), experimental_api=experimental_api))
        await self.transport.send(driver.initialized())
        return response

    async def start_thread(
        self,
        *,
        cwd: str,
        model: str,
        effort: str,
        persist: bool = False,
        config: dict[str, Any] | None = None,
        instructions: str = "",
        dynamic_tools: list[dict[str, Any]] | None = None,
    ) -> CodexReceipt:
        return await self.request(
            driver.thread_start(
                self._request_id(),
                cwd=cwd,
                model=model,
                effort=effort,
                persist=persist,
                config=config,
                instructions=instructions,
                dynamic_tools=dynamic_tools,
            )
        )

    async def resume_thread(
        self, *, thread_id: str, base_instructions: str = "", instructions: str = ""
    ) -> CodexReceipt:
        return await self.request(
            driver.thread_resume(
                self._request_id(), thread_id=thread_id, base_instructions=base_instructions, instructions=instructions
            )
        )

    def turn_start_request(self, *, thread_id: str, text: str, model: str | None = None) -> wire.TurnStartRequest:
        """A `turn/start` with this facade's next request id, for a caller that translates the answer
        by that id when it arrives and sends it with `request`."""
        return driver.turn_start(self._request_id(), thread_id=thread_id, text=text, model=model)

    async def start_turn(self, *, thread_id: str, text: str, model: str | None = None) -> CodexReceipt:
        return await self.request(self.turn_start_request(thread_id=thread_id, text=text, model=model))

    async def steer(self, *, thread_id: str, turn_id: str, text: str) -> CodexReceipt:
        return await self.request(driver.steer(self._request_id(), thread_id=thread_id, turn_id=turn_id, text=text))

    async def interrupt(self, *, thread_id: str, turn_id: str) -> CodexReceipt:
        return await self.request(driver.interrupt(self._request_id(), thread_id=thread_id, turn_id=turn_id))

    async def send(self, frame: BaseModel) -> None:
        """Send a typed response generated while translating a server request."""
        await self.transport.send(frame)

    async def request(self, request: wire.Request) -> CodexReceipt:
        """Send `request` and return Codex's response to it."""
        receipt = await self.transport.request(request, matches=lambda frame: frame.get("id") == request.id)
        return CodexReceipt(response=wire.Response.model_validate(receipt.frame), sequence=receipt.sequence)

    def _request_id(self) -> str:
        return next(self._request_ids)
