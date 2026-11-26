"""A PolicyService serving one namespace's Identities from a test API server, for the Go History Service.

History resolves Identities through a WatchIdentities-fed cache with read-through `GetIdentity`. This fake sends an
empty, complete snapshot and no changes, so every lookup reads the API server through `GetIdentity`.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import override

import grpc
from google.protobuf import empty_pb2, timestamp_pb2
from kubernetes_asyncio import client as k8s_client

from agentplane.net.peertls.config import POLICY_CONTROLLER_URI
from agentplane.policy.v1 import policy_service_pb2, policy_service_pb2_grpc
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer
from agentplane.testing.tls import CertificateAuthority, issue_workload_leaf


class _IdentitiesFromApiServer(policy_service_pb2_grpc.PolicyServiceServicer):
    def __init__(self, api: KubernetesApiServer, namespace: str) -> None:
        self._custom = k8s_client.CustomObjectsApi(api.api)
        self._namespace = namespace

    @override
    async def WatchIdentities(
        self, request: policy_service_pb2.WatchIdentitiesRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[policy_service_pb2.WatchIdentitiesResponse]:
        yield policy_service_pb2.WatchIdentitiesResponse(snapshot_complete=empty_pb2.Empty())
        await asyncio.Event().wait()

    @override
    async def GetIdentity(
        self, request: policy_service_pb2.GetIdentityRequest, context: grpc.aio.ServicerContext
    ) -> policy_service_pb2.GetIdentityResponse:
        try:
            identity = await self._custom.get_namespaced_custom_object(
                "agentplane.allegedly.works", "v1alpha1", self._namespace, "identities", request.name
            )
        except k8s_client.ApiException as error:
            if error.status == 404:
                await context.abort(grpc.StatusCode.NOT_FOUND, "Identity not found")
            raise
        metadata = identity["metadata"]
        message = policy_service_pb2.Identity(
            identity=policy_service_pb2.PolicyResourceIdentity(
                namespace=self._namespace, name=metadata["name"], uid=metadata["uid"]
            )
        )
        if "deletionTimestamp" in metadata:
            deletion = timestamp_pb2.Timestamp()
            deletion.FromJsonString(metadata["deletionTimestamp"])
            message.deletion_timestamp.CopyFrom(deletion)
        return policy_service_pb2.GetIdentityResponse(identity=message)


@asynccontextmanager
async def fake_policy_service(
    api: KubernetesApiServer, *, namespace: str, ca: CertificateAuthority, directory: Path
) -> AsyncIterator[str]:
    """The `host:port` of a mutual-TLS PolicyService, authenticating callers against `ca`, for the context's life."""
    await asyncio.to_thread(directory.mkdir)
    cert_file, key_file = issue_workload_leaf(ca, POLICY_CONTROLLER_URI, directory, dns_name="localhost")
    server = grpc.aio.server()
    policy_service_pb2_grpc.add_PolicyServiceServicer_to_server(_IdentitiesFromApiServer(api, namespace), server)
    port = server.add_secure_port(
        "localhost:0",
        grpc.ssl_server_credentials(
            [(key_file.read_bytes(), cert_file.read_bytes())], root_certificates=ca.cert_pem, require_client_auth=True
        ),
    )
    await server.start()
    try:
        yield f"localhost:{port}"
    finally:
        await server.stop(grace=None)
