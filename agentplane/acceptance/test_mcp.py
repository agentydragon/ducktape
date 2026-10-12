"""Real testing agents discover, submit, poll and report an MCP-backed Action."""

import json
import logging
import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_bazel
from pydantic import BaseModel, ConfigDict, JsonValue
from tenacity import Retrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.acceptance.agent import Agent
from agentplane.acceptance.conftest import NAMESPACE, TESTING_NAMESPACE, Sandboxes
from agentplane.acceptance.operator_login import (
    IdentityProvider,
    LoginBlockedError,
    OperatorCredentials,
    app_origin,
    follow_dex_authorization,
    login_operator,
    read_operator_credentials,
    verify_action_federation,
)
from agentplane.action_service.mcp_linkage import McpLinkageStatus, McpLinkageView
from agentplane.action_service.models import (
    ActionRequestView,
    ActionState,
    DecisionInput,
    ExecutionState,
    MatchedPolicy,
    OperatorPrincipal,
    PolicyKind,
    PolicySource,
    Verdict,
)
from agentplane.action_service.policies.resources import ACTION_RULE_TYPE, ACTIONS_READY_CONDITION
from agentplane.action_service.policy_evaluation import PROVIDER_NAME
from agentplane.app.action_policy import ActionPolicyView
from agentplane.app.api import CallerGrantView
from agentplane.app.sandbox_models import SandboxView
from agentplane.app.testing.client import Client
from agentplane.crd_group import GROUP, VERSION
from agentplane.policy_sets import POLICY_BINDINGS_PLURAL, POLICY_SETS_PLURAL
from agentplane.proto import runner_pb2

# `runner_pb2.pyi` imports google.protobuf, which mypy follows for this direct dependency.
# gazelle:include_dep @pypi//protobuf

# A status write follows the informer's next watch event; the bound covers a relist after a
# dropped watch, not a healthy round trip.
POLICY_READY_SECONDS = 120.0


class McpReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    state: Literal["succeeded"]
    output: dict[str, JsonValue]


class PendingMcpReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    state: Literal["decision_pending"]


def _execute_echo(group: str, message: str) -> str:
    """A prompt to run echo to its terminal result, answered as an `McpReport`."""
    return f"""
This is an Agentplane infrastructure test. Use the Actions Service to execute the echo Action in
the {group} group with message {message}. Wait for its terminal result. Return only JSON with
exactly request_id, state, and output, where output is the request's execution result. Do not
infer or fabricate a result.
"""


def _queue_echo(message: str) -> str:
    """A prompt to submit an echo that waits for the operator, answered as a `PendingMcpReport`."""
    return f"""
This is an Agentplane infrastructure test. Request the echo Action in the everything group with
message {message}. This request requires operator approval. Confirm that it is queued for approval;
return only JSON with exactly request_id and state, where state must be decision_pending. Do not
wait for an operator decision in this turn.
"""


def operator_idp() -> IdentityProvider:
    value = os.environ.get("AGENTPLANE_ACCEPTANCE_IDP", IdentityProvider.DEX)
    if value not in set(IdentityProvider):
        pytest.fail("BLOCKED: AGENTPLANE_ACCEPTANCE_IDP must be authentik or dex", pytrace=False)
    return IdentityProvider(value)


class KubectlError(Exception):
    """kubectl refused; its own message says why."""


class PolicyObjectNotReadyError(Exception):
    """The Action Service has not yet judged the binding's current generation."""


class PolicyBindingPresentError(Exception):
    """The binding, or the Action Service's view of it, is still there."""


