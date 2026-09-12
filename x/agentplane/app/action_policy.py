"""What the Action Service auto-decides for a Sandbox, as the app writes and shows it: the
ActionPolicyBindings naming the Sandbox and the ActionPolicySets they reference.

The Action Service (x/agentplane/action_service) enforces these resources; the app writes one
binding per Sandbox it creates, from the preset's set list, and reads the same objects back to
show what the service would evaluate at admission: the unexpired bindings whose subject is the live
Sandbox, the sets each names, and the three lists that result, in the order the service walks
them. Nothing here is in the decision path, and nothing edits a binding at runtime.

The kinds themselves are `x.agentplane.action_service.policy_resources`, shared with the service
that enforces them, so a set the app shows as valid is one the service parses the same way.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from util.kubernetes import CustomObjectsClient
from x.agentplane.action_service.models import PolicyKind
from x.agentplane.action_service.policy_resources import (
    BINDINGS_PLURAL,
    GROUP,
    POLICY_SETS_PLURAL,
    VERSION,
    ActionPolicyBinding,
    ActionPolicySet,
    ArgumentSchemaPolicy,
    Condition,
    ExactActionsPolicy,
    InvalidResource,
    SandboxSubject,
    parse_binding,
    parse_policy_set,
)
from x.agentplane.app.egress import FLUX_KUSTOMIZATION_LABEL
from x.agentplane.app.inventory import SANDBOX_API, InventoryError

ACTION_POLICY_API = (GROUP, VERSION)
# Stamped on every binding the app writes, so a reader can tell it from one the operator wrote with
# kubectl. Which preset selected the sets is on the Sandbox's own annotation, not repeated here.
MANAGED_BY_LABEL = "app.agentplane.allegedly.works/managed-by"
MANAGED_BY_APP = "integration-app"


class UnknownPolicySetError(InventoryError):
    """A binding naming a set the namespace does not hold, which would grant nothing.

    The CRD admits any string in `spec.policySets` and the Action Service treats a name that
    resolves to nothing as contributing nothing, so a dangling name is a state the system already
    handles. This refuses one at the moment it would be written; one the operator deletes
    afterwards still lands there without granting anything from the missing set.
    """

    def __init__(self, names: list[str]) -> None:
        super().__init__(f"no ActionPolicySet in the namespace is named {', '.join(names)}")
        self.names = names


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _Labels(_Wire):
    labels: dict[str, str] = Field(default_factory=dict)


class _Labelled(_Wire):
    """The one metadata field the service's own models leave unread and the app decides from."""

    metadata: _Labels


class _ResourceList(_Wire):
    items: list[dict[str, Any]]


# API views.


class BindingProvenance(StrEnum):
    GIT = "git"
    APP = "app"
    OPERATOR = "operator"


class ReadyConditionView(BaseModel):
    """The Action Service's verdict on an object's spec, as its informer last wrote it."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["True", "False", "Unknown"]
    reason: str
    message: str
    observed_generation: int | None = Field(
        default=None, description="The generation judged; behind the object's while an edit is unjudged."
    )


class ExactActionsView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal[PolicyKind.EXACT_ACTIONS]
    actions: dict[str, list[str]] = Field(description="Action names by ActionGroup key, sorted.")


class ArgumentSchemaView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal[PolicyKind.ARGUMENT_SCHEMA]
    actions: dict[str, list[str]] = Field(description="Action names by ActionGroup key, sorted.")
    argument_schema: dict[str, JsonValue] = Field(description="The JSON Schema the arguments must satisfy.")


PolicyView = Annotated[ExactActionsView | ArgumentSchemaView, Field(discriminator="type")]


class ActionPolicySetView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    generation: int
    ready: ReadyConditionView | None = Field(
        default=None, description="Absent until the Action Service has judged the set at all."
    )
    refused: str | None = Field(
        default=None, description="Why the spec does not parse, when it does not; such a set contributes nothing."
    )


class ActionPolicyBindingView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    provenance: BindingProvenance = Field(
        description="git: Flux applied it; app: this app wrote it at Sandbox creation; operator: anything else."
    )
    expires_at: datetime | None = None
    ready: ReadyConditionView | None = Field(
        default=None, description="Absent until the Action Service has judged the binding at all."
    )
    policy_sets: list[ActionPolicySetView] = Field(description="The named sets that exist, in the binding's order.")
    missing_policy_sets: list[str] = Field(description="Names in the binding that no ActionPolicySet answers to.")


class EffectivePolicyView(BaseModel):
    """One policy as the Action Service walks it: the binding and set it came through, and its
    position there, which is what a Decision's evidence names."""

    model_config = ConfigDict(extra="forbid")

    binding: str
    policy_set: str
    index: int
    policy: PolicyView


