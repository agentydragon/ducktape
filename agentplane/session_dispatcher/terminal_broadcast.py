"""Transactional PostgreSQL broadcast of deleted-Sandbox terminal snapshots."""

import asyncio
import base64
import binascii
import json
import logging
from dataclasses import dataclass, field
from uuid import UUID

from google.protobuf.message import DecodeError
from sqlalchemy import func, select
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession

from agentplane.postgres.listener import PostgresListener
from agentplane.proto import session_dispatcher_pb2
from agentplane.session_dispatcher.ended_dispatches import EndedDispatches, TerminalEvent
from agentplane.session_dispatcher.models import Reason

logger = logging.getLogger(__name__)

CHANNEL = "agentplane_dispatcher_terminal"
MAX_NOTIFY_BYTES = 7900
CHUNK_BYTES = 4000
MAX_PARTS = 2048
MAX_PENDING_EVENTS = 32
ASSEMBLY_TIMEOUT_S = 10.0
_NOTIFICATION_PARSE_ERRORS = (ValueError, TypeError, KeyError, binascii.Error)
_PROTOBUF_PARSE_ERRORS = (ValueError, DecodeError)


@dataclass
class _Parts:
    part_count: int
    values: dict[int, bytes] = field(default_factory=dict)
    timeout: asyncio.TimerHandle | None = None


def _payloads(event: TerminalEvent) -> list[str]:
    serialized = event.dispatch.SerializeToString(deterministic=True)
    chunks = [serialized[offset : offset + CHUNK_BYTES] for offset in range(0, len(serialized), CHUNK_BYTES)] or [b""]
    if len(chunks) > MAX_PARTS:
        raise ValueError(f"terminal snapshot requires too many notification parts: {len(chunks)}")
    payloads = []
    for number, chunk in enumerate(chunks):
        payload = json.dumps(
            {
                "event_id": event.event_id,
                "part": number,
                "part_count": len(chunks),
                "data": base64.b64encode(chunk).decode("ascii"),
            },
            separators=(",", ":"),
        )
        if len(payload.encode("utf-8")) > MAX_NOTIFY_BYTES:
            raise ValueError("terminal notification payload exceeds PostgreSQL's safe payload bound")
        payloads.append(payload)
    return payloads


class TerminalBroadcast:
    """Sends one encoded protobuf snapshot per event and fans complete events to local streams."""

    def __init__(self, url: URL, ended: EndedDispatches, *, assembly_timeout_s: float = ASSEMBLY_TIMEOUT_S) -> None:
        self.ended = ended
        self.assembly_timeout_s = assembly_timeout_s
        self._pending: dict[str, _Parts] = {}
        self._generation = 0
        self.listener = PostgresListener(
            url,
            channels=(CHANNEL,),
            application_name="agentplane-dispatcher-terminal",
            notified=self._notified,
            invalidated=self._invalidated,
        )

    async def notify(self, session: AsyncSession, events: list[TerminalEvent]) -> None:
        """Queue terminal snapshots in the same transaction that deletes their Dispatch rows."""
        for event in events:
            for payload in _payloads(event):
                await session.execute(select(func.pg_notify(CHANNEL, payload)))

    def _invalidated(self) -> None:
        generation = self.listener.generation
        connected = self.listener.connected
        if self._generation and generation != self._generation:
            self._clear_pending()
            self.ended.fail_active("terminal event listener generation changed; reconnect this WatchDispatches stream")
        self._generation = generation
        self.ended.listener_state_changed(connected=connected)
        if not connected:
            self._clear_pending()

    def _notified(self, channel: str, payload: object) -> None:
        if channel != CHANNEL or not isinstance(payload, str):
            self._fail_active("terminal event notification was malformed; reconnect this WatchDispatches stream")
            return
        try:
            event_id, number, count, chunk = self._parse_payload(payload)
        except _NOTIFICATION_PARSE_ERRORS:
            logger.warning("discarding malformed terminal event notification")
            self._fail_active("terminal event notification was malformed; reconnect this WatchDispatches stream")
            return
        if self.ended.has_published(event_id):
            return
        parts = self._pending.get(event_id)
        if parts is None:
            if len(self._pending) >= MAX_PENDING_EVENTS:
                self._fail_active("terminal event assembly limit was exceeded; reconnect this WatchDispatches stream")
                return
            parts = _Parts(count)
            self._pending[event_id] = parts
            parts.timeout = asyncio.get_running_loop().call_later(self.assembly_timeout_s, self._incomplete, event_id)
        elif parts.part_count != count:
            self._fail_active("terminal event part count changed; reconnect this WatchDispatches stream")
            return
        previous = parts.values.get(number)
        if previous is not None and previous != chunk:
            self._fail_active("terminal event contained conflicting parts; reconnect this WatchDispatches stream")
            return
        parts.values[number] = chunk
        if len(parts.values) != parts.part_count:
            return
        if any(len(parts.values[index]) != CHUNK_BYTES for index in range(parts.part_count - 1)):
            self._fail_active("terminal event had a missing or short part; reconnect this WatchDispatches stream")
            return
        if len(parts.values[parts.part_count - 1]) > CHUNK_BYTES:
            self._fail_active("terminal event part exceeded its bound; reconnect this WatchDispatches stream")
            return
        self._pending.pop(event_id)
        if parts.timeout is not None:
            parts.timeout.cancel()
        dispatch = session_dispatcher_pb2.Dispatch()
        try:
            dispatch.ParseFromString(b"".join(parts.values[index] for index in range(parts.part_count)))
        except _PROTOBUF_PARSE_ERRORS:
            self._fail_active("terminal event protobuf was invalid; reconnect this WatchDispatches stream")
            return
        if (
            not dispatch.command_id
            or dispatch.reason != Reason.SANDBOX_DELETED
            or dispatch.state
            not in {session_dispatcher_pb2.DELIVERY_STATE_FAILED, session_dispatcher_pb2.DELIVERY_STATE_UNKNOWN}
        ):
            self._fail_active("terminal event snapshot did not describe Sandbox deletion; reconnect this stream")
            return
        self.ended.publish([TerminalEvent(event_id, dispatch)])

    def _parse_payload(self, payload: str) -> tuple[str, int, int, bytes]:
        value = json.loads(payload)
        if not isinstance(value, dict) or set(value) != {"event_id", "part", "part_count", "data"}:
            raise ValueError("unexpected terminal event fields")
        event_id, number, count, data = (value[key] for key in ("event_id", "part", "part_count", "data"))
        if not isinstance(event_id, str):
            raise ValueError("invalid terminal event ID")
        UUID(event_id)
        if type(number) is not int or type(count) is not int or not 1 <= count <= MAX_PARTS or not 0 <= number < count:
            raise ValueError("invalid terminal event part number")
        if not isinstance(data, str):
            raise ValueError("invalid terminal event data")
        chunk = base64.b64decode(data, validate=True)
        if len(chunk) > CHUNK_BYTES:
            raise ValueError("terminal event part is too large")
        return event_id, number, count, chunk

    def _incomplete(self, event_id: str) -> None:
        if event_id in self._pending:
            self._fail_active("terminal event parts were incomplete; reconnect this WatchDispatches stream")

    def _fail_active(self, message: str) -> None:
        self._clear_pending()
        self.ended.fail_active(message)

    def _clear_pending(self) -> None:
        for parts in self._pending.values():
            if parts.timeout is not None:
                parts.timeout.cancel()
        self._pending.clear()
