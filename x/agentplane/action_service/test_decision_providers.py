"""Synchronous DecisionProvider aggregation: matrix, deny dominance, races, timeout, human fallback,
and the policy-set provider deciding from the caller's bindings at admission."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
import pytest_bazel
from pydantic import JsonValue, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from github_policy.visibility import RepositoryVisibilityService
from x.agentplane.action_service.catalog import ActionCatalog, ActionIdentity
from x.agentplane.action_service.db import ActionConflictError, ActionStore, make_sessionmaker
from x.agentplane.action_service.mcp_executor import McpActionGroupExecutor
from x.agentplane.action_service.models import (
    ActionRequestInput,
    ActionRequestView,
    ActionState,
    BindingEvidence,
    DecisionInput,
    MatchedPolicy,
    PolicyKind,
    PolicySetEvidence,
    Principal,
    PrincipalRole,
    ProviderOutcome,
    ProviderVerdict,
    SandboxCaller,
    Verdict,
)
from x.agentplane.action_service.policies.resources import parse_binding, parse_policy_set
from x.agentplane.action_service.policy_evaluation import AUTO_APPROVE_REASON, PROVIDER_NAME, PolicySetDecisionProvider
from x.agentplane.action_service.policy_informer import PolicyIndex
from x.agentplane.action_service.providers import DecisionContext
from x.agentplane.action_service.service import ActionService, InvalidActionArgumentsError

NAMESPACE = "agentplane-test"
SANDBOX = SandboxCaller(namespace=NAMESPACE, sandbox_uid="sandbox-a-uid")
CALLER = SANDBOX.principal()
OPERATOR = Principal(issuer="test-bff", subject="operator", role=PrincipalRole.OPERATOR)
NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
ECHO = ActionIdentity(group="agentplane", name="echo")


class ScriptedProvider:
    """Returns a fixed outcome; records every context it was asked to evaluate."""

    def __init__(self, name: str, verdict: ProviderVerdict, *, delay: float = 0.0, reason_code: str = "scripted"):
        self._name = name
        self._verdict = verdict
        self._delay = delay
        self._reason_code = reason_code
        self.contexts: list[DecisionContext] = []

    @property
    def name(self) -> str:
        return self._name

    async def decide(self, context: DecisionContext) -> ProviderOutcome:
        self.contexts.append(context)
        if self._delay:
            await asyncio.sleep(self._delay)
        return ProviderOutcome(
            verdict=self._verdict, reason_code=self._reason_code, reason_description=f"{self._name} decided"
        )


class HangingProvider:
    @property
    def name(self) -> str:
        return "hanging"

    async def decide(self, context: DecisionContext) -> ProviderOutcome:
        del context
        await asyncio.sleep(10)
        raise AssertionError("must be cancelled by the aggregator's bounded deadline")


class ExplodingProvider:
    @property
    def name(self) -> str:
        return "exploding"

    async def decide(self, context: DecisionContext) -> ProviderOutcome:
        del context
        raise RuntimeError("backend rejected Authorization: Bearer provider-secret-must-not-leak")


class RacingHumanProvider:
    """Simulates an operator Decision landing while this provider is still evaluating."""

    def __init__(self, store: ActionStore, verdict: ProviderVerdict):
        self._store = store
        self._verdict = verdict

    @property
    def name(self) -> str:
        return "racing"

    async def decide(self, context: DecisionContext) -> ProviderOutcome:
        await self._store.decide(
            context.request_id,
            DecisionInput(verdict=Verdict.DENY, expected_version=1, idempotency_key="human-wins-race"),
            OPERATOR,
            provider=ActionService.HUMAN_PROVIDER,
        )
        return ProviderOutcome(verdict=self._verdict, reason_code="late-vote")


def body(idempotency_key: str, n: int = 1) -> ActionRequestInput:
    return ActionRequestInput(idempotency_key=idempotency_key, action=ECHO, arguments={"n": n})


async def _succeeded(service: ActionService, request_id: UUID) -> ActionRequestView:
    async with asyncio.timeout(10):
        view = await service.get(request_id, CALLER)
        while view.state is not ActionState.SUCCEEDED:
            await asyncio.sleep(0.01)
            view = await service.get(request_id, CALLER)
    return view


def test_provider_outcome_bounds_the_reason_description() -> None:
    with pytest.raises(ValidationError):
        ProviderOutcome(verdict=ProviderVerdict.DENY, reason_code="x", reason_description="a" * 501)


async def test_allow_deny_no_opinion_matrix(
    mcp_executor: McpActionGroupExecutor, engine: AsyncEngine, echo_catalog: ActionCatalog
) -> None:
    store = ActionStore(make_sessionmaker(engine))

    allow_only = ActionService(
        store, echo_catalog, {"agentplane": mcp_executor}, providers=[ScriptedProvider("p", ProviderVerdict.ALLOW)]
    )
    allowed = await allow_only.submit(body("matrix-allow"), CALLER)
    assert allowed.state is ActionState.ALLOWED
    assert allowed.decision is not None
    assert allowed.decision.verdict is Verdict.ALLOW
    await allow_only.close()

    deny_only = ActionService(
        store, echo_catalog, {"agentplane": mcp_executor}, providers=[ScriptedProvider("p", ProviderVerdict.DENY)]
    )
    denied = await deny_only.submit(body("matrix-deny"), CALLER)
    assert denied.state is ActionState.DENIED
    assert denied.decision is not None
    assert denied.decision.verdict is Verdict.DENY
    await deny_only.close()

    no_opinion_only = ActionService(
        store, echo_catalog, {"agentplane": mcp_executor}, providers=[ScriptedProvider("p", ProviderVerdict.NO_OPINION)]
    )
    pending = await no_opinion_only.submit(body("matrix-no-opinion"), CALLER)
    assert pending.state is ActionState.DECISION_PENDING
    assert pending.decision is None
    await no_opinion_only.close()


async def test_deny_dominates_even_when_the_allow_vote_finishes_first(
    mcp_executor: McpActionGroupExecutor, engine: AsyncEngine, echo_catalog: ActionCatalog
) -> None:
    store = ActionStore(make_sessionmaker(engine))
    fast_allow = ScriptedProvider("fast-allow", ProviderVerdict.ALLOW, delay=0.0)
    slow_deny = ScriptedProvider("slow-deny", ProviderVerdict.DENY, delay=0.05)
    service = ActionService(store, echo_catalog, {"agentplane": mcp_executor}, providers=[fast_allow, slow_deny])
    try:
        result = await service.submit(body("deny-dominance"), CALLER)
        assert result.state is ActionState.DENIED
        assert result.decision is not None
        assert result.decision.provider == "slow-deny"
        assert len(fast_allow.contexts) == len(slow_deny.contexts) == 1
    finally:
        await service.close()


async def test_provider_timeout_is_not_allow_and_does_not_block_other_providers(
    mcp_executor: McpActionGroupExecutor, engine: AsyncEngine, echo_catalog: ActionCatalog
) -> None:
    store = ActionStore(make_sessionmaker(engine))

    alone = ActionService(
        store, echo_catalog, {"agentplane": mcp_executor}, providers=[HangingProvider()], provider_timeout_seconds=0.02
    )
    pending = await alone.submit(body("timeout-alone"), CALLER)
    assert pending.state is ActionState.DECISION_PENDING, "a timeout must never be treated as an allow"
    await alone.close()

    with_deny = ActionService(
        store,
        echo_catalog,
        {"agentplane": mcp_executor},
        providers=[HangingProvider(), ScriptedProvider("deny", ProviderVerdict.DENY)],
        provider_timeout_seconds=0.02,
    )
    try:
        denied = await with_deny.submit(body("timeout-with-deny"), CALLER)
        assert denied.state is ActionState.DENIED, "one provider timing out must not block another's deny"
    finally:
        await with_deny.close()


async def test_provider_exception_is_not_allow_and_material_is_not_projected_or_logged(
    mcp_executor: McpActionGroupExecutor,
    engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
    echo_catalog: ActionCatalog,
) -> None:
    store = ActionStore(make_sessionmaker(engine))
    service = ActionService(store, echo_catalog, {"agentplane": mcp_executor}, providers=[ExplodingProvider()])
    try:
        pending = await service.submit(body("provider-explodes"), CALLER)
        assert pending.state is ActionState.DECISION_PENDING
        rendered = "\n".join(record.getMessage() for record in caplog.records)
        assert "provider-secret-must-not-leak" not in rendered
        assert "provider-secret-must-not-leak" not in pending.model_dump_json()
    finally:
        await service.close()


async def test_human_fallback_when_no_provider_has_an_opinion(
    mcp_executor: McpActionGroupExecutor, engine: AsyncEngine, echo_catalog: ActionCatalog
) -> None:
    store = ActionStore(make_sessionmaker(engine))
    service = ActionService(
        store, echo_catalog, {"agentplane": mcp_executor}, providers=[ScriptedProvider("p", ProviderVerdict.NO_OPINION)]
    )
    try:
        pending = await service.submit(body("human-fallback"), CALLER)
        assert pending.state is ActionState.DECISION_PENDING

        decided = await service.decide(
            pending.id,
            DecisionInput(verdict=Verdict.ALLOW, expected_version=pending.version, idempotency_key="operator-allow"),
            OPERATOR,
        )
        assert decided.decision is not None
        assert decided.decision.provider == ActionService.HUMAN_PROVIDER
    finally:
        await service.close()


async def test_stale_human_decision_after_auto_provider_already_decided(
    mcp_executor: McpActionGroupExecutor, engine: AsyncEngine, echo_catalog: ActionCatalog
) -> None:
    store = ActionStore(make_sessionmaker(engine))
    service = ActionService(
        store, echo_catalog, {"agentplane": mcp_executor}, providers=[ScriptedProvider("policy", ProviderVerdict.DENY)]
    )
    try:
        auto_decided = await service.submit(body("stale-human-after-auto"), CALLER)
        assert auto_decided.state is ActionState.DENIED

        with pytest.raises(ActionConflictError, match="already decided"):
            await service.decide(
                auto_decided.id,
                DecisionInput(
                    verdict=Verdict.ALLOW, expected_version=auto_decided.version, idempotency_key="late-human-allow"
                ),
                OPERATOR,
            )

        unchanged = await store.get(auto_decided.id, OPERATOR)
        assert unchanged.state is ActionState.DENIED, "a stale human callback must never override the Decision"
    finally:
        await service.close()


async def test_stale_auto_decision_after_human_already_decided(
    mcp_executor: McpActionGroupExecutor, engine: AsyncEngine, echo_catalog: ActionCatalog
) -> None:
    store = ActionStore(make_sessionmaker(engine))
    racing = RacingHumanProvider(store, ProviderVerdict.ALLOW)
    service = ActionService(store, echo_catalog, {"agentplane": mcp_executor}, providers=[racing])
    try:
        result = await service.submit(body("stale-auto-after-human"), CALLER)
        assert result.state is ActionState.DENIED, "the human Decision that committed first must win"
        assert result.decision is not None
        assert result.decision.provider == ActionService.HUMAN_PROVIDER
    finally:
        await service.close()


async def test_decision_context_carries_only_the_authenticated_caller(
    mcp_executor: McpActionGroupExecutor, engine: AsyncEngine, echo_catalog: ActionCatalog
) -> None:
    store = ActionStore(make_sessionmaker(engine))
    provider = ScriptedProvider("identity-check", ProviderVerdict.NO_OPINION)
    service = ActionService(store, echo_catalog, {"agentplane": mcp_executor}, providers=[provider])
    try:
        forged = ActionRequestInput(
            idempotency_key="identity-context",
            action=ECHO,
            arguments={"n": 1},
            origin={"agent_id": "forged-agent", "owner": "forged-owner", "sandbox_uid": "forged-uid"},
            correlation={"turn_ref": "forged-turn", "binding": "forged-binding"},
        )
        await service.submit(forged, CALLER)
        (context,) = provider.contexts
        assert context.caller == SANDBOX
        assert context.bindings == ()
    finally:
        await service.close()


async def test_provider_reason_is_projected_separately_from_human_note(
    mcp_executor: McpActionGroupExecutor, engine: AsyncEngine, echo_catalog: ActionCatalog
) -> None:
    store = ActionStore(make_sessionmaker(engine))
    service = ActionService(
        store,
        echo_catalog,
        {"agentplane": mcp_executor},
        providers=[ScriptedProvider("policy", ProviderVerdict.DENY, reason_code="untrusted_action")],
    )
    try:
        result = await service.submit(body("reason-projection"), CALLER)
        assert result.decision is not None
        assert result.decision.reason_code == "untrusted_action"
        assert result.decision.reason_description == "policy decided"
        # Provider outcome evidence is separate from the human-authored note.
        assert result.decision.decision_note is None
        assert result.decision.policy_evidence is None

        operator_view = await store.get(result.id, OPERATOR)
        assert operator_view.decision is not None
        assert operator_view.decision.reason_code == "untrusted_action"
        assert operator_view.decision.provider == "policy"
        assert operator_view.decision == result.decision
    finally:
        await service.close()


def _meta(name: str, *, generation: int = 1, version: str = "1") -> dict[str, Any]:
    return {
        "name": name,
        "namespace": NAMESPACE,
        "uid": f"uid-{name}",
        "generation": generation,
        "resourceVersion": version,
    }


def _index(
    *,
    sets: dict[str, dict[str, Any]] | None = None,
    bindings: dict[str, dict[str, Any]] | None = None,
    synced: bool = True,
) -> PolicyIndex:
    """An index from raw specs, parsed the way the informer parses them, so invalid ones stay invalid."""
    index = PolicyIndex(synced=synced)
    for name, spec in (sets or {}).items():
        policy_set = parse_policy_set({"metadata": _meta(name, generation=4, version="40"), "spec": spec})
        index.policy_sets[policy_set.namespaced_name] = policy_set
    for name, spec in (bindings or {}).items():
        binding = parse_binding({"metadata": _meta(name, version="7"), "spec": spec})
        index.bindings[binding.namespaced_name] = binding
    return index


BOUNDED_ECHO: dict[str, Any] = {
    "autoApproveIf": [
        {
            "type": "argument_schema",
            "actions": {"agentplane": ["echo"]},
            "schema": {"type": "object", "required": ["n"], "properties": {"n": {"type": "integer", "maximum": 5}}},
        }
    ]
}
OWN_SANDBOX: dict[str, Any] = {"sandbox": {"name": "coder", "uid": SANDBOX.sandbox_uid}}


def _service(
    engine: AsyncEngine,
    catalog: ActionCatalog,
    executor: McpActionGroupExecutor,
    index: PolicyIndex | None,
    visibility: RepositoryVisibilityService,
) -> ActionService:
    return ActionService(
        ActionStore(make_sessionmaker(engine)),
        catalog,
        {"agentplane": executor},
        providers=[PolicySetDecisionProvider(visibility=visibility)],
        policies=index,
        clock=lambda: NOW,
    )


async def test_bound_sandbox_is_auto_approved_with_evidence_and_an_execution(
    mcp_executor: McpActionGroupExecutor,
    engine: AsyncEngine,
    echo_catalog: ActionCatalog,
    github_visibility: Callable[..., RepositoryVisibilityService],
) -> None:
    index = _index(
        sets={"bounded-echo": BOUNDED_ECHO, "unused": {"autoApproveIf": []}},
        bindings={
            "coder-bounded": {"subject": OWN_SANDBOX, "policySets": ["bounded-echo", "missing"]},
            "coder-unused": {"subject": OWN_SANDBOX, "policySets": ["unused"]},
        },
    )
    service = _service(engine, echo_catalog, mcp_executor, index, github_visibility())
    try:
        allowed = await service.submit(body("bound", n=3), CALLER)
        assert allowed.state is ActionState.ALLOWED
        assert allowed.decision is not None
        assert allowed.decision.provider == PROVIDER_NAME
        assert allowed.decision.reason_code == AUTO_APPROVE_REASON
        assert allowed.decision.reason_description is not None
        assert "coder-bounded" in allowed.decision.reason_description
        assert allowed.decision.policy_evidence is not None
        assert allowed.decision.policy_evidence.bindings == [
            BindingEvidence(namespace=NAMESPACE, name="coder-bounded", resource_version="7"),
            BindingEvidence(namespace=NAMESPACE, name="coder-unused", resource_version="7"),
        ]
        assert allowed.decision.policy_evidence.policy_sets == [
            PolicySetEvidence(namespace=NAMESPACE, name="bounded-echo", generation=4),
            PolicySetEvidence(namespace=NAMESPACE, name="unused", generation=4),
        ]
        assert allowed.decision.policy_evidence.matched == MatchedPolicy(
            namespace=NAMESPACE,
            policy_set="bounded-echo",
            source="autoApproveIf",
            index=0,
            type=PolicyKind.ARGUMENT_SCHEMA,
        )
        assert allowed.execution is not None
        view = await _succeeded(service, allowed.id)
        assert view.execution is not None
        assert view.execution.result == {"n": 3}
        # The same evidence is the operator's, and a retry recovers the same Decision.
        assert (await service.get(allowed.id, OPERATOR)).decision == allowed.decision
        assert (await service.submit(body("bound", n=3), CALLER)).decision == allowed.decision
        # An argument miss takes the human path with no evidence recorded.
        pending = await service.submit(body("argument-miss", n=9), CALLER)
        assert pending.state is ActionState.DECISION_PENDING
        assert pending.decision is None
    finally:
        await service.close()


@pytest.mark.parametrize(
    "index",
    [
        pytest.param(lambda: None, id="no-policy-objects"),
        pytest.param(
            lambda: _index(
                sets={"bounded-echo": BOUNDED_ECHO},
                bindings={"coder": {"subject": OWN_SANDBOX, "policySets": ["bounded-echo"]}},
                synced=False,
            ),
            id="unsynced",
        ),
        pytest.param(lambda: _index(sets={"bounded-echo": BOUNDED_ECHO}), id="no-binding"),
        pytest.param(
            lambda: _index(
                sets={"bounded-echo": BOUNDED_ECHO},
                bindings={"coder": {"subject": OWN_SANDBOX, "policySets": ["other"]}},
            ),
            id="missing-set",
        ),
        pytest.param(
            lambda: _index(
                sets={"broken": {"autoApproveIf": [{"type": "anything"}]}},
                bindings={"coder": {"subject": OWN_SANDBOX, "policySets": ["broken"]}},
            ),
            id="invalid-set",
        ),
        pytest.param(
            lambda: _index(
                sets={"bounded-echo": BOUNDED_ECHO},
                bindings={"coder": {"subject": {"sandbox": {"name": "coder"}}, "policySets": ["bounded-echo"]}},
            ),
            id="invalid-binding",
        ),
        pytest.param(
            lambda: _index(
                sets={"bounded-echo": BOUNDED_ECHO},
                bindings={
                    "coder": {
                        "subject": OWN_SANDBOX,
                        "policySets": ["bounded-echo"],
                        "expiresAt": (NOW - timedelta(minutes=1)).isoformat(),
                    }
                },
            ),
            id="expired-binding",
        ),
        pytest.param(
            lambda: _index(
                sets={"bounded-echo": BOUNDED_ECHO},
                bindings={
                    "other": {
                        "subject": {"sandbox": {"name": "coder", "uid": "other-sandbox-uid"}},
                        "policySets": ["bounded-echo"],
                    }
                },
            ),
            id="another-sandbox",
        ),
        pytest.param(
            lambda: _index(
                sets={"bounded-echo": BOUNDED_ECHO},
                bindings={
                    "account": {
                        "subject": {"serviceAccount": {"namespace": NAMESPACE, "name": "sandbox-a-uid"}},
                        "policySets": ["bounded-echo"],
                    }
                },
            ),
            id="service-account-not-sandbox",
        ),
        pytest.param(
            lambda: _index(
                sets={"deny-only": {"autoDenyIf": BOUNDED_ECHO["autoApproveIf"], "autoDenyUnless": []}},
                bindings={"coder": {"subject": OWN_SANDBOX, "policySets": ["deny-only"]}},
            ),
            id="deny-lists-decide-nothing-yet",
        ),
    ],
)
async def test_nothing_grants_without_a_matching_valid_unexpired_binding(
    mcp_executor: McpActionGroupExecutor,
    engine: AsyncEngine,
    echo_catalog: ActionCatalog,
    index: Callable[[], PolicyIndex | None],
    github_visibility: Callable[..., RepositoryVisibilityService],
) -> None:
    service = _service(engine, echo_catalog, mcp_executor, index(), github_visibility())
    try:
        pending = await service.submit(
            ActionRequestInput(
                idempotency_key="unbound",
                action=ECHO,
                arguments={"n": 1},
                origin={"binding": "coder", "sandbox_uid": SANDBOX.sandbox_uid, "policy_set": "bounded-echo"},
            ),
            CALLER,
        )
        assert pending.state is ActionState.DECISION_PENDING
        assert pending.decision is None
        assert pending.execution is None
    finally:
        await service.close()


async def test_binding_change_after_admission_leaves_the_decision_alone(
    mcp_executor: McpActionGroupExecutor,
    engine: AsyncEngine,
    echo_catalog: ActionCatalog,
    github_visibility: Callable[..., RepositoryVisibilityService],
) -> None:
    index = _index(
        sets={"bounded-echo": BOUNDED_ECHO},
        bindings={"coder": {"subject": OWN_SANDBOX, "policySets": ["bounded-echo"]}},
    )
    service = _service(engine, echo_catalog, mcp_executor, index, github_visibility())
    try:
        allowed = await service.submit(body("before-removal"), CALLER)
        assert allowed.state is ActionState.ALLOWED
        index.bindings.clear()
        assert (await service.submit(body("after-removal"), CALLER)).state is ActionState.DECISION_PENDING
        view = await _succeeded(service, allowed.id)
        assert view.decision == allowed.decision
    finally:
        await service.close()


@pytest.mark.parametrize("arguments", [{}, {"n": "not-an-integer"}, {"n": 1, "extra": "not-allowed"}])
async def test_invalid_arguments_are_rejected_before_persistence_and_provider_evaluation(
    engine: AsyncEngine,
    echo_catalog: ActionCatalog,
    mcp_executor: McpActionGroupExecutor,
    arguments: dict[str, JsonValue],
) -> None:
    echo_catalog.groups["agentplane"].actions["echo"].input_schema = {
        "type": "object",
        "properties": {"n": {"type": "integer"}},
        "required": ["n"],
        "additionalProperties": False,
    }
    store = ActionStore(make_sessionmaker(engine))
    provider = ScriptedProvider("policy", ProviderVerdict.NO_OPINION)
    service = ActionService(store, echo_catalog, {"agentplane": mcp_executor}, providers=[provider])
    try:
        with pytest.raises(InvalidActionArgumentsError, match="advertised Action schema"):
            await service.submit(
                ActionRequestInput(idempotency_key="schema-check", action=ECHO, arguments=arguments), CALLER
            )
        assert provider.contexts == []
        assert await store.list_requests(CALLER) == []
        # A rejection neither creates a row nor reserves the caller's idempotency key.
        accepted = await service.submit(body("schema-check"), CALLER)
        assert accepted.state is ActionState.DECISION_PENDING
        assert len(provider.contexts) == 1
    finally:
        await service.close()


if __name__ == "__main__":
    pytest_bazel.main()
