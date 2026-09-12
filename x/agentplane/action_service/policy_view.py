"""What a caller may know about its own action policy, and what the operator may know about a subject's.

A caller cannot act on policy it cannot read: without this it has to submit and watch the Decision
to learn whether an Action would have waited for the operator. Both views are built from
`policy_evaluation.resolve_bindings`, the resolution admission uses, so what they report and what
a Decision auto-decides cannot drift: an expired binding, a refused set or an unsynced watch drops
out of both at once.

The caller view is redacted by construction, as the egress proxy's `agent_view` is: it is built
from its own field list, never from a resource wholesale. A set that is missing or refused
contributes nothing and simply is not there; Ready conditions, validation reports and labels are
the operator's to read, in the subject view.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from x.agentplane.action_service.models import PolicyKind, SandboxCaller, ServiceAccountCaller, ServiceAccountRef
from x.agentplane.action_service.policies.argument_schema import ArgumentSchema
from x.agentplane.action_service.policies.exact_actions import ExactActions
from x.agentplane.action_service.policies.github_public_repository import GitHubPublicRepository
from x.agentplane.action_service.policies.github_repository import GitHubRepository
from x.agentplane.action_service.policies.registry import Policy
from x.agentplane.action_service.policies.resources import (
    ActionPolicyBinding,
    ActionPolicySet,
    Condition,
    InvalidResource,
)
from x.agentplane.action_service.policy_evaluation import resolve_bindings
from x.agentplane.action_service.policy_informer import PolicyIndex, namespaced_key
from x.agentplane.action_service.providers import ResolvedBinding

# What a binding's subject names, as the operator asks about it: a live Sandbox by namespace and
# UID, or a ServiceAccount. A caller is one of these behind its authentication.
type PolicySubject = SandboxCaller | ServiceAccountRef


class _View(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SandboxTarget(_View):
    """A live Sandbox as the subject to read, by the namespace and UID a binding pins."""

    sandbox: SandboxCaller


class ServiceAccountTarget(_View):
    service_account: ServiceAccountRef


# Whose policy a caller asks for: its own, or a named subject.
type SelfTarget = Literal["self"]
SELF: Final[SelfTarget] = "self"
type PolicyTarget = SelfTarget | SandboxTarget | ServiceAccountTarget


class ReadyConditionView(_View):
    """The service's verdict on an object's spec, as its informer last wrote it."""

    status: Literal["True", "False", "Unknown"]
    reason: str
    message: str
    observed_generation: int | None = Field(
        default=None, description="The generation judged; behind the object's while an edit is unjudged."
    )


class ExactActionsView(_View):
    type: Literal[PolicyKind.EXACT_ACTIONS]
    actions: dict[str, list[str]] = Field(description="Action names by ActionGroup key, sorted.")


class ArgumentSchemaView(_View):
    type: Literal[PolicyKind.ARGUMENT_SCHEMA]
    actions: dict[str, list[str]] = Field(description="Action names by ActionGroup key, sorted.")
    argument_schema: dict[str, JsonValue] = Field(description="The JSON Schema the arguments must satisfy.")


class GitHubRepositoryView(_View):
    type: Literal[PolicyKind.GITHUB_REPOSITORY]
    actions: dict[str, list[str]] = Field(description="Action names by ActionGroup key, sorted.")
    owner: str = Field(description="The GitHub repository owner the call must target.")
    repository: str = Field(description="The GitHub repository name the call must target.")


class GitHubPublicRepositoryView(_View):
    type: Literal[PolicyKind.GITHUB_PUBLIC_REPOSITORY]
    actions: dict[str, list[str]] = Field(description="Action names by ActionGroup key, sorted.")


PolicyView = Annotated[
    ExactActionsView | ArgumentSchemaView | GitHubRepositoryView | GitHubPublicRepositoryView,
    Field(discriminator="type"),
]


class EffectivePolicyView(_View):
    """One policy as admission walks it: the binding and set it came through and its position
    there, which is what a Decision's `MatchedPolicy` names."""

    binding: str
    policy_set: str
    index: int = Field(ge=0)
    policy: PolicyView


