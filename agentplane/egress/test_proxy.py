"""Black-box tests of the built Go `egress-proxy` process.

A real client goes through the binary to a scripted upstream. The proxy intercepts with a throwaway
CA the client trusts, verifies the upstream's certificate against a second throwaway CA, takes its
identity and policy from a real envtest API server, and appends decisions to PostgreSQL. Every
assertion is on what the client and the upstream actually saw.
"""

from __future__ import annotations

import asyncio
import datetime
import ssl
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, field
from ipaddress import ip_network
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import UUID

import aiohttp
import grpc
import pytest
import pytest_bazel
import yaml
from aiohttp import web
from cryptography import x509
from kubernetes_asyncio import client as k8s_client
from more_itertools import one
from sqlalchemy import create_engine, text
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane.crd_group import GROUP, VERSION
from agentplane.egress import egress_rules_pb2, egress_rules_pb2_grpc
from agentplane.egress.platform_routes import (
    PLATFORM_ROUTES_LISTEN_ADDRESS,
    PlatformRoute,
    PlatformRoutes,
    TLSConnectRouteTransport,
)
from agentplane.egress.resources import CREDENTIALS_PLURAL, TargetMethod, placeholder_of
from agentplane.egress.settings import Settings
from agentplane.egress.sidecar_config import Proxy, SpiffeProxyAuth, TLSProxyTransport
from agentplane.egress.testing.go_replica import GoReplica, go_replica
from agentplane.egress.testing.sidecar_process import SidecarProcess
from agentplane.identity.auth import listener
from agentplane.policy_sets import POLICY_BINDINGS_PLURAL, POLICY_SETS_PLURAL
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.kubernetes import CustomObjectsClient
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer, kubernetes_apiserver
from agentplane.testing.tls import (
    CertificateAuthority,
    client_tls_context,
    issue_leaf,
    issue_workload_leaf,
    make_ca,
    server_tls_context,
    workload_uri,
    write_ca,
)
from util.net import Listener

# Three namespaces, all different, so that a proxy reading one from the wrong one fails.
NAMESPACE = "agentplane-egress-test"
SANDBOX_NAMESPACE = "agentplane-egress-test-sandboxes"
CREDENTIALS_NAMESPACE = "agentplane-egress-test-credentials"
UPSTREAM_HOST = "localhost"
SANDBOX_A = "sb-a"
SANDBOX_B = "sb-b"
RUNNER_CREDENTIAL_ID = "a" * 32
WORKLOAD_CREDENTIALS_PLURAL = "workloadcredentials"
ROTATED_RUNNER_CREDENTIAL_ID = "b" * 32
BINDING_B = f"{SANDBOX_B}-private-policy"
EGRESS_PROXY_URI = "spiffe://agentplane/egress-proxy"
# Workload Identities have distinct names from the Pod ServiceAccounts they run beside.
SUBJECT_A = f"{SANDBOX_A}-identity"
SUBJECT_B = f"{SANDBOX_B}-identity"


@dataclass
class RecordingUpstream:
    """HTTP and HTTPS listeners that record every request's method, path, and headers."""

    slow_started: asyncio.Event = field(default_factory=asyncio.Event)
    slow_release: asyncio.Event = field(default_factory=asyncio.Event)
    port: int = 0
    http_port: int = 0
    requests: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
    bodies: list[bytes] = field(default_factory=list, repr=False)

    async def handle(self, request: web.Request) -> web.Response:
        if request.path == "/public/slow":
            self.slow_started.set()
            await self.slow_release.wait()
        headers = {k.lower(): v for k, v in request.headers.items()}
        self.requests.append((request.method, request.path_qs, headers))
        self.bodies.append(await request.read())
        return web.Response(text="upstream ok")


@asynccontextmanager
async def recording_upstream(cert_path: Path, key_path: Path) -> AsyncIterator[RecordingUpstream]:
    upstream = RecordingUpstream()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", upstream.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    https_site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_tls_context(cert_path, key_path))
    await https_site.start()
    upstream.port = one(runner.addresses)[1]
    http_site = web.TCPSite(runner, "127.0.0.1", 0)
    await http_site.start()
    upstream.http_port = one(address[1] for address in runner.addresses if address[1] != upstream.port)
    try:
        yield upstream
    finally:
        await runner.cleanup()


@dataclass(frozen=True)
class SeededSandbox:
    sandbox_uid: str
    pod_uid: str
    identity_uid: str


def replica_settings(
    *,
    api_config: Path,
    ca: tuple[Path, Path],
    database_url: str,
    svid_cert: tuple[Path, Path],
    svid_ca_file: Path,
    upstream_ca_file: Path | None = None,
) -> Settings:
    cert, key = ca
    return Settings(
        rules_namespace=NAMESPACE,
        allowed_service_account_namespaces=frozenset({SANDBOX_NAMESPACE}),
        credentials_namespace=CREDENTIALS_NAMESPACE,
        kubeconfig=api_config,
        log_levels={},
        ca_cert=cert,
        ca_key=key,
        upstream_ca_file=upstream_ca_file,
        forward=listener.Listener(
            listen_address="0.0.0.0:8888",
            tls=listener.TLS(cert_file=svid_cert[0], key_file=svid_cert[1]),
            auth=[listener.SPIFFEAuth(ca_file=svid_ca_file)],
        ),
        database_url=database_url,
        exempt_networks=[ip_network("127.0.0.0/8"), ip_network("::1/128")],
    )


_CONTRACT_CREDENTIAL = "transport-contract"
_CONTRACT_PATH = "/google.devtools.build.v1.PublishBuildEvent/PublishBuildToolEventStream"
BINDING = f"{SANDBOX_A}-{_CONTRACT_CREDENTIAL}"
_API_VERSION = f"{GROUP}/{VERSION}"


