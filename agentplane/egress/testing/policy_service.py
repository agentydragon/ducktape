"""A PolicyService that serves the Identity resources of an API server, for the proxy to read as its Identity source."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, override

import grpc
from google.protobuf.empty_pb2 import Empty
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.config import new_client_from_config
from kubernetes_asyncio.watch import Watch

from agentplane.crd_group import GROUP, VERSION
from agentplane.net.peertls.config import EGRESS_PROXY_URI, POLICY_CONTROLLER_URI, ClientTLS, PolicyServicePeer
from agentplane.policy.v1 import policy_service_pb2, policy_service_pb2_grpc
from agentplane.testing.tls import issue_workload_leaf, make_ca, write_ca


def _identity(resource: dict[str, Any]) -> policy_service_pb2.Identity:
    metadata = resource["metadata"]
    identity = policy_service_pb2.Identity(
        identity=policy_service_pb2.PolicyResourceIdentity(
            namespace=metadata["namespace"], name=metadata["name"], uid=metadata["uid"]
        ),
        generation=metadata["generation"],
    )
    if deleting := metadata.get("deletionTimestamp"):
        identity.deletion_timestamp.FromJsonString(deleting if isinstance(deleting, str) else deleting.isoformat())
    return identity


class _Identities(policy_service_pb2_grpc.PolicyServiceServicer):
    """WatchIdentities and GetIdentity over the Identity resources `kubeconfig` reaches in `namespace`.

    Read through its own API client, so a test that cuts `kubeconfig`'s API server cuts this view too.
    """

    def __init__(self, kubeconfig: Path, namespace: str, impersonate: str | None) -> None:
        self._kubeconfig = kubeconfig
        self._namespace = namespace
        self._impersonate = impersonate

    @asynccontextmanager
    async def _api(self) -> AsyncIterator[k8s_client.ApiClient]:
        # kubernetes_asyncio ignores a kubeconfig user's `as`, so impersonation is explicit.
        async with await new_client_from_config(config_file=str(self._kubeconfig)) as api:
            if self._impersonate is not None:
                api.set_default_header("Impersonate-User", self._impersonate)
            yield api

    @override
    async def WatchIdentities(
        self, request: policy_service_pb2.WatchIdentitiesRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[policy_service_pb2.WatchIdentitiesResponse]:
        async with self._api() as api:
            custom = k8s_client.CustomObjectsApi(api)
            listing = await custom.list_namespaced_custom_object(GROUP, VERSION, self._namespace, "identities")
            yield policy_service_pb2.WatchIdentitiesResponse(
                snapshot=policy_service_pb2.IdentityChunk(identities=[_identity(item) for item in listing["items"]])
            )
            yield policy_service_pb2.WatchIdentitiesResponse(snapshot_complete=Empty())
            async for event in Watch().stream(
                custom.list_namespaced_custom_object,
                GROUP,
                VERSION,
                self._namespace,
                "identities",
                resource_version=listing["metadata"]["resourceVersion"],
            ):
                resource = event["object"]
                if event["type"] == "DELETED":
                    yield policy_service_pb2.WatchIdentitiesResponse(removed=_identity(resource).identity)
                else:
                    yield policy_service_pb2.WatchIdentitiesResponse(upsert=_identity(resource))
        await context.abort(grpc.StatusCode.UNAVAILABLE, "the Identity watch ended")

    @override
    async def GetIdentity(
        self, request: policy_service_pb2.GetIdentityRequest, context: grpc.aio.ServicerContext
    ) -> policy_service_pb2.GetIdentityResponse:
        async with self._api() as api:
            try:
                resource = await k8s_client.CustomObjectsApi(api).get_namespaced_custom_object(
                    GROUP, VERSION, self._namespace, "identities", request.name
                )
            except ApiException as error:
                if error.status == 404:
                    await context.abort(grpc.StatusCode.NOT_FOUND, "no such Identity")
                raise
        return policy_service_pb2.GetIdentityResponse(identity=_identity(resource))


@dataclass(frozen=True)
class _Credentials:
    ca_file: Path
    ca_pem: bytes
    server_certificate_pem: bytes
    server_key_pem: bytes
    client_certificate: Path
    client_key: Path


def _issue_credentials(directory: Path) -> _Credentials:
    directory.mkdir()
    ca = make_ca("agentplane-egress-test-policy-service")
    ca_file, _ = write_ca(ca, directory, "policy-service-ca")
    server_directory = directory / "policy-service-server"
    server_directory.mkdir()
    server_certificate, server_key = issue_workload_leaf(ca, POLICY_CONTROLLER_URI, server_directory)
    client_directory = directory / "policy-service-client"
    client_directory.mkdir()
    client_certificate, client_key = issue_workload_leaf(ca, EGRESS_PROXY_URI, client_directory)
    return _Credentials(
        ca_file, ca.cert_pem, server_certificate.read_bytes(), server_key.read_bytes(), client_certificate, client_key
    )


@asynccontextmanager
async def policy_service(
    kubeconfig: Path, namespace: str, directory: Path, *, impersonate: str | None = None
) -> AsyncIterator[PolicyServicePeer]:
    """Serve `namespace`'s Identities over mutual TLS and yield the `policy_service` block that reaches it as the proxy.

    `directory` is created here and holds the service's CA and certificates. `kubeconfig` is read per call, so it
    may be written after the service starts. `impersonate` is the user it reads as, when `kubeconfig` acts as another.
    """
    credentials = await asyncio.to_thread(_issue_credentials, directory)
    server = grpc.aio.server()
    policy_service_pb2_grpc.add_PolicyServiceServicer_to_server(_Identities(kubeconfig, namespace, impersonate), server)
    port = server.add_secure_port(
        "127.0.0.1:0",
        grpc.ssl_server_credentials(
            [(credentials.server_key_pem, credentials.server_certificate_pem)],
            root_certificates=credentials.ca_pem,
            require_client_auth=True,
        ),
    )
    await server.start()
    try:
        yield PolicyServicePeer(
            target=f"127.0.0.1:{port}",
            tls=ClientTLS(
                ca_file=credentials.ca_file,
                server_uri=POLICY_CONTROLLER_URI,
                cert_file=credentials.client_certificate,
                key_file=credentials.client_key,
                client_uri=EGRESS_PROXY_URI,
            ),
        )
    finally:
        await server.stop(grace=0)
