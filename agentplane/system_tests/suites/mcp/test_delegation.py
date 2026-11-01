"""External same-account delegation when the integration app is unavailable."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_bazel
import yaml
from google.protobuf.json_format import ParseDict
from more_itertools import one

from agentplane.app.testing.client import Client
from agentplane.app.testing.models import NewSandbox, SandboxView
from agentplane.mcp_edge import connection_service_pb2
from agentplane.policy_sets import POLICY_BINDINGS_PLURAL
from agentplane.proto import session_pb2
from agentplane.system_tests.cluster import Cluster
from agentplane.system_tests.k3s.stack import Stack
from agentplane.system_tests.mcp_connection import ExternalMCPSession, connected_session
from agentplane.system_tests.operator_login import OperatorCredentials
from agentplane.system_tests.port_forward import PortForward, async_port_forward
from agentplane.system_tests.sandbox import POD_TEMPLATE, suspend_and_delete, wait_until_running
from agentplane.system_tests.scenarios import mcp
from agentplane.system_tests.scripted_upstream import ScriptedUpstream
from agentplane.testing.scripted_model import Text
from util.testing.undeclared_outputs import undeclared_outputs_dir

_REQUIRED_TOOLS = {
    "environments__list",
    "environments__get",
    "sessions__open",
    "sessions__submit_input",
    "sessions__observe_input",
    "sessions__get",
    "sessions__read_turns",
    "sessions__cancel_input",
    "sessions__interrupt_turn",
}


async def _tool(session: ExternalMCPSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async with asyncio.timeout(300):
        result = await session.call_tool(name, arguments=arguments)
        while True:
            assert not result.is_error, name
            content = result.structured_content
            assert isinstance(content, dict), name
            request_id, state = content.get("request_id"), content.get("state")
            if not isinstance(request_id, str) or not isinstance(state, str):
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


async def _wait_basic_policy(cluster: Cluster, view: SandboxView, policy_objects: mcp.PolicyObjects) -> None:
    inventory = json.loads(await cluster.kubectl("get", POLICY_BINDINGS_PLURAL, "-o", "json"))
    subject = view.service_account
    bindings = [
        binding
        for binding in inventory["items"]
        if binding.get("spec", {}).get("subject", {}).get("namespace") == subject.namespace
        and binding.get("spec", {}).get("subject", {}).get("name") == subject.name
        and "basic" in binding.get("spec", {}).get("policySets", [])
    ]
    if not bindings:
        name = await policy_objects.create_binding(f"{view.name}-delegation-basic", sandbox=view, policy_sets=["basic"])
        await policy_objects.wait_actions_ready(name)
        return
    for binding in bindings:
        await policy_objects.wait_actions_ready(binding["metadata"]["name"])


async def _cleanup_sandbox(cluster: Cluster, token: str, name: str, uid: UUID) -> None:
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


async def _capture_submit_boundary(
    cluster: Cluster,
    *,
    pod_name: str,
    session_id: str,
    command_id: str,
    target: dict[str, Any],
    configuration: dict[str, Any],
    submit_result: dict[str, Any],
    observation: dict[str, Any] | None,
    model_request_count: int,
) -> dict[str, Any]:
    """Capture safe route, pod and proxy evidence when the scripted model gets no request."""
    evidence: dict[str, Any] = {
        "boundary": "submit_input returned, but ScriptedUpstream received no model request",
        "target": target,
        "session_id": session_id,
        "command_id": command_id,
        "selected_configuration": {
            key: configuration.get(key) for key in ("harness", "model", "reasoning_efforts") if key in configuration
        },
        "submit_result_keys": sorted(submit_result),
        "submit_dispatch": {
            key: submit_result["dispatch"][key]
            for key in ("state", "session_id", "command_id", "turn_id", "request_id", "status", "kind")
            if isinstance(submit_result.get("dispatch"), dict) and key in submit_result["dispatch"]
        },
        "observation": observation,
        "scripted_model_wait_calls": model_request_count,
        "capture_errors": [],
    }

    try:
        pod = json.loads(await cluster.kubectl("get", "pod", pod_name, "-o", "json", timeout_s=15))
        status = pod.get("status", {})
        evidence["sandbox_pod"] = {
            "name": pod.get("metadata", {}).get("name"),
            "uid": pod.get("metadata", {}).get("uid"),
            "phase": status.get("phase"),
            "container_statuses": [
                {
                    "name": container.get("name"),
                    "ready": container.get("ready"),
                    "restart_count": container.get("restartCount"),
                    "state": next(iter(container.get("state", {})), "unknown"),
                }
                for container in status.get("containerStatuses", [])
            ],
        }

        def config_map_name(volume_name: str) -> str | None:
            for volume in pod.get("spec", {}).get("volumes", []):
                if volume.get("name") == volume_name:
                    config_map = volume.get("configMap")
                    if isinstance(config_map, dict):
                        name = config_map.get("name")
                        if isinstance(name, str):
                            return name
            return None

        runtime_map_name = config_map_name("environment-runtime-config")
        sidecar_map_name = config_map_name("egress-sidecar-settings")
        routes: dict[str, Any] = {}
        if runtime_map_name is not None:
            runtime_map = json.loads(
                await cluster.kubectl("get", "configmap", runtime_map_name, "-o", "json", timeout_s=15)
            )
            runtime = json.loads(runtime_map["data"]["environment-runtime.json"])
            codex = runtime.get("codex", {})
            routes["runtime_codex_base_url"] = codex.get("base_url")
            routes["runtime_codex_token_env_name"] = codex.get("token_env")
        else:
            evidence["capture_errors"].append("runtime_config_map_not_mounted")

        if sidecar_map_name is not None:
            sidecar_map = json.loads(
                await cluster.kubectl("get", "configmap", sidecar_map_name, "-o", "json", timeout_s=15)
            )
            sidecar = yaml.safe_load(sidecar_map["data"]["settings.yaml"])
            routes["egress_proxy_host"] = sidecar.get("proxy_host")
            routes["egress_proxy_port"] = sidecar.get("proxy_port")
        else:
            evidence["capture_errors"].append("egress_sidecar_config_map_not_mounted")

        ingress_settings_json = await cluster.kubectl(
            "get", "configmap", "llm-ingress-settings", "-o", "json", timeout_s=15
        )
        ingress_settings_map = json.loads(ingress_settings_json)
        ingress_settings = yaml.safe_load(ingress_settings_map["data"]["settings.yaml"])
        ingress_deployment = json.loads(
            await cluster.kubectl("get", "deployment", "llm-ingress", "-o", "json", timeout_s=15)
        )
        ingress_container = next(
            item for item in ingress_deployment["spec"]["template"]["spec"]["containers"] if item["name"] == "ingress"
        )
        ingress_url = next(
            (
                item.get("value")
                for item in ingress_container.get("env", [])
                if item.get("name") == "AGENTPLANE_LLM_INGRESS_LITELLM_URL"
            ),
            None,
        )
        routes["llm_ingress_upstream_url"] = ingress_url
        routes["llm_ingress_log_llm_requests"] = ingress_settings.get("log_llm_requests")
        routes["llm_ingress_model_ids"] = [item.get("model") for item in ingress_settings.get("models", [])]
        evidence["configured_routes"] = routes

    except Exception as error:
        evidence["capture_errors"].append(f"route_or_pod_snapshot:{type(error).__name__}")

    async def filtered_logs(*args: str) -> list[str] | str:
        try:
            output = await cluster.kubectl("logs", *args, "--since=2m", "--tail=200", timeout_s=15)
        except Exception as error:
            return f"unavailable:{type(error).__name__}"
        lines = [
            line
            for line in output.splitlines()
            if re.search(r"(?i)(error|warn|refus|unavailable|failed|proxy|forwarding)", line)
        ]
        return lines[-100:]

    logs: dict[str, list[str] | str] = {}
    for name, args in {
        "environment_runtime": (pod_name, "-c", "environment-runtime"),
        "egress_sidecar": (pod_name, "-c", "egress-sidecar"),
        "central_egress_proxy": ("deployment/egress", "-c", "proxy"),
        "llm_ingress": ("deployment/llm-ingress", "-c", "ingress"),
    }.items():
        logs[name] = await filtered_logs(*args)
    evidence["relevant_logs"] = logs
    return evidence


async def _capture_egress_decisions(cluster: Cluster, token: str, name: str) -> list[dict[str, Any]]:
    """Read the proxy's name-only decision snapshot once the integration app is restored."""
    service = json.loads(await cluster.kubectl("get", "service", "app", "-o", "json", timeout_s=15))
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
        Client(base_url=f"http://{endpoint}", token=token) as diagnostics_client,
    ):
        async with asyncio.timeout(20):
            decisions = await diagnostics_client.decisions(name)
    return [decision.model_dump(mode="json") for decision in decisions]