class _EffectivePolicy(_View):
    synced: bool = Field(
        description="Whether the service's watch has synced. Until it has, nothing auto-decides and every "
        "request takes the human path, whatever the objects say; the lists below are then empty."
    )
    auto_approve_if: list[EffectivePolicyView] = Field(description="In evaluation order; the first match approves.")
    auto_deny_if: list[EffectivePolicyView] = Field(
        description="In evaluation order. Accepted and reported; this version decides nothing from it."
    )
    auto_deny_unless: list[EffectivePolicyView] = Field(
        description="In evaluation order. Accepted and reported; this version decides nothing from it."
    )


class CallerBindingView(_View):
    name: str
    expires_at: datetime | None = Field(default=None, description="After this instant the binding contributes nothing.")
    policy_sets: list[str] = Field(
        description="The named sets that resolved, present and valid, in the binding's order."
    )


class CallerActionPolicyView(_EffectivePolicy):
    """What a subject's bindings auto-decide, as admission would resolve them now, in the form a
    caller may see: the caller's own subject by default, or one it named."""

    subject: SandboxCaller | ServiceAccountRef = Field(
        description="Whose bindings these are: a Sandbox by namespace and UID, or a ServiceAccount."
    )
    bindings: list[CallerBindingView] = Field(
        description="The subject's unexpired, valid bindings in name order; empty means every request waits for the operator."
    )


class ActionPolicySetView(_View):
    name: str
    generation: int
    ready: ReadyConditionView | None = Field(
        default=None, description="Absent until the service has judged the set at all."
    )
    refused: str | None = Field(
        default=None, description="Why the spec does not parse, when it does not; such a set contributes nothing."
    )


class SubjectBindingView(_View):
    name: str
    labels: dict[str, str] = Field(
        description="The binding's metadata labels, for a reader that tells writers apart by them."
    )
    expires_at: datetime | None = Field(default=None, description="After this instant the binding contributes nothing.")
    ready: ReadyConditionView | None = Field(
        default=None, description="Absent until the service has judged the binding at all."
    )
    policy_sets: list[ActionPolicySetView] = Field(
        description="The named sets that exist, valid or refused, in the binding's order."
    )
    missing_policy_sets: list[str] = Field(description="Names in the binding that no ActionPolicySet answers to.")


class SubjectActionPolicyView(_EffectivePolicy):
    """What a named subject's bindings auto-decide, for the operator: the caller view plus the
    verdicts and failures the caller is not shown."""

    bindings: list[SubjectBindingView] = Field(
        description="The subject's unexpired, valid bindings in name order, as admission would resolve them now."
    )


def _policy_view(
    policy: Policy,
) -> ExactActionsView | ArgumentSchemaView | GitHubRepositoryView | GitHubPublicRepositoryView:
    actions = {group: sorted(names) for group, names in sorted(policy.actions.items())}
    match policy:
        case ExactActions():
            return ExactActionsView(type=PolicyKind.EXACT_ACTIONS, actions=actions)
        case ArgumentSchema(argument_schema=schema):
            return ArgumentSchemaView(type=PolicyKind.ARGUMENT_SCHEMA, actions=actions, argument_schema=schema)
        case GitHubRepository(owner=owner, repository=repository):
            return GitHubRepositoryView(
                type=PolicyKind.GITHUB_REPOSITORY, actions=actions, owner=owner, repository=repository
            )
        case GitHubPublicRepository():
            return GitHubPublicRepositoryView(type=PolicyKind.GITHUB_PUBLIC_REPOSITORY, actions=actions)


