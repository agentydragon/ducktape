"""Agents submit Actions with the `agentplane` CLI: MCP-backed ones, and the orchestration_v1 group's
launches of Sandboxes they then drive. An operator links an OAuth-protected MCP server through Dex and
decides what waits for them. The model is scripted: each turn runs one CLI command, whose receipt the
scenario reads back from what the harness sent the model. The k3s target runs Dex, the MCP servers and
the Action groups (`mcp_servers.py`)."""

from __future__ import annotations

import asyncio
import json
import shlex
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from google.protobuf import json_format
from google.protobuf.struct_pb2 import Value
from more_itertools import one
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.action_service import action_service_pb2, operator_action_service_pb2
from agentplane.action_service.policies.resources import ACTION_RULE_TYPE, ACTIONS_READY_CONDITION
from agentplane.app.testing.client import Client
from agentplane.app.testing.models import ActionPolicyView, CallerGrantView, SandboxView
from agentplane.crd_group import GROUP, VERSION
from agentplane.policy_sets import POLICY_BINDINGS_PLURAL, POLICY_SETS_PLURAL
from agentplane.proto import runner_pb2
from agentplane.system_tests import mcp_servers
from agentplane.system_tests.agent import RUNNER_ANSWERS_SECONDS, TURN_SECONDS, WORKING_DIRECTORY, Agent
from agentplane.system_tests.cluster import Cluster
from agentplane.system_tests.operator_login import LoginBlockedError, OperatorCredentials, follow_dex_authorization
from agentplane.system_tests.sandbox import POD_TEMPLATE, Sandboxes, suspend_and_delete, wait_until_running
from agentplane.system_tests.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.tracing import tracer
from agentplane.testing.scripted_model import ModelRequest, ScriptedModel, ShellCall, Text

# A status write follows the informer's next watch event; the bound covers a relist after a
# dropped watch, not a healthy round trip.
POLICY_READY_SECONDS = 120.0
# Where the runner disk mounts the CLI in a Sandbox Pod (`agentplane/runner/BUILD.bazel`).
CLI = "/run/agentplane/runner/bin/agentplane"
# The key of the orchestration_v1 group in every deployment that offers it (cluster/cdk8s/agentplane/actions.py).
ORCHESTRATION_GROUP = "orchestration_v1"


def _assert_echoed(result: Value, message: str) -> None:
    """A call of either MCP server's echo tool recorded its text; the OAuth fixture, a FastMCP server,
    records structured content and metadata beside it."""
    result_json = json_format.MessageToDict(result)
    assert isinstance(result_json, dict), result_json
    assert (result_json["content"], result_json.get("isError", False)) == (
        [{"type": "text", "text": f"Echo: {message}"}],
        False,
    )


def _command(*args: str) -> str:
    return shlex.join([CLI, "--json", *args])


def _submission(group: str, action: str, arguments: dict[str, Any], *, title: str, wait: bool) -> list[str]:
    """The `agentplane` arguments that submit one Action request under a fresh key."""
    return [
        "actions",
        "submit",
        f"{group}/{action}",
        "--title",
        title,
        "--arguments",
        json.dumps(arguments),
        "--key",
        str(uuid4()),
        *(["--wait"] if wait else []),
    ]


async def _play_cli_turn(model: ScriptedModel[Any], *args: str) -> action_service_pb2.ActionRequest:
    """Play the model through a turn that runs `agentplane --json ARGS`: the receipt it printed, as the
    harness reported it to the model."""
    with tracer.start_as_current_span("system_test.mcp.turn"):
        call_id = f"agentplane-{uuid4().hex[:8]}"
        request = await model.request()
        await model.reply(request, ShellCall(call_id=call_id, command=_command(*args)))
        request = await model.request()
        await model.reply(request, Text("Done."))
        return _receipt(request, call_id)


def _receipt(request: ModelRequest[Any], call_id: str) -> action_service_pb2.ActionRequest:
    reported = one(output for output in request.tool_outputs if output.call_id == call_id)
    # A harness frames the command's output with its own lines; `--json` prints the receipt as one.
    receipt = action_service_pb2.ActionRequest()
    json_format.Parse(
        one(line for block in reported.blocks for line in block.splitlines() if line.startswith("{")), receipt
    )
    return receipt


