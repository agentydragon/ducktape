"""Synchronous DecisionProvider aggregation: matrix, deny dominance, races, timeout, human fallback."""

from __future__ import annotations

import asyncio

import pytest
import pytest_bazel
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from x.agentplane.action_service.db import ActionConflictError, ActionStore, make_sessionmaker
from x.agentplane.action_service.models import (
    ActionRequestInput,
    ActionState,
    DecisionContext,
    DecisionInput,
    ExecutionRequest,
    ExecutionResult,
    ExecutionState,
    Principal,
    PrincipalRole,
    ProviderOutcome,
    ProviderVerdict,
    Verdict,
)
from x.agentplane.action_service.service import ActionService

CALLER = Principal(issuer="kubernetes-sandbox", subject="agentplane-staging:sandbox-a-uid", role=PrincipalRole.CALLER)
OPERATOR = Principal(issuer="test-bff", subject="operator", role=PrincipalRole.OPERATOR)


class EchoExecutor:
    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({"agentplane:v0.echo"})

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        return ExecutionResult(state=ExecutionState.SUCCEEDED, result={"echo": request.arguments})


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


def body(idempotency_key: str) -> ActionRequestInput:
    return ActionRequestInput(idempotency_key=idempotency_key, capability="agentplane:v0.echo", arguments={"n": 1})


def test_provider_outcome_bounds_the_reason_description() -> None:
    with pytest.raises(ValidationError):
        ProviderOutcome(verdict=ProviderVerdict.DENY, reason_code="x", reason_description="a" * 501)


async def test_allow_deny_no_opinion_matrix(engine: AsyncEngine) -> None:
    store = ActionStore(make_sessionmaker(engine))

    allow_only = ActionService(store, EchoExecutor(), providers=[ScriptedProvider("p", ProviderVerdict.ALLOW)])
    allowed = await allow_only.submit(body("matrix-allow"), CALLER)
    assert allowed.state is ActionState.ALLOWED
    assert allowed.decision is not None
    assert allowed.decision.verdict is Verdict.ALLOW
    await allow_only.close()

    deny_only = ActionService(store, EchoExecutor(), providers=[ScriptedProvider("p", ProviderVerdict.DENY)])
    denied = await deny_only.submit(body("matrix-deny"), CALLER)
    assert denied.state is ActionState.DENIED
    assert denied.decision is not None
    assert denied.decision.verdict is Verdict.DENY
    await deny_only.close()

    no_opinion_only = ActionService(
        store, EchoExecutor(), providers=[ScriptedProvider("p", ProviderVerdict.NO_OPINION)]
    )
    pending = await no_opinion_only.submit(body("matrix-no-opinion"), CALLER)
    assert pending.state is ActionState.DECISION_PENDING
    assert pending.decision is None
    await no_opinion_only.close()


async def test_deny_dominates_even_when_the_allow_vote_finishes_first(engine: AsyncEngine) -> None:
    store = ActionStore(make_sessionmaker(engine))
    fast_allow = ScriptedProvider("fast-allow", ProviderVerdict.ALLOW, delay=0.0)
    slow_deny = ScriptedProvider("slow-deny", ProviderVerdict.DENY, delay=0.05)
    service = ActionService(store, EchoExecutor(), providers=[fast_allow, slow_deny])
    try:
        result = await service.submit(body("deny-dominance"), CALLER)
        assert result.state is ActionState.DENIED
        assert result.decision is not None
        assert result.decision.provider == "slow-deny"
        assert len(fast_allow.contexts) == len(slow_deny.contexts) == 1
    finally:
        await service.close()


async def test_provider_timeout_is_not_allow_and_does_not_block_other_providers(engine: AsyncEngine) -> None:
    store = ActionStore(make_sessionmaker(engine))

    alone = ActionService(store, EchoExecutor(), providers=[HangingProvider()], provider_timeout_seconds=0.02)
    pending = await alone.submit(body("timeout-alone"), CALLER)
    assert pending.state is ActionState.DECISION_PENDING, "a timeout must never be treated as an allow"
    await alone.close()

    with_deny = ActionService(
        store,
        EchoExecutor(),
        providers=[HangingProvider(), ScriptedProvider("deny", ProviderVerdict.DENY)],
        provider_timeout_seconds=0.02,
    )
    try:
        denied = await with_deny.submit(body("timeout-with-deny"), CALLER)
        assert denied.state is ActionState.DENIED, "one provider timing out must not block another's deny"
    finally:
        await with_deny.close()