async def seed(server: KubernetesApiServer) -> SeededSandbox:
    """Workload A's bound Sandbox and destination tokens. A is bound to a policy that
    substitutes the contract credential on the contract paths and admits `/public/**` as is."""
    core = k8s_client.CoreV1Api(server.api)
    for namespace in (NAMESPACE, SANDBOX_NAMESPACE, CREDENTIALS_NAMESPACE):
        await core.create_namespace(k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name=namespace)))
    await core.create_namespaced_service_account(
        SANDBOX_NAMESPACE, k8s_client.V1ServiceAccount(metadata=k8s_client.V1ObjectMeta(name=SANDBOX_A))
    )
    await core.create_namespaced_service_account(
        SANDBOX_NAMESPACE, k8s_client.V1ServiceAccount(metadata=k8s_client.V1ObjectMeta(name=SANDBOX_B))
    )
    pod = await core.create_namespaced_pod(
        SANDBOX_NAMESPACE,
        k8s_client.V1Pod(
            metadata=k8s_client.V1ObjectMeta(name=SANDBOX_A),
            spec=k8s_client.V1PodSpec(
                service_account_name=SANDBOX_A,
                containers=[k8s_client.V1Container(name="workload", image="registry.test/unused")],
            ),
        ),
    )
    await core.create_namespaced_secret(
        CREDENTIALS_NAMESPACE,
        k8s_client.V1Secret(
            metadata=k8s_client.V1ObjectMeta(name=_CONTRACT_CREDENTIAL), string_data={"key": "contract-secret"}
        ),
    )
    custom = cast(CustomObjectsClient, k8s_client.CustomObjectsApi(server.api))
    identities = {}
    for name in (SUBJECT_A, SUBJECT_B):
        identities[name] = await custom.create_namespaced_custom_object(
            GROUP,
            VERSION,
            NAMESPACE,
            "identities",
            {
                "apiVersion": _API_VERSION,
                "kind": "Identity",
                "metadata": {"name": name},
                "spec": {"description": f"Sandbox {name} test Identity"},
            },
        )
    bodies: dict[str, dict[str, Any]] = {
        CREDENTIALS_PLURAL: {
            "apiVersion": _API_VERSION,
            "kind": "EgressCredential",
            "metadata": {"name": _CONTRACT_CREDENTIAL},
            "spec": {
                "source": {"secretRef": {"name": _CONTRACT_CREDENTIAL, "key": "key"}},
                "description": "a test credential",
                "targets": [{"header": "x-buildbuddy-api-key", "method": TargetMethod.WHOLE_VALUE}],
            },
        },
        POLICY_SETS_PLURAL: {
            "apiVersion": _API_VERSION,
            "kind": "PolicySet",
            "metadata": {"name": _CONTRACT_CREDENTIAL},
            "spec": {
                "rules": [
                    {
                        "type": "egress",
                        "hosts": [UPSTREAM_HOST],
                        "paths": ["/contract", "/contract-sse", "/contract-ws", _CONTRACT_PATH],
                        "credentialRef": {"name": _CONTRACT_CREDENTIAL},
                    },
                    {"type": "egress", "hosts": [UPSTREAM_HOST], "paths": ["/public/**"]},
                ]
            },
        },
        POLICY_BINDINGS_PLURAL: {
            "apiVersion": _API_VERSION,
            "kind": "PolicyBinding",
            "metadata": {"name": BINDING},
            "spec": {"subject": {"identity": {"name": SUBJECT_A}}, "policySets": [_CONTRACT_CREDENTIAL]},
        },
    }
    for plural, body in bodies.items():
        await custom.create_namespaced_custom_object(GROUP, VERSION, NAMESPACE, plural, body)
    for plural, body in (
        (
            POLICY_SETS_PLURAL,
            {
                "apiVersion": _API_VERSION,
                "kind": "PolicySet",
                "metadata": {"name": "sandbox-b-private-policy"},
                "spec": {"rules": [{"type": "egress", "hosts": [UPSTREAM_HOST], "paths": ["/private-b-only"]}]},
            },
        ),
        (
            POLICY_BINDINGS_PLURAL,
            {
                "apiVersion": _API_VERSION,
                "kind": "PolicyBinding",
                "metadata": {"name": BINDING_B},
                "spec": {"subject": {"identity": {"name": SUBJECT_B}}, "policySets": ["sandbox-b-private-policy"]},
            },
        ),
    ):
        await custom.create_namespaced_custom_object(GROUP, VERSION, NAMESPACE, plural, body)
    await custom.create_namespaced_custom_object(
        GROUP,
        VERSION,
        SANDBOX_NAMESPACE,
        "sandboxes",
        {
            "apiVersion": _API_VERSION,
            "kind": "Sandbox",
            "metadata": {"name": SANDBOX_A, "finalizers": ["agentplane-egress-test/hold"]},
            "spec": {"template": "test", "running": True},
        },
    )
    sandbox = await custom.get_namespaced_custom_object(GROUP, VERSION, SANDBOX_NAMESPACE, "sandboxes", SANDBOX_A)
    await core.patch_namespaced_service_account(
        SANDBOX_A,
        SANDBOX_NAMESPACE,
        k8s_client.V1ServiceAccount(
            metadata=k8s_client.V1ObjectMeta(
                owner_references=[
                    k8s_client.V1OwnerReference(
                        api_version=_API_VERSION,
                        kind="Sandbox",
                        name=SANDBOX_A,
                        uid=sandbox["metadata"]["uid"],
                        controller=True,
                    )
                ]
            )
        ),
    )
    await custom.patch_namespaced_custom_object_status(
        GROUP,
        VERSION,
        SANDBOX_NAMESPACE,
        "sandboxes",
        SANDBOX_A,
        {"status": {"incarnation": {"name": SANDBOX_A, "uid": pod.metadata.uid}}},
        _content_type="application/merge-patch+json",
    )
    identity_uid = identities[SUBJECT_A]["metadata"]["uid"]
    await issue_record(
        custom, sandbox["metadata"]["uid"], pod.metadata.name, pod.metadata.uid, identity_uid, RUNNER_CREDENTIAL_ID
    )
    return SeededSandbox(sandbox_uid=sandbox["metadata"]["uid"], pod_uid=pod.metadata.uid, identity_uid=identity_uid)


def _record_status(credential_id: str, pod_uid: str) -> dict[str, Any]:
    return {
        "observedGeneration": 1,
        "credentialID": credential_id,
        "uri": workload_uri(SANDBOX_NAMESPACE, SANDBOX_A, credential_id),
        "secretName": f"svid-{SANDBOX_A}-{credential_id}",
        "acceptedBinding": {"podUID": pod_uid},
    }


async def issue_record(
    custom: CustomObjectsClient, sandbox_uid: str, pod_name: str, pod_uid: str, identity_uid: str, credential_id: str
) -> None:
    """Write sandbox A's WorkloadCredential as the Sandbox Controller requests it and the issuer accepts it."""
    await custom.create_namespaced_custom_object(
        GROUP,
        VERSION,
        SANDBOX_NAMESPACE,
        WORKLOAD_CREDENTIALS_PLURAL,
        {
            "apiVersion": _API_VERSION,
            "kind": "WorkloadCredential",
            "metadata": {
                "name": SANDBOX_A,
                "ownerReferences": [
                    {
                        "apiVersion": _API_VERSION,
                        "kind": "Sandbox",
                        "name": SANDBOX_A,
                        "uid": sandbox_uid,
                        "controller": True,
                    }
                ],
            },
            "spec": {
                "subject": {"identity": {"name": SUBJECT_A, "uid": identity_uid}},
                "binding": {"podUID": pod_uid},
                "pod": {"name": pod_name, "uid": pod_uid},
            },
        },
    )
    await custom.patch_namespaced_custom_object_status(
        GROUP,
        VERSION,
        SANDBOX_NAMESPACE,
        WORKLOAD_CREDENTIALS_PLURAL,
        SANDBOX_A,
        {"status": _record_status(credential_id, pod_uid)},
        _content_type="application/merge-patch+json",
    )


async def rotate_record(custom: CustomObjectsClient, pod_uid: str) -> None:
    """Issue sandbox A's record a fresh credential ID, as the issuer does when the binding moves."""
    await custom.patch_namespaced_custom_object_status(
        GROUP,
        VERSION,
        SANDBOX_NAMESPACE,
        WORKLOAD_CREDENTIALS_PLURAL,
        SANDBOX_A,
        {"status": _record_status(ROTATED_RUNNER_CREDENTIAL_ID, pod_uid)},
        _content_type="application/merge-patch+json",
    )


