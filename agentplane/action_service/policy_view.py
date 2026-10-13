"""What a caller may know about its own action policy, and what the operator may know about a subject's.

A caller cannot act on policy it cannot read: without this it has to submit and watch the Decision
to learn whether an Action would have waited for the operator. Both views are built from
`policy_evaluation.resolve_bindings`, the resolution admission uses, so what they report and what
a Decision auto-decides cannot drift: a refused set or an unsynced watch drops
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

from agentplane import policy_sets
from agentplane.action_service.models import PolicyKind, PolicySource
from agentplane.action_service.policies.argument_schema import ArgumentSchema
from agentplane.action_service.policies.exact_actions import ExactActions
from agentplane.action_service.policies.github_public_repository import GitHubPublicRepository
from agentplane.action_service.policies.github_repository import GitHubRepository
from agentplane.action_service.policies.home_assistant_entity_control import HomeAssistantEntityControl
from agentplane.action_service.policies.registry import Policy
from agentplane.action_service.policies.resources import ACTIONS_READY_CONDITION
from agentplane.action_service.policy_evaluation import resolve_bindings
from agentplane.action_service.policy_informer import PolicyIndex
from agentplane.action_service.providers import BindingKind, ResolvedBinding
from agentplane.policy_sets import ConditionStatus, PolicySet, RefusedPolicySet
from agentplane.subjects import ServiceAccountRef


class _View(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ServiceAccountTarget(_View):
    service_account: ServiceAccountRef


# Whose policy a caller asks for: its own, or a named subject.
type SelfTarget = Literal["self"]
SELF: Final[SelfTarget] = "self"
type PolicyTarget = SelfTarget | ServiceAccountTarget


class ReadyConditionView(_View):
    """The service's verdict on an object's spec, as its informer last wrote it."""

    status: ConditionStatus
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


class HomeAssistantEntityControlView(_View):
    type: Literal[PolicyKind.HOME_ASSISTANT_ENTITY_CONTROL]
    actions: dict[str, list[str]] = Field(description="Action names by ActionGroup key, sorted.")
    entities: dict[str, list[str]] = Field(
        description="The Home Assistant entity ids a call may target, each with the services it may use, sorted."
    )


PolicyView = Annotated[
    ExactActionsView
    | ArgumentSchemaView
    | GitHubRepositoryView
    | GitHubPublicRepositoryView
    | HomeAssistantEntityControlView,
    Field(discriminator="type"),
]


class EffectivePolicyView(_View):
    """One policy as admission walks it: the binding and set it came through and its position
    there, which is what a Decision's `MatchedPolicy` names."""

    binding: str
    policy_set: str
    source: PolicySource = Field(description="The list of the set the policy sits in.")
    index: int = Field(ge=0)
    policy: PolicyView


class _EffectivePolicy(_View):
    synced: bool = Field(
        description="Whether the service's copy of the policy objects is complete and still being refreshed. "
        "False while it is not -- including where a watch has wedged and the copy has stopped moving -- and "
        "then nothing auto-decides, every request takes the human path whatever the objects say, and the "
        "lists below are empty."
    )
    auto_approve_if: list[EffectivePolicyView] = Field(description="In evaluation order; the first match approves.")


class CallerBindingView(_View):
    name: str
    expires_at: datetime | None = Field(default=None, description="After this instant the binding contributes nothing.")
    policy_sets: list[str] = Field(
        description="The named sets that resolved, present and valid, in the binding's order."
    )


class CallerActionPolicyView(_EffectivePolicy):
    """What a subject's bindings auto-decide, as admission would resolve them now, in the form a
    caller may see: the caller's own subject by default, or one it named."""

    subject: ServiceAccountRef = Field(description="The ServiceAccount whose bindings these are.")
    bindings: list[CallerBindingView] = Field(
        description="The subject's unexpired, valid bindings in name order; empty means every request waits for the operator."
    )


class ActionPolicySetView(_View):
    name: str
    generation: int
    refused: str | None = Field(
        default=None,
        description="Why a rule does not parse, when one does not; such a set contributes nothing. A PolicySet has "
        "no status of its own: each binding's `ActionsReady` says whether its sets resolved.",
    )


