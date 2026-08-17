"""Composable, per-Agent auto-approval policies for haku-console MCP calls."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

import jsonschema
from fastmcp import FastMCP

from haku.console.mcp_config import (
    AnyOfAutoApprovalPolicy,
    AutoApprovalPolicy,
    ConsoleConfigFile,
    ExactToolsAutoApprovalPolicy,
    GitHubRepositoryAutoApprovalPolicy,
    GmailLabelNamespaceAutoApprovalPolicy,
    NeverAutoApprovalPolicy,
)
from haku.console.tool_call_actor import AgentActor, OperatorActor, ToolCallActor
from haku.console.tools.gmail_client import GmailToolsClient

logger = logging.getLogger(__name__)

AGENT_AUTO_APPROVAL_ID = "agent_policy_v1"
SCHEMA_AUTO_DENIAL_EVALUATION = "denied: arguments failed the registered tool schema"
GMAIL_LABEL_NAMESPACE_TOOLS = frozenset({"threads_modify_labels", "labels_patch", "labels_delete"})


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
class SchemaDenial:
    """Arguments failed an owned in-process tool's schema and can never execute."""

    reason: str
    evaluation: str = SCHEMA_AUTO_DENIAL_EVALUATION


@dataclass(frozen=True, slots=True)
class AutoApproved:
    explanation: str


@dataclass(frozen=True, slots=True)
class NotAutoApproved:
    reason: str


type AutoApprovalDecision = AutoApproved | NotAutoApproved


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


class AutoApprovalPolicyRegistry:
    """Validated policy graph plus static-Agent root assignments.

    Configuration validation has already guaranteed unique ids, valid references, an acyclic graph,
    and an explicit root for every static Agent. Dynamically enrolled/OAuth Agents absent from the
    deploy-time static list require manual approval.
    """

    def __init__(self, config: ConsoleConfigFile) -> None:
        self._policies: dict[str, AutoApprovalPolicy] = {policy.id: policy for policy in config.auto_approval_policies}
        # Retain deploy-time roots as a mixed-version fallback while durable assignments are seeded.
        self._static_agent_roots = {agent.agent_id: agent.auto_approval_policy for agent in config.static_agents}
        # Operators bypass the Agent approval lifecycle, but they share the reflected MCP catalog.
        # Preserve the useful transparent schema for any tool that an assigned Agent policy always
        # auto-approves; this affects presentation only, never Operator authorization.
        self._assigned_roots = tuple(self._policies)

    def _actor_root(self, actor: AgentActor) -> str | None:
        root = actor.auto_approval_policy or self._static_agent_roots.get(actor.agent_id)
        return root if root in self._policies else None

    def tool_mode(self, actor: ToolCallActor, server_id: str, tool_name: str) -> ToolAutoApprovalMode:
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
                    if server_id == server and tool_name in GMAIL_LABEL_NAMESPACE_TOOLS
                    else ToolAutoApprovalMode.MANUAL_APPROVAL_REQUIRED
                )
            case GitHubRepositoryAutoApprovalPolicy(server=server, tools=tools):
                return (
                    ToolAutoApprovalMode.CONDITIONALLY_AUTO_APPROVED
                    if server_id == server and tool_name in tools
                    else ToolAutoApprovalMode.MANUAL_APPROVAL_REQUIRED
                )
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
        actor: ToolCallActor,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        gmail: GmailToolsClient | None,
    ) -> tuple[str | None, str | None]:
        if not isinstance(actor, AgentActor):
            return None, None
        root = self._actor_root(actor)
        if root is None:
            return None, f"manual: Agent has no auto-approval policy for {server_id}/{tool_name}"

        evaluation = AutoApprovalEvaluation()
        await self._evaluate_policy(
            root,
            policy_path=(),
            server_id=server_id,
            tool_name=tool_name,
            arguments=arguments,
            gmail=gmail,
            evaluation=evaluation,
        )
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
                if server_id != server or tool_name not in GMAIL_LABEL_NAMESPACE_TOOLS:
                    return
                decision = await _evaluate_gmail_label_namespace(tool_name, arguments, label_prefix, gmail)
                evaluation.record(current_path, decision)
            case GitHubRepositoryAutoApprovalPolicy(server=server, owner=owner, repository=repository, tools=tools):
                if server_id != server or tool_name not in tools:
                    return
                evaluation.record(current_path, _evaluate_github_repository(arguments, owner, repository))
            case AnyOfAutoApprovalPolicy(policies=members):
                for member in members:
                    await self._evaluate_policy(
                        member,
                        policy_path=current_path,
                        server_id=server_id,
                        tool_name=tool_name,
                        arguments=arguments,
                        gmail=gmail,
                        evaluation=evaluation,
                    )
            case NeverAutoApprovalPolicy():
                evaluation.record(current_path, NotAutoApproved("policy never auto-approves"))


