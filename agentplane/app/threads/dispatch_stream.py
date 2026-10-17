"""The Dispatcher's state of a Thread's commands, pushed to the page that sent them.

A frame is a whole snapshot of the commands the page asked about, as in `app/live.py`, so connect,
change and reconnect share one code path. The Dispatcher's watch only says when to read again.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Collection
from typing import cast
from uuid import UUID

from google.protobuf.json_format import MessageToDict
from pydantic import BaseModel, ConfigDict, Field

from agentplane.app.live import frames
from agentplane.app.threads.dispatcher_client import SessionDispatcherClient
from agentplane.changes import ChangeSignal
from agentplane.proto import session_dispatcher_pb2


class DispatchesSnapshot(BaseModel):
    """A frame of the Thread's dispatch stream."""

    model_config = ConfigDict(extra="forbid")

    dispatches: list[dict[str, object]] = Field(
        description="Proto-JSON Dispatch of each asked-for command the Dispatcher still holds."
    )
    absent: list[str] = Field(
        description="Asked-for commands the Dispatcher holds no row for: its Sandbox was deleted, or the command"
        " is not this Thread's."
    )


class DispatchesHealth(BaseModel):
    """The heartbeat carries nothing; its arrival is the news."""

    model_config = ConfigDict(extra="forbid")


async def read_dispatches(
    dispatcher: SessionDispatcherClient, thread_id: UUID, command_ids: Collection[str]
) -> DispatchesSnapshot:
    response = cast(
        session_dispatcher_pb2.ListDispatchesResponse,
        await dispatcher.unary(
            dispatcher.stub.ListDispatches,
            session_dispatcher_pb2.ListDispatchesRequest(
                filter=session_dispatcher_pb2.DispatchFilter(session_id=str(thread_id))
            ),
        ),
    )
    held = {dispatch.command_id: dispatch for dispatch in response.dispatches}
    return DispatchesSnapshot(
        dispatches=[MessageToDict(held[command_id]) for command_id in command_ids if command_id in held],
        absent=[command_id for command_id in command_ids if command_id not in held],
    )


async def dispatch_frames(
    dispatcher: SessionDispatcherClient, thread_id: UUID, command_ids: Collection[str], *, interval_s: float
) -> AsyncGenerator[bytes]:
    """A snapshot of `command_ids` at connect and at every change of the Thread's Dispatches, and a
    heartbeat every `interval_s` of quiet in between. Ends with the error that ended the watch."""
    changes = ChangeSignal()
    asked = list(dict.fromkeys(command_ids))

    async def notify_changes() -> None:
        async for _ in dispatcher.watch_dispatches(thread_id):
            changes.notify()

    watching = asyncio.create_task(notify_changes())
    # A watch that ended wakes the stream, which then raises why instead of waiting out the quiet.
    watching.add_done_callback(lambda _: changes.notify())
    try:
        async for chunk in frames(
            lambda: read_dispatches(dispatcher, thread_id, asked), DispatchesHealth, changes, interval_s=interval_s
        ):
            if watching.done():
                watching.result()
            yield chunk
    finally:
        watching.cancel()
