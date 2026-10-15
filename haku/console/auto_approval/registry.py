"""The policy graph: validated Agent access profiles dispatching to per-kind evaluators."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

import jsonschema
from fastmcp import FastMCP

from haku.console.auto_approval.decision import AutoApprovalDecision, AutoApproved, AutoDenied, NotAutoApproved
from haku.console.mcp_config import (
    AnyOfAutoApprovalPolicy,
    AutoApprovalPolicy,
    ConsoleConfigFile,
    ExactToolsAutoApprovalPolicy,
    NeverAutoApprovalPolicy,
    RetiredGrantSelfListAutoApprovalPolicy,
)
from haku.console.tool_call_actor import AgentActor, OperatorActor, RuntimeActor

# The types-jsonschema stubs import referencing, so mypy needs the dist wherever
# jsonschema is imported; gazelle cannot see the dependency.
# gazelle:include_dep @pypi//referencing

logger = logging.getLogger(__name__)

AGENT_AUTO_APPROVAL_ID = "agent_policy_v1"
SCHEMA_AUTO_DENIAL_EVALUATION = "denied: arguments failed the registered tool schema"


class ToolAutoApprovalMode(IntEnum):
    """Whether the effective policy can auto-approve a tool.

    The ordering is intentional: an ``any_of`` policy takes the strongest mode among its members.
    Only ``ALWAYS_AUTO_APPROVED`` may use the transparent pass-through schema; manual calls retain
    the approval-request envelope.
    """

    MANUAL_APPROVAL_REQUIRED = 0
    ALWAYS_AUTO_APPROVED = 2


@dataclass(frozen=True)
class PolicyDenial:
    """A policy or schema check explicitly auto-denies the tool call."""

    reason: str
    evaluation: str


@dataclass(frozen=True, slots=True)
class AutoApprovalEvaluationStep:
    """One material result produced while walking a policy graph."""

    policy_path: tuple[str, ...]
    decision: AutoApprovalDecision


@dataclass(slots=True)
class AutoApprovalEvaluation:
    """Logger-like collector for the material decisions made by policy leaves."""

    steps: list[AutoApprovalEvaluationStep] = field(default_factory=list)

    def record(self, policy_path: tuple[str, ...], decision: AutoApprovalDecision) -> None:
        self.steps.append(AutoApprovalEvaluationStep(policy_path=policy_path, decision=decision))

    @property
    def approvals(self) -> tuple[AutoApprovalEvaluationStep, ...]:
        return tuple(step for step in self.steps if isinstance(step.decision, AutoApproved))

    @property
    def rejections(self) -> tuple[AutoApprovalEvaluationStep, ...]:
        return tuple(step for step in self.steps if isinstance(step.decision, NotAutoApproved))

    @property
    def denials(self) -> tuple[AutoApprovalEvaluationStep, ...]:
        return tuple(step for step in self.steps if isinstance(step.decision, AutoDenied))


class AutoApprovalPolicyRegistry:
    """Validated policy graph selected through durable, config-defined Agent access profiles."""

    def __init__(self, config: ConsoleConfigFile) -> None:
        self._config = config
        self._profiles = {profile.id: profile for profile in config.access_profiles}
        self._policies: dict[str, AutoApprovalPolicy] = {policy.id: policy for policy in config.auto_approval_policies}
        # Operators bypass the Agent approval lifecycle, but they share the reflected MCP catalog.
        # Preserve the useful transparent schema for any tool that an assigned Agent policy always
        # auto-approves; this affects presentation only, never Operator authorization.
        self._assigned_roots = tuple(dict.fromkeys(profile.auto_approval_policy for profile in self._profiles.values()))

    def _actor_root(self, actor: AgentActor) -> str | None:
        profile = self._profiles.get(actor.access_profile_id) if actor.access_profile_id is not None else None
        return profile.auto_approval_policy if profile is not None else None

    def tool_mode(self, actor: RuntimeActor, server_id: str, tool_name: str) -> ToolAutoApprovalMode:
        if server_id == "grants":
            # An older ConfigMap may remain during a rolling update. Its exact-tools leaves must
            # not retain auto-approval after the server backend has been removed.
            return ToolAutoApprovalMode.MANUAL_APPROVAL_REQUIRED
        if isinstance(actor, AgentActor):
            root = self._actor_root(actor)
            if root is None:
                return ToolAutoApprovalMode.MANUAL_APPROVAL_REQUIRED
            return self._policy_mode(root, server_id, tool_name)
        assert isinstance(actor, OperatorActor)
        return max(
            (self._policy_mode(root, server_id, tool_name) for root in self._assigned_roots),
            default=ToolAutoApprovalMode.MANUAL_APPROVAL_REQUIRED,
        )

    def _policy_mode(self, policy_id: str, server_id: str, tool_name: str) -> ToolAutoApprovalMode:
        policy = self._policies[policy_id]
        match policy:
            case ExactToolsAutoApprovalPolicy(tools=tools):
                return (
                    ToolAutoApprovalMode.ALWAYS_AUTO_APPROVED
                    if tool_name in tools.get(server_id, ())
                    else ToolAutoApprovalMode.MANUAL_APPROVAL_REQUIRED
                )
            case RetiredGrantSelfListAutoApprovalPolicy():
                return ToolAutoApprovalMode.MANUAL_APPROVAL_REQUIRED
            case AnyOfAutoApprovalPolicy(policies=members):
                return max(
                    (self._policy_mode(member, server_id, tool_name) for member in members),
                    default=ToolAutoApprovalMode.MANUAL_APPROVAL_REQUIRED,
                )
            case NeverAutoApprovalPolicy():
                return ToolAutoApprovalMode.MANUAL_APPROVAL_REQUIRED

    async def evaluate(
        self, *, actor: RuntimeActor, server_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> tuple[str | None, str | None] | PolicyDenial:
        if not isinstance(actor, AgentActor):
            return None, None
        if server_id == "grants":
            return None, "manual: the grants server has been retired"

        root = self._actor_root(actor)
        if root is None:
            return None, f"manual: Agent has no configured access profile for {server_id}/{tool_name}"

        evaluation = AutoApprovalEvaluation()
        await self._evaluate_policy(
            root, policy_path=(), server_id=server_id, tool_name=tool_name, arguments=arguments, evaluation=evaluation
        )
        if evaluation.denials:
            step = evaluation.denials[0]
            assert isinstance(step.decision, AutoDenied)
            return PolicyDenial(reason=step.decision.reason, evaluation=step.decision.evaluation)
        if evaluation.approvals:
            rendered = "; ".join(
                f"{' -> '.join(step.policy_path)}: {step.decision.explanation}"
                for step in evaluation.approvals
                if isinstance(step.decision, AutoApproved)
            )
            return AGENT_AUTO_APPROVAL_ID, f"approved: Agent policy {root!r} matched {rendered}"
        reasons = tuple(
            f"{step.policy_path[-1]}: {step.decision.reason}"
            for step in evaluation.rejections
            if isinstance(step.decision, NotAutoApproved)
        )
        detail = f" ({'; '.join(reasons)})" if reasons else ""
        return None, f"manual: Agent policy {root!r} did not auto-approve {server_id}/{tool_name}{detail}"

    async def _evaluate_policy(
        self,
        policy_id: str,
        *,
        policy_path: tuple[str, ...],
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        evaluation: AutoApprovalEvaluation,
    ) -> None:
        policy = self._policies[policy_id]
        current_path = (*policy_path, policy.id)
        match policy:
            case ExactToolsAutoApprovalPolicy(tools=tools):
                if tool_name not in tools.get(server_id, ()):
                    return
                evaluation.record(current_path, AutoApproved(f"exact tool {server_id}/{tool_name} is listed"))
            case RetiredGrantSelfListAutoApprovalPolicy():
                evaluation.record(current_path, NotAutoApproved("manual: the grants server has been retired"))
            case AnyOfAutoApprovalPolicy(policies=members):
                for member in members:
                    await self._evaluate_policy(
                        member,
                        policy_path=current_path,
                        server_id=server_id,
                        tool_name=tool_name,
                        arguments=arguments,
                        evaluation=evaluation,
                    )
            case NeverAutoApprovalPolicy():
                evaluation.record(current_path, NotAutoApproved("policy never auto-approves"))


async def _validate_arguments(mcp: FastMCP, tool_name: str, arguments: dict[str, Any]) -> PolicyDenial | str | None:
    """Validate arguments against an owned in-process tool's generated schema."""
    try:
        tool = await mcp.get_tool(tool_name)
        if tool is None:
            raise RuntimeError(f"tool {tool_name!r} is unavailable")
    except Exception:
        logger.exception("auto-approval tool lookup failed tool=%s", tool_name)
        return "error: registered tool lookup failed"
    try:
        jsonschema.validate(instance=arguments, schema=tool.to_mcp_tool().input_schema)
    except jsonschema.ValidationError as exc:
        logger.warning("auto-denied invalid MCP arguments tool=%s: %s", tool_name, exc)
        return PolicyDenial(
            reason=f"arguments failed the registered tool schema: {exc.message}",
            evaluation=SCHEMA_AUTO_DENIAL_EVALUATION,
        )
    except jsonschema.SchemaError:
        logger.exception("auto-approval tool schema is invalid tool=%s", tool_name)
        return "error: registered tool schema is invalid"
    return None


async def auto_approve_tool_call(
    *,
    policies: AutoApprovalPolicyRegistry,
    actor: RuntimeActor,
    server_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    mcp: FastMCP | None,
) -> tuple[str | None, str | None] | PolicyDenial:
    """Evaluate one call under the authenticated Agent's configured policy graph."""
    if not isinstance(actor, AgentActor):
        return None, None
    mode = policies.tool_mode(actor, server_id, tool_name)
    if mode is not ToolAutoApprovalMode.MANUAL_APPROVAL_REQUIRED and mcp is not None:
        error = await _validate_arguments(mcp, tool_name, arguments)
        if isinstance(error, PolicyDenial):
            return error
        if error is not None:
            return None, error
    return await policies.evaluate(actor=actor, server_id=server_id, tool_name=tool_name, arguments=arguments)
