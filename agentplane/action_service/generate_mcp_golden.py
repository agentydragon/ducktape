"""Golden vectors that pin the Go MCP server (`agentplane/mcp_edge/mcpserver`) to this service's `/mcp` tools.

The Go server answers the same tools from the Action Service's workload API (`action_service.proto`) instead of from
this process. Each call vector is one tool call: its arguments, what each core RPC the Go server makes answers (in
protojson, or the gRPC status code and message that stand for the exception this service raises), and the
`CallToolResult` this service's own tool function answers when its collaborators return the same records. The listing vectors are the
eight tools as FastMCP lists them, the server's instructions, and the direct tools FastMCP lists for an external caller.

Regenerate both files with
`bb run //agentplane/action_service:generate_mcp_golden_bin -- "$PWD/agentplane/mcp_edge/mcpserver"`.
"""

import asyncio
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

from fastmcp import Client, FastMCP
from fastmcp.exceptions import FastMCPError
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.types import CallToolResult, ImageContent, TextContent, Tool, ToolAnnotations
from pydantic import JsonValue

from agentplane.action_service.caller_auth import CallerToken, CallerTokenVerifier
from agentplane.action_service.catalog import (
    DIRECT_TOOL_SEPARATOR,
    ActionCatalog,
    ActionDefinition,
    ActionGroup,
    ActionIdentity,
    ActionUnavailableError,
    McpExecutorBinding,
)
from agentplane.action_service.mcp_frontend import PolicyField, RequestField, create_server
from agentplane.action_service.models import (
    ActionEventView,
    ActionRequestInput,
    ActionRequestView,
    ActionState,
    BindingEvidence,
    CallerPrincipal,
    CancellationOutcome,
    CancellationResult,
    DecisionView,
    ExecutionState,
    ExecutionView,
    ExternalGrantProvenance,
    MatchedPolicy,
    MatchedRepository,
    OperatorPrincipal,
    PolicyEvidence,
    PolicyKind,
    PolicySetEvidence,
    PolicySource,
    Principal,
    Verdict,
)
from agentplane.action_service.policy_view import (
    ArgumentSchemaView,
    CallerActionPolicyView,
    CallerBindingView,
    EffectivePolicyView,
    ExactActionsView,
    GitHubPublicRepositoryView,
    GitHubRepositoryView,
    HomeAssistantEntityControlView,
    PolicyView,
)
from agentplane.action_service.sandbox.binding import PodExecutorBinding
from agentplane.action_service.service import (
    ActionService,
    InvalidActionArgumentsError,
    UndecidedRequestError,
    UnsupportedActionError,
)
from agentplane.action_service.store import ActionConflictError, ActionNotFoundError
from agentplane.action_service.updates import ActionUpdateBroker
from agentplane.subjects import ServiceAccountRef

Json = dict[str, JsonValue]

CALLER = ServiceAccountRef(namespace="test-namespace", name="test-caller")
OPERATOR = OperatorPrincipal(issuer="https://test-issuer.example", subject="test-operator")
MAX_WAIT_SECONDS = 30.0
CREATED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
DECIDED = datetime(2026, 1, 2, 3, 4, 6, 120000, tzinfo=UTC)
STARTED = datetime(2026, 1, 2, 3, 4, 7, 5, tzinfo=UTC)
COMPLETED = datetime(2026, 1, 2, 3, 5, 0, 999999, tzinfo=UTC)
# The caller on an external Connection, the only kind the Go server serves.
CALLER_TOKEN = CallerToken(
    token="test-token",
    client_id="test-client",
    scopes=[],
    principal=CallerPrincipal(account=CALLER),
    external_grant=ExternalGrantProvenance(
        caller=CALLER,
        issuer="https://test-issuer.example",
        client_id="test-client",
        connection_id=UUID(int=0xC1),
        grant_id=UUID(int=0xC2),
        revision=1,
    ),
)


# Every JSON kind, with characters JSON encoders escape differently, as arguments carried through
# `google.protobuf.Struct`.
EVERY_JSON_KIND: Json = {
    "message": 'line one\nline two\ttabbed "quoted" back\\slash </script> & é 名 🚀 \u2028 \x7f \x01',
    "int": 42,
    "negative": -7,
    "zero": 0,
    "fraction": 1.5,
    "negative fraction": -0.25,
    "small": 1.5e-7,
    "tiny": 0.0001,
    "large": 1e20,
    "precise": 123.456789012345,
    "true": True,
    "false": False,
    "null": None,
    "empty object": {},
    "empty list": [],
    "list": [1, "two", None, {"three": [3.5]}],
    "nested": {"b": {"aa": [{"c": "d"}], "a": 1}},
    "": "empty key",
}


