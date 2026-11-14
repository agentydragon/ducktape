"""Real Egress and Notification processes behind the guest's credential-holding sidecar."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from ipaddress import ip_network
from pathlib import Path
from uuid import uuid4

import grpc
from google.protobuf.json_format import Parse
from kubernetes_asyncio import client as k8s_client
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.crd_group import GROUP, VERSION
from agentplane.egress.api.admin import egress_admin_pb2, egress_admin_pb2_grpc
from agentplane.egress.api.rules import egress_rules_pb2, egress_rules_pb2_grpc
from agentplane.egress.platform_routes import PlatformRoute, PlatformRoutes, TLSConnectRouteTransport
from agentplane.egress.settings import Settings
from agentplane.egress.sidecar_config import Proxy, SpiffeProxyAuth, TLSProxyTransport, WorkloadRoute
from agentplane.egress.testing.go_replica import go_replica
from agentplane.egress.testing.sidecar_process import SidecarProcess
from agentplane.identity.auth import listener
from agentplane.net.peertls.config import APP_URI, EGRESS_PROXY_URI
from agentplane.notification_service.api.notifications import notifications_pb2, notifications_pb2_grpc
from agentplane.notification_service.testing.process_fixture.process import NotificationProcess, notification_process
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from agentplane.testing.tls import CertificateAuthority, issue_workload_leaf, make_ca, workload_uri, write_ca
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from util.testing.postgres import create_temporary_database

_RULES_MARKER = "AGENTPLANE_EGRESS_RULES_JSON"
_NOTIFICATIONS_MARKER = "AGENTPLANE_NOTIFICATION_SUBSCRIPTIONS_JSON"
_END_MARKER = "AGENTPLANE_PLATFORM_PROBE_COMPLETE"
CLI_PLATFORM_PROBE = (
    f"printf '\\n{_RULES_MARKER}\\n'\n"
    "agentplane --json egress rules\n"
    f"printf '\\n{_NOTIFICATIONS_MARKER}\\n'\n"
    "agentplane --json notifications subscriptions --limit 1\n"
    f"printf '\\n{_END_MARKER}\\n'\n"
)


@dataclass(frozen=True)
class PlatformServices:
    guest_url: str
    proxy_guest_url: str
    notification: NotificationProcess
    egress_admin_client: egress_admin_pb2_grpc.EgressAdminStub
    rules_client: egress_rules_pb2_grpc.EgressRulesStub
    notifications_client: notifications_pb2_grpc.NotificationsAsyncStub


@asynccontextmanager
async def platform_services(
    *,
    postgres_admin_url: str,
    sandbox: SandboxCluster,
    ca: CertificateAuthority,
    directory: Path,
    notifications_server_uri: str,
    workload_routes: Mapping[str, WorkloadRoute] | None = None,
    tls_passthrough_targets: tuple[str, ...] = (),
) -> AsyncIterator[PlatformServices]:
    await asyncio.to_thread(directory.mkdir, parents=True)
    runtime_root, _ = write_ca(ca, directory, "runtime")
    interception = write_ca(make_ca("direct-guest-interception"), directory, "interception")
    server_directory = directory / "egress-server"
    await asyncio.to_thread(server_directory.mkdir)
    server_certificate, server_key = issue_workload_leaf(ca, EGRESS_PROXY_URI, server_directory, dns_name="localhost")
    client_directory = directory / "sidecar-client"
    await asyncio.to_thread(client_directory.mkdir)
    certificate, key = issue_workload_leaf(ca, sandbox.environment_runtime_identity_uri, client_directory)
    admin_client_directory = directory / "admin-client"
    await asyncio.to_thread(admin_client_directory.mkdir)
    admin_certificate, admin_key = issue_workload_leaf(ca, APP_URI, admin_client_directory)
    private_key, certificate_chain = await asyncio.gather(
        asyncio.to_thread(key.read_bytes), asyncio.to_thread(certificate.read_bytes)
    )
    credentials = grpc.ssl_channel_credentials(
        root_certificates=ca.cert_pem, private_key=private_key, certificate_chain=certificate_chain
    )
    admin_private_key, admin_certificate_chain = await asyncio.gather(
        asyncio.to_thread(admin_key.read_bytes), asyncio.to_thread(admin_certificate.read_bytes)
    )
    admin_credentials = grpc.ssl_channel_credentials(
        root_certificates=ca.cert_pem, private_key=admin_private_key, certificate_chain=admin_certificate_chain
    )
    with (
        create_temporary_database(postgres_admin_url, f"guest_notifications_{uuid4().hex}") as notifications_database,
        create_temporary_database(postgres_admin_url, f"guest_egress_{uuid4().hex}") as egress_database,
    ):
        async with notification_process(
            database_url=notifications_database,
            api=sandbox.server,
            sandbox=sandbox,
            ca=ca,
            directory=directory / "notifications",
        ) as notification:
            settings = Settings(
                rules_namespace=sandbox.namespace,
                credentials_namespace=sandbox.namespace,
                allowed_service_account_namespaces=frozenset({sandbox.namespace}),
                kubeconfig=sandbox.server.kubeconfig,
                log_levels={},
                ca_cert=interception[0],
                ca_key=interception[1],
                upstream_ca_file=runtime_root,
                tls_passthrough_targets=[notification.address, *tls_passthrough_targets],
                admin_grpc_audience="direct-vm-admin",
                admin_grpc_callers=[f"{sandbox.namespace}/direct-vm-test-reader"],
                admin_grpc_service_callers=frozenset({APP_URI}),
                forward=listener.Listener(
                    listen_address="127.0.0.1:0",
                    tls=listener.TLS(cert_file=server_certificate, key_file=server_key),
                    auth=[listener.SPIFFEAuth(ca_file=runtime_root)],
                ),
                exempt_networks=[ip_network("127.0.0.0/8"), ip_network("::1/128")],
            )
            async with go_replica(
                settings, database_url=egress_database, directory=directory, tunnel_rules=True
            ) as egress:
                rules_address = f"localhost:{egress.admin_listener.port}"
                async with (
                    SidecarProcess(
                        proxy=Proxy(
                            address=f"{egress.proxy_listener.host}:{egress.proxy_listener.port}",
                            transport=TLSProxyTransport(
                                ca_file=runtime_root, auth=SpiffeProxyAuth(cert_file=certificate, key_file=key)
                            ),
                        ),
                        workload_routes=workload_routes or {},
                        platform_routes=PlatformRoutes(
                            listen_address="127.0.0.1:0",
                            routes={
                                "rules": PlatformRoute(
                                    service_prefixes=("/agentplane.egress.EgressRules/",),
                                    upstream=rules_address,
                                    transport=TLSConnectRouteTransport(server_uri=EGRESS_PROXY_URI),
                                ),
                                "notifications": PlatformRoute(
                                    service_prefixes=("/agentplane.notification_service.Notifications/",),
                                    upstream=notification.address,
                                    transport=TLSConnectRouteTransport(server_uri=notifications_server_uri),
                                ),
                            },
                        ),
                        stderr=undeclared_outputs_dir() / f"guest-platform-sidecar-{sandbox.uid}.stderr",
                    ) as sidecar,
                    grpc.aio.secure_channel(rules_address, credentials) as rules_channel,
                    grpc.aio.secure_channel(notification.address, credentials) as notification_channel,
                    grpc.aio.secure_channel(rules_address, admin_credentials) as admin_channel,
                ):
                    yield PlatformServices(
                        guest_url=f"http://10.0.2.1:{sidecar.platform_route_listener.port}",
                        proxy_guest_url=f"http://10.0.2.1:{sidecar.proxy_listener.port}",
                        notification=notification,
                        egress_admin_client=egress_admin_pb2_grpc.EgressAdminStub(admin_channel),
                        rules_client=egress_rules_pb2_grpc.EgressRulesStub(rules_channel),
                        notifications_client=notifications_pb2_grpc.NotificationsStub(notification_channel),
                    )


async def assert_model_connect_decision(
    services: PlatformServices, sandbox: SandboxCluster, *, host: str, port: int
) -> None:
    request = egress_admin_pb2.ListDecisionsRequest(
        filter=egress_admin_pb2.DecisionFilter(
            identity=egress_rules_pb2.IdentitySubject(name=sandbox.execution_identity_name)
        )
    )
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(5), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            response = await services.egress_admin_client.ListDecisions(request, timeout=2)
            matching = [
                decision
                for decision in response.decisions
                if decision.method == "CONNECT" and decision.host == host and decision.port == port
            ]
            assert matching, (
                f"Egress recorded no CONNECT to {host}:{port} for Sandbox Identity "
                f"{sandbox.execution_identity_name}: {response.decisions!r}"
            )
            for decision in matching:
                assert decision.outcome == egress_admin_pb2.Decision.OUTCOME_ALLOW, decision
                assert not decision.policy, f"the exact SVID route unexpectedly used a policy: {decision}"


def assert_platform_cli_output(stdout: str, services: PlatformServices, sandbox: SandboxCluster) -> None:
    rules_json, _ = json.JSONDecoder().raw_decode(stdout.split(_RULES_MARKER + "\n", 1)[1].lstrip())
    rules = Parse(json.dumps(rules_json), egress_rules_pb2.GetRulesResponse())
    assert rules.WhichOneof("caller") == "identity", rules
    assert rules.identity.name == sandbox.execution_identity_name, rules
    # List commands print one JSON object per line. Require exactly one nonempty
    # owned row, including no second value after --limit 1.
    notifications_stdout = stdout.split(_NOTIFICATIONS_MARKER + "\n", 1)[1].split(_END_MARKER + "\n", 1)[0].strip()
    assert _END_MARKER + "\n" in stdout, stdout
    notifications_json, end = json.JSONDecoder().raw_decode(notifications_stdout)
    assert not notifications_stdout[end:].strip(), notifications_stdout
    subscription = Parse(json.dumps(notifications_json), notifications_pb2.Subscription())
    assert subscription.id == str(services.notification.subscription_id), subscription
    assert subscription.idempotency_key == services.notification.idempotency_key, subscription
    assert subscription.source.actions.request_id == str(services.notification.request_id), subscription


async def assert_credential_rotation_refuses_old_svid(services: PlatformServices, sandbox: SandboxCluster) -> None:
    rules = await services.rules_client.GetRules(egress_rules_pb2.GetRulesRequest(), timeout=5)
    assert rules.identity.name == sandbox.execution_identity_name, rules
    listed = await services.notifications_client.ListSubscriptions(
        notifications_pb2.ListSubscriptionsRequest(), timeout=5
    )
    assert [subscription.id for subscription in listed.subscriptions] == [str(services.notification.subscription_id)]
    records = sandbox.custom_objects
    before = await records.get_namespaced_custom_object(
        GROUP, VERSION, sandbox.namespace, "workloadcredentials", sandbox.name
    )
    credential_id = uuid4().hex
    await records.patch_namespaced_custom_object_status(
        GROUP,
        VERSION,
        sandbox.namespace,
        "workloadcredentials",
        sandbox.name,
        {
            "status": {
                "credentialID": credential_id,
                "uri": workload_uri(sandbox.namespace, sandbox.name, credential_id),
                "secretName": f"svid-{sandbox.name}-{credential_id}",
            }
        },
        _content_type="application/merge-patch+json",
    )
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(10), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            results = await asyncio.gather(
                services.rules_client.GetRules(egress_rules_pb2.GetRulesRequest(), timeout=2),
                services.notifications_client.ListSubscriptions(
                    notifications_pb2.ListSubscriptionsRequest(), timeout=2
                ),
                return_exceptions=True,
            )
            for result in results:
                assert isinstance(result, grpc.aio.AioRpcError), result
                assert result.code() == grpc.StatusCode.UNAUTHENTICATED, result
            break
    after = await records.get_namespaced_custom_object(
        GROUP, VERSION, sandbox.namespace, "workloadcredentials", sandbox.name
    )
    assert after["metadata"]["uid"] == before["metadata"]["uid"]
    pod = await k8s_client.CoreV1Api(sandbox.server.api).read_namespaced_pod(sandbox.name, sandbox.namespace)
    assert pod.metadata.uid == sandbox.pod_uid
