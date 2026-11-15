"""Agents submit Actions with the `agentplane` CLI: MCP-backed ones, and the orchestration_v1 group's
launches of Sandboxes they then drive. An operator links an OAuth-protected MCP server through Dex and
decides what waits for them. The model is scripted: each turn runs one CLI command, whose receipt the
scenario reads back from what the harness sent the model. The k3s target runs Dex, the MCP servers and
the Action groups (`mcp_servers.py`)."""

from __future__ import annotations

import json
from http import HTTPStatus
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from google.protobuf import json_format
from more_itertools import one
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.action_service.api.contracts import action_service_pb2, operator_action_service_pb2
from agentplane.action_service.policies.resources import ACTION_RULE_TYPE
from agentplane.app.testing.client import Client
from agentplane.mcp_edge.api.connection import connection_service_pb2
from agentplane.net.peertls.config import MCP_EDGE_URI
from agentplane.policy_sets import POLICY_BINDINGS_PLURAL, POLICY_SETS_PLURAL
from agentplane.proto import identity_pb2, session_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.deployment import mcp_servers
from agentplane.system_tests.driver.action_requests import (
    action_request,
    assert_echoed_result,
    decide_pending_action,
    decision_response,
)
from agentplane.system_tests.driver.agent import RUNNER_ANSWERS_SECONDS, TURN_SECONDS, WORKING_DIRECTORY
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.identity import LAUNCH_DEFAULT_POLICY_SETS
from agentplane.system_tests.driver.mcp_connection import connected_session
from agentplane.system_tests.driver.operator_login import (
    LoginBlockedError,
    OperatorCredentials,
    follow_dex_authorization,
)
from agentplane.system_tests.driver.policy_objects import POLICY_READY_SECONDS, PolicyBindingPresentError, PolicyObjects
from agentplane.system_tests.driver.sandbox import POD_TEMPLATE, Sandboxes, suspend_and_delete, wait_until_running
from agentplane.system_tests.driver.scripted_agent import ScriptedAgent, play_cli_turn, submission
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.driver.tracing import tracer

# The key of the orchestration_v1 group in every deployment that offers it (cluster/cdk8s/agentplane/actions.py).
ORCHESTRATION_GROUP = "orchestration_v1"


async def _submit_echo(
    agent: ScriptedAgent, group: str, message: str, *, wait: bool
) -> action_service_pb2.ActionRequest:
    return await agent.submit(group, mcp_servers.ECHO, {"message": message}, title="Echo a test marker", wait=wait)


