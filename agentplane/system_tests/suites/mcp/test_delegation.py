"""External ServiceAccount delegation stays isolated from Sandbox Identities with the app down."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

import grpc
import httpx
import pytest
import pytest_bazel
from google.protobuf.json_format import ParseDict
from more_itertools import one

from agentplane.app.testing.client import Client
from agentplane.environments.v1 import environment_service_pb2
from agentplane.mcp_edge import connection_service_pb2
from agentplane.proto import identity_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.deployment.stack import Stack
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.identity import (
    LAUNCH_DEFAULT_POLICY_SETS,
    create_scenario_identity,
    delete_scenario_identity,
)
from agentplane.system_tests.driver.mcp_connection import ExternalMCPSession, connected_session
from agentplane.system_tests.driver.operator_login import OperatorCredentials
from agentplane.system_tests.driver.port_forward import PortForward, async_port_forward
from agentplane.system_tests.driver.sandbox import POD_TEMPLATE, suspend_and_delete, wait_until_running

_REQUIRED_TOOLS = {
    "environments__list",
    "environments__get",
    "sessions__create",
    "sessions__submit_input",
    "sessions__observe_input",
    "sessions__get",
    "sessions__read_turns",
    "sessions__cancel_input",
    "sessions__interrupt_turn",
}


async def _tool(
    session: ExternalMCPSession, name: str, arguments: dict[str, Any], *, error_kind: str | None = None
) -> dict[str, Any]:
    async with asyncio.timeout(300):
        result = await session.call_tool(name, arguments=arguments)
        while True:
            content = result.structured_content
            assert isinstance(content, dict), name
            if result.is_error:
                assert error_kind is not None, content
                assert content["state"] == "failed", content
                assert content["error"]["kind"] == error_kind, content
                return content
            request_id, state = content.get("request_id"), content.get("state")
            if not isinstance(request_id, str) or not isinstance(state, str):
                assert error_kind is None, content
                return content
            # A pending Action receipt is not the tool result. Reconcile only this exact request;
            # never replay the mutation when its initial response is pending or unknown.
            result = await session.call_tool(
                "get_action_result",
                arguments={"request_id": request_id, "wait": {"wait_seconds": 30, "wait_until": "terminal"}},
            )


async def _wait_app(cluster: Cluster, *, replicas: int) -> None:
    async with asyncio.timeout(150):
        while True:
            deployment = json.loads(await cluster.kubectl("get", "deployment", "app", "-o", "json"))
            selector = deployment["spec"]["selector"]["matchLabels"]
            label_selector = ",".join(f"{key}={value}" for key, value in selector.items())
            pods = json.loads(await cluster.kubectl("get", "pods", "-l", label_selector, "-o", "json"))["items"]
            status = deployment.get("status", {})
            if replicas == 0 and not pods and (status.get("readyReplicas") or 0) == 0:
                return
            if replicas > 0 and status.get("availableReplicas", 0) >= replicas:
                return
            await asyncio.sleep(0.5)


async def _cleanup_sandbox(cluster: Cluster, token: str, name: str, uid: str) -> None:
    service = json.loads(await cluster.kubectl("get", "service", "app", "-o", "json"))
    remote_port = service["spec"]["ports"][0]["port"]
    forward = PortForward(
        cluster.command("port-forward"),
        resource="service/app",
        local_address="127.0.0.1",
        local_port=None,
        remote_port=remote_port,
    )
    async with (
        async_port_forward(forward) as endpoint,
        Client(base_url=f"http://{endpoint}", token=token) as cleanup_client,
    ):
        await suspend_and_delete(cluster, cleanup_client, name, uid)


@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_external_connection_cannot_control_an_identity_sandbox(
    pytestconfig: pytest.Config,
    deployment: Stack,
    operator_bff: httpx.AsyncClient,
    operator_credentials: OperatorCredentials,
    client: Client,
    dispatcher: SessionDispatcherClient,
) -> None:
    """An external ServiceAccount Connection cannot discover or create Sessions for another Identity."""
    if pytestconfig.option.showlocals:
        pytest.fail("Disable pytest local-variable dumps before external OAuth", pytrace=False)
    assert deployment.mcp_fixtures is not None

    cluster = deployment.cluster
    name = "systest-delegation-identity"
    identity = await create_scenario_identity(cluster, name, list(LAUNCH_DEFAULT_POLICY_SETS))
    created: identity_pb2.EnvironmentTarget | None = None
    app_needs_restore = False
    try:
        request = environment_service_pb2.CreateEnvironmentRequest(
            name=name, template=POD_TEMPLATE, identity=identity["name"]
        )
        try:
            created = await client.create_sandbox(request)
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            if 400 <= status < 500 and status not in (408, 429):
                await delete_scenario_identity(cluster, identity)
            raise
        view = await wait_until_running(client, created)
        assert view.kubernetes_diagnostics.HasField("pod")
        assert view.identity_name == identity["name"]

        response = await operator_bff.get("/upstream/connections/v1/operator/caller-service-accounts")
        assert response.status_code == 200
        inventory = ParseDict(response.json(), connection_service_pb2.ListCallerServiceAccountsResponse())
        account = one(
            service_account
            for service_account in inventory.service_accounts
            if service_account.namespace == cluster.namespace and service_account.name == "mcp-edge-test-caller"
        )
        async with connected_session(
            server=deployment.mcp_fixtures.mcp_edge_url,
            operator=operator_bff,
            credentials=operator_credentials,
            account=account,
            ca_bundle=deployment.mcp_fixtures.ca_bundle,
        ) as session:
            # OAuth setup and operator consent are complete. Calls below use only the standing
            # ServiceAccount Connection while the integration app is scaled to zero.
            app_needs_restore = True
            await cluster.kubectl("scale", "deployment/app", "--replicas=0")
            await _wait_app(cluster, replicas=0)
            catalog = await session.list_tools()
            assert {tool.name for tool in catalog.tools} >= _REQUIRED_TOOLS

            page = await _tool(session, "environments__list", {"limit": 10})
            environments = page.get("items")
            assert environments == [], environments

            target = {"namespace": created.namespace, "name": created.name, "uid": created.uid}
            await _tool(
                session, "environments__get", {"target": target}, error_kind="EXECUTION_ERROR_KIND_TARGET_NOT_FOUND"
            )

            configuration = one(
                choice for choice in view.configuration.configurations if choice.harness == "HARNESS_CODEX"
            )
            assert "low" in configuration.reasoning_efforts
            session_id = str(uuid4())
            await _tool(
                session,
                "sessions__create",
                {
                    "target": target,
                    "session_id": session_id,
                    "spec": {
                        "harness": configuration.harness,
                        "model": configuration.model,
                        "cwd": f"{view.configuration.workspace.root.rstrip('/')}/external-delegation-{uuid4()}",
                        "reasoning_effort": "low",
                    },
                },
                error_kind="EXECUTION_ERROR_KIND_BACKEND_REFUSED",
            )
            # The error above is a definite Dispatcher refusal. Independently confirm that its
            # caller-minted ID did not leave a Session behind while the app remains unavailable.
            with pytest.raises(grpc.aio.AioRpcError) as missing:
                await dispatcher.get_session(session_id)
            assert missing.value.code() == grpc.StatusCode.NOT_FOUND

            await cluster.kubectl("scale", "deployment/app", "--replicas=1")
            await cluster.kubectl("wait", "--for=condition=Available", "deployment/app", "--timeout=180s")
            await _wait_app(cluster, replicas=1)
            app_needs_restore = False
    finally:
        if app_needs_restore:
            await cluster.kubectl("scale", "deployment/app", "--replicas=1")
            await cluster.kubectl("wait", "--for=condition=Available", "deployment/app", "--timeout=180s")
            await _wait_app(cluster, replicas=1)
        if created is not None:
            await _cleanup_sandbox(cluster, deployment.token, created.name, created.uid)
            await delete_scenario_identity(cluster, identity)


if __name__ == "__main__":
    pytest_bazel.main()