async def revoke_binding(server: KubernetesApiServer) -> None:
    await cast(CustomObjectsClient, k8s_client.CustomObjectsApi(server.api)).delete_namespaced_custom_object(
        GROUP, VERSION, NAMESPACE, POLICY_BINDINGS_PLURAL, BINDING, body=k8s_client.V1DeleteOptions()
    )


@dataclass(frozen=True)
class Egress:
    """The running proxy and the scripted world around it."""

    replica: GoReplica
    client_tls: ssl.SSLContext
    client_certificate: tuple[Path, Path]
    api: KubernetesApiServer
    interception_ca: CertificateAuthority
    upstream_ca: CertificateAuthority
    runtime_ca: CertificateAuthority
    upstream: RecordingUpstream
    sandbox_uid: str
    pod_uid: str
    identity_uid: str
    tmp_path: Path
    database_url: str

    @property
    def proxy_listener(self) -> Listener:
        return self.replica.proxy_listener

    @property
    def svid_url(self) -> str:
        return f"https://localhost:{self.proxy_listener.port}"

    def url(self, path: str) -> str:
        return f"https://{UPSTREAM_HOST}:{self.upstream.port}{path}"


@pytest.fixture
async def egress(tmp_path: Path, history_db_url: str) -> AsyncIterator[Egress]:
    interception_ca = make_ca("agentplane-egress-test-interception")
    upstream_ca = make_ca("agentplane-egress-test-upstream")
    runtime_ca = make_ca("agentplane-egress-test-runtime")
    async with (
        kubernetes_apiserver() as api,
        recording_upstream(*issue_leaf(upstream_ca, UPSTREAM_HOST, tmp_path)) as upstream,
    ):
        seeded = await seed(api)
        svid_server_directory = tmp_path / "svid-server"
        svid_server_directory.mkdir()
        svid_server = issue_workload_leaf(runtime_ca, EGRESS_PROXY_URI, svid_server_directory, dns_name="localhost")
        client_directory = tmp_path / "runner-client"
        client_directory.mkdir()
        client_certificate = issue_workload_leaf(
            runtime_ca, workload_uri(SANDBOX_NAMESPACE, SANDBOX_A, RUNNER_CREDENTIAL_ID), client_directory
        )
        client_tls = ssl.create_default_context(cadata=(runtime_ca.cert_pem + interception_ca.cert_pem).decode())
        client_tls.load_cert_chain(*(str(path) for path in client_certificate))
        settings = replica_settings(
            api_config=api.kubeconfig,
            ca=write_ca(interception_ca, tmp_path, "interception"),
            database_url=history_db_url,
            upstream_ca_file=write_ca(upstream_ca, tmp_path, "upstream")[0],
            svid_cert=svid_server,
            svid_ca_file=write_ca(runtime_ca, tmp_path, "runtime")[0],
        )
        async with go_replica(settings, directory=tmp_path, tunnel_rules=True) as running:
            yield Egress(
                replica=running,
                client_tls=client_tls,
                client_certificate=client_certificate,
                api=api,
                interception_ca=interception_ca,
                upstream_ca=upstream_ca,
                runtime_ca=runtime_ca,
                upstream=upstream,
                sandbox_uid=seeded.sandbox_uid,
                pod_uid=seeded.pod_uid,
                identity_uid=seeded.identity_uid,
                tmp_path=tmp_path,
                database_url=history_db_url,
            )


def issue_runner_svid(
    egress: Egress,
    directory: Path,
    *,
    credential_id: str = RUNNER_CREDENTIAL_ID,
    valid_for: datetime.timedelta | None = None,
) -> tuple[Path, Path]:
    directory.mkdir()
    return issue_workload_leaf(
        egress.runtime_ca,
        workload_uri(SANDBOX_NAMESPACE, SANDBOX_A, credential_id),
        directory,
        dns_name="localhost",
        valid_for=valid_for,
    )


def proxy_svid_client_context(egress: Egress, certificate: tuple[Path, Path]) -> ssl.SSLContext:
    context = ssl.create_default_context(cadata=(egress.runtime_ca.cert_pem + egress.interception_ca.cert_pem).decode())
    context.load_cert_chain(*(str(path) for path in certificate))
    return context


async def svid_request(
    client: aiohttp.ClientSession,
    egress: Egress,
    proxy_tls: ssl.SSLContext,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    target_url: str | None = None,
) -> tuple[int, dict[str, str]]:
    request_options: dict[str, Any] = {"proxy": egress.svid_url, "ssl": proxy_tls, "headers": headers or {}}
    url = target_url or f"http://{UPSTREAM_HOST}:{egress.upstream.http_port}{path}"
    async with client.get(url, **request_options) as response:
        await response.read()
        return response.status, {name.lower(): value for name, value in response.headers.items()}


def rules_credentials(egress: Egress, certificate: tuple[Path, Path]) -> grpc.ChannelCredentials:
    return grpc.ssl_channel_credentials(
        root_certificates=egress.runtime_ca.cert_pem,
        private_key=certificate[1].read_bytes(),
        certificate_chain=certificate[0].read_bytes(),
    )


async def test_rules_grpc_resolves_its_own_svid_and_returns_the_credential_description(egress: Egress) -> None:
    async with grpc.aio.secure_channel(
        f"localhost:{egress.replica.admin_listener.port}", rules_credentials(egress, egress.client_certificate)
    ) as channel:
        client = egress_rules_pb2_grpc.EgressRulesStub(channel)
        request = egress_rules_pb2.GetRulesRequest()
        response = await client.GetRules(
            request, metadata=(("x-agentplane-subject", f"{SANDBOX_NAMESPACE}/{SANDBOX_B}"),), timeout=5
        )
        assert response.identity.name == SUBJECT_A
        assert not response.HasField("subject")
        credentials = [
            rule.credential for policy in response.policies for rule in policy.rules if rule.HasField("credential")
        ]
        contract = one(credential for credential in credentials if credential.name == _CONTRACT_CREDENTIAL)
        assert contract.description == "a test credential"
        assert contract.placeholder == placeholder_of(_CONTRACT_CREDENTIAL)
        with pytest.raises(grpc.aio.AioRpcError) as failure:
            await client.GetRules(request, metadata=(("authorization", "Bearer forged"),), timeout=5)
        assert failure.value.code() == grpc.StatusCode.UNAUTHENTICATED
        await rotate_record(cast(CustomObjectsClient, k8s_client.CustomObjectsApi(egress.api.api)), egress.pod_uid)
        with pytest.raises(grpc.aio.AioRpcError) as failure:
            await client.GetRules(request, timeout=5)
        assert failure.value.code() == grpc.StatusCode.UNAUTHENTICATED
    assert not egress.upstream.requests