async def test_provider_exception_is_not_allow_and_material_is_not_projected_or_logged(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    store = ActionStore(make_sessionmaker(engine))
    service = ActionService(store, EchoExecutor(), providers=[ExplodingProvider()])
    try:
        pending = await service.submit(body("provider-explodes"), CALLER)
        assert pending.state is ActionState.DECISION_PENDING
        rendered = "\n".join(record.getMessage() for record in caplog.records)
        assert "provider-secret-must-not-leak" not in rendered
        assert "provider-secret-must-not-leak" not in pending.model_dump_json()
    finally:
        await service.close()


async def test_human_fallback_when_no_provider_has_an_opinion(engine: AsyncEngine) -> None:
    store = ActionStore(make_sessionmaker(engine))
    service = ActionService(store, EchoExecutor(), providers=[ScriptedProvider("p", ProviderVerdict.NO_OPINION)])
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


async def test_stale_human_decision_after_auto_provider_already_decided(engine: AsyncEngine) -> None:
    store = ActionStore(make_sessionmaker(engine))
    service = ActionService(store, EchoExecutor(), providers=[ScriptedProvider("policy", ProviderVerdict.DENY)])
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


async def test_stale_auto_decision_after_human_already_decided(engine: AsyncEngine) -> None:
    store = ActionStore(make_sessionmaker(engine))
    racing = RacingHumanProvider(store, ProviderVerdict.ALLOW)
    service = ActionService(store, EchoExecutor(), providers=[racing])
    try:
        result = await service.submit(body("stale-auto-after-human"), CALLER)
        assert result.state is ActionState.DENIED, "the human Decision that committed first must win"
        assert result.decision is not None
        assert result.decision.provider == ActionService.HUMAN_PROVIDER
    finally:
        await service.close()


async def test_decision_context_carries_only_trusted_identity(engine: AsyncEngine) -> None:
    store = ActionStore(make_sessionmaker(engine))
    provider = ScriptedProvider("identity-check", ProviderVerdict.NO_OPINION)
    service = ActionService(store, EchoExecutor(), providers=[provider])
    try:
        forged = ActionRequestInput(
            idempotency_key="identity-context",
            capability="agentplane:v0.echo",
            arguments={"n": 1},
            origin={"agent_id": "forged-agent", "owner": "forged-owner"},
            correlation={"turn_ref": "forged-turn"},
        )
        await service.submit(forged, CALLER)
        assert len(provider.contexts) == 1
        context = provider.contexts[0]
        assert context.caller_principal == CALLER
        assert context.agent_identity is None
    finally:
        await service.close()


async def test_provider_reason_is_projected_to_caller_unlike_private_operator_reason(engine: AsyncEngine) -> None:
    store = ActionStore(make_sessionmaker(engine))
    service = ActionService(
        store,
        EchoExecutor(),
        providers=[ScriptedProvider("policy", ProviderVerdict.DENY, reason_code="untrusted_capability")],
    )
    try:
        result = await service.submit(body("reason-projection"), CALLER)
        assert result.decision is not None
        assert result.decision.reason_code == "untrusted_capability"
        assert result.decision.reason_description == "policy decided"
        # Unlike a human operator's private_reason, a provider's bounded reason is safe to show
        # the caller directly; it never touches private_reason/private_reason_redacted.
        assert result.decision.private_reason is None
        assert result.decision.private_reason_redacted is False

        operator_view = await store.get(result.id, OPERATOR)
        assert operator_view.decision is not None
        assert operator_view.decision.reason_code == "untrusted_capability"
        assert operator_view.decision.provider == "policy"
    finally:
        await service.close()


if __name__ == "__main__":
    pytest_bazel.main()