def _evaluate_github_repository(arguments: dict[str, Any], owner: str, repository: str) -> AutoApprovalDecision:
    actual_owner = arguments.get("owner")
    actual_repository = arguments.get("repo")
    if not isinstance(actual_owner, str) or not isinstance(actual_repository, str):
        return NotAutoApproved("call does not identify a repository with string owner/repo arguments")
    if (actual_owner.casefold(), actual_repository.casefold()) != (owner.casefold(), repository.casefold()):
        return NotAutoApproved(f"repository {actual_owner}/{actual_repository} is outside {owner}/{repository}")
    return AutoApproved(f"reviewed read targets repository {owner}/{repository}")


async def _validate_arguments(mcp: FastMCP, tool_name: str, arguments: dict[str, Any]) -> SchemaDenial | str | None:
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
        return SchemaDenial(reason=f"arguments failed the registered tool schema: {exc.message}")
    except jsonschema.SchemaError:
        logger.exception("auto-approval tool schema is invalid tool=%s", tool_name)
        return "error: registered tool schema is invalid"
    return None


async def auto_approve_tool_call(
    *,
    policies: AutoApprovalPolicyRegistry,
    actor: ToolCallActor,
    server_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    gmail: GmailToolsClient | None,
    mcp: FastMCP | None,
) -> tuple[str | None, str | None] | SchemaDenial:
    """Evaluate one call under the authenticated Agent's configured policy graph."""
    if not isinstance(actor, AgentActor):
        return None, None
    mode = policies.tool_mode(actor, server_id, tool_name)
    if mode is not ToolAutoApprovalMode.MANUAL_APPROVAL_REQUIRED and mcp is not None:
        error = await _validate_arguments(mcp, tool_name, arguments)
        if isinstance(error, SchemaDenial):
            return error
        if error is not None:
            return None, error
    return await policies.evaluate(
        actor=actor, server_id=server_id, tool_name=tool_name, arguments=arguments, gmail=gmail
    )


async def _evaluate_gmail_label_namespace(
    tool_name: str, arguments: dict[str, Any], label_prefix: str, gmail: GmailToolsClient | None
) -> AutoApprovalDecision:
    """Evaluate the reviewed Gmail label-mutation boundary."""
    try:

        def allows_label(name: str) -> bool:
            return name.startswith(label_prefix)

        if tool_name == "threads_modify_labels":
            add = arguments.get("add") or []
            remove = arguments.get("remove") or []
            if not add and not remove:
                return NotAutoApproved("no label changes requested")
            if set(add) & set(remove):
                return NotAutoApproved("a label cannot be both added and removed")
            if all(allows_label(name) for name in [*add, *remove]):
                return AutoApproved(f"all label names are under {label_prefix!r}")
            return NotAutoApproved(f"at least one label name is outside {label_prefix!r}")
        if gmail is None:
            raise RuntimeError("Gmail client is unavailable")
        if tool_name == "labels_patch":
            new_name = arguments.get("name")
            if (
                new_name is None
                or arguments.get("label_list_visibility") is not None
                or arguments.get("message_list_visibility") is not None
            ):
                return NotAutoApproved("label rename required without visibility changes")
            current = await asyncio.to_thread(gmail.labels_get, arguments["label_id"])
            if allows_label(current.name) and allows_label(new_name):
                return AutoApproved(f"current and new label names are under {label_prefix!r}")
            return NotAutoApproved(f"current or new label name is outside {label_prefix!r}")
        if tool_name == "labels_delete":
            current = await asyncio.to_thread(gmail.labels_get, arguments["label_id"])
            if allows_label(current.name):
                return AutoApproved(f"label name is under {label_prefix!r}")
            return NotAutoApproved(f"label name is outside {label_prefix!r}")
        return NotAutoApproved("Gmail operation is not handled by this policy")
    except Exception:
        logger.exception("auto-approval evaluation failed tool=%s", tool_name)
        return NotAutoApproved("Gmail auto-approval evaluation failed")