async def external_mcp_connection_uses_its_consented_identity(
    operator_bff: httpx.AsyncClient, operator_credentials: OperatorCredentials, *, server: str, ca_bundle: Path
) -> None:
    """Use an external OAuth grant to call Actions as the Identity approved in the App."""
    response = await operator_bff.get("/upstream/connections/v1/operator/connection-identities")
    assert response.status_code == HTTPStatus.OK
    inventory = json_format.ParseDict(response.json(), connection_service_pb2.ListConnectionIdentitiesResponse())
    identity = one(candidate for candidate in inventory.identities if candidate.name == "mcp-edge-test-caller")
    response = await operator_bff.get("/upstream/connections/v1/operator/connections")
    assert response.status_code == HTTPStatus.OK
    connections_before = json_format.ParseDict(response.json(), connection_service_pb2.ListConnectionsResponse())
    connection_ids_before = {connection.id for connection in connections_before.connections}
    async with connected_session(
        server=server, operator=operator_bff, credentials=operator_credentials, identity=identity, ca_bundle=ca_bundle
    ) as session:
        catalog = await session.list_tools()
        assert "get_action_policy" in {tool.name for tool in catalog.tools}
        policy = await session.call_tool("get_action_policy", arguments={"include_fields": ["subject"]})
        assert not policy.is_error
        assert policy.structured_content == {"subject": {"name": identity.name, "uid": identity.uid}}

        response = await operator_bff.get("/upstream/connections/v1/operator/connections")
        assert response.status_code == HTTPStatus.OK
        connections_after = json_format.ParseDict(response.json(), connection_service_pb2.ListConnectionsResponse())
        connection = one(
            candidate for candidate in connections_after.connections if candidate.id not in connection_ids_before
        )
        assert connection.bound_identity == identity
        grant = one(
            candidate
            for candidate in connection.grants
            if candidate.status == connection_service_pb2.GRANT_STATUS_ACTIVE
        )

        action_marker = f"external-MCP-{connection.id}"
        submitted = await session.call_tool(
            "request_action",
            arguments={
                "request": {
                    "idempotency_key": f"system-test-{connection.id}",
                    "action": {"group": mcp_servers.EVERYTHING_GROUP, "name": mcp_servers.ECHO},
                    "arguments": {"message": action_marker},
                    "title": "Record an external MCP receipt",
                },
                "respond_with": "receipt",
                "include_fields": ["id"],
            },
        )
        assert not submitted.is_error
        assert isinstance(submitted.structured_content, dict)
        request_id = submitted.structured_content.get("id")
        assert isinstance(request_id, str)

        # Deny the pending request before inspecting its retained receipt.
        await decide_pending_action(operator_bff, request_id, action_service_pb2.VERDICT_DENY)
        receipt_response = await operator_bff.get(f"/upstream/actions/v1/action-requests/{request_id}")
        assert receipt_response.status_code == HTTPStatus.OK
        receipt = action_request(receipt_response)
        assert receipt.state == action_service_pb2.ACTION_STATE_DENIED
        assert receipt.action.group == mcp_servers.EVERYTHING_GROUP
        assert receipt.action.name == mcp_servers.ECHO
        assert receipt.arguments.fields["message"].string_value == action_marker
        assert receipt.HasField("caller_identity")
        assert not receipt.HasField("caller")
        assert receipt.caller_identity == identity
        assert receipt.delegating_actor.WhichOneof("actor") == "service_spiffe_id"
        assert receipt.delegating_actor.service_spiffe_id == MCP_EDGE_URI
        assert receipt.WhichOneof("provenance") == "connection_provenance"
        assert receipt.connection_provenance.connection_id == connection.id
        assert receipt.connection_provenance.grant_id == grant.id


async def agent_executes_mcp_action(
    client: Client,
    dispatcher: SessionDispatcherClient,
    sandbox: Sandboxes,
    harness: session_pb2.Harness,
    offered_model: str,
    scripted: ScriptedUpstream,
    policy_objects: PolicyObjects,
    sandbox_prefix: str,
) -> None:
    view = await sandbox(f"{sandbox_prefix}-mcp-{harness}")
    await policy_objects.bind(
        view, "exact-echo", [{"type": "exact_actions", "actions": {mcp_servers.EVERYTHING_GROUP: [mcp_servers.ECHO]}}]
    )
    agent = await ScriptedAgent.create(
        client, dispatcher, view, harness=harness, model=offered_model, upstream=scripted
    )
    marker = f"MCP0-{uuid4()}"
    receipt = await _submit_echo(agent, mcp_servers.EVERYTHING_GROUP, marker, wait=True)
    assert receipt.state == action_service_pb2.ACTION_STATE_SUCCEEDED
    assert receipt.HasField("execution")
    assert_echoed_result(receipt.execution.result, marker)


async def operator_links_oauth_mcp_server(
    operator_bff: httpx.AsyncClient,
    operator_credentials: OperatorCredentials,
    client: Client,
    dispatcher: SessionDispatcherClient,
    sandbox: Sandboxes,
    harness: session_pb2.Harness,
    offered_model: str,
    scripted: ScriptedUpstream,
    policy_objects: PolicyObjects,
    *,
    server_id: str,
) -> None:
    """Links the OAuth-protected MCP server through Dex and executes its protected tool."""
    start_response = await operator_bff.post(
        f"/upstream/actions/v1/operator/mcp-servers/{server_id}/linkage/start", json={"scopes": []}
    )
    assert start_response.status_code == HTTPStatus.OK, start_response.text
    authorization_url = start_response.json()["authorizationUrl"]

    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=30) as dex_browser:
            state, code = await follow_dex_authorization(
                authorization_url,
                dex_browser,
                operator_credentials,
                callback_origin=operator_bff.base_url,
                callback_path="/mcp-linkage/callback",
            )
    except LoginBlockedError as exc:
        pytest.fail(str(exc), pytrace=False)

    callback = await operator_bff.get("/mcp-linkage/callback", params={"state": state, "code": code})
    assert callback.status_code == HTTPStatus.SEE_OTHER, callback.text

    view = await sandbox(f"systest-oauth-mcp-{harness}")
    await policy_objects.bind(
        view, "oauth-echo", [{"type": "exact_actions", "actions": {server_id: [mcp_servers.ECHO]}}]
    )
    agent = await ScriptedAgent.create(
        client, dispatcher, view, harness=harness, model=offered_model, upstream=scripted
    )
    marker = f"MCP-OAUTH-{uuid4()}"
    receipt = await _submit_echo(agent, server_id, marker, wait=True)
    assert receipt.state == action_service_pb2.ACTION_STATE_SUCCEEDED
    assert receipt.HasField("execution")
    assert_echoed_result(receipt.execution.result, marker)


