"""Authenticated, subject-filtered decision snapshots from the proxy's admin gRPC service."""

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC
from pathlib import Path

import grpc

from agentplane.egress.decisions import Decision, Outcome
from agentplane.grpc.calls import bearer
from agentplane.proto import egress_admin_pb2 as pb, egress_admin_pb2_grpc as rpc
from agentplane.subjects import ServiceAccountRef

# Generated messages use Timestamp methods and protobuf field-presence stubs.
# gazelle:include_dep @pypi//protobuf


class DecisionsUnavailableError(Exception):
    """The proxy could not supply the subject's decisions."""


def _decision(row: pb.Decision) -> Decision:
    return Decision(
        at=row.at.ToDatetime(tzinfo=UTC),
        method=row.method,
        host=row.host,
        port=row.port,
        outcome={pb.Decision.OUTCOME_ALLOW: Outcome.ALLOW, pb.Decision.OUTCOME_DENY: Outcome.DENY}[row.outcome],
        reason=row.reason if row.HasField("reason") else None,
        binding=row.binding if row.HasField("binding") else None,
        policy=row.policy if row.HasField("policy") else None,
        rule=row.rule if row.HasField("rule") else None,
        substituted=row.substituted,
        address=row.address if row.HasField("address") else None,
    )


class DecisionsClient:
    def __init__(self, channel: grpc.aio.Channel, *, token_file: Path, request_timeout_s: float) -> None:
        self._stub = rpc.EgressAdminStub(channel)
        self._token_file = token_file
        self._request_timeout_s = request_timeout_s

    async def watch(self, subject: ServiceAccountRef) -> AsyncGenerator[list[Decision]]:
        """The initial snapshot and each replacement, until the proxy's authorization lease ends.

        Only the first snapshot has a deadline: a quiet subject need not produce another decision.
        The caller reconnects on completion, reading the rotating projected token afresh.
        """
        call = None
        try:
            call = self._stub.WatchDecisions(
                pb.WatchDecisionsRequest(
                    filter=pb.DecisionFilter(subject=pb.Subject(namespace=subject.namespace, name=subject.name))
                ),
                metadata=await bearer(self._token_file),
            )
            handshake: float | None = self._request_timeout_s
            while True:
                async with asyncio.timeout(handshake):
                    snapshot = await call.read()
                if snapshot is grpc.aio.EOF:
                    return
                assert isinstance(snapshot, pb.WatchDecisionsResponse)
                handshake = None
                yield [_decision(row) for row in snapshot.decisions]
        except grpc.aio.AioRpcError as error:
            raise DecisionsUnavailableError(f"{error.code().name}: {error.details()}") from error
        except TimeoutError as error:
            raise DecisionsUnavailableError(f"no initial snapshot within {self._request_timeout_s:g}s") from error
        except OSError as error:
            raise DecisionsUnavailableError(str(error)) from error
        finally:
            if call is not None:
                call.cancel()
