"""Real testing agents discover, submit, poll and report an MCP-backed Action."""

import json
import logging
import os
import re
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Any, Literal, cast
from urllib.parse import parse_qs
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_bazel
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter
from tenacity import Retrying, retry_if_exception_type, stop_after_delay, wait_fixed

from x.agentplane.acceptance.agent import Agent
from x.agentplane.acceptance.conftest import NAMESPACE, TESTING_NAMESPACE
from x.agentplane.acceptance.operator_login import (
    LoginBlockedError,
    OperatorCredentials,
    app_origin,
    login_operator,
    read_operator_credentials,
)
from x.agentplane.action_service.mcp_linkage import McpLinkageStatus, McpLinkageView
from x.agentplane.action_service.models import (
    ActionEventView,
    ActionRequestView,
    ActionState,
    DecisionInput,
    ExecutionState,
    MatchedPolicy,
    PolicyKind,
    Verdict,
)
from x.agentplane.action_service.policies.resources import (
    BINDINGS_PLURAL,
    GROUP,
    POLICY_SETS_PLURAL,
    READY_CONDITION,
    VERSION,
)
from x.agentplane.action_service.policy_evaluation import PROVIDER_NAME
from x.agentplane.app.api import Provider
from x.agentplane.app.client import Client
from x.agentplane.app.inventory import SandboxView

# A status write follows the informer's next watch event; the bound covers a relist after a
# dropped watch, not a healthy round trip.
POLICY_READY_SECONDS = 120.0
DEX_OAUTH_PATH = re.compile(r"/dex/(auth|auth/local|auth/local/login|approval|callback)/?")


class McpReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    state: Literal["succeeded"]
    output: dict[str, JsonValue]


class PendingMcpReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    state: Literal["decision_pending"]


def operator_idp() -> Literal["authentik", "dex"]:
    value = os.environ.get("AGENTPLANE_ACCEPTANCE_IDP", "dex")
    if value not in {"authentik", "dex"}:
        pytest.fail("BLOCKED: AGENTPLANE_ACCEPTANCE_IDP must be authentik or dex", pytrace=False)
    return cast(Literal["authentik", "dex"], value)


class KubectlError(Exception):
    """kubectl refused; its own message says why."""


class PolicyObjectNotReadyError(Exception):
    """The Action Service has not yet judged the object's current generation."""


@dataclass
class PolicyObjects:
    """ActionPolicySets and ActionPolicyBindings this test writes with the caller's kubeconfig,
    deleted whatever the test did. RBAC on the two kinds in the namespace is what gates this."""

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

    def create_policy_set(self, name: str, auto_approve_if: list[dict[str, Any]]) -> str:
        return self._create("ActionPolicySet", POLICY_SETS_PLURAL, name, {"autoApproveIf": auto_approve_if})

    def create_binding(self, name: str, *, sandbox: SandboxView, policy_sets: list[str]) -> str:
        return self._create(
            "ActionPolicyBinding",
            BINDINGS_PLURAL,
            name,
            {"subject": {"sandbox": {"name": sandbox.name, "uid": str(sandbox.uid)}}, "policySets": policy_sets},
        )

    def expire_binding(self, name: str) -> None:
        past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        self._kubectl("patch", BINDINGS_PLURAL, name, "--type=merge", "-p", json.dumps({"spec": {"expiresAt": past}}))

    def wait_ready(self, plural: str, name: str) -> None:
        """Until the object's Ready condition is True for its current generation: the Action
        Service's own acknowledgement that it holds the spec as written."""
        for attempt in Retrying(
            stop=stop_after_delay(POLICY_READY_SECONDS),
            wait=wait_fixed(1),
            retry=retry_if_exception_type(PolicyObjectNotReadyError),
            reraise=True,
        ):
            with attempt:
                obj = json.loads(self._kubectl("get", plural, name, "-o", "json"))
                ready = [c for c in obj.get("status", {}).get("conditions", []) if c["type"] == READY_CONDITION]
                if not ready or ready[0].get("observedGeneration") != obj["metadata"]["generation"]:
                    raise PolicyObjectNotReadyError(f"{plural}/{name} generation {obj['metadata']['generation']}")
                if ready[0]["status"] != "True":
                    pytest.fail(f"{plural}/{name} was refused: {ready[0]['message']}", pytrace=False)

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
    client: Client,
    sandbox: Callable[..., Awaitable[SandboxView]],
    provider: Provider,
    model: str,
    policy_objects: PolicyObjects,
) -> None:
    view = await sandbox(f"accept-mcp-{provider}")
    suffix = uuid4().hex[:8]
    set_name = policy_objects.create_policy_set(
        f"accept-exact-echo-{suffix}", [{"type": "exact_actions", "actions": {"everything": ["echo"]}}]
    )
    binding_name = policy_objects.create_binding(f"{view.name}-exact-{suffix}", sandbox=view, policy_sets=[set_name])
    policy_objects.wait_ready(POLICY_SETS_PLURAL, set_name)
    policy_objects.wait_ready(BINDINGS_PLURAL, binding_name)
    agent = await Agent.open(client, sandbox=view.name, provider=provider, model=model)
    marker = f"MCP0-{uuid4()}"
    turn = await agent.run(f"""
This is an Agentplane infrastructure test. Use the Actions Service to execute the echo Action in
the everything group with message {marker}. Wait for its terminal result. Return only JSON with
exactly request_id, state, and output, where output is the request's execution result. Do not
infer or fabricate a result.
""")
    report = turn.report(McpReport)
    assert report.output == {"content": [f"Echo: {marker}"]}, turn.transcript


