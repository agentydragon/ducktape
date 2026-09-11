"""The policy graph: validated Agent access profiles dispatching to per-kind evaluators."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

import jsonschema
from fastmcp import FastMCP

from haku.console.auto_approval.decision import AutoApprovalDecision, AutoApproved, AutoDenied, NotAutoApproved
from haku.console.auto_approval.github import (
    GitHubRepositoryVisibilityService,
    evaluate_fixed_repository,
    evaluate_public_repository,
)
from haku.console.auto_approval.gmail import LABEL_NAMESPACE_TOOLS, evaluate_label_namespace
from haku.console.auto_approval.home_assistant import CALL_SERVICE_TOOL, evaluate_entity_control
from haku.console.auto_approval.kubernetes import evaluate_passthrough_redundancy
from haku.console.grants.kubernetes.authorization_service import KubernetesAuthorizationService
from haku.console.mcp_config import (
    AnyOfAutoApprovalPolicy,
    AutoApprovalPolicy,
    ConsoleConfigFile,
    ExactToolsAutoApprovalPolicy,
    GitHubPublicRepositoryAutoApprovalPolicy,
    GitHubRepositoryAutoApprovalPolicy,
    GmailLabelNamespaceAutoApprovalPolicy,
    GrantSelfListAutoApprovalPolicy,
    HomeAssistantEntityControlAutoApprovalPolicy,
    KubernetesPassthroughAutoApprovalPolicy,
    NeverAutoApprovalPolicy,
)
from haku.console.tool_call_actor import AgentActor, OperatorActor, RuntimeActor
from haku.console.tools.gmail_client import GmailToolsClient

# The types-jsonschema stubs import referencing, so mypy needs the dist wherever
# jsonschema is imported; gazelle cannot see the dependency.
# gazelle:include_dep @pypi//referencing

logger = logging.getLogger(__name__)

AGENT_AUTO_APPROVAL_ID = "agent_policy_v1"
SCHEMA_AUTO_DENIAL_EVALUATION = "denied: arguments failed the registered tool schema"

# The `grants` server's own-grant list read and the scope value that makes it click-free. Mirrors
# `haku.console.tools.grants` (`list_grants`, `GrantPrincipalInput`) without importing the tool layer into
# the policy engine.
_LIST_GRANTS_TOOL = "list_grants"
_OWN_GRANT_SCOPE = "self"


class ToolAutoApprovalMode(IntEnum):
    """Whether the effective policy can auto-approve a tool.

    The ordering is intentional: an ``any_of`` policy takes the strongest mode among its members.
    Only ``ALWAYS_AUTO_APPROVED`` may use the transparent pass-through schema; conditional and
    manual calls retain the approval-request envelope.
    """

    MANUAL_APPROVAL_REQUIRED = 0
    CONDITIONALLY_AUTO_APPROVED = 1
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

    def __init__(
        self,
        config: ConsoleConfigFile,
        *,
        kubernetes_authorization: KubernetesAuthorizationService | None = None,
        github_repository_visibility: GitHubRepositoryVisibilityService | None = None,
    ) -> None:
        self._config = config
        self._profiles = {profile.id: profile for profile in config.access_profiles}
        self._policies: dict[str, AutoApprovalPolicy] = {policy.id: policy for policy in config.auto_approval_policies}
        # Operators bypass the Agent approval lifecycle, but they share the reflected MCP catalog.
        # Preserve the useful transparent schema for any tool that an assigned Agent policy always
        # auto-approves; this affects presentation only, never Operator authorization.
        self._assigned_roots = tuple(dict.fromkeys(profile.auto_approval_policy for profile in self._profiles.values()))
        self._kubernetes_authorization = kubernetes_authorization
        self._github_repository_visibility = github_repository_visibility

    def _actor_root(self, actor: AgentActor) -> str | None:
        profile = self._profiles.get(actor.access_profile_id) if actor.access_profile_id is not None else None
        return profile.auto_approval_policy if profile is not None else None

    def tool_mode(self, actor: RuntimeActor, server_id: str, tool_name: str) -> ToolAutoApprovalMode:
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
            case GmailLabelNamespaceAutoApprovalPolicy(server=server):
                return (
                    ToolAutoApprovalMode.CONDITIONALLY_AUTO_APPROVED
                    if server_id == server and tool_name in LABEL_NAMESPACE_TOOLS
                    else ToolAutoApprovalMode.MANUAL_APPROVAL_REQUIRED
                )
            case GitHubRepositoryAutoApprovalPolicy(server=server, tools=tools):
                return (
                    ToolAutoApprovalMode.CONDITIONALLY_AUTO_APPROVED
                    if server_id == server and tool_name in tools
                    else ToolAutoApprovalMode.MANUAL_APPROVAL_REQUIRED
                )
            case GitHubPublicRepositoryAutoApprovalPolicy(server=server, tools=tools):
                return (
                    ToolAutoApprovalMode.CONDITIONALLY_AUTO_APPROVED
                    if server_id == server and tool_name in tools
                    else ToolAutoApprovalMode.MANUAL_APPROVAL_REQUIRED
                )
            case GrantSelfListAutoApprovalPolicy(server=server):
                return (
                    ToolAutoApprovalMode.CONDITIONALLY_AUTO_APPROVED
                    if server_id == server and tool_name == _LIST_GRANTS_TOOL
                    else ToolAutoApprovalMode.MANUAL_APPROVAL_REQUIRED
                )
            case HomeAssistantEntityControlAutoApprovalPolicy(server=server):
                return (
                    ToolAutoApprovalMode.CONDITIONALLY_AUTO_APPROVED
                    if server_id == server and tool_name == CALL_SERVICE_TOOL
                    else ToolAutoApprovalMode.MANUAL_APPROVAL_REQUIRED
                )
            case KubernetesPassthroughAutoApprovalPolicy():
                return ToolAutoApprovalMode.MANUAL_APPROVAL_REQUIRED
            case AnyOfAutoApprovalPolicy(policies=members):
                return max(
                    (self._policy_mode(member, server_id, tool_name) for member in members),
                    default=ToolAutoApprovalMode.MANUAL_APPROVAL_REQUIRED,
                )
            case NeverAutoApprovalPolicy():
                return ToolAutoApprovalMode.MANUAL_APPROVAL_REQUIRED

    async def evaluate(
        self,
        *,
        actor: RuntimeActor,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        gmail: GmailToolsClient | None,
    ) -> tuple[str | None, str | None] | PolicyDenial:
        if not isinstance(actor, AgentActor):
            return None, None

        root = self._actor_root(actor)
        if root is None:
            return None, f"manual: Agent has no configured access profile for {server_id}/{tool_name}"

        evaluation = AutoApprovalEvaluation()
        await self._evaluate_policy(
            root,
            actor=actor,
            policy_path=(),
            server_id=server_id,
            tool_name=tool_name,
            arguments=arguments,
            gmail=gmail,
            evaluation=evaluation,
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
        actor: RuntimeActor,
        policy_path: tuple[str, ...],
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        gmail: GmailToolsClient | None,
        evaluation: AutoApprovalEvaluation,
    ) -> None:
        policy = self._policies[policy_id]
        current_path = (*policy_path, policy.id)
        match policy:
            case ExactToolsAutoApprovalPolicy(tools=tools):
                if tool_name not in tools.get(server_id, ()):
                    return
                evaluation.record(current_path, AutoApproved(f"exact tool {server_id}/{tool_name} is listed"))
            case GmailLabelNamespaceAutoApprovalPolicy(server=server, label_prefix=label_prefix):
                if server_id != server or tool_name not in LABEL_NAMESPACE_TOOLS:
                    return
                decision = await evaluate_label_namespace(tool_name, arguments, label_prefix, gmail)
                evaluation.record(current_path, decision)
            case GitHubRepositoryAutoApprovalPolicy(server=server, owner=owner, repository=repository, tools=tools):
                if server_id != server or tool_name not in tools:
                    return
                evaluation.record(current_path, evaluate_fixed_repository(tool_name, arguments, owner, repository))
            case GitHubPublicRepositoryAutoApprovalPolicy(server=server, tools=tools):
                if server_id != server or tool_name not in tools:
                    return
                decision = await evaluate_public_repository(tool_name, arguments, self._github_repository_visibility)
                evaluation.record(current_path, decision)
            case GrantSelfListAutoApprovalPolicy(server=server):
                if server_id != server or tool_name != _LIST_GRANTS_TOOL:
                    return
                if arguments.get("principal") == _OWN_GRANT_SCOPE:
                    evaluation.record(
                        current_path,
                        AutoApproved(
                            "list_grants is scoped to the caller's own grants (principal=self), including history when requested"
                        ),
                    )
                else:
                    evaluation.record(
                        current_path,
                        NotAutoApproved(
                            "list_grants auto-approves only with principal=self; all-grants and named reads are manual"
                        ),
                    )
            case HomeAssistantEntityControlAutoApprovalPolicy(server=server, entities=entities):
                if server_id != server or tool_name != CALL_SERVICE_TOOL:
                    return
                evaluation.record(current_path, evaluate_entity_control(tool_name, arguments, entities))
            case KubernetesPassthroughAutoApprovalPolicy(server=server):
                if server_id != server:
                    return
                passthrough_decision = await evaluate_passthrough_redundancy(
                    actor, tool_name, arguments, self._kubernetes_authorization
                )
                if passthrough_decision is not None:
                    evaluation.record(current_path, passthrough_decision)
            case AnyOfAutoApprovalPolicy(policies=members):
                for member in members:
                    await self._evaluate_policy(
                        member,
                        actor=actor,
                        policy_path=current_path,
                        server_id=server_id,
                        tool_name=tool_name,
                        arguments=arguments,
                        gmail=gmail,
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
        jsonschema.validate(instance=arguments, schema=tool.to_mcp_tool().inputSchema)
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
    gmail: GmailToolsClient | None,
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
    return await policies.evaluate(
        actor=actor, server_id=server_id, tool_name=tool_name, arguments=arguments, gmail=gmail
    )