@dataclass
class PolicyObjects:
    """PolicySets and PolicyBindings this test writes with the caller's kubeconfig, deleted whatever
    the test did. RBAC on the two kinds in the namespace is what gates this."""

    namespace: str
    created: list[tuple[str, str]] = field(default_factory=list)

    def _kubectl(self, *args: str, stdin: str | None = None) -> str:
        command = ["kubectl", "-n", self.namespace, *args]
        result = subprocess.run(command, input=stdin, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise KubectlError(f"`{' '.join(command)}` failed with {result.returncode}: {result.stderr.strip()}")
        return result.stdout

    def _create(self, kind: str, plural: str, name: str, spec: dict[str, Any]) -> str:
        body = {"apiVersion": f"{GROUP}/{VERSION}", "kind": kind, "metadata": {"name": name}, "spec": spec}
        self._kubectl("create", "-f", "-", stdin=json.dumps(body))
        self.created.append((plural, name))
        return name

    def create_policy_set(self, name: str, auto_approve: list[dict[str, Any]]) -> str:
        rules = [{"type": ACTION_RULE_TYPE, "policy": policy} for policy in auto_approve]
        return self._create("PolicySet", POLICY_SETS_PLURAL, name, {"rules": rules})

    def create_binding(self, name: str, *, sandbox: SandboxView, policy_sets: list[str]) -> str:
        subject = {"namespace": sandbox.service_account.namespace, "name": sandbox.service_account.name}
        return self._create(
            "PolicyBinding", POLICY_BINDINGS_PLURAL, name, {"subject": subject, "policySets": policy_sets}
        )

    def bind(self, sandbox: SandboxView, name: str, auto_approve: list[dict[str, Any]]) -> tuple[str, str]:
        """One set bound to `sandbox`'s ServiceAccount, the binding ActionsReady: the set's and the
        binding's names."""
        suffix = uuid4().hex[:8]
        set_name = self.create_policy_set(f"accept-{name}-{suffix}", auto_approve)
        binding_name = self.create_binding(f"{sandbox.name}-{name}-{suffix}", sandbox=sandbox, policy_sets=[set_name])
        self.wait_actions_ready(binding_name)
        return set_name, binding_name

    def expire_binding(self, name: str) -> None:
        """Sets `expiresAt` in the past and waits until the Policy Controller has deleted the binding."""
        past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        self._kubectl(
            "patch", POLICY_BINDINGS_PLURAL, name, "--type=merge", "-p", json.dumps({"spec": {"expiresAt": past}})
        )
        for attempt in Retrying(
            stop=stop_after_delay(POLICY_READY_SECONDS),
            wait=wait_fixed(1),
            retry=retry_if_exception_type(PolicyBindingPresentError),
            reraise=True,
        ):
            with attempt:
                if self._kubectl("get", POLICY_BINDINGS_PLURAL, name, "--ignore-not-found", "-o", "name"):
                    raise PolicyBindingPresentError(f"{POLICY_BINDINGS_PLURAL}/{name}")

    def wait_actions_ready(self, name: str) -> None:
        """Until the binding's ActionsReady condition is True for its current generation: the Action
        Service's own acknowledgement that it holds the binding and every set it names."""
        for attempt in Retrying(
            stop=stop_after_delay(POLICY_READY_SECONDS),
            wait=wait_fixed(1),
            retry=retry_if_exception_type(PolicyObjectNotReadyError),
            reraise=True,
        ):
            with attempt:
                obj = json.loads(self._kubectl("get", POLICY_BINDINGS_PLURAL, name, "-o", "json"))
                conditions = obj.get("status", {}).get("conditions", [])
                ready = [c for c in conditions if c["type"] == ACTIONS_READY_CONDITION]
                generation = obj["metadata"]["generation"]
                if not ready or ready[0].get("observedGeneration") != generation or ready[0]["status"] != "True":
                    # False is also how a set not yet seen reads, so only the deadline makes it final.
                    raise PolicyObjectNotReadyError(f"{POLICY_BINDINGS_PLURAL}/{name} generation {generation}: {ready}")

    def delete_all(self) -> None:
        for plural, name in reversed(self.created):
            self._kubectl("delete", plural, name, "--ignore-not-found")


@pytest.fixture
def policy_objects() -> Iterator[PolicyObjects]:
    objects = PolicyObjects(namespace=os.environ.get(NAMESPACE, TESTING_NAMESPACE))
    try:
        yield objects
    finally:
        objects.delete_all()


async def test_agent_executes_mcp_action(
    client: Client, sandbox: Sandboxes, harness: runner_pb2.Harness, model: str, policy_objects: PolicyObjects
) -> None:
    view = await sandbox(f"accept-mcp-{harness}")
    policy_objects.bind(view, "exact-echo", [{"type": "exact_actions", "actions": {"everything": ["echo"]}}])
    agent = await Agent.open(client, sandbox=view.name, harness=harness, model=model)
    marker = f"MCP0-{uuid4()}"
    turn = await agent.run(_execute_echo("everything", marker))
    report = turn.report(McpReport)
    assert report.output == {"content": [f"Echo: {marker}"]}, turn.transcript


async def test_operator_links_oauth_mcp_server(
    operator_bff: httpx.AsyncClient,
    operator_credentials: OperatorCredentials,
    client: Client,
    sandbox: Sandboxes,
    harness: runner_pb2.Harness,
    model: str,
    policy_objects: PolicyObjects,
) -> None:
    """Links the `example` MCP server through Dex and executes its protected tool."""
    server_id = "example"
    start_response = await operator_bff.post(f"/mcp-servers/{server_id}/linkage/start", json={"scopes": []})
    assert start_response.status_code == HTTPStatus.OK, start_response.text
    authorization_url = start_response.json()["authorization_url"]

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

    linkage_response = await operator_bff.get(f"/mcp-servers/{server_id}/linkage")
    assert linkage_response.status_code == HTTPStatus.OK
    linkage = McpLinkageView.model_validate(linkage_response.json())
    assert linkage.status is McpLinkageStatus.LINKED

    view = await sandbox(f"accept-oauth-mcp-{harness}")
    policy_objects.bind(view, "oauth-example", [{"type": "exact_actions", "actions": {"example": ["echo"]}}])
    agent = await Agent.open(client, sandbox=view.name, harness=harness, model=model)
    marker = f"MCP-OAUTH-{uuid4()}"
    turn = await agent.run(_execute_echo("example", marker))
    report = turn.report(McpReport)
    assert report.output == {"content": [f"Echo: {marker}"]}, turn.transcript


@pytest.fixture
def operator_credentials(pytestconfig: pytest.Config) -> OperatorCredentials:
    if pytestconfig.option.showlocals:
        pytest.fail("BLOCKED: disable pytest local-variable dumps before reading operator credentials", pytrace=False)
    try:
        return read_operator_credentials()
    except LoginBlockedError as exc:
        pytest.fail(str(exc), pytrace=False)


@pytest.fixture
async def operator_bff(base_url: str, operator_credentials: OperatorCredentials) -> AsyncIterator[httpx.AsyncClient]:
    """A real app-issued session. No credential injection, fabricated cookies, or DB writes."""
    # HTTP debug logging includes cookie/token headers and callback query strings. Suppress it
    # throughout this client lifetime (including failed assertions/teardown), even under --log-cli-level.
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        origin = str(app_origin(base_url)).rstrip("/")
        async with httpx.AsyncClient(base_url=origin, timeout=30, follow_redirects=False) as http:
            try:
                await login_operator(http, operator_credentials, provider=operator_idp())
            except LoginBlockedError as exc:
                pytest.fail(str(exc), pytrace=False)
            except httpx.HTTPError, httpx.InvalidURL, ValueError, KeyError, TypeError:
                pytest.fail("BLOCKED: OIDC transport or response invalid; auth details withheld", pytrace=False)
            http.headers["Origin"] = origin
            await verify_action_federation(http)
            yield http
    except LoginBlockedError as exc:
        pytest.fail(str(exc), pytrace=False)
    finally:
        logging.disable(previous_logging)


class DecidedMcpReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    state: Literal["succeeded", "denied"]
    result: JsonValue


def _bff_request(response: httpx.Response) -> ActionRequestView:
    """Validation errors must not render the response's operator identity into test artifacts."""
    __tracebackhide__ = True
    try:
        return ActionRequestView.model_validate(response.json())
    except ValueError:
        pytest.fail("BFF returned an invalid ActionRequestView; response body withheld", pytrace=False)


@pytest.mark.parametrize("verdict", list(Verdict))
async def test_agent_mcp_bff_decision(
    operator_bff: httpx.AsyncClient,
    operator_credentials: OperatorCredentials,
    client: Client,
    sandbox: Sandboxes,
    harness: runner_pb2.Harness,
    model: str,
    verdict: Verdict,
) -> None:
    view = await sandbox(f"accept-mcp-{harness}-{verdict}")
    agent = await Agent.open(client, sandbox=view.name, harness=harness, model=model)
    # Nothing binds this Sandbox to a policy set, so every Action of its waits for the operator.
    marker = f"MCP-BFF-{uuid4()}-" + "x" * 201
    submitted = await agent.run(_queue_echo(marker))
    submitted_report = submitted.report(PendingMcpReport)
    request_id = submitted_report.request_id
    path = f"/actions/{request_id}"
    pending_response = await operator_bff.get(path)
    assert pending_response.status_code == HTTPStatus.OK
    pending = _bff_request(pending_response)
    assert pending.id == request_id
    assert pending.idempotency_key
    assert (pending.action.group, pending.action.name) == ("everything", "echo")
    assert pending.arguments == {"message": marker}
    assert pending.state is ActionState.DECISION_PENDING
    assert pending.version == 1
    if pending.decision is not None:
        pytest.fail("Pending request already has a Decision", pytrace=False)
    assert pending.execution is None

    decision = DecisionInput(verdict=verdict, expected_version=pending.version, idempotency_key=str(uuid4()))
    decided_response = await operator_bff.post(f"{path}/decision", json=decision.model_dump(mode="json"))
    assert decided_response.status_code == HTTPStatus.OK
    decided = _bff_request(decided_response)
    assert decided.id == request_id
    assert decided.arguments == pending.arguments
    assert decided.version == pending.version + 1
    assert decided.decision is not None
    assert decided.decision.verdict is verdict
    assert decided.decision.provider == "human_operator"
    if decided.decision.operator != OperatorPrincipal(
        issuer=operator_credentials.issuer.get_secret_value(), subject=operator_credentials.subject.get_secret_value()
    ):
        pytest.fail("Decision operator identity differs from the dedicated Secret", pytrace=False)
    assert decided.decision.idempotency_key == decision.idempotency_key
    expected_state = ActionState.SUCCEEDED if verdict is Verdict.ALLOW else ActionState.DENIED
    expected_result = (
        {"content": [{"type": "text", "text": f"Echo: {marker}"}], "isError": False}
        if verdict is Verdict.ALLOW
        else None
    )
    if verdict is Verdict.ALLOW:
        assert decided.state is ActionState.ALLOWED
        assert decided.execution is not None
    else:
        assert decided.state is ActionState.DENIED
        assert decided.execution is None

    completed = await agent.run(f"""
Resume Action request {request_id}. The operator has decided {verdict}. Determine its terminal
outcome without submitting or deciding anything. Return only JSON with exactly request_id, state,
and result. Copy result from execution when it exists; otherwise use null. Do not infer or fabricate it.
""")
    # Unlike Turn.report, reject fences and surrounding prose here.
    report = DecidedMcpReport.model_validate_json(completed.answer)
    assert report.request_id == request_id
    assert report.state == expected_state
    assert report.result == expected_result
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
    assert terminal.state is expected_state
    if verdict is Verdict.ALLOW:
        assert terminal.execution is not None
        assert decided.execution is not None
        assert terminal.execution.id == decided.execution.id
        assert terminal.execution.state is ExecutionState.SUCCEEDED
        assert terminal.execution.result == expected_result
        assert terminal.execution.error is None
    else:
        assert terminal.execution is None

    # Replay the original version/key after completion: same Decision and Execution, no new dispatch.
    duplicate = await operator_bff.post(f"{path}/decision", json=decision.model_dump(mode="json"))
    assert duplicate.status_code == HTTPStatus.OK
    if _bff_request(duplicate) != terminal:
        pytest.fail("Duplicate decision changed the terminal request", pytrace=False)
    stale = decision.model_copy(update={"idempotency_key": str(uuid4())})
    refused = await operator_bff.post(f"{path}/decision", json=stale.model_dump(mode="json"))
    assert refused.status_code == HTTPStatus.CONFLICT
    unchanged = await operator_bff.get(path)
    assert unchanged.status_code == HTTPStatus.OK
    if _bff_request(unchanged) != terminal:
        pytest.fail("Stale decision changed the terminal request", pytrace=False)


async def _deny_pending(operator_bff: httpx.AsyncClient, request_id: UUID) -> ActionRequestView:
    """Check a request waited for the operator, then close it so nothing lingers undecided."""
    receipt = _bff_request(await operator_bff.get(f"/actions/{request_id}"))
    assert receipt.state is ActionState.DECISION_PENDING
    if receipt.decision is not None:
        pytest.fail("Request expected on the human path already has a Decision", pytrace=False)
    denial = DecisionInput(verdict=Verdict.DENY, expected_version=receipt.version, idempotency_key=str(uuid4()))
    denied = await operator_bff.post(f"/actions/{request_id}/decision", json=denial.model_dump(mode="json"))
    assert denied.status_code == HTTPStatus.OK
    return receipt


async def test_policy_binding_auto_approves_the_bound_sandbox(
    operator_bff: httpx.AsyncClient,
    client: Client,
    sandbox: Sandboxes,
    harness: runner_pb2.Harness,
    model: str,
    policy_objects: PolicyObjects,
) -> None:
    """A set and a binding written through the Kubernetes API for the Sandbox this test launches:
    a matching echo is auto-approved and executed with the binding and set on its Decision, an
    argument miss waits for the operator, and once the Policy Controller has deleted the expired
    binding so does a match."""
    view = await sandbox(f"accept-policy-{harness}")
    set_name, binding_name = policy_objects.bind(
        view,
        "echo",
        [
            {
                "type": "argument_schema",
                "actions": {"everything": ["echo"]},
                "schema": {
                    "type": "object",
                    "required": ["message"],
                    "properties": {"message": {"type": "string", "maxLength": 200}},
                    "additionalProperties": False,
                },
            }
        ],
    )
    agent = await Agent.open(client, sandbox=view.name, harness=harness, model=model)

    marker = f"MCP-POLICY-{uuid4()}"
    approved = await agent.run(_execute_echo("everything", marker))
    report = approved.report(McpReport)
    assert report.output == {"content": [f"Echo: {marker}"]}, approved.transcript
    receipt = _bff_request(await operator_bff.get(f"/actions/{report.request_id}"))
    assert receipt.state is ActionState.SUCCEEDED
    assert receipt.decision is not None
    assert receipt.decision.provider == PROVIDER_NAME
    evidence = receipt.decision.policy_evidence
    assert evidence is not None
    # Our Sandbox kind brings a PolicyBinding of its own, so the evidence may name more than ours.
    assert binding_name in {binding.name for binding in evidence.bindings}
    assert set_name in {policy_set.name for policy_set in evidence.policy_sets}
    assert evidence.matched == MatchedPolicy(
        namespace=policy_objects.namespace,
        policy_set=set_name,
        source=PolicySource.RULES,
        index=0,
        type=PolicyKind.ARGUMENT_SCHEMA,
    )
    assert receipt.execution is not None
    assert receipt.execution.state is ExecutionState.SUCCEEDED

    too_long = f"MCP-POLICY-{uuid4()}-" + "x" * 201
    missed = await agent.run(_queue_echo(too_long))
    await _deny_pending(operator_bff, missed.report(PendingMcpReport).request_id)

    policy_objects.expire_binding(binding_name)
    await _wait_unbound(operator_bff, view, binding_name)
    after_expiry = f"MCP-POLICY-{uuid4()}"
    expired = await agent.run(_queue_echo(after_expiry))
    await _deny_pending(operator_bff, expired.report(PendingMcpReport).request_id)


async def _wait_unbound(operator_bff: httpx.AsyncClient, sandbox: SandboxView, binding_name: str) -> None:
    """Until the Action Service no longer resolves `binding_name` for the Sandbox's ServiceAccount:
    a deleted binding has no condition left to acknowledge, so ask the service itself."""
    account = sandbox.service_account
    for attempt in Retrying(
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
            if binding_name in {binding.name for binding in policy.bindings}:
                raise PolicyBindingPresentError(f"{account} still resolves {binding_name}")


if __name__ == "__main__":
    pytest_bazel.main()
