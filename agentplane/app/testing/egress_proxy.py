"""A gRPC stand-in for the egress proxy's admin service."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import override

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from agentplane.egress.api.admin import egress_admin_pb2 as pb, egress_admin_pb2_grpc as rpc


class FakeEgressAdmin(rpc.EgressAdminServicer):
    def __init__(self) -> None:
        self.decisions: dict[tuple[str, str], list[pb.Decision]] = {}
        self.changed = asyncio.Event()
        self.tokens: list[str | None] = []

    def notify(self) -> None:
        self.changed.set()

    @override
    async def WatchDecisions(
        self, request: pb.WatchDecisionsRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[pb.WatchDecisionsResponse]:
        subject = (request.filter.subject.namespace, request.filter.subject.name)
        self.tokens.append(
            next((str(value) for key, value in context.invocation_metadata() or () if key == "authorization"), None)
        )

        previous = {item.event_id: item for item in self.decisions.get(subject, [])}
        yield pb.WatchDecisionsResponse(snapshot=pb.DecisionSnapshot(decisions=list(previous.values())))
        while True:
            await self.changed.wait()
            self.changed.clear()
            current = {item.event_id: item for item in self.decisions.get(subject, [])}
            delta = pb.DecisionDelta(
                added=[item for key, item in current.items() if key not in previous],
                removed_event_ids=[key for key in previous if key not in current],
            )
            previous = current
            if delta.added or delta.removed_event_ids:
                yield pb.WatchDecisionsResponse(delta=delta)


def decision(at: str, method: str, host: str, outcome: str) -> pb.Decision:
    """One admission record as the proxy returns it over gRPC."""
    timestamp = Timestamp()
    timestamp.FromJsonString(at)
    row = pb.Decision(
        event_id=str(uuid.uuid4()),
        at=timestamp,
        method=method,
        host=host,
        port=443,
        outcome=pb.Decision.OUTCOME_ALLOW if outcome == "allow" else pb.Decision.OUTCOME_DENY,
        substituted=outcome == "allow" and method != "CONNECT",
    )
    if outcome == "allow":
        row.binding = "test-sandbox-github-writes"
        row.policy = "test-github-writes"
        row.rule = 0
    return row