@dataclass(frozen=True)
class ScriptedAgent:
    """A session whose model the scenario plays: each turn runs one `agentplane` command."""

    agent: Agent
    model: ScriptedModel[Any]

    @classmethod
    async def open(
        cls,
        client: Client,
        sandbox: SandboxView,
        *,
        harness: runner_pb2.Harness,
        model: str,
        upstream: ScriptedUpstream,
    ) -> ScriptedAgent:
        assert sandbox.pod is not None
        agent = await Agent.open(client, sandbox=sandbox.name, harness=harness, model=model)
        return cls(agent=agent, model=upstream.model(sandbox.pod.name, harness))

    async def run(self, *args: str) -> action_service_pb2.ActionRequest:
        """The receipt `agentplane --json ARGS` printed, as the harness reported it to the model."""
        async with asyncio.TaskGroup() as turns:
            turns.create_task(self.agent.run(f"Run {_command(*args)}"))
            # The TaskGroup still waits for the turn to complete before this returns.
            return await _play_cli_turn(self.model, *args)

    async def submit(
        self, group: str, action: str, arguments: dict[str, Any], *, title: str, wait: bool
    ) -> action_service_pb2.ActionRequest:
        return await self.run(*_submission(group, action, arguments, title=title, wait=wait))

    async def submit_echo(self, group: str, message: str, *, wait: bool) -> action_service_pb2.ActionRequest:
        return await self.submit(group, mcp_servers.ECHO, {"message": message}, title="Echo a test marker", wait=wait)


class PolicyObjectNotReadyError(Exception):
    """The Action Service has not yet judged the binding's current generation."""


class PolicyBindingPresentError(Exception):
    """The binding, or the Action Service's view of it, is still there."""


@dataclass
class PolicyObjects:
    """PolicySets and PolicyBindings this test writes with the caller's kubeconfig, deleted whatever
    the test did. RBAC on the two kinds in the namespace is what gates this."""

    cluster: Cluster
    created: list[tuple[str, str]] = field(default_factory=list)

    async def _create(self, kind: str, plural: str, name: str, spec: dict[str, Any]) -> str:
        body = {"apiVersion": f"{GROUP}/{VERSION}", "kind": kind, "metadata": {"name": name}, "spec": spec}
        await self.cluster.kubectl("create", "-f", "-", body=body)
        self.created.append((plural, name))
        return name

    async def create_policy_set(self, name: str, auto_approve: list[dict[str, Any]]) -> str:
        rules = [{"type": ACTION_RULE_TYPE, "policy": policy} for policy in auto_approve]
        return await self._create("PolicySet", POLICY_SETS_PLURAL, name, {"rules": rules})

    async def create_binding(self, name: str, *, sandbox: SandboxView, policy_sets: list[str]) -> str:
        subject = {"namespace": sandbox.service_account.namespace, "name": sandbox.service_account.name}
        return await self._create(
            "PolicyBinding", POLICY_BINDINGS_PLURAL, name, {"subject": subject, "policySets": policy_sets}
        )

    async def bind(self, sandbox: SandboxView, name: str, auto_approve: list[dict[str, Any]]) -> tuple[str, str]:
        """One set bound to `sandbox`'s ServiceAccount, the binding ActionsReady: the set's and the
        binding's names."""
        suffix = uuid4().hex[:8]
        set_name = await self.create_policy_set(f"systest-{name}-{suffix}", auto_approve)
        binding_name = await self.create_binding(
            f"{sandbox.name}-{name}-{suffix}", sandbox=sandbox, policy_sets=[set_name]
        )
        await self.wait_actions_ready(binding_name)
        return set_name, binding_name

    async def expire_binding(self, name: str) -> None:
        """Sets `expiresAt` in the past and waits until the Policy Controller has deleted the binding."""
        past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        await self.cluster.kubectl(
            "patch", POLICY_BINDINGS_PLURAL, name, "--type=merge", "-p", json.dumps({"spec": {"expiresAt": past}})
        )
        # Yield during policy propagation so the other harness can continue its scenario.
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(POLICY_READY_SECONDS),
            wait=wait_fixed(1),
            retry=retry_if_exception_type(PolicyBindingPresentError),
            reraise=True,
        ):
            with attempt:
                if await self.cluster.kubectl("get", POLICY_BINDINGS_PLURAL, name, "--ignore-not-found", "-o", "name"):
                    raise PolicyBindingPresentError(f"{POLICY_BINDINGS_PLURAL}/{name}")

    async def wait_actions_ready(self, name: str) -> None:
        """Until the binding's ActionsReady condition is True for its current generation: the Action
        Service's own acknowledgement that it holds the binding and every set it names."""
        with tracer.start_as_current_span("system_test.mcp.policy_ready"):
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(POLICY_READY_SECONDS),
                wait=wait_fixed(1),
                retry=retry_if_exception_type(PolicyObjectNotReadyError),
                reraise=True,
            ):
                with attempt:
                    obj = json.loads(await self.cluster.kubectl("get", POLICY_BINDINGS_PLURAL, name, "-o", "json"))
                    conditions = obj.get("status", {}).get("conditions", [])
                    ready = [c for c in conditions if c["type"] == ACTIONS_READY_CONDITION]
                    generation = obj["metadata"]["generation"]
                    if not ready or ready[0].get("observedGeneration") != generation or ready[0]["status"] != "True":
                        # False is also how a set not yet seen reads, so only the deadline makes it final.
                        raise PolicyObjectNotReadyError(
                            f"{POLICY_BINDINGS_PLURAL}/{name} generation {generation}: {ready}"
                        )

    async def delete_all(self) -> None:
        with tracer.start_as_current_span("system_test.mcp.policy_cleanup"):
            for plural, name in reversed(self.created):
                await self.cluster.kubectl("delete", plural, name, "--ignore-not-found")