async def test_rules_grpc_sidecar_presents_its_svid_and_discards_caller_credentials(egress: Egress) -> None:
    certificate, key = issue_runner_svid(egress, egress.tmp_path / "rules-sidecar-client")
    root = write_ca(egress.runtime_ca, egress.tmp_path, "rules-sidecar-root")[0]
    caller_token = await egress.api.workload_token(
        ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name=SANDBOX_B), ["ignored-caller-token"]
    )
    async with SidecarProcess(
        proxy=Proxy(
            address=f"{egress.proxy_listener.host}:{egress.proxy_listener.port}",
            transport=TLSProxyTransport(ca_file=root, auth=SpiffeProxyAuth(cert_file=certificate, key_file=key)),
        ),
        platform_routes=PlatformRoutes(
            listen_address=PLATFORM_ROUTES_LISTEN_ADDRESS,
            routes={
                "rules": PlatformRoute(
                    service_prefixes=("/agentplane.egress.EgressRules/",),
                    upstream=f"localhost:{egress.replica.admin_listener.port}",
                    transport=TLSConnectRouteTransport(server_uri=EGRESS_PROXY_URI),
                )
            },
        ),
    ) as sidecar:
        listener = sidecar.platform_route_listener
        async with grpc.aio.insecure_channel(f"{listener.host}:{listener.port}") as channel:
            response = await egress_rules_pb2_grpc.EgressRulesStub(channel).GetRules(
                egress_rules_pb2.GetRulesRequest(),
                metadata=(
                    ("authorization", f"Bearer {caller_token}"),
                    ("proxy-authorization", f"Bearer {caller_token}"),
                    ("x-agentplane-subject", f"{SANDBOX_NAMESPACE}/{SANDBOX_B}"),
                ),
                timeout=5,
            )
            assert response.identity.name == SUBJECT_A
            assert not response.HasField("subject")
    assert not egress.upstream.requests


async def test_rules_grpc_refuses_forged_and_expired_clients(egress: Egress) -> None:
    uri = workload_uri(SANDBOX_NAMESPACE, SANDBOX_A, RUNNER_CREDENTIAL_ID)
    forged_directory = egress.tmp_path / "forged-rules-client"
    forged_directory.mkdir()
    forged = issue_workload_leaf(make_ca("untrusted-runner-ca"), uri, forged_directory)
    expired = issue_runner_svid(
        egress, egress.tmp_path / "expired-rules-client", valid_for=datetime.timedelta(milliseconds=100)
    )
    expires = x509.load_pem_x509_certificate(expired[0].read_bytes()).not_valid_after_utc
    remaining = expires - datetime.datetime.now(datetime.UTC)
    if remaining >= datetime.timedelta(0):
        await asyncio.sleep(remaining.total_seconds() + 0.01)
    for certificate in (forged, expired):
        async with grpc.aio.secure_channel(
            f"localhost:{egress.replica.admin_listener.port}", rules_credentials(egress, certificate)
        ) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as failure:
                await egress_rules_pb2_grpc.EgressRulesStub(channel).GetRules(
                    egress_rules_pb2.GetRulesRequest(), timeout=5
                )
            assert failure.value.code() == grpc.StatusCode.UNAVAILABLE
    assert not egress.upstream.requests


async def test_svid_listener_maps_live_sandbox_and_ignores_forged_subject_header(egress: Egress) -> None:
    certificate = issue_runner_svid(egress, egress.tmp_path / "svid-client")
    proxy_tls = proxy_svid_client_context(egress, certificate)
    async with aiohttp.ClientSession() as client:
        status, _ = await svid_request(client, egress, proxy_tls, "/public/svid")
        assert status == 200
        status, headers = await svid_request(
            client,
            egress,
            proxy_tls,
            "/private-b-only",
            headers={"X-Agentplane-Subject": f"{SANDBOX_NAMESPACE}/{SANDBOX_B}"},
        )
    assert status == 403, headers
    assert headers["x-agentplane-egress"] == "denied; reason=no-rule"
    assert [path for _, path, _ in egress.upstream.requests] == ["/public/svid"]


@pytest.mark.parametrize("revocation", ["cleared", "rotated"])
async def test_svid_listener_rejects_a_revoked_runner_credential(egress: Egress, revocation: str) -> None:
    custom = cast(CustomObjectsClient, k8s_client.CustomObjectsApi(egress.api.api))
    if revocation == "rotated":
        await rotate_record(custom, egress.pod_uid)
    else:
        # The owner clearing its binding revokes before the issuer rotates the ID.
        await custom.patch_namespaced_custom_object(
            GROUP,
            VERSION,
            SANDBOX_NAMESPACE,
            WORKLOAD_CREDENTIALS_PLURAL,
            SANDBOX_A,
            {"spec": {"binding": None, "pod": None}},
            _content_type="application/merge-patch+json",
        )
    proxy_tls = proxy_svid_client_context(egress, issue_runner_svid(egress, egress.tmp_path / "svid-client"))
    async with aiohttp.ClientSession() as client:
        status, headers = await svid_request(client, egress, proxy_tls, "/public/revoked")
    assert status == 403, headers
    assert headers["x-agentplane-egress"] == "denied; reason=token-rejected"
    assert not egress.upstream.requests


async def test_svid_listener_keeps_a_deleting_sandboxs_credential_until_its_record_goes(egress: Egress) -> None:
    """Runtime seals history while its Sandbox finalizes: the record outlives the Sandbox's deletion,
    and only its removal, by the garbage collector here played by the test, revokes. A Sandbox that
    reuses the name gets a new record and a fresh credential ID, which the old certificate does not carry."""
    custom = cast(CustomObjectsClient, k8s_client.CustomObjectsApi(egress.api.api))
    certificate = issue_runner_svid(egress, egress.tmp_path / "svid-client")
    proxy_tls = proxy_svid_client_context(egress, certificate)
    await custom.delete_namespaced_custom_object(
        GROUP, VERSION, SANDBOX_NAMESPACE, "sandboxes", SANDBOX_A, body=k8s_client.V1DeleteOptions()
    )
    deleting = await custom.get_namespaced_custom_object(GROUP, VERSION, SANDBOX_NAMESPACE, "sandboxes", SANDBOX_A)
    assert deleting["metadata"].get("deletionTimestamp")
    async with aiohttp.ClientSession() as client:
        status, headers = await svid_request(client, egress, proxy_tls, "/public/deleting")
    assert status == 200, headers

    await custom.patch_namespaced_custom_object(
        GROUP,
        VERSION,
        SANDBOX_NAMESPACE,
        "sandboxes",
        SANDBOX_A,
        {"metadata": {"finalizers": []}},
        _content_type="application/merge-patch+json",
    )
    await custom.delete_namespaced_custom_object(
        GROUP, VERSION, SANDBOX_NAMESPACE, WORKLOAD_CREDENTIALS_PLURAL, SANDBOX_A, body=k8s_client.V1DeleteOptions()
    )
    async with aiohttp.ClientSession() as client:
        status, headers = await svid_request(client, egress, proxy_tls, "/public/removed")
    assert status == 403, headers
    assert headers["x-agentplane-egress"] == "denied; reason=token-rejected"

    replacement = await custom.create_namespaced_custom_object(
        GROUP,
        VERSION,
        SANDBOX_NAMESPACE,
        "sandboxes",
        {
            "apiVersion": _API_VERSION,
            "kind": "Sandbox",
            "metadata": {"name": SANDBOX_A},
            "spec": {"template": "test", "running": True},
        },
    )
    assert replacement["metadata"]["uid"] != egress.sandbox_uid
    await issue_record(
        custom,
        replacement["metadata"]["uid"],
        SANDBOX_A,
        egress.pod_uid,
        egress.identity_uid,
        ROTATED_RUNNER_CREDENTIAL_ID,
    )
    async with aiohttp.ClientSession() as client:
        status, headers = await svid_request(client, egress, proxy_tls, "/public/replaced")
    assert status == 403, headers
    assert headers["x-agentplane-egress"] == "denied; reason=token-rejected"
    assert [path for _, path, _ in egress.upstream.requests] == ["/public/deleting"]