async def _follow_dex_authorization(
    authorization_url: str, operator_bff: httpx.AsyncClient, operator_credentials: OperatorCredentials
) -> tuple[str, str]:
    """Follow the MCP authorization redirect with only the Dex cookies from app login.

    The app session stays on `operator_bff`; this client carries only host-scoped Dex cookies and
    refuses to follow anything except the known Dex endpoints and the exact MCP callback.
    """
    __tracebackhide__ = True
    try:
        authorization = httpx.URL(authorization_url)
        issuer = httpx.URL(operator_credentials.issuer.get_secret_value())
        app = app_origin(str(operator_bff.base_url))
    except (httpx.InvalidURL, ValueError):
        pytest.fail("BLOCKED: Dex authorization URL or operator issuer is invalid", pytrace=False)
    dex = issuer.copy_with(path="/", query=None, fragment=None)
    if (
        authorization.copy_with(path="/", query=None, fragment=None) != dex
        or not DEX_OAUTH_PATH.fullmatch(authorization.path)
    ):
        pytest.fail("BLOCKED: MCP authorization did not target the configured Dex origin", pytrace=False)

    async with httpx.AsyncClient(follow_redirects=False, timeout=30) as browser:
        copied = 0
        for cookie in operator_bff.cookies.jar:
            domain = cookie.domain
            value = cookie.value
            if domain is None or value is None or domain.lstrip(".") != dex.host:
                continue
            browser.cookies.set(cookie.name, value, domain=domain, path=cookie.path or "/")
            copied += 1
        if copied == 0:
            pytest.fail("BLOCKED: app login did not establish a Dex session cookie", pytrace=False)

        response = await browser.get(authorization)
        for _ in range(12):
            if not response.is_redirect:
                # A 200 Dex login form means the existing app login did not establish a reusable
                # Dex session. Never submit the operator password in this separate flow.
                pytest.fail("BLOCKED: Dex required a fresh interactive login during MCP linkage", pytrace=False)
            location = response.headers.get("location")
            if not location:
                pytest.fail("BLOCKED: Dex returned a redirect without a location", pytrace=False)
            target = response.url.join(location)
            if target.scheme != "https" or target.userinfo or target.fragment:
                pytest.fail("BLOCKED: Dex returned an unsafe MCP authorization redirect", pytrace=False)
            target_origin = target.copy_with(path="/", query=None, fragment=None)
            if target_origin == dex and DEX_OAUTH_PATH.fullmatch(target.path):
                response = await browser.get(target)
                continue
            if target_origin == app and target.path == "/mcp-linkage/callback":
                query = parse_qs(target.query.decode())
                if "state" not in query or "code" not in query:
                    pytest.fail("BLOCKED: Dex callback omitted OAuth state or code", pytrace=False)
                return query["state"][0], query["code"][0]
            pytest.fail("BLOCKED: Dex returned a redirect outside the MCP callback", pytrace=False)
    pytest.fail("BLOCKED: Dex authorization exceeded its redirect bound", pytrace=False)