async def agent_executes_mcp_action(
    client: Client,
    sandbox: Sandboxes,
    harness: runner_pb2.Harness,
    offered_model: str,
    scripted: ScriptedUpstream,
    policy_objects: PolicyObjects,
) -> None:
    view = await sandbox(f"systest-mcp-{harness}")
    await policy_objects.bind(
        view, "exact-echo", [{"type": "exact_actions", "actions": {mcp_servers.EVERYTHING_GROUP: [mcp_servers.ECHO]}}]
    )
    agent = await ScriptedAgent.open(client, view, harness=harness, model=offered_model, upstream=scripted)
    marker = f"MCP0-{uuid4()}"
    receipt = await agent.submit_echo(mcp_servers.EVERYTHING_GROUP, marker, wait=True)
    assert receipt.state == action_service_pb2.ACTION_STATE_SUCCEEDED
    assert receipt.HasField("execution")
    _assert_echoed(receipt.execution.result, marker)


async def operator_links_oauth_mcp_server(
    operator_bff: httpx.AsyncClient,
    operator_credentials: OperatorCredentials,
    client: Client,
    sandbox: Sandboxes,
    harness: runner_pb2.Harness,
    offered_model: str,
    scripted: ScriptedUpstream,
    policy_objects: PolicyObjects,
) -> None:
    """Links the OAuth-protected MCP server through Dex and executes its protected tool."""
    server_id = mcp_servers.OAUTH
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
                callback_app=operator_bff.base_url,
                callback_path="/mcp-linkage/callback",
            )
    except LoginBlockedError as exc:
        pytest.fail(str(exc), pytrace=False)

    callback = await operator_bff.get("/mcp-linkage/callback", params={"state": state, "code": code})
    assert callback.status_code == HTTPStatus.SEE_OTHER, callback.text

    view = await sandbox(f"systest-oauth-mcp-{harness}")
    await policy_objects.bind(
        view, "oauth-echo", [{"type": "exact_actions", "actions": {mcp_servers.OAUTH: [mcp_servers.ECHO]}}]
    )
    agent = await ScriptedAgent.open(client, view, harness=harness, model=offered_model, upstream=scripted)
    marker = f"MCP-OAUTH-{uuid4()}"
    receipt = await agent.submit_echo(mcp_servers.OAUTH, marker, wait=True)
    assert receipt.state == action_service_pb2.ACTION_STATE_SUCCEEDED
    assert receipt.HasField("execution")
    _assert_echoed(receipt.execution.result, marker)


