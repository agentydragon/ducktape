"""Management gRPC service backed by independently owned providers."""

from __future__ import annotations

import asyncio
import logging
from typing import override

import grpc
from google.protobuf import empty_pb2

from platform_sessions.limits import MAX_SESSION_BYTES
from platform_sessions.management.v1 import management_pb2 as pb, management_pb2_grpc as rpc
from platform_sessions.provider.control_client import ProviderControlClient
from platform_sessions.provider.v1 import session_provider_pb2 as provider_pb

logger = logging.getLogger(__name__)


class SessionManagement(rpc.SessionManagementServicer):
    def __init__(self, clients: dict[str, ProviderControlClient]):
        if any(key != client.instance.instance_id for key, client in clients.items()):
            raise ValueError("Provider client identity differs from its configured instance")
        self.clients = clients

    async def _client(self, instance_id, context):
        client = self.clients.get(instance_id)
        if client is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "Unknown provider instance")
        return client

    @staticmethod
    def _log(client, operation, exc):
        logger.warning(
            "Provider control operation failed operation=%s instance_id=%s error_type=%s",
            operation,
            client.instance.instance_id,
            type(exc).__name__,
        )

    @override
    async def ListInstances(self, request: empty_pb2.Empty, context):
        async def instance(client):
            result = pb.Instance(ref=client.instance)
            try:
                response = await client.get_info()
                info = next(
                    item for item in response.provider.instances if item.instance_id == client.instance.instance_id
                )
                result.info.CopyFrom(info)
            except (grpc.aio.AioRpcError, OSError, ValueError) as exc:
                self._log(client, "GetInfo", exc)
            return result

        return pb.ListInstancesResponse(instances=await asyncio.gather(*(instance(c) for c in self.clients.values())))

    @override
    async def GetSession(self, request, context):
        client = await self._client(request.instance_id, context)
        try:
            return await client.get_session()
        except (grpc.aio.AioRpcError, OSError, ValueError) as exc:
            self._log(client, "GetSession", exc)
            return await context.abort(grpc.StatusCode.UNAVAILABLE, "Provider service unavailable")

    @override
    async def Probe(self, request, context):
        client = await self._client(request.instance_id, context)
        try:
            return await client.probe(source=provider_pb.PROBE_SOURCE_MANUAL)
        except (grpc.aio.AioRpcError, OSError, ValueError) as exc:
            self._log(client, "Probe", exc)
            return await context.abort(
                grpc.StatusCode.UNKNOWN, "Probe outcome unknown; inspect session state before retrying"
            )

    async def _candidate(self, request, context, *, verify):
        client = await self._client(request.instance_id, context)
        if (
            not request.candidate
            or len(request.candidate) > MAX_SESSION_BYTES
            or not request.media_type
            or not request.format_version
        ):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Invalid session candidate")
        try:
            operation = client.verify_candidate if verify else client.import_session
            return await operation(
                request.candidate, media_type=request.media_type, format_version=request.format_version
            )
        except grpc.aio.AioRpcError as exc:
            if verify and exc.code() == grpc.StatusCode.UNIMPLEMENTED:
                await context.abort(grpc.StatusCode.UNIMPLEMENTED, "Candidate verification unsupported")
            self._log(client, "VerifyCandidate" if verify else "ImportSession", exc)
        except (OSError, ValueError) as exc:
            self._log(client, "VerifyCandidate" if verify else "ImportSession", exc)
        return await context.abort(
            grpc.StatusCode.UNKNOWN, "Operation outcome unknown; inspect session state before retrying"
        )

    @override
    async def VerifyCandidate(self, request, context):
        return await self._candidate(request, context, verify=True)

    @override
    async def ImportSession(self, request, context):
        return await self._candidate(request, context, verify=False)