def _effective(
    bindings: tuple[ResolvedBinding, ...],
) -> tuple[list[EffectivePolicyView], list[EffectivePolicyView], list[EffectivePolicyView]]:
    """The three lists in the order `PolicySetDecisionProvider` walks them: binding, then set, then entry."""
    auto_approve_if: list[EffectivePolicyView] = []
    auto_deny_if: list[EffectivePolicyView] = []
    auto_deny_unless: list[EffectivePolicyView] = []
    for resolved in bindings:
        for policy_set in resolved.policy_sets:
            for source, target in (
                (policy_set.spec.auto_approve_if, auto_approve_if),
                (policy_set.spec.auto_deny_if, auto_deny_if),
                (policy_set.spec.auto_deny_unless, auto_deny_unless),
            ):
                target.extend(
                    EffectivePolicyView(
                        binding=resolved.binding.metadata.name,
                        policy_set=policy_set.metadata.name,
                        index=index,
                        policy=_policy_view(policy),
                    )
                    for index, policy in enumerate(source)
                )
    return auto_approve_if, auto_deny_if, auto_deny_unless


def caller_view(
    index: PolicyIndex, subject: PolicySubject | ServiceAccountCaller, now: datetime
) -> CallerActionPolicyView:
    """The caller-facing view of a subject, from the same bindings admission resolves."""
    bindings = resolve_bindings(index, subject, now)
    auto_approve_if, auto_deny_if, auto_deny_unless = _effective(bindings)
    return CallerActionPolicyView(
        subject=subject.service_account if isinstance(subject, ServiceAccountCaller) else subject,
        synced=index.synced,
        bindings=[
            CallerBindingView(
                name=resolved.binding.metadata.name,
                expires_at=resolved.binding.spec.expires_at,
                policy_sets=[policy_set.metadata.name for policy_set in resolved.policy_sets],
            )
            for resolved in bindings
        ],
        auto_approve_if=auto_approve_if,
        auto_deny_if=auto_deny_if,
        auto_deny_unless=auto_deny_unless,
    )


def subject_view(index: PolicyIndex, subject: PolicySubject, now: datetime) -> SubjectActionPolicyView:
    """The operator's view of one subject: the same resolution, with each named set's standing."""
    bindings = resolve_bindings(index, subject, now)
    auto_approve_if, auto_deny_if, auto_deny_unless = _effective(bindings)
    return SubjectActionPolicyView(
        synced=index.synced,
        bindings=[_subject_binding(index, resolved.binding) for resolved in bindings],
        auto_approve_if=auto_approve_if,
        auto_deny_if=auto_deny_if,
        auto_deny_unless=auto_deny_unless,
    )


def _subject_binding(index: PolicyIndex, binding: ActionPolicyBinding) -> SubjectBindingView:
    # In the binding's own order, duplicates included: what `resolve_bindings` walks.
    named = [
        (name, index.policy_sets.get(namespaced_key(binding.metadata.namespace, name)))
        for name in binding.spec.policy_sets
    ]
    return SubjectBindingView(
        name=binding.metadata.name,
        labels=binding.metadata.labels,
        expires_at=binding.spec.expires_at,
        ready=_ready_view(binding.status.ready()),
        policy_sets=[_set_view(policy_set) for _, policy_set in named if policy_set is not None],
        missing_policy_sets=[name for name, policy_set in named if policy_set is None],
    )


def _set_view(policy_set: ActionPolicySet | InvalidResource) -> ActionPolicySetView:
    return ActionPolicySetView(
        name=policy_set.metadata.name,
        generation=policy_set.metadata.generation,
        ready=_ready_view(policy_set.status.ready()),
        refused=policy_set.message if isinstance(policy_set, InvalidResource) else None,
    )


def _ready_view(condition: Condition | None) -> ReadyConditionView | None:
    if condition is None:
        return None
    return ReadyConditionView(
        status=condition.status,
        reason=condition.reason,
        message=condition.message,
        observed_generation=condition.observed_generation,
    )