def catalog() -> ActionCatalog:
    return ActionCatalog(
        groups={
            "test-mcp": ActionGroup(
                title="Test MCP",
                description="test-mcp-description",
                executor=McpExecutorBinding(
                    description="test-mcp-executor", config={"token": "test-backend-secret-never-listed"}
                ),
                actions={
                    "alpha": ActionDefinition(
                        description="test-alpha-description é 🚀",
                        input_schema={
                            "type": "object",
                            "properties": {
                                "message": {"type": "string", "description": "test message", "default": None},
                                "count": {"type": "number", "minimum": 0.5, "maximum": 10},
                            },
                            "required": ["message"],
                            "additionalProperties": False,
                        },
                    ),
                    "beta": ActionDefinition(
                        description="test-beta-description",
                        title="Beta",
                        annotations=ToolAnnotations(read_only_hint=True),
                    ),
                },
                direct_tools=frozenset({"alpha", "beta"}),
            ),
            "test-sandbox": ActionGroup(
                title="Test sandbox",
                description="test-sandbox-description",
                executor=PodExecutorBinding(
                    description="test-sandbox-executor", namespace="test-namespace", templates={"test": "test box"}
                ),
                actions={"exec": ActionDefinition(description="test-exec", input_schema={"type": "object"})},
                direct_tools=frozenset({"exec"}),
            ),
            # An unavailable group lists no Actions, as the core lists it.
            "test-down": ActionGroup(
                title="Test down",
                description="test-down-description",
                executor=McpExecutorBinding(description="test-down-executor"),
                available=False,
                direct_tools=frozenset({"test-gone"}),
            ),
        }
    )


def enum(prefix: str, value: str) -> str:
    return f"{prefix}_{value.upper()}"


def timestamp(at: datetime | None) -> str | None:
    if at is None:
        return None
    assert at.tzinfo is UTC
    fraction = f".{at.microsecond:06d}" if at.microsecond else ""
    return at.strftime("%Y-%m-%dT%H:%M:%S") + fraction + "Z"


def present(**fields: object) -> Json:
    """A protojson message: a field that is absent is left out."""
    return {key: cast(JsonValue, value) for key, value in fields.items() if value is not None}


def proto_account(account: ServiceAccountRef) -> Json:
    return {"namespace": account.namespace, "name": account.name}


def proto_operator(operator: OperatorPrincipal | None) -> Json | None:
    return None if operator is None else {"issuer": operator.issuer, "subject": operator.subject}


def proto_evidence(evidence: PolicyEvidence | None) -> Json | None:
    if evidence is None:
        return None
    matched = evidence.matched
    repository = matched.repository
    return {
        "bindings": [
            {"namespace": b.namespace, "name": b.name, "resourceVersion": b.resource_version} for b in evidence.bindings
        ],
        "policySets": [
            {"namespace": s.namespace, "name": s.name, "generation": str(s.generation)} for s in evidence.policy_sets
        ],
        "matched": present(
            namespace=matched.namespace,
            policySet=matched.policy_set,
            source=enum("POLICY_SOURCE", matched.source),
            index=matched.index,
            type=enum("POLICY_KIND", matched.type),
            repository=None
            if repository is None
            else {
                "owner": repository.owner,
                "repository": repository.repository,
                "confirmedPublic": repository.confirmed_public,
            },
        ),
    }


def proto_decision(decision: DecisionView | None) -> Json | None:
    if decision is None:
        return None
    return present(
        id=str(decision.id),
        verdict=enum("VERDICT", decision.verdict),
        provider=decision.provider,
        operator=proto_operator(decision.operator),
        decisionNote=decision.decision_note,
        reasonCode=decision.reason_code,
        reasonDescription=decision.reason_description,
        policyEvidence=proto_evidence(decision.policy_evidence),
        idempotencyKey=decision.idempotency_key,
        decidedAt=timestamp(decision.decided_at),
    )


def proto_execution(execution: ExecutionView | None) -> Json | None:
    if execution is None:
        return None
    return present(
        id=str(execution.id),
        state=enum("EXECUTION_STATE", execution.state),
        result=execution.result,
        error=execution.error,
        createdAt=timestamp(execution.created_at),
        startedAt=timestamp(execution.started_at),
        completedAt=timestamp(execution.completed_at),
        reconciledAt=timestamp(execution.reconciled_at),
    )


def proto_request(view: ActionRequestView) -> Json:
    return present(
        id=str(view.id),
        idempotencyKey=view.idempotency_key,
        action=view.action.model_dump(),
        arguments=view.arguments,
        title=view.title,
        description=view.description,
        caller=proto_account(view.caller),
        state=enum("ACTION_STATE", view.state),
        version=str(view.version),
        createdAt=timestamp(view.created_at),
        updatedAt=timestamp(view.updated_at),
        decision=proto_decision(view.decision),
        execution=proto_execution(view.execution),
    )


def proto_event(event: ActionEventView) -> Json:
    actor: Json | None = None
    match event.actor:
        case CallerPrincipal(account=account):
            actor = {"caller": proto_account(account)}
        case OperatorPrincipal():
            actor = {"operator": proto_operator(event.actor)}
        case None:
            pass
    return present(
        sequence=event.sequence, state=enum("ACTION_STATE", event.state), at=timestamp(event.at), actor=actor
    )


def proto_action(identity: ActionIdentity, definition: ActionDefinition) -> Json:
    annotations = definition.annotations
    return present(
        group=identity.group,
        name=identity.name,
        description=definition.description,
        inputSchema=definition.input_schema,
        title=definition.title,
        annotations=None
        if annotations is None
        else present(
            readOnlyHint=annotations.read_only_hint,
            destructiveHint=annotations.destructive_hint,
            idempotentHint=annotations.idempotent_hint,
            openWorldHint=annotations.open_world_hint,
        ),
    )


