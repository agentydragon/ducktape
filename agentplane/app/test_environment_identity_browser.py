"""Environment identity changes through Chromium, the Gateway and Kubernetes admission."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast, override

import grpc
import pytest_bazel
from google.protobuf.empty_pb2 import Empty
from playwright.async_api import expect

from agentplane.app.testing.browser_certificate import BrowserCertificate
from agentplane.app.testing.history import SessionHistory
from agentplane.app.testing.http2_proxy import http2_proxy
from agentplane.app.testing.replication_process import GatewayAccess, SandboxesAccess, app_process
from agentplane.history_service.testing.electric_service import ElectricService
from agentplane.net.peertls.config import APP_URI, POLICY_CONTROLLER_URI, ClientTLS
from agentplane.policy.v1 import policy_service_pb2, policy_service_pb2_grpc
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from agentplane.testing.tls import issue_workload_leaf, make_ca
from testing.frontend_visual.capture import VisualPage

# gazelle:include_dep //agentplane/app:integration_fixtures
# gazelle:include_dep //agentplane/app/testing:thread_browser
# gazelle:include_dep //agentplane/app/testing:thread_browser_backends
pytest_plugins = (
    "agentplane.app.testing.thread_browser",
    "agentplane.app.integration_fixtures",
    "agentplane.app.testing.thread_browser_backends",
)


@asynccontextmanager
async def serving_tls(server: grpc.aio.Server, address: str, credentials: grpc.ServerCredentials) -> AsyncIterator[int]:
    port = server.add_secure_port(address, credentials)
    await server.start()
    try:
        yield port
    finally:
        await server.stop(grace=0)


async def test_environment_identity_change_obeys_real_gateway_and_kubernetes_admission(
    review_view: VisualPage,
    db_url: str,
    event_logs: SessionHistory,
    electric: ElectricService,
    certificate: BrowserCertificate,
    sandbox: SandboxCluster,
    gateway_access: GatewayAccess,
    tmp_path: Path,
) -> None:
    """The browser uses real BFF, Gateway, PostgreSQL and Kubernetes mutation paths."""
    identity = await sandbox.custom_objects.create_namespaced_custom_object(
        "agentplane.allegedly.works",
        "v1alpha1",
        sandbox.namespace,
        "identities",
        {
            "apiVersion": "agentplane.allegedly.works/v1alpha1",
            "kind": "Identity",
            "metadata": {"name": "browser-identity"},
            "spec": {"description": "Browser identity change"},
        },
    )
    assert identity["metadata"]["uid"]

    # Policy watches supply the identity picker and the page's unrelated empty policy tables.
    # The change request continues through the real app, Gateway, database, and API server.
    class IdentityWatch(policy_service_pb2_grpc.PolicyServiceServicer):
        @override
        async def WatchIdentities(
            self, request: policy_service_pb2.WatchIdentitiesRequest, context: grpc.aio.ServicerContext
        ) -> AsyncIterator[policy_service_pb2.WatchIdentitiesResponse]:
            yield policy_service_pb2.WatchIdentitiesResponse(
                snapshot=policy_service_pb2.IdentityChunk(
                    identities=[
                        policy_service_pb2.Identity(
                            identity=policy_service_pb2.PolicyResourceIdentity(
                                namespace=sandbox.namespace, name="browser-identity", uid=identity["metadata"]["uid"]
                            )
                        )
                    ]
                )
            )
            yield policy_service_pb2.WatchIdentitiesResponse(snapshot_complete=Empty())
            await asyncio.Event().wait()

        @override
        async def WatchPolicyBindings(
            self, request: policy_service_pb2.WatchPolicyBindingsRequest, context: grpc.aio.ServicerContext
        ) -> AsyncIterator[policy_service_pb2.WatchPolicyBindingsResponse]:
            yield policy_service_pb2.WatchPolicyBindingsResponse(snapshot=policy_service_pb2.PolicyBindingChunk())
            yield policy_service_pb2.WatchPolicyBindingsResponse(snapshot_complete=Empty())
            await asyncio.Event().wait()

        @override
        async def WatchPolicySets(
            self, request: policy_service_pb2.WatchPolicySetsRequest, context: grpc.aio.ServicerContext
        ) -> AsyncIterator[policy_service_pb2.WatchPolicySetsResponse]:
            yield policy_service_pb2.WatchPolicySetsResponse(snapshot=policy_service_pb2.PolicySetChunk())
            yield policy_service_pb2.WatchPolicySetsResponse(snapshot_complete=Empty())
            await asyncio.Event().wait()

    policy = grpc.aio.server()
    policy_service_pb2_grpc.add_PolicyServiceServicer_to_server(IdentityWatch(), policy)
    policy_ca = make_ca("Policy Service browser test")
    policy_ca_file = tmp_path / "policy-service-ca.pem"
    policy_ca_file.write_bytes(policy_ca.cert_pem)
    policy_certificate, policy_key = issue_workload_leaf(policy_ca, POLICY_CONTROLLER_URI, tmp_path)
    policy_server_credentials = grpc.ssl_server_credentials(
        [(policy_key.read_bytes(), policy_certificate.read_bytes())],
        root_certificates=policy_ca.cert_pem,
        require_client_auth=True,
    )
    app_certificate_dir = tmp_path / "policy-app-svid"
    app_certificate_dir.mkdir()
    app_certificate, app_key = issue_workload_leaf(policy_ca, APP_URI, app_certificate_dir)
    async with (
        serving_tls(policy, "127.0.0.1:0", policy_server_credentials) as policy_port,
        app_process(
            db_url,
            history=event_logs.history.service,
            sandboxes=SandboxesAccess(kubeconfig=sandbox.server.kubeconfig, namespace=sandbox.namespace),
            gateway=gateway_access,
            browser_login=True,
            certificate=certificate,
            electric_url=electric.url,
            config={
                "policy_service_target": f"127.0.0.1:{policy_port}",
                "policy_service_token_file": "",
                "policy_service_tls": ClientTLS(
                    ca_file=policy_ca_file,
                    server_uri=POLICY_CONTROLLER_URI,
                    cert_file=app_certificate,
                    key_file=app_key,
                    client_uri=APP_URI,
                ).model_dump(mode="json"),
            },
        ) as app,
        http2_proxy(app.url, certificate) as ingress,
    ):
        page = review_view.page
        await app.login(page)
        await page.goto(f"{ingress.url}/#/sandboxes/{sandbox.name}?tab=status")
        panel = page.get_by_role("region", name="Environment identity")
        initial_identity = f"{sandbox.name}-identity"
        await expect(panel.get_by_role("link", name=initial_identity)).to_be_visible()
        await expect(panel.get_by_text(f"Credential at last check: {initial_identity}", exact=True)).to_be_visible()
        await expect(panel.get_by_role("button", name="Change identity")).to_have_count(0)

        async def change() -> dict[str, object]:
            return cast(
                dict[str, object],
                await page.evaluate(
                    """async ({namespace, name, uid}) => {
                  const response = await fetch(`/upstream/environments/v1/environments/${namespace}/${name}:identity`, {
                    method: 'PATCH', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({target: {namespace, name, uid}, identityName: 'browser-identity'})
                  });
                  return {status: response.status, body: await response.json()};
                }""",
                    {"namespace": sandbox.namespace, "name": sandbox.name, "uid": sandbox.uid},
                ),
            )

        refused = await change()
        assert refused["status"] == 400
        assert "nothing runs" in str(refused["body"])
        assert (await sandbox.sandbox())["spec"]["identity"] == {"name": initial_identity}

        await sandbox.patch_sandbox_status({"status": {"stopStage": "Stopped"}})
        selector = panel.get_by_role("combobox", name="Identity for next start")
        await expect(selector).to_be_visible()
        await selector.click()
        await page.get_by_role("option", name="browser-identity").click()
        await expect(panel.get_by_text("The next identity can read anything earlier agents left")).to_be_visible()
        await panel.get_by_role("button", name="Change identity").click()
        await expect(panel.get_by_text("Current: browser-identity")).to_be_visible()
        assert (await sandbox.sandbox())["spec"]["identity"] == {"name": "browser-identity"}


if __name__ == "__main__":
    pytest_bazel.main()