async def test_operator_links_oauth_mcp_server(
    operator_bff: httpx.AsyncClient,
    operator_credentials: OperatorCredentials,
    client: Client,
    sandbox: Callable[..., Awaitable[SandboxView]],
    provider: Provider,
    model: str,
    policy_objects: PolicyObjects,
) -> None:
    """Links the `example` MCP server through Dex and executes its protected tool."""
    server_id = "example"
    start_response = await operator_bff.post(f"/mcp-servers/{server_id}/linkage/start", json={"scopes": []})
    assert start_response.status_code == HTTPStatus.OK, start_response.text
    authorization_url = start_response.json()["authorization_url"]

    state, code = await _follow_dex_authorization(authorization_url, operator_bff, operator_credentials)

    callback = await operator_bff.get("/mcp-linkage/callback", params={"state": state, "code": code})
    assert callback.status_code == HTTPStatus.SEE_OTHER, callback.text

    linkage_response = await operator_bff.get(f"/mcp-servers/{server_id}/linkage")
    assert linkage_response.status_code == HTTPStatus.OK
    linkage = McpLinkageView.model_validate(linkage_response.json())
    assert linkage.status is McpLinkageStatus.LINKED

    view = await sandbox(f"accept-oauth-mcp-{provider}")
    suffix = uuid4().hex[:8]
    set_name = policy_objects.create_policy_set(
        f"accept-oauth-example-{suffix}", [{"type": "exact_actions", "actions": {"example": ["echo"]}}]
    )
    binding_name = policy_objects.create_binding(f"{view.name}-exact-{suffix}", sandbox=view, policy_sets=[set_name])
    policy_objects.wait_ready(POLICY_SETS_PLURAL, set_name)
    policy_objects.wait_ready(BINDINGS_PLURAL, binding_name)
    agent = await Agent.open(client, sandbox=view.name, provider=provider, model=model)
    marker = f"MCP-OAUTH-{uuid4()}"
    turn = await agent.run(f"""
This is an Agentplane infrastructure test. Use the Actions Service to execute the echo Action in
the example group with message {marker}. Wait for its terminal result. Return only JSON with
exactly request_id, state, and output, where output is the request's execution result. Do not
infer or fabricate a result.
""")
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
            except (httpx.HTTPError, httpx.InvalidURL, ValueError, KeyError, TypeError):
                pytest.fail("BLOCKED: OIDC transport or response invalid; auth details withheld", pytrace=False)
            http.headers["Origin"] = origin
            # An absent request exercises federation without listing other requests.
            probe = await http.get(f"/actions/{uuid4()}")
            if probe.status_code != HTTPStatus.NOT_FOUND or probe.json() != {
                "detail": "Action Service rejected the request"
            }:
                pytest.fail(
                    "BLOCKED: BFF Action federation preflight refused; verify dedicated operator target identity",
                    pytrace=False,
                )
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


def _bff_events(response: httpx.Response) -> list[ActionEventView]:
    """Withhold malformed event bodies just as for request receipts."""
    __tracebackhide__ = True
    try:
        return TypeAdapter(list[ActionEventView]).validate_json(response.content)
    except ValueError:
        pytest.fail("BFF returned invalid ActionEventView history; response body withheld", pytrace=False)