def proto_groups(actions: ActionCatalog) -> Json:
    return {
        "actionGroups": [
            present(
                key=key,
                title=group.title,
                description=group.description,
                executorKind=enum("EXECUTOR_KIND", group.executor.kind),
                executorDescription=group.executor.description,
                available=group.available,
                actions=[
                    proto_action(ActionIdentity(group=key, name=name), group.actions[name])
                    for name in sorted(group.actions)
                ],
                directTools=sorted(group.direct_tools or ()),
            )
            for key, group in sorted(actions.groups.items())
        ]
    }


def proto_policy_kind(view: EffectivePolicyView) -> Json:
    policy = view.policy
    actions: JsonValue = {group: {"values": list(names)} for group, names in policy.actions.items()}
    match policy:
        case ExactActionsView():
            return {"exactActions": {"actions": actions}}
        case ArgumentSchemaView(argument_schema=schema):
            return {"argumentSchema": {"actions": actions, "argumentSchema": schema}}
        case GitHubRepositoryView(owner=owner, repository=repository):
            return {"githubRepository": {"actions": actions, "owner": owner, "repository": repository}}
        case GitHubPublicRepositoryView():
            return {"githubPublicRepository": {"actions": actions}}
        case HomeAssistantEntityControlView(entities=entities):
            return {
                "homeAssistantEntityControl": {
                    "actions": actions,
                    "entities": {entity: {"values": list(services)} for entity, services in entities.items()},
                }
            }


TEST_SET_GENERATION = 9


def proto_policy(view: CallerActionPolicyView) -> Json:
    return {
        "actionPolicy": {
            "subject": proto_account(view.subject),
            "bindings": [
                present(
                    name=b.name,
                    expiresAt=timestamp(b.expires_at),
                    # A caller's view names each set; the core also says which generation of it resolved.
                    policySets=[{"name": name, "generation": str(TEST_SET_GENERATION)} for name in b.policy_sets],
                )
                for b in view.bindings
            ],
            "synced": view.synced,
            "autoApproveIf": [
                {
                    "binding": p.binding,
                    "policySet": p.policy_set,
                    "source": enum("POLICY_SOURCE", p.source),
                    "index": p.index,
                    "policy": proto_policy_kind(p),
                }
                for p in view.auto_approve_if
            ],
        }
    }


def request(
    number: int,
    state: ActionState,
    *,
    group: str = "test-mcp",
    name: str = "alpha",
    description: str | None = None,
    arguments: Json | None = None,
    decision: DecisionView | None = None,
    execution: ExecutionView | None = None,
) -> ActionRequestView:
    return ActionRequestView(
        id=UUID(int=number),
        idempotency_key=f"test-key-{number}",
        action=ActionIdentity(group=group, name=name),
        arguments=arguments or {},
        title=f"test title {number}",
        description=description,
        caller=CALLER,
        state=state,
        version=number,
        created_at=CREATED,
        updated_at=COMPLETED if execution is not None and execution.completed_at is not None else DECIDED,
        decision=decision,
        execution=execution,
    )


def operator_decision(verdict: Verdict, note: str | None) -> DecisionView:
    return DecisionView(
        id=UUID(int=0xD1),
        verdict=verdict,
        provider="human_operator",
        operator=OPERATOR,
        decision_note=note,
        reason_code=None,
        reason_description=None,
        policy_evidence=None,
        idempotency_key="test-decision-key",
        decided_at=DECIDED,
    )


def policy_decision(verdict: Verdict, reason: str | None, *, repository: MatchedRepository | None) -> DecisionView:
    return DecisionView(
        id=UUID(int=0xD2),
        verdict=verdict,
        provider="action_policy_set",
        operator=None,
        decision_note=None,
        reason_code="test_reason_code",
        reason_description=reason,
        policy_evidence=PolicyEvidence(
            bindings=[BindingEvidence(namespace="test-namespace", name="test-binding", resource_version="7")],
            policy_sets=[PolicySetEvidence(namespace="test-namespace", name="test-set", generation=4)],
            matched=MatchedPolicy(
                namespace="test-namespace",
                policy_set="test-set",
                source=PolicySource.RULES,
                index=2,
                type=PolicyKind.GITHUB_REPOSITORY if repository is not None else PolicyKind.EXACT_ACTIONS,
                repository=repository,
            ),
        ),
        idempotency_key="test-policy-decision-key",
        decided_at=DECIDED,
    )


def execution(
    state: ExecutionState,
    *,
    result: JsonValue = None,
    error: Json | None = None,
    started: bool = True,
    completed: bool = True,
    reconciled: bool = False,
) -> ExecutionView:
    return ExecutionView(
        id=UUID(int=0xE1),
        state=state,
        result=result,
        error=error,
        created_at=DECIDED,
        started_at=STARTED if started else None,
        completed_at=COMPLETED if completed else None,
        reconciled_at=COMPLETED if reconciled else None,
    )