async def test_untrusted_workload_token_header_is_not_forwarded(egress: Egress) -> None:
    proxy_tls = proxy_svid_client_context(egress, issue_runner_svid(egress, egress.tmp_path / "header-client"))
    async with aiohttp.ClientSession() as client:
        status, headers = await svid_request(
            client,
            egress,
            proxy_tls,
            "/public/untrusted-header",
            headers={"X-Agentplane-Workload-Token": "attacker-controlled token"},
        )
    assert status == 200, headers
    assert len(egress.upstream.requests) == 1
    _, path, forwarded_headers = egress.upstream.requests[0]
    assert path == "/public/untrusted-header"
    assert "x-agentplane-workload-token" not in forwarded_headers


STANDING_RECORD = "standing-a"
STANDING_CREDENTIAL_ID = "c" * 32


async def _write_record(
    custom: CustomObjectsClient,
    name: str,
    spec: dict[str, Any],
    *,
    owner_uid: str | None = None,
    accepted_binding: dict[str, str] | None = None,
) -> tuple[str, str]:
    """A record as its requester writes it and the issuer accepts it; returns the URI and credential ID it issues."""
    metadata: dict[str, Any] = {"name": name}
    if owner_uid is not None:
        metadata["ownerReferences"] = [
            {"apiVersion": _API_VERSION, "kind": "Sandbox", "name": SANDBOX_A, "uid": owner_uid, "controller": True}
        ]
    body = {"apiVersion": _API_VERSION, "kind": "WorkloadCredential", "metadata": metadata, "spec": spec}
    await custom.create_namespaced_custom_object(GROUP, VERSION, SANDBOX_NAMESPACE, WORKLOAD_CREDENTIALS_PLURAL, body)
    uri = workload_uri(SANDBOX_NAMESPACE, name, STANDING_CREDENTIAL_ID)
    status: dict[str, Any] = {
        "observedGeneration": 1,
        "credentialID": STANDING_CREDENTIAL_ID,
        "uri": uri,
        "secretName": f"svid-{name}",
    }
    if accepted_binding is not None:
        status["acceptedBinding"] = accepted_binding
    await custom.patch_namespaced_custom_object_status(
        GROUP,
        VERSION,
        SANDBOX_NAMESPACE,
        WORKLOAD_CREDENTIALS_PLURAL,
        name,
        {"status": status},
        _content_type="application/merge-patch+json",
    )
    return uri, STANDING_CREDENTIAL_ID


async def test_a_standing_record_authenticates_its_subject_without_a_pod(egress: Egress) -> None:
    """A record with no owner and no binding proves its subject alone: it reaches that subject's rules,
    no forwarded Pod token binds to it, and deleting the record revokes it."""
    custom = cast(CustomObjectsClient, k8s_client.CustomObjectsApi(egress.api.api))
    uri, _ = await _write_record(custom, STANDING_RECORD, {"subject": {"identity": {"name": SUBJECT_A}}})
    directory = egress.tmp_path / "standing-client"
    directory.mkdir()
    proxy_tls = proxy_svid_client_context(
        egress, issue_workload_leaf(egress.runtime_ca, uri, directory, dns_name="localhost")
    )
    async with aiohttp.ClientSession() as client:
        status, headers = await svid_request(client, egress, proxy_tls, "/public/standing")
        assert status == 200, headers
        status, headers = await svid_request(
            client,
            egress,
            proxy_tls,
            "/public/standing-with-untrusted-token",
            headers={"X-Agentplane-Workload-Token": "attacker-controlled token"},
        )
        assert status == 200, headers
        assert "x-agentplane-workload-token" not in egress.upstream.requests[-1][2]

        await custom.delete_namespaced_custom_object(
            GROUP,
            VERSION,
            SANDBOX_NAMESPACE,
            WORKLOAD_CREDENTIALS_PLURAL,
            STANDING_RECORD,
            body=k8s_client.V1DeleteOptions(),
        )
        status, headers = await svid_request(client, egress, proxy_tls, "/public/pruned")
    assert status == 403, headers
    assert headers["x-agentplane-egress"] == "denied; reason=token-rejected"
    assert [path for _, path, _ in egress.upstream.requests] == [
        "/public/standing",
        "/public/standing-with-untrusted-token",
    ]
    # Both authenticated decisions name the subject and no Pod.
    async for attempt in AsyncRetrying(stop=stop_after_delay(10), wait=wait_fixed(0.05), reraise=True):
        with attempt:
            assert await asyncio.to_thread(_source_pod_uids, egress.database_url, SUBJECT_A) == [None, None]


async def test_a_bound_record_still_needs_its_pod(egress: Egress) -> None:
    """A bound incarnation authenticates only once its owner has named the Pod it runs in."""
    custom = cast(CustomObjectsClient, k8s_client.CustomObjectsApi(egress.api.api))
    binding = {"podUID": egress.pod_uid}
    uri, _ = await _write_record(
        custom,
        "bound-without-pod",
        {"subject": {"identity": {"name": SUBJECT_A}}, "binding": binding},
        owner_uid=egress.sandbox_uid,
        accepted_binding=binding,
    )
    directory = egress.tmp_path / "bound-client"
    directory.mkdir()
    proxy_tls = proxy_svid_client_context(
        egress, issue_workload_leaf(egress.runtime_ca, uri, directory, dns_name="localhost")
    )
    async with aiohttp.ClientSession() as client:
        status, headers = await svid_request(client, egress, proxy_tls, "/public/no-pod")
    assert status == 403, headers
    assert headers["x-agentplane-egress"] == "denied; reason=token-rejected"
    assert not egress.upstream.requests


async def test_svid_certificate_expiry_denies_requests_inside_an_open_connect_tunnel(egress: Egress) -> None:
    certificate = issue_runner_svid(egress, egress.tmp_path / "svid-client", valid_for=datetime.timedelta(seconds=10))
    proxy_tls = proxy_svid_client_context(egress, certificate)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=1)) as client:
        status, _ = await svid_request(
            client, egress, proxy_tls, "/public/before-expiry", target_url=egress.url("/public/before-expiry")
        )
        assert status == 200
        await asyncio.sleep(10.1)
        status, headers = await svid_request(
            client, egress, proxy_tls, "/public/after-expiry", target_url=egress.url("/public/after-expiry")
        )
    assert status == 403, headers
    assert headers["x-agentplane-egress"] == "denied; reason=token-rejected"
    assert [path for _, path, _ in egress.upstream.requests] == ["/public/before-expiry"]