@pytest.mark.parametrize("verdict", list(Verdict))
async def test_agent_mcp_bff_decision(
    operator_bff: httpx.AsyncClient,
    operator_credentials: OperatorCredentials,
    client: Client,
    sandbox: Callable[..., Awaitable[SandboxView]],
    provider: Provider,
    model: str,
    verdict: Verdict,
) -> None:
    view = await sandbox(f"accept-mcp-{provider}-{verdict}")
    agent = await Agent.open(client, sandbox=view.name, provider=provider, model=model)
    # Nothing binds this Sandbox to a policy set, so every Action of its waits for the operator.
    marker = f"MCP-BFF-{uuid4()}-" + "x" * 201
    submitted = await agent.run(f"""
This is an Agentplane infrastructure test. Request the echo Action in the everything group with
message {marker}. This request requires operator approval. Confirm that it is queued for approval;
return only JSON with exactly request_id and state, where state must be decision_pending. Do not
wait for an operator decision in this turn.
""")
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
    if decided.decision.issuer != (
        f"{operator_credentials.issuer.get_secret_value()}:{operator_credentials.subject.get_secret_value()}"
    ):
        pytest.fail("Decision operator identity differs from the dedicated Secret", pytrace=False)
    assert decided.decision.idempotency_key == decision.idempotency_key
    expected_state = ActionState.SUCCEEDED if verdict is Verdict.ALLOW else ActionState.DENIED
    expected_result = {"content": [f"Echo: {marker}"]} if verdict is Verdict.ALLOW else None
    if verdict is Verdict.ALLOW:
        assert decided.state is ActionState.ALLOWED
        assert decided.execution is not None
    else:
        assert decided.state is ActionState.DENIED
        assert decided.execution is None

    completed = await agent.run(f"""
Resume Action request {request_id}. The operator has decided {verdict.value}. Determine its terminal
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
    assert terminal.caller_principal == pending.caller_principal
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

    events_response = await operator_bff.get(f"{path}/events", params={"after_sequence": 0})
    assert events_response.status_code == HTTPStatus.OK
    events = _bff_events(events_response)
    assert [event.sequence for event in events] == list(range(1, terminal.version + 1))
    assert [event.state for event in events] == (
        [
            ActionState.DECISION_PENDING,
            ActionState.ALLOWED,
            ActionState.DISPATCHING,
            ActionState.RUNNING,
            ActionState.SUCCEEDED,
        ]
        if verdict is Verdict.ALLOW
        else [ActionState.DECISION_PENDING, ActionState.DENIED]
    )

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
    sandbox: Callable[..., Awaitable[SandboxView]],
    provider: Provider,
    model: str,
    policy_objects: PolicyObjects,
) -> None:
    """A set and a binding written through the Kubernetes API for the Sandbox this test launches:
    a matching echo is auto-approved and executed with the binding and set on its Decision, an
    argument miss waits for the operator, and once the binding has expired so does a match."""
    view = await sandbox(f"accept-policy-{provider}")
    suffix = uuid4().hex[:8]
    set_name = policy_objects.create_policy_set(
        f"accept-echo-{suffix}",
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
    binding_name = policy_objects.create_binding(f"{view.name}-echo-{suffix}", sandbox=view, policy_sets=[set_name])
    policy_objects.wait_ready(POLICY_SETS_PLURAL, set_name)
    policy_objects.wait_ready(BINDINGS_PLURAL, binding_name)
    agent = await Agent.open(client, sandbox=view.name, provider=provider, model=model)

    marker = f"MCP-POLICY-{uuid4()}"
    approved = await agent.run(f"""
This is an Agentplane infrastructure test. Use the Actions Service to execute the echo Action in
the everything group with message {marker}. Wait for its terminal result. Return only JSON with
exactly request_id, state, and output, where output is the request's execution result. Do not
infer or fabricate a result.
""")
    report = approved.report(McpReport)
    assert report.output == {"content": [f"Echo: {marker}"]}, approved.transcript
    receipt = _bff_request(await operator_bff.get(f"/actions/{report.request_id}"))
    assert receipt.state is ActionState.SUCCEEDED
    assert receipt.decision is not None
    assert receipt.decision.provider == PROVIDER_NAME
    evidence = receipt.decision.policy_evidence
    assert evidence is not None
    assert [binding.name for binding in evidence.bindings] == [binding_name]
    assert [policy_set.name for policy_set in evidence.policy_sets] == [set_name]
    assert evidence.matched == MatchedPolicy(
        namespace=policy_objects.namespace,
        policy_set=set_name,
        source="autoApproveIf",
        index=0,
        type=PolicyKind.ARGUMENT_SCHEMA,
    )
    assert receipt.execution is not None
    assert receipt.execution.state is ExecutionState.SUCCEEDED

    too_long = f"MCP-POLICY-{uuid4()}-" + "x" * 201
    missed = await agent.run(f"""
This is an Agentplane infrastructure test. Request the echo Action in the everything group with
message {too_long}. This request requires operator approval. Confirm that it is queued for approval;
return only JSON with exactly request_id and state, where state must be decision_pending. Do not
wait for an operator decision in this turn.
""")
    await _deny_pending(operator_bff, missed.report(PendingMcpReport).request_id)

    policy_objects.expire_binding(binding_name)
    policy_objects.wait_ready(BINDINGS_PLURAL, binding_name)
    after_expiry = f"MCP-POLICY-{uuid4()}"
    expired = await agent.run(f"""
This is an Agentplane infrastructure test. Request the echo Action in the everything group with
message {after_expiry}. This request requires operator approval. Confirm that it is queued for
approval; return only JSON with exactly request_id and state, where state must be decision_pending.
Do not wait for an operator decision in this turn.
""")
    await _deny_pending(operator_bff, expired.report(PendingMcpReport).request_id)


if __name__ == "__main__":
    pytest_bazel.main()