MCP_ANSWER = CallToolResult(
    content=[
        TextContent(type="text", text="test caption é"),
        ImageContent(type="image", data="dGVzdC1pbWFnZS1ieXRlcw==", mime_type="image/png"),
    ],
    structured_content={"width": 1, "height": 2.5},
    is_error=True,
).model_dump(mode="json", by_alias=True, exclude_none=True)

SANDBOX_ANSWER: Json = {
    "exit": {"kind": "exited", "exit_code": 1},
    "stdout": "test output é\n",
    "stderr": "test-missing-file",
    "duration_seconds": 0.5,
}


def requests() -> dict[str, ActionRequestView]:
    allowed = operator_decision(Verdict.ALLOW, "test note é")
    return {
        "decision pending": request(1, ActionState.DECISION_PENDING, arguments=EVERY_JSON_KIND),
        "allowed, not dispatched": request(
            2,
            ActionState.ALLOWED,
            description="test description 🚀",
            arguments={"message": "test"},
            decision=allowed,
            execution=execution(ExecutionState.PENDING_DISPATCH, started=False, completed=False),
        ),
        "dispatching": request(
            3,
            ActionState.DISPATCHING,
            decision=allowed,
            execution=execution(ExecutionState.DISPATCHING, started=False, completed=False),
        ),
        "running": request(
            4,
            ActionState.RUNNING,
            group="test-sandbox",
            name="exec",
            decision=policy_decision(Verdict.ALLOW, "test allowed", repository=None),
            execution=execution(ExecutionState.RUNNING, completed=False),
        ),
        "succeeded over MCP": request(
            5,
            ActionState.SUCCEEDED,
            decision=policy_decision(
                Verdict.ALLOW,
                "test allowed",
                repository=MatchedRepository(owner="test-owner", repository="test-repo", confirmed_public=False),
            ),
            execution=execution(ExecutionState.SUCCEEDED, result=MCP_ANSWER),
        ),
        "succeeded in a sandbox": request(
            6,
            ActionState.SUCCEEDED,
            group="test-sandbox",
            name="exec",
            decision=allowed,
            execution=execution(ExecutionState.SUCCEEDED, result=SANDBOX_ANSWER),
        ),
        "failed": request(
            7,
            ActionState.FAILED,
            group="test-sandbox",
            name="exec",
            decision=allowed,
            execution=execution(
                ExecutionState.FAILED,
                error={"kind": "sandbox_unavailable", "message": 'test box é 🚀 "not" ready\n', "retry": 1.5},
            ),
        ),
        "execution unknown": request(
            8,
            ActionState.EXECUTION_UNKNOWN,
            decision=allowed,
            execution=execution(
                ExecutionState.EXECUTION_UNKNOWN,
                error={"kind": "execution_outcome_unknown", "message": "test lost"},
                reconciled=True,
            ),
        ),
        "denied by an operator": request(
            9, ActionState.DENIED, decision=operator_decision(Verdict.DENY, "test denial note é")
        ),
        "denied by a policy": request(
            10, ActionState.DENIED, decision=policy_decision(Verdict.DENY, "test policy reason", repository=None)
        ),
        "denied without a reason": request(11, ActionState.DENIED, decision=operator_decision(Verdict.DENY, None)),
        "cancelled": request(12, ActionState.CANCELLED),
    }


def events() -> list[ActionEventView]:
    return [
        ActionEventView(sequence=1, state=ActionState.DECISION_PENDING, at=CREATED),
        ActionEventView(sequence=2, state=ActionState.ALLOWED, at=DECIDED),
        ActionEventView(sequence=3, state=ActionState.CANCELLED, at=COMPLETED, actor=CallerPrincipal(account=CALLER)),
        ActionEventView(sequence=4, state=ActionState.DENIED, at=COMPLETED, actor=OPERATOR),
    ]


def policy() -> CallerActionPolicyView:
    actions = {"test-mcp": ["alpha", "beta"], "test-sandbox": ["exec"]}
    kinds: list[PolicyView] = [
        ExactActionsView(type=PolicyKind.EXACT_ACTIONS, actions=actions),
        ArgumentSchemaView(
            type=PolicyKind.ARGUMENT_SCHEMA,
            actions={"test-mcp": ["alpha"]},
            argument_schema={"type": "object", "properties": {"count": {"maximum": 2.5}}},
        ),
        GitHubRepositoryView(
            type=PolicyKind.GITHUB_REPOSITORY, actions=actions, owner="test-owner", repository="test-repo"
        ),
        GitHubPublicRepositoryView(type=PolicyKind.GITHUB_PUBLIC_REPOSITORY, actions={"test-mcp": ["beta"]}),
        HomeAssistantEntityControlView(
            type=PolicyKind.HOME_ASSISTANT_ENTITY_CONTROL,
            actions={"test-mcp": ["alpha"]},
            entities={"light.test": ["turn_off", "turn_on"]},
        ),
    ]
    return CallerActionPolicyView(
        subject=CALLER,
        synced=True,
        bindings=[
            CallerBindingView(name="test-binding", expires_at=COMPLETED, policy_sets=["test-set", "test-other-set"]),
            CallerBindingView(name="test-forever", expires_at=None, policy_sets=[]),
        ],
        auto_approve_if=[
            EffectivePolicyView(
                binding="test-binding", policy_set="test-set", source=PolicySource.RULES, index=index, policy=kind
            )
            for index, kind in enumerate(kinds)
        ],
    )


