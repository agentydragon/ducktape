"""A gRPC stand-in for the egress proxy's admin service."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import override

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from agentplane.proto import egress_admin_pb2 as pb, egress_admin_pb2_grpc as rpc


class FakeEgressAdmin(rpc.EgressAdminServicer):
    def __init__(self) -> None:
        self.decisions: dict[tuple[str, str], list[pb.Decision]] = {}
        self.reachable = True
        self.changed = asyncio.Event()
        self.end_stream = False
        self.tokens: list[str | None] = []

    def notify(self) -> None:
        self.changed.set()

    def expire_lease(self) -> None:
        self.end_stream = True
        self.notify()

    @override
    async def WatchDecisions(
        self, request: pb.WatchDecisionsRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[pb.WatchDecisionsResponse]:
        subject = (request.filter.subject.namespace, request.filter.subject.name)
        self.tokens.append(
            next((str(value) for key, value in context.invocation_metadata() or () if key == "authorization"), None)
        )
        if not self.reachable:
            await context.abort(grpc.StatusCode.UNAVAILABLE, "egress proxy unavailable")

        yield pb.WatchDecisionsResponse(decisions=self.decisions.get(subject, []))
        while True:
            await self.changed.wait()
            self.changed.clear()
            if not self.reachable:
                await context.abort(grpc.StatusCode.UNAVAILABLE, "egress proxy unavailable")
            if self.end_stream:
                self.end_stream = False
                return
            yield pb.WatchDecisionsResponse(decisions=self.decisions.get(subject, []))


def decision(
    at: str, method: str, host: str, outcome: str, *, reason: str | None = None, address: str | None = None
) -> pb.Decision:
    """One admission record as the proxy returns it over gRPC."""
    timestamp = Timestamp()
    timestamp.FromJsonString(at)
    row = pb.Decision(
        at=timestamp,
        method=method,
        host=host,
        port=443,
        outcome=pb.Decision.OUTCOME_ALLOW if outcome == "allow" else pb.Decision.OUTCOME_DENY,
        substituted=outcome == "allow" and method != "CONNECT",
    )
    if reason is not None:
        row.reason = reason
    if outcome == "allow":
        row.binding = "sandboxes-github-agentydragon-agent"
        row.policy = "github-agentydragon-agent"
        row.rule = 0
        if address is not None:
            row.address = address
    return row