def _bff_request(response: httpx.Response) -> action_service_pb2.ActionRequest:
    """Decode an Actions facade response to its generated request; never render identity details."""
    __tracebackhide__ = True
    try:
        envelope = action_service_pb2.GetActionRequestResponse()
        json_format.Parse(response.text, envelope)
        if not envelope.HasField("action_request"):
            raise ValueError("Action Service response omitted its request")
        return envelope.action_request
    except json_format.ParseError, ValueError:
        pytest.fail("BFF returned an invalid ActionRequest; response body withheld", pytrace=False)


def _bff_decision(response: httpx.Response) -> action_service_pb2.ActionRequest:
    """Decode an operator decision response to its generated ActionRequest."""
    __tracebackhide__ = True
    try:
        envelope = operator_action_service_pb2.DecideActionRequestResponse()
        json_format.Parse(response.text, envelope)
        if not envelope.HasField("action_request"):
            raise ValueError("Action Service response omitted its request")
        return envelope.action_request
    except json_format.ParseError, ValueError:
        pytest.fail("BFF returned an invalid decision response; response body withheld", pytrace=False)


async def agent_mcp_bff_decision(
    operator_bff: httpx.AsyncClient,
    operator_credentials: OperatorCredentials,
    client: Client,
    sandbox: Sandboxes,
    harness: runner_pb2.Harness,
    offered_model: str,
    scripted: ScriptedUpstream,
) -> None:
    view = await sandbox(f"systest-mcp-{harness}")
    agent = await ScriptedAgent.open(client, view, harness=harness, model=offered_model, upstream=scripted)
    # Denial runs first on a fresh session; the next request must still wait for its own decision.
    for verdict in (action_service_pb2.VERDICT_DENY, action_service_pb2.VERDICT_ALLOW):
        # Nothing binds this Sandbox to a policy set, so every Action of its waits for the operator.
        marker = f"MCP-BFF-{uuid4()}-" + "x" * 201
        submitted = await agent.submit_echo(mcp_servers.EVERYTHING_GROUP, marker, wait=False)
        assert submitted.state == action_service_pb2.ACTION_STATE_DECISION_PENDING
        request_id = submitted.id
        path = f"/upstream/actions/v1/action-requests/{request_id}"
        decision_path = f"/upstream/actions/v1/operator/action-requests/{request_id}/decision"
        pending_response = await operator_bff.get(path)
        assert pending_response.status_code == HTTPStatus.OK
        pending = _bff_request(pending_response)
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
        decided = _bff_decision(decided_response)
        assert decided.id == request_id
        assert json_format.MessageToDict(decided.arguments) == json_format.MessageToDict(pending.arguments)
        assert decided.version == pending.version + 1
        assert decided.HasField("decision")
        assert decided.decision.verdict == verdict
        assert decided.decision.provider == "human_operator"
        if not decided.decision.HasField("operator") or (
            decided.decision.operator.issuer != operator_credentials.issuer.get_secret_value()
            or decided.decision.operator.subject != operator_credentials.subject.get_secret_value()
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
        terminal = _bff_request(terminal_response)
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
            _assert_echoed(terminal.execution.result, marker)
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
        if _bff_decision(duplicate) != terminal:
            pytest.fail("Duplicate decision changed the terminal request", pytrace=False)
        stale = operator_action_service_pb2.DecideActionRequestRequest()
        stale.CopyFrom(decision)
        stale.idempotency_key = str(uuid4())
        refused = await operator_bff.post(decision_path, json=json_format.MessageToDict(stale))
        assert refused.status_code == HTTPStatus.CONFLICT
        unchanged = await operator_bff.get(path)
        assert unchanged.status_code == HTTPStatus.OK
        if _bff_request(unchanged) != terminal:
            pytest.fail("Stale decision changed the terminal request", pytrace=False)


async def _decide_pending(
    operator_bff: httpx.AsyncClient, request_id: str, verdict: action_service_pb2.Verdict
) -> action_service_pb2.ActionRequest:
    """Check a request waited for the operator, then decide it, so nothing lingers undecided."""
    with tracer.start_as_current_span("system_test.mcp.operator_decision"):
        path = f"/upstream/actions/v1/action-requests/{request_id}"
        receipt = _bff_request(await operator_bff.get(path))
        assert receipt.state == action_service_pb2.ACTION_STATE_DECISION_PENDING
        if receipt.HasField("decision"):
            pytest.fail("Request expected on the human path already has a Decision", pytrace=False)
        decision = operator_action_service_pb2.DecideActionRequestRequest(
            request_id=str(request_id), verdict=verdict, expected_version=receipt.version, idempotency_key=str(uuid4())
        )
        decided = await operator_bff.post(
            f"/upstream/actions/v1/operator/action-requests/{request_id}/decision",
            json=json_format.MessageToDict(decision),
        )
        assert decided.status_code == HTTPStatus.OK
        decided_request = _bff_decision(decided)
        assert decided_request.state == (
            action_service_pb2.ACTION_STATE_ALLOWED
            if verdict == action_service_pb2.VERDICT_ALLOW
            else action_service_pb2.ACTION_STATE_DENIED
        )
        assert decided_request.HasField("decision")
        assert decided_request.decision.verdict == verdict
        return receipt


async def policy_binding_auto_approves_the_bound_sandbox(
    operator_bff: httpx.AsyncClient,
    client: Client,
    sandbox: Sandboxes,
    harness: runner_pb2.Harness,
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
    agent = await ScriptedAgent.open(client, view, harness=harness, model=offered_model, upstream=scripted)

    marker = f"MCP-POLICY-{uuid4()}"
    approved = await agent.submit_echo(mcp_servers.EVERYTHING_GROUP, marker, wait=True)
    assert approved.state == action_service_pb2.ACTION_STATE_SUCCEEDED
    assert approved.HasField("execution")
    _assert_echoed(approved.execution.result, marker)
    receipt = _bff_request(await operator_bff.get(f"/upstream/actions/v1/action-requests/{approved.id}"))
    assert receipt.state == action_service_pb2.ACTION_STATE_SUCCEEDED
    assert receipt.HasField("decision")
    assert receipt.decision.provider == "action_policy_set"
    assert receipt.decision.HasField("policy_evidence")
    evidence = receipt.decision.policy_evidence
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
    missed = await agent.submit_echo(mcp_servers.EVERYTHING_GROUP, too_long, wait=False)
    await _decide_pending(operator_bff, missed.id, action_service_pb2.VERDICT_DENY)

    await policy_objects.expire_binding(binding_name)
    await _wait_unbound(operator_bff, view, binding_name)
    after_expiry = f"MCP-POLICY-{uuid4()}"
    expired = await agent.submit_echo(mcp_servers.EVERYTHING_GROUP, after_expiry, wait=False)
    await _decide_pending(operator_bff, expired.id, action_service_pb2.VERDICT_DENY)


async def _wait_unbound(operator_bff: httpx.AsyncClient, sandbox: SandboxView, binding_name: str) -> None:
    """Until the Action Service no longer resolves `binding_name` for the Sandbox's ServiceAccount:
    a deleted binding has no condition left to acknowledge, so ask the service itself."""
    with tracer.start_as_current_span("system_test.mcp.policy_unbound"):
        account = sandbox.service_account
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(POLICY_READY_SECONDS),
            wait=wait_fixed(1),
            retry=retry_if_exception_type(PolicyBindingPresentError),
            reraise=True,
        ):
            with attempt:
                response = await operator_bff.get(f"/caller-grants/{account.namespace}/{account.name}")
                assert response.status_code == HTTPStatus.OK, response.text
                policy = CallerGrantView.model_validate(response.json()).action_policy
                if not isinstance(policy, ActionPolicyView) or not policy.synced:
                    raise PolicyBindingPresentError(f"{account}: Action policy not synced: {policy}")
                if binding_name in {effective.binding for effective in policy.auto_approve_if}:
                    raise PolicyBindingPresentError(f"{account} still resolves {binding_name}")


async def _launcher_approved(
    operator_bff: httpx.AsyncClient, receipt: action_service_pb2.ActionRequest, sandbox: str
) -> None:
    """The request succeeded on the launcher grant of `sandbox`, with no operator asked."""
    assert receipt.state == action_service_pb2.ACTION_STATE_SUCCEEDED, receipt
    decided = _bff_request(await operator_bff.get(f"/upstream/actions/v1/action-requests/{receipt.id}"))
    assert decided.decision.provider == "action_policy_set"
    assert decided.decision.policy_evidence.matched.policy_set == f"{sandbox}.launcher"


async def launcher_drives_only_the_sandbox_it_launched(
    cluster: Cluster,
    operator_bff: httpx.AsyncClient,
    client: Client,
    sandbox: Sandboxes,
    harness: runner_pb2.Harness,
    offered_model: str,
    scripted: ScriptedUpstream,
    policy_objects: PolicyObjects,
) -> None:
    """An agent launches a Sandbox through orchestration_v1, which waits for the operator; the launch's
    grants then let that agent open a Session in it, send input, wait for its turn and read the outcome
    without asking again. The launched Sandbox's own account, another incarnation of the name and the
    deleted Sandbox still wait for the operator; a Session in a different Sandbox is refused."""
    launcher_view = await sandbox(f"systest-launcher-{harness}")
    launcher = await ScriptedAgent.open(client, launcher_view, harness=harness, model=offered_model, upstream=scripted)
    name = f"systest-launched-{harness}"
    launch = await launcher.submit(
        ORCHESTRATION_GROUP,
        "launch_sandbox",
        {
            "name": name,
            "template": POD_TEMPLATE,
            "launcher_grants": ["open_session", "submit_input", "get_session", "read_turns", "wait_for_turn"],
        },
        title="Launch a test Sandbox",
        wait=False,
    )
    await _decide_pending(operator_bff, launch.id, action_service_pb2.VERDICT_ALLOW)
    launched = await launcher.run("actions", "wait", str(launch.id))
    assert launched.state == action_service_pb2.ACTION_STATE_SUCCEEDED, launched
    target = json_format.MessageToDict(launched.execution.result)["sandbox"]
    assert target["name"] == name
    deleted = False
    try:
        view = await wait_until_running(client, name, expected_uid=UUID(target["uid"]))
        assert str(view.uid) == target["uid"]
        assert view.pod is not None
        # The Action Service holds the grant once it acknowledges the binding.
        await policy_objects.wait_actions_ready(f"{name}.launcher")

        session_id = str(uuid4())
        session = {"sandbox": target, "session_id": session_id}
        spec = {
            "harness": runner_pb2.Harness.Name(harness),
            "model": offered_model,
            "cwd": WORKING_DIRECTORY,
            "reasoning_effort": "low",
        }
        opened = await launcher.submit(
            ORCHESTRATION_GROUP, "open_session", {**session, "spec": spec}, title="Open a test Session", wait=True
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

        # The launched Sandbox's turn asks for the same read with its own account, which no grant names.
        own = await _play_cli_turn(
            scripted.model(view.pod.name, harness),
            *_submission(
                ORCHESTRATION_GROUP,
                "read_turns",
                {**session, "count": 1},
                title="Read a test Session's turns",
                wait=False,
            ),
        )
        await _decide_pending(operator_bff, own.id, action_service_pb2.VERDICT_DENY)
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
        await _decide_pending(operator_bff, elsewhere.id, action_service_pb2.VERDICT_DENY)

        other_session = await launcher.submit(
            ORCHESTRATION_GROUP,
            "read_turns",
            {**session, "session_id": str(launcher.agent.thread_id), "count": 1},
            title="Read another Session's turns",
            wait=True,
        )
        refused = _bff_request(await operator_bff.get(f"/upstream/actions/v1/action-requests/{other_session.id}"))
        assert refused.state == action_service_pb2.ACTION_STATE_FAILED, refused
        assert refused.decision.provider == "action_policy_set"
        assert refused.decision.policy_evidence.matched.policy_set == f"{name}.launcher"

        await suspend_and_delete(cluster, client, name, view.uid)
        deleted = True
        await _wait_unbound(operator_bff, launcher_view, f"{name}.launcher")
        revoked = await launcher.submit(
            ORCHESTRATION_GROUP, "get_session", session, title="Read a test Session", wait=False
        )
        await _decide_pending(operator_bff, revoked.id, action_service_pb2.VERDICT_DENY)
    finally:
        if not deleted:
            await suspend_and_delete(cluster, client, name, UUID(target["uid"]))


# `runner_pb2.pyi` imports google.protobuf, which mypy follows for this direct dependency.
# gazelle:include_dep @pypi//protobuf