@dataclass(frozen=True)
class Refusal:
    """A submission the core refuses: what this service raises, and the gRPC status the core answers instead."""

    error: Exception
    code: str
    message: str


# What the caller's policy could auto-approve, for listing its direct tools: `test-mcp/alpha` is a direct tool it
# leaves out, `test-down/test-gone` one its unavailable group does not offer, and `test-other/test-unconfigured` no
# direct tool at all.
APPROVABLE = [
    ActionIdentity(group="test-down", name="test-gone"),
    ActionIdentity(group="test-mcp", name="beta"),
    ActionIdentity(group="test-other", name="test-unconfigured"),
    ActionIdentity(group="test-sandbox", name="exec"),
]
# A direct call's idempotency key is a fresh `direct-` UUID each call; the vector names its form.
FRESH_DIRECT_KEY = "direct-<uuid4>"


class Core:
    """The service the tools call, answering from fixed records and recording each call as the core RPC it is."""

    def __init__(
        self,
        views: list[ActionRequestView],
        *,
        cancellation: CancellationOutcome | None = None,
        refusal: Refusal | None = None,
    ) -> None:
        self._views = {view.id: view for view in views}
        self._cancellation = cancellation
        self._refusal = refusal
        self.calls: list[Json] = []

    def _answer(
        self, rpc: str, request: Json, request_id: UUID, response: Callable[[ActionRequestView], Json]
    ) -> ActionRequestView:
        view = self._views.get(request_id)
        if view is None:
            self.calls.append({"rpc": rpc, "request": request, "code": "NOT_FOUND"})
            raise ActionNotFoundError(str(request_id))
        self.calls.append({"rpc": rpc, "request": request, "response": response(view)})
        return view

    async def get(self, request_id: UUID, principal: CallerPrincipal) -> ActionRequestView:
        return self._answer(
            "GetActionRequest",
            {"requestId": str(request_id)},
            request_id,
            lambda view: {"actionRequest": proto_request(view)},
        )

    async def list_requests(self, principal: CallerPrincipal, *, idempotency_key: str) -> list[ActionRequestView]:
        found = [view for view in self._views.values() if view.idempotency_key == idempotency_key]
        self.calls.append(
            {
                "rpc": "ListActionRequests",
                "request": {"idempotencyKey": idempotency_key},
                "response": {"actionRequests": [proto_request(view) for view in found]},
            }
        )
        return found

    async def submit(self, body: ActionRequestInput, principal: CallerPrincipal) -> ActionRequestView:
        return self._submit(body, decided_only=False)

    async def submit_decided(self, body: ActionRequestInput, principal: CallerPrincipal) -> ActionRequestView:
        return self._submit(body, decided_only=True)

    def _submit(self, body: ActionRequestInput, *, decided_only: bool) -> ActionRequestView:
        key = body.idempotency_key
        if decided_only:
            prefix, _, fresh = key.partition("-")
            assert prefix == "direct", key
            assert UUID(fresh).version == 4, key
            key = FRESH_DIRECT_KEY
        request = present(
            idempotencyKey=key,
            action=body.action.model_dump(),
            arguments=body.arguments,
            title=body.title,
            description=body.description,
            decidedOnly=decided_only or None,
        )
        if self._refusal is not None:
            self.calls.append(
                {
                    "rpc": "SubmitActionRequest",
                    "request": request,
                    "code": self._refusal.code,
                    "message": self._refusal.message,
                }
            )
            raise self._refusal.error
        view = next(iter(self._views.values()))
        self.calls.append(
            {"rpc": "SubmitActionRequest", "request": request, "response": {"actionRequest": proto_request(view)}}
        )
        return view

    async def cancel(self, request_id: UUID, principal: Principal) -> CancellationResult:
        outcome = self._cancellation
        assert outcome is not None
        view = self._answer(
            "CancelActionRequest",
            {"requestId": str(request_id)},
            request_id,
            lambda view: {"outcome": enum("CANCELLATION_OUTCOME", outcome), "actionRequest": proto_request(view)},
        )
        return CancellationResult(outcome=outcome, request=view)

    async def events(
        self, request_id: UUID, principal: CallerPrincipal, *, after_sequence: int, limit: int
    ) -> list[ActionEventView]:
        page = [event for event in events() if event.sequence > after_sequence][:limit]
        self._answer(
            "ListActionRequestEvents",
            present(requestId=str(request_id), afterSequence=after_sequence or None, limit=limit),
            request_id,
            lambda _: {"events": [proto_event(event) for event in page]},
        )
        return page

    def auto_approvable(self, principal: CallerPrincipal) -> set[ActionIdentity]:
        # The listing reads only `autoApprovable` of the caller's policy.
        self.calls.append(
            {
                "rpc": "GetActionPolicy",
                "request": {},
                "response": {
                    "actionPolicy": {
                        "subject": proto_account(CALLER),
                        "synced": True,
                        "autoApprovable": [identity.model_dump() for identity in APPROVABLE],
                    }
                },
            }
        )
        return set(APPROVABLE)

    def caller_action_policy(self, principal: CallerPrincipal) -> CallerActionPolicyView:
        self.calls.append({"rpc": "GetActionPolicy", "request": {}, "response": proto_policy(policy())})
        return policy()

    def target_action_policy(self, subject: ServiceAccountRef) -> CallerActionPolicyView:
        view = policy().model_copy(update={"subject": subject})
        self.calls.append(
            {"rpc": "GetActionPolicy", "request": {"subject": proto_account(subject)}, "response": proto_policy(view)}
        )
        return view