async def agent_mcp_bff_decision(
    operator_bff: httpx.AsyncClient,
    operator_credentials: OperatorCredentials,
    client: Client,
    dispatcher: SessionDispatcherClient,
    sandbox: Sandboxes,
    harness: session_pb2.Harness,
    offered_model: str,
    scripted: ScriptedUpstream,
    sandbox_prefix: str,
) -> None:
    view = await sandbox(f"{sandbox_prefix}-mcp-{harness}")
    agent = await ScriptedAgent.create(
        client, dispatcher, view, harness=harness, model=offered_model, upstream=scripted
    )
    # Denial runs first on a fresh session; the next request must still wait for its own decision.
    for verdict in (action_service_pb2.VERDICT_DENY, action_service_pb2.VERDICT_ALLOW):
        # Nothing binds this Sandbox to a policy set, so every Action of its waits for the operator.
        marker = f"MCP-BFF-{uuid4()}-" + "x" * 201
        submitted = await _submit_echo(agent, mcp_servers.EVERYTHING_GROUP, marker, wait=False)
        assert submitted.state == action_service_pb2.ACTION_STATE_DECISION_PENDING
        request_id = submitted.id
        path = f"/upstream/actions/v1/action-requests/{request_id}"
        decision_path = f"/upstream/actions/v1/operator/action-requests/{request_id}/decision"
        pending_response = await operator_bff.get(path)
        assert pending_response.status_code == HTTPStatus.OK
        pending = action_request(pending_response)
        assert pending.id == request_id
        assert pending.idempotency_key
        assert (pending.action.group, pending.action.name) == (mcp_servers.EVERYTHING_GROUP, mcp_servers.ECHO)
        assert json_format.MessageToDict(pending.arguments) == {"message": marker}
        assert pending.state == action_service_pb2.ACTION_STATE_DECISION_PENDING
        assert pending.version == 1
        if pending.HasField("decision"):
            pytest.fail("Pending request already has a Decision", pytrace=False)
        assert not pending.HasField("execution")

        decision = operator_action_service_pb2.DecideActionRequestRequest(
            request_id=str(request_id), verdict=verdict, expected_version=pending.version, idempotency_key=str(uuid4())
        )
        decided_response = await operator_bff.post(decision_path, json=json_format.MessageToDict(decision))
        assert decided_response.status_code == HTTPStatus.OK
        decided = decision_response(decided_response)
        assert decided.id == request_id
        assert json_format.MessageToDict(decided.arguments) == json_format.MessageToDict(pending.arguments)
        assert decided.version == pending.version + 1
        assert decided.HasField("decision")
        assert decided.decision.verdict == verdict
        operator = decided.decision.operator.operator
        if decided.decision.WhichOneof("decider") != "operator" or (
            operator.issuer != operator_credentials.issuer.get_secret_value()
            or operator.subject != operator_credentials.subject.get_secret_value()
        ):
            pytest.fail("Decision operator identity differs from the dedicated Secret", pytrace=False)
        assert decided.decision.idempotency_key == decision.idempotency_key
        expected_completed_state = (
            action_service_pb2.ACTION_STATE_SUCCEEDED
            if verdict == action_service_pb2.VERDICT_ALLOW
            else action_service_pb2.ACTION_STATE_DENIED
        )
        if verdict == action_service_pb2.VERDICT_ALLOW:
            assert decided.state == action_service_pb2.ACTION_STATE_ALLOWED
            assert decided.HasField("execution")
        else:
            assert decided.state == action_service_pb2.ACTION_STATE_DENIED
            assert not decided.HasField("execution")

        # What the agent learns of the operator's decision.
        completed = await agent.run("actions", "wait", str(request_id))
        assert completed.id == request_id
        assert completed.state == expected_completed_state
        terminal_response = await operator_bff.get(path)
        assert terminal_response.status_code == HTTPStatus.OK
        terminal = action_request(terminal_response)
        assert terminal.id == request_id
        assert terminal.idempotency_key == pending.idempotency_key
        assert terminal.action == pending.action
        assert terminal.arguments == pending.arguments
        assert terminal.caller == pending.caller
        if terminal.decision != decided.decision:
            pytest.fail("Terminal Decision changed", pytrace=False)
        assert terminal.state == expected_completed_state
        if verdict == action_service_pb2.VERDICT_ALLOW:
            assert terminal.HasField("execution")
            assert decided.HasField("execution")
            assert terminal.execution.id == decided.execution.id
            assert terminal.execution.state == action_service_pb2.EXECUTION_STATE_SUCCEEDED
            assert_echoed_result(terminal.execution.result, marker)
            assert not terminal.execution.HasField("error")
            assert completed.HasField("execution")
            assert completed.execution.state == action_service_pb2.EXECUTION_STATE_SUCCEEDED
            assert completed.execution.id == terminal.execution.id
            assert json_format.MessageToDict(completed.execution.result) == json_format.MessageToDict(
                terminal.execution.result
            )
            assert not completed.execution.HasField("error")
        else:
            assert not terminal.HasField("execution")
            assert not completed.HasField("execution")

        # Replay the original version/key after completion: same Decision and Execution, no new dispatch.
        duplicate = await operator_bff.post(decision_path, json=json_format.MessageToDict(decision))
        assert duplicate.status_code == HTTPStatus.OK
        if decision_response(duplicate) != terminal:
            pytest.fail("Duplicate decision changed the terminal request", pytrace=False)
        stale = operator_action_service_pb2.DecideActionRequestRequest()
        stale.CopyFrom(decision)
        stale.idempotency_key = str(uuid4())
        refused = await operator_bff.post(decision_path, json=json_format.MessageToDict(stale))
        assert refused.status_code == HTTPStatus.CONFLICT
        unchanged = await operator_bff.get(path)
        assert unchanged.status_code == HTTPStatus.OK
        if action_request(unchanged) != terminal:
            pytest.fail("Stale decision changed the terminal request", pytrace=False)


