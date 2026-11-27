"""PolicyService Identity view over a test API server's Identity objects, for the real Dispatcher binary."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast, override

import grpc
from google.protobuf.empty_pb2 import Empty
from google.protobuf.timestamp_pb2 import Timestamp
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.config import new_client_from_config

from agentplane.crd_group import GROUP, VERSION
from agentplane.net.peertls.config import POLICY_CONTROLLER_URI, SESSIONS_URI, ClientTLS, PolicyServicePeer
from agentplane.policy.v1 import policy_service_pb2, policy_service_pb2_grpc
from agentplane.testing.tls import CertificateAuthority, issue_workload_leaf

_POLL_INTERVAL_S = 0.1


def _identity(namespace: str, item: dict[str, Any]) -> policy_service_pb2.Identity:
    metadata = item["metadata"]
    identity = policy_service_pb2.Identity(
        identity=policy_service_pb2.PolicyResourceIdentity(
            namespace=namespace, name=metadata["name"], uid=metadata["uid"]
        )
    )
    if metadata.get("deletionTimestamp"):
        identity.deletion_timestamp.CopyFrom(Timestamp())
    return identity


class _IdentityService(policy_service_pb2_grpc.PolicyServiceServicer):
    """Serves the Identities the test creates in `namespace`; a watch reports each change within a poll."""

    def __init__(self, api: k8s_client.ApiClient, namespace: str) -> None:
        self._custom = cast(Any, k8s_client.CustomObjectsApi(api))
        self._namespace = namespace

    async def _list(self) -> dict[str, policy_service_pb2.Identity]:
        listing = await self._custom.list_namespaced_custom_object(GROUP, VERSION, self._namespace, "identities")
        return {item["metadata"]["name"]: _identity(self._namespace, item) for item in listing["items"]}

    @override
    async def WatchIdentities(
        self, request: policy_service_pb2.WatchIdentitiesRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[policy_service_pb2.WatchIdentitiesResponse]:
        known = await self._list()
        yield policy_service_pb2.WatchIdentitiesResponse(
            snapshot=policy_service_pb2.IdentityChunk(identities=known.values())
        )
        yield policy_service_pb2.WatchIdentitiesResponse(snapshot_complete=Empty())
        while True:
            await asyncio.sleep(_POLL_INTERVAL_S)
            current = await self._list()
            for name, identity in current.items():
                if known.get(name) != identity:
                    yield policy_service_pb2.WatchIdentitiesResponse(upsert=identity)
            for name, identity in known.items():
                if name not in current or current[name].identity.uid != identity.identity.uid:
                    yield policy_service_pb2.WatchIdentitiesResponse(removed=identity.identity)
            known = current

    @override
    async def GetIdentity(
        self, request: policy_service_pb2.GetIdentityRequest, context: grpc.aio.ServicerContext
    ) -> policy_service_pb2.GetIdentityResponse:
        try:
            item = await self._custom.get_namespaced_custom_object(
                GROUP, VERSION, self._namespace, "identities", request.name
            )
        except ApiException as error:
            if error.status == 404:
                await context.abort(grpc.StatusCode.NOT_FOUND, f"Identity {request.name!r} not found")
            raise
        return policy_service_pb2.GetIdentityResponse(identity=_identity(self._namespace, item))


@asynccontextmanager
async def serve_identities(
    kubeconfig: Path, namespace: str, *, ca: CertificateAuthority, ca_file: Path, directory: Path
) -> AsyncIterator[PolicyServicePeer]:
    """Serve `namespace`'s Identity objects as PolicyService; yields the Dispatcher's `policy_service` block."""
    server_directory = directory / "policy-service-server"
    server_directory.mkdir()
    client_directory = directory / "policy-service-client"
    client_directory.mkdir()
    server_cert, server_key = issue_workload_leaf(ca, POLICY_CONTROLLER_URI, server_directory)
    client_cert, client_key = issue_workload_leaf(ca, SESSIONS_URI, client_directory)
    async with await new_client_from_config(config_file=str(kubeconfig)) as api:
        server = grpc.aio.server()
        policy_service_pb2_grpc.add_PolicyServiceServicer_to_server(_IdentityService(api, namespace), server)
        port = server.add_secure_port(
            "127.0.0.1:0",
            grpc.ssl_server_credentials(
                [(server_key.read_bytes(), server_cert.read_bytes())],
                root_certificates=ca.cert_pem,
                require_client_auth=True,
            ),
        )
        await server.start()
        try:
            yield PolicyServicePeer(
                target=f"127.0.0.1:{port}",
                tls=ClientTLS(
                    ca_file=ca_file,
                    server_uri=POLICY_CONTROLLER_URI,
                    cert_file=client_cert,
                    key_file=client_key,
                    client_uri=SESSIONS_URI,
                ),
            )
        finally:
            await server.stop(grace=0)
