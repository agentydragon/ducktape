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

from x.agentplane.native.codex import driver, wire
from x.agentplane.native.transport import NativeTransport


@dataclass(frozen=True)
class CodexReceipt:
    response: wire.Response
    sequence: int


class CodexHarness:
    """Codex-native JSON-RPC operations over an append-only transport."""

    def __init__(self, transport: NativeTransport, *, request_prefix: str) -> None:
        self.transport = transport
        self._request_ids = (f"{request_prefix}-{n}" for n in itertools.count(1))

    async def initialize(self) -> CodexReceipt:
        response = await self._request(driver.initialize(self._request_id()))
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
    ) -> CodexReceipt:
        return await self._request(
            driver.thread_start(
                self._request_id(),
                cwd=cwd,
                model=model,
                effort=effort,
                persist=persist,
                config=config,
                instructions=instructions,
            )
        )

    async def resume_thread(
        self, *, thread_id: str, base_instructions: str = "", instructions: str = ""
    ) -> CodexReceipt:
        return await self._request(
            driver.thread_resume(
                self._request_id(), thread_id=thread_id, base_instructions=base_instructions, instructions=instructions
            )
        )

    async def start_turn(self, *, thread_id: str, text: str, model: str | None = None) -> CodexReceipt:
        return await self._request(driver.turn_start(self._request_id(), thread_id=thread_id, text=text, model=model))

    async def steer(self, *, thread_id: str, turn_id: str, text: str) -> CodexReceipt:
        return await self._request(driver.steer(self._request_id(), thread_id=thread_id, turn_id=turn_id, text=text))

    async def interrupt(self, *, thread_id: str, turn_id: str) -> CodexReceipt:
        return await self._request(driver.interrupt(self._request_id(), thread_id=thread_id, turn_id=turn_id))

    async def send(self, frame: BaseModel) -> None:
        """Send a typed response generated while translating a server request."""
        await self.transport.send(frame)

    async def _request(self, request: wire.Request) -> CodexReceipt:
        receipt = await self.transport.request(request, matches=lambda frame: frame.get("id") == request.id)
        return CodexReceipt(response=wire.Response.model_validate(receipt.frame), sequence=receipt.sequence)

    def _request_id(self) -> str:
        return next(self._request_ids)