class Bearers:
    """The bearer verifier a direct call consults after its wait: the caller's bearer is still the caller's."""

    async def verify_token(self, token: str) -> CallerToken | None:
        assert token == CALLER_TOKEN.token
        return CALLER_TOKEN


def server(core: Core, max_wait_seconds: float, *, direct_wait_seconds: float) -> FastMCP:
    """This service's MCP server over `core`. A tool call without a wait never reaches the update broker: only a wait
    consults it."""
    return create_server(
        cast(ActionService, core),
        catalog(),
        cast(ActionUpdateBroker, None),
        cast(CallerTokenVerifier, Bearers()),
        direct_wait_seconds=direct_wait_seconds,
        max_wait_seconds=max_wait_seconds,
    )


def wire(result: CallToolResult) -> Json:
    return result.model_dump(mode="json", by_alias=True, exclude_none=True)


async def call(
    name: str,
    tool: str,
    arguments: Json,
    *,
    views: list[ActionRequestView] | None = None,
    cancellation: CancellationOutcome | None = None,
    refusal: Refusal | None = None,
    max_wait_seconds: float = MAX_WAIT_SECONDS,
) -> Json:
    """One call vector: `tool`'s answer as FastMCP's `tools/call` handler sends it, for the caller the HTTP
    transport authenticated, with the core RPCs that answer stands on. A direct call reads its receipt once, without
    waiting: there is no update broker to wait on."""
    core = Core(views or [], cancellation=cancellation, refusal=refusal)
    authenticated = auth_context_var.set(AuthenticatedUser(CALLER_TOKEN))
    try:
        result = await server(core, max_wait_seconds, direct_wait_seconds=0).call_tool(tool, arguments)
    except FastMCPError as error:
        answer = CallToolResult(content=[TextContent(type="text", text=str(error))], is_error=True)
    else:
        # How FastMCP's `tools/call` handler turns a tool's result into the protocol's.
        sent = result.to_mcp_result()
        if isinstance(sent, CallToolResult):
            answer = sent
        elif isinstance(sent, tuple):
            answer = CallToolResult(content=sent[0], structured_content=sent[1])
        else:
            answer = CallToolResult(content=sent)
    finally:
        auth_context_var.reset(authenticated)
    return {
        "name": name,
        "tool": tool,
        "arguments": arguments,
        "max_wait_seconds": max_wait_seconds,
        "direct_wait_seconds": 0,
        "core": cast(list[JsonValue], core.calls),
        "result": wire(answer),
    }


def listed(tools: list[Tool]) -> list[JsonValue]:
    """Tools as FastMCP lists them, without the `_meta` FastMCP keeps for itself."""
    return [
        {
            key: value
            for key, value in tool.model_dump(mode="json", by_alias=True, exclude_none=True).items()
            if key != "_meta"
        }
        for tool in tools
    ]


async def tool_definitions() -> list[JsonValue]:
    """The generic tools: without an authenticated caller FastMCP lists no direct tools."""
    async with Client(server(Core([]), MAX_WAIT_SECONDS, direct_wait_seconds=MAX_WAIT_SECONDS)) as client:
        tools = await client.list_tools_mcp()
    return listed(tools.tools)


async def direct_listing(direct_wait_seconds: float) -> Json:
    """The direct tools FastMCP lists for the external caller, with the core RPCs the listing stands on."""
    core = Core([])
    authenticated = auth_context_var.set(AuthenticatedUser(CALLER_TOKEN))
    try:
        async with Client(server(core, MAX_WAIT_SECONDS, direct_wait_seconds=direct_wait_seconds)) as client:
            tools = await client.list_tools_mcp()
    finally:
        auth_context_var.reset(authenticated)
    return {
        "max_wait_seconds": MAX_WAIT_SECONDS,
        "direct_wait_seconds": direct_wait_seconds,
        "core": cast(list[JsonValue], core.calls),
        "tools": listed([tool for tool in tools.tools if DIRECT_TOOL_SEPARATOR in tool.name]),
    }


ALL_REQUEST_FIELDS: list[JsonValue] = [field.value for field in RequestField]
ALL_POLICY_FIELDS: list[JsonValue] = [field.value for field in PolicyField]
UNKNOWN_REQUEST = str(UUID(int=0xFFFF))