async def policy_binding_auto_approves_the_bound_sandbox(
    operator_bff: httpx.AsyncClient,
    client: Client,
    dispatcher: SessionDispatcherClient,
    sandbox: Sandboxes,
    harness: session_pb2.Harness,
    offered_model: str,
    scripted: ScriptedUpstream,
    policy_objects: PolicyObjects,
) -> None:
    """A set and a binding written through the Kubernetes API for the Sandbox this test launches:
    a matching echo is auto-approved and executed with the binding and set on its Decision, an
    argument miss waits for the operator, and once the Policy Controller has deleted the expired
    binding so does a match."""
    view = await sandbox(f"systest-policy-{harness}")
    set_name, binding_name = await policy_objects.bind(
        view,
        "echo",
        [
            {
                "type": "argument_schema",
                "actions": {mcp_servers.EVERYTHING_GROUP: [mcp_servers.ECHO]},
                "schema": {
                    "type": "object",
                    "required": ["message"],
                    "properties": {"message": {"type": "string", "maxLength": 200}},
                    "additionalProperties": False,
                },
            }
        ],
    )
    agent = await ScriptedAgent.create(
        client, dispatcher, view, harness=harness, model=offered_model, upstream=scripted
    )

    marker = f"MCP-POLICY-{uuid4()}"
    approved = await _submit_echo(agent, mcp_servers.EVERYTHING_GROUP, marker, wait=True)
    assert approved.state == action_service_pb2.ACTION_STATE_SUCCEEDED
    assert approved.HasField("execution")
    assert_echoed_result(approved.execution.result, marker)
    receipt = action_request(await operator_bff.get(f"/upstream/actions/v1/action-requests/{approved.id}"))
    assert receipt.state == action_service_pb2.ACTION_STATE_SUCCEEDED
    assert receipt.HasField("decision")
    assert receipt.decision.WhichOneof("decider") == "policy"
    assert receipt.decision.policy.HasField("evidence")
    evidence = receipt.decision.policy.evidence
    # Our Sandbox kind brings a PolicyBinding of its own, so the evidence may name more than ours.
    assert binding_name in {binding.name for binding in evidence.bindings}
    assert set_name in {policy_set.name for policy_set in evidence.policy_sets}
    assert evidence.HasField("matched")
    assert evidence.matched.namespace == policy_objects.cluster.namespace
    assert evidence.matched.policy_set == set_name
    assert evidence.matched.source == action_service_pb2.POLICY_SOURCE_RULES
    assert evidence.matched.index == 0
    assert evidence.matched.type == action_service_pb2.POLICY_KIND_ARGUMENT_SCHEMA
    assert receipt.HasField("execution")
    assert receipt.execution.state == action_service_pb2.EXECUTION_STATE_SUCCEEDED

    too_long = f"MCP-POLICY-{uuid4()}-" + "x" * 201
    missed = await _submit_echo(agent, mcp_servers.EVERYTHING_GROUP, too_long, wait=False)
    await decide_pending_action(operator_bff, missed.id, action_service_pb2.VERDICT_DENY)

    await policy_objects.expire_binding(binding_name)
    await _wait_unbound(agent, view.identity_name, binding_name)
    after_expiry = f"MCP-POLICY-{uuid4()}"
    expired = await _submit_echo(agent, mcp_servers.EVERYTHING_GROUP, after_expiry, wait=False)
    await decide_pending_action(operator_bff, expired.id, action_service_pb2.VERDICT_DENY)