@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_external_connection_delegates_without_the_integration_app(
    pytestconfig: pytest.Config,
    deployment: Stack,
    operator_bff: httpx.AsyncClient,
    operator_credentials: OperatorCredentials,
    client: Client,
    scripted: ScriptedUpstream,
    policy_objects: mcp.PolicyObjects,
) -> None:
    if pytestconfig.option.showlocals:
        pytest.fail("Disable pytest local-variable dumps before external OAuth", pytrace=False)
    assert deployment.mcp_fixtures is not None

    cluster = deployment.cluster
    name = "systest-delegation-caller"
    created = await client.create_sandbox(NewSandbox(name=name, template=POD_TEMPLATE))
    app_needs_restore = False
    boundary_evidence: dict[str, Any] | None = None
    try:
        view = await wait_until_running(client, created.name, expected_uid=created.uid)
        assert view.pod is not None
        await _wait_basic_policy(cluster, view, policy_objects)

        response = await operator_bff.get("/upstream/connections/v1/operator/caller-service-accounts")
        assert response.status_code == 200
        inventory = ParseDict(response.json(), connection_service_pb2.ListCallerServiceAccountsResponse())
        account = one(
            service_account
            for service_account in inventory.service_accounts
            if service_account.namespace == view.service_account.namespace
            and service_account.name == view.service_account.name
        )

        async with connected_session(
            server=deployment.mcp_fixtures.mcp_edge_url,
            operator=operator_bff,
            credentials=operator_credentials,
            account=account,
            ca_bundle=deployment.mcp_fixtures.ca_bundle,
        ) as session:
            # OAuth setup and operator consent are complete. All calls below use only the external MCP
            # Connection's token, while app remains scaled to zero.
            app_needs_restore = True
            try:
                await cluster.kubectl("scale", "deployment/app", "--replicas=0")
                await _wait_app(cluster, replicas=0)
                catalog = await session.list_tools()
                assert {tool.name for tool in catalog.tools} >= _REQUIRED_TOOLS

                page = await _tool(session, "environments__list", {"limit": 10})
                environments = page.get("items")
                assert isinstance(environments, list)
                descriptor = one(
                    item
                    for item in environments
                    if isinstance(item, dict)
                    and item.get("target", {}).get("name") == view.name
                    and item.get("target", {}).get("uid") == str(view.uid)
                )
                target = descriptor["target"]
                assert isinstance(target, dict)

                current = await _tool(session, "environments__get", {"target": target})
                execution_account = current.get("execution_account")
                assert isinstance(execution_account, dict)
                assert execution_account["namespace"] == account.namespace
                assert execution_account["name"] == account.name
                assert execution_account["uid"]
                assert current["readiness"]["state"] == "READINESS_STATE_READY"
                workspace = current["workspace"]
                assert isinstance(workspace, dict)
                assert workspace["availability"] == "AVAILABILITY_CONFIGURED"
                configurations = current.get("configurations")
                assert isinstance(configurations, list)
                configuration = one(
                    choice
                    for choice in configurations
                    if isinstance(choice, dict) and choice.get("harness") == "HARNESS_CODEX"
                )
                available_efforts = configuration.get("reasoning_efforts")
                assert isinstance(available_efforts, list)
                reasoning_effort: str | None = next(
                    (effort for effort in available_efforts if isinstance(effort, str) and effort), None
                )
                assert reasoning_effort, "the advertised Codex configuration has no nonempty effort"
                cwd = f"{workspace['root'].rstrip('/')}/external-delegation-{uuid4()}"
                session_id = str(uuid4())
                guidance_marker = f"EXTERNAL-SESSION-GUIDANCE-{uuid4()}"
                opening = await _tool(
                    session,
                    "sessions__open",
                    {
                        "target": target,
                        "session_id": session_id,
                        "spec": {
                            "harness": configuration["harness"],
                            "model": configuration["model"],
                            "cwd": cwd,
                            "reasoning_effort": reasoning_effort,
                        },
                        "additional_instructions": guidance_marker,
                    },
                )
                assert isinstance(opening.get("opening"), dict)

                model = scripted.model(view.pod.name, session_pb2.HARNESS_CODEX)
                first_command = str(uuid4())
                first_input = f"Report the exact marker {uuid4()} and wait for follow-up."
                submitted = await _tool(
                    session,
                    "sessions__submit_input",
                    {"target": target, "session_id": session_id, "command_id": first_command, "text": first_input},
                )
                assert isinstance(submitted.get("dispatch"), dict)
                preflight = await _tool(
                    session,
                    "sessions__observe_input",
                    {"session_id": session_id, "command_id": first_command, "wait_seconds": 5},
                )
                preflight_observation = preflight.get("observation")
                assert isinstance(preflight_observation, dict)
                if preflight_observation.get("kind") in {"command_failed", "command_no_op"}:
                    boundary_evidence = await _capture_submit_boundary(
                        cluster,
                        pod_name=view.pod.name,
                        session_id=session_id,
                        command_id=first_command,
                        target=target,
                        configuration=configuration,
                        submit_result=submitted,
                        observation=preflight_observation,
                        model_request_count=0,
                    )
                    (undeclared_outputs_dir() / "delegation-submit-boundary.json").write_text(
                        json.dumps(boundary_evidence, indent=2) + "\n"
                    )
                    raise AssertionError(
                        "submitted input failed before model response; see test.outputs/delegation-submit-boundary.json"
                    )
                try:
                    async with asyncio.timeout(45):
                        first_request = await model.request()
                except TimeoutError as error:
                    boundary_evidence = await _capture_submit_boundary(
                        cluster,
                        pod_name=view.pod.name,
                        session_id=session_id,
                        command_id=first_command,
                        target=target,
                        configuration=configuration,
                        submit_result=submitted,
                        observation=preflight_observation,
                        model_request_count=model.request_count,
                    )
                    (undeclared_outputs_dir() / "delegation-submit-boundary.json").write_text(
                        json.dumps(boundary_evidence, indent=2) + "\n"
                    )
                    raise AssertionError(
                        "ScriptedUpstream received no Codex request within the bounded diagnostic wait; "
                        "see test.outputs/delegation-submit-boundary.json"
                    ) from error
                assert first_input in first_request.user_texts
                assert guidance_marker in first_request.system_text
                assert "You run inside an Agentplane Sandbox" in first_request.system_text
                first_answer = f"First retained answer {uuid4()}"
                await model.reply(first_request, Text(first_answer))
                first_observation = await _tool(
                    session,
                    "sessions__observe_input",
                    {"session_id": session_id, "command_id": first_command, "wait_seconds": 30},
                )
                assert first_observation["observation"]["kind"] == "turn_completed"
                assert first_observation["observation"]["turn"]["final_assistant_message"] == first_answer

                second_command = str(uuid4())
                second_input = f"Now report the second marker {uuid4()}."
                await _tool(
                    session,
                    "sessions__submit_input",
                    {"target": target, "session_id": session_id, "command_id": second_command, "text": second_input},
                )
                second_request = await model.request()
                assert second_input in second_request.user_texts
                second_answer = f"Follow-up retained answer {uuid4()}"
                await model.reply(second_request, Text(second_answer))
                second_observation = await _tool(
                    session,
                    "sessions__observe_input",
                    {"session_id": session_id, "command_id": second_command, "wait_seconds": 30},
                )
                assert second_observation["observation"]["kind"] == "turn_completed"
                assert second_observation["observation"]["turn"]["final_assistant_message"] == second_answer

                snapshot = await _tool(session, "sessions__get", {"session_id": session_id})
                assert snapshot["session_id"] == session_id
                assert snapshot["opening"]["kind"] == "observed"
                assert snapshot["opening"]["value"]["sessionId"] == session_id
                assert snapshot["history"]["kind"] == "observed"
                assert snapshot["history"]["value"]["sessionId"] == session_id

                first_page = await _tool(session, "sessions__read_turns", {"session_id": session_id, "count": 1})
                assert first_page["session_id"] == session_id
                assert first_page["source"] == "history"
                assert not first_page.get("error")
                assert len(first_page["turns"]) == 1
                assert first_page["turns"][0]["input"][0]["text"] == first_input
                assert first_page["turns"][0]["status"] == "TURN_STATUS_COMPLETED"
                assert first_page["turns"][0]["final_assistant_message"] == first_answer
                assert first_page["next_cursor"]

                second_page = await _tool(
                    session,
                    "sessions__read_turns",
                    {"session_id": session_id, "count": 1, "page_cursor": first_page["next_cursor"]},
                )
                assert len(second_page["turns"]) == 1
                assert second_page["turns"][0]["input"][0]["text"] == second_input
                assert second_page["turns"][0]["status"] == "TURN_STATUS_COMPLETED"
                assert second_page["turns"][0]["final_assistant_message"] == second_answer
                assert not second_page.get("next_cursor")
            finally:
                await cluster.kubectl("scale", "deployment/app", "--replicas=1")
                await cluster.kubectl("wait", "--for=condition=Available", "deployment/app", "--timeout=180s")
                await _wait_app(cluster, replicas=1)
                if boundary_evidence is not None:
                    try:
                        boundary_evidence["egress_decisions_after_app_restore"] = await _capture_egress_decisions(
                            cluster, deployment.token, name
                        )
                    except Exception as error:
                        boundary_evidence["egress_decision_capture_error"] = type(error).__name__
                    output = undeclared_outputs_dir() / "delegation-submit-boundary.json"
                    output.write_text(json.dumps(boundary_evidence, indent=2) + "\n")
                app_needs_restore = False
    finally:
        if app_needs_restore:
            await cluster.kubectl("scale", "deployment/app", "--replicas=1")
            await cluster.kubectl("wait", "--for=condition=Available", "deployment/app", "--timeout=180s")
            await _wait_app(cluster, replicas=1)
        await _cleanup_sandbox(cluster, deployment.token, created.name, created.uid)


if __name__ == "__main__":
    pytest_bazel.main()