class SubjectBindingView(_View):
    kind: BindingKind
    name: str
    labels: dict[str, str] = Field(
        description="The binding's metadata labels, for a reader that tells writers apart by them."
    )
    generation: int = Field(description="The binding's metadata generation; a Ready judged at an older one is stale.")
    expires_at: datetime | None = Field(default=None, description="After this instant the binding contributes nothing.")
    ready: ReadyConditionView | None = Field(
        default=None,
        description="The binding's `ActionsReady`; absent until the service has judged the binding at all.",
    )
    policy_sets: list[ActionPolicySetView] = Field(
        description="The named sets that exist, valid or refused, in the binding's order."
    )
    missing_policy_sets: list[str] = Field(description="Names in the binding that no set of its kind answers to.")


class SubjectActionPolicyView(_EffectivePolicy):
    """What a named subject's bindings auto-decide, for the operator: the caller view plus the
    verdicts and failures the caller is not shown."""

    bindings: list[SubjectBindingView] = Field(
        description="The subject's unexpired, valid bindings in name order, as admission would resolve them now."
    )


def _policy_view(policy: Policy) -> PolicyView:
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
        case HomeAssistantEntityControl(entities=entities):
            return HomeAssistantEntityControlView(
                type=PolicyKind.HOME_ASSISTANT_ENTITY_CONTROL,
                actions=actions,
                entities={entity_id: sorted(services) for entity_id, services in sorted(entities.items())},
            )


def _effective(bindings: tuple[ResolvedBinding, ...]) -> list[EffectivePolicyView]:
    """Auto-approve policies in the order `PolicySetDecisionProvider` walks them: binding, then set,
    then entry."""
    return [
        EffectivePolicyView(
            binding=resolved.name,
            policy_set=policy_set.name,
            source=policy_set.source,
            index=index,
            policy=_policy_view(policy),
        )
        for resolved in bindings
        for policy_set in resolved.policy_sets
        for index, policy in policy_set.policies
    ]


def caller_view(index: PolicyIndex, subject: ServiceAccountRef) -> CallerActionPolicyView:
    """The caller-facing view of a subject, from the same bindings admission resolves."""
    bindings = resolve_bindings(index, subject)
    return CallerActionPolicyView(
        subject=subject,
        synced=index.synced,
        bindings=[
            CallerBindingView(
                name=resolved.name,
                expires_at=resolved.expires_at,
                policy_sets=[policy_set.name for policy_set in resolved.policy_sets],
            )
            for resolved in bindings
        ],
        auto_approve_if=_effective(bindings),
    )


def subject_view(index: PolicyIndex, subject: ServiceAccountRef) -> SubjectActionPolicyView:
    """The operator's view of one subject: the same resolution, with each named set's standing."""
    bindings = resolve_bindings(index, subject)
    return SubjectActionPolicyView(
        synced=index.synced,
        bindings=[_subject_binding(index, resolved) for resolved in bindings],
        auto_approve_if=_effective(bindings),
    )


def _subject_binding(index: PolicyIndex, resolved: ResolvedBinding) -> SubjectBindingView:
    binding = index.policy_bindings[resolved.name]
    found = [(name, index.rule_sets.get(name)) for name in binding.spec.policy_sets]
    return SubjectBindingView(
        kind=resolved.kind,
        name=binding.metadata.name,
        generation=binding.metadata.generation,
        labels=binding.metadata.labels,
        expires_at=binding.spec.expires_at,
        ready=_ready_view(binding.condition(ACTIONS_READY_CONDITION)),
        policy_sets=[set_view(policy_set) for _, policy_set in found if policy_set is not None],
        missing_policy_sets=[name for name, policy_set in found if policy_set is None],
    )


def set_view(policy_set: PolicySet[Policy] | RefusedPolicySet) -> ActionPolicySetView:
    assert policy_set.generation is not None, "the informer's sets are read off the API server"
    return ActionPolicySetView(
        name=policy_set.name,
        generation=policy_set.generation,
        refused=policy_set.reason if isinstance(policy_set, RefusedPolicySet) else None,
    )


def _ready_view(condition: policy_sets.Condition | None) -> ReadyConditionView | None:
    if condition is None:
        return None
    return ReadyConditionView(
        status=ConditionStatus(condition.status),
        reason=condition.reason,
        message=condition.message,
        observed_generation=condition.observed_generation,
    )