async def _wait_unbound(agent: ScriptedAgent, identity_name: str, binding_name: str) -> None:
    """Until the Identity caller's own Action policy no longer includes `binding_name`."""
    with tracer.start_as_current_span("system_test.mcp.policy_unbound"):
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(POLICY_READY_SECONDS),
            wait=wait_fixed(1),
            retry=retry_if_exception_type(PolicyBindingPresentError),
            reraise=True,
        ):
            with attempt:
                policy = await agent.read_action_policy()
                assert policy.identity.name == identity_name
                if not policy.synced:
                    raise PolicyBindingPresentError(f"Identity {identity_name}: Action policy not synced")
                if binding_name in {effective.binding for effective in policy.auto_approve_if}:
                    raise PolicyBindingPresentError(f"Identity {identity_name} still resolves {binding_name}")


async def _launcher_approved(
    operator_bff: httpx.AsyncClient, receipt: action_service_pb2.ActionRequest, sandbox: str
) -> None:
    """The request succeeded on the launcher grant of `sandbox`, with no operator asked."""
    assert receipt.state == action_service_pb2.ACTION_STATE_SUCCEEDED, receipt
    decided = action_request(await operator_bff.get(f"/upstream/actions/v1/action-requests/{receipt.id}"))
    assert decided.decision.WhichOneof("decider") == "policy"
    assert decided.decision.policy.evidence.matched.policy_set == f"{sandbox}.launcher"