class ActionPolicyView(BaseModel):
    """What the Action Service would evaluate for the Sandbox at admission, from the objects as they
    stand: deny wins over approve, a request matching nothing takes the human path."""

    model_config = ConfigDict(extra="forbid")

    bindings: list[ActionPolicyBindingView] = Field(
        description="The unexpired, valid bindings whose subject is the live Sandbox, in name order."
    )
    auto_approve_if: list[EffectivePolicyView] = Field(description="In evaluation order; the first match approves.")
    auto_deny_if: list[EffectivePolicyView]
    auto_deny_unless: list[EffectivePolicyView]


class ActionPolicyInventory:
    """The namespace's sets and bindings, read and written through the API server."""

    def __init__(
        self,
        *,
        namespace: str,
        custom_objects: CustomObjectsClient,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._namespace = namespace
        self._custom_objects = custom_objects
        self._clock = clock

    async def require_policy_sets(self, names: Sequence[str]) -> None:
        """Every name must resolve to a set the namespace holds, or nothing is written."""
        _require_known(names, await self._policy_sets_by_name())

    async def for_sandbox(self, sandbox_uid: UUID) -> ActionPolicyView:
        """The Sandbox's policy as the Action Service resolves it; the UID is what a subject pins."""
        bindings, policy_sets = await asyncio.gather(self._list(BINDINGS_PLURAL), self._list(POLICY_SETS_PLURAL))
        return action_policy_view(
            _ResourceList.model_validate(bindings).items,
            _ResourceList.model_validate(policy_sets).items,
            sandbox_uid=sandbox_uid,
            now=self._clock(),
        )

    async def bind(self, *, sandbox: str, sandbox_uid: UUID, policy_sets: Sequence[str]) -> ActionPolicyBindingView:
        """One binding of the Sandbox to the sets, owned by the Sandbox so its deletion
        garbage-collects it. Creating it is the whole grant; the Action Service reads `spec` and
        learns nothing of which preset chose the sets.
        """
        known = await self._policy_sets_by_name()
        _require_known(policy_sets, known)
        created = await self._custom_objects.create_namespaced_custom_object(
            *ACTION_POLICY_API,
            self._namespace,
            BINDINGS_PLURAL,
            {
                "apiVersion": "/".join(ACTION_POLICY_API),
                "kind": "ActionPolicyBinding",
                "metadata": {
                    # The API server names it, as it does the egress binding: a Sandbox may be
                    # bound again later, and a name derived from the Sandbox alone would 409.
                    "generateName": f"{sandbox}-",
                    "labels": {MANAGED_BY_LABEL: MANAGED_BY_APP},
                    # Not the controller: the Sandbox controller owns the Pod and PVC, and this
                    # reference is for cascading deletion only. The binding lives in the Sandbox's
                    # namespace, which is also where the Action Service matches it to the caller,
                    # so the cascade holds.
                    "ownerReferences": [
                        {
                            "apiVersion": "/".join(SANDBOX_API),
                            "kind": "Sandbox",
                            "name": sandbox,
                            "uid": str(sandbox_uid),
                            "controller": False,
                            "blockOwnerDeletion": False,
                        }
                    ],
                },
                "spec": {
                    "subject": {"sandbox": {"name": sandbox, "uid": str(sandbox_uid)}},
                    "policySets": list(policy_sets),
                },
            },
        )
        return _binding_view(
            ActionPolicyBinding.model_validate(created), _Labelled.model_validate(created).metadata.labels, known
        )

    async def _policy_sets_by_name(self) -> dict[str, ActionPolicySet | InvalidResource]:
        return _by_name(_ResourceList.model_validate(await self._list(POLICY_SETS_PLURAL)).items)

    async def _list(self, plural: str) -> dict[str, object]:
        return await self._custom_objects.list_namespaced_custom_object(*ACTION_POLICY_API, self._namespace, plural)


def _require_known(names: Sequence[str], policy_sets: dict[str, ActionPolicySet | InvalidResource]) -> None:
    if unknown := [name for name in names if name not in policy_sets]:
        raise UnknownPolicySetError(unknown)


# The projection, over objects however they were obtained: one request's list, or the copy
# `live.py` keeps under a watch. Both go through here, so a pushed view and a fetched one are the
# same view.


def action_policy_view(
    bindings: Iterable[dict[str, Any]], policy_sets: Iterable[dict[str, Any]], *, sandbox_uid: UUID, now: datetime
) -> ActionPolicyView:
    """The Sandbox's policy as the Action Service resolves it: bindings by name, each binding's
    sets in its own order, each set's lists in theirs. A binding that does not parse cannot say
    whom it names and is left out, as the service leaves it out; the subject's name is for humans
    and the UID is what matches."""
    sets = _by_name(policy_sets)
    current = sorted(_current(bindings, sandbox_uid=sandbox_uid, now=now), key=lambda pair: pair[0].metadata.name)
    auto_approve_if: list[EffectivePolicyView] = []
    auto_deny_if: list[EffectivePolicyView] = []
    auto_deny_unless: list[EffectivePolicyView] = []
    for binding, _labels in current:
        for name in binding.spec.policy_sets:
            policy_set = sets.get(name)
            if not isinstance(policy_set, ActionPolicySet):
                continue
            for source, target in (
                (policy_set.spec.auto_approve_if, auto_approve_if),
                (policy_set.spec.auto_deny_if, auto_deny_if),
                (policy_set.spec.auto_deny_unless, auto_deny_unless),
            ):
                target.extend(
                    EffectivePolicyView(
                        binding=binding.metadata.name, policy_set=name, index=index, policy=_policy_view(policy)
                    )
                    for index, policy in enumerate(source)
                )
    return ActionPolicyView(
        bindings=[_binding_view(binding, labels, sets) for binding, labels in current],
        auto_approve_if=auto_approve_if,
        auto_deny_if=auto_deny_if,
        auto_deny_unless=auto_deny_unless,
    )


def _current(
    bindings: Iterable[dict[str, Any]], *, sandbox_uid: UUID, now: datetime
) -> Iterator[tuple[ActionPolicyBinding, dict[str, str]]]:
    """The valid, unexpired bindings whose subject is this Sandbox, with their labels."""
    uid = str(sandbox_uid)
    for raw in bindings:
        parsed = parse_binding(raw)
        if isinstance(parsed, InvalidResource):
            continue
        spec = parsed.spec
        if spec.expires_at is not None and spec.expires_at <= now:
            continue
        if isinstance(spec.subject, SandboxSubject) and spec.subject.sandbox.uid == uid:
            yield parsed, _Labelled.model_validate(raw).metadata.labels


def _by_name(policy_sets: Iterable[dict[str, Any]]) -> dict[str, ActionPolicySet | InvalidResource]:
    return {parsed.metadata.name: parsed for parsed in map(parse_policy_set, policy_sets)}


def _binding_view(
    binding: ActionPolicyBinding, labels: dict[str, str], policy_sets: dict[str, ActionPolicySet | InvalidResource]
) -> ActionPolicyBindingView:
    return ActionPolicyBindingView(
        name=binding.metadata.name,
        provenance=_provenance(labels),
        expires_at=binding.spec.expires_at,
        ready=_ready_view(binding.status.ready()),
        policy_sets=[_set_view(policy_sets[name]) for name in binding.spec.policy_sets if name in policy_sets],
        missing_policy_sets=[name for name in binding.spec.policy_sets if name not in policy_sets],
    )


def _provenance(labels: dict[str, str]) -> BindingProvenance:
    if FLUX_KUSTOMIZATION_LABEL in labels:
        return BindingProvenance.GIT
    if labels.get(MANAGED_BY_LABEL) == MANAGED_BY_APP:
        return BindingProvenance.APP
    return BindingProvenance.OPERATOR


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


def _policy_view(policy: ExactActionsPolicy | ArgumentSchemaPolicy) -> ExactActionsView | ArgumentSchemaView:
    actions = {group: sorted(names) for group, names in sorted(policy.actions.items())}
    match policy:
        case ExactActionsPolicy():
            return ExactActionsView(type=PolicyKind.EXACT_ACTIONS, actions=actions)
        case ArgumentSchemaPolicy(argument_schema=schema):
            return ArgumentSchemaView(type=PolicyKind.ARGUMENT_SCHEMA, actions=actions, argument_schema=schema)
