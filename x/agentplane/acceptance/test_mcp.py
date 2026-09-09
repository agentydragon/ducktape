"""Real staging agents discover, submit, poll and report an MCP-backed Action."""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from http import HTTPStatus
from typing import Literal
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_bazel
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

from x.agentplane.acceptance.agent import Agent
from x.agentplane.acceptance.operator_login import (
    LoginBlockedError,
    OperatorCredentials,
    app_origin,
    login_operator,
    read_operator_credentials,
)
from x.agentplane.action_service.models import (
    ActionEventView,
    ActionRequestView,
    ActionState,
    DecisionInput,
    ExecutionState,
    Verdict,
)
from x.agentplane.app.api import Provider
from x.agentplane.app.client import Client
from x.agentplane.app.inventory import SandboxView


class McpReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    state: Literal["succeeded"]
    output: dict[str, JsonValue]


class PendingMcpReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    state: Literal["decision_pending"]


async def test_agent_executes_mcp_action(
    client: Client, sandbox: Callable[..., Awaitable[SandboxView]], provider: Provider, model: str
) -> None:
    view = await sandbox(f"accept-mcp-{provider}")
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
                await login_operator(http, operator_credentials)
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
    # FixtureDecisionProvider auto-allows only messages of at most 200 characters.
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


if __name__ == "__main__":
    pytest_bazel.main()