@asynccontextmanager
async def recording_grpc_upstream(
    keypair: tuple[bytes, bytes] | None, *, finished: asyncio.Event
) -> AsyncIterator[tuple[int, asyncio.Queue[tuple[tuple[str, str], ...]]]]:
    """A gRPC server recording the metadata of a bidirectional call: TLS with a keypair, else plaintext."""
    requests: asyncio.Queue[tuple[tuple[str, str], ...]] = asyncio.Queue()

    async def stream(request_iterator: AsyncIterator[bytes], context: grpc.aio.ServicerContext) -> AsyncIterator[bytes]:
        metadata = context.invocation_metadata() or ()
        await requests.put(tuple((str(item[0]), str(item[1])) for item in metadata))
        try:
            async for request in request_iterator:
                yield b"upstream:" + request
            context.set_trailing_metadata((("buildbuddy-test-trailer", "ok"),))
        finally:
            finished.set()

    server = grpc.aio.server()
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "google.devtools.build.v1.PublishBuildEvent",
                {
                    "PublishBuildToolEventStream": grpc.stream_stream_rpc_method_handler(
                        stream, request_deserializer=lambda value: value, response_serializer=lambda value: value
                    )
                },
            ),
        )
    )
    port = (
        server.add_insecure_port("127.0.0.1:0")
        if keypair is None
        else server.add_secure_port("127.0.0.1:0", grpc.ssl_server_credentials([(keypair[1], keypair[0])]))
    )
    await server.start()
    try:
        yield port, requests
    finally:
        await server.stop(grace=None)


def read_keypair(cert_path: Path, key_path: Path) -> tuple[bytes, bytes]:
    return cert_path.read_bytes(), key_path.read_bytes()


async def test_health_reports_ready_only_once_the_tunnel_accepts(egress: Egress) -> None:
    """Readiness gates every Service this Pod backs, and the tunnel is the one that carries traffic."""
    _, writer = await asyncio.open_connection("localhost", egress.proxy_listener.port, ssl=egress.client_tls)
    writer.close()
    await writer.wait_closed()


# SQLAlchemy loads this URL dialect dynamically.
# gazelle:include_dep @pypi//psycopg
_SUBJECT_DECISIONS = text("""
SELECT producer_id, connection_id, reason FROM egress_decision
WHERE subject_identity = :name
""")


_SUBJECT_SOURCE_PODS = text("""
SELECT source_pod_uid FROM egress_decision
WHERE subject_identity = :name
""")


def _source_pod_uids(database_url: str, subject: str) -> list[str | None]:
    """The source Pod UIDs the subject's decisions recorded in the shared history."""
    with ExitStack() as stack:
        engine = create_engine(database_url)
        stack.callback(engine.dispose)
        with engine.connect() as connection:
            return [row.source_pod_uid for row in connection.execute(_SUBJECT_SOURCE_PODS, {"name": subject})]


def _decisions_of(database_url: str, subject: str) -> list[tuple[UUID, str, str | None]]:
    """The producer, connection and refusal reason of every decision the subject has in the shared history."""
    with ExitStack() as stack:
        engine = create_engine(database_url)
        stack.callback(engine.dispose)
        with engine.connect() as connection:
            return [
                (row.producer_id, row.connection_id, row.reason)
                for row in connection.execute(_SUBJECT_DECISIONS, {"name": subject})
            ]


@dataclass
class SeverableProxy:
    """A TCP pass-through to an API server, whose open connections a test can cut as a dropped network path does."""

    url: str
    open: set[asyncio.StreamWriter] = field(default_factory=set)

    def sever(self) -> None:
        """Cuts every open connection; new ones are still accepted."""
        for writer in self.open:
            writer.transport.abort()
        self.open.clear()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except ConnectionError:
        pass  # The other half was severed or closed; aborting this one below is all that is left to do.
    finally:
        writer.transport.abort()