async def launcher_drives_only_the_sandbox_it_launched(
    cluster: Cluster,
    operator_bff: httpx.AsyncClient,
    client: Client,
    dispatcher: SessionDispatcherClient,
    sandbox: Sandboxes,
    harness: session_pb2.Harness,
    offered_model: str,
    scripted: ScriptedUpstream,
    policy_objects: PolicyObjects,
    sandbox_prefix: str,
) -> None:
    """An agent launches a Sandbox through orchestration_v1, which waits for the operator; the launch's
    grants then let that agent create a Session in it, send input, wait for its turn and read the outcome
    without asking again. The launched Sandbox's own Identity, another incarnation of the name and the
    deleted Sandbox still wait for the operator; a Session in a different Sandbox is refused."""
    launcher_view = await sandbox(f"{sandbox_prefix}-launcher-{harness}")
    launcher = await ScriptedAgent.create(
        client, dispatcher, launcher_view, harness=harness, model=offered_model, upstream=scripted
    )
    name = f"{sandbox_prefix}-launched-{harness}"
    launcher_grants = ["create_session", "submit_input", "get_session", "read_turns", "wait_for_turn"]
    launch = await launcher.submit(
        ORCHESTRATION_GROUP,
        "launch_sandbox",
        {
            "name": name,
            "template": POD_TEMPLATE,
            "policy_sets": list(LAUNCH_DEFAULT_POLICY_SETS),
            "launcher_grants": launcher_grants,
        },
        title="Launch a test Sandbox",
        wait=False,
    )
    await decide_pending_action(operator_bff, launch.id, action_service_pb2.VERDICT_ALLOW)
    launched = await launcher.run("actions", "wait", str(launch.id))
    assert launched.state == action_service_pb2.ACTION_STATE_SUCCEEDED, launched
    result = json_format.MessageToDict(launched.execution.result)
    target = result["sandbox"]
    assert result["launcher_grants"] == sorted(launcher_grants)
    assert target["name"] == name
    deleted = False
    try:
        environment_target = identity_pb2.EnvironmentTarget(
            namespace=cluster.namespace, name=target["name"], uid=target["uid"]
        )
        view = await wait_until_running(client, environment_target)
        assert view.target.uid == target["uid"]
        assert view.identity_name == name
        assert view.kubernetes_diagnostics.HasField("pod")
        # The Action Service holds the grant once it acknowledges the binding.
        await policy_objects.wait_actions_ready(f"{name}.launcher")

        launcher_binding = json.loads(
            await cluster.kubectl("get", POLICY_BINDINGS_PLURAL, f"{name}.launcher", "-o", "json")
        )
        assert launcher_binding["spec"]["subject"] == {"identity": {"name": launcher_view.identity_name}}
        assert launcher_binding["spec"]["policySets"] == [f"{name}.launcher"]
        launcher_set = json.loads(await cluster.kubectl("get", POLICY_SETS_PLURAL, f"{name}.launcher", "-o", "json"))
        assert launcher_set["spec"]["rules"] == [
            {
                "type": ACTION_RULE_TYPE,
                "policy": {
                    "type": "argument_schema",
                    "actions": {ORCHESTRATION_GROUP: sorted(launcher_grants)},
                    "schema": {
                        "type": "object",
                        "required": ["sandbox"],
                        "properties": {"sandbox": {"const": {"name": name, "uid": target["uid"]}}},
                    },
                },
            }
        ]

        identity_binding = json.loads(
            await cluster.kubectl("get", POLICY_BINDINGS_PLURAL, f"{name}.identity", "-o", "json")
        )
        assert identity_binding["spec"]["subject"] == {"identity": {"name": name}}
        assert identity_binding["spec"]["policySets"] == sorted(LAUNCH_DEFAULT_POLICY_SETS)

        session_id = str(uuid4())
        session = {"sandbox": target, "session_id": session_id}
        spec = {
            "harness": session_pb2.Harness.Name(harness),
            "model": offered_model,
            "cwd": WORKING_DIRECTORY,
            "reasoning_effort": "low",
        }
        opened = await launcher.submit(
            ORCHESTRATION_GROUP, "create_session", {**session, "spec": spec}, title="Create a test Session", wait=True
        )
        await _launcher_approved(operator_bff, opened, name)
        await client.wait_for_thread_projection(UUID(session_id), timeout_s=RUNNER_ANSWERS_SECONDS)
        history = await launcher.submit(
            ORCHESTRATION_GROUP, "read_turns", {**session, "count": 1}, title="Read a test Session's turns", wait=True
        )
        await _launcher_approved(operator_bff, history, name)
        before = json_format.MessageToDict(history.execution.result)
        assert before["turns"] == []
        resume_token = before["resume_token"]
        early_wait = await launcher.submit(
            ORCHESTRATION_GROUP,
            "wait_for_turn",
            {**session, "resume_token": resume_token, "wait_seconds": 1},
            title="Wait briefly for a test Session's turn",
            wait=True,
        )
        await _launcher_approved(operator_bff, early_wait, name)
        pending = json_format.MessageToDict(early_wait.execution.result)
        assert pending["completed"] is False
        resume_token = pending["resume_token"]

        command_id = str(uuid4())
        sent = await launcher.submit(
            ORCHESTRATION_GROUP,
            "submit_input",
            {**session, "command_id": command_id, "text": "Run the test command."},
            title="Send test input",
            wait=True,
        )
        await _launcher_approved(operator_bff, sent, name)

        # The launched Sandbox's turn asks for the same read with its own Identity, which no grant names.
        own = await play_cli_turn(
            scripted.model(view.kubernetes_diagnostics.pod.name, harness),
            *submission(
                ORCHESTRATION_GROUP,
                "read_turns",
                {**session, "count": 1},
                title="Read a test Session's turns",
                wait=False,
            ),
        )
        await decide_pending_action(operator_bff, own.id, action_service_pb2.VERDICT_DENY)
        async for attempt in AsyncRetrying(stop=stop_after_delay(TURN_SECONDS), wait=wait_fixed(1), reraise=True):
            with attempt:
                thread = await client.wait_for_thread_projection(UUID(session_id), timeout_s=RUNNER_ANSWERS_SECONDS)
                assert thread.last_turn_status == "TURN_STATUS_COMPLETED", thread

        waited = await launcher.submit(
            ORCHESTRATION_GROUP,
            "wait_for_turn",
            {**session, "resume_token": resume_token, "wait_seconds": 30},
            title="Wait for a test Session's turn",
            wait=True,
        )
        await _launcher_approved(operator_bff, waited, name)
        outcome = json_format.MessageToDict(waited.execution.result)
        assert outcome["completed"] is True
        assert outcome["turn"]["status"] == "TURN_STATUS_COMPLETED"

        read = await launcher.submit(
            ORCHESTRATION_GROUP, "read_turns", {**session, "count": 1}, title="Read a test Session's turns", wait=True
        )
        await _launcher_approved(operator_bff, read, name)
        state = json_format.MessageToDict(read.execution.result)
        turn = state["turns"][0]
        assert turn["turn_id"] == outcome["turn"]["turn_id"]
        assert turn["input"] == [{"text": "Run the test command.", "command_ids": [command_id]}]
        assert turn["tool_calls"]
        assert turn["status"] == "TURN_STATUS_COMPLETED"
        assert turn["final_assistant_message"] == "Done."

        read = await launcher.submit(
            ORCHESTRATION_GROUP, "get_session", session, title="Read a test Session", wait=True
        )
        await _launcher_approved(operator_bff, read, name)
        state = json_format.MessageToDict(read.execution.result)
        assert state["sandbox_ready"]["status"] == "True"
        assert state["session"]["last_turn_status"] == "TURN_STATUS_COMPLETED"
        assert [(d["command_id"], d["operation"]) for d in state["dispatches"]] == [
            (command_id, "OPERATION_SUBMIT_INPUT")
        ]

        assert state["dispatches"][0]["history"] == {"outcome": "confirmed", "turn_id": turn["turn_id"]}
        assert state["dispatches"][0]["state"] == "DELIVERY_STATE_RUNNER_ADMITTED"

        another = {**session, "sandbox": {"name": name, "uid": str(uuid4())}}
        elsewhere = await launcher.submit(
            ORCHESTRATION_GROUP, "read_turns", {**another, "count": 1}, title="Read a test Session's turns", wait=False
        )
        await decide_pending_action(operator_bff, elsewhere.id, action_service_pb2.VERDICT_DENY)

        other_session = await launcher.submit(
            ORCHESTRATION_GROUP,
            "read_turns",
            {**session, "session_id": str(launcher.agent.thread_id), "count": 1},
            title="Read another Session's turns",
            wait=True,
        )
        refused = action_request(await operator_bff.get(f"/upstream/actions/v1/action-requests/{other_session.id}"))
        assert refused.state == action_service_pb2.ACTION_STATE_FAILED, refused
        assert refused.decision.WhichOneof("decider") == "policy"
        assert refused.decision.policy.evidence.matched.policy_set == f"{name}.launcher"

        await suspend_and_delete(cluster, client, name, view.target.uid)
        deleted = True
        await _wait_unbound(launcher, launcher_view.identity_name, f"{name}.launcher")
        revoked = await launcher.submit(
            ORCHESTRATION_GROUP, "get_session", session, title="Read a test Session", wait=False
        )
        await decide_pending_action(operator_bff, revoked.id, action_service_pb2.VERDICT_DENY)
    finally:
        if not deleted:
            await suspend_and_delete(cluster, client, name, target["uid"])


# `session_pb2.pyi` imports google.protobuf, which mypy follows for this direct dependency.
# gazelle:include_dep @pypi//protobuf