async def calls() -> list[JsonValue]:
    views = requests()
    vectors: list[JsonValue] = []
    for name, view in views.items():
        vectors.append(
            await call(
                f"{name}: full receipt",
                "get_action_request",
                {"request_id": str(view.id), "include_fields": ALL_REQUEST_FIELDS},
                views=[view],
            )
        )
        vectors.append(await call(f"{name}: result", "get_action_result", {"request_id": str(view.id)}, views=[view]))
    pending, allowed, running = views["decision pending"], views["allowed, not dispatched"], views["running"]
    succeeded, failed = views["succeeded over MCP"], views["failed"]
    denied, cancelled, dispatching = views["denied by a policy"], views["cancelled"], views["dispatching"]
    gone = request(13, ActionState.SUCCEEDED, group="test-gone", execution=execution(ExecutionState.SUCCEEDED))
    vectors += [
        await call("compact receipt", "get_action_request", {"request_id": str(pending.id)}, views=[pending]),
        await call(
            "receipt of input and execution",
            "get_action_request",
            {"request_id": str(allowed.id), "include_fields": ["input", "execution"]},
            views=[allowed],
        ),
        await call(
            "receipt of nothing",
            "get_action_request",
            {"request_id": str(succeeded.id), "include_fields": []},
            views=[succeeded],
        ),
        await call(
            "receipt of the decision",
            "get_action_request",
            {"request_id": str(denied.id), "include_fields": ["decision"]},
            views=[denied],
        ),
        await call(
            "receipt by idempotency key",
            "get_action_request",
            {"idempotency_key": running.idempotency_key},
            views=[running, pending],
        ),
        await call(
            "result by idempotency key",
            "get_action_result",
            {"idempotency_key": failed.idempotency_key},
            views=[failed],
        ),
        await call("receipt of an unknown request", "get_action_request", {"request_id": UNKNOWN_REQUEST}),
        await call("receipt of an unknown key", "get_action_request", {"idempotency_key": "test-unknown-key"}),
        await call("result of an unknown request", "get_action_result", {"request_id": UNKNOWN_REQUEST}),
        await call("receipt naming no request", "get_action_request", {}),
        await call(
            "result naming the request twice",
            "get_action_result",
            {"request_id": str(pending.id), "idempotency_key": pending.idempotency_key},
            views=[pending],
        ),
        await call(
            "result of a group no longer configured", "get_action_result", {"request_id": str(gone.id)}, views=[gone]
        ),
        await call(
            "waiting result with a fractional limit",
            "get_action_result",
            {"request_id": str(running.id)},
            views=[running],
            max_wait_seconds=2.5,
        ),
        await call(
            "wait above the limit",
            "get_action_result",
            {"request_id": str(running.id), "wait": {"wait_seconds": 2.6}},
            views=[running],
            max_wait_seconds=2.5,
        ),
    ]
    for outcome, view in (
        (CancellationOutcome.CANCELLED, cancelled),
        (CancellationOutcome.ALREADY_CANCELLED, cancelled),
        (CancellationOutcome.ALREADY_FINISHED, denied),
        (CancellationOutcome.TOO_LATE, dispatching),
    ):
        vectors.append(
            await call(
                f"cancellation {outcome}",
                "cancel_action_request",
                {"request_id": str(view.id), "include_fields": ALL_REQUEST_FIELDS},
                views=[view],
                cancellation=outcome,
            )
        )
    vectors += [
        await call(
            "compact cancellation",
            "cancel_action_request",
            {"request_id": str(cancelled.id)},
            views=[cancelled],
            cancellation=CancellationOutcome.CANCELLED,
        ),
        await call(
            "cancellation of an unknown request",
            "cancel_action_request",
            {"request_id": UNKNOWN_REQUEST},
            cancellation=CancellationOutcome.CANCELLED,
        ),
        await call("every event", "list_action_request_events", {"request_id": str(pending.id)}, views=[pending]),
        await call(
            "first page of events",
            "list_action_request_events",
            {"request_id": str(pending.id), "limit": 2},
            views=[pending],
        ),
        await call(
            "last page of events",
            "list_action_request_events",
            {"request_id": str(pending.id), "after_sequence": 2, "limit": 2},
            views=[pending],
        ),
        await call("events of an unknown request", "list_action_request_events", {"request_id": UNKNOWN_REQUEST}),
    ]
    envelope: Json = {
        "idempotency_key": "test-submit-key",
        "action": {"group": "test-mcp", "name": "alpha"},
        "arguments": EVERY_JSON_KIND,
        "title": "test title",
    }
    vectors += [
        await call("submission answered with its result", "request_action", {"request": envelope}, views=[pending]),
        await call(
            "submission with a description answered with its sandbox result",
            "request_action",
            {"request": envelope | {"description": "test description é"}},
            views=[views["succeeded in a sandbox"]],
        ),
        await call(
            "submission answered with a receipt",
            "request_action",
            {"request": envelope, "respond_with": "receipt"},
            views=[pending],
        ),
        await call(
            "submission answered with a full receipt",
            "request_action",
            {"request": envelope, "respond_with": "receipt", "include_fields": ALL_REQUEST_FIELDS},
            views=[allowed],
        ),
        await call(
            "submission asking for receipt fields without a receipt",
            "request_action",
            {"request": envelope, "include_fields": ["decision"]},
            views=[pending],
        ),
        await call(
            "submission waiting above the limit",
            "request_action",
            {"request": envelope, "wait": {"wait_seconds": 31}},
            views=[pending],
        ),
    ]
    used_key = "idempotency key already used by this caller; read that request by key instead of resubmitting"
    unsupported = "Action is unknown or unavailable; use list_actions/get_action to check the catalog."
    outside_schema = "arguments do not match the advertised Action schema"
    unavailable = "ActionGroup is temporarily unavailable"
    for refused, refusal in (
        ("under a used key", Refusal(ActionConflictError(used_key), "ALREADY_EXISTS", used_key)),
        (
            "of an Action the service cannot run",
            Refusal(
                UnsupportedActionError(ActionIdentity(group="test-mcp", name="alpha")), "INVALID_ARGUMENT", unsupported
            ),
        ),
        (
            "with arguments outside the schema",
            Refusal(InvalidActionArgumentsError(outside_schema), "INVALID_ARGUMENT", outside_schema),
        ),
        ("to an unavailable group", Refusal(ActionUnavailableError(unavailable), "UNAVAILABLE", unavailable)),
    ):
        vectors.append(await call(f"submission {refused}", "request_action", {"request": envelope}, refusal=refusal))
    undecided = "test-binding: lists test-mcp/alpha only; test reason é"
    vectors += [
        await call("direct call answered as its MCP tool did", "test-mcp__alpha", EVERY_JSON_KIND, views=[succeeded]),
        await call(
            "direct call answered with its sandbox result",
            "test-sandbox__exec",
            {},
            views=[views["succeeded in a sandbox"]],
        ),
        await call("direct call still running", "test-sandbox__exec", {}, views=[running]),
        await call("direct call that failed", "test-sandbox__exec", {}, views=[failed]),
        await call(
            "direct call no policy approves",
            "test-mcp__beta",
            {},
            refusal=Refusal(UndecidedRequestError(undecided), "PERMISSION_DENIED", undecided),
        ),
        await call(
            "direct call with arguments outside the schema",
            "test-mcp__alpha",
            {"count": 11},
            refusal=Refusal(InvalidActionArgumentsError(outside_schema), "INVALID_ARGUMENT", outside_schema),
        ),
        await call(
            "direct call to an unavailable group",
            "test-mcp__alpha",
            {"message": "test"},
            refusal=Refusal(ActionUnavailableError(unavailable), "UNAVAILABLE", unavailable),
        ),
    ]
    alpha: Json = {"group": "test-mcp", "name": "alpha"}
    vectors += [
        await call("every Action", "list_actions", {}),
        await call("first page of Actions", "list_actions", {"limit": 1}),
        await call("next page of Actions", "list_actions", {"limit": 1, "after": alpha}),
        await call("one group's Actions", "list_actions", {"group": "test-sandbox"}),
        await call("an unavailable group's Actions", "list_actions", {"group": "test-down"}),
        await call("Actions with their schemas", "list_actions", {"include_fields": ["input_schema", "description"]}),
        await call("Actions of an unknown group", "list_actions", {"group": "test-unknown"}),
        await call(
            "next page in another group",
            "list_actions",
            {"group": "test-mcp", "after": {"group": "test-sandbox", "name": "exec"}},
        ),
        await call("compact Action", "get_action", alpha),
        await call("Action with its schema", "get_action", alpha | {"include_fields": ["input_schema", "description"]}),
        await call(
            "Action without a schema",
            "get_action",
            {"group": "test-mcp", "name": "beta", "include_fields": ["input_schema"]},
        ),
        await call("unknown Action", "get_action", {"group": "test-mcp", "name": "test-unknown"}),
        await call("Action of an unavailable group", "get_action", {"group": "test-down", "name": "test-gone"}),
        await call("own policy", "get_action_policy", {}),
        await call("own policy, every field", "get_action_policy", {"include_fields": ALL_POLICY_FIELDS}),
        await call(
            "own policy named as self", "get_action_policy", {"target": "self", "include_fields": ["auto_approve_if"]}
        ),
        await call(
            "named account's policy",
            "get_action_policy",
            {
                "target": {"service_account": {"namespace": "test-other-namespace", "name": "test-other"}},
                "include_fields": ALL_POLICY_FIELDS,
            },
        ),
    ]
    return vectors


async def build() -> Json:
    return {
        "server": {
            "name": server(Core([]), MAX_WAIT_SECONDS, direct_wait_seconds=MAX_WAIT_SECONDS).name,
            "instructions": [
                {
                    "max_wait_seconds": seconds,
                    "text": server(Core([]), seconds, direct_wait_seconds=seconds).instructions,
                }
                for seconds in (MAX_WAIT_SECONDS, 2.5)
            ],
        },
        "catalog": proto_groups(catalog()),
        # The second direct wait is above the limit, which bounds it.
        "direct_listings": [await direct_listing(seconds) for seconds in (2.5, 60.0)],
        "calls": await calls(),
    }


def serialize(golden: JsonValue) -> str:
    return json.dumps(golden, indent=1, ensure_ascii=False) + "\n"


async def generate(directory: Path) -> None:
    (directory / "tools.json").write_text(serialize(await tool_definitions()), encoding="utf-8")
    (directory / "golden.json").write_text(serialize(await build()), encoding="utf-8")


def main() -> None:
    asyncio.run(generate(Path(sys.argv[1])))


if __name__ == "__main__":
    main()