@asynccontextmanager
async def severable_proxy(kubeconfig: Path) -> AsyncIterator[SeverableProxy]:
    """A `SeverableProxy` in front of the API server `kubeconfig` reaches; TLS passes through untouched."""
    target = urlsplit(
        one(yaml.safe_load(await asyncio.to_thread(kubeconfig.read_text))["clusters"])["cluster"]["server"]
    )

    async def forward(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        upstream_reader, upstream_writer = await asyncio.open_connection(target.hostname, target.port)
        proxy.open |= {client_writer, upstream_writer}
        await asyncio.gather(_pipe(client_reader, upstream_writer), _pipe(upstream_reader, client_writer))
        proxy.open -= {client_writer, upstream_writer}

    server = await asyncio.start_server(forward, "127.0.0.1", 0)
    # The API server's certificate names its IP, not its port, so it verifies through this one too.
    proxy = SeverableProxy(url=f"https://127.0.0.1:{server.sockets[0].getsockname()[1]}")
    try:
        yield proxy
    finally:
        proxy.sever()
        server.close()
        await server.wait_closed()


# The second replica reads as this user. Taking its grant in the rules namespace makes that replica's
# API server refuse its lists and watches there, so its view fails while it still reviews tokens.
_REPLICA_USER = "test-egress-replica"
_RBAC = "rbac.authorization.k8s.io"


async def grant_replica(server: KubernetesApiServer, namespace: str) -> None:
    await k8s_client.RbacAuthorizationV1Api(server.api).create_namespaced_role_binding(
        namespace,
        k8s_client.V1RoleBinding(
            metadata=k8s_client.V1ObjectMeta(name=_REPLICA_USER),
            role_ref=k8s_client.V1RoleRef(api_group=_RBAC, kind="ClusterRole", name=_REPLICA_USER),
            subjects=[k8s_client.RbacV1Subject(api_group=_RBAC, kind="User", name=_REPLICA_USER)],
        ),
    )


async def revocable_kubeconfig(server: KubernetesApiServer, path: Path, via: SeverableProxy) -> Path:
    """`server`'s kubeconfig acting as `_REPLICA_USER`, who may review tokens and read what the proxy reads, through `via`."""
    rbac = k8s_client.RbacAuthorizationV1Api(server.api)
    await rbac.create_cluster_role(
        k8s_client.V1ClusterRole(
            metadata=k8s_client.V1ObjectMeta(name=_REPLICA_USER),
            rules=[
                k8s_client.V1PolicyRule(api_groups=[""], resources=["secrets"], verbs=["get", "list", "watch"]),
                k8s_client.V1PolicyRule(api_groups=[""], resources=["serviceaccounts"], verbs=["get"]),
                k8s_client.V1PolicyRule(
                    api_groups=[GROUP],
                    resources=[
                        CREDENTIALS_PLURAL,
                        POLICY_SETS_PLURAL,
                        POLICY_BINDINGS_PLURAL,
                        "identities",
                        WORKLOAD_CREDENTIALS_PLURAL,
                    ],
                    verbs=["get", "list", "watch"],
                ),
                k8s_client.V1PolicyRule(
                    api_groups=[GROUP], resources=[f"{POLICY_BINDINGS_PLURAL}/status"], verbs=["patch"]
                ),
            ],
        )
    )
    await rbac.create_cluster_role_binding(
        k8s_client.V1ClusterRoleBinding(
            metadata=k8s_client.V1ObjectMeta(name=_REPLICA_USER),
            role_ref=k8s_client.V1RoleRef(api_group=_RBAC, kind="ClusterRole", name="system:auth-delegator"),
            subjects=[k8s_client.RbacV1Subject(api_group=_RBAC, kind="User", name=_REPLICA_USER)],
        )
    )
    for namespace in (NAMESPACE, CREDENTIALS_NAMESPACE, SANDBOX_NAMESPACE):
        await grant_replica(server, namespace)
    config = yaml.safe_load(await asyncio.to_thread(server.kubeconfig.read_text))
    for entry in config["users"]:
        entry["user"]["as"] = _REPLICA_USER
    one(config["clusters"])["cluster"]["server"] = via.url
    await asyncio.to_thread(path.write_text, yaml.safe_dump(config))
    return path


async def test_two_process_replicas_diverge_fail_closed_and_share_decisions(
    tmp_path: Path, history_db_url: str
) -> None:
    ca = make_ca("test-replica-ca")
    cert, key = write_ca(ca, tmp_path, "replica")
    async with (
        kubernetes_apiserver() as first_api,
        kubernetes_apiserver() as second_api,
        severable_proxy(second_api.kubeconfig) as second_path,
        recording_upstream(*issue_leaf(ca, UPSTREAM_HOST, tmp_path)) as upstream,
    ):
        # The replicas have independent live Sandbox objects and client SVIDs, under the same test root.
        seeded = [await seed(first_api), await seed(second_api)]
        server_directory = tmp_path / "replica-proxy-server"
        server_directory.mkdir()
        server_certificate = issue_workload_leaf(ca, EGRESS_PROXY_URI, server_directory, dns_name="localhost")
        client_contexts = []
        for index in range(len(seeded)):
            directory = tmp_path / f"replica-client-{index}"
            directory.mkdir()
            certificate = issue_workload_leaf(
                ca, workload_uri(SANDBOX_NAMESPACE, SANDBOX_A, RUNNER_CREDENTIAL_ID), directory
            )
            context = client_tls_context(ca)
            context.load_cert_chain(*(str(path) for path in certificate))
            client_contexts.append(context)
        first_tls, second_tls = client_contexts
        settings = [
            replica_settings(
                api_config=api_config,
                ca=(cert, key),
                database_url=history_db_url,
                svid_cert=server_certificate,
                svid_ca_file=cert,
            )
            for api_config in (
                first_api.kubeconfig,
                await revocable_kubeconfig(second_api, tmp_path / "second-replica-kubeconfig.yaml", via=second_path),
            )
        ]
        async with (
            go_replica(settings[0], directory=tmp_path) as first,
            go_replica(settings[1], directory=tmp_path) as second,
            aiohttp.ClientSession() as client,
        ):

            async def get(replica: GoReplica, context: ssl.SSLContext, path: str, expected: int) -> None:
                async with client.get(
                    f"http://{UPSTREAM_HOST}:{upstream.http_port}/public/{path}",
                    proxy=f"https://localhost:{replica.proxy_listener.port}",
                    ssl=context,
                ) as response:
                    await response.read()
                    assert response.status == expected, dict(response.headers)

            await get(first, first_tls, "first", 200)
            await get(second, second_tls, "second", 200)
            # Only one watch source observes revocation. No replica can acknowledge the other.
            await revoke_binding(first_api)
            async for attempt in AsyncRetrying(stop=stop_after_delay(10), wait=wait_fixed(0.05), reraise=True):
                with attempt:
                    await get(first, first_tls, "revoked", 403)
            await get(second, second_tls, "still-current", 200)
            # Its API server now refuses the second replica's lists and watches of the rules namespace. RBAC
            # is checked only when a request starts, so cut the watches already open to make them restart.
            await k8s_client.RbacAuthorizationV1Api(second_api.api).delete_namespaced_role_binding(
                _REPLICA_USER, NAMESPACE
            )
            second_path.sever()
            await second.process.health(serving=False)
            before = len(upstream.requests)
            await get(second, second_tls, "stale-existing-connection", 502)
            assert len(upstream.requests) == before
            await revoke_binding(second_api)
            await grant_replica(second_api, NAMESPACE)
            await second.process.health(serving=True)
            await get(second, second_tls, "recovered-revocation", 403)
            async for attempt in AsyncRetrying(stop=stop_after_delay(10), wait=wait_fixed(0.05), reraise=True):
                with attempt:
                    rows = await asyncio.to_thread(_decisions_of, history_db_url, SUBJECT_A)
                    producers = {producer for producer, _, _ in rows}
                    assert len(producers) == 2
                    assert all(
                        len({connection for row_producer, connection, _ in rows if row_producer == producer}) == 1
                        for producer in producers
                    )
                    assert any(reason == "unavailable" for _, _, reason in rows)


async def test_go_proxy_finishes_an_admitted_request_after_sigterm(egress: Egress) -> None:
    """Termination takes the Pod out of rotation at once, yet lets the request already admitted finish."""
    upstream = egress.upstream
    async with aiohttp.ClientSession() as client:

        async def slow_status() -> int:
            async with client.get(
                f"http://{UPSTREAM_HOST}:{upstream.http_port}/public/slow", proxy=egress.svid_url, ssl=egress.client_tls
            ) as response:
                await response.read()
                return response.status

        pending = asyncio.create_task(slow_status())
        await asyncio.wait_for(upstream.slow_started.wait(), 10)
        egress.replica.process.terminate()
        await egress.replica.process.health(serving=False)
        # Readiness turns NOT_SERVING before the proxy listener finishes shutting down.
        async for attempt in AsyncRetrying(stop=stop_after_delay(10), wait=wait_fixed(0.05), reraise=True):
            with attempt:
                try:
                    _, writer = await asyncio.open_connection(egress.proxy_listener.host, egress.proxy_listener.port)
                except ConnectionRefusedError:
                    break
                writer.close()
                await writer.wait_closed()
                raise AssertionError("proxy listener still accepts connections while draining")
        assert not pending.done()
        upstream.slow_release.set()
        assert await asyncio.wait_for(pending, 10) == 200
    assert len(upstream.requests) == 1


async def test_header_substitution(egress: Egress) -> None:
    async with (
        aiohttp.ClientSession() as client,
        client.get(
            egress.url("/contract"),
            proxy=egress.svid_url,
            ssl=egress.client_tls,
            headers={"x-buildbuddy-api-key": placeholder_of(_CONTRACT_CREDENTIAL)},
        ) as response,
    ):
        assert response.status == 200
        await response.read()
    assert one(egress.upstream.requests)[2]["x-buildbuddy-api-key"] == "contract-secret"
    assert "proxy-authorization" not in one(egress.upstream.requests)[2]


async def test_real_binding_revocation_denies_the_next_connection(egress: Egress) -> None:
    async with aiohttp.ClientSession() as client:

        async def request_status() -> int:
            try:
                async with client.get(
                    egress.url("/contract"),
                    proxy=egress.svid_url,
                    ssl=egress.client_tls,
                    headers={"x-buildbuddy-api-key": placeholder_of(_CONTRACT_CREDENTIAL)},
                ) as response:
                    await response.read()
                    return response.status
            except aiohttp.ClientHttpProxyError as error:
                return error.status

        assert await request_status() == 200
        await revoke_binding(egress.api)
        async for attempt in AsyncRetrying(stop=stop_after_delay(10), wait=wait_fixed(0.05), reraise=True):
            with attempt:
                assert await request_status() == 403
        before = len(egress.upstream.requests)
        assert await request_status() == 403
        assert len(egress.upstream.requests) == before


async def test_incremental_sse_and_websocket(egress: Egress) -> None:
    finish = asyncio.Event()

    async def sse(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(b"data: first\n\n")
        await finish.wait()
        await response.write_eof()
        return response

    async def websocket(request: web.Request) -> web.WebSocketResponse:
        response = web.WebSocketResponse()
        await response.prepare(request)
        async for message in response:
            assert message.type is aiohttp.WSMsgType.TEXT
            await response.send_str(f"upstream:{message.data}")
        return response

    app = web.Application()
    app.router.add_get("/contract-sse", sse)
    app.router.add_get("/contract-ws", websocket)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(
        runner,
        "127.0.0.1",
        0,
        ssl_context=server_tls_context(*issue_leaf(egress.upstream_ca, UPSTREAM_HOST, egress.tmp_path)),
    )
    await site.start()
    port = one(runner.addresses)[1]
    try:
        async with aiohttp.ClientSession() as client:
            async with client.get(
                f"https://{UPSTREAM_HOST}:{port}/contract-sse", proxy=egress.svid_url, ssl=egress.client_tls
            ) as response:
                assert response.status == 200
                async with asyncio.timeout(5):
                    assert await response.content.readline() == b"data: first\n"
                # The server is still waiting here: completion cannot explain the read.
                assert not finish.is_set()
                finish.set()
                await response.read()
            async with client.ws_connect(
                f"https://{UPSTREAM_HOST}:{port}/contract-ws", proxy=egress.svid_url, ssl=egress.client_tls
            ) as websocket_client:
                await websocket_client.send_str("first")
                async with asyncio.timeout(5):
                    assert (await websocket_client.receive()).data == "upstream:first"
                await websocket_client.send_str("second")
                async with asyncio.timeout(5):
                    assert (await websocket_client.receive()).data == "upstream:second"
    finally:
        finish.set()
        await runner.cleanup()


async def test_denial_has_zero_upstream_tcp_accepts(egress: Egress) -> None:
    accepted: list[asyncio.StreamWriter] = []
    observed = asyncio.Event()

    async def accept(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        accepted.append(writer)
        observed.set()
        writer.close()
        await writer.wait_closed()

    # Plain raw TCP sees accepts even if the proxy would fail its subsequent TLS
    # handshake. Counting aiohttp handlers would miss this security boundary.
    async with await asyncio.start_server(accept, "127.0.0.1", 0) as upstream:
        port = upstream.sockets[0].getsockname()[1]
        async with (
            aiohttp.ClientSession() as client,
            client.get(
                f"https://{UPSTREAM_HOST}:{port}/not-admitted", proxy=egress.svid_url, ssl=egress.client_tls
            ) as response,
        ):
            assert response.status == 403
            await response.read()
        assert accepted == []
        # Positive control proves the accept counter is attached to a live socket.
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        async with asyncio.timeout(5):
            await observed.wait()
        writer.close()
        await writer.wait_closed()
        assert len(accepted) == 1


@pytest.mark.parametrize("tls", [True, False], ids=["tls-h2", "h2c"])
async def test_interactive_grpc_before_eof(egress: Egress, monkeypatch: pytest.MonkeyPatch, tls: bool) -> None:
    keypair = read_keypair(*issue_leaf(egress.upstream_ca, UPSTREAM_HOST, egress.tmp_path)) if tls else None
    finished = asyncio.Event()
    async with (
        recording_grpc_upstream(keypair, finished=finished) as (port, requests),
        SidecarProcess(
            proxy=Proxy(
                address=f"localhost:{egress.proxy_listener.port}",
                transport=TLSProxyTransport(
                    ca_file=egress.tmp_path / "runtime-cert.pem",
                    auth=SpiffeProxyAuth(cert_file=egress.client_certificate[0], key_file=egress.client_certificate[1]),
                ),
            )
        ) as sidecar,
    ):
        monkeypatch.setenv(
            "https_proxy" if tls else "grpc_proxy",
            f"http://{sidecar.proxy_listener.host}:{sidecar.proxy_listener.port}",
        )
        monkeypatch.setenv("no_proxy", "")
        channel = (
            grpc.aio.secure_channel(
                f"{UPSTREAM_HOST}:{port}",
                grpc.ssl_channel_credentials(root_certificates=egress.interception_ca.cert_pem),
            )
            if tls
            else grpc.aio.insecure_channel(f"{UPSTREAM_HOST}:{port}")
        )
        async with channel:
            call = channel.stream_stream(
                _CONTRACT_PATH, request_serializer=lambda value: value, response_deserializer=lambda value: value
            )(metadata=(("x-buildbuddy-api-key", placeholder_of(_CONTRACT_CREDENTIAL)),), timeout=10)
            for message in (b"first", b"second"):
                await call.write(message)
                assert await call.read() == b"upstream:" + message
            await call.done_writing()
            assert await call.read() is grpc.aio.EOF
            assert dict(await call.trailing_metadata() or ())["buildbuddy-test-trailer"] == "ok"
            assert finished.is_set()
            finished.clear()
            deadline = channel.stream_stream(
                _CONTRACT_PATH, request_serializer=lambda value: value, response_deserializer=lambda value: value
            )(metadata=(("x-buildbuddy-api-key", placeholder_of(_CONTRACT_CREDENTIAL)),), timeout=1)
            await deadline.write(b"before-deadline")
            assert await deadline.read() == b"upstream:before-deadline"
            with pytest.raises(grpc.aio.AioRpcError) as expired:
                await deadline.read()
            assert expired.value.code() is grpc.StatusCode.DEADLINE_EXCEEDED
            async with asyncio.timeout(5):
                await finished.wait()
            finished.clear()
            cancelled = channel.stream_stream(
                _CONTRACT_PATH, request_serializer=lambda value: value, response_deserializer=lambda value: value
            )(metadata=(("x-buildbuddy-api-key", placeholder_of(_CONTRACT_CREDENTIAL)),), timeout=10)
            await cancelled.write(b"before-cancel")
            assert await cancelled.read() == b"upstream:before-cancel"
            assert cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled.read()
            async with asyncio.timeout(5):
                await finished.wait()
    headers = dict(await requests.get())
    assert headers["x-buildbuddy-api-key"] == "contract-secret"
    assert "proxy-authorization" not in headers


if __name__ == "__main__":
    pytest_bazel.main()
