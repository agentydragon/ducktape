"""Serve a scripted Runner directly to the Runner Gateway over mTLS."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import grpc
from cryptography import x509

from agentplane.proto import runner_state_pb2
from agentplane.runner import runner_service_pb2
from agentplane.runner.configuration import RunnerTransportTLS

# gazelle:include_dep @pypi//protobuf

# The Gateway authenticates this certificate identity before it sends Runner RPCs.
_RUNNER_GATEWAY_URI = "spiffe://agentplane/runner-gateway"
_WATCH_INTERVAL_S = 5


def _gateway_peer_uri(context: grpc.aio.ServicerContext) -> str | None:
    certificates = tuple(context.auth_context().get("x509_pem_cert", ()))
    if len(certificates) != 1:
        return None
    certificate = x509.load_pem_x509_certificate(certificates[0])
    try:
        names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return None
    uris = names.get_values_for_type(x509.UniformResourceIdentifier)
    return uris[0] if len(uris) == 1 else None


class _WatchStateInterceptor(grpc.aio.ServerInterceptor):
    def __init__(self, boot_id: str) -> None:
        self.boot_id = boot_id

    async def intercept_service(self, continuation, handler_call_details):
        if handler_call_details.method != "/agentplane.runner.Runner/WatchState":
            return await continuation(handler_call_details)

        async def watch_state(
            request: runner_service_pb2.WatchStateRequest, context: grpc.aio.ServicerContext
        ) -> AsyncIterator[runner_service_pb2.WatchStateResponse]:
            if _gateway_peer_uri(context) != _RUNNER_GATEWAY_URI:
                await context.abort(grpc.StatusCode.PERMISSION_DENIED, "peer is not the Runner Gateway")
            if not request.sandbox_uid or not request.incarnation_uid or request.connection_epoch == 0:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Sandbox, incarnation and epoch are required")
            response = runner_service_pb2.WatchStateResponse(
                sandbox_uid=request.sandbox_uid,
                incarnation_uid=request.incarnation_uid,
                runner_boot_id=self.boot_id,
                connection_epoch=request.connection_epoch,
                facts=runner_state_pb2.RunnerFacts(),
            )
            while True:
                yield response
                await asyncio.sleep(_WATCH_INTERVAL_S)

        return grpc.unary_stream_rpc_method_handler(
            watch_state,
            request_deserializer=runner_service_pb2.WatchStateRequest.FromString,
            response_serializer=runner_service_pb2.WatchStateResponse.SerializeToString,
        )


@asynccontextmanager
async def runner_server(
    listen_address: str, tls: RunnerTransportTLS, add_services: Callable[[grpc.aio.Server], None]
) -> AsyncIterator[grpc.aio.Server]:
    """Serve the scripted Runner RPCs on `listen_address` until the context exits.

    `tls` carries the Runner's server certificate and the CA used to authenticate the Gateway client.
    `add_services` registers the scripted Runner methods; WatchState is supplied by this fixture.
    """
    root_certificates, private_key, certificate_chain = await asyncio.gather(
        asyncio.to_thread(Path(tls.ca_file).read_bytes),
        asyncio.to_thread(Path(tls.key_file).read_bytes),
        asyncio.to_thread(Path(tls.cert_file).read_bytes),
    )
    credentials = grpc.ssl_server_credentials(
        [(private_key, certificate_chain)], root_certificates=root_certificates, require_client_auth=True
    )
    server = grpc.aio.server(interceptors=(_WatchStateInterceptor(str(uuid4())),))
    add_services(server)
    if server.add_secure_port(listen_address, credentials) == 0:
        raise OSError(f"could not bind scripted Runner to {listen_address}")
    await server.start()
    try:
        yield server
    finally:
        await server.stop(grace=0)
