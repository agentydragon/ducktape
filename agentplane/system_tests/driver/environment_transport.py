"""Native Controller EnvironmentService transport for system-test lifecycle ownership."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Self

import grpc
from google.protobuf.empty_pb2 import Empty
from google.protobuf.message import Message

from agentplane.environments.v1 import environment_service_pb2, environment_service_pb2_grpc
from agentplane.grpc.calls import call_unary, dial, raise_status
from agentplane.proto import identity_pb2

ENVIRONMENT_SERVICE = "environments"
ENVIRONMENT_PORT = 8082
RECONNECT_PAUSE_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 30.0


class EnvironmentServiceClient:
    """Keep generated RPCs and complete-target checks at the transport boundary."""

    def __init__(self, channel: grpc.aio.Channel) -> None:
        self.stub = environment_service_pb2_grpc.EnvironmentServiceStub(channel)

    @classmethod
    @asynccontextmanager
    async def connect(
        cls, target: str, *, channel_credentials: grpc.ChannelCredentials, server_name: str
    ) -> AsyncIterator[Self]:
        async with dial(target, channel_credentials=channel_credentials, server_name=server_name) as channel:
            yield cls(channel)

    async def unary[ResponseT: Message](self, call: Callable[..., Awaitable[ResponseT]], request: Message) -> ResponseT:
        return await call_unary(call, request, timeout_s=REQUEST_TIMEOUT_SECONDS)

    async def create_environment(
        self, request: environment_service_pb2.CreateEnvironmentRequest
    ) -> identity_pb2.EnvironmentTarget:
        target = await self.unary(self.stub.CreateEnvironment, request)
        self._require_target(target)
        return target

    async def get_environment(
        self, target: identity_pb2.EnvironmentTarget
    ) -> environment_service_pb2.EnvironmentDescriptor:
        requested = self._copy_target(target)
        descriptor = await self.unary(
            self.stub.GetEnvironment, environment_service_pb2.GetEnvironmentRequest(target=requested)
        )
        if not descriptor.HasField("target") or descriptor.target != requested:
            raise RuntimeError(f"EnvironmentService returned a different target for {requested}")
        return descriptor

    async def suspend_environment(self, target: identity_pb2.EnvironmentTarget) -> None:
        await self.unary(
            self.stub.SuspendEnvironment,
            environment_service_pb2.EnvironmentMutationRequest(target=self._copy_target(target)),
        )

    async def resume_environment(
        self, target: identity_pb2.EnvironmentTarget
    ) -> environment_service_pb2.ResumeEnvironmentResponse:
        requested = self._copy_target(target)
        response = await self.unary(
            self.stub.ResumeEnvironment, environment_service_pb2.ResumeEnvironmentRequest(target=requested)
        )
        if response.target != requested or response.outcome not in (
            environment_service_pb2.RESUME_OUTCOME_REQUESTED,
            environment_service_pb2.RESUME_OUTCOME_ALREADY_RUNNING,
        ):
            raise RuntimeError(f"EnvironmentService resume was not accepted for {requested}: {response}")
        return response

    async def environment_snapshots(self) -> AsyncGenerator[list[environment_service_pb2.EnvironmentDescriptor]]:
        """Yield complete inventories; only UNAVAILABLE reconnects into a fresh snapshot."""
        while True:
            pending: dict[tuple[str, str], environment_service_pb2.EnvironmentDescriptor] | None = {}
            current: dict[tuple[str, str], environment_service_pb2.EnvironmentDescriptor] = {}
            reconnect = False
            stream = self.stub.WatchEnvironments(Empty(), metadata=())
            try:
                async for frame in stream:
                    match frame.WhichOneof("frame"):
                        case "snapshot":
                            if pending is None:
                                raise RuntimeError("unexpected Environment snapshot chunk")
                            for descriptor in frame.snapshot.environments:
                                pending[self._descriptor_key(descriptor)] = descriptor
                        case "snapshot_complete":
                            if pending is None:
                                raise RuntimeError("unexpected Environment snapshot completion")
                            current, pending = pending, None
                            yield [current[key] for key in sorted(current)]
                        case "upsert":
                            if pending is not None:
                                raise RuntimeError("Environment update arrived before snapshot completion")
                            descriptor = frame.upsert
                            current[self._descriptor_key(descriptor)] = descriptor
                            yield [current[key] for key in sorted(current)]
                        case "removed":
                            if pending is not None:
                                raise RuntimeError("Environment removal arrived before snapshot completion")
                            target = frame.removed
                            key = (target.namespace, target.name)
                            previous = current.get(key)
                            if previous is not None and previous.target.uid == target.uid:
                                del current[key]
                                yield [current[key] for key in sorted(current)]
                        case _:
                            raise RuntimeError("EnvironmentService returned an empty watch frame")
            except grpc.aio.AioRpcError as error:
                if error.code() != grpc.StatusCode.UNAVAILABLE:
                    raise_status(error)
                reconnect = True
            finally:
                stream.cancel()
            if not reconnect:
                raise RuntimeError("EnvironmentService inventory stream ended")
            await asyncio.sleep(RECONNECT_PAUSE_SECONDS)

    @classmethod
    def _copy_target(cls, target: identity_pb2.EnvironmentTarget) -> identity_pb2.EnvironmentTarget:
        cls._require_target(target)
        copied = identity_pb2.EnvironmentTarget()
        copied.CopyFrom(target)
        return copied

    @staticmethod
    def _require_target(target: identity_pb2.EnvironmentTarget) -> None:
        if not target.namespace or not target.name or not target.uid:
            raise ValueError("complete EnvironmentService target handle is required")

    @classmethod
    def _descriptor_key(cls, descriptor: environment_service_pb2.EnvironmentDescriptor) -> tuple[str, str]:
        if not descriptor.HasField("target"):
            raise RuntimeError("EnvironmentService returned a descriptor without its target")
        cls._require_target(descriptor.target)
        return descriptor.target.namespace, descriptor.target.name
